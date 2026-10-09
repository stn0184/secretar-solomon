"""Сферы жизни в приёме сообщения (`techspec/30-spheres.md` §30.2–30.3): что
уходит модели, что пишется в базу и что слышит владелец. Сервис — на
подменённых базе и модели.

Сферы и люди выдуманные: VoiceFin, РЕЙВА, Игорь, Олег.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from solomon import texts
from solomon.db.spheres import Sphere
from solomon.db.tasks import StoredMessage
from solomon.services.spheres import SPHERE_LIMIT
from solomon.services.understanding import (
    NO_SPHERES,
    SPHERE_RULES,
    SPHERE_SHORT_RULES,
    PhotoAnalysis,
    SphereStep,
    build_system_prompt,
    open_task_lines,
)
from tests.conftest import (
    OWNER_ID,
    FakeEdits,
    FakePlanner,
    load_image,
    make_details,
    make_item,
    make_message_understanding,
    make_photo_understanding,
    make_understanding,
)
from tests.test_chat_edit_service import (
    LAMP,
    MEETING,
    MEETING_ID,
    MESSAGE_ID,
    NOW,
    REPORT,
    TZ,
    build,
    edit,
    saved,
    say,
)

VOICEFIN = Sphere(id="s1", name="VoiceFin", facts=("продаём подписку бухгалтерам",))
REIVA = Sphere(id="s2", name="РЕЙВА", facts=("в РЕЙВА отвечаю за продажи",))
FAMILY = Sphere(id="s3", name="семья")
BOOK = [VOICEFIN, REIVA, FAMILY]


def store(
    *tasks: Any, spheres: list[Sphere] | None = None, broken: tuple[str, ...] = ()
) -> FakeEdits:
    """Хранилище правки со сферами: задачи по умолчанию — список правки."""
    return FakeEdits(
        tasks or [LAMP, REPORT, MEETING],
        spheres=BOOK if spheres is None else spheres,
        broken=broken,
    )


def sphere_edit(task: int | None = 1, **fields: Any) -> dict[str, Any]:
    return edit(action="sphere", task=task, **fields)


# --- Что уходит модели --------------------------------------------------------------


async def test_prompt_gets_the_live_spheres_with_their_knowledge() -> None:
    service, analyst, _, _, edits = build(make_understanding(), store())

    await say(service, "купить хлеб")

    assert analyst.spheres == [BOOK]
    assert edits.sphere_reads == 1


async def test_spheres_failure_goes_without_the_block_and_records_the_errand() -> None:
    """Сферы не прочитались — разбор без блока сфер: поручение важнее контекста."""
    service, analyst, understandings, _, _ = build(
        make_understanding(sphere="VoiceFin"), store(broken=("spheres",))
    )

    outcome = await say(service, "созвон с бухгалтерами")

    assert analyst.spheres == [None]
    assert outcome.ok
    assert saved(understandings, "task")["sphere"] is None


def test_text_prompt_carries_the_sphere_block_and_its_rules() -> None:
    prompt = build_system_prompt(NOW, TZ, spheres=BOOK)
    empty = build_system_prompt(NOW, TZ, spheres=[])

    assert "Сферы владельца:\n- VoiceFin: продаём подписку бухгалтерам" in prompt
    assert SPHERE_RULES in prompt
    assert f"{NO_SPHERES}\n{SPHERE_RULES}" in empty
    assert "Сферы владельца:\n" not in build_system_prompt(NOW, TZ)
    assert NO_SPHERES not in build_system_prompt(NOW, TZ)


def test_forwarded_photo_and_conversation_get_only_the_short_rules() -> None:
    """Чужие слова и снимок сфер не заводят (§30.2): у них — сфера дела по
    смыслу, и только когда сферы есть."""
    for prompt in (
        build_system_prompt(NOW, TZ, spheres=BOOK, short=True),
        build_system_prompt(NOW, TZ, spheres=BOOK, photo=True),
        build_system_prompt(NOW, TZ, spheres=BOOK, conversation=True),
    ):
        assert SPHERE_SHORT_RULES in prompt
        assert SPHERE_RULES not in prompt
    assert "Сфер у владельца" not in build_system_prompt(NOW, TZ, spheres=[], photo=True)


def test_open_task_line_names_its_sphere() -> None:
    """Блок 5 (§30.2): у задачи — сфера; её нет в списке — строки нет."""
    sphered = make_details(title="созвон с бухгалтерами", sphere_id="s1")
    gone = make_details(title="купить хлеб", sphere_id="s9")

    lines = open_task_lines([sphered, gone], TZ, BOOK)

    assert lines == ["1. созвон с бухгалтерами (сфера: VoiceFin)", "2. купить хлеб"]


# --- Заведение и убирание ------------------------------------------------------------------


async def test_my_spheres_are_added_and_named_in_the_reply() -> None:
    """Приёмка 1: «мои сферы: VoiceFin, РЕЙВА, семья» — три сферы и «Завёл сферы»."""
    verdict = make_message_understanding(
        kind="sphere",
        title="VoiceFin",
        sphere="VoiceFin",
        more_spheres=[SphereStep(drop=False, name="РЕЙВА"), SphereStep(drop=False, name="семья")],
    )
    service, _, understandings, _, _ = build(verdict, store(spheres=[]))

    outcome = await say(service, "мои сферы: VoiceFin, РЕЙВА, семья")

    assert outcome.message == "✅ Завёл сферы: VoiceFin, РЕЙВА, семья."
    assert saved(understandings, "spheres") == {"add": ["VoiceFin", "РЕЙВА", "семья"]}
    assert saved(understandings, "tasks") == []


async def test_the_same_sphere_again_is_not_added_twice() -> None:
    verdict = make_message_understanding(kind="sphere", title="voicefin", sphere="voicefin")
    service, _, understandings, _, _ = build(verdict, store())

    outcome = await say(service, "заведи сферу voicefin")

    assert outcome.message == "✅ Сфера VoiceFin уже есть."
    assert saved(understandings, "spheres") is None


async def test_the_thirteenth_sphere_is_refused_in_words() -> None:
    full = [Sphere(id=f"s{index}", name=f"сфера {index}") for index in range(SPHERE_LIMIT)]
    verdict = make_message_understanding(kind="sphere", title="спорт", sphere="спорт")
    service, _, understandings, _, _ = build(verdict, store(spheres=full))

    outcome = await say(service, "заведи сферу спорт")

    assert outcome.message.startswith("⚠️ Больше 12 сфер не веду — не завёл: спорт.")
    assert saved(understandings, "spheres") is None


async def test_dropped_sphere_goes_and_its_tasks_stay() -> None:
    """Приёмка 7: «убери сферу семья» — сфера уходит, дела и записи остаются."""
    verdict = make_message_understanding(kind="sphere_drop", title="семья", sphere="Семья")
    service, _, understandings, _, _ = build(verdict, store())

    outcome = await say(service, "убери сферу семья")

    assert outcome.message == "✏️ Убрал сферу: семья. Её дела и записи остались — без сферы."
    assert saved(understandings, "spheres") == {"drop": ["семья"]}


async def test_dropping_a_missing_sphere_changes_nothing() -> None:
    verdict = make_message_understanding(kind="sphere_drop", title="спорт", sphere="спорт")
    service, _, understandings, _, _ = build(verdict, store())

    outcome = await say(service, "убери сферу спорт")

    assert outcome.message == "⚠️ Сферы «спорт» нет — ничего не убирал."
    assert saved(understandings, "spheres") is None


async def test_a_task_and_a_new_sphere_in_one_message_both_are_said() -> None:
    verdict = make_message_understanding(
        kind="task",
        title="тренировка",
        more_spheres=[SphereStep(drop=False, name="спорт")],
    )
    service, _, understandings, _, _ = build(verdict, store())

    outcome = await say(service, "заведи сферу спорт и запиши тренировку")

    assert outcome.message == "✅ Записал: тренировка\n\nЗавёл сферу: спорт."
    assert saved(understandings, "spheres") == {"add": ["спорт"]}
    assert saved(understandings, "task")["title"] == "тренировка"


async def test_forwarded_message_adds_no_spheres() -> None:
    """Чужие слова сфер не заводят (§30.2)."""
    verdict = make_message_understanding(kind="sphere", title="спорт", sphere="спорт")
    service, _, understandings, _, _ = build(verdict, store())

    await say(service, "заведи сферу спорт", forwarded_from="Олег")

    assert saved(understandings, "spheres") is None


# --- Знание о сфере ---------------------------------------------------------------------


async def test_knowledge_is_remembered_about_its_sphere() -> None:
    """Приёмка 2: «это по VoiceFin: продаём подписку бухгалтерам» — знание сферы."""
    verdict = make_message_understanding(
        kind="about_me",
        title="продаём подписку бухгалтерам",
        sphere="voicefin",
        facts=[{"category": "work", "text": "Продаём подписку бухгалтерам"}],
    )
    service, _, understandings, _, _ = build(verdict, store())

    outcome = await say(service, "это по VoiceFin: продаём подписку бухгалтерам")

    assert outcome.message == "✅ Запомнил про VoiceFin: Продаём подписку бухгалтерам"
    assert saved(understandings, "facts") == [
        {
            "category": "work",
            "text": "Продаём подписку бухгалтерам",
            "status": "fact",
            "sphere": "VoiceFin",
        }
    ]
    assert saved(understandings, "spheres") is None


async def test_knowledge_of_a_new_sphere_creates_it() -> None:
    verdict = make_message_understanding(
        kind="about_me",
        title="бегаю по утрам",
        sphere="спорт",
        facts=[{"category": "habit", "text": "Бегаю по утрам"}],
    )
    service, _, understandings, _, _ = build(verdict, store())

    outcome = await say(service, "по спорту: бегаю по утрам")

    assert outcome.message == "✅ Запомнил про спорт: Бегаю по утрам\n\nЗавёл сферу: спорт."
    assert saved(understandings, "facts")[0]["sphere"] == "спорт"


async def test_plain_knowledge_has_no_sphere() -> None:
    verdict = make_message_understanding(
        kind="about_me",
        title="Toyota Camry",
        facts=[{"category": "car", "text": "Машина — Toyota Camry"}],
    )
    service, _, understandings, _, _ = build(verdict, store())

    outcome = await say(service, "у меня Camry")

    assert outcome.message == "✅ Запомнил: Машина — Toyota Camry"
    assert "sphere" not in saved(understandings, "facts")[0]


# --- Сфера дела ------------------------------------------------------------------------


async def test_task_of_a_sphere_is_recorded_with_it_and_says_it() -> None:
    """Приёмка 3: дело явно по сфере — запись со сферой и «Записал · VoiceFin»."""
    verdict = make_message_understanding(
        title="созвон с бухгалтерами по подписке",
        sphere="voicefin",
        due_at=datetime(2026, 9, 30, 11, 0, tzinfo=TZ),
        due_precision="time",
    )
    service, _, understandings, _, _ = build(verdict, store())

    outcome = await say(service, "созвон с бухгалтерами по подписке завтра в 11")

    assert outcome.message.startswith(
        "✅ Записал · VoiceFin: созвон с бухгалтерами по подписке. Срок: среда, 30 сентября, 11:00"
    )
    assert saved(understandings, "task")["sphere"] == "VoiceFin"


async def test_task_with_an_unknown_sphere_is_recorded_without_one() -> None:
    """Приёмка 8: сфер, которых владелец не называл, не появляется."""
    verdict = make_message_understanding(title="купить хлеб", sphere="покупки")
    service, _, understandings, _, _ = build(verdict, store())

    outcome = await say(service, "купить хлеб")

    assert outcome.message == "✅ Записал: купить хлеб"
    assert saved(understandings, "task")["sphere"] is None
    assert saved(understandings, "spheres") is None


async def test_several_tasks_name_their_spheres_in_the_list() -> None:
    verdict = make_message_understanding(
        title="созвон с бухгалтерами",
        sphere="VoiceFin",
        also=[make_item(title="забрать Мишу из садика", sphere="семья"), make_item()],
    )
    service, _, understandings, _, _ = build(verdict, store())

    outcome = await say(service, "созвон с бухгалтерами, забрать Мишу и костюм")

    assert outcome.message == (
        "✅ Записал:\n1. Созвон с бухгалтерами · VoiceFin\n"
        "2. Забрать Мишу из садика · семья\n3. Забрать костюм из химчистки"
    )
    assert [row["task"]["sphere"] for row in saved(understandings, "tasks")] == [
        "VoiceFin",
        "семья",
        None,
    ]


async def test_photo_task_gets_a_listed_sphere() -> None:
    service, analyst, understandings, _, _ = build(make_understanding(), store())
    analyst.photo_verdict = PhotoAnalysis(
        understanding=make_photo_understanding(title="счёт для бухгалтеров", sphere="voicefin"),
        model="claude-opus-5",
        input_tokens=1900,
        output_tokens=310,
    )

    outcome = await service.record_from_photo(
        chat_id=OWNER_ID,
        telegram_message_id=MESSAGE_ID,
        file_id="photo-1",
        media_type="image/jpeg",
        caption="",
        load_image=load_image,
    )

    assert outcome.message == "✅ Записал · VoiceFin: счёт для бухгалтеров"
    assert saved(understandings, "task")["sphere"] == "VoiceFin"
    assert analyst.spheres == [BOOK]


# --- Правка сферы словом --------------------------------------------------------------


async def test_this_is_for_reiva_moves_the_task_to_the_sphere() -> None:
    """Приёмка 4: «это по РЕЙВА» о деле — сфера дела меняется."""
    verdict = make_understanding(title="это по РЕЙВА", sphere="рейва", edit=sphere_edit())
    service, _, understandings, _, _ = build(verdict, store())

    outcome = await say(service, "это по РЕЙВА")

    assert outcome.message == "✏️ Поправил: встреча с Ренатой · РЕЙВА"
    row = saved(understandings, "edit")
    assert (row["task_id"], row["action"], row["sphere"]) == (MEETING_ID, "sphere", "РЕЙВА")
    assert outcome.buttons == ()


async def test_the_same_sphere_again_is_said_and_not_written() -> None:
    sphered = make_details(**{**_details(MEETING), "sphere_id": "s1"})
    verdict = make_understanding(title="это по VoiceFin", sphere="VoiceFin", edit=sphere_edit())
    service, _, understandings, _, _ = build(verdict, store(LAMP, REPORT, sphered))

    outcome = await say(service, "это по VoiceFin")

    assert outcome.message == "✏️ Так и записано: встреча с Ренатой · VoiceFin"
    row = saved(understandings, "edit")
    assert (row["action"], row["changes"]) == ("change", {})


async def test_no_sphere_takes_it_off_the_task() -> None:
    sphered = make_details(**{**_details(MEETING), "sphere_id": "s1"})
    verdict = make_understanding(title="без сферы", sphere=None, edit=sphere_edit())
    service, _, understandings, _, _ = build(verdict, store(LAMP, REPORT, sphered))

    outcome = await say(service, "это без сферы")

    assert outcome.message == "✏️ Поправил: встреча с Ренатой · без сферы"
    assert saved(understandings, "edit")["sphere"] is None


async def test_a_new_sphere_named_by_the_owner_is_created_with_the_edit() -> None:
    verdict = make_understanding(title="это по спорту", sphere="спорт", edit=sphere_edit())
    service, _, understandings, _, _ = build(verdict, store())

    outcome = await say(service, "это по спорту")

    assert outcome.message == "✏️ Поправил: встреча с Ренатой · спорт. Завёл сферу: спорт."
    assert saved(understandings, "edit")["sphere"] == "спорт"


async def test_an_edit_to_a_thirteenth_sphere_changes_nothing() -> None:
    full = [Sphere(id=f"s{index}", name=f"сфера {index}") for index in range(SPHERE_LIMIT)]
    verdict = make_understanding(title="это по спорту", sphere="спорт", edit=sphere_edit())
    service, _, understandings, _, _ = build(verdict, store(spheres=full))

    outcome = await say(service, "это по спорту")

    assert outcome.message.startswith("⚠️ Больше 12 сфер не веду")
    assert saved(understandings, "edit")["changes"] == {}


async def test_a_sphere_edit_without_a_task_says_so() -> None:
    verdict = make_understanding(title="это по РЕЙВА", sphere="РЕЙВА", edit=sphere_edit(None))
    service, _, understandings, _, _ = build(verdict, store())

    outcome = await say(service, "это по РЕЙВА")

    assert outcome.message == "⚠️ Не понял, какое дело отнести к сфере РЕЙВА — ничего не менял."
    assert saved(understandings, "edit") is None


async def test_candidates_of_a_sphere_edit_are_asked_with_the_sphere() -> None:
    verdict = make_understanding(
        title="это по РЕЙВА", sphere="РЕЙВА", edit=sphere_edit(None, candidates=[1, 2])
    )
    service, _, _, _, _ = build(verdict, store())

    outcome = await say(service, "это по РЕЙВА")

    assert outcome.message == "❓ Какое дело отнести к сфере РЕЙВА?"
    assert len(outcome.buttons) == 2


async def test_picked_candidate_gets_the_sphere() -> None:
    """Кнопка кандидата (§12.6) — та же правка сферы, сферы — на момент нажатия."""
    understanding = make_understanding(
        title="это по РЕЙВА", sphere="РЕЙВА", edit=sphere_edit(None, candidates=[1, 2])
    )
    edits = store()
    edits.messages[MESSAGE_ID] = StoredMessage(
        id="m1",
        text="это по РЕЙВА",
        task_id=None,
        analysis=understanding.model_dump(mode="json"),
        reply="❓ Какое дело отнести к сфере РЕЙВА?",
    )
    service, _, _, _, _ = build(make_understanding(), edits)

    outcome = await service.pick(
        chat_id=OWNER_ID, telegram_message_id=MESSAGE_ID, task_id=MEETING_ID
    )

    assert outcome.message == "✏️ Поправил: встреча с Ренатой · РЕЙВА"
    message_id, row, _ = edits.picks[0]
    assert (message_id, row["action"], row["sphere"]) == ("m1", "sphere", "РЕЙВА")
    assert edits.sphere_reads == 1


async def test_stored_answer_before_the_stage_reads_without_a_sphere() -> None:
    """Разбор до этапа 032 (без `sphere`) по-прежнему читается кнопками."""
    analysis = make_understanding(edit=edit(action="done", candidates=[1, 2])).model_dump(
        mode="json"
    )
    del analysis["sphere"]
    edits = store()
    edits.messages[MESSAGE_ID] = StoredMessage(
        id="m1", text="сделал", task_id=None, analysis=analysis, reply="❓ Какую задачу закрыть?"
    )
    service, _, _, _, _ = build(make_understanding(), edits, planner=FakePlanner())

    outcome = await service.pick(
        chat_id=OWNER_ID, telegram_message_id=MESSAGE_ID, task_id=MEETING_ID
    )

    assert outcome.message.startswith("✏️ Закрыл: встреча с Ренатой")
    assert edits.sphere_reads == 0


def _details(task: Any) -> dict[str, Any]:
    """Поля задачи для копии с другой сферой."""
    return {name: getattr(task, name) for name in task.__dataclass_fields__}


def test_sphere_name_with_braces_does_not_break_the_reply() -> None:
    """Название — слова владельца: скобки в нём не ломают шаблон ответа."""
    head = texts.sphered("Записал: {title}", "Вася {1}")

    assert head.format(title="x") == "Записал · Вася {1}: x"
    assert texts.sphered("{title}", "VoiceFin").format(title="Созвон") == "Созвон · VoiceFin"
    assert texts.sphered("Записал: {title}", None) == "Записал: {title}"
