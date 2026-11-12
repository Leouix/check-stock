"""Подключение к базе.

DATABASE_URL задаётся переменной окружения. Значение по умолчанию совпадает
с docker-compose.yml и с локальным запуском через `make db`.
"""

from __future__ import annotations

import os

from sqlalchemy import create_engine
from sqlalchemy.orm import DeclarativeBase, sessionmaker

DATABASE_URL = os.getenv(
    "DATABASE_URL",
    "postgresql+psycopg://checkstock:checkstock@localhost:55432/checkstock",
)

engine = create_engine(DATABASE_URL, pool_pre_ping=True, future=True)
SessionLocal = sessionmaker(bind=engine, expire_on_commit=False)


class Base(DeclarativeBase):
    pass


def get_session():
    """Одна транзакция на запрос: commit при успехе, rollback при ошибке."""
    session = SessionLocal()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()
