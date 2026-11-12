from __future__ import annotations

import os
from urllib.parse import urlparse

import psycopg
import pytest

TEST_URL = os.getenv(
    "TEST_DATABASE_URL",
    "postgresql+psycopg://checkstock:checkstock@localhost:55432/checkstock_test",
)
os.environ["DATABASE_URL"] = TEST_URL


def _ensure_database(url: str) -> None:
    parsed = urlparse(url.replace("postgresql+psycopg", "postgresql"))
    name = parsed.path.lstrip("/")
    with psycopg.connect(
        host=parsed.hostname,
        port=parsed.port or 5432,
        user=parsed.username,
        password=parsed.password,
        dbname="postgres",
        autocommit=True,
    ) as conn:
        exists = conn.execute(
            "SELECT 1 FROM pg_database WHERE datname = %s", (name,)
        ).fetchone()
        if not exists:
            conn.execute(f'CREATE DATABASE "{name}"')


_ensure_database(TEST_URL)

from sqlalchemy import text  # noqa: E402

from app.db import Base, SessionLocal, engine  # noqa: E402

TRUNCATE = (
    "TRUNCATE stock_entries, operations, transfers, orders, products, locations "
    "RESTART IDENTITY CASCADE"
)


@pytest.fixture(scope="session", autouse=True)
def schema():
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)
    yield
    Base.metadata.drop_all(engine)


@pytest.fixture()
def session():
    with engine.begin() as conn:
        conn.execute(text(TRUNCATE))
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


@pytest.fixture()
def shop(session):
    """Базовые данные из задания: один магазин, два ФФ, три артикула."""
    from app.models import Product
    from app.services import create_ff, receive_stock

    north = create_ff(session, name="Север")
    south = create_ff(session, name="Юг")

    products = {}
    data = [
        ("SKU-101", "Органайзер", 10, 6),
        ("SKU-102", "Контейнер", 5, 0),
        ("SKU-103", "Корзина", 0, 4),
    ]
    for sku, name, north_qty, south_qty in data:
        product = Product(sku=sku, name=name)
        session.add(product)
        session.flush()
        if north_qty:
            receive_stock(session, sku_id=product.id, ff_id=north.id, qty=north_qty)
        if south_qty:
            receive_stock(session, sku_id=product.id, ff_id=south.id, qty=south_qty)
        products[sku] = product

    session.commit()
    return {"north": north, "south": south, "products": products}


@pytest.fixture()
def client():
    from fastapi.testclient import TestClient

    from app.main import app

    with TestClient(app) as test_client:
        yield test_client
