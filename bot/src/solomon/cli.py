"""Точки входа: запуск бота и health-запрос к базе.

Неполное окружение — это не ошибка программиста, а незаполненный `.env`:
процесс называет переменную и выходит, трассировку никто не читает.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import sys
from collections.abc import Iterable
from pathlib import Path
from typing import NoReturn

from dotenv import load_dotenv

from solomon.config import ConfigError, Settings, load_settings
from solomon.db.client import create_supabase_client
from solomon.db.health import HealthReport, check_database

LOG_FORMAT = "%(asctime)s %(levelname)s %(name)s: %(message)s"
# Чем в журнале заменяется значение ключа.
HIDDEN = "<скрыто>"
# Ключ в адресе запроса: так его передаёт Meta (`refresh_access_token`), и
# httpx пишет адрес каждого запроса в журнал. Продлённый ключ Instagram
# появляется после запуска, и в `secrets_of` его нет — его прячет образец.
TOKEN_IN_URL = re.compile(r"(access_token=)[^&\s\"'<>]+")


class HidingFormatter(logging.Formatter):
    """Строка журнала без значений ключей (инвариант 1).

    Адрес файла у Telegram несёт токен бота (`/file/bot<токен>/…`), а aiohttp
    кладёт адрес в текст своих ошибок. Наш код текст таких ошибок в журнал не
    пишет, но строку пишет и aiogram — с трассировкой. Поэтому строка
    собирается целиком, с аргументами и трассировкой, и только потом из неё
    вырезаются ключи: что бы ни попало в журнал, ключа там не будет. Ключ в
    адресе (`access_token=…`) вырезается по образцу — даже незнакомый.
    """

    def __init__(self, fmt: str, secrets: Iterable[str]) -> None:
        super().__init__(fmt)
        self._secrets = tuple(secret for secret in secrets if secret)

    def format(self, record: logging.LogRecord) -> str:
        line = super().format(record)
        for secret in self._secrets:
            line = line.replace(secret, HIDDEN)
        return TOKEN_IN_URL.sub(rf"\g<1>{HIDDEN}", line)


def secrets_of(settings: Settings) -> tuple[str, ...]:
    """Значения ключей из настроек — то, чего в журнале быть не должно."""
    return (
        settings.telegram_bot_token,
        settings.supabase_service_role_key,
        settings.anthropic_api_key,
        settings.deepgram_api_key,
        settings.instagram_token or "",
    )


def _repo_root() -> Path:
    # bot/src/solomon/cli.py -> bot/src/solomon -> bot/src -> bot -> корень
    return Path(__file__).resolve().parents[3]


def _setup_logging(settings: Settings) -> None:
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(HidingFormatter(LOG_FORMAT, secrets_of(settings)))
    logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO").upper(), handlers=[handler])


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
    # Сначала настройки: журналу нужны значения ключей, чтобы их вырезать.
    settings = _settings_or_exit()
    _setup_logging(settings)
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
    settings = _settings_or_exit()
    _setup_logging(settings)

    try:
        create_supabase_client(settings)
    except Exception as error:  # noqa: BLE001 - причина понятнее трассировки
        _fail(f"Не получилось собрать клиент Supabase: {error}")

    report: HealthReport = asyncio.run(check_database(settings))
    print(report.message)
    raise SystemExit(0 if report.ok else 1)
