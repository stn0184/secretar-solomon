"""Разговор — чистые функции (`techspec/17-conversation.md`).

Строки недавнего разговора (блок 6) и их обрезка, предел блока, ответ
разговора и проверка действия. Сети и базы здесь нет: сообщения собраны
руками, время — в поясе владельца.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from solomon import texts
from solomon.db.tasks import RecentMessage
from solomon.services.conversation import (
    ACTION_WORDS,
    BLOCK_LIMIT,
    RECENT_LIMIT,
    REPLY_LIMIT,
    TEXT_LIMIT,
    RecentTalk,
    cut_middle,
    recent_block,
    reply_text,
    reports_action,
)
from tests.conftest import OWNER_TIMEZONE

TZ = ZoneInfo(OWNER_TIMEZONE)
HEADER = "Недавний разговор (последний час, от старых к новым):"


def at(hour: int, minute: int) -> datetime:
    """Время сообщения в поясе владельца (+05:00) — 2 октября 2026."""
    return datetime(2026, 10, 2, hour, minute, tzinfo=TZ)


def said(
    when: datetime,
    text: str,
    *,
    reply: str | None = None,
    kind: str = "text",
    forwarded_from: str | None = None,
) -> RecentMessage:
    return RecentMessage(
        received_at=when, kind=kind, text=text, forwarded_from=forwarded_from, reply=reply
    )


# --- Обрезка посередине --------------------------------------------------------


def test_short_text_is_kept_as_is() -> None:
    assert cut_middle("что у меня в четверг?") == "что у меня в четверг?"
    assert cut_middle("а" * TEXT_LIMIT) == "а" * TEXT_LIMIT


def test_long_text_keeps_its_start_and_end_around_an_ellipsis() -> None:
    """По началу видно, о чём речь, а в конце обычно вопрос (§17.3)."""
    text = "начало " + "середина " * 100 + "Во сколько?"

    cut = cut_middle(text)

    assert len(cut) == TEXT_LIMIT
    assert cut.startswith("начало середина")
    assert cut.endswith("середина Во сколько?")
    assert cut.count("…") == 1


def test_cut_respects_a_smaller_limit() -> None:
    assert cut_middle("абвгдежзик", 5) == "аб…ик"


# --- Блок 6 --------------------------------------------------------------------


def test_no_messages_is_no_block() -> None:
    assert recent_block([], TZ) is None


def test_block_reads_like_the_techspec_example() -> None:
    """Время в поясе владельца, ответ бота — следующей строкой без времени."""
    talk = recent_block(
        [
            said(at(10, 5), "что у меня в четверг?", reply="В четверг в 10:00 созвон с Георгием."),
            said(at(10, 7), "Во сколько?", forwarded_from="Рената"),
            said(at(10, 12), "подпись", kind="photo"),
        ],
        TZ,
    )

    assert talk == RecentTalk(
        text=(
            f"{HEADER}\n"
            "10:05 Вы: что у меня в четверг?\n"
            "Соломон: В четверг в 10:00 созвон с Георгием.\n"
            "10:07 Вы переслали (от: Рената): Во сколько?\n"
            "10:12 Вы прислали снимок: подпись"
        ),
        count=3,
    )


def test_time_is_shown_in_the_owners_zone() -> None:
    """База отдаёт время в UTC — в блоке оно в поясе владельца."""
    talk = recent_block([said(datetime(2026, 10, 2, 5, 5, tzinfo=UTC), "привет")], TZ)

    assert talk is not None
    assert talk.text.splitlines()[1] == "10:05 Вы: привет"


def test_messages_go_from_old_to_new() -> None:
    talk = recent_block([said(at(10, 20), "второе"), said(at(10, 5), "первое")], TZ)

    assert talk is not None
    assert talk.text.splitlines()[1:] == ["10:05 Вы: первое", "10:20 Вы: второе"]


def test_photo_without_caption_says_so() -> None:
    talk = recent_block([said(at(10, 12), "", kind="photo")], TZ)

    assert talk is not None
    assert talk.text.splitlines()[1] == "10:12 Вы прислали снимок: без подписи"


def test_forwarded_photo_names_the_sender() -> None:
    talk = recent_block([said(at(10, 12), "", kind="photo", forwarded_from="Рената")], TZ)

    assert talk is not None
    assert talk.text.splitlines()[1] == "10:12 Вы переслали снимок (от: Рената): без подписи"


@pytest.mark.parametrize("kind", ["voice", "video_note"])
def test_voice_line_is_the_transcript(kind: str) -> None:
    talk = recent_block([said(at(10, 5), "напомни позвонить маме", kind=kind)], TZ)

    assert talk is not None
    assert talk.text.splitlines()[1] == "10:05 Вы: напомни позвонить маме"


def test_voice_without_transcript_says_so() -> None:
    talk = recent_block([said(at(10, 5), "", kind="voice")], TZ)

    assert talk is not None
    assert talk.text.splitlines()[1] == "10:05 Вы: без текста"


def test_message_without_reply_has_no_reply_line() -> None:
    """Ответа ещё нет — разбирается параллельно или разбор упал (§17.3)."""
    talk = recent_block([said(at(10, 5), "привет", reply=None), said(at(10, 6), "ну?")], TZ)

    assert talk is not None
    assert talk.text.splitlines()[1:] == ["10:05 Вы: привет", "10:06 Вы: ну?"]


def test_one_message_is_one_line() -> None:
    """Переводы строк внутри текста и ответа не рвут блок на чужие строки."""
    talk = recent_block(
        [said(at(10, 5), "купить:\n- хлеб\n\n- молоко", reply="Записал:\nкупить")], TZ
    )

    assert talk is not None
    assert talk.text.splitlines()[1:] == [
        "10:05 Вы: купить: - хлеб - молоко",
        "Соломон: Записал: купить",
    ]


def test_long_text_and_reply_are_cut_in_the_middle() -> None:
    long = "а" * 300 + "б" * 300
    talk = recent_block([said(at(10, 5), long, reply=long)], TZ)

    assert talk is not None
    line, reply = talk.text.splitlines()[1:]
    assert line == f"10:05 Вы: {cut_middle(long)}"
    assert reply == f"Соломон: {cut_middle(long)}"


def test_only_the_last_ten_messages_are_kept() -> None:
    start = at(10, 0)
    messages = [said(start + timedelta(minutes=n), f"сообщение {n}") for n in range(12)]

    talk = recent_block(messages, TZ)

    assert talk is not None
    assert talk.count == RECENT_LIMIT == 10
    lines = talk.text.splitlines()[1:]
    assert lines[0] == "10:02 Вы: сообщение 2"
    assert lines[-1] == "10:11 Вы: сообщение 11"


def test_oldest_messages_drop_out_when_the_block_is_too_long() -> None:
    """Блок не длиннее 4000 знаков: выпадают самые старые вместе с ответом."""
    start = at(10, 0)
    messages = [
        said(start + timedelta(minutes=n), f"{n}" + "т" * 600, reply=f"{n}" + "о" * 600)
        for n in range(10)
    ]

    talk = recent_block(messages, TZ)

    assert talk is not None
    assert len(talk.text) <= BLOCK_LIMIT
    assert talk.count < 10
    lines = talk.text.splitlines()[1:]
    assert len(lines) == 2 * talk.count
    assert lines[-2].startswith("10:09 Вы: 9")
    assert lines[0].startswith(f"10:0{10 - talk.count} Вы:")
    # Ещё одно, более старое, сообщение уже не влезло бы.
    wider = recent_block(messages[-(talk.count + 1) :], TZ)
    assert wider is not None and wider.count == talk.count


# --- Ответ разговора -----------------------------------------------------------


def test_reply_is_stripped() -> None:
    assert reply_text("  В четверг в 10:00 созвон с Георгием.\n") == (
        "В четверг в 10:00 созвон с Георгием."
    )


@pytest.mark.parametrize("hint", [None, "", "   ", "\n\t "])
def test_empty_reply_is_no_reply(hint: str | None) -> None:
    assert reply_text(hint) is None


def test_long_reply_is_cut_with_an_ellipsis() -> None:
    assert reply_text("я" * REPLY_LIMIT) == "я" * REPLY_LIMIT
    assert reply_text("я" * (REPLY_LIMIT + 1)) == "я" * REPLY_LIMIT + "…"
    assert REPLY_LIMIT == 3500


# --- Проверка действия ---------------------------------------------------------


def test_reply_reporting_an_action_is_caught() -> None:
    assert reports_action("Перенёс встречу на пятницу.")


@pytest.mark.parametrize("word", sorted(ACTION_WORDS))
def test_every_action_word_is_caught_in_any_case(word: str) -> None:
    assert reports_action(f"Готово: {word} всё, как просили.")
    assert reports_action(word.upper())


@pytest.mark.parametrize(
    "reply",
    [
        "Ничего не записал.",
        texts.NO_ERRAND,
        "Записать задачей?",
        "Не перенёс: такой встречи нет.",
        "Переписала бы, но не могу.",
        "В четверг в 10:00 созвон с Георгием.",
        "",
    ],
)
def test_reply_without_an_action_passes(reply: str) -> None:
    assert not reports_action(reply)


def test_action_words_are_the_techspec_list() -> None:
    assert ACTION_WORDS == frozenset(
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
