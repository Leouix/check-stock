"""Запросы для экранов. Только чтение, без побочных эффектов."""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .models import (
    Location,
    LocationKind,
    Operation,
    OperationKind,
    Order,
    OrderStatus,
    Product,
    StockEntry,
    Transfer,
)

KIND_LABELS = {
    OperationKind.RECEIPT: "Приход",
    OperationKind.FBS_RESERVE: "Резерв FBS",
    OperationKind.FBS_CANCEL: "Отмена резерва",
    OperationKind.FBS_SHIP: "Отправка FBS",
    OperationKind.FBO_SHIP: "Отправка FBO",
    OperationKind.FBO_RECEIVE: "Приёмка FBO",
    OperationKind.TRANSFER_SEND: "Перемещение",
    OperationKind.TRANSFER_RECEIVE: "Приёмка перемещения",
    OperationKind.FF_CREATE: "Склад создан",
    OperationKind.FF_RENAME: "Склад переименован",
    OperationKind.FF_DELETE: "Склад удалён",
    OperationKind.FF_ARCHIVE: "Склад удалён, остатки в архиве",
}

STATUS_LABELS = {
    OrderStatus.RESERVED: "В резерве",
    OrderStatus.SHIPPED: "Отправлен",
    OrderStatus.CANCELLED: "Отменён",
    OrderStatus.RECEIVED: "Принят",
}


def _balances(session: Session) -> dict[tuple[int, int], int]:
    rows = session.execute(
        select(
            StockEntry.sku_id,
            StockEntry.location_id,
            func.sum(StockEntry.qty_delta),
        ).group_by(StockEntry.sku_id, StockEntry.location_id)
    ).all()
    return {(sku_id, loc_id): int(qty) for sku_id, loc_id, qty in rows if qty}


def _reserves(session: Session) -> dict[tuple[int, int], int]:
    rows = session.execute(
        select(Order.sku_id, Order.ff_location_id, func.sum(Order.qty))
        .where(Order.channel == "FBS", Order.status == OrderStatus.RESERVED)
        .group_by(Order.sku_id, Order.ff_location_id)
    ).all()
    return {(sku_id, ff_id): int(qty) for sku_id, ff_id, qty in rows if qty}


def stock_overview(session: Session) -> dict:
    products = session.execute(select(Product).order_by(Product.sku)).scalars().all()
    ffs = (
        session.execute(
            select(Location)
            .where(Location.kind == LocationKind.FF, Location.is_active.is_(True))
            .order_by(Location.name)
        )
        .scalars()
        .all()
    )
    locations = {loc.id: loc for loc in session.execute(select(Location)).scalars()}
    balances = _balances(session)
    reserves = _reserves(session)

    rows = []
    totals = {"physical": 0, "reserved": 0, "available": 0}
    for product in products:
        cells = {}
        product_total = 0
        for ff in ffs:
            physical = balances.get((product.id, ff.id), 0)
            reserved = reserves.get((product.id, ff.id), 0)
            product_total += physical
            cells[ff.id] = {
                "physical": physical,
                "reserved": reserved,
                "available": physical - reserved,
            }
        product_available = sum(cell["available"] for cell in cells.values())
        totals["physical"] += product_total
        totals["reserved"] += sum(cell["reserved"] for cell in cells.values())
        totals["available"] += product_available
        rows.append(
            {
                "sku": product.sku,
                "name": product.name,
                "cells": cells,
                "physical": product_total,
                "reserved": sum(cell["reserved"] for cell in cells.values()),
                "available": product_available,
            }
        )

    extras = {"TRANSIT": [], "MP": [], "CLIENT": [], "ARCHIVE": []}
    for (sku_id, loc_id), qty in sorted(balances.items()):
        loc = locations.get(loc_id)
        if loc is None or loc.kind not in extras:
            continue
        product = session.get(Product, sku_id)
        extras[loc.kind].append(
            {
                "sku": product.sku if product else "—",
                "name": product.name if product else "—",
                "place": loc.name,
                "qty": qty,
            }
        )
    for items in extras.values():
        items.sort(key=lambda item: (item["sku"], item["place"]))

    kind_totals = {
        kind: sum(item["qty"] for item in items) for kind, items in extras.items()
    }

    return {
        "rows": rows,
        "ffs": ffs,
        "totals": totals,
        "extras": extras,
        "kind_totals": kind_totals,
    }


def warehouses_view(session: Session) -> dict:
    ffs = (
        session.execute(
            select(Location)
            .where(Location.kind == LocationKind.FF, Location.is_active.is_(True))
            .order_by(Location.name)
        )
        .scalars()
        .all()
    )
    balances = _balances(session)
    reserves = _reserves(session)
    products = {p.id: p for p in session.execute(select(Product)).scalars()}

    rows = []
    for ff in ffs:
        physical = sum(
            qty for (sku_id, loc_id), qty in balances.items() if loc_id == ff.id
        )
        reserved = sum(
            qty for (sku_id, loc_id), qty in reserves.items() if loc_id == ff.id
        )
        positions = [
            {
                "sku": products[sku_id].sku,
                "name": products[sku_id].name,
                "physical": qty,
                "reserved": reserves.get((sku_id, ff.id), 0),
                "available": qty - reserves.get((sku_id, ff.id), 0),
            }
            for (sku_id, loc_id), qty in sorted(balances.items())
            if loc_id == ff.id and qty
        ]
        rows.append(
            {
                "id": ff.id,
                "name": ff.name,
                "physical": physical,
                "reserved": reserved,
                "available": physical - reserved,
                "positions": positions,
            }
        )

    transfers = session.execute(
        select(Transfer).order_by(Transfer.id.desc()).limit(100)
    ).scalars().all()
    transfer_rows = [
        {
            "id": tr.id,
            "number": tr.number,
            "sku": session.get(Product, tr.sku_id).sku,
            "from": session.get(Location, tr.from_location_id).name,
            "to": session.get(Location, tr.to_location_id).name,
            "qty": tr.qty,
            "status": tr.status,
            "created_at": tr.created_at,
        }
        for tr in transfers
    ]
    return {"warehouses": rows, "transfers": transfer_rows}


def orders_view(session: Session) -> dict:
    orders = session.execute(select(Order).order_by(Order.id.desc()).limit(200)).scalars().all()
    rows = []
    for order in orders:
        rows.append(
            {
                "id": order.id,
                "number": order.number,
                "channel": order.channel,
                "sku": session.get(Product, order.sku_id).sku,
                "name": session.get(Product, order.sku_id).name,
                "ff": session.get(Location, order.ff_location_id).name,
                "qty": order.qty,
                "status": order.status,
                "status_label": STATUS_LABELS.get(order.status, order.status),
                "created_at": order.created_at,
            }
        )
    return {
        "orders": rows,
        "fbs": [row for row in rows if row["channel"] == "FBS"],
        "fbo": [row for row in rows if row["channel"] == "FBO"],
    }


def _op_description(session: Session, op: Operation) -> str:
    payload = op.payload or {}
    sku = session.get(Product, payload.get("sku_id")) if payload.get("sku_id") else None
    sku_label = f"{sku.sku} · {sku.name}" if sku else ""
    qty = payload.get("qty")

    if op.kind == OperationKind.RECEIPT:
        ff = session.get(Location, payload.get("ff_id"))
        return f"{sku_label}: приход {qty} шт на «{ff.name if ff else '—'}»"
    if op.kind == OperationKind.FBS_RESERVE:
        ff = session.get(Location, payload.get("ff_id"))
        return f"{sku_label}: резерв {qty} шт на «{ff.name if ff else '—'}»"
    if op.kind == OperationKind.FBS_CANCEL:
        return f"{sku_label}: отмена резерва {qty} шт"
    if op.kind == OperationKind.FBS_SHIP:
        return f"{sku_label}: отправка клиенту {qty} шт"
    if op.kind == OperationKind.FBO_SHIP:
        ff = session.get(Location, payload.get("ff_id"))
        return f"{sku_label}: отправка на маркетплейс {qty} шт с «{ff.name if ff else '—'}»"
    if op.kind == OperationKind.FBO_RECEIVE:
        return f"{sku_label}: приёмка {qty} шт на складе маркетплейса"
    if op.kind in (OperationKind.TRANSFER_SEND, OperationKind.TRANSFER_RECEIVE):
        source = session.get(Location, payload.get("from_ff_id"))
        target = session.get(Location, payload.get("to_ff_id"))
        route = f"«{source.name}» → «{target.name}»" if source and target else ""
        action = "перемещение" if op.kind == OperationKind.TRANSFER_SEND else "приёмка перемещения"
        return f"{sku_label}: {action} {qty} шт, {route}"
    if op.kind == OperationKind.FF_CREATE:
        return f"Создан склад «{payload.get('name')}»"
    if op.kind == OperationKind.FF_RENAME:
        return f"Склад «{payload.get('old')}» переименован в «{payload.get('new')}»"
    if op.kind in (OperationKind.FF_DELETE, OperationKind.FF_ARCHIVE):
        moved = payload.get("moved") or []
        units = sum(item.get("qty", 0) for item in moved)
        suffix = f", остатки ({units} шт) перенесены в архив" if moved else ""
        return f"Склад «{payload.get('name')}» удалён{suffix}"
    return KIND_LABELS.get(op.kind, op.kind)


def history_view(session: Session, limit: int = 300) -> dict:
    ops = (
        session.execute(
            select(Operation)
            .order_by(Operation.created_at.desc(), Operation.id.desc())
            .limit(limit)
        )
        .scalars()
        .all()
    )
    locations = {loc.id: loc for loc in session.execute(select(Location)).scalars()}
    products = {p.id: p for p in session.execute(select(Product)).scalars()}

    rows = []
    for op in ops:
        entries = []
        for entry in sorted(op.entries, key=lambda item: (item.location_id, item.qty_delta)):
            if entry.qty_delta > 0:
                sign, place = "+", locations.get(entry.location_id)
                entries.append(
                    {
                        "sign": "in",
                        "qty": entry.qty_delta,
                        "place": place.name if place else "—",
                        "sku": products[entry.sku_id].sku if entry.sku_id in products else "—",
                    }
                )
            else:
                place = locations.get(entry.location_id)
                entries.append(
                    {
                        "sign": "out",
                        "qty": abs(entry.qty_delta),
                        "place": place.name if place else "—",
                        "sku": products[entry.sku_id].sku if entry.sku_id in products else "—",
                    }
                )
        rows.append(
            {
                "id": op.id,
                "kind": op.kind,
                "label": KIND_LABELS.get(op.kind, op.kind),
                "description": _op_description(session, op),
                "note": op.note,
                "created_at": op.created_at,
                "entries": entries,
            }
        )
    return {"operations": rows}


def reconcile(session: Session) -> dict:
    """Проверка инвариантов: журнал, остатки и резервы должны сходиться."""
    problems: list[str] = []
    balances = _balances(session)
    locations = {loc.id: loc for loc in session.execute(select(Location)).scalars()}
    products = {p.id: p for p in session.execute(select(Product)).scalars()}

    for (sku_id, loc_id), qty in balances.items():
        if qty < 0:
            problems.append(
                f"Отрицательный остаток: {products.get(sku_id).sku if sku_id in products else sku_id} "
                f"на «{locations.get(loc_id).name if loc_id in locations else loc_id}»: {qty}"
            )

    reserves = _reserves(session)
    for (sku_id, ff_id), qty in reserves.items():
        physical_qty = balances.get((sku_id, ff_id), 0)
        if qty > physical_qty:
            problems.append(
                f"Резерв больше остатка: {products.get(sku_id).sku if sku_id in products else sku_id} "
                f"на «{locations.get(ff_id).name if ff_id in locations else ff_id}»: "
                f"резерв {qty}, физически {physical_qty}"
            )

    for transfer in session.execute(select(Transfer)).scalars():
        in_transit = balances.get((transfer.sku_id, transfer.transit_location_id), 0)
        expected = transfer.qty if transfer.status == "SENT" else 0
        if in_transit != expected:
            problems.append(
                f"Перемещение {transfer.number}: в пути {in_transit}, ожидалось {expected}"
            )

    for order in session.execute(select(Order).where(Order.channel == "FBO")).scalars():
        in_transit = (
            balances.get((order.sku_id, order.transit_location_id), 0)
            if order.transit_location_id
            else 0
        )
        expected = order.qty if order.status == OrderStatus.SHIPPED else 0
        if in_transit != expected:
            problems.append(
                f"Отправка {order.number}: в пути {in_transit}, ожидалось {expected}"
            )

    for order in session.execute(
        select(Order).where(Order.channel == "FBS", Order.status == OrderStatus.SHIPPED)
    ).scalars():
        at_client = (
            balances.get((order.sku_id, order.destination_location_id), 0)
            if order.destination_location_id
            else 0
        )
        if at_client != order.qty:
            problems.append(
                f"Заказ {order.number}: у клиента {at_client}, ожидалось {order.qty}"
            )

    return {
        "ok": not problems,
        "problems": problems,
        "checked_at": datetime.now(timezone.utc).isoformat(),
        "products": len(products),
        "locations": len(locations),
    }
