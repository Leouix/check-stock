"""Схема данных.

Главный принцип: остатки нигде не хранятся отдельными полями. Есть только
журнал движений (stock_entries), а любой остаток — это сумма движений по
паре «артикул + место». Поэтому остатки не могут «разъехаться» с историей.

Места хранения одного типа (locations):
  FF       — склад фулфилмента;
  TRANSIT  — товар в пути: между ФФ или на склад маркетплейса (FBO);
  MP       — склад маркетплейса, куда приняли FBO;
  CLIENT   — товар уехал к клиенту по FBS;
  ARCHIVE  — остатки удалённого склада, чтобы они не пропали.

Заказ FBS в статусе RESERVED — это и есть резерв: физически товар лежит на
ФФ, но в «доступно» не попадает. Отдельной таблицы резервов не нужно.
"""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import (
    JSON,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Sequence,
    String,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .db import Base


# Отдельные счётчики документов, чтобы номера были короткими и без дыр
# из-за операций другого типа.
fbs_order_seq = Sequence("fbs_order_seq", metadata=Base.metadata)
fbo_order_seq = Sequence("fbo_order_seq", metadata=Base.metadata)
transfer_seq = Sequence("transfer_seq", metadata=Base.metadata)


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class LocationKind:
    FF = "FF"
    TRANSIT = "TRANSIT"
    MP = "MP"
    CLIENT = "CLIENT"
    ARCHIVE = "ARCHIVE"

    ALL = (FF, TRANSIT, MP, CLIENT, ARCHIVE)


class OperationKind:
    RECEIPT = "RECEIPT"
    FBS_RESERVE = "FBS_RESERVE"
    FBS_CANCEL = "FBS_CANCEL"
    FBS_SHIP = "FBS_SHIP"
    FBO_SHIP = "FBO_SHIP"
    FBO_RECEIVE = "FBO_RECEIVE"
    TRANSFER_SEND = "TRANSFER_SEND"
    TRANSFER_RECEIVE = "TRANSFER_RECEIVE"
    FF_CREATE = "FF_CREATE"
    FF_RENAME = "FF_RENAME"
    FF_DELETE = "FF_DELETE"
    FF_ARCHIVE = "FF_ARCHIVE"


class OrderStatus:
    RESERVED = "RESERVED"
    SHIPPED = "SHIPPED"
    CANCELLED = "CANCELLED"
    RECEIVED = "RECEIVED"


class Product(Base):
    __tablename__ = "products"

    id: Mapped[int] = mapped_column(primary_key=True)
    sku: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    name: Mapped[str] = mapped_column(String(200))


class Location(Base):
    __tablename__ = "locations"
    __table_args__ = (
        CheckConstraint(
            "kind IN ('FF','TRANSIT','MP','CLIENT','ARCHIVE')",
            name="ck_locations_kind",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(200))
    kind: Mapped[str] = mapped_column(String(16), index=True)
    # Ссылка на документ-источник: transfer:12, order:7, archive:3, mp:wb
    ref: Mapped[str | None] = mapped_column(String(64), nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    archived_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class Operation(Base):
    """Одно действие менеджера. Хранит идемпотентность и описание для истории."""

    __tablename__ = "operations"

    id: Mapped[int] = mapped_column(primary_key=True)
    kind: Mapped[str] = mapped_column(String(32), index=True)
    idempotency_key: Mapped[str | None] = mapped_column(
        String(80), unique=True, nullable=True
    )
    note: Mapped[str | None] = mapped_column(String(500), nullable=True)
    payload: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, index=True
    )

    entries: Mapped[list["StockEntry"]] = relationship(back_populates="operation")


class StockEntry(Base):
    """Строка журнала. Только вставка, никогда не обновляется и не удаляется."""

    __tablename__ = "stock_entries"
    __table_args__ = (
        CheckConstraint("qty_delta <> 0", name="ck_stock_entries_nonzero"),
        Index("ix_stock_entries_sku_location", "sku_id", "location_id"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    operation_id: Mapped[int] = mapped_column(
        ForeignKey("operations.id", ondelete="RESTRICT"), index=True
    )
    sku_id: Mapped[int] = mapped_column(ForeignKey("products.id"), index=True)
    location_id: Mapped[int] = mapped_column(ForeignKey("locations.id"), index=True)
    qty_delta: Mapped[int] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    operation: Mapped[Operation] = relationship(back_populates="entries")


class Transfer(Base):
    """Перемещение между ФФ. Товар виден в TRANSIT, пока его не приняли."""

    __tablename__ = "transfers"
    __table_args__ = (
        CheckConstraint("qty > 0", name="ck_transfers_qty_positive"),
        CheckConstraint("status IN ('SENT','RECEIVED')", name="ck_transfers_status"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    number: Mapped[str] = mapped_column(String(32), unique=True)
    sku_id: Mapped[int] = mapped_column(ForeignKey("products.id"))
    from_location_id: Mapped[int] = mapped_column(ForeignKey("locations.id"))
    to_location_id: Mapped[int] = mapped_column(ForeignKey("locations.id"))
    transit_location_id: Mapped[int] = mapped_column(ForeignKey("locations.id"))
    qty: Mapped[int] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String(16), default="SENT")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    received_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class Order(Base):
    """Отправка FBS (клиенту) или FBO (на склад маркетплейса)."""

    __tablename__ = "orders"
    __table_args__ = (
        CheckConstraint("channel IN ('FBS','FBO')", name="ck_orders_channel"),
        CheckConstraint("qty > 0", name="ck_orders_qty_positive"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    number: Mapped[str] = mapped_column(String(32), unique=True)
    channel: Mapped[str] = mapped_column(String(8), index=True)
    sku_id: Mapped[int] = mapped_column(ForeignKey("products.id"))
    ff_location_id: Mapped[int] = mapped_column(ForeignKey("locations.id"))
    transit_location_id: Mapped[int | None] = mapped_column(
        ForeignKey("locations.id"), nullable=True
    )
    destination_location_id: Mapped[int | None] = mapped_column(
        ForeignKey("locations.id"), nullable=True
    )
    qty: Mapped[int] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String(16), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    shipped_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    received_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    cancelled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
