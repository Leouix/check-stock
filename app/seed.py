"""Тестовые данные: один магазин, два склада ФФ, три артикула.

Запуск:
    python -m app.seed          # создать схему и загрузить данные, если пусто
    python -m app.seed --reset  # очистить и загрузить заново
"""

from __future__ import annotations

import argparse

from sqlalchemy import text

from .db import Base, SessionLocal, engine
from .models import Location, Product
from .services import create_ff, receive_stock

PRODUCTS = [
    ("SKU-101", "Органайзер", 10, 6),
    ("SKU-102", "Контейнер", 5, 0),
    ("SKU-103", "Корзина", 0, 4),
]


def reset(session) -> None:
    session.execute(
        text(
            "TRUNCATE stock_entries, operations, transfers, orders, products, locations "
            "RESTART IDENTITY CASCADE"
        )
    )
    for sequence in ("fbs_order_seq", "fbo_order_seq", "transfer_seq"):
        session.execute(text(f"ALTER SEQUENCE IF EXISTS {sequence} RESTART WITH 1"))
    session.commit()


def load(session) -> None:
    if session.query(Product).count():
        print("Данные уже загружены. Для перезагрузки: python -m app.seed --reset")
        return

    north = create_ff(session, name="Север")
    south = create_ff(session, name="Юг")

    for sku, name, north_qty, south_qty in PRODUCTS:
        product = Product(sku=sku, name=name)
        session.add(product)
        session.flush()
        if north_qty:
            receive_stock(
                session,
                sku_id=product.id,
                ff_id=north.id,
                qty=north_qty,
                note="Стартовый приход по тестовым данным",
                idem=f"seed:{sku}:north",
            )
        if south_qty:
            receive_stock(
                session,
                sku_id=product.id,
                ff_id=south.id,
                qty=south_qty,
                note="Стартовый приход по тестовым данным",
                idem=f"seed:{sku}:south",
            )

    session.commit()
    print("Загружено: 3 артикула, склады «Север» и «Юг».")
    for sku, name, north_qty, south_qty in PRODUCTS:
        print(f"  {sku} {name}: Север {north_qty}, Юг {south_qty}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Загрузка тестовых данных CheckStock")
    parser.add_argument("--reset", action="store_true", help="очистить базу перед загрузкой")
    args = parser.parse_args()

    Base.metadata.create_all(engine)
    session = SessionLocal()
    try:
        if args.reset:
            reset(session)
        load(session)
    finally:
        session.close()


if __name__ == "__main__":
    main()
