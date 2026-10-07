"""Настройки из окружения.

Инвариант 1: секреты только отсюда — ни одного значения в коде и в репозитории.
Список переменных совпадает с `.env.example` в корне репозитория.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


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
    owner_timezone: ZoneInfo
    supabase_url: str
    supabase_service_role_key: str
    anthropic_api_key: str
    # Ключ Deepgram — распознавание голосовых (`techspec/09-voice.md` §9.2).
    deepgram_api_key: str
    # Пусто — ходим в api.anthropic.com; задано — в посредника
    # (`techspec/05-ai.md` §5.1).
    anthropic_base_url: str | None = None
    # Долгоживущий ключ Instagram (`techspec/26-instagram.md` §26.1). Пусто —
    # Direct не опрашивается, бот работает как без него.
    instagram_token: str | None = None
    # Бот в MAX (`techspec/27-max.md` §27.1): его токен и id владельца в MAX.
    # Нет хотя бы одного — MAX не опрашивается, бот работает как без него.
    max_bot_token: str | None = None
    owner_max_id: int | None = None

    @property
    def max_enabled(self) -> bool:
        """Источник MAX включён: заданы и токен, и id владельца (§27.1)."""
        return self.max_bot_token is not None and self.owner_max_id is not None


def _required(env: Mapping[str, str], name: str) -> str:
    value = (env.get(name) or "").strip()
    if not value:
        raise MissingVariable(name)
    return value


def _optional(env: Mapping[str, str], name: str) -> str | None:
    value = (env.get(name) or "").strip()
    return value or None


def load_settings(env: Mapping[str, str]) -> Settings:
    """Собрать настройки из окружения или сказать, чего не хватает."""
    token = _required(env, "TELEGRAM_BOT_TOKEN")
    raw_owner = _required(env, "OWNER_TELEGRAM_ID")
    raw_timezone = _required(env, "OWNER_TIMEZONE")
    url = _required(env, "SUPABASE_URL").rstrip("/")
    service_key = _required(env, "SUPABASE_SERVICE_ROLE_KEY")
    anthropic_key = _required(env, "ANTHROPIC_API_KEY")
    deepgram_key = _required(env, "DEEPGRAM_API_KEY")
    anthropic_base_url = _optional(env, "ANTHROPIC_BASE_URL")
    instagram_token = _optional(env, "INSTAGRAM_TOKEN")
    max_bot_token = _optional(env, "MAX_BOT_TOKEN")
    raw_max_owner = _optional(env, "OWNER_MAX_ID")

    try:
        owner = int(raw_owner)
    except ValueError as error:
        raise InvalidVariable("OWNER_TELEGRAM_ID", "число — Telegram-id владельца") from error

    # Id владельца в MAX необязателен, но заданный с опечаткой останавливает
    # запуск: молча выключенный MAX выглядел бы как «бот не видит сообщений».
    owner_max_id = None
    if raw_max_owner is not None:
        try:
            owner_max_id = int(raw_max_owner)
        except ValueError as error:
            raise InvalidVariable("OWNER_MAX_ID", "число — id владельца в MAX") from error

    # Пояс владельца — без него «в пятницу» не превратить в дату
    # (`techspec/05-ai.md` §5.1), поэтому непонятное значение останавливает
    # запуск так же, как пустое.
    try:
        timezone = ZoneInfo(raw_timezone)
    except (ZoneInfoNotFoundError, ValueError) as error:
        raise InvalidVariable(
            "OWNER_TIMEZONE", "часовой пояс IANA, например Asia/Yekaterinburg"
        ) from error

    return Settings(
        telegram_bot_token=token,
        owner_telegram_id=owner,
        owner_timezone=timezone,
        supabase_url=url,
        supabase_service_role_key=service_key,
        anthropic_api_key=anthropic_key,
        deepgram_api_key=deepgram_key,
        anthropic_base_url=anthropic_base_url.rstrip("/") if anthropic_base_url else None,
        instagram_token=instagram_token,
        max_bot_token=max_bot_token,
        owner_max_id=owner_max_id,
    )
