from __future__ import annotations

from urllib.parse import unquote_plus

import pytest
from sqlalchemy import func, select

from app.models import Location, LocationKind, Operation, StockEntry
from app.queries import reconcile, stock_overview
from app.services import (
    InsufficientStock,
    InvalidState,
    cancel_fbs,
    delete_ff,
    physical,
    receive_fbo,
    receive_stock,
    receive_transfer,
    reserve_fbs,
    reserved,
    send_transfer,
    ship_fbo,
    ship_fbs,
    available,
)


def kind_total(session, sku_id: int, kind: str) -> int:
    total = session.execute(
        select(func.coalesce(func.sum(StockEntry.qty_delta), 0))
        .join(Location, Location.id == StockEntry.location_id)
        .where(StockEntry.sku_id == sku_id, Location.kind == kind)
    ).scalar_one()
    return int(total)


def test_starting_balances(shop, session):
    p = shop["products"]
    north, south = shop["north"], shop["south"]

    assert physical(session, p["SKU-101"].id, north.id) == 10
    assert physical(session, p["SKU-101"].id, south.id) == 6
    assert physical(session, p["SKU-102"].id, north.id) == 5
    assert physical(session, p["SKU-102"].id, south.id) == 0
    assert physical(session, p["SKU-103"].id, north.id) == 0
    assert physical(session, p["SKU-103"].id, south.id) == 4

    assert reserved(session, p["SKU-101"].id, north.id) == 0
    assert available(session, p["SKU-101"].id, north.id) == 10

    view = stock_overview(session)
    assert view["totals"] == {"physical": 25, "reserved": 0, "available": 25}


def test_receipt_is_idempotent(shop, session):
    p, north = shop["products"]["SKU-101"], shop["north"]

    first = receive_stock(session, sku_id=p.id, ff_id=north.id, qty=5, idem="receipt-1")
    second = receive_stock(session, sku_id=p.id, ff_id=north.id, qty=5, idem="receipt-1")

    assert first.id == second.id
    assert physical(session, p.id, north.id) == 15
    receipts = session.execute(select(Operation).where(Operation.kind == "RECEIPT")).scalars().all()
    assert len(receipts) == 5  # четыре стартовых прихода + один новый


def test_fbs_reserve_cancel_and_ship(shop, session):
    p, north = shop["products"]["SKU-101"], shop["north"]

    order = reserve_fbs(session, sku_id=p.id, ff_id=north.id, qty=4, idem="fbs-1")
    assert order.status == "RESERVED"
    assert physical(session, p.id, north.id) == 10
    assert reserved(session, p.id, north.id) == 4
    assert available(session, p.id, north.id) == 6

    # повторный резерв с тем же ключом не создаёт второй заказ
    again = reserve_fbs(session, sku_id=p.id, ff_id=north.id, qty=4, idem="fbs-1")
    assert again.id == order.id
    assert reserved(session, p.id, north.id) == 4

    cancel_fbs(session, order_id=order.id)
    assert available(session, p.id, north.id) == 10

    order = reserve_fbs(session, sku_id=p.id, ff_id=north.id, qty=4, idem="fbs-2")
    ship_fbs(session, order_id=order.id)
    session.flush()

    assert physical(session, p.id, north.id) == 6
    assert reserved(session, p.id, north.id) == 0
    assert kind_total(session, p.id, LocationKind.CLIENT) == 4

    # повторное подтверждение отправки не списывает второй раз
    ship_fbs(session, order_id=order.id)
    session.flush()
    assert physical(session, p.id, north.id) == 6
    assert kind_total(session, p.id, LocationKind.CLIENT) == 4

    with pytest.raises(InvalidState):
        cancel_fbs(session, order_id=order.id)


def test_transfer_send_and_receive(shop, session):
    p, north, south = shop["products"]["SKU-101"], shop["north"], shop["south"]

    transfer = send_transfer(
        session,
        sku_id=p.id,
        from_ff_id=north.id,
        to_ff_id=south.id,
        qty=4,
        idem="tr-1",
    )
    session.flush()
    assert transfer.status == "SENT"
    assert physical(session, p.id, north.id) == 6
    assert physical(session, p.id, south.id) == 6
    assert kind_total(session, p.id, LocationKind.TRANSIT) == 4

    receive_transfer(session, transfer_id=transfer.id)
    session.flush()
    assert physical(session, p.id, north.id) == 6
    assert physical(session, p.id, south.id) == 10
    assert kind_total(session, p.id, LocationKind.TRANSIT) == 0

    # повторная приёмка — no-op
    receive_transfer(session, transfer_id=transfer.id)
    session.flush()
    assert physical(session, p.id, south.id) == 10


def test_shortage_on_ff_with_stock_elsewhere(shop, session):
    p, north, south = shop["products"]["SKU-102"], shop["north"], shop["south"]

    with pytest.raises(InsufficientStock) as error:
        reserve_fbs(session, sku_id=p.id, ff_id=south.id, qty=3, idem="shortage-1")

    message = str(error.value)
    assert "Всего по сети 5" in message
    assert "Север — 5" in message
    assert error.value.available == 0

    # лечится перемещением: общего остатка хватает
    transfer = send_transfer(
        session, sku_id=p.id, from_ff_id=north.id, to_ff_id=south.id, qty=3
    )
    receive_transfer(session, transfer_id=transfer.id)
    order = reserve_fbs(session, sku_id=p.id, ff_id=south.id, qty=3)
    assert order.qty == 3
    assert available(session, p.id, south.id) == 0


def test_fbo_ships_to_marketplace(shop, session):
    p, north = shop["products"]["SKU-101"], shop["north"]

    order = ship_fbo(session, sku_id=p.id, ff_id=north.id, qty=5, idem="fbo-1")
    session.flush()
    assert order.status == "SHIPPED"
    assert physical(session, p.id, north.id) == 5
    assert kind_total(session, p.id, LocationKind.TRANSIT) == 5

    receive_fbo(session, order_id=order.id)
    session.flush()
    assert kind_total(session, p.id, LocationKind.TRANSIT) == 0
    assert kind_total(session, p.id, LocationKind.MP) == 5

    # повторная приёмка не задваивает товар на МП
    receive_fbo(session, order_id=order.id)
    session.flush()
    assert kind_total(session, p.id, LocationKind.MP) == 5


def test_delete_ff_moves_stock_to_archive(shop, session):
    p101, p103 = shop["products"]["SKU-101"], shop["products"]["SKU-103"]
    south = shop["south"]

    delete_ff(session, ff_id=south.id)
    session.flush()

    assert south.is_active is False
    assert physical(session, p101.id, south.id) == 0
    assert physical(session, p103.id, south.id) == 0
    assert kind_total(session, p101.id, LocationKind.ARCHIVE) == 6
    assert kind_total(session, p103.id, LocationKind.ARCHIVE) == 4

    # история и физический объём по сети не потерялись
    assert kind_total(session, p101.id, LocationKind.FF) + kind_total(
        session, p101.id, LocationKind.ARCHIVE
    ) == 16

    with pytest.raises(InvalidState):
        receive_stock(session, sku_id=p101.id, ff_id=south.id, qty=1)
    with pytest.raises(InvalidState):
        send_transfer(
            session,
            sku_id=p101.id,
            from_ff_id=shop["north"].id,
            to_ff_id=south.id,
            qty=1,
        )


def test_delete_ff_blocked_by_active_reservation(shop, session):
    p, south = shop["products"]["SKU-101"], shop["south"]
    reserve_fbs(session, sku_id=p.id, ff_id=south.id, qty=2)

    with pytest.raises(InvalidState) as error:
        delete_ff(session, ff_id=south.id)
    assert "FBS-резерв" in str(error.value)
    assert south.is_active is True


def test_reconcile_after_mixed_scenario(shop, session):
    p101, p102 = shop["products"]["SKU-101"], shop["products"]["SKU-102"]
    north, south = shop["north"], shop["south"]

    receive_stock(session, sku_id=p101.id, ff_id=north.id, qty=3)
    order = reserve_fbs(session, sku_id=p101.id, ff_id=north.id, qty=2)
    ship_fbs(session, order_id=order.id)
    transfer = send_transfer(
        session, sku_id=p102.id, from_ff_id=north.id, to_ff_id=south.id, qty=2
    )
    receive_transfer(session, transfer_id=transfer.id)
    fbo = ship_fbo(session, sku_id=p101.id, ff_id=north.id, qty=4)
    receive_fbo(session, order_id=fbo.id)

    session.commit()
    report = reconcile(session)
    assert report["ok"] is True, report["problems"]


def test_http_double_submit_posts_once(client, session, shop):
    p, north = shop["products"]["SKU-101"], shop["north"]
    data = {"sku_id": p.id, "ff_id": north.id, "qty": 5, "idem": "http-receipt-1"}

    first = client.post("/receipts", data=data, follow_redirects=False)
    second = client.post("/receipts", data=data, follow_redirects=False)

    assert first.status_code == 303
    assert second.status_code == 303
    assert physical(session, p.id, north.id) == 15
    receipts = session.execute(
        select(Operation).where(Operation.kind == "RECEIPT")
    ).scalars().all()
    assert len(receipts) == 5  # четыре стартовых прихода + один новый


def test_http_shortage_shows_error_message(client, shop):
    p, south = shop["products"]["SKU-102"], shop["south"]
    response = client.post(
        "/orders/fbs",
        data={"sku_id": p.id, "ff_id": south.id, "qty": 3},
        follow_redirects=False,
    )

    assert response.status_code == 303
    location = unquote_plus(response.headers["location"])
    assert "level=error" in location
    assert "Нехватка" in location
    assert "Всего по сети 5" in location


def test_http_ship_twice_does_not_double_ship(client, session, shop):
    p, north = shop["products"]["SKU-101"], shop["north"]
    order = reserve_fbs(session, sku_id=p.id, ff_id=north.id, qty=2)
    session.commit()

    first = client.post(f"/orders/fbs/{order.id}/ship", follow_redirects=False)
    second = client.post(f"/orders/fbs/{order.id}/ship", follow_redirects=False)

    assert first.status_code == 303
    assert second.status_code == 303
    assert physical(session, p.id, north.id) == 8
    assert kind_total(session, p.id, LocationKind.CLIENT) == 2


def test_api_stock_and_reconcile(client, shop):
    stock = client.get("/api/stock").json()
    assert stock["totals"]["physical"] == 25
    assert {warehouse["name"] for warehouse in stock["warehouses"]} == {"Север", "Юг"}

    report = client.get("/api/reconcile").json()
    assert report["ok"] is True
