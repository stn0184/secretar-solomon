"""Точки входа: запуск бота и health-запрос к базе.

Неполное окружение — это не ошибка программиста, а незаполненный `.env`:
процесс называет переменную и выходит, трассировку никто не читает.
"""

from __future__ import annotations

import asyncio
import logging
import os
import sys
from pathlib import Path
from typing import NoReturn

from dotenv import load_dotenv

from solomon.config import ConfigError, Settings, load_settings
from solomon.db.client import create_supabase_client
from solomon.db.health import HealthReport, check_database

LOG_FORMAT = "%(asctime)s %(levelname)s %(name)s: %(message)s"


def _repo_root() -> Path:
    # bot/src/solomon/cli.py -> bot/src/solomon -> bot/src -> bot -> корень
    return Path(__file__).resolve().parents[3]


def _setup_logging() -> None:
    logging.basicConfig(
        level=os.environ.get("LOG_LEVEL", "INFO").upper(),
        format=LOG_FORMAT,
        stream=sys.stdout,
    )


def load_environment() -> Settings:
    """Прочитать `.env` из корня репозитория и собрать настройки."""
    load_dotenv(_repo_root() / ".env")
    return load_settings(os.environ)


def _fail(message: str) -> NoReturn:
    print(message, file=sys.stderr)
    raise SystemExit(2)


def _settings_or_exit() -> Settings:
    try:
        return load_environment()
    except ConfigError as error:
        _fail(str(error))


def run_bot() -> None:
    """`solomon-bot` — запустить бота."""
    _setup_logging()
    settings = _settings_or_exit()
    from solomon import runner

    try:
        db = create_supabase_client(settings)
    except Exception as error:  # noqa: BLE001 - на старте нужна причина, а не трассировка
        _fail(f"Не получилось собрать клиент Supabase: {error}")

    try:
        asyncio.run(runner.run(settings, db=db))
    except KeyboardInterrupt:
        print("Остановлено.")


def run_health() -> None:
    """`solomon-health` — одна команда: жива ли база."""
    _setup_logging()
    settings = _settings_or_exit()

    try:
        create_supabase_client(settings)
    except Exception as error:  # noqa: BLE001 - причина понятнее трассировки
        _fail(f"Не получилось собрать клиент Supabase: {error}")

    report: HealthReport = asyncio.run(check_database(settings))
    print(report.message)
    raise SystemExit(0 if report.ok else 1)
