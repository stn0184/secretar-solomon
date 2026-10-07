"""Журнал без значений ключей: `cli.HidingFormatter` (инвариант 1)."""

from __future__ import annotations

import logging
import sys
from dataclasses import fields, replace

from solomon.cli import HIDDEN, LOG_FORMAT, HidingFormatter, secrets_of
from solomon.config import Settings
from tests.conftest import TEST_TOKEN

# Адрес файла у Telegram — так токен и попадает в текст ошибок aiohttp.
FILE_URL = f"https://api.telegram.org/file/bot{TEST_TOKEN}/voice/voice-1.oga"


def make_record(message: str, *args: object) -> logging.LogRecord:
    """Строка журнала, какой её пишет aiogram."""
    return logging.LogRecord("aiogram.event", logging.ERROR, __file__, 1, message, args, None)


def test_token_is_hidden_in_the_message() -> None:
    formatter = HidingFormatter(LOG_FORMAT, [TEST_TOKEN])

    line = formatter.format(make_record("Файл не скачан: %s", FILE_URL))

    assert TEST_TOKEN not in line
    assert f"https://api.telegram.org/file/bot{HIDDEN}/voice/voice-1.oga" in line


def test_token_is_hidden_in_the_traceback() -> None:
    formatter = HidingFormatter(LOG_FORMAT, [TEST_TOKEN])
    try:
        raise TimeoutError(f"Connection timeout to host {FILE_URL}")
    except TimeoutError:
        record = logging.LogRecord(
            "aiogram.event", logging.ERROR, __file__, 1, "Cause exception", None, sys.exc_info()
        )

    line = formatter.format(record)

    assert "TimeoutError" in line
    assert TEST_TOKEN not in line


def test_every_key_from_settings_is_hidden(settings: Settings) -> None:
    """Ключ, заведённый в `Settings`, без правки `secrets_of` не останется."""
    settings = replace(settings, instagram_token="IGAA-test-instagram-token")
    keys = [
        getattr(settings, field.name)
        for field in fields(settings)
        if field.name.endswith(("_token", "_key"))
    ]
    formatter = HidingFormatter(LOG_FORMAT, secrets_of(settings))

    line = formatter.format(make_record(" ".join(["%s"] * len(keys)), *keys))

    assert len(keys) == 5
    for key in keys:
        assert key not in line


def test_line_without_keys_is_left_as_is() -> None:
    formatter = HidingFormatter("%(message)s", ["", TEST_TOKEN])

    assert formatter.format(make_record("Команда /start")) == "Команда /start"


def test_token_in_a_request_url_is_hidden_even_if_unknown() -> None:
    """Продлённый ключ Instagram появляется после запуска, а httpx пишет адрес
    каждого запроса — ключ в адресе вырезается по образцу (§26.2)."""
    formatter = HidingFormatter(LOG_FORMAT, [TEST_TOKEN])
    url = (
        "https://graph.instagram.com/refresh_access_token"
        "?grant_type=ig_refresh_token&access_token=IGAAfresh-token_42"
    )

    line = formatter.format(make_record('HTTP Request: GET %s "HTTP/1.1 200 OK"', url))

    assert "IGAAfresh-token_42" not in line
    assert f"grant_type=ig_refresh_token&access_token={HIDDEN} " in line


def test_settings_without_instagram_hide_nothing_extra(settings: Settings) -> None:
    formatter = HidingFormatter("%(message)s", secrets_of(settings))

    assert formatter.format(make_record("Команда /start")) == "Команда /start"
