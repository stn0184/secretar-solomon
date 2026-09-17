"""Приём поручения: что уходит в базу и какими словами бот отвечает.

База подменена — проверяется операция, а не сеть: успех, отказ базы и
повторное обновление.
"""

from __future__ import annotations

from solomon import texts
from solomon.config import Settings
from solomon.services.tasks import SUMMARY_LIMIT, TaskService, summarize
from tests.conftest import OWNER_ID, BrokenRecorder, FakeRecorder

SETTINGS = Settings(
    telegram_bot_token="123456:test-token",
    owner_telegram_id=OWNER_ID,
    supabase_url="https://example.supabase.co",
    supabase_service_role_key="service-role-key",
)


def test_short_text_is_retold_as_is() -> None:
    assert summarize("купить лампочку в коридор") == "купить лампочку в коридор"


def test_text_at_the_limit_is_not_cut() -> None:
    text = "я" * SUMMARY_LIMIT

    assert summarize(text) == text


def test_long_text_is_cut_to_the_limit_with_ellipsis() -> None:
    text = "я" * (SUMMARY_LIMIT + 50)

    retold = summarize(text)

    assert retold == "я" * SUMMARY_LIMIT + "…"
    assert len(retold) == SUMMARY_LIMIT + 1


async def test_recorded_message_is_confirmed_in_words() -> None:
    recorder = FakeRecorder()
    service = TaskService(settings=SETTINGS, recorder=recorder)

    outcome = await service.record_from_message(
        chat_id=42, telegram_message_id=7, text="купить лампочку"
    )

    assert outcome.ok
    assert outcome.message == texts.RECORDED.format(text="купить лампочку")
    assert "Записал" in outcome.message


async def test_owner_comes_from_settings_and_text_goes_whole() -> None:
    recorder = FakeRecorder()
    service = TaskService(settings=SETTINGS, recorder=recorder)
    long_text = "я" * (SUMMARY_LIMIT + 50)

    outcome = await service.record_from_message(chat_id=42, telegram_message_id=7, text=long_text)

    # Инвариант 5: в базу поручение уходит целиком, обрезается только пересказ.
    assert recorder.calls == [
        {
            "owner_telegram_id": OWNER_ID,
            "chat_id": 42,
            "telegram_message_id": 7,
            "text": long_text,
        }
    ]
    assert outcome.message == texts.RECORDED.format(text="я" * SUMMARY_LIMIT + "…")


async def test_database_failure_is_not_called_recorded() -> None:
    service = TaskService(settings=SETTINGS, recorder=BrokenRecorder())

    outcome = await service.record_from_message(
        chat_id=42, telegram_message_id=7, text="купить лампочку"
    )

    # Инвариант 4: «Записал» говорится только после ответа базы.
    assert not outcome.ok
    assert "Записал" not in outcome.message
    assert outcome.message == texts.NOT_SAVED


async def test_repeated_update_is_confirmed_again() -> None:
    recorder = FakeRecorder()
    service = TaskService(settings=SETTINGS, recorder=recorder)

    first = await service.record_from_message(
        chat_id=42, telegram_message_id=7, text="купить лампочку"
    )
    second = await service.record_from_message(
        chat_id=42, telegram_message_id=7, text="купить лампочку"
    )

    # Задачу вторую не заводит база (unique в §3.2), а бот отвечает оба раза:
    # первый ответ человек мог не увидеть.
    assert first.ok and second.ok
    assert first.message == second.message
