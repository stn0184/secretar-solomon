"""Переписка от Partner Assistant (`techspec/28-relay.md`): ключ передачи в
настройках и в базе, вопрос о согласии с припиской, `/chats` и общий путь
§25 для переданного.

Сети нет: клиент Supabase, модель и Telegram подменены. Сам приём
`relay_chat_events` — функция базы, её проверяет `supabase/tests/relay.test.ts`;
здесь — то, что делает бот: пишет хэш ключа при запуске, спрашивает согласия,
разбирает и сообщает. Переписки выдуманные — Игорь и Олег.
"""

from __future__ import annotations

import hashlib
import logging
from dataclasses import replace
from datetime import timedelta
from typing import Any, cast

import pytest
from aiogram import Bot
from aiogram.methods import SendMessage
from aiogram.types import InlineKeyboardMarkup
from supabase import Client

from solomon import texts
from solomon.config import InvalidVariable, Settings, load_settings
from solomon.db import relay as db_relay
from solomon.db.chats import ChatMessage
from solomon.db.rpc import DatabaseError
from solomon.runner import build_dispatcher
from solomon.services import chats, relay
from tests.conftest import (
    OWNER_ID,
    RecordingSession,
    make_callback_update,
    make_settings,
    make_update,
)
from tests.test_chats import (
    MORNING,
    QUIET_LATER,
    TZ,
    FakeChatCall,
    FakeChatStore,
    FakeSender,
    analyzing_service,
    chat_service,
    incoming,
    make_answer,
    make_deal,
)
from tests.test_config import FULL_ENV
from tests.test_reminders import FakeRpcClient

RELAY_KEY = "relay-key-for-tests-only-0123456789abcdef"
PARTNER_BOT = "partner_assistant_bot"
RELAY = "relay:partner"


def relay_settings(**changes: Any) -> Settings:
    return replace(
        make_settings(),
        chat_relay_key=RELAY_KEY,
        partner_bot_username=PARTNER_BOT,
        **changes,
    )


# ------------------------------------------------------ настройки (§28.2)


def test_relay_is_off_without_the_key_or_the_partner_bot() -> None:
    """Нет хотя бы одной переменной — приём выключен, бот работает как раньше."""
    assert not load_settings(FULL_ENV).relay_enabled
    only_key = load_settings({**FULL_ENV, "CHAT_RELAY_KEY": RELAY_KEY})
    only_bot = load_settings({**FULL_ENV, "PARTNER_BOT_USERNAME": PARTNER_BOT})
    blank = load_settings({**FULL_ENV, "CHAT_RELAY_KEY": " ", "PARTNER_BOT_USERNAME": " "})

    assert (only_key.chat_relay_key, only_key.partner_bot_username) == (RELAY_KEY, None)
    assert (only_bot.chat_relay_key, only_bot.partner_bot_username) == (None, PARTNER_BOT)
    assert not only_key.relay_enabled
    assert not only_bot.relay_enabled
    assert (blank.chat_relay_key, blank.partner_bot_username) == (None, None)


def test_relay_is_on_with_both_and_the_username_loses_the_at() -> None:
    settings = load_settings(
        {**FULL_ENV, "CHAT_RELAY_KEY": f" {RELAY_KEY} ", "PARTNER_BOT_USERNAME": f"@{PARTNER_BOT}"}
    )

    assert settings.chat_relay_key == RELAY_KEY
    assert settings.partner_bot_username == PARTNER_BOT
    assert settings.relay_enabled


def test_short_relay_key_stops_the_start() -> None:
    """Короткий ключ подбирается: опечатка или «123» останавливают запуск."""
    with pytest.raises(InvalidVariable) as caught:
        load_settings({**FULL_ENV, "CHAT_RELAY_KEY": "x" * 31})

    assert caught.value.name == "CHAT_RELAY_KEY"
    assert "x" * 31 not in str(caught.value), "ключ в сообщении не повторяется"
    assert load_settings({**FULL_ENV, "CHAT_RELAY_KEY": "x" * 32}).chat_relay_key == "x" * 32


@pytest.mark.parametrize(
    "name", ["pa", "t.me/partner_bot", "partner bot", "1partner_bot", "a" * 33]
)
def test_partner_bot_must_look_like_a_username(name: str) -> None:
    with pytest.raises(InvalidVariable) as caught:
        load_settings({**FULL_ENV, "PARTNER_BOT_USERNAME": name})

    assert caught.value.name == "PARTNER_BOT_USERNAME"


# ------------------------------------------- ключ в базе при запуске (§28.2)


def test_key_hash_is_sha256_hex_as_in_the_database() -> None:
    """Тот же хэш считает `relay_chat_events`: `encode(sha256(convert_to(…)), 'hex')`."""
    assert relay.key_hash("test") == (
        "9f86d081884c7d659a2feaa0c55ad015a3bf4f1b2b0b822cd15d6c15b0f00a08"
    )
    assert relay.key_hash("ключ") == hashlib.sha256("ключ".encode()).hexdigest()


def test_partner_link_opens_the_share_question() -> None:
    assert relay.partner_link(PARTNER_BOT) == (
        "https://t.me/partner_assistant_bot?start=share_solomon"
    )


async def test_register_sends_the_source_and_the_hash_and_reads_the_count() -> None:
    client = FakeRpcClient({"register_chat_relay": 1})

    revoked = await db_relay.register_chat_relay(
        cast(Client, client), name="partner", key_hash="ab" * 32
    )

    assert revoked == 1
    assert client.calls == ["register_chat_relay"]
    assert client.params == [{"name": "partner", "key_hash": "ab" * 32}]


@pytest.mark.parametrize("answer", [None, "1", True, [{"x": 1}]])
async def test_register_with_an_unclear_answer_is_a_refusal(answer: object) -> None:
    with pytest.raises(DatabaseError):
        await db_relay.register_chat_relay(
            cast(Client, FakeRpcClient({"register_chat_relay": answer})),
            name="partner",
            key_hash=None,
        )


async def test_start_writes_the_hash_of_the_key_and_never_the_key(
    caplog: pytest.LogCaptureFixture,
) -> None:
    client = FakeRpcClient({"register_chat_relay": 0})
    caplog.set_level(logging.INFO)

    assert await relay.register_relay(relay_settings(), cast(Client, client)) is True

    assert client.params == [{"name": "partner", "key_hash": relay.key_hash(RELAY_KEY)}]
    assert RELAY_KEY not in caplog.text
    assert relay.key_hash(RELAY_KEY) not in caplog.text


async def test_start_without_the_relay_revokes_every_key() -> None:
    """Приём выключен — в базе не остаётся действующего ключа (§28.2)."""
    for settings in (make_settings(), replace(make_settings(), chat_relay_key=RELAY_KEY)):
        client = FakeRpcClient({"register_chat_relay": 1})

        assert await relay.register_relay(settings, cast(Client, client)) is True

        assert client.params == [{"name": "partner", "key_hash": None}]


async def test_start_survives_a_database_refusal(caplog: pytest.LogCaptureFixture) -> None:
    client = FakeRpcClient(broken=True)

    assert await relay.register_relay(relay_settings(), cast(Client, client)) is False

    assert "Ключ передачи переписки не записан" in caplog.text
    assert RELAY_KEY not in caplog.text


# ---------------------------------------------- linked и согласие (§28.1)


def relayed_store() -> FakeChatStore:
    """Partner Assistant прислал `linked`: площадка telegram включена через него,
    вопрос ещё не задан — так её оставляет функция базы."""
    store = FakeChatStore()
    store.sources["telegram"] = {
        "connection_id": RELAY,
        "is_enabled": True,
        "asked_at": None,
        "consented_at": None,
        "declined_at": None,
    }
    return store


def test_consent_question_through_the_partner_says_who_passes_the_chats() -> None:
    question = texts.consent_question("telegram", relay=True)

    assert "переписку передаёт Partner Assistant" in question
    assert "7 дней" in question and "Claude через посредника, Deepgram" in question
    assert question.endswith("Согласны?")
    assert texts.consent_question("telegram") != question


async def test_tick_asks_the_relayed_consent_with_the_partner_note() -> None:
    store = relayed_store()
    sender = FakeSender()

    assert await chat_service(store, sender).tick(QUIET_LATER) == 1

    assert sender.texts == [texts.consent_question("telegram", relay=True)]
    _, buttons = sender.sent[0]
    assert [button.data for button in buttons] == ["consent:telegram:yes", "consent:telegram:no"]
    assert store.sources["telegram"]["asked_at"] is not None


async def test_relayed_consent_answer_keeps_the_note_and_says_where_to_stop() -> None:
    store = relayed_store()
    service = chat_service(store)

    agreed = await service.answer_consent("telegram", True)
    refused = await service.answer_consent("telegram", False)

    assert agreed.message == texts.consent_answered("telegram", agreed=True, relay=True)
    assert agreed.message.startswith(texts.consent_question("telegram", relay=True))
    assert refused.message == texts.consent_answered("telegram", agreed=False, relay=True)
    assert "в Partner Assistant" in refused.message
    assert "Автоматизация чатов" not in refused.message


async def test_consent_button_through_telegram_edits_the_relayed_question(
    bot: Bot, session: RecordingSession
) -> None:
    store = relayed_store()
    dispatcher = build_dispatcher(relay_settings(), chats=chat_service(store))

    await dispatcher.feed_update(
        bot,
        make_callback_update(
            "consent:telegram:yes", text=texts.consent_question("telegram", relay=True)
        ),
    )

    assert store.sources["telegram"]["consented_at"] is not None
    assert [edit.text for edit in session.edits] == [
        texts.consent_answered("telegram", agreed=True, relay=True)
    ]


async def test_old_direct_connection_switched_off_leaves_the_relay_alone() -> None:
    """Partner Assistant занял место Соломона в «Автоматизации чатов»: Telegram
    сообщает, что прежнее прямое подключение выключено. Площадку держит
    Partner Assistant — её это не трогает (§28.1)."""
    store = relayed_store()
    store.sources["telegram"]["consented_at"] = QUIET_LATER

    await chat_service(store).connected(
        platform="telegram", connection_id="biz-777", user_id=OWNER_ID, enabled=False
    )

    assert store.sources["telegram"]["connection_id"] == RELAY
    assert store.sources["telegram"]["is_enabled"] is True


async def test_current_direct_connection_switched_off_still_stops_the_platform() -> None:
    store = FakeChatStore()
    store.consent()

    await chat_service(store).connected(
        platform="telegram", connection_id="biz-777", user_id=OWNER_ID, enabled=False
    )

    assert store.sources["telegram"]["is_enabled"] is False


# ------------------------------------------- переданное — общий путь (§28.3)


async def relayed_chat(store: FakeChatStore, *lines: tuple[str, str]) -> None:
    """Переписка с Игорем, принятая функцией базы: подключение `relay:partner`,
    согласие есть. В тесте бота её кладёт тот же приём сервиса."""
    store.consent(connection=RELAY)
    service = chat_service(store)
    for index, (direction, text) in enumerate(lines):
        await service.receive(
            incoming(
                text,
                external_id=str(200 + index),
                direction=direction,
                sent_at=MORNING + timedelta(minutes=index),
                connection=RELAY,
            )
        )


async def test_relayed_promise_is_reported_like_a_direct_one() -> None:
    store = FakeChatStore()
    await relayed_chat(store, ("in", "Пришлёшь расчёт до пятницы?"))
    sender = FakeSender()
    call = FakeChatCall(make_answer(deals=[make_deal()]))

    assert await analyzing_service(store, call).analyze_due() == 1
    assert await analyzing_service(store, sender=sender).send_reports(QUIET_LATER) == 1

    [(text, buttons)] = sender.sent
    assert text.startswith("Из переписки с Игорем (Telegram) записал:")
    # Имени пользователя Partner Assistant не передаёт — «Открыть чат» по id
    # (этап 030).
    assert [(button.text, button.url) for button in buttons] == [
        (texts.drop_button(1, single=True), None),
        (texts.OPEN_CHAT_BUTTON, "tg://user?id=1001"),
    ]


async def test_reply_by_the_partner_is_the_owner_line_and_the_owner_promise() -> None:
    """`by_assistant` приходит `out`: в переписке это строка владельца, и
    обещанное в ней — обещание владельца (§28.3)."""
    store = FakeChatStore()
    await relayed_chat(
        store,
        ("in", "Пришлёшь расчёт до пятницы?"),
        ("out", "Да, пришлю в пятницу"),
    )
    call = FakeChatCall(make_answer(deals=[make_deal(promise="mine")], waiting=None))
    sender = FakeSender()

    await analyzing_service(store, call).analyze_due()
    await analyzing_service(store, sender=sender).send_reports(QUIET_LATER)

    [(_, text)] = call.calls
    assert "Владелец: Да, пришлю в пятницу" in text
    assert "(вы обещали)" in sender.texts[0]
    assert store.thread()["waiting_since"] is None


def test_relayed_voice_reads_by_the_transcript_or_as_unheard() -> None:
    """Голос Соломон не скачивает (`file_id` чужого бота): расшифровка Partner
    Assistant — строкой голосового, без неё — пометка общего пути (§25.2)."""
    heard = ChatMessage(
        id="m1",
        direction="in",
        sender="Игорь Петров",
        sent_at=MORNING,
        kind="voice",
        text="Перезвоню после обеда",
        erased=False,
    )
    silent = replace(heard, text="")

    assert chats.message_line(heard, MORNING, TZ).endswith(
        "Игорь Петров: [голосовое] Перезвоню после обеда"
    )
    assert chats.message_line(silent, MORNING, TZ).endswith(
        "Игорь Петров: [голосовое, не расслышал]"
    )


# --------------------------------------------------------- /chats (§28.1)


async def test_chats_command_explains_both_ways_with_a_partner_button(
    bot: Bot, session: RecordingSession
) -> None:
    dispatcher = build_dispatcher(relay_settings())

    await dispatcher.feed_update(bot, make_update("/chats"))

    [sent] = [method for method in session.sent if isinstance(method, SendMessage)]
    assert sent.text == texts.chats_help(relay=True)
    assert "подключите меня в «Автоматизации чатов»" in sent.text
    assert "Partner Assistant" in sent.text
    markup = sent.reply_markup
    assert isinstance(markup, InlineKeyboardMarkup)
    [[button]] = markup.inline_keyboard
    assert button.text == texts.PARTNER_BUTTON
    assert button.url == "https://t.me/partner_assistant_bot?start=share_solomon"
    assert button.callback_data is None


async def test_chats_command_without_the_relay_offers_only_the_direct_way(
    bot: Bot, session: RecordingSession
) -> None:
    dispatcher = build_dispatcher(replace(make_settings(), chat_relay_key=RELAY_KEY))

    await dispatcher.feed_update(bot, make_update("/chats"))

    [sent] = [method for method in session.sent if isinstance(method, SendMessage)]
    assert sent.text == texts.chats_help(relay=False)
    assert "Partner Assistant" not in sent.text
    assert sent.reply_markup is None


def test_help_names_the_chats_command() -> None:
    assert "/chats — " in texts.HELP
