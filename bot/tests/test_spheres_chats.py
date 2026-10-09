"""Сферы в личных чатах (`techspec/30-spheres.md` §30.2–30.3): разбор чата
относит переписку и её дела к сфере, отчёт её называет, ответ на отчёт
«это по X» меняет сферу чата и всех его дел.

Сферы и люди выдуманные: VoiceFin, РЕЙВА, Игорь.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import replace
from typing import cast

from supabase import Client

from solomon import texts
from solomon.db import chats as db_chats
from solomon.db.chats import ReportedChat
from solomon.db.spheres import Sphere
from solomon.services import chats
from solomon.services.chats import ChatService
from solomon.services.tasks import Swipe
from solomon.services.understanding import KnownSphere, OpenTask
from tests.conftest import (
    OWNER_ID,
    FakePlanner,
    make_details,
    make_settings,
    make_understanding,
)
from tests.test_chat_edit_service import MESSAGE_ID, build, saved, say
from tests.test_chats import (
    ANALYSIS_ID,
    NOW,
    PLAN,
    QUIET_LATER,
    TZ,
    FakeChatCall,
    FakeChatStore,
    FakeSender,
    igor_said,
    make_answer,
    report_line,
    report_of,
    rpc,
    ticking,
)
from tests.test_spheres_service import BOOK, sphere_edit, store

THREAD_ID = "6c7d8e9f-0a1b-4c2d-8e3f-4a5b6c7d8e9f"
REPORT_MESSAGE = 9001
IGOR_REPORT = ReportedChat(
    thread_id=THREAD_ID,
    platform="telegram",
    chat_name="Игорь Петров",
    chat_with="Игорем",
    sphere="VoiceFin",
)
REPORT_TEXT = (
    "💬 Из переписки с Игорем (Telegram) · VoiceFin записал: "
    "Прислать Игорю расчёт — пятница, 9 октября (вы обещали)"
)


def sphered_service(
    store: FakeChatStore,
    call: FakeChatCall,
    spheres: Sequence[KnownSphere] = BOOK,
    tasks: Sequence[OpenTask] = (),
) -> ChatService:
    """Разбор чатов с подменёнными базой и моделью и со сферами владельца."""

    async def read_spheres() -> Sequence[KnownSphere]:
        return spheres

    async def open_tasks() -> Sequence[OpenTask]:
        return tasks

    return ChatService(
        settings=make_settings(),
        store=store,
        send=FakeSender(),
        call=call,
        planner=FakePlanner(PLAN),
        open_tasks=open_tasks,
        clock=lambda: QUIET_LATER,
        timer=ticking(5.0, 14.5),
        spheres=read_spheres,
    )


# --- Разбор чата ---------------------------------------------------------------------


def test_chat_prompt_carries_the_spheres_and_the_sphere_of_open_tasks() -> None:
    task = make_details(title="созвон с бухгалтерами", sphere_id="s1")

    system = chats.build_chat_system(NOW, TZ, [], [task], BOOK)
    bare = chats.build_chat_system(NOW, TZ, [], [task])

    assert "Сферы владельца:\n- VoiceFin: продаём подписку бухгалтерам" in system
    assert chats.SPHERE_CHAT_RULES in system
    assert "1. созвон с бухгалтерами (сфера: VoiceFin)" in system
    assert "Сферы владельца:" not in bare
    assert "1. созвон с бухгалтерами" in bare


def test_chat_answer_has_one_more_field_for_the_sphere() -> None:
    assert "sphere" in chats.ChatAnswer.model_fields
    assert chats.trim_answer(make_answer(sphere=" «VoiceFin» "), TZ).sphere == "VoiceFin"
    assert chats.trim_answer(make_answer(sphere="  "), TZ).sphere is None


async def test_chat_of_a_listed_sphere_is_recorded_with_it() -> None:
    """Приёмка 5: разобранная переписка получает сферу, её дела — тоже (база)."""
    store = FakeChatStore()
    await igor_said(store, ("in", "Пришлёшь расчёт по подписке?"), ("out", "Да, в пятницу"))
    call = FakeChatCall(make_answer(sphere="voicefin"))

    assert await sphered_service(store, call).analyze_due() == 1

    [(system, _)] = call.calls
    assert "Сферы владельца:" in system
    assert store.analyses[0]["sphere"] == "VoiceFin"


async def test_chat_sphere_not_in_the_list_is_not_recorded() -> None:
    """Разбор сфер не заводит (§30.2): нет такой — чат без сферы."""
    store = FakeChatStore()
    await igor_said(store, ("in", "Пришлёшь расчёт?"))
    call = FakeChatCall(make_answer(sphere="Стройка"))

    await sphered_service(store, call).analyze_due()

    assert store.analyses[0]["sphere"] is None


async def test_chat_without_spheres_has_no_sphere_block() -> None:
    store = FakeChatStore()
    await igor_said(store, ("in", "Пришлёшь расчёт?"))
    call = FakeChatCall(make_answer(sphere="VoiceFin"))

    await sphered_service(store, call, spheres=[]).analyze_due()

    [(system, _)] = call.calls
    assert "Сферы владельца:" not in system
    assert store.analyses[0]["sphere"] is None


# --- Отчёт -----------------------------------------------------------------------------


def test_report_names_the_sphere_of_the_chat() -> None:
    """Приёмка 5: «💬 Из переписки с Игорем (Telegram) · VoiceFin записал: …»."""
    sphered = replace(report_of(report_line()), sphere="VoiceFin")

    text, _ = chats.report_message(sphered, ANALYSIS_ID, NOW, TZ)

    assert text.startswith("💬 Из переписки с Игорем (Telegram) · VoiceFin записал: ")


def test_notes_report_names_the_sphere_too() -> None:
    text = texts.chat_report("", "max", [(1, "купить бумагу")], notes=True, sphere="РЕЙВА")

    assert text == "Из ваших заметок в MAX · РЕЙВА записал: купить бумагу"


async def test_report_and_reported_chat_read_the_sphere() -> None:
    client = rpc(
        {
            "chat_report": [
                {
                    "platform": "telegram",
                    "chat_key": "1001",
                    "chat_name": "Игорь Петров",
                    "username": None,
                    "chat_with": "Игорем",
                    "item": 1,
                    "task_id": "k1",
                    "title": "прислать Игорю расчёт",
                    "due_at": None,
                    "due_precision": None,
                    "promise": "mine",
                    "status": "active",
                    "sphere": "VoiceFin",
                }
            ],
            "reported_chat": [
                {
                    "thread_id": THREAD_ID,
                    "platform": "telegram",
                    "chat_key": "1001",
                    "chat_name": "Игорь Петров",
                    "chat_with": "Игорем",
                    "sphere": "VoiceFin",
                }
            ],
        }
    )
    db = cast(Client, client)

    report = await db_chats.chat_report(db, owner_telegram_id=OWNER_ID, analysis_id=ANALYSIS_ID)
    reported = await db_chats.reported_chat(
        db, owner_telegram_id=OWNER_ID, telegram_message_id=REPORT_MESSAGE
    )

    assert report is not None and report.sphere == "VoiceFin"
    assert reported == IGOR_REPORT
    assert client.params[1] == {
        "owner_telegram_id": OWNER_ID,
        "telegram_message_id": REPORT_MESSAGE,
    }


async def test_not_a_report_is_none() -> None:
    client = rpc({"reported_chat": []})

    found = await db_chats.reported_chat(
        cast(Client, client), owner_telegram_id=OWNER_ID, telegram_message_id=REPORT_MESSAGE
    )

    assert found is None


# --- Ответ на отчёт ----------------------------------------------------------------------


def report_swipe() -> Swipe:
    return Swipe(telegram_message_id=REPORT_MESSAGE, from_bot=True, text=REPORT_TEXT)


async def test_reply_to_a_report_moves_the_chat_and_its_tasks_to_the_sphere() -> None:
    """Приёмка 5: ответ на отчёт «это по РЕЙВА» — сфера чата и его дел."""
    edits = store()
    edits.reports[REPORT_MESSAGE] = IGOR_REPORT
    verdict = make_understanding(title="это по РЕЙВА", sphere="рейва", edit=sphere_edit(None))
    service, analyst, understandings, _, _ = build(verdict, edits)

    outcome = await say(service, "это по РЕЙВА", swipe=report_swipe())

    assert analyst.swipes == [f"Ответ на отчёт о переписке: «{REPORT_TEXT}»"]
    assert outcome.message == "✏️ Поправил: переписка с Игорем и её дела · РЕЙВА"
    assert saved(understandings, "spheres") == {"chat": {"thread_id": THREAD_ID, "sphere": "РЕЙВА"}}
    assert saved(understandings, "edit") is None
    assert edits.report_reads == [REPORT_MESSAGE]


async def test_reply_with_a_task_number_still_moves_the_chat() -> None:
    """Ответом на отчёт правится чат, даже если модель назвала дело номером."""
    edits = store()
    edits.reports[REPORT_MESSAGE] = IGOR_REPORT
    verdict = make_understanding(title="это по РЕЙВА", sphere="РЕЙВА", edit=sphere_edit(1))
    service, _, understandings, _, _ = build(verdict, edits)

    await say(service, "это по РЕЙВА", swipe=report_swipe())

    assert saved(understandings, "spheres")["chat"]["thread_id"] == THREAD_ID
    assert saved(understandings, "edit") is None


async def test_reply_with_the_same_sphere_writes_nothing() -> None:
    edits = store()
    edits.reports[REPORT_MESSAGE] = IGOR_REPORT
    verdict = make_understanding(title="это по VoiceFin", sphere="VoiceFin", edit=sphere_edit(None))
    service, _, understandings, _, _ = build(verdict, edits)

    outcome = await say(service, "это по VoiceFin", swipe=report_swipe())

    assert outcome.message == "✏️ Так и записано: переписка с Игорем и её дела · VoiceFin"
    assert saved(understandings, "spheres") is None


async def test_reply_with_a_new_sphere_creates_it() -> None:
    edits = store()
    edits.reports[REPORT_MESSAGE] = IGOR_REPORT
    verdict = make_understanding(title="это по стройке", sphere="стройка", edit=sphere_edit(None))
    service, _, understandings, _, _ = build(verdict, edits)

    outcome = await say(service, "это по стройке", swipe=report_swipe())

    assert outcome.message == (
        "✏️ Поправил: переписка с Игорем и её дела · стройка. Завёл сферу: стройка."
    )
    assert saved(understandings, "spheres") == {
        "chat": {"thread_id": THREAD_ID, "sphere": "стройка"}
    }


async def test_reply_to_another_bot_message_is_not_a_chat() -> None:
    """Сообщение бота, которое не отчёт, — прежняя строка свайпа."""
    verdict = make_understanding(title="это по РЕЙВА", sphere="РЕЙВА", edit=sphere_edit(None))
    service, analyst, _, _, edits = build(verdict, store())

    await say(
        service,
        "это по РЕЙВА",
        swipe=Swipe(telegram_message_id=MESSAGE_ID + 1, from_bot=True, text="✅ Записал: x"),
    )

    assert analyst.swipes == ["Ответ на сообщение бота: «✅ Записал: x»"]
    assert edits.report_reads == [MESSAGE_ID + 1]


def test_spheres_in_the_book_are_spheres() -> None:
    assert all(isinstance(sphere, Sphere) for sphere in BOOK)
