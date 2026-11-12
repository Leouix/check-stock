"""Сквозная проверка работающего приложения по HTTP (как это делает менеджер)."""

from __future__ import annotations

import os

import httpx
from sqlalchemy import select

from app.db import SessionLocal
from app.models import Location, Product

BASE = os.getenv("CHECKSTOCK_URL", "http://127.0.0.1:8000")
client = httpx.Client(base_url=BASE, follow_redirects=False)

session = SessionLocal()
products = {p.sku: p.id for p in session.execute(select(Product)).scalars()}
ffs = {f.name: f.id for f in session.execute(select(Location).where(Location.kind == "FF")).scalars()}

def stock():
    return client.get("/api/stock").json()

def balance(sku, place):
    data = stock()
    for row in data["rows"]:
        if row["sku"] == sku:
            for ff in data["warehouses"]:
                if ff["name"] == place:
                    return row["cells"][str(ff["id"])] if str(ff["id"]) in row["cells"] else row["cells"][ff["id"]]
    raise AssertionError(f"нет {sku} на {place}")

def check(label, condition, detail=""):
    print(("OK  " if condition else "FAIL") + f" {label}" + (f" — {detail}" if detail else ""))
    if not condition:
        raise SystemExit(1)

# 0. Главная страница
page = client.get("/")
check("главная открывается", page.status_code == 200)
check("в таблице есть SKU-101", "SKU-101" in page.text)
check("видны оба склада", "Север" in page.text and "Юг" in page.text)
check("показан резерв FBS колонкой", "рез." in page.text)

# 1. Приход через форму
r = client.post("/receipts", data={"sku_id": products["SKU-101"], "ff_id": ffs["Север"], "qty": 5, "idem": "verify-receipt-1"})
check("приход: редирект", r.status_code == 303, r.headers.get("location", "")[:60])
b = balance("SKU-101", "Север")
check("приход +5", b["physical"] == 15 and b["available"] == 15, str(b))

# 2. Резерв FBS и нехватка
r = client.post("/api/fbs-orders", json={"sku_id": products["SKU-101"], "ff_id": ffs["Север"], "qty": 4})
order = r.json()
check("резерв FBS создан", r.status_code == 200 and order["status"] == "RESERVED", str(order))
b = balance("SKU-101", "Север")
check("резерв уменьшил доступное, не физическое", b["physical"] == 15 and b["reserved"] == 4 and b["available"] == 11, str(b))

r = client.post("/api/fbs-orders", json={"sku_id": products["SKU-102"], "ff_id": ffs["Юг"], "qty": 3})
check("нехватка на Юге при остатке на Севере", r.status_code == 409 and "Всего по сети 5" in r.text, r.text[:80])

# 3. Отправка FBS дважды
for _ in range(2):
    r = client.post(f"/api/fbs-orders/{order['id']}/ship")
check("отправка FBS идемпотентна", r.status_code == 200 and r.json()["status"] == "SHIPPED")
b = balance("SKU-101", "Север")
check("FBS списал 4 один раз", b["physical"] == 11 and b["reserved"] == 0, str(b))
client_loc = [x for x in stock()["to_customer"] if x["sku"] == "SKU-101"]
check("товар числится у клиента", client_loc and client_loc[0]["qty"] == 4, str(client_loc))

# 4. Перемещение Север -> Юг и двойная приёмка
r = client.post("/api/transfers", json={"sku_id": products["SKU-101"], "from_ff_id": ffs["Север"], "to_ff_id": ffs["Юг"], "qty": 4})
transfer = r.json()
check("перемещение отправлено", r.status_code == 200 and transfer["status"] == "SENT", str(transfer))
north, south = balance("SKU-101", "Север"), balance("SKU-101", "Юг")
check("товар в пути, а не на складе", north["physical"] == 7 and south["physical"] == 6, f"{north} {south}")
check("в пути 4 шт", any(x["qty"] == 4 for x in stock()["in_transit"]))
for _ in range(2):
    r = client.post(f"/api/transfers/{transfer['id']}/receive")
check("приёмка перемещения идемпотентна", r.status_code == 200 and r.json()["status"] == "RECEIVED")
south = balance("SKU-101", "Юг")
check("на Юге 10, не 14", south["physical"] == 10, str(south))

# 5. FBO: отправка и приёмка
r = client.post("/api/fbo-orders", json={"sku_id": products["SKU-101"], "ff_id": ffs["Север"], "qty": 5})
fbo = r.json()
check("FBO отправлен", r.status_code == 200 and fbo["status"] == "SHIPPED")
check("FBO в пути", any(x["qty"] == 5 for x in stock()["in_transit"]))
for _ in range(2):
    r = client.post(f"/api/fbo-orders/{fbo['id']}/receive")
check("приёмка FBO идемпотентна", r.status_code == 200 and r.json()["status"] == "RECEIVED")
mp = [x for x in stock()["marketplace"] if x["sku"] == "SKU-101"]
check("на складе МП 5, не 10", mp and mp[0]["qty"] == 5, str(mp))

# 6. Удаление склада с остатком
r = client.post(f"/warehouses/{ffs['Юг']}/delete")
check("удаление склада", r.status_code == 303)
data = stock()
check("склад исчез из активных", [w["name"] for w in data["warehouses"]] == ["Север"], str(data["warehouses"]))
archive = [x for x in data["archive"] if x["sku"] == "SKU-101"]
check("остаток ушёл в архив", archive and archive[0]["qty"] == 10, str(archive))
check("история сохранилась", "Архив" in client.get("/history").text)

# 7. Попытка операции на удалённом складе
r = client.post("/api/receipts", json={"sku_id": products["SKU-101"], "ff_id": ffs["Юг"], "qty": 1})
check("операции на удалённом складе запрещены", r.status_code == 409 and "удалён" in r.text, r.text[:80])

# 8. Сходимость и страницы
report = client.get("/api/reconcile").json()
check("сверка сходится", report["ok"] is True, str(report["problems"]))
for path in ["/orders", "/warehouses", "/history"]:
    check(f"страница {path} открывается", client.get(path).status_code == 200)

print("\nСквозной сценарий пройден.")
