"""Разговор — чистые функции (`techspec/17-conversation.md`).

Строки недавнего разговора (блок 6 промпта, §17.3), их обрезка и предел
блока; ответ разговора из поля `reply_hint` и проверка, не говорит ли он
о действии, которого не было (§17.2, инвариант 4). Сети и базы здесь нет:
сообщения приходят готовыми, а модуль знает о них только поля.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime, tzinfo
from typing import Protocol

# Сколько последних сообщений берётся в блок (§17.3).
RECENT_LIMIT = 10
# Предел текста одного сообщения и одного ответа бота в блоке: длиннее —
# режется посередине.
TEXT_LIMIT = 500
# Предел всего блока: не влезает — выпадают самые старые сообщения.
BLOCK_LIMIT = 4000
# Предел ответа разговора: сообщение Telegram вмещает до 4096 знаков (§17.2).
REPLY_LIMIT = 3500
ELLIPSIS = "…"
HEADER = "Недавний разговор (последний час, от старых к новым):"
BOT_NAME = "Соломон"
PHOTO_KIND = "photo"
NO_CAPTION = "без подписи"
NO_TEXT = "без текста"
# Слова, которыми сообщают о сделанном (§17.2): разговор ничего не меняет,
# и ответ с таким словом без «не» перед ним уходить не должен.
ACTION_WORDS = frozenset(
    {
        "записал",
        "запомнил",
        "сохранил",
        "перенёс",
        "перенес",
        "поменял",
        "изменил",
        "исправил",
        "поправил",
        "закрыл",
        "отметил",
        "отменил",
        "убрал",
        "удалил",
        "добавил",
        "поставил",
        "отправил",
    }
)
NEGATION = "не"
_WORD = re.compile(r"\w+")


class PastMessage(Protocol):
    """Прошлое сообщение владельца, как его видит блок 6 (§17.5)."""

    @property
    def received_at(self) -> datetime: ...

    @property
    def kind(self) -> str: ...

    @property
    def text(self) -> str: ...

    @property
    def forwarded_from(self) -> str | None: ...

    @property
    def reply(self) -> str | None: ...


@dataclass(frozen=True, slots=True)
class RecentTalk:
    """Готовый блок 6 и число сообщений в нём — для журнала, где текстов нет."""

    text: str
    count: int


def cut_middle(text: str, limit: int = TEXT_LIMIT) -> str:
    """Текст не длиннее `limit`: начало, «…» и конец.

    По началу видно, о чём речь, а в конце обычно вопрос или предложение
    (§17.3). Короткий текст остаётся как есть.
    """
    if len(text) <= limit:
        return text
    kept = limit - len(ELLIPSIS)
    head = (kept + 1) // 2
    tail = kept - head
    return text[:head] + ELLIPSIS + (text[-tail:] if tail else "")


def _flat(text: str) -> str:
    """Одно сообщение — одна строка блока: переводы строк и лишние пробелы схлопнуты."""
    return " ".join(text.split())


def _who(message: PastMessage) -> str:
    """Кто писал: владелец сам, переслал чужое или прислал снимок."""
    sender = _flat(message.forwarded_from) if message.forwarded_from is not None else None
    if message.kind == PHOTO_KIND:
        return "Вы прислали снимок" if sender is None else f"Вы переслали снимок (от: {sender})"
    return "Вы" if sender is None else f"Вы переслали (от: {sender})"


def _lines(message: PastMessage, timezone: tzinfo) -> list[str]:
    """Строка сообщения со временем и, если ответ был, строка ответа без времени."""
    body = _flat(message.text)
    if not body:
        body = NO_CAPTION if message.kind == PHOTO_KIND else NO_TEXT
    clock = f"{message.received_at.astimezone(timezone):%H:%M}"
    lines = [f"{clock} {_who(message)}: {cut_middle(body)}"]
    reply = _flat(message.reply or "")
    if reply:
        lines.append(f"{BOT_NAME}: {cut_middle(reply)}")
    return lines


def recent_block(messages: Iterable[PastMessage], timezone: tzinfo) -> RecentTalk | None:
    """Блок 6: последние `RECENT_LIMIT` сообщений от старых к новым, не длиннее
    `BLOCK_LIMIT` знаков. Не влезает — выпадают самые старые вместе с ответом.
    Сообщений нет — блока нет.
    """
    ordered = sorted(messages, key=lambda message: message.received_at)[-RECENT_LIMIT:]
    entries = [_lines(message, timezone) for message in ordered]
    while entries:
        text = "\n".join([HEADER, *(line for entry in entries for line in entry)])
        if len(text) <= BLOCK_LIMIT:
            return RecentTalk(text=text, count=len(entries))
        entries.pop(0)
    return None


def reply_text(hint: str | None) -> str | None:
    """Ответ разговора из `reply_hint` (§17.2): без пробелов по краям, не
    длиннее `REPLY_LIMIT` знаков с «…» на месте обрезки. Пусто — ответа нет.
    """
    if hint is None:
        return None
    text = hint.strip()
    if not text:
        return None
    if len(text) > REPLY_LIMIT:
        return text[:REPLY_LIMIT] + ELLIPSIS
    return text


def reports_action(text: str) -> bool:
    """Говорит ли ответ о сделанном (§17.2): слово из `ACTION_WORDS` целиком,
    в любом регистре, и перед ним нет «не». «Ничего не записал» — не говорит.
    """
    words = _WORD.findall(text.lower())
    return any(
        word in ACTION_WORDS and (index == 0 or words[index - 1] != NEGATION)
        for index, word in enumerate(words)
    )
