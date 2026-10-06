"""Поиск по поручению (`techspec/24-search.md`).

Вызовы базы — с подменённым клиентом Supabase (`FakeRpcClient`): проверяется,
что уходит в функцию и что бот делает с ответом.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Sequence
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta
from typing import Any, cast
from zoneinfo import ZoneInfo

import httpx2
import pytest
from aiogram import Bot
from anthropic import (
    APIConnectionError,
    APIStatusError,
    APITimeoutError,
    AuthenticationError,
    RateLimitError,
)
from anthropic.types import (
    ContentBlock,
    MessageParam,
    ServerToolUsage,
    ServerToolUseBlock,
    TextBlock,
    WebFetchBlock,
    WebFetchToolResultBlock,
    WebSearchResultBlock,
    WebSearchToolResultBlock,
)
from supabase import Client

from solomon import texts
from solomon.config import Settings
from solomon.db import searches as db_searches
from solomon.db.facts import Fact
from solomon.db.rpc import DatabaseError
from solomon.db.searches import PastSearch, SearchRow, SearchTrace
from solomon.db.tasks import SavedMessage
from solomon.runner import build_dispatcher
from solomon.services import search
from solomon.services.reminders import ReminderService
from solomon.services.tasks import RecordOutcome, TaskService, is_several
from solomon.services.understanding import (
    MessageUnderstanding,
    NotUnderstood,
    PhotoUnderstanding,
    Understanding,
    Verdict,
)
from tests.conftest import (
    OWNER_ID,
    OWNER_TIMEZONE,
    FakeAnalyst,
    FakeMessages,
    FakePlanner,
    FakeTranscriber,
    FakeUnderstandings,
    RecordingSession,
    load_audio,
    load_image,
    make_conversation_understanding,
    make_message_understanding,
    make_photo_understanding,
    make_settings,
    make_update,
    make_voice_update,
)
from tests.test_reminders import (
    SATURDAY_NOON,
    FakeAnnouncer,
    FakeAskRecorder,
    FakeClearMoved,
    FakeCloser,
    FakeDue,
    FakeMarks,
    FakeMoved,
    FakeNotifier,
    FakeRpcClient,
    FakeUndated,
    make_undated,
)
from tests.test_tasks_service import CHAT, HEAD, conversation_service, record_of, send

TZ = ZoneInfo(OWNER_TIMEZONE)
SEARCH_ID = "5d1e0f3a-7b2c-4d8e-9f10-2a3b4c5d6e7f"
MESSAGE_ID = "9a71c2d4-1e5f-4a6b-8c7d-0e1f2a3b4c5d"
QUERY = "билеты на самолёт Екатеринбург — Москва 15 октября, обратно 18 октября"
NOW = datetime(2026, 10, 6, 12, 0, tzinfo=TZ)
TRACE = SearchTrace(
    input_tokens=2400, output_tokens=870, web_searches=2, web_fetches=1, duration_ms=41500
)


def search_row(**changes: object) -> dict[str, object]:
    """Строка `take_search` и `searches_to_resume`, как её отдаёт PostgREST."""
    row: dict[str, object] = {
        "id": SEARCH_ID,
        "query": QUERY,
        "attempts": 1,
        "answer": None,
        "created_at": "2026-10-06T07:00:00+00:00",
        "request_chat_id": OWNER_ID,
        "request_message_id": 4242,
    }
    row.update(changes)
    return row


# --------------------------------------------------------- база (§24.5)


async def test_start_search_goes_with_the_owner_message_and_query() -> None:
    client = FakeRpcClient({"start_search": SEARCH_ID})

    search_id = await db_searches.start_search(
        cast(Client, client), owner_telegram_id=OWNER_ID, message_id=MESSAGE_ID, query=QUERY
    )

    assert search_id == SEARCH_ID
    assert client.calls == ["start_search"]
    assert client.params == [
        {"owner_telegram_id": OWNER_ID, "message_id": MESSAGE_ID, "query": QUERY}
    ]


@pytest.mark.parametrize("answer", [None, "", 42, [SEARCH_ID]])
async def test_start_search_without_an_id_is_a_refusal(answer: object) -> None:
    client = FakeRpcClient({"start_search": answer})

    with pytest.raises(DatabaseError):
        await db_searches.start_search(
            cast(Client, client), owner_telegram_id=OWNER_ID, message_id=MESSAGE_ID, query=QUERY
        )


async def test_take_search_reads_the_row_with_the_request() -> None:
    """Взятый поиск — запрос, попытка, время и сообщение с просьбой."""
    client = FakeRpcClient({"take_search": [search_row()]})
    stale = NOW - timedelta(minutes=10)

    taken = await db_searches.take_search(
        cast(Client, client), owner_telegram_id=OWNER_ID, search_id=SEARCH_ID, stale_before=stale
    )

    assert taken == SearchRow(
        id=SEARCH_ID,
        query=QUERY,
        attempts=1,
        answer=None,
        created_at=datetime(2026, 10, 6, 12, 0, tzinfo=TZ),
        chat_id=OWNER_ID,
        request_message_id=4242,
    )
    assert client.params == [
        {"owner_telegram_id": OWNER_ID, "search_id": SEARCH_ID, "stale_before": stale.isoformat()}
    ]


async def test_take_search_not_taken_is_none() -> None:
    client = FakeRpcClient({"take_search": []})

    taken = await db_searches.take_search(
        cast(Client, client), owner_telegram_id=OWNER_ID, search_id=SEARCH_ID, stale_before=NOW
    )

    assert taken is None


@pytest.mark.parametrize(
    "row",
    [
        search_row(query=None),
        search_row(created_at=None),
        search_row(request_message_id=None),
        {"id": SEARCH_ID},
        "строка",
    ],
)
async def test_incomplete_search_row_is_a_refusal(row: object) -> None:
    client = FakeRpcClient({"searches_to_resume": [row]})

    with pytest.raises(DatabaseError):
        await db_searches.searches_to_resume(
            cast(Client, client), owner_telegram_id=OWNER_ID, stale_before=NOW
        )


async def test_record_answer_goes_with_the_trace() -> None:
    client = FakeRpcClient({"record_search_answer": True})

    saved = await db_searches.record_search_answer(
        cast(Client, client),
        owner_telegram_id=OWNER_ID,
        search_id=SEARCH_ID,
        answer="1. Победа",
        trace=TRACE,
    )

    assert saved is True
    assert client.params == [
        {
            "owner_telegram_id": OWNER_ID,
            "search_id": SEARCH_ID,
            "answer": "1. Победа",
            "input_tokens": 2400,
            "output_tokens": 870,
            "web_searches": 2,
            "web_fetches": 1,
            "duration_ms": 41500,
        }
    ]


async def test_finish_and_fail_go_with_the_owner() -> None:
    client = FakeRpcClient({"finish_search": True, "fail_search": False})

    finished = await db_searches.finish_search(
        cast(Client, client),
        owner_telegram_id=OWNER_ID,
        search_id=SEARCH_ID,
        telegram_message_id=501,
    )
    failed = await db_searches.fail_search(
        cast(Client, client), owner_telegram_id=OWNER_ID, search_id=SEARCH_ID
    )

    assert (finished, failed) == (True, False)
    assert client.calls == ["finish_search", "fail_search"]
    assert client.params == [
        {"owner_telegram_id": OWNER_ID, "search_id": SEARCH_ID, "telegram_message_id": 501},
        {"owner_telegram_id": OWNER_ID, "search_id": SEARCH_ID},
    ]


@pytest.mark.parametrize(("answer", "attempts"), [(2, 2), (None, None), ([3], 3)])
async def test_release_names_the_attempts(answer: object, attempts: int | None) -> None:
    client = FakeRpcClient({"release_search": answer})

    released = await db_searches.release_search(
        cast(Client, client), owner_telegram_id=OWNER_ID, search_id=SEARCH_ID
    )

    assert released == attempts
    assert client.params == [{"owner_telegram_id": OWNER_ID, "search_id": SEARCH_ID}]


@pytest.mark.parametrize("function", ["record_search_answer", "finish_search", "fail_search"])
async def test_yes_or_no_without_a_clear_answer_is_a_refusal(function: str) -> None:
    client = cast(Client, FakeRpcClient({function: "ok"}))

    with pytest.raises(DatabaseError):
        if function == "record_search_answer":
            await db_searches.record_search_answer(
                client, owner_telegram_id=OWNER_ID, search_id=SEARCH_ID, answer="а", trace=TRACE
            )
        elif function == "finish_search":
            await db_searches.finish_search(
                client, owner_telegram_id=OWNER_ID, search_id=SEARCH_ID, telegram_message_id=1
            )
        else:
            await db_searches.fail_search(client, owner_telegram_id=OWNER_ID, search_id=SEARCH_ID)


async def test_searches_to_resume_go_in_order_of_the_base() -> None:
    client = FakeRpcClient(
        {
            "searches_to_resume": [
                search_row(),
                search_row(id="b", attempts=0, answer="1. Победа", request_message_id=4343),
            ]
        }
    )
    stale = NOW - timedelta(minutes=10)

    rows = await db_searches.searches_to_resume(
        cast(Client, client), owner_telegram_id=OWNER_ID, stale_before=stale
    )

    assert [(row.id, row.attempts, row.answer, row.request_message_id) for row in rows] == [
        (SEARCH_ID, 1, None, 4242),
        ("b", 0, "1. Победа", 4343),
    ]
    assert client.params == [{"owner_telegram_id": OWNER_ID, "stale_before": stale.isoformat()}]


async def test_previous_search_is_the_query_and_the_answer() -> None:
    client = FakeRpcClient({"previous_search": [{"query": QUERY, "answer": "1. Победа"}]})
    before = NOW
    since = NOW - timedelta(hours=1)

    past = await db_searches.previous_search(
        cast(Client, client), owner_telegram_id=OWNER_ID, before=before, since=since
    )

    assert past == PastSearch(query=QUERY, answer="1. Победа")
    assert client.params == [
        {
            "owner_telegram_id": OWNER_ID,
            "before": before.isoformat(),
            "since": since.isoformat(),
        }
    ]


async def test_no_previous_search_is_none() -> None:
    client = FakeRpcClient({"previous_search": []})

    past = await db_searches.previous_search(
        cast(Client, client), owner_telegram_id=OWNER_ID, before=NOW, since=NOW
    )

    assert past is None


async def test_broken_connection_is_a_refusal_for_every_call() -> None:
    """Сбой клиента наружу не течёт — один понятный тип, как у остальных вызовов."""
    client = cast(Client, FakeRpcClient(broken=True))
    owner = OWNER_ID

    calls = [
        db_searches.start_search(client, owner_telegram_id=owner, message_id=MESSAGE_ID, query="а"),
        db_searches.take_search(client, owner_telegram_id=owner, search_id="s", stale_before=NOW),
        db_searches.record_search_answer(
            client, owner_telegram_id=owner, search_id="s", answer="а", trace=TRACE
        ),
        db_searches.finish_search(
            client, owner_telegram_id=owner, search_id="s", telegram_message_id=1
        ),
        db_searches.release_search(client, owner_telegram_id=owner, search_id="s"),
        db_searches.fail_search(client, owner_telegram_id=owner, search_id="s"),
        db_searches.searches_to_resume(client, owner_telegram_id=owner, stale_before=NOW),
        db_searches.previous_search(client, owner_telegram_id=owner, before=NOW, since=NOW),
    ]
    for call in calls:
        with pytest.raises(DatabaseError):
            await call


# --------------------------------------------------------- тексты (§24.4, §24.6)


def test_searching_names_the_query_and_promises_the_answer() -> None:
    assert texts.searching(QUERY) == f"Ищу: {QUERY}. Пришлю, как найду."
    # Знак в конце запроса не встаёт перед точкой.
    assert texts.searching("какая погода завтра? ") == (
        "Ищу: какая погода завтра. Пришлю, как найду."
    )


def test_one_at_a_time_names_the_searches_left() -> None:
    assert texts.one_at_a_time(["школа с математикой"]) == (
        "Ищу по одному: «школа с математикой» поищу, если попросите отдельно."
    )
    assert texts.one_at_a_time(["школа", "квартира"]) == (
        "Ищу по одному: «школа», «квартира» поищу, если попросите отдельно."
    )


def test_failure_texts_name_the_query_shortly() -> None:
    assert texts.search_failed("билеты") == (
        "Не получилось поискать «билеты»: поиск не ответил. Попросите ещё раз, если ещё нужно."
    )
    assert texts.search_late("билеты") == (
        "Не успел поискать «билеты»: бот не работал. Попросите ещё раз, если ещё нужно."
    )
    long = "я" * 300
    assert f"«{'я' * 200}…»" in texts.search_failed(long)


def test_search_refusals_say_what_to_do() -> None:
    assert texts.SEARCH_NOT_SAVED == (
        "Не получилось записать поиск — попросите ещё раз, пожалуйста."
    )
    assert "текстом или голосом" in texts.SEARCH_TEXT_ONLY
    assert "Ищу" not in texts.SEARCH_NOT_SAVED


def test_help_tells_about_the_search() -> None:
    assert "Интернета у меня нет" not in texts.HELP
    assert (
        "Могу поискать в интернете: «найди билеты в Москву на 15-е» — пришлю варианты "
        "со ссылками." in " ".join(texts.HELP.split())
    )


# ------------------------------------------- вызов поиска: чистые функции (§24.2)


def text(value: str) -> TextBlock:
    return TextBlock(type="text", text=value, citations=None)


def tool_use(name: str = "web_search") -> ServerToolUseBlock:
    return ServerToolUseBlock.model_construct(
        type="server_tool_use", id="srvtoolu_1", name=name, input={"query": "билеты"}
    )


def searched(*urls: str) -> WebSearchToolResultBlock:
    """Результат поиска со ссылками."""
    return WebSearchToolResultBlock(
        type="web_search_tool_result",
        tool_use_id="srvtoolu_1",
        content=[
            WebSearchResultBlock(
                type="web_search_result", url=url, title="страница", encrypted_content="x"
            )
            for url in urls
        ],
    )


def fetched(url: str) -> WebFetchToolResultBlock:
    """Открытая страница: содержимое тесту не нужно."""
    return WebFetchToolResultBlock.model_construct(
        type="web_fetch_tool_result",
        tool_use_id="srvtoolu_2",
        content=WebFetchBlock.model_construct(type="web_fetch_result", url=url, content=None),
    )


POBEDA = "https://www.flypobeda.ru/flights/SVX/MOW"
AVIASALES = "https://www.aviasales.ru/routes/svx/mow"


def test_answer_is_the_text_after_the_last_result() -> None:
    """Подводка до вызова инструмента и текст между поисками — не ответ (§24.2)."""
    blocks: list[ContentBlock] = [
        text("I'll search for flights."),
        tool_use(),
        searched(POBEDA),
        text("Теперь открою страницу."),
        tool_use("web_fetch"),
        fetched(POBEDA),
        text("1. Победа — прямые рейсы.\n"),
        text(POBEDA),
    ]

    assert search.answer_text(blocks) == f"1. Победа — прямые рейсы.\n{POBEDA}"


def test_answer_without_results_is_all_the_text() -> None:
    blocks: list[ContentBlock] = [text("Уточните, "), text("куда лететь.")]

    assert search.answer_text(blocks) == "Уточните, куда лететь."
    assert search.answer_text([tool_use(), searched(POBEDA)]) == ""


def test_blocks_cut_by_citations_are_glued_as_they_are() -> None:
    """Живая проба 2026-10-06: пробелы и запятые на стыках цитат сохраняются."""
    assert search.glue(["прямые. ", "Билеты от 3 499 ₽", ", туда-обратно"]) == (
        "прямые. Билеты от 3 499 ₽, туда-обратно"
    )


def test_a_line_break_lost_at_a_seam_comes_back() -> None:
    """Проба разведки: «**Победа**Прямые рейсы» — на стыке пропал перевод
    строки. Без пробела по обе стороны перед заглавной буквой или номером
    варианта встаёт перевод строки; перед строчной и знаком — ничего."""
    assert search.glue(["Победа", "Прямые рейсы"]) == "Победа\nПрямые рейсы"
    assert search.glue(["ограничена.", "2. Аэрофлот"]) == "ограничена.\n2. Аэрофлот"
    assert search.glue(["от 3 499 ₽", "."]) == "от 3 499 ₽."
    assert search.glue(["рейс", "ы"]) == "рейсы"
    assert search.glue(["цена от", "3 300 ₽"]) == "цена от3 300 ₽"


def test_markup_is_taken_off() -> None:
    """Бот шлёт простой текст (§24.2): звёздочки, решётки и ссылки разметки снимаются."""
    assert search.plain("**Победа** — прямые") == "Победа — прямые"
    assert search.plain("## Варианты\n1. Победа") == "Варианты\n1. Победа"
    assert search.plain(f"[Победа]({POBEDA})") == f"Победа — {POBEDA}"
    assert search.plain(f"[{POBEDA}]({POBEDA})") == POBEDA


def test_answer_glues_after_taking_the_markup_off() -> None:
    blocks: list[ContentBlock] = [searched(POBEDA), text("1. **Победа**"), text("Прямые рейсы")]

    assert search.answer_text(blocks) == "1. Победа\nПрямые рейсы"


def test_long_answer_is_cut_at_a_line_with_an_ellipsis() -> None:
    line = "я" * 99
    answer = "\n".join([line] * 50)

    cut = search.cut_answer(answer)

    assert len(cut) <= search.ANSWER_LIMIT
    assert cut.endswith("\n…")
    assert cut[:-2].split("\n")[-1] == line, "строка не разрезана посередине"
    assert search.cut_answer("коротко") == "коротко"
    assert search.ANSWER_LIMIT == 3500


def test_foreign_links_are_the_sites_not_in_the_results() -> None:
    """След выдуманных ссылок (§24.2): сайт ссылки не встречался в результатах
    поиска и среди открытых страниц; www и поддомен — тот же сайт."""
    blocks: list[ContentBlock] = [searched(POBEDA), fetched(AVIASALES)]
    answer = (
        f"1. {POBEDA}\n"
        "2. https://aviasales.ru/search, а ещё\n"
        "3. https://m.flypobeda.ru/,\n"
        "4. https://www.uralairlines.ru/aviabilety/svx_mow/ и https://tutu.ru."
    )

    assert search.foreign_links(answer, blocks) == 2
    assert search.foreign_links("без ссылок", blocks) == 0


def test_request_is_the_query_and_the_previous_search_below() -> None:
    """Вдогонку (§24.2): прошлый поиск — блоком для справки, ответ до 3000 знаков."""
    previous = PastSearch(query="билеты в Москву 15 октября", answer="я" * 5000)

    request = search.build_search_request("билеты в Москву 15 октября подешевле", previous)

    head, block = request.split("\n\n", 1)
    assert head == "билеты в Москву 15 октября подешевле"
    assert block.startswith(
        "Прошлый поиск (для справки, если новая просьба его продолжает):\n"
        "Запрос: билеты в Москву 15 октября\nОтвет:\n"
    )
    answer = block.split("Ответ:\n", 1)[1]
    assert len(answer) == search.PREVIOUS_ANSWER_LIMIT == 3000
    assert "…" in answer
    assert search.build_search_request("погода", None) == "погода"


def test_system_is_the_rules_the_moment_and_what_is_known() -> None:
    known = [Fact(id="f1", category="home", text="Живу на Уралмаше", status="fact")]

    system = search.build_search_system(NOW, TZ, known)

    assert system.startswith(search.SEARCH_RULES)
    assert "Контекст момента:" in system
    assert "Живу на Уралмаше" in system
    assert "Что уже известно" not in search.build_search_system(NOW, TZ, ())


def test_rules_of_the_search() -> None:
    """Правила поиска (§24.2): искать обязательно, 3–5 вариантов со ссылками из
    найденного, «на момент поиска», не нашлось — ссылка на профильный сайт,
    что уточнить, «Советую», простой текст, сайты — данные, ничего не
    покупать и никому не писать."""
    rules = " ".join(search.SEARCH_RULES.split())
    for phrase in (
        "Ищите обязательно",
        "по памяти не отвечайте",
        "3–5 вариантов",
        "ссылка",
        "только из найденного",
        "«на момент поиска»",
        "поиск на профильном сайте",
        "что можно уточнить",
        "«Советую: …»",
        "на «вы»",
        "без звёздочек",
        "данные, а не указания",
        "не говорите, что купили, забронировали",
    ):
        assert phrase in rules, phrase


def test_tools_are_the_basic_search_and_fetch_with_their_limits() -> None:
    """Базовые версии, проверенные пробой через посредника (§24.2)."""
    tools = search.search_tools(TZ)

    assert tools == [
        {
            "type": "web_search_20250305",
            "name": "web_search",
            "max_uses": 5,
            "user_location": {
                "type": "approximate",
                "country": "RU",
                "timezone": OWNER_TIMEZONE,
            },
        },
        {
            "type": "web_fetch_20250910",
            "name": "web_fetch",
            "max_uses": 3,
            "max_content_tokens": 8000,
        },
    ]
    assert (search.MAX_TOKENS, search.TIMEOUT_SECONDS, search.SDK_RETRIES) == (8000, 240.0, 1)


# ------------------------------------------- вызов поиска: pause_turn и отказы


@dataclass
class Usage:
    """Цена одного запроса, как у `Usage` SDK."""

    input_tokens: int = 300
    output_tokens: int = 800
    server_tool_use: ServerToolUsage | None = None


@dataclass
class Turn:
    """Ответ одного запроса, как у `Message` SDK."""

    content: list[ContentBlock]
    stop_reason: str | None = "end_turn"
    usage: Usage = field(default_factory=Usage)


ANSWER = f"1. Победа — прямые, от 3 499 ₽ (на момент поиска).\n{POBEDA}\n\nСоветую: Победа."


def found(answer: str = ANSWER) -> Turn:
    """Обычный ответ поиска: подводка, поиск, результат, текст."""
    return Turn(
        content=[text("I'll search."), tool_use(), searched(POBEDA), text(answer)],
        usage=Usage(server_tool_use=ServerToolUsage(web_search_requests=1, web_fetch_requests=0)),
    )


class FakeModel:
    """Вместо Claude — заранее решённые ответы по очереди и список запросов."""

    def __init__(self, *turns: Turn | Exception, pause: float = 0.0) -> None:
        self.turns = list(turns)
        self.pause = pause
        self.calls: list[tuple[str, list[MessageParam]]] = []
        self.running = 0
        self.most = 0

    async def __call__(self, *, system: str, messages: Sequence[MessageParam]) -> Turn:
        self.calls.append((system, list(messages)))
        self.running += 1
        self.most = max(self.most, self.running)
        try:
            await asyncio.sleep(self.pause)
            turn = self.turns.pop(0) if len(self.turns) > 1 else self.turns[0]
        finally:
            self.running -= 1
        if isinstance(turn, Exception):
            raise turn
        return turn


class FakeTimer:
    """Монотонные часы: каждый вызов — на 20 с позже."""

    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        value = self.now
        self.now += 20.0
        return value


async def test_search_answer_and_its_trace() -> None:
    model = FakeModel(found())

    outcome = await search.run_search(model, system="правила", text=QUERY, timer=FakeTimer())

    assert outcome == search.Searched(
        answer=ANSWER,
        trace=SearchTrace(
            input_tokens=300, output_tokens=800, web_searches=1, web_fetches=0, duration_ms=20000
        ),
        foreign=0,
        continuations=0,
    )
    assert model.calls == [("правила", [{"role": "user", "content": QUERY}])]


async def test_pause_turn_goes_back_with_the_content_as_is() -> None:
    """`pause_turn` (§24.2): тот же запрос и содержимое ответа ассистентом; токены,
    поиски и страницы складываются по всем запросам."""
    paused = Turn(
        content=[tool_use(), searched(AVIASALES), tool_use("web_fetch")],
        stop_reason="pause_turn",
        usage=Usage(
            input_tokens=200,
            output_tokens=100,
            server_tool_use=ServerToolUsage(web_search_requests=2, web_fetch_requests=1),
        ),
    )
    model = FakeModel(paused, found())

    outcome = await search.run_search(model, system="правила", text=QUERY, timer=FakeTimer())

    assert isinstance(outcome, search.Searched)
    assert outcome.answer == ANSWER
    assert outcome.continuations == 1
    assert outcome.trace == SearchTrace(
        input_tokens=500, output_tokens=900, web_searches=3, web_fetches=1, duration_ms=20000
    )
    assert model.calls[1][1] == [
        {"role": "user", "content": QUERY},
        {"role": "assistant", "content": paused.content},
    ]


async def test_three_continuations_at_most() -> None:
    paused = Turn(content=[tool_use()], stop_reason="pause_turn")
    model = FakeModel(paused)

    outcome = await search.run_search(model, system="правила", text=QUERY)

    assert isinstance(outcome, search.NotSearched)
    assert len(model.calls) == 1 + search.MAX_CONTINUATIONS == 4
    # Каждое продолжение несёт всё, что модель уже сделала.
    assistant = model.calls[-1][1][1]
    assert assistant["role"] == "assistant"
    assert len(list(assistant["content"])) == 3


@pytest.mark.parametrize("stop", ["refusal", "max_tokens"])
async def test_refusal_and_cut_off_are_failed_attempts(stop: str) -> None:
    turn = found()
    model = FakeModel(Turn(content=turn.content, stop_reason=stop))

    outcome = await search.run_search(model, system="правила", text=QUERY)

    assert outcome == search.NotSearched(f"модель остановилась: {stop}")


async def test_empty_answer_is_a_failed_attempt() -> None:
    model = FakeModel(Turn(content=[text("Сейчас поищу."), tool_use(), searched(POBEDA)]))

    outcome = await search.run_search(model, system="правила", text=QUERY)

    assert outcome == search.NotSearched("пустой ответ")


REQUEST = httpx2.Request("POST", "https://api.anthropic.com/v1/messages")


@pytest.mark.parametrize(
    ("error", "reason"),
    [
        (APITimeoutError(REQUEST), "модель недоступна: APITimeoutError"),
        (APIConnectionError(request=REQUEST), "модель недоступна: APIConnectionError"),
        (
            RateLimitError("429", response=httpx2.Response(429, request=REQUEST), body=None),
            "лимит запросов",
        ),
        (
            APIStatusError("503", response=httpx2.Response(503, request=REQUEST), body=None),
            "модель ответила 503",
        ),
        (RuntimeError("что-то сломалось"), "сбой вызова: RuntimeError"),
    ],
)
async def test_call_failures_do_not_raise(error: Exception, reason: str) -> None:
    outcome = await search.run_search(FakeModel(error), system="правила", text=QUERY)

    assert outcome == search.NotSearched(reason)


async def test_bad_key_names_the_variable_in_the_log(caplog: pytest.LogCaptureFixture) -> None:
    error = AuthenticationError("401", response=httpx2.Response(401, request=REQUEST), body=None)

    with caplog.at_level(logging.ERROR):
        outcome = await search.run_search(FakeModel(error), system="правила", text=QUERY)

    assert outcome == search.NotSearched("ключ не подошёл")
    assert "ANTHROPIC_API_KEY" in caplog.text


# -------------------------------------------------- очередь и тик (§24.3, §24.6)


CREATED = datetime(2026, 10, 6, 11, 58, tzinfo=TZ)


def row(search_id: str = SEARCH_ID, **changes: Any) -> SearchRow:
    base: dict[str, Any] = {
        "id": search_id,
        "query": QUERY,
        "attempts": 1,
        "answer": None,
        "created_at": CREATED,
        "chat_id": OWNER_ID,
        "request_message_id": 4242,
    }
    base.update(changes)
    return SearchRow(**base)


class FakeStore:
    """Строки поисков в памяти: что взято, записано, помечено — и отказы.

    `rows` — поиски по id; `take` отдаёт строку с попыткой плюс один, пока
    поиск ждёт и не начат. `broken` — имена шагов, на которых база
    не отвечает.
    """

    def __init__(self, *rows: SearchRow, broken: Sequence[str] = ()) -> None:
        self.rows = {item.id: item for item in rows}
        self.status = {item.id: "pending" for item in rows}
        self.started: set[str] = set()
        self.broken = set(broken)
        self.calls: list[tuple[str, tuple[Any, ...]]] = []
        self.answers: dict[str, tuple[str, SearchTrace]] = {}
        self.finished: dict[str, int] = {}
        self.past: PastSearch | None = None
        self.resume: list[SearchRow] = []

    def _call(self, name: str, *args: Any) -> None:
        self.calls.append((name, args))
        if name in self.broken:
            raise DatabaseError("ConnectTimeout: timed out")

    async def start(self, message_id: str, query: str) -> str:
        self._call("start", message_id, query)
        return SEARCH_ID

    async def take(self, search_id: str, stale_before: datetime) -> SearchRow | None:
        self._call("take", search_id, stale_before)
        current = self.rows.get(search_id)
        if current is None or self.status[search_id] != "pending" or search_id in self.started:
            return None
        if current.answer is not None:
            return None
        self.started.add(search_id)
        taken = replace(current, attempts=current.attempts + 1)
        self.rows[search_id] = taken
        return taken

    async def record_answer(self, search_id: str, answer: str, trace: SearchTrace) -> bool:
        self._call("record_answer", search_id)
        self.answers[search_id] = (answer, trace)
        self.rows[search_id] = replace(self.rows[search_id], answer=answer)
        return True

    async def finish(self, search_id: str, telegram_message_id: int) -> bool:
        self._call("finish", search_id, telegram_message_id)
        self.status[search_id] = "done"
        self.finished[search_id] = telegram_message_id
        return True

    async def release(self, search_id: str) -> int | None:
        self._call("release", search_id)
        self.started.discard(search_id)
        return self.rows[search_id].attempts

    async def fail(self, search_id: str) -> bool:
        self._call("fail", search_id)
        self.status[search_id] = "failed"
        return True

    async def to_resume(self, stale_before: datetime) -> list[SearchRow]:
        self._call("to_resume", stale_before)
        return list(self.resume)

    async def previous(self, before: datetime, since: datetime) -> PastSearch | None:
        self._call("previous", before, since)
        return self.past

    def names(self) -> list[str]:
        return [name for name, _ in self.calls]


class FakeReplier:
    """Вместо Telegram — список отправленного; `broken` — отправка падает."""

    def __init__(self, broken: bool = False) -> None:
        self.sent: list[tuple[int, int, str]] = []
        self.broken = broken

    async def __call__(self, *, chat_id: int, reply_to: int, text: str) -> int:
        if self.broken:
            raise RuntimeError("Telegram: https://api.telegram.org/bot123:secret/sendMessage")
        self.sent.append((chat_id, reply_to, text))
        return 9000 + len(self.sent)


def build_searches(
    store: FakeStore,
    model: FakeModel | None = None,
    replier: FakeReplier | None = None,
    known: Sequence[Fact] = (),
) -> tuple[search.SearchService, FakeModel, FakeReplier]:
    """Сервис поиска на подменённых базе, модели и Telegram; «сейчас» — NOW."""
    model = model or FakeModel(found())
    replier = replier or FakeReplier()

    async def read_known() -> Sequence[Fact]:
        return known

    service = search.SearchService(
        settings=make_settings(),
        store=store,
        model=model,
        reply=replier,
        known=read_known,
        clock=lambda: NOW,
        timer=FakeTimer(),
    )
    return service, model, replier


async def test_search_is_taken_found_recorded_sent_and_finished() -> None:
    """Порядок §24.3: взять, найти, записать ответ со следом, отправить ответом на
    просьбу, пометить `done` с id сообщения."""
    store = FakeStore(row(attempts=0))
    service, model, replier = build_searches(store)

    assert service.launch(SEARCH_ID) is True
    await service.wait()

    assert store.names() == ["take", "previous", "record_answer", "finish"]
    assert store.calls[0] == ("take", (SEARCH_ID, NOW - timedelta(minutes=10)))
    assert store.calls[1] == ("previous", (CREATED, CREATED - timedelta(hours=1)))
    answer, trace = store.answers[SEARCH_ID]
    assert answer == ANSWER
    assert trace == SearchTrace(
        input_tokens=300, output_tokens=800, web_searches=1, web_fetches=0, duration_ms=20000
    )
    assert replier.sent == [(OWNER_ID, 4242, ANSWER)]
    assert store.finished == {SEARCH_ID: 9001}
    system, messages = model.calls[0]
    assert system.startswith(search.SEARCH_RULES)
    assert messages == [{"role": "user", "content": QUERY}]


async def test_previous_search_and_known_facts_reach_the_call() -> None:
    store = FakeStore(row(attempts=0))
    store.past = PastSearch(query="билеты в Москву 15 октября", answer="1. Победа")
    home = Fact(id="f1", category="home", text="Живу на Уралмаше", status="fact")
    service, model, _ = build_searches(store, known=[home])

    service.launch(SEARCH_ID)
    await service.wait()

    system, messages = model.calls[0]
    assert "Живу на Уралмаше" in system
    assert messages[0]["content"] == search.build_search_request(QUERY, store.past)


async def test_search_not_taken_does_nothing() -> None:
    """Поиск уже ищется, завершён или чужой — модель не зовётся."""
    store = FakeStore()
    service, model, replier = build_searches(store)

    service.launch(SEARCH_ID)
    await service.wait()

    assert store.names() == ["take"]
    assert model.calls == []
    assert replier.sent == []


async def test_failed_attempt_is_released_and_said_nothing() -> None:
    store = FakeStore(row(attempts=0))
    service, _, replier = build_searches(store, model=FakeModel(RuntimeError("сбой")))

    service.launch(SEARCH_ID)
    await service.wait()

    assert store.names() == ["take", "previous", "release"]
    assert replier.sent == []
    assert store.status[SEARCH_ID] == "pending"


async def test_third_failed_attempt_says_so_and_then_fails_the_search() -> None:
    """Три попытки (§24.6): «Не получилось поискать «…»», потом `failed`."""
    store = FakeStore(row(attempts=2))
    service, _, replier = build_searches(store, model=FakeModel(RuntimeError("сбой")))

    service.launch(SEARCH_ID)
    await service.wait()

    assert store.names() == ["take", "previous", "release", "fail"]
    assert replier.sent == [(OWNER_ID, 4242, texts.search_failed(QUERY))]
    assert store.status[SEARCH_ID] == "failed"


async def test_notice_not_sent_keeps_the_search_waiting() -> None:
    """Отказ не ушёл — `failed` не ставится: следующий тик скажет снова."""
    store = FakeStore(row(attempts=2))
    service, _, _ = build_searches(
        store, model=FakeModel(RuntimeError("сбой")), replier=FakeReplier(broken=True)
    )

    service.launch(SEARCH_ID)
    await service.wait()

    assert "fail" not in store.names()
    assert store.status[SEARCH_ID] == "pending"


async def test_answer_not_recorded_is_not_sent() -> None:
    """Без записи ответ не уходит (§24.3, шаг 4): попытка возвращается."""
    store = FakeStore(row(attempts=0), broken=["record_answer"])
    service, _, replier = build_searches(store)

    service.launch(SEARCH_ID)
    await service.wait()

    assert store.names() == ["take", "previous", "record_answer", "release"]
    assert replier.sent == []


async def test_answer_not_sent_stays_recorded_and_the_log_has_no_token(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Не ушло в Telegram — строка с ответом ждёт следующего тика; в журнале —
    только тип ошибки: в её тексте адрес с токеном бота (§24.6)."""
    store = FakeStore(row(attempts=0))
    service, _, _ = build_searches(store, replier=FakeReplier(broken=True))

    with caplog.at_level(logging.INFO):
        service.launch(SEARCH_ID)
        await service.wait()

    assert store.names() == ["take", "previous", "record_answer"]
    assert store.rows[SEARCH_ID].answer == ANSWER
    assert "secret" not in caplog.text
    assert "RuntimeError" in caplog.text


async def test_log_has_numbers_but_no_query_answer_or_links(
    caplog: pytest.LogCaptureFixture,
) -> None:
    store = FakeStore(row(attempts=0))
    service, _, _ = build_searches(store)

    with caplog.at_level(logging.INFO):
        service.launch(SEARCH_ID)
        await service.wait()

    assert "поисков 1, страниц 0, токенов 300/800" in caplog.text
    assert "ссылок не из результатов 0" in caplog.text
    assert QUERY not in caplog.text
    assert "Победа" not in caplog.text
    assert "flypobeda" not in caplog.text


async def test_searches_go_one_at_a_time_in_order() -> None:
    """По одному и по порядку (§24.3): второй ждёт, пока найдётся первый."""
    first, second = row("a", attempts=0, request_message_id=1), row("b", attempts=0)
    store = FakeStore(first, second)
    model = FakeModel(found("1. первый"), found("1. второй"), pause=0.01)
    service, model, replier = build_searches(store, model=model)

    service.launch("a")
    service.launch("b")
    await service.wait()

    assert model.most == 1
    assert [sent[2] for sent in replier.sent] == ["1. первый", "1. второй"]


async def test_the_same_search_is_launched_once() -> None:
    store = FakeStore(row(attempts=0))
    service, model, _ = build_searches(store, model=FakeModel(found(), pause=0.01))

    assert service.launch(SEARCH_ID) is True
    assert service.launch(SEARCH_ID) is False
    await service.wait()

    assert len(model.calls) == 1


async def test_start_goes_to_the_store() -> None:
    store = FakeStore()
    service, _, _ = build_searches(store)

    assert await service.start(message_id=MESSAGE_ID, query=QUERY) == SEARCH_ID
    assert store.calls == [("start", (MESSAGE_ID, QUERY))]


async def test_tick_sends_a_recorded_answer_without_a_new_search() -> None:
    """Ответ записан, а не ушёл (§24.3, шаг 4): тик шлёт записанный."""
    waiting = row(answer=ANSWER)
    store = FakeStore(waiting)
    store.resume = [waiting]
    service, model, replier = build_searches(store)

    sent = await service.resume()

    assert sent == 1
    assert store.calls[0] == ("to_resume", (NOW - timedelta(minutes=10),))
    assert replier.sent == [(OWNER_ID, 4242, ANSWER)]
    assert store.finished == {SEARCH_ID: 9001}
    assert model.calls == []


async def test_tick_says_late_after_six_hours_and_fails_the_search() -> None:
    old = row(attempts=1, created_at=NOW - timedelta(hours=6, minutes=1))
    store = FakeStore(old)
    store.resume = [old]
    service, model, replier = build_searches(store)

    assert await service.resume() == 1

    assert replier.sent == [(OWNER_ID, 4242, texts.search_late(QUERY))]
    assert store.status[SEARCH_ID] == "failed"
    assert model.calls == []


async def test_tick_says_failed_after_three_attempts_without_searching() -> None:
    """Третья попытка брошена перезапуском — четвёртой нет, только отказ."""
    tried = row(attempts=3)
    store = FakeStore(tried)
    store.resume = [tried]
    service, model, replier = build_searches(store)

    assert await service.resume() == 1

    assert replier.sent == [(OWNER_ID, 4242, texts.search_failed(QUERY))]
    assert store.status[SEARCH_ID] == "failed"
    assert model.calls == []


async def test_tick_launches_an_abandoned_search_and_does_not_wait() -> None:
    abandoned = row(attempts=1)
    store = FakeStore(abandoned)
    store.resume = [abandoned]
    service, model, replier = build_searches(store, model=FakeModel(found(), pause=0.01))

    assert await service.resume() == 0
    assert model.calls == []

    await service.wait()
    assert replier.sent == [(OWNER_ID, 4242, ANSWER)]
    assert store.rows[SEARCH_ID].attempts == 2


async def test_tick_does_not_touch_a_search_in_the_queue() -> None:
    queued = row(attempts=0)
    store = FakeStore(queued)
    store.resume = [queued]
    service, model, _ = build_searches(store, model=FakeModel(found(), pause=0.01))

    service.launch(SEARCH_ID)
    assert await service.resume() == 0
    await service.wait()

    assert len(model.calls) == 1
    assert store.names().count("take") == 1


async def test_tick_survives_a_silent_base() -> None:
    store = FakeStore(broken=["to_resume"])
    service, _, _ = build_searches(store)

    assert await service.resume() == 0


# ------------------------------------------- приём сообщения с поиском (§24.3)


class FakeStarter:
    """Вместо `start_search` — список заведённого; `broken` — база не ответила.

    `seen` — сколько записей разбора было к моменту заведения: строка поиска
    обязана лечь раньше, чем ответ «Ищу» (инварианты 4 и 5).
    """

    def __init__(self, understandings: FakeUnderstandings, broken: bool = False) -> None:
        self.understandings = understandings
        self.broken = broken
        self.calls: list[tuple[str, str]] = []
        self.seen: list[int] = []

    async def __call__(self, *, message_id: str, query: str) -> str:
        self.calls.append((message_id, query))
        self.seen.append(len(self.understandings.calls))
        if self.broken:
            raise DatabaseError("ConnectTimeout: timed out")
        return SEARCH_ID


def tasks_service(
    verdict: Understanding | Verdict,
    *,
    broken_start: bool = False,
    messages: FakeMessages | None = None,
    understandings: FakeUnderstandings | None = None,
    photo: PhotoUnderstanding | None = None,
) -> tuple[TaskService, FakeStarter, FakeUnderstandings, FakeAnalyst]:
    """Приём поручения на подменённой базе и модели — с заведением поиска."""
    recorder = understandings or FakeUnderstandings()
    starter = FakeStarter(recorder, broken=broken_start)
    analyst = FakeAnalyst(verdict, photo=photo)
    service = TaskService(
        settings=make_settings(),
        record_message=messages or FakeMessages(),
        record_understanding=recorder,
        analyst=analyst,
        transcriber=FakeTranscriber(),
        planner=FakePlanner(),
        clock=lambda: NOW,
        start_search=starter,
    )
    return service, starter, recorder, analyst


async def say(service: TaskService, text_: str, forwarded_from: str | None = None) -> RecordOutcome:
    return await service.record_from_message(
        chat_id=OWNER_ID, telegram_message_id=7, text=text_, forwarded_from=forwarded_from
    )


def tickets(**fields: Any) -> MessageUnderstanding:
    """Разбор просьбы найти билеты."""
    return make_message_understanding(**{"kind": "search", "title": QUERY, **fields})


async def test_search_row_is_written_before_the_reply_says_searching() -> None:
    """Приёмка 1, 12, 13: строка поиска — раньше «Ищу» (§24.3); ответ — «Ищу:
    …», задачи нет, в `messages.reply` — только «Ищу»."""
    service, starter, understandings, _ = tasks_service(tickets())

    outcome = await say(service, "найди билеты в Москву на 15-е")

    assert outcome.ok
    assert outcome.message == f"Ищу: {QUERY}. Пришлю, как найду."
    assert outcome.search_id == SEARCH_ID
    assert starter.calls == [("9a71", QUERY)]
    assert starter.seen == [0]
    [call] = understandings.calls
    assert call["reply"] == outcome.message
    assert call["tasks"] == []
    assert cast(dict[str, Any], call["analysis"])["kind"] == "search"


async def test_search_not_written_is_not_announced() -> None:
    """Приёмка 11: строка не записалась — «Ищу» не звучит, поиска нет."""
    service, _, understandings, _ = tasks_service(tickets(), broken_start=True)

    outcome = await say(service, "найди билеты в Москву на 15-е")

    assert outcome.message == texts.SEARCH_NOT_SAVED
    assert outcome.search_id is None
    assert understandings.calls[0]["reply"] == texts.SEARCH_NOT_SAVED


async def test_tasks_are_recorded_and_the_search_is_started() -> None:
    """Приёмка 8: дело записано как раньше, «Ищу» — отдельным абзацем после."""
    verdict = make_message_understanding(title="позвонить Игорю", more_searches=[QUERY])
    service, starter, understandings, _ = tasks_service(verdict)

    outcome = await say(service, "позвони Игорю и найди билеты в Москву на 15-е")

    assert outcome.message == f"Записал: позвонить Игорю\n\nИщу: {QUERY}. Пришлю, как найду."
    assert outcome.search_id == SEARCH_ID
    rows = cast(list[dict[str, Any]], understandings.calls[0]["tasks"])
    assert [entry["item"] for entry in rows] == [1]
    assert starter.calls == [("9a71", QUERY)]


async def test_second_search_of_the_message_is_not_started() -> None:
    """Приёмка 8: из сообщения ищется один поиск, второй — «Ищу по одному»."""
    service, starter, _, _ = tasks_service(tickets(more_searches=["школа с математикой"]))

    outcome = await say(service, "найди билеты и школу с математикой")

    assert outcome.message == (
        f"Ищу: {QUERY}. Пришлю, как найду.\n\n"
        "Ищу по одному: «школа с математикой» поищу, если попросите отдельно."
    )
    assert starter.calls == [("9a71", QUERY)]


async def test_question_stays_the_last_paragraph_after_the_search() -> None:
    """Вопрос — всегда последний абзац (§23.4): «Ищу» встаёт перед ним."""
    verdict = make_message_understanding(
        title="позвонить", question="Кому позвонить?", needs_review=True, more_searches=[QUERY]
    )
    service, _, understandings, _ = tasks_service(verdict)

    outcome = await say(service, "позвонить и найди билеты")

    assert outcome.message == (
        f"Записал: позвонить\n\nИщу: {QUERY}. Пришлю, как найду.\n\nКому позвонить?"
    )
    rows = cast(list[dict[str, Any]], understandings.calls[0]["tasks"])
    assert rows[0]["task"]["open_question"] == "Кому позвонить?"


async def test_forwarded_request_does_not_search() -> None:
    """Пересланное — слова отправителя (инвариант 3): поиска нет, просьба
    сказать текстом или голосом."""
    service, starter, _, _ = tasks_service(tickets())

    outcome = await say(service, "найди мне билеты", forwarded_from="Олег")

    assert outcome.message == texts.SEARCH_TEXT_ONLY
    assert outcome.search_id is None
    assert starter.calls == []


async def test_message_not_recorded_starts_no_search() -> None:
    """Запись разбора упала — «Не смог записать», и запускать нечего; строка
    поиска остаётся без «Ищу», и тик её не возьмёт (`searches_to_resume`)."""
    service, starter, _, _ = tasks_service(
        tickets(), understandings=FakeUnderstandings(broken=True)
    )

    outcome = await say(service, "найди билеты в Москву на 15-е")

    assert outcome.ok is False
    assert outcome.message == texts.NOT_SAVED
    assert outcome.search_id is None
    assert starter.calls == [("9a71", QUERY)]


async def test_repeated_update_answers_with_the_saved_reply_and_starts_nothing() -> None:
    saved = SavedMessage(id="9a71", reply=f"Ищу: {QUERY}. Пришлю, как найду.")
    service, starter, _, _ = tasks_service(tickets(), messages=FakeMessages(message=saved))

    outcome = await say(service, "найди билеты в Москву на 15-е")

    assert outcome.message == saved.reply
    assert outcome.search_id is None
    assert starter.calls == []


async def test_voice_request_searches_too() -> None:
    service, starter, _, _ = tasks_service(tickets())

    outcome = await service.record_from_voice(
        chat_id=OWNER_ID,
        telegram_message_id=7,
        kind="voice",
        file_id="voice-1",
        duration=4,
        load_audio=load_audio,
    )

    assert outcome.message == f"Ищу: {QUERY}. Пришлю, как найду."
    assert outcome.search_id == SEARCH_ID
    assert starter.calls == [("9a71", QUERY)]


async def test_photo_request_does_not_search() -> None:
    """Приёмка 15: снимок поиска не запускает — просьба сказать текстом или голосом."""
    photo = make_photo_understanding(kind="search", title="кроссовки как на фото")
    service, starter, understandings, _ = tasks_service(
        NotUnderstood(reason="текста тест не ждал"), photo=photo
    )

    outcome = await service.record_from_photo(
        chat_id=OWNER_ID,
        telegram_message_id=7,
        file_id="photo-1",
        media_type="image/jpeg",
        caption="найди такие же",
        load_image=load_image,
    )

    assert outcome.message == texts.SEARCH_TEXT_ONLY
    assert outcome.search_id is None
    assert starter.calls == []
    assert understandings.calls[0]["tasks"] == []


async def test_forwarded_conversation_does_not_search() -> None:
    """Приёмка 15: переписка, пересланная разом, поиска не запускает."""
    analyst = FakeAnalyst(
        NotUnderstood(reason="одно сообщение тест не ждал"),
        conversation=make_conversation_understanding(kind="search", title="школа"),
    )
    service, _, understandings = conversation_service(analyst)

    replies = await send(service, *CHAT)

    assert replies[-1] == texts.SEARCH_TEXT_ONLY
    assert record_of(understandings, HEAD)["tasks"] == []


def test_message_with_a_search_answers_like_several_tasks() -> None:
    """Кнопки под ответом с «Ищу» шлют итог новым сообщением (§23.5, §24.4)."""
    assert is_several(tickets()) is True
    assert is_several(make_message_understanding(more_searches=[QUERY])) is True
    assert is_several(make_message_understanding()) is False


# ---------------------------------------------- обработчик, тик и сборка


class FakeSearches:
    """Вместо очереди поисков — запуски и сколько сообщений ушло к запуску."""

    def __init__(self, session: RecordingSession) -> None:
        self.session = session
        self.launched: list[tuple[str, int]] = []

    def launch(self, search_id: str) -> bool:
        self.launched.append((search_id, len(self.session.texts)))
        return True


async def test_handler_launches_the_search_after_the_reply(
    bot: Bot, session: RecordingSession, settings: Settings
) -> None:
    """«Ищу» уходит первым, поиск запускается потом — ответ его не обгонит."""
    service, _, _, _ = tasks_service(tickets())
    searches = FakeSearches(session)
    dispatcher = build_dispatcher(
        settings, tasks=service, searches=cast(search.SearchService, searches)
    )

    await dispatcher.feed_update(bot, make_update("найди билеты в Москву на 15-е"))

    assert session.texts == [f"Ищу: {QUERY}. Пришлю, как найду."]
    assert searches.launched == [(SEARCH_ID, 1)]


async def test_handler_launches_nothing_without_a_search(
    bot: Bot, session: RecordingSession, settings: Settings
) -> None:
    service, _, _, _ = tasks_service(make_message_understanding(title="купить лампочку"))
    searches = FakeSearches(session)
    dispatcher = build_dispatcher(
        settings, tasks=service, searches=cast(search.SearchService, searches)
    )

    await dispatcher.feed_update(bot, make_update("купить лампочку"))

    assert searches.launched == []


async def test_voice_handler_launches_the_search_after_the_reply(
    bot: Bot, session: RecordingSession, settings: Settings
) -> None:
    service, _, _, _ = tasks_service(tickets())
    searches = FakeSearches(session)
    dispatcher = build_dispatcher(
        settings, tasks=service, searches=cast(search.SearchService, searches)
    )

    await dispatcher.feed_update(bot, make_voice_update())

    assert session.texts == [f"Ищу: {QUERY}. Пришлю, как найду."]
    assert searches.launched == [(SEARCH_ID, 1)]


class FakeResumer:
    """Шаг поисков тика: сколько сообщений «ушло»; `broken` — шаг упал."""

    def __init__(self, sent: int = 0, broken: bool = False) -> None:
        self.sent = sent
        self.broken = broken
        self.calls: list[datetime | None] = []

    async def resume(self, now: datetime | None = None) -> int:
        self.calls.append(now)
        if self.broken:
            raise RuntimeError("шаг поисков упал")
        return self.sent


def ticking(resumer: FakeResumer, undated: FakeUndated) -> ReminderService:
    return ReminderService(
        settings=make_settings(),
        due=FakeDue(),
        mark_sent=FakeMarks(),
        close_task=FakeCloser(),
        notify=FakeNotifier(),
        moved=FakeMoved(),
        clear_moved=FakeClearMoved(),
        announce=FakeAnnouncer(),
        clock=lambda: SATURDAY_NOON,
        undated=undated,
        record_ask=FakeAskRecorder(),
        searches=resumer,
    )


async def test_tick_resumes_searches_and_counts_what_they_sent() -> None:
    """Шаг поисков (§6.2, §24.3): ушедший ответ поиска — не тишина, вопрос о
    деле без срока в этом тике не задаётся."""
    resumer = FakeResumer(sent=1)
    undated = FakeUndated(make_undated())
    service = ticking(resumer, undated)

    assert await service.tick(SATURDAY_NOON) == 1
    assert resumer.calls == [SATURDAY_NOON]
    assert undated.calls == []


async def test_failed_search_step_does_not_stop_the_tick(
    caplog: pytest.LogCaptureFixture,
) -> None:
    resumer = FakeResumer(broken=True)
    undated = FakeUndated(make_undated())
    service = ticking(resumer, undated)

    with caplog.at_level(logging.ERROR):
        assert await service.tick(SATURDAY_NOON) == 1

    assert len(undated.calls) == 1, "вопрос о деле без срока ушёл как обычно"
    assert "Шаг поисков" in caplog.text


def test_dispatcher_carries_the_searches(settings: Settings, session: RecordingSession) -> None:
    searches = FakeSearches(session)

    dispatcher = build_dispatcher(settings, searches=cast(search.SearchService, searches))

    assert dispatcher["searches"] is searches
