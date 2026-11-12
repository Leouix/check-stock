"""JSON API — те же операции, что и кнопки в интерфейсе.

Нужен для интеграций и для быстрой проверки сценариев через curl.
Запись коммитится до ответа, чтобы следующий запрос видел изменение.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from . import services
from .db import get_session
from .queries import reconcile, stock_overview

router = APIRouter(prefix="/api", tags=["api"])


class ReceiptIn(BaseModel):
    sku_id: int
    ff_id: int
    qty: int = Field(gt=0)
    note: str | None = None
    idempotency_key: str | None = None


class FbsIn(BaseModel):
    sku_id: int
    ff_id: int
    qty: int = Field(gt=0)
    note: str | None = None
    idempotency_key: str | None = None


class FboIn(FbsIn):
    pass


class TransferIn(BaseModel):
    sku_id: int
    from_ff_id: int
    to_ff_id: int
    qty: int = Field(gt=0)
    note: str | None = None
    idempotency_key: str | None = None


def _write(session: Session, action):
    result = action()
    session.commit()
    return result


def _order_payload(order) -> dict:
    return {
        "id": order.id,
        "number": order.number,
        "channel": order.channel,
        "sku_id": order.sku_id,
        "ff_location_id": order.ff_location_id,
        "qty": order.qty,
        "status": order.status,
    }


def _transfer_payload(transfer) -> dict:
    return {
        "id": transfer.id,
        "number": transfer.number,
        "sku_id": transfer.sku_id,
        "from_location_id": transfer.from_location_id,
        "to_location_id": transfer.to_location_id,
        "qty": transfer.qty,
        "status": transfer.status,
    }


@router.get("/stock")
def get_stock(session: Session = Depends(get_session)):
    data = stock_overview(session)
    return {
        "warehouses": [{"id": ff.id, "name": ff.name} for ff in data["ffs"]],
        "rows": data["rows"],
        "totals": data["totals"],
        "in_transit": data["extras"]["TRANSIT"],
        "marketplace": data["extras"]["MP"],
        "to_customer": data["extras"]["CLIENT"],
        "archive": data["extras"]["ARCHIVE"],
    }


@router.get("/reconcile")
def get_reconcile(session: Session = Depends(get_session)):
    return reconcile(session)


@router.post("/receipts")
def post_receipt(payload: ReceiptIn, session: Session = Depends(get_session)):
    op = _write(
        session,
        lambda: services.receive_stock(
            session,
            sku_id=payload.sku_id,
            ff_id=payload.ff_id,
            qty=payload.qty,
            note=payload.note,
            idem=payload.idempotency_key,
        ),
    )
    return {"operation_id": op.id, "kind": op.kind}


@router.post("/fbs-orders")
def post_fbs(payload: FbsIn, session: Session = Depends(get_session)):
    order = _write(
        session,
        lambda: services.reserve_fbs(
            session,
            sku_id=payload.sku_id,
            ff_id=payload.ff_id,
            qty=payload.qty,
            note=payload.note,
            idem=payload.idempotency_key,
        ),
    )
    return _order_payload(order)


@router.post("/fbs-orders/{order_id}/ship")
def post_fbs_ship(order_id: int, session: Session = Depends(get_session)):
    return _order_payload(
        _write(session, lambda: services.ship_fbs(session, order_id=order_id))
    )


@router.post("/fbs-orders/{order_id}/cancel")
def post_fbs_cancel(order_id: int, session: Session = Depends(get_session)):
    return _order_payload(
        _write(session, lambda: services.cancel_fbs(session, order_id=order_id))
    )


@router.post("/fbo-orders")
def post_fbo(payload: FboIn, session: Session = Depends(get_session)):
    order = _write(
        session,
        lambda: services.ship_fbo(
            session,
            sku_id=payload.sku_id,
            ff_id=payload.ff_id,
            qty=payload.qty,
            note=payload.note,
            idem=payload.idempotency_key,
        ),
    )
    return _order_payload(order)


@router.post("/fbo-orders/{order_id}/receive")
def post_fbo_receive(order_id: int, session: Session = Depends(get_session)):
    return _order_payload(
        _write(session, lambda: services.receive_fbo(session, order_id=order_id))
    )


@router.post("/transfers")
def post_transfer(payload: TransferIn, session: Session = Depends(get_session)):
    transfer = _write(
        session,
        lambda: services.send_transfer(
            session,
            sku_id=payload.sku_id,
            from_ff_id=payload.from_ff_id,
            to_ff_id=payload.to_ff_id,
            qty=payload.qty,
            note=payload.note,
            idem=payload.idempotency_key,
        ),
    )
    return _transfer_payload(transfer)


@router.post("/transfers/{transfer_id}/receive")
def post_transfer_receive(transfer_id: int, session: Session = Depends(get_session)):
    return _transfer_payload(
        _write(session, lambda: services.receive_transfer(session, transfer_id=transfer_id))
    )
