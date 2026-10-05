"""Несколько дел в одном сообщении (`techspec/23-several-tasks.md`).

Номера дел и дела сверх десяти — чистая функция `several_items`; запись,
ответ и кнопки — сервис на подменённой базе и модели.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from solomon import texts
from solomon.db.reminders import Planned
from solomon.db.tasks import OpenQuestion, SavedMessage
from solomon.services.tasks import MAX_ITEMS, Button, several_items
from tests.conftest import (
    FakeAnalyst,
    FakeMessages,
    FakePlanner,
    FakeQuestions,
    FakeTranscriber,
    FakeUnderstandings,
    make_item,
    make_message_understanding,
)
from tests.test_chat_edit_service import (
    MEETING_ID,
    MESSAGE_ID,
    MONDAY,
    NOW,
    REPORT,
    REPORT_ID,
    TODAY_FIVE,
    TZ,
    build,
    edit,
    saved,
    say,
)
from tests.test_tasks_service import build_service, record_voice

# --------------------------------------------------------- номера дел (§23.3)


def test_message_without_more_tasks_has_no_numbered_items() -> None:
    assert several_items([]) == ([], [])


def test_more_tasks_are_numbered_from_two_in_order() -> None:
    """Номер 1 — поля верхнего уровня; дела `also` — со второго, по порядку."""
    call = make_item(title="позвонить Игорю")
    suit = make_item(title="забрать костюм из химчистки")
    gift = make_item(kind="idea", title="подарок к годовщине")

    numbered, beyond = several_items([call, suit, gift])

    assert numbered == [(2, call), (3, suit), (4, gift)]
    assert beyond == []


def test_empty_task_is_skipped_without_shifting_the_numbers() -> None:
    """Номер — позиция в `also`: тот же номер получит кнопка, перечитав разбор."""
    call = make_item(title="позвонить Игорю")
    gift = make_item(kind="idea", title="подарок к годовщине")

    numbered, _ = several_items([call, make_item(title="  "), gift])

    assert numbered == [(2, call), (4, gift)]


def test_tasks_beyond_ten_are_named_not_recorded() -> None:
    """Десять дел на сообщение: верх и девять из `also`; остальные — сутью."""
    items = [make_item(title=f"дело {number}") for number in range(2, 13)]

    numbered, beyond = several_items(items)

    assert MAX_ITEMS == 10
    assert [number for number, _ in numbered] == list(range(2, 11))
    assert [item.title for _, item in numbered] == [f"дело {n}" for n in range(2, 11)]
    assert beyond == ["дело 11", "дело 12"]


def test_empty_tasks_beyond_ten_are_not_named() -> None:
    items = [make_item(title=f"дело {number}") for number in range(2, 11)]
    items += [make_item(title=" "), make_item(title=" купить цветы ")]

    _, beyond = several_items(items)

    assert beyond == ["купить цветы"]


# ------------------------------------------------------------- тексты (§23.4)


def test_listed_line_retells_without_the_word_recorded() -> None:
    """Строка списка — пересказ §6.4 без «Записал»: суть задачи с заглавной."""
    line = texts.listed_line(
        "task",
        "позвонить Игорю",
        due="среда, 30 сентября, 10:00",
        remind_at="30 сентября в 09:00",
    )

    assert line == "Позвонить Игорю. Срок: среда, 30 сентября, 10:00. Напомню: 30 сентября в 09:00"


def test_listed_line_names_idea_and_wish() -> None:
    assert texts.listed_line("idea", "подарок к годовщине") == "Идея: подарок к годовщине"
    assert texts.listed_line("wish", "съездить в Казань") == "Желание: съездить в Казань"


def test_listed_line_ends_with_priority_and_the_review_tail() -> None:
    line = texts.listed_line(
        "task", "купить подарок", review_reason=texts.REVIEW_DEFAULT, priority="high"
    )

    assert line == "Купить подарок. Приоритет: высокий. Не всё понял — перепроверьте"


def test_listed_reply_numbers_lines_from_one() -> None:
    reply = texts.listed_reply(["Позвонить Игорю", "Идея: подарок к годовщине"])

    assert reply == "Записал:\n1. Позвонить Игорю\n2. Идея: подарок к годовщине"


def test_same_time_of_one_of_several_tasks_names_it_first() -> None:
    clash = texts.same_time(["позвонить Игорю"], title="купить цветы")

    assert clash == "Купить цветы — в это же время у вас: «позвонить Игорю»."


def test_tasks_beyond_ten_are_named_in_their_own_paragraph() -> None:
    more = texts.more_in_message(["купить хлеб", "позвонить Олегу"])

    assert more == (
        "Ещё в сообщении: «купить хлеб», «позвонить Олегу». "
        "Нужны — напишите или надиктуйте отдельно."
    )


def test_help_tells_about_several_tasks() -> None:
    assert (
        "Можно надиктовать несколько дел одним сообщением — запишу каждое со своим сроком."
        in texts.HELP
    )


# ------------------------------------------------- запись и ответ (§23.3–23.4)

WEDNESDAY_TEN = datetime(2026, 9, 30, 10, 0, tzinfo=TZ)
FRIDAY_FIVE = datetime(2026, 10, 2, 17, 0, tzinfo=TZ)
FRIDAY = datetime(2026, 10, 2, 18, 0, tzinfo=TZ)
SATURDAY = datetime(2026, 10, 3, 18, 0, tzinfo=TZ)
WEDNESDAY_PLAN = [Planned(stage="due", fire_at=WEDNESDAY_TEN - timedelta(hours=1))]
FRIDAY_PLAN = [Planned(stage="due", fire_at=FRIDAY_FIVE - timedelta(hours=1))]


class DuePlanner(FakePlanner):
    """План по сроку: у дела без срока — пусто, как у `reminder_plan` (§6.1).

    Обычный `FakePlanner` отдаёт один план на любой вызов; в сообщении о
    нескольких делах у каждого дела свой.
    """

    def __init__(self, plans: dict[datetime, list[Planned]]) -> None:
        super().__init__()
        self.plans = plans

    async def __call__(
        self,
        *,
        due_at: datetime | None,
        due_precision: str | None,
        kind: str,
        now: datetime,
    ) -> list[Planned]:
        await super().__call__(due_at=due_at, due_precision=due_precision, kind=kind, now=now)
        return list(self.plans.get(due_at, [])) if due_at is not None else []


def three_tasks() -> Any:
    """«Завтра в 10 позвонить Игорю, в пятницу забрать костюм из химчистки и
    запиши идею подарка к годовщине»."""
    return make_message_understanding(
        title="позвонить Игорю",
        due_at=WEDNESDAY_TEN,
        due_precision="time",
        also=[
            make_item(title="забрать костюм из химчистки", due_at=FRIDAY, due_precision="day"),
            make_item(kind="idea", title="подарок к годовщине"),
        ],
    )


THREE_TASKS_REPLY = (
    "Записал:\n"
    "1. Позвонить Игорю. Срок: среда, 30 сентября, 10:00. Напомню: 30 сентября в 09:00\n"
    "2. Забрать костюм из химчистки. Срок: пятница, 2 октября\n"
    "3. Идея: подарок к годовщине"
)


def items_of(understandings: FakeUnderstandings) -> list[int]:
    return [entry["item"] for entry in saved(understandings, "tasks")]


def task_of(understandings: FakeUnderstandings, item: int) -> dict[str, Any]:
    entry = next(entry for entry in saved(understandings, "tasks") if entry["item"] == item)
    return dict(entry["task"])


def paragraphs_of(message: str) -> list[str]:
    return message.split("\n\n")


async def test_three_tasks_are_recorded_each_with_its_own_plan() -> None:
    """Три дела — три задачи одной записью, у каждой свой план (§23.3)."""
    planner = DuePlanner({WEDNESDAY_TEN: WEDNESDAY_PLAN})
    service, analyst, understandings, _, _ = build(three_tasks(), planner=planner)

    outcome = await say(service, "завтра в 10 позвонить Игорю, в пятницу забрать костюм")

    assert outcome.ok
    assert outcome.message == THREE_TASKS_REPLY
    assert outcome.buttons == ()
    assert len(analyst.calls) == 1
    assert len(understandings.calls) == 1
    assert saved(understandings, "reply") == THREE_TASKS_REPLY
    assert items_of(understandings) == [1, 2, 3]
    assert task_of(understandings, 2)["title"] == "забрать костюм из химчистки"
    assert task_of(understandings, 3)["kind"] == "idea"
    rows = saved(understandings, "tasks")
    assert [len(entry["reminders"]) for entry in rows] == [1, 0, 0]
    assert saved(understandings, "edit") is None
    assert saved(understandings, "amend") is None


async def test_one_task_keeps_the_old_answer() -> None:
    """Дело одно — ответ одной строкой, как было (§23.4)."""
    service, _, understandings, _, _ = build(make_message_understanding())

    outcome = await say(service, "купить лампочку")

    assert outcome.message == "Записал: купить лампочку"
    assert items_of(understandings) == [1]


async def test_one_new_task_beside_small_talk_is_recorded_with_its_number() -> None:
    """Верх — разговор: абзаца у него нет, а дело из `also` — номер 2."""
    verdict = make_message_understanding(
        kind="chat", title="привет", also=[make_item(title="забрать костюм из химчистки")]
    )
    service, _, understandings, _, _ = build(verdict)

    outcome = await say(service, "привет! да, и забрать костюм")

    assert outcome.message == "Записал: забрать костюм из химчистки"
    assert items_of(understandings) == [2]
    assert saved(understandings, "task") is None


async def test_voice_with_several_tasks_answers_with_the_list() -> None:
    """Голосовое — тот же путь: расшифровка, разбор, список (§23.2)."""
    analyst = FakeAnalyst(three_tasks())
    planner = DuePlanner({WEDNESDAY_TEN: WEDNESDAY_PLAN})
    service, _, understandings = build_service(
        analyst, transcriber=FakeTranscriber(), planner=planner
    )

    outcome = await record_voice(service)

    assert outcome.message.startswith("Записал:\n1. Позвонить Игорю")
    assert items_of(understandings) == [1, 2, 3]


async def test_tasks_beyond_ten_are_named_and_not_recorded() -> None:
    """Десять дел записаны одной записью, остальные — абзацем последним."""
    also = [make_item(title=f"дело {number}") for number in range(2, 13)]
    service, analyst, understandings, _, _ = build(make_message_understanding(also=also))

    outcome = await say(service, "двенадцать дел")

    assert items_of(understandings) == list(range(1, 11))
    assert len(analyst.calls) == 1
    last = paragraphs_of(outcome.message)[-1]
    assert last == (
        "Ещё в сообщении: «дело 11», «дело 12». Нужны — напишите или надиктуйте отдельно."
    )
    assert "10. Дело 10" in outcome.message


async def test_two_unclear_tasks_get_one_question_about_the_first() -> None:
    """Вопрос один — у первого (§23.3), последним абзацем; второе — с пометкой."""
    verdict = make_message_understanding(
        title="позвонить",
        needs_review=True,
        question="Кому позвонить?",
        also=[make_item(title="купить подарок", needs_review=True, question="Кому подарок?")],
    )
    service, _, understandings, _, _ = build(verdict)

    outcome = await say(service, "позвонить и купить подарок")

    assert outcome.message == (
        "Записал:\n1. Позвонить\n2. Купить подарок. Не всё понял — перепроверьте\n\nКому позвонить?"
    )
    first, second = task_of(understandings, 1), task_of(understandings, 2)
    assert first["open_question"] == "Кому позвонить?"
    assert first["needs_review"] is True
    assert second.get("open_question") is None
    assert second["needs_review"] is True


async def test_unasked_task_keeps_its_own_review_reason() -> None:
    verdict = make_message_understanding(
        title="позвонить",
        question="Кому позвонить?",
        also=[
            make_item(
                title="купить подарок",
                question="Кому подарок?",
                review_reason="Не понял, кому подарок — перепроверьте",
            )
        ],
    )
    service, _, _, _, _ = build(verdict)

    outcome = await say(service, "позвонить и купить подарок")

    assert "2. Купить подарок. Не понял, кому подарок — перепроверьте" in outcome.message


async def test_question_of_a_later_task_is_asked_when_the_first_is_clear() -> None:
    verdict = make_message_understanding(
        title="позвонить Игорю",
        also=[make_item(title="купить подарок", question="Кому подарок?")],
    )
    service, _, understandings, _, _ = build(verdict)

    outcome = await say(service, "позвонить Игорю и купить подарок")

    assert outcome.message == "Записал:\n1. Позвонить Игорю\n2. Купить подарок\n\nКому подарок?"
    assert task_of(understandings, 2)["open_question"] == "Кому подарок?"
    assert task_of(understandings, 1).get("open_question") is None


async def test_question_of_the_only_new_task_stays_in_its_line() -> None:
    """Весь ответ — одна строка: вопрос в ней, как в §10.1."""
    verdict = make_message_understanding(
        kind="chat", title="привет", also=[make_item(title="позвонить", question="Кому позвонить?")]
    )
    service, _, understandings, _, _ = build(verdict)

    outcome = await say(service, "привет! и позвонить")

    assert outcome.message == "Записал: позвонить. Кому позвонить?"
    assert task_of(understandings, 2)["open_question"] == "Кому позвонить?"


# --------------------------------------------------------- дубли и накладки


def dup(title: str, same_as: int, **fields: Any) -> Any:
    """Дело, которое модель узнала в задаче №`same_as` списка (§15.2)."""
    return make_item(title=title, same_as=same_as, **fields)


MEETING_SAID = "Это уже записано: встреча с Ренатой. Срок: пятница, 2 октября, 17:00"
REPORT_SAID = "Это уже записано: отправить отчёт. Срок: пятница, 2 октября"


async def test_duplicate_task_gets_its_paragraph_and_numbered_button() -> None:
    """Дубль не записывается: абзац и «Записать отдельно» со своим номером (§23.5)."""
    verdict = make_message_understanding(title="позвонить Игорю", also=[dup("созвон с Ренатой", 1)])
    service, _, understandings, _, _ = build(verdict)

    outcome = await say(service, "позвонить Игорю и созвон с Ренатой")

    assert paragraphs_of(outcome.message) == ["Записал: позвонить Игорю", MEETING_SAID]
    assert outcome.buttons == (Button(text="Записать отдельно", data=f"apart:{MESSAGE_ID}:2"),)
    assert items_of(understandings) == [1]
    assert saved(understandings, "same_task") is None


async def test_two_duplicates_get_two_buttons_with_their_titles() -> None:
    verdict = make_message_understanding(
        title="позвонить Игорю",
        also=[dup("созвон с Ренатой", 1), dup("отправить отчёт Петрову", 2)],
    )
    service, _, understandings, _, _ = build(verdict)

    outcome = await say(service, "позвонить Игорю, созвон с Ренатой, отчёт Петрову")

    assert paragraphs_of(outcome.message) == [
        "Записал: позвонить Игорю",
        MEETING_SAID,
        REPORT_SAID,
    ]
    assert outcome.buttons == (
        Button(text="Записать отдельно: созвон с Ренатой", data=f"apart:{MESSAGE_ID}:2"),
        Button(text="Записать отдельно: отправить отчёт Петрову", data=f"apart:{MESSAGE_ID}:3"),
    )
    assert items_of(understandings) == [1]


async def test_duplicate_on_top_leaves_the_other_tasks_recorded() -> None:
    verdict = make_message_understanding(
        title="созвон с Ренатой",
        same_as=1,
        also=[make_item(title="забрать костюм из химчистки")],
    )
    service, _, understandings, _, _ = build(verdict)

    outcome = await say(service, "созвон с Ренатой и забрать костюм")

    assert paragraphs_of(outcome.message) == [
        "Записал: забрать костюм из химчистки",
        MEETING_SAID,
    ]
    assert outcome.buttons == (Button(text="Записать отдельно", data=f"apart:{MESSAGE_ID}:1"),)
    assert items_of(understandings) == [2]


async def test_clash_with_the_base_and_with_a_task_above_names_the_task() -> None:
    """Накладка — с базой и с делами выше; суть дела впереди (§23.4)."""
    verdict = make_message_understanding(
        title="позвонить Игорю",
        due_at=FRIDAY_FIVE,
        due_precision="time",
        also=[make_item(title="купить цветы", due_at=FRIDAY_FIVE, due_precision="time")],
    )
    service, _, _, _, store = build(verdict, planner=DuePlanner({FRIDAY_FIVE: FRIDAY_PLAN}))

    outcome = await say(service, "в пятницу в пять позвонить Игорю и купить цветы")

    assert paragraphs_of(outcome.message)[1:] == [
        "Позвонить Игорю — в это же время у вас: «встреча с Ренатой».",
        "Купить цветы — в это же время у вас: «встреча с Ренатой», «позвонить Игорю».",
    ]
    assert store.minutes == [(FRIDAY_FIVE, None), (FRIDAY_FIVE, None)]


async def test_clash_inside_the_message_is_said_by_the_lower_task() -> None:
    verdict = make_message_understanding(
        title="позвонить Игорю",
        due_at=WEDNESDAY_TEN,
        due_precision="time",
        also=[make_item(title="купить цветы", due_at=WEDNESDAY_TEN, due_precision="time")],
    )
    service, _, _, _, _ = build(verdict)

    outcome = await say(service, "завтра в 10 позвонить Игорю и купить цветы")

    assert paragraphs_of(outcome.message)[1:] == [
        "Купить цветы — в это же время у вас: «позвонить Игорю»."
    ]


# ------------------------------------------- правка и ответ рядом с делами


async def test_edit_and_a_new_task_start_with_the_edit() -> None:
    """«Перенеси встречу с Ренатой на сегодня в пять и купи цветы к субботе»."""
    verdict = make_message_understanding(
        title="встреча с Ренатой",
        edit=edit(task=1, due_at=TODAY_FIVE, due_precision="time"),
        also=[make_item(title="купить цветы", due_at=SATURDAY, due_precision="day")],
    )
    service, _, understandings, _, _ = build(verdict)

    outcome = await say(service, "перенеси встречу на пять и купи цветы к субботе")

    first, second = paragraphs_of(outcome.message)
    assert first.startswith("Перенёс: встреча с Ренатой. Срок: вторник, 29 сентября, 17:00")
    assert second == "Записал: купить цветы. Срок: суббота, 3 октября"
    assert saved(understandings, "edit")["task_id"] == MEETING_ID
    assert items_of(understandings) == [2]


async def test_new_task_on_the_moved_minute_clashes_with_the_moved_task() -> None:
    """Задача правки — «выше» по сообщению: база её не считает, дело называет."""
    today_five = datetime.fromisoformat(TODAY_FIVE)
    verdict = make_message_understanding(
        title="встреча с Ренатой",
        edit=edit(task=1, due_at=TODAY_FIVE, due_precision="time"),
        also=[make_item(title="купить цветы", due_at=today_five, due_precision="time")],
    )
    service, _, _, _, store = build(verdict)

    outcome = await say(service, "перенеси встречу на пять, и в пять купить цветы")

    assert paragraphs_of(outcome.message)[-1] == (
        "Купить цветы — в это же время у вас: «встреча с Ренатой»."
    )
    assert store.minutes == [(today_five, MEETING_ID), (today_five, MEETING_ID)]


def asked_about_report(question: str = "Во сколько отправить?") -> OpenQuestion:
    return OpenQuestion(
        task_id=REPORT_ID,
        question=question,
        title=REPORT.title,
        kind="task",
        due_at=REPORT.due_at,
        due_precision="day",
        priority="normal",
        promise=None,
        people=REPORT.people,
        asked_at=NOW - timedelta(minutes=5),
    )


async def test_answer_and_a_new_task_amend_the_asked_task_and_record_the_new() -> None:
    verdict = make_message_understanding(
        title=REPORT.title,
        priority="high",
        answers_question=True,
        also=[make_item(title="купить цветы")],
    )
    service, _, understandings, _, _ = build(verdict, questions=FakeQuestions(asked_about_report()))

    outcome = await say(service, "это срочно, и купить цветы")

    first, second = paragraphs_of(outcome.message)
    assert first.startswith("Понял: отправить отчёт")
    assert second == "Записал: купить цветы"
    assert saved(understandings, "amend")["task_id"] == REPORT_ID
    assert items_of(understandings) == [2]


async def test_move_question_beside_new_tasks_names_the_task() -> None:
    """«Не успел» и новое дело: «На когда перенести?» — последним, с сутью."""
    verdict = make_message_understanding(
        title=REPORT.title, answers_question=True, also=[make_item(title="купить цветы")]
    )
    service, _, understandings, _, _ = build(
        verdict, questions=FakeQuestions(asked_about_report(texts.OVERDUE_QUESTION))
    )

    outcome = await say(service, "не успел, и купить цветы")

    assert paragraphs_of(outcome.message) == [
        "Записал: купить цветы",
        "Отправить отчёт — на когда перенести?",
    ]
    assert saved(understandings, "amend")["question"] == texts.OVERDUE_MOVE_QUESTION


async def test_candidates_and_a_new_task_record_it_and_ask_with_buttons() -> None:
    """Выбор задачи — последним абзацем с кнопками, новое дело уже записано."""
    verdict = make_message_understanding(
        title="встреча",
        edit=edit(task=None, candidates=[2, 1], due_at=MONDAY, due_precision="day"),
        also=[make_item(title="купить цветы")],
    )
    service, _, understandings, _, _ = build(verdict)

    outcome = await say(service, "перенеси на понедельник и купить цветы")

    assert paragraphs_of(outcome.message) == [
        "Записал: купить цветы",
        "Какую задачу перенести на понедельник, 5 октября?",
    ]
    assert [button.data for button in outcome.buttons] == [
        f"pick:{MESSAGE_ID}:{REPORT_ID}",
        f"pick:{MESSAGE_ID}:{MEETING_ID}",
    ]
    assert saved(understandings, "edit") is None
    assert items_of(understandings) == [2]


# ----------------------------------------------------------- повтор и сбои


async def test_repeated_update_answers_with_the_saved_list() -> None:
    messages = FakeMessages(SavedMessage(id="9a71", reply=THREE_TASKS_REPLY))
    service, analyst, understandings, _, _ = build(three_tasks(), messages=messages)

    outcome = await say(service, "завтра в 10 позвонить Игорю, в пятницу забрать костюм")

    assert outcome.message == THREE_TASKS_REPLY
    assert analyst.calls == []
    assert understandings.calls == []


async def test_database_failure_records_none_of_the_tasks() -> None:
    """Отказ базы откатывает сообщение целиком (§23.3) — ответ прежний."""
    service, _, _, _, _ = build(three_tasks(), understandings=FakeUnderstandings(broken=True))

    outcome = await say(service, "завтра в 10 позвонить Игорю, в пятницу забрать костюм")

    assert not outcome.ok
    assert outcome.message == texts.NOT_SAVED
