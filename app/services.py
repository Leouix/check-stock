"""Доменные операции.

Все изменения остатков идут только здесь и только через журнал.
Каждая публичная функция — один атомарный сценарий: проверки, движения,
история. Идемпотентность обеспечивается ключом операции или статусом
документа, поэтому повторный POST не списывает товар второй раз.
"""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import Sequence, func, select
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
    fbo_order_seq,
    fbs_order_seq,
    transfer_seq,
)


class DomainError(Exception):
    status_code = 400


class NotFound(DomainError):
    status_code = 404


class InvalidState(DomainError):
    status_code = 409


class InsufficientStock(DomainError):
    status_code = 409

    def __init__(
        self,
        *,
        sku: Product,
        ff: Location,
        requested: int,
        available: int,
        network: list[tuple[str, int]],
    ) -> None:
        self.sku = sku
        self.ff = ff
        self.requested = requested
        self.available = available
        self.network = network
        total = sum(qty for _, qty in network)
        breakdown = ", ".join(f"{name} — {qty}" for name, qty in network)
        message = (
            f"Нехватка на складе «{ff.name}»: по {sku.sku} запрошено {requested} шт, "
            f"доступно {available}. Всего по сети {total} шт ({breakdown}). "
            f"Переместите товар с другого склада или выберите другой ФФ."
        )
        super().__init__(message)


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


# ---------------------------------------------------------------- проверки


def _validate_qty(qty: int) -> int:
    if qty is None or int(qty) <= 0:
        raise DomainError("Количество должно быть больше нуля")
    if int(qty) > 1_000_000:
        raise DomainError("Слишком большое количество")
    return int(qty)


def get_ff(session: Session, ff_id: int, *, require_active: bool = True) -> Location:
    ff = session.get(Location, ff_id)
    if ff is None or ff.kind != LocationKind.FF:
        raise NotFound("Склад фулфилмента не найден")
    if require_active and not ff.is_active:
        raise InvalidState(f"Склад «{ff.name}» удалён, операции по нему недоступны")
    return ff


def _lock_sku(session: Session, sku_id: int) -> Product:
    """Блокировка по артикулу сериализует параллельные операции с ним."""
    sku = session.execute(
        select(Product).where(Product.id == sku_id).with_for_update()
    ).scalar_one_or_none()
    if sku is None:
        raise NotFound("Артикул не найден")
    return sku


def physical(session: Session, sku_id: int, location_id: int) -> int:
    return int(
        session.execute(
            select(func.coalesce(func.sum(StockEntry.qty_delta), 0)).where(
                StockEntry.sku_id == sku_id,
                StockEntry.location_id == location_id,
            )
        ).scalar_one()
    )


def reserved(session: Session, sku_id: int, ff_id: int) -> int:
    return int(
        session.execute(
            select(func.coalesce(func.sum(Order.qty), 0)).where(
                Order.sku_id == sku_id,
                Order.ff_location_id == ff_id,
                Order.channel == "FBS",
                Order.status == OrderStatus.RESERVED,
            )
        ).scalar_one()
    )


def available(session: Session, sku_id: int, ff_id: int) -> int:
    return physical(session, sku_id, ff_id) - reserved(session, sku_id, ff_id)


def network_availability(session: Session, sku_id: int) -> list[tuple[str, int]]:
    ffs = (
        session.execute(
            select(Location)
            .where(Location.kind == LocationKind.FF, Location.is_active.is_(True))
            .order_by(Location.name)
        )
        .scalars()
        .all()
    )
    return [(ff.name, available(session, sku_id, ff.id)) for ff in ffs]


def _check_available(session: Session, sku: Product, ff: Location, qty: int) -> None:
    free = available(session, sku.id, ff.id)
    if qty > free:
        raise InsufficientStock(
            sku=sku,
            ff=ff,
            requested=qty,
            available=free,
            network=network_availability(session, sku.id),
        )


# ------------------------------------------------------------ запись истории


def _existing_op(session: Session, idem: str | None) -> Operation | None:
    if not idem:
        return None
    return session.execute(
        select(Operation).where(Operation.idempotency_key == idem)
    ).scalar_one_or_none()


def _log(
    session: Session,
    kind: str,
    *,
    idem: str | None = None,
    note: str | None = None,
    payload: dict | None = None,
) -> Operation:
    op = Operation(
        kind=kind,
        idempotency_key=idem,
        note=(note or "").strip() or None,
        payload=payload or {},
    )
    session.add(op)
    session.flush()
    return op


def _entries(session: Session, op: Operation, entries: list[tuple[int, int, int]]) -> None:
    for sku_id, location_id, delta in entries:
        session.add(
            StockEntry(
                operation_id=op.id,
                sku_id=sku_id,
                location_id=location_id,
                qty_delta=delta,
            )
        )
    session.flush()


def _operation_result(session: Session, op: Operation | None, model, key: str):
    if op is None:
        return None
    entity_id = (op.payload or {}).get(key)
    return session.get(model, entity_id) if entity_id else None


def _next_number(session: Session, sequence: Sequence, prefix: str) -> str:
    value = session.execute(select(sequence.next_value())).scalar_one()
    return f"{prefix}-{value:04d}"


def _location(
    session: Session, *, kind: str, ref: str, name: str
) -> Location:
    loc = session.execute(
        select(Location).where(Location.kind == kind, Location.ref == ref)
    ).scalar_one_or_none()
    if loc is None:
        loc = Location(kind=kind, ref=ref, name=name)
        session.add(loc)
        session.flush()
    return loc


# -------------------------------------------------------------------- приход


def receive_stock(
    session: Session,
    *,
    sku_id: int,
    ff_id: int,
    qty: int,
    note: str | None = None,
    idem: str | None = None,
) -> Operation:
    if existing := _existing_op(session, idem):
        return existing
    qty = _validate_qty(qty)
    sku = _lock_sku(session, sku_id)
    ff = get_ff(session, ff_id)
    op = _log(
        session,
        OperationKind.RECEIPT,
        idem=idem,
        note=note,
        payload={"sku_id": sku.id, "ff_id": ff.id, "qty": qty},
    )
    _entries(session, op, [(sku.id, ff.id, qty)])
    return op


# --------------------------------------------------------------- FBS (клиент)


def reserve_fbs(
    session: Session,
    *,
    sku_id: int,
    ff_id: int,
    qty: int,
    note: str | None = None,
    idem: str | None = None,
) -> Order:
    if existing := _existing_op(session, idem):
        order = _operation_result(session, existing, Order, "order_id")
        if order is not None:
            return order
    qty = _validate_qty(qty)
    sku = _lock_sku(session, sku_id)
    ff = get_ff(session, ff_id)
    _check_available(session, sku, ff, qty)

    op = _log(
        session,
        OperationKind.FBS_RESERVE,
        idem=idem,
        note=note,
        payload={"sku_id": sku.id, "ff_id": ff.id, "qty": qty},
    )
    order = Order(
        number=_next_number(session, fbs_order_seq, "FBS"),
        channel="FBS",
        sku_id=sku.id,
        ff_location_id=ff.id,
        qty=qty,
        status=OrderStatus.RESERVED,
    )
    session.add(order)
    session.flush()
    op.payload = {**op.payload, "order_id": order.id}
    return order


def _lock_order(session: Session, order_id: int, channel: str) -> Order:
    order = session.execute(
        select(Order).where(Order.id == order_id).with_for_update()
    ).scalar_one_or_none()
    if order is None or order.channel != channel:
        raise NotFound("Отправка не найдена")
    return order


def ship_fbs(session: Session, *, order_id: int, idem: str | None = None) -> Order:
    order = _lock_order(session, order_id, "FBS")
    if order.status == OrderStatus.SHIPPED:
        return order  # повторное подтверждение — без второго списания
    if order.status != OrderStatus.RESERVED:
        raise InvalidState(
            f"Заказ {order.number} в статусе «{order.status}», отправить нельзя"
        )

    ff = get_ff(session, order.ff_location_id)
    sku = session.get(Product, order.sku_id)
    if physical(session, order.sku_id, ff.id) < order.qty:
        raise InvalidState(
            f"На складе «{ff.name}» физически меньше {order.qty} шт — отправка невозможна"
        )

    client = _location(
        session,
        kind=LocationKind.CLIENT,
        ref=f"order:{order.id}",
        name=f"У клиента · {order.number}",
    )
    order.destination_location_id = client.id
    op = _log(
        session,
        OperationKind.FBS_SHIP,
        idem=idem,
        payload={"order_id": order.id, "sku_id": order.sku_id, "qty": order.qty},
    )
    _entries(
        session,
        op,
        [
            (order.sku_id, ff.id, -order.qty),
            (order.sku_id, client.id, order.qty),
        ],
    )
    order.status = OrderStatus.SHIPPED
    order.shipped_at = utcnow()
    return order


def cancel_fbs(session: Session, *, order_id: int, idem: str | None = None) -> Order:
    order = _lock_order(session, order_id, "FBS")
    if order.status == OrderStatus.CANCELLED:
        return order  # повторная отмена — no-op
    if order.status != OrderStatus.RESERVED:
        raise InvalidState(
            f"Заказ {order.number} уже отправлен, отменить нельзя"
        )
    _log(
        session,
        OperationKind.FBS_CANCEL,
        idem=idem,
        payload={"order_id": order.id, "sku_id": order.sku_id, "qty": order.qty},
    )
    order.status = OrderStatus.CANCELLED
    order.cancelled_at = utcnow()
    return order


# ------------------------------------------------------- FBO (маркетплейс)


def ship_fbo(
    session: Session,
    *,
    sku_id: int,
    ff_id: int,
    qty: int,
    note: str | None = None,
    idem: str | None = None,
) -> Order:
    if existing := _existing_op(session, idem):
        order = _operation_result(session, existing, Order, "order_id")
        if order is not None:
            return order
    qty = _validate_qty(qty)
    sku = _lock_sku(session, sku_id)
    ff = get_ff(session, ff_id)
    _check_available(session, sku, ff, qty)
    mp = _location(
        session, kind=LocationKind.MP, ref="mp:wb", name="Склад маркетплейса (WB)"
    )

    op = _log(
        session,
        OperationKind.FBO_SHIP,
        idem=idem,
        note=note,
        payload={"sku_id": sku.id, "ff_id": ff.id, "qty": qty},
    )
    order = Order(
        number=_next_number(session, fbo_order_seq, "FBO"),
        channel="FBO",
        sku_id=sku.id,
        ff_location_id=ff.id,
        destination_location_id=mp.id,
        qty=qty,
        status=OrderStatus.SHIPPED,
        shipped_at=utcnow(),
    )
    session.add(order)
    session.flush()
    transit = _location(
        session,
        kind=LocationKind.TRANSIT,
        ref=f"order:{order.id}",
        name=f"В пути FBO · {order.number}",
    )
    order.transit_location_id = transit.id
    op.payload = {**op.payload, "order_id": order.id}
    _entries(
        session,
        op,
        [(order.sku_id, ff.id, -order.qty), (order.sku_id, transit.id, order.qty)],
    )
    return order


def receive_fbo(session: Session, *, order_id: int, idem: str | None = None) -> Order:
    order = _lock_order(session, order_id, "FBO")
    if order.status == OrderStatus.RECEIVED:
        return order  # повторная приёмка — без второго движения
    if order.status != OrderStatus.SHIPPED:
        raise InvalidState(f"Отправка {order.number} ещё не отправлена")
    if order.transit_location_id is None or order.destination_location_id is None:
        raise InvalidState(f"У отправки {order.number} не задан маршрут")

    op = _log(
        session,
        OperationKind.FBO_RECEIVE,
        idem=idem,
        payload={"order_id": order.id, "sku_id": order.sku_id, "qty": order.qty},
    )
    _entries(
        session,
        op,
        [
            (order.sku_id, order.transit_location_id, -order.qty),
            (order.sku_id, order.destination_location_id, order.qty),
        ],
    )
    order.status = OrderStatus.RECEIVED
    order.received_at = utcnow()
    return order


# ---------------------------------------------------------- перемещение ФФ


def send_transfer(
    session: Session,
    *,
    sku_id: int,
    from_ff_id: int,
    to_ff_id: int,
    qty: int,
    note: str | None = None,
    idem: str | None = None,
) -> Transfer:
    if existing := _existing_op(session, idem):
        transfer = _operation_result(session, existing, Transfer, "transfer_id")
        if transfer is not None:
            return transfer
    qty = _validate_qty(qty)
    if from_ff_id == to_ff_id:
        raise DomainError("Склады отправки и приёмки должны различаться")
    sku = _lock_sku(session, sku_id)
    source = get_ff(session, from_ff_id)
    target = get_ff(session, to_ff_id)
    _check_available(session, sku, source, qty)

    op = _log(
        session,
        OperationKind.TRANSFER_SEND,
        idem=idem,
        note=note,
        payload={
            "sku_id": sku.id,
            "from_ff_id": source.id,
            "to_ff_id": target.id,
            "qty": qty,
        },
    )
    number = _next_number(session, transfer_seq, "TR")
    transit = _location(
        session,
        kind=LocationKind.TRANSIT,
        ref=f"transfer:{number}",
        name=f"В пути · {source.name} → {target.name} · {number}",
    )
    transfer = Transfer(
        number=number,
        sku_id=sku.id,
        from_location_id=source.id,
        to_location_id=target.id,
        transit_location_id=transit.id,
        qty=qty,
        status="SENT",
    )
    session.add(transfer)
    session.flush()
    op.payload = {**op.payload, "transfer_id": transfer.id}
    _entries(
        session,
        op,
        [(sku.id, source.id, -qty), (sku.id, transit.id, qty)],
    )
    return transfer


def receive_transfer(
    session: Session, *, transfer_id: int, idem: str | None = None
) -> Transfer:
    transfer = session.execute(
        select(Transfer).where(Transfer.id == transfer_id).with_for_update()
    ).scalar_one_or_none()
    if transfer is None:
        raise NotFound("Перемещение не найдено")
    if transfer.status == "RECEIVED":
        return transfer  # повторная приёмка — no-op
    if transfer.status != "SENT":
        raise InvalidState(f"Перемещение {transfer.number} нельзя принять из статуса {transfer.status}")

    op = _log(
        session,
        OperationKind.TRANSFER_RECEIVE,
        idem=idem,
        payload={
            "transfer_id": transfer.id,
            "sku_id": transfer.sku_id,
            "qty": transfer.qty,
            "from_ff_id": transfer.from_location_id,
            "to_ff_id": transfer.to_location_id,
        },
    )
    _entries(
        session,
        op,
        [
            (transfer.sku_id, transfer.transit_location_id, -transfer.qty),
            (transfer.sku_id, transfer.to_location_id, transfer.qty),
        ],
    )
    transfer.status = "RECEIVED"
    transfer.received_at = utcnow()
    return transfer


# -------------------------------------------------------------------- склады


def create_ff(session: Session, *, name: str, note: str | None = None) -> Location:
    name = (name or "").strip()
    if not name:
        raise DomainError("Укажите название склада")
    ff = Location(kind=LocationKind.FF, name=name, is_active=True)
    session.add(ff)
    session.flush()
    _log(
        session,
        OperationKind.FF_CREATE,
        note=note,
        payload={"ff_id": ff.id, "name": name},
    )
    return ff


def rename_ff(
    session: Session, *, ff_id: int, name: str, note: str | None = None
) -> Location:
    name = (name or "").strip()
    if not name:
        raise DomainError("Укажите название склада")
    ff = get_ff(session, ff_id, require_active=False)
    old = ff.name
    ff.name = name
    _log(
        session,
        OperationKind.FF_RENAME,
        note=note,
        payload={"ff_id": ff.id, "old": old, "new": name},
    )
    return ff


def delete_ff(
    session: Session, *, ff_id: int, note: str | None = None
) -> Location:
    """Мягкое удаление: склад архивируется, остатки переезжают в «Архив»."""
    ff = get_ff(session, ff_id, require_active=False)
    if not ff.is_active:
        return ff  # повторное удаление — no-op

    reserved_total = int(
        session.execute(
            select(func.coalesce(func.sum(Order.qty), 0)).where(
                Order.ff_location_id == ff.id,
                Order.channel == "FBS",
                Order.status == OrderStatus.RESERVED,
            )
        ).scalar_one()
    )
    if reserved_total:
        raise InvalidState(
            f"На складе «{ff.name}» есть активные FBS-резервы ({reserved_total} шт). "
            f"Отправьте или отмените их, затем удалите склад."
        )

    balances = session.execute(
        select(
            StockEntry.sku_id,
            func.sum(StockEntry.qty_delta).label("qty"),
        )
        .where(StockEntry.location_id == ff.id)
        .group_by(StockEntry.sku_id)
        .having(func.sum(StockEntry.qty_delta) != 0)
    ).all()

    archive = _location(
        session,
        kind=LocationKind.ARCHIVE,
        ref=f"archive:{ff.id}",
        name=f"Архив · {ff.name}",
    )
    op = _log(
        session,
        OperationKind.FF_ARCHIVE if balances else OperationKind.FF_DELETE,
        note=note,
        payload={
            "ff_id": ff.id,
            "name": ff.name,
            "moved": [{"sku_id": sku_id, "qty": int(qty)} for sku_id, qty in balances],
        },
    )
    for sku_id, qty in balances:
        _entries(
            session,
            op,
            [(sku_id, ff.id, -int(qty)), (sku_id, archive.id, int(qty))],
        )
    ff.is_active = False
    ff.archived_at = utcnow()
    return ff
