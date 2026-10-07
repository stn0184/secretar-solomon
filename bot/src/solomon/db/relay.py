"""Ключ передачи переписки от Partner Assistant (`techspec/28-relay.md` §28.2).

Единственный модуль `db/` без владельца: в `chat_relays` нет данных человека —
только пропуск источника, хэш его ключа (`techspec/03-schema.md` §3.16). Чей
аккаунт, называет каждое переданное событие, и сверяет его функция приёма
`relay_chat_events`, а не бот. Сам приём бот не зовёт: его зовёт Partner
Assistant под ролью `anon`.
"""

from __future__ import annotations

from typing import Any

from supabase import Client

from solomon.db.rpc import DatabaseError, ask, single_row

REGISTER_FUNCTION = "register_chat_relay"

# Источник — Partner Assistant. Площадка `telegram`, которую включает его
# `linked`, получает подключение `relay:<источник>` (§28.1).
PARTNER = "partner"
PARTNER_CONNECTION = f"relay:{PARTNER}"


def _count(data: Any) -> int:
    """Сколько ключей отозвано; не число — отказ, а не догадка."""
    value = single_row(data)
    if isinstance(value, bool) or not isinstance(value, int):
        raise DatabaseError(f"База не назвала число отозванных ключей: {data!r}.")
    return value


async def register_chat_relay(db: Client, *, name: str, key_hash: str | None) -> int:
    """Ключ источника из окружения бота (§28.2): хэш становится единственным
    действующим, прочие ключи источника отзываются; `None` — отзываются все.
    Возвращает, сколько ключей отозвано сейчас."""
    params = {"name": name, "key_hash": key_hash}
    data = await ask(lambda: db.rpc(REGISTER_FUNCTION, params).execute().data)
    return _count(data)
