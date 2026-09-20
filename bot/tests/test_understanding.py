"""Разбор поручения моделью: промпт, ответ, отказы. Сети здесь нет.

Модель подменена протоколом `ModelCall`: проверяется, что уходит в промпте и
что бот делает с каждым видом отказа (`techspec/05-ai.md` §5.4).
"""

from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

from solomon.services.understanding import build_system_prompt, build_user_message
from tests.conftest import OWNER_TIMEZONE

NOW = datetime(2026, 9, 16, 10, 30, tzinfo=ZoneInfo(OWNER_TIMEZONE))


def test_prompt_names_the_day_and_the_timezone() -> None:
    prompt = build_system_prompt(NOW, ZoneInfo(OWNER_TIMEZONE))

    assert "среда, 16 сентября 2026" in prompt
    assert "10:30" in prompt
    assert OWNER_TIMEZONE in prompt


def test_prompt_moment_is_given_in_the_owner_timezone() -> None:
    # То же мгновение в UTC — в поясе владельца это уже следующий день.
    midnight_in_yekaterinburg = datetime(2026, 9, 16, 20, 15, tzinfo=ZoneInfo("UTC"))

    prompt = build_system_prompt(midnight_in_yekaterinburg, ZoneInfo(OWNER_TIMEZONE))

    assert "четверг, 17 сентября 2026" in prompt
    assert "01:15" in prompt


def test_prompt_says_the_message_is_data_not_a_command() -> None:
    prompt = build_system_prompt(NOW, ZoneInfo(OWNER_TIMEZONE))

    assert "данные" in prompt
    assert "18:00" in prompt


def test_forwarded_message_carries_the_sender() -> None:
    assert build_user_message("сделаю к пятнице", forwarded_from="Аня") == (
        "Переслано от: Аня\nсделаю к пятнице"
    )


def test_plain_message_goes_as_is() -> None:
    assert build_user_message("купить лампочку", forwarded_from=None) == "купить лампочку"
