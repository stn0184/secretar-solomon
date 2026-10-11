"""Разобрать позже, если модель недоступна (`techspec/33-retry-later.md`).

Временный отказ (посредник или сеть) отличается от постоянного (§33.1):
первый ждёт в базе и повторяется тиком с растущей паузой, второй сразу идёт
прежним путём «как есть» (§5.4). Модель подменена: 503, таймаут и успех
задаёт тест.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import httpx2
import pytest
from anthropic import (
    APIConnectionError,
    APIStatusError,
    APITimeoutError,
    AuthenticationError,
    BadRequestError,
    InternalServerError,
    RateLimitError,
)
from pydantic import ValidationError

from solomon.services.understanding import (
    NotUnderstood,
    UnderstandingService,
    is_temporary,
    next_retry,
    retry_pause,
)
from tests.conftest import OWNER_TIMEZONE, make_settings

ZONE = ZoneInfo(OWNER_TIMEZONE)
NOW = datetime(2026, 10, 11, 16, 18, tzinfo=ZONE)
REQUEST = httpx2.Request("POST", "https://api.anthropic.com/v1/messages")


def status_error(code: int) -> APIStatusError:
    """Ответ посредника с кодом `code` — как его поднимает SDK."""
    return APIStatusError("boom", response=httpx2.Response(code, request=REQUEST), body=None)


def schema_error() -> ValidationError:
    """Ответ не по схеме — исключение pydantic, как у `messages.parse`."""
    from solomon.services.understanding import Understanding

    try:
        Understanding.model_validate({"kind": "не вид"})
    except ValidationError as error:
        return error
    raise AssertionError("схема приняла чужой вид")


# --- Временный и постоянный отказ (§33.1) ------------------------------------


@pytest.mark.parametrize(
    "error",
    [
        APITimeoutError(REQUEST),
        APIConnectionError(request=REQUEST),
        RateLimitError("429", response=httpx2.Response(429, request=REQUEST), body=None),
        InternalServerError("500", response=httpx2.Response(500, request=REQUEST), body=None),
        status_error(502),
        status_error(503),
        status_error(504),
        # «Перегружен» у Anthropic — 529.
        status_error(529),
        # Таймаут на стороне посредника.
        status_error(408),
    ],
    ids=["timeout", "connection", "429", "500", "502", "503", "504", "529", "408"],
)
def test_network_and_overload_are_temporary(error: Exception) -> None:
    assert is_temporary(error)


@pytest.mark.parametrize(
    "error",
    [
        AuthenticationError("401", response=httpx2.Response(401, request=REQUEST), body=None),
        BadRequestError("400", response=httpx2.Response(400, request=REQUEST), body=None),
        status_error(403),
        status_error(404),
        status_error(413),
        status_error(422),
        schema_error(),
        ValueError("не то"),
    ],
    ids=["401", "400", "403", "404", "413", "422", "schema", "other"],
)
def test_refusals_with_an_answer_are_permanent(error: Exception) -> None:
    # Ответ есть, но непригоден: повтор его не вылечит (§33.1).
    assert not is_temporary(error)


class _Usage:
    input_tokens = 120
    output_tokens = 45


class _Answer:
    """Ответ SDK без сети: ровно те поля, которые читает сервис."""

    def __init__(self, stop_reason: str = "end_turn", parsed: bool = True) -> None:
        from tests.conftest import make_message_understanding

        self.parsed_output = make_message_understanding() if parsed else None
        self.stop_reason = stop_reason
        self.model = "claude-opus-5"
        self.usage = _Usage()


def _service(error: Exception | None = None, answer: _Answer | None = None) -> UnderstandingService:
    async def call(*, system: str, text: str) -> _Answer:
        if error is not None:
            raise error
        assert answer is not None
        return answer

    return UnderstandingService(settings=make_settings(), call=call, clock=lambda: NOW)


@pytest.mark.parametrize(
    "error",
    [APITimeoutError(REQUEST), status_error(503), status_error(529)],
    ids=["timeout", "503", "529"],
)
async def test_unavailable_model_is_a_temporary_refusal(error: Exception) -> None:
    verdict = await _service(error=error).analyze("купить лампочку")

    assert isinstance(verdict, NotUnderstood)
    assert verdict.temporary


@pytest.mark.parametrize(
    ("error", "answer"),
    [
        (status_error(400), None),
        (
            AuthenticationError("401", response=httpx2.Response(401, request=REQUEST), body=None),
            None,
        ),
        (schema_error(), None),
        (None, _Answer(parsed=False)),
        (None, _Answer(stop_reason="refusal")),
        (None, _Answer(stop_reason="max_tokens")),
    ],
    ids=["400", "401", "schema", "no-parsed", "refusal", "max_tokens"],
)
async def test_unusable_answer_is_a_permanent_refusal(
    error: Exception | None, answer: _Answer | None
) -> None:
    verdict = await _service(error=error, answer=answer).analyze("купить лампочку")

    assert isinstance(verdict, NotUnderstood)
    assert not verdict.temporary


# --- Растущая пауза (§33.2) ---------------------------------------------------


@pytest.mark.parametrize(
    ("failures", "minutes"),
    [(1, 1), (2, 2), (3, 5), (4, 10), (5, 15), (6, 30), (7, 30), (40, 30)],
)
def test_pause_grows_to_half_an_hour(failures: int, minutes: int) -> None:
    assert retry_pause(failures) == timedelta(minutes=minutes)


def test_pause_before_any_failure_is_the_first_one() -> None:
    # Сообщение встало в очередь, модель его ещё не видела: пауза как после первой.
    assert retry_pause(0) == timedelta(minutes=1)


def test_next_retry_is_after_the_pause() -> None:
    deadline = NOW + timedelta(hours=6)

    assert next_retry(NOW, 3, deadline) == NOW + timedelta(minutes=5)


def test_next_retry_does_not_go_past_the_deadline() -> None:
    # Последняя попытка — ровно на пределе ожидания, а не через полчаса после.
    deadline = NOW + timedelta(minutes=12)

    assert next_retry(NOW, 6, deadline) == deadline
