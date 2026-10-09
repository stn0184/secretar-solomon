"""Значки категорий в сообщениях бота (`techspec/29-icons.md`): чистые правила.

Значок — первый символ сообщения и пробел после него (§29.2); какой — решают
категория сообщения, точность срока у напоминания и состав ответа о записи.
"""

from __future__ import annotations

import pytest

from solomon import texts
from solomon.services.reminders import reminder_icon
from solomon.services.tasks import message_icon, record_icon


def test_iconed_puts_icon_and_space_before_text() -> None:
    assert texts.iconed(texts.ICON_RECORDED, "Записал: купить хлеб") == "✅ Записал: купить хлеб"


def test_iconed_without_icon_keeps_text() -> None:
    # Ответ разговора и справка — без значка (§29.1).
    assert texts.iconed(None, "В четверг у вас созвон.") == "В четверг у вас созвон."


def test_iconed_keeps_empty_text_empty() -> None:
    # Пустой ответ — сообщение переписки, которое не голова: его не отправляют.
    assert texts.iconed(texts.ICON_TROUBLE, "") == ""


def test_icon_set_is_the_approved_one() -> None:
    # Набор утверждён владельцем (§29.1): одиннадцать разных значков.
    icons = [
        texts.ICON_RECORDED,
        texts.ICON_IDEA,
        texts.ICON_REMINDER,
        texts.ICON_MEETING,
        texts.ICON_MORNING,
        texts.ICON_QUESTION,
        texts.ICON_EDIT,
        texts.ICON_CHAT,
        texts.ICON_WAITING,
        texts.ICON_SEARCH,
        texts.ICON_TROUBLE,
    ]
    assert icons == ["✅", "💡", "🔔", "📅", "☀️", "❓", "✏️", "💬", "⏳", "🔍", "⚠️"]


@pytest.mark.parametrize(
    ("precision", "icon"),
    [
        ("time", "📅"),
        ("day", "🔔"),
        ("morning", "🔔"),
        ("afternoon", "🔔"),
        ("evening", "🔔"),
        # Пустая точность читается как день (§6.1).
        (None, "🔔"),
    ],
)
def test_reminder_icon_by_due_precision(precision: str | None, icon: str) -> None:
    assert reminder_icon(precision) == icon


@pytest.mark.parametrize(
    ("kinds", "icon"),
    [
        (["task"], "✅"),
        (["idea"], "💡"),
        (["wish"], "💡"),
        (["idea", "wish"], "💡"),
        # Дело и идея в одном ответе — ✅: дело ждёт действия (§29.2).
        (["idea", "task"], "✅"),
        (["task", "wish", "idea"], "✅"),
    ],
)
def test_record_icon_by_kinds(kinds: list[str], icon: str) -> None:
    assert record_icon(kinds) == icon


def test_message_icon_is_first_paragraph_icon() -> None:
    assert message_icon(None, texts.ICON_RECORDED, texts.ICON_SEARCH) == "✅"


def test_message_icon_question_wins() -> None:
    # Сообщение ждёт ответа владельца — ❓, даже если в нём запись (§29.2).
    assert message_icon(texts.ICON_EDIT, texts.ICON_RECORDED, asks=True) == "❓"


def test_message_icon_without_parts_is_none() -> None:
    assert message_icon(None, None) is None


def test_help_start_and_chats_go_without_an_icon() -> None:
    """Справка — без значка (§29.1): это не сообщение о деле."""
    icons = (
        texts.ICON_RECORDED,
        texts.ICON_IDEA,
        texts.ICON_REMINDER,
        texts.ICON_MEETING,
        texts.ICON_MORNING,
        texts.ICON_QUESTION,
        texts.ICON_EDIT,
        texts.ICON_CHAT,
        texts.ICON_WAITING,
        texts.ICON_SEARCH,
        texts.ICON_TROUBLE,
    )
    helps = [texts.HELP, texts.START, texts.chats_help(relay=True), texts.chats_help(relay=False)]
    assert not any(text.startswith(icons) for text in helps)
