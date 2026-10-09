"""Сферы жизни — чистые правила (`techspec/30-spheres.md`): название, поиск по
списку, сфера в разборе, план заведения и убирания, строки блока промпта.

Сферы и люди выдуманные: VoiceFin, РЕЙВА, Игорь.
"""

from __future__ import annotations

import inspect
from typing import Any

import pytest

from solomon.db import spheres as db_spheres
from solomon.db.rpc import DatabaseError
from solomon.db.spheres import Sphere
from solomon.services.spheres import (
    SPHERE_LIMIT,
    SpherePlan,
    clean_name,
    find,
    plan_spheres,
    settle,
    sphere_note,
)
from solomon.services.understanding import (
    MessageAnswer,
    SphereStep,
    format_spheres,
    sphere_lines,
    sphere_steps,
)
from tests.conftest import (
    OWNER_ID,
    make_conversation_understanding,
    make_item,
    make_message_understanding,
    make_photo_understanding,
    make_understanding,
)
from tests.test_tasks_db import FakeClient, as_client

VOICEFIN = Sphere(id="s1", name="VoiceFin", facts=("продаём подписку бухгалтерам",))
REIVA = Sphere(id="s2", name="РЕЙВА", facts=("отвечаю за продажи",))
FAMILY = Sphere(id="s3", name="семья")
BOOK = [VOICEFIN, REIVA, FAMILY]


def model_edit(**fields: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "action": "sphere",
        "task": 1,
        "candidates": [],
        "title": None,
        "due_at": None,
        "due_precision": None,
        "due_removed": False,
        "time_removed": False,
        "repeat": None,
        "repeat_removed": False,
        "priority": None,
        "promise": None,
        "people": None,
    }
    return {**base, **fields}


# --- Название и поиск ------------------------------------------------------------


def test_name_is_trimmed_to_one_line_without_quotes_and_to_forty() -> None:
    assert clean_name("  «VoiceFin»  ") == "VoiceFin"
    assert clean_name('"РЕЙВА"') == "РЕЙВА"
    assert clean_name("моя   семья\n") == "моя семья"
    assert clean_name("я" * 50) == "я" * 40
    assert clean_name("   ") is None
    assert clean_name(None) is None


def test_sphere_is_found_by_name_without_case_and_yo() -> None:
    assert find(BOOK, "voicefin") is VOICEFIN
    assert find(BOOK, " Рейва ") is REIVA
    assert find([Sphere(id="s4", name="Ёлка")], "елка") is not None
    assert find(BOOK, "спорт") is None
    assert find(BOOK, None) is None


# --- Сфера в разборе -----------------------------------------------------------------


def test_task_sphere_is_only_from_the_list_and_takes_its_spelling() -> None:
    """Сфера дела по смыслу — только из списка (§30.2): нет такой — без сферы."""
    known = settle(make_understanding(sphere="voicefin"), BOOK)
    unknown = settle(make_understanding(sphere="покупки"), BOOK)

    assert known.sphere == "VoiceFin"
    assert unknown.sphere is None


def test_knowledge_creation_and_edit_keep_a_new_name() -> None:
    """Знание, «заведи» и правка «это по X» — владелец назвал сферу сам."""
    about = settle(make_understanding(kind="about_me", sphere="«спорт»"), BOOK)
    add = settle(make_understanding(kind="sphere", sphere=None, title="спорт"), BOOK)
    edit = settle(make_understanding(sphere="рейва", edit=model_edit()), BOOK)
    new = settle(make_understanding(sphere="спорт", edit=model_edit()), BOOK)

    assert about.sphere == "спорт"
    assert add.sphere == "спорт"
    assert edit.sphere == "РЕЙВА"
    assert new.sphere == "спорт"


def test_other_edits_and_talk_keep_only_a_listed_sphere() -> None:
    """У правки срока верх — «как новое поручение»: сфера только из списка;
    у разговора и поиска сферы нет."""
    move = settle(make_understanding(sphere="спорт", edit=model_edit(action="change")), BOOK)
    talk = settle(make_understanding(kind="chat", sphere="VoiceFin"), BOOK)

    assert move.sphere is None
    assert talk.sphere is None


def test_items_of_several_tasks_get_listed_spheres_and_steps_are_cleaned() -> None:
    message = make_message_understanding(
        also=[make_item(sphere="рейва"), make_item(sphere="спорт")],
        sphere="VoiceFin",
        more_spheres=[SphereStep(drop=False, name=" «спорт» "), SphereStep(drop=False, name=" ")],
    )

    settled = settle(message, BOOK)

    assert [item.sphere for item in settled.also] == ["РЕЙВА", None]
    assert settled.more_spheres == [SphereStep(drop=False, name="спорт")]


def test_photo_and_conversation_get_a_listed_sphere_only() -> None:
    photo = settle(make_photo_understanding(sphere="voicefin"), BOOK)
    conversation = settle(make_conversation_understanding(kind="about_me", sphere="спорт"), BOOK)

    assert photo.sphere == "VoiceFin"
    assert conversation.sphere is None


# --- Шаги и план ------------------------------------------------------------------


def test_items_answer_turns_sphere_items_into_steps() -> None:
    """«Мои сферы: A, B, C» — по элементу на сферу (§23): верх и следующие."""
    first = make_understanding(kind="sphere", sphere="VoiceFin", title="VoiceFin")
    rest = [
        make_understanding(kind="sphere", sphere="РЕЙВА", title="РЕЙВА"),
        make_understanding(kind="sphere_drop", sphere="семья", title="семья"),
        make_understanding(kind="task", title="позвонить Игорю"),
    ]

    message = MessageAnswer(items=[first, *rest]).as_message()

    assert [item.title for item in message.also] == ["позвонить Игорю"]
    assert message.more_spheres == [
        SphereStep(drop=False, name="РЕЙВА"),
        SphereStep(drop=True, name="семья"),
    ]
    assert sphere_steps(message) == [
        SphereStep(drop=False, name="VoiceFin"),
        SphereStep(drop=False, name="РЕЙВА"),
        SphereStep(drop=True, name="семья"),
    ]


def test_steps_come_only_from_a_new_message_of_text_or_voice() -> None:
    edit = make_understanding(kind="sphere", sphere="спорт", edit=model_edit(action="change"))
    photo = make_photo_understanding(kind="sphere", sphere="спорт")

    assert sphere_steps(edit) == []
    assert sphere_steps(photo) == []
    assert sphere_steps(make_understanding()) == []


def test_plan_adds_new_spheres_once_and_names_the_known() -> None:
    message = make_message_understanding(
        kind="sphere",
        sphere="VoiceFin",
        more_spheres=[
            SphereStep(drop=False, name="спорт"),
            SphereStep(drop=False, name="Спорт"),
            SphereStep(drop=False, name="рейва"),
        ],
    )

    plan = plan_spheres(message, BOOK)

    assert plan == SpherePlan(add=("спорт",), known=("VoiceFin", "РЕЙВА"))


def test_plan_drops_live_spheres_and_names_the_missing() -> None:
    message = make_message_understanding(
        kind="sphere_drop",
        sphere="Семья",
        more_spheres=[SphereStep(drop=True, name="спорт")],
    )

    plan = plan_spheres(message, BOOK)

    assert plan == SpherePlan(drop=("семья",), unknown=("спорт",))


def test_plan_keeps_the_limit_and_counts_the_dropped() -> None:
    """Живых сфер не больше 12 (§30.1); убранная тем же сообщением место освобождает."""
    full = [Sphere(id=f"s{index}", name=f"сфера {index}") for index in range(SPHERE_LIMIT)]
    add = make_message_understanding(
        kind="sphere", sphere="спорт", more_spheres=[SphereStep(drop=True, name="сфера 0")]
    )
    over = make_message_understanding(kind="sphere", sphere="спорт")

    assert plan_spheres(add, full) == SpherePlan(add=("спорт",), drop=("сфера 0",))
    assert plan_spheres(over, full) == SpherePlan(refused=("спорт",))


def test_knowledge_names_its_sphere_and_creates_a_new_one() -> None:
    """«Это по X: …» — запись со сферой X; сферы нет — заводится (§30.2)."""
    known = make_message_understanding(kind="about_me", sphere="VoiceFin")
    new = make_message_understanding(kind="about_me", sphere="спорт")
    full = [Sphere(id=f"s{index}", name=f"сфера {index}") for index in range(SPHERE_LIMIT)]

    assert plan_spheres(known, BOOK) == SpherePlan(fact_sphere="VoiceFin")
    assert plan_spheres(new, BOOK) == SpherePlan(fact_sphere="спорт", created=("спорт",))
    assert plan_spheres(new, full) == SpherePlan(refused=("спорт",))
    assert plan_spheres(make_message_understanding(kind="about_me"), BOOK) == SpherePlan()


def test_plan_of_a_photo_or_an_edit_is_empty() -> None:
    photo = make_photo_understanding(kind="about_me", sphere="спорт")
    edit = make_message_understanding(kind="about_me", sphere="спорт", edit=model_edit())

    assert plan_spheres(photo, BOOK) == SpherePlan()
    assert plan_spheres(edit, BOOK) == SpherePlan()


# --- Строки блока промпта ------------------------------------------------------------


def test_sphere_lines_name_every_sphere_with_its_knowledge() -> None:
    assert sphere_lines(BOOK) == [
        "- VoiceFin: продаём подписку бухгалтерам",
        "- РЕЙВА: отвечаю за продажи",
        "- семья",
    ]
    assert sphere_lines([]) == []


def test_sphere_lines_keep_the_newest_knowledge_within_the_limit() -> None:
    """До 2000 знаков на всё (§30.2): названия — всегда, знания — по кругу,
    у каждой сферы свежие первыми, пока влезают; внутри сферы — по времени."""
    old = "а" * 900
    newer = "б" * 900
    newest = "в" * 900
    spheres = [
        Sphere(id="s1", name="VoiceFin", facts=(old, newer)),
        Sphere(id="s2", name="РЕЙВА", facts=(newest,)),
        Sphere(id="s3", name="семья", facts=("сын Миша", "дочь Аня")),
    ]

    lines = sphere_lines(spheres)

    assert lines == [f"- VoiceFin: {newer}", f"- РЕЙВА: {newest}", "- семья: сын Миша; дочь Аня"]
    assert len("\n".join(lines)) <= 2000


def test_sphere_lines_keep_every_name_even_over_the_limit() -> None:
    many = [Sphere(id=f"s{index}", name="я" * 40, facts=("знание",)) for index in range(60)]

    lines = sphere_lines(many)

    assert len(lines) == 60
    assert all(line == "- " + "я" * 40 for line in lines)


def test_format_spheres_puts_the_rules_after_the_list() -> None:
    block = format_spheres(BOOK, "ПРАВИЛА")

    assert block.splitlines()[0] == "Сферы владельца:"
    assert "- семья" in block
    assert block.endswith("ПРАВИЛА")
    assert format_spheres([], "ПРАВИЛА") == "Сфер у владельца пока нет.\nПРАВИЛА"


# --- Ответ о сферах -------------------------------------------------------------------


def test_note_names_added_known_dropped_missing_and_refused_with_one_icon() -> None:
    """Абзац ответа о сферах (§30.3): что заведено, что уже было, что убрано, чего
    нет и что не влезло; значок — у первой части."""
    added = sphere_note(SpherePlan(add=("VoiceFin", "РЕЙВА", "семья")))
    known = sphere_note(SpherePlan(known=("VoiceFin",)))
    mixed = sphere_note(SpherePlan(add=("спорт",), known=("VoiceFin", "РЕЙВА")))
    dropped = sphere_note(SpherePlan(drop=("семья",), unknown=("спорт",)))
    missing = sphere_note(SpherePlan(unknown=("спорт",)))
    full = sphere_note(SpherePlan(refused=("спорт",)))

    assert added == ("✅", "Завёл сферы: VoiceFin, РЕЙВА, семья.")
    assert known == ("✅", "Сфера VoiceFin уже есть.")
    assert mixed == ("✅", "Завёл сферу: спорт. Сферы VoiceFin, РЕЙВА уже есть.")
    assert dropped == (
        "✏️",
        "Убрал сферу: семья. Её дела и записи остались — без сферы. "
        "Сферы «спорт» нет — ничего не убирал.",
    )
    assert missing == ("⚠️", "Сферы «спорт» нет — ничего не убирал.")
    assert full == (
        "⚠️",
        "Больше 12 сфер не веду — не завёл: спорт. Уберите лишнюю: «убери сферу …».",
    )
    assert sphere_note(SpherePlan(fact_sphere="VoiceFin")) is None


def test_note_of_knowledge_names_the_sphere_it_created() -> None:
    note = sphere_note(SpherePlan(fact_sphere="спорт", created=("спорт",)))

    assert note == ("✅", "Завёл сферу: спорт.")


# --- Чтение из базы ---------------------------------------------------------------------


async def test_spheres_are_read_alive_with_their_knowledge_for_this_owner() -> None:
    """Живые сферы владельца по времени заведения, знания — факты со сферой,
    внутри сферы от старых к новым (§30.2)."""
    fake = FakeClient(
        tables={
            "spheres": [{"id": "s1", "name": "VoiceFin"}, {"id": "s2", "name": "семья"}],
            "facts": [
                {"sphere_id": "s1", "text": "новое"},
                {"sphere_id": "s9", "text": "чужая сфера"},
                {"sphere_id": "s1", "text": "старое"},
            ],
        }
    )

    found = await db_spheres.list_spheres(as_client(fake), owner_telegram_id=OWNER_ID)

    assert found == [
        Sphere(id="s1", name="VoiceFin", facts=("старое", "новое")),
        Sphere(id="s2", name="семья"),
    ]
    assert ("is", "removed_at", "null") in fake.calls
    assert ("not.is", "sphere_id", "null") in fake.calls
    assert ("eq", "status", "fact") in fake.calls
    assert fake.calls.count(("eq", "owner_telegram_id", OWNER_ID)) == 2


async def test_without_spheres_knowledge_is_not_read() -> None:
    fake = FakeClient(tables={"spheres": []})

    assert await db_spheres.list_spheres(as_client(fake), owner_telegram_id=OWNER_ID) == []
    assert ("table", "facts") not in fake.calls


async def test_broken_sphere_row_is_a_failure() -> None:
    fake = FakeClient(tables={"spheres": [{"id": "s1"}]})

    with pytest.raises(DatabaseError):
        await db_spheres.list_spheres(as_client(fake), owner_telegram_id=OWNER_ID)


def test_sphere_reader_takes_the_owner_by_name_only() -> None:
    parameter = inspect.signature(db_spheres.list_spheres).parameters["owner_telegram_id"]

    assert parameter.kind is inspect.Parameter.KEYWORD_ONLY
    assert parameter.default is inspect.Parameter.empty
