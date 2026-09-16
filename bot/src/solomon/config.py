"""Настройки из окружения.

Инвариант 1: секреты только отсюда — ни одного значения в коде и в репозитории.
Список переменных совпадает с `.env.example` в корне репозитория.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass


class ConfigError(Exception):
    """Окружение не годится для запуска.

    Наружу выходит текстом для человека, а не трассировкой: точка входа ловит
    это исключение и завершает процесс с понятным сообщением.
    """


class MissingVariable(ConfigError):
    """Переменной нет или она пустая."""

    def __init__(self, name: str) -> None:
        self.name = name
        super().__init__(
            f"Не хватает переменной окружения: {name}. "
            "Заполните .env в корне репозитория по образцу .env.example."
        )


class InvalidVariable(ConfigError):
    """Переменная есть, но значение не годится."""

    def __init__(self, name: str, expected: str) -> None:
        self.name = name
        super().__init__(f"Переменная окружения {name} задана неверно: ожидается {expected}.")


@dataclass(frozen=True, slots=True)
class Settings:
    """Всё, что боту нужно знать снаружи."""

    telegram_bot_token: str
    owner_telegram_id: int
    supabase_url: str
    supabase_service_role_key: str


def _required(env: Mapping[str, str], name: str) -> str:
    value = (env.get(name) or "").strip()
    if not value:
        raise MissingVariable(name)
    return value


def load_settings(env: Mapping[str, str]) -> Settings:
    """Собрать настройки из окружения или сказать, чего не хватает."""
    token = _required(env, "TELEGRAM_BOT_TOKEN")
    raw_owner = _required(env, "OWNER_TELEGRAM_ID")
    url = _required(env, "SUPABASE_URL").rstrip("/")
    service_key = _required(env, "SUPABASE_SERVICE_ROLE_KEY")

    try:
        owner = int(raw_owner)
    except ValueError as error:
        raise InvalidVariable("OWNER_TELEGRAM_ID", "число — Telegram-id владельца") from error

    return Settings(
        telegram_bot_token=token,
        owner_telegram_id=owner,
        supabase_url=url,
        supabase_service_role_key=service_key,
    )
