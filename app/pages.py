"""HTML-экраны. Все действия — обычные POST-формы с redirect после успеха."""

from __future__ import annotations

from pathlib import Path
from urllib.parse import urlencode
from uuid import uuid4
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from . import services
from .config import READ_ONLY_BANNER, read_only_enabled
from .db import get_session
from .models import Location, LocationKind, Product
from .queries import history_view, orders_view, stock_overview, warehouses_view
from .services import DomainError

router = APIRouter()
templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))
templates.env.filters["dt"] = lambda value: value.astimezone(
    ZoneInfo("Asia/Yerevan")
).strftime("%d.%m.%Y %H:%M")
# Флаг читается на каждый рендер: в демо-режиме шаблоны показывают плашку
# и гасят кнопки записи, в остальных запусках ничего не меняется.
templates.env.globals["read_only"] = read_only_enabled
templates.env.globals["read_only_banner"] = READ_ONLY_BANNER


def _redirect(path: str, message: str | None = None, level: str = "ok"):
    if message:
        path = f"{path}?{urlencode({'msg': message, 'level': level})}"
    return RedirectResponse(path, status_code=303)


def _perform(session: Session, path: str, ok_message: str, action) -> RedirectResponse:
    try:
        action()
        # Коммитим до ответа: следующий запрос не должен обгонять транзакцию.
        session.commit()
    except DomainError as exc:
        session.rollback()
        return _redirect(path, str(exc), "error")
    return _redirect(path, ok_message)


def _form_data(session: Session) -> dict:
    return {
        "products": session.query(Product).order_by(Product.sku).all(),
        "ffs": session.query(Location)
        .filter(Location.kind == LocationKind.FF, Location.is_active.is_(True))
        .order_by(Location.name)
        .all(),
        "idem": uuid4().hex,
    }


@router.get("/")
def stock_page(request: Request, session: Session = Depends(get_session)):
    context = {
        "request": request,
        "page": "stock",
        **stock_overview(session),
        **_form_data(session),
    }
    return templates.TemplateResponse(request, "stock.html", context)


@router.post("/receipts")
def receipt(
    sku_id: int = Form(...),
    ff_id: int = Form(...),
    qty: int = Form(...),
    note: str = Form(""),
    idem: str = Form(""),
    session: Session = Depends(get_session),
):
    return _perform(
        session,
        "/",
        "Приход оформлен",
        lambda: services.receive_stock(
            session, sku_id=sku_id, ff_id=ff_id, qty=qty, note=note or None, idem=idem or None
        ),
    )


@router.get("/orders")
def orders_page(request: Request, session: Session = Depends(get_session)):
    context = {
        "request": request,
        "page": "orders",
        **orders_view(session),
        **_form_data(session),
    }
    return templates.TemplateResponse(request, "orders.html", context)


@router.post("/orders/fbs")
def create_fbs(
    sku_id: int = Form(...),
    ff_id: int = Form(...),
    qty: int = Form(...),
    note: str = Form(""),
    idem: str = Form(""),
    session: Session = Depends(get_session),
):
    return _perform(
        session,
        "/orders",
        "Резерв FBS создан",
        lambda: services.reserve_fbs(
            session, sku_id=sku_id, ff_id=ff_id, qty=qty, note=note or None, idem=idem or None
        ),
    )


@router.post("/orders/fbs/{order_id}/ship")
def ship_fbs(order_id: int, session: Session = Depends(get_session)):
    return _perform(
        session,
        "/orders",
        "Заказ уехал к клиенту",
        lambda: services.ship_fbs(session, order_id=order_id),
    )


@router.post("/orders/fbs/{order_id}/cancel")
def cancel_fbs(order_id: int, session: Session = Depends(get_session)):
    return _perform(
        session,
        "/orders",
        "Резерв снят",
        lambda: services.cancel_fbs(session, order_id=order_id),
    )


@router.post("/orders/fbo")
def create_fbo(
    sku_id: int = Form(...),
    ff_id: int = Form(...),
    qty: int = Form(...),
    note: str = Form(""),
    idem: str = Form(""),
    session: Session = Depends(get_session),
):
    return _perform(
        session,
        "/orders",
        "Товар отправлен на маркетплейс",
        lambda: services.ship_fbo(
            session, sku_id=sku_id, ff_id=ff_id, qty=qty, note=note or None, idem=idem or None
        ),
    )


@router.post("/orders/fbo/{order_id}/receive")
def receive_fbo(order_id: int, session: Session = Depends(get_session)):
    return _perform(
        session,
        "/orders",
        "Приёмка на складе маркетплейса оформлена",
        lambda: services.receive_fbo(session, order_id=order_id),
    )


@router.get("/warehouses")
def warehouses_page(request: Request, session: Session = Depends(get_session)):
    data = _form_data(session)
    data["default_to_ff_id"] = (
        data["ffs"][1].id if len(data["ffs"]) > 1 else (data["ffs"][0].id if data["ffs"] else None)
    )
    context = {
        "request": request,
        "page": "warehouses",
        **warehouses_view(session),
        **data,
        "transfer_idem": uuid4().hex,
    }
    return templates.TemplateResponse(request, "warehouses.html", context)


@router.post("/warehouses")
def create_warehouse(
    name: str = Form(...),
    session: Session = Depends(get_session),
):
    return _perform(
        session, "/warehouses", "Склад создан", lambda: services.create_ff(session, name=name)
    )


@router.post("/warehouses/{ff_id}/rename")
def rename_warehouse(
    ff_id: int,
    name: str = Form(...),
    session: Session = Depends(get_session),
):
    return _perform(
        session,
        "/warehouses",
        "Склад переименован",
        lambda: services.rename_ff(session, ff_id=ff_id, name=name),
    )


@router.post("/warehouses/{ff_id}/delete")
def delete_warehouse(ff_id: int, session: Session = Depends(get_session)):
    return _perform(
        session,
        "/warehouses",
        "Склад удалён, остатки сохранены в архиве",
        lambda: services.delete_ff(session, ff_id=ff_id),
    )


@router.post("/transfers")
def create_transfer(
    sku_id: int = Form(...),
    from_ff_id: int = Form(...),
    to_ff_id: int = Form(...),
    qty: int = Form(...),
    note: str = Form(""),
    idem: str = Form(""),
    session: Session = Depends(get_session),
):
    return _perform(
        session,
        "/warehouses",
        "Перемещение отправлено, товар в пути",
        lambda: services.send_transfer(
            session,
            sku_id=sku_id,
            from_ff_id=from_ff_id,
            to_ff_id=to_ff_id,
            qty=qty,
            note=note or None,
            idem=idem or None,
        ),
    )


@router.post("/transfers/{transfer_id}/receive")
def receive_transfer(transfer_id: int, session: Session = Depends(get_session)):
    return _perform(
        session,
        "/warehouses",
        "Перемещение принято",
        lambda: services.receive_transfer(session, transfer_id=transfer_id),
    )


@router.get("/history")
def history_page(request: Request, session: Session = Depends(get_session)):
    context = {
        "request": request,
        "page": "history",
        **history_view(session),
    }
    return templates.TemplateResponse(request, "history.html", context)
