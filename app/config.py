"""Настройки демо-стенда.

DEMO_READ_ONLY включает режим «только просмотр»: любые не-GET запросы
отклоняются до обработки, данные не меняются. В обычном запуске флаг выключен,
в docker-compose.demo.yml — включён.
"""

from __future__ import annotations

import os

READ_ONLY_FLAG = "DEMO_READ_ONLY"
READ_ONLY_MESSAGE = "Демо: только просмотр, изменения отключены"
READ_ONLY_BANNER = "Демо-режим: данные можно смотреть, но нельзя менять."

_TRUE = {"1", "true", "yes", "on"}


def read_only_enabled() -> bool:
    """Флаг читается на каждый запрос — тесты могут переключать его без перезапуска."""
    return os.getenv(READ_ONLY_FLAG, "").strip().lower() in _TRUE
