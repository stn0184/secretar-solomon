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
from typing import Any, cast

import pytest
from supabase import Client

from solomon.config import InvalidVariable, Settings, load_settings
from solomon.db import relay as db_relay
from solomon.db.rpc import DatabaseError
from solomon.services import relay
from tests.conftest import make_settings
from tests.test_config import FULL_ENV
from tests.test_reminders import FakeRpcClient

RELAY_KEY = "relay-key-for-tests-only-0123456789abcdef"
PARTNER_BOT = "partner_assistant_bot"


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
