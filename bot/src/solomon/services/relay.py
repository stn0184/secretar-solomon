"""Переписка от Partner Assistant (`techspec/28-relay.md`): ключ передачи и
ссылка на Partner Assistant.

Partner Assistant подключён к аккаунту человека в «Автоматизации чатов» и с
его разрешения передаёт Соломону переписку функцией базы `relay_chat_events`
по ключу передачи. Бот в этом приёме не участвует: событие ложится в базу
само, а дальше — общий путь чатов (`services/chats.py`): вопрос о согласии
задаёт тик, разбор и сообщения — как у прямого подключения.

Здесь — то, что бот делает сам: при запуске пишет в базу хэш ключа из
окружения (сам ключ туда не уходит) и собирает ссылку на Partner Assistant
для `/chats`. В журнал — ни ключа, ни хэша.
"""

from __future__ import annotations

import hashlib
import logging

from supabase import Client

from solomon.config import Settings
from solomon.db import relay as db_relay
from solomon.db.rpc import DatabaseError

logger = logging.getLogger(__name__)

# Partner Assistant по этой ссылке спрашивает «Передавать переписку
# Соломону?» (его сторона, §28.5).
SHARE_START = "share_solomon"


def key_hash(key: str) -> str:
    """SHA-256 ключа передачи в hex — так же его считает функция приёма:
    `encode(sha256(convert_to(ключ, 'UTF8')), 'hex')`."""
    return hashlib.sha256(key.encode("utf-8")).hexdigest()


def partner_link(username: str) -> str:
    """Ссылка на Partner Assistant с вопросом о передаче (§28.1)."""
    return f"https://t.me/{username}?start={SHARE_START}"


async def register_relay(settings: Settings, db: Client) -> bool:
    """Ключ передачи — в базу при запуске (§28.2).

    Приём включён — хэш ключа из окружения становится единственным
    действующим, прежние отзываются: смена ключа — заменить его в обоих
    `.env`. Выключен (нет ключа или имени бота) — отзываются все, и Partner
    Assistant получает отказ. Не записалось — строка в журнале, бот работает
    дальше: в базе остаётся прежнее состояние ключей.
    """
    key = settings.chat_relay_key if settings.relay_enabled else None
    try:
        revoked = await db_relay.register_chat_relay(
            db,
            name=db_relay.PARTNER,
            key_hash=key_hash(key) if key is not None else None,
        )
    except DatabaseError as error:
        logger.error("Ключ передачи переписки не записан в базу: %s", error)
        return False
    state = "включена" if key is not None else "выключена"
    logger.info("Передача переписки от Partner Assistant %s; ключей отозвано: %s", state, revoked)
    return True
