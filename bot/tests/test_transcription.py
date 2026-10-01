"""Распознавание речи: ответ Deepgram, тишина, отказы и подсказки. Сети здесь нет.

Deepgram подменён протоколом `SpeechCall`: проверяется, что бот делает с
каждым видом ответа и отказа (`techspec/09-voice.md` §9.3) — результат,
а не исключение, — и что уходит подсказками (§9.5).
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, cast

import httpx
import pytest
from deepgram import AsyncDeepgramClient
from deepgram.core.api_error import ApiError
from deepgram.core.parse_error import ParsingError
from deepgram.types import ListenV1Response

from solomon.services.names import forms, known_names, volume
from solomon.services.transcription import (
    KEYTERM_LIMIT,
    LOW_CONFIDENCE,
    DeepgramTranscriber,
    NotTranscribed,
    SpeechAnswer,
    Transcript,
    deepgram_call,
    read_transcript,
)

AUDIO = b"OggS\x00fake-opus"
SPOKEN = "В пятницу отправить расчёт клиенту."


@dataclass(frozen=True, slots=True)
class FakeAlternative:
    transcript: str | None
    confidence: float | None = 0.93


@dataclass(frozen=True, slots=True)
class FakeChannel:
    alternatives: list[FakeAlternative] | None


@dataclass(frozen=True, slots=True)
class FakeResults:
    channels: list[FakeChannel]


@dataclass(frozen=True, slots=True)
class FakeAnswer:
    """Ответ SDK без сети: ровно те поля, которые читает сервис."""

    results: FakeResults


def answer(text: str | None, confidence: float | None = 0.93) -> FakeAnswer:
    return FakeAnswer(FakeResults([FakeChannel([FakeAlternative(text, confidence)])]))


class FakeCall:
    """Один запрос к распознаванию: либо готовый ответ, либо заготовленный отказ.

    `hint_error` — отказ только на запрос с подсказками: так Deepgram отвечает
    400, когда подсказок больше, чем он принимает (§9.5).
    """

    def __init__(
        self,
        answer: FakeAnswer | None = None,
        error: Exception | None = None,
        hint_error: Exception | None = None,
    ) -> None:
        self.answer = answer
        self.error = error
        self.hint_error = hint_error
        self.calls: list[bytes] = []
        self.keyterms: list[tuple[str, ...]] = []

    async def __call__(self, audio: bytes, keyterms: Sequence[str]) -> SpeechAnswer:
        self.calls.append(audio)
        self.keyterms.append(tuple(keyterms))
        if keyterms and self.hint_error is not None:
            raise self.hint_error
        if self.error is not None:
            raise self.error
        assert self.answer is not None
        return self.answer


def build(
    answer: FakeAnswer | None = None,
    error: Exception | None = None,
    hint_error: Exception | None = None,
) -> tuple[DeepgramTranscriber, FakeCall]:
    call = FakeCall(answer=answer, error=error, hint_error=hint_error)
    return DeepgramTranscriber(call), call


# ------------------------------------------------------------ чтение ответа


def test_transcript_is_the_first_alternative_of_the_first_channel() -> None:
    assert read_transcript(answer(SPOKEN, 0.93)) == Transcript(text=SPOKEN, confidence=0.93)


def test_transcript_is_stripped() -> None:
    result = read_transcript(answer(f"  {SPOKEN}\n"))

    assert isinstance(result, Transcript)
    assert result.text == SPOKEN


@pytest.mark.parametrize("silence", ["", "   ", None])
def test_silence_is_not_transcribed(silence: str | None) -> None:
    """Тишина или не речь: Deepgram отвечает пустой строкой (§9.3)."""
    assert isinstance(read_transcript(answer(silence)), NotTranscribed)


def test_answer_without_channels_or_alternatives_is_not_transcribed() -> None:
    assert isinstance(read_transcript(FakeAnswer(FakeResults([]))), NotTranscribed)
    assert isinstance(read_transcript(FakeAnswer(FakeResults([FakeChannel(None)]))), NotTranscribed)


def test_missing_confidence_is_kept_as_unknown() -> None:
    result = read_transcript(answer(SPOKEN, None))

    assert isinstance(result, Transcript)
    assert result.confidence is None
    # Неизвестное качество — не низкое: без оценки модели ничего не говорится.
    assert not result.low_confidence


def test_low_confidence_is_below_the_threshold() -> None:
    """Порог живёт здесь, а не в промпте (§9.4)."""
    assert LOW_CONFIDENCE == 0.6
    assert Transcript(SPOKEN, 0.59).low_confidence
    assert not Transcript(SPOKEN, 0.6).low_confidence
    assert not Transcript(SPOKEN, 0.93).low_confidence


# ---------------------------------------------------------------- транскрайбер


async def test_audio_goes_to_the_call_as_is() -> None:
    transcriber, call = build(answer=answer(SPOKEN))

    result = await transcriber.transcribe(AUDIO)

    assert result == Transcript(text=SPOKEN, confidence=0.93)
    # Файл не режется и не сжимается: уходит как есть (спека «Чего не делаем»).
    assert call.calls == [AUDIO]


async def test_silence_from_the_call_is_not_transcribed() -> None:
    transcriber, _ = build(answer=answer(""))

    assert isinstance(await transcriber.transcribe(AUDIO), NotTranscribed)


async def test_timeout_is_not_transcribed() -> None:
    transcriber, _ = build(error=httpx.ReadTimeout("slow"))

    result = await transcriber.transcribe(AUDIO)

    assert isinstance(result, NotTranscribed)
    assert "таймаут" in result.reason


async def test_connection_error_is_not_transcribed() -> None:
    transcriber, _ = build(error=httpx.ConnectError("no route to host"))

    result = await transcriber.transcribe(AUDIO)

    assert isinstance(result, NotTranscribed)
    assert "ConnectError" in result.reason


async def test_server_error_is_not_transcribed() -> None:
    transcriber, _ = build(error=ApiError(status_code=503, body="unavailable"))

    result = await transcriber.transcribe(AUDIO)

    assert isinstance(result, NotTranscribed)
    assert "503" in result.reason


async def test_bad_key_names_the_variable_in_the_log(caplog: pytest.LogCaptureFixture) -> None:
    transcriber, _ = build(error=ApiError(status_code=401, body="invalid credentials"))

    with caplog.at_level(logging.ERROR):
        result = await transcriber.transcribe(AUDIO)

    # Ошибка настройки: человеку тот же ответ, а в журнале — что чинить (§9.3).
    assert isinstance(result, NotTranscribed)
    assert "DEEPGRAM_API_KEY" in caplog.text


async def test_unparsable_answer_is_not_transcribed() -> None:
    error = ParsingError(status_code=200, body={"odd": True}, cause=ValueError("shape"))
    transcriber, _ = build(error=error)

    assert isinstance(await transcriber.transcribe(AUDIO), NotTranscribed)


async def test_failure_reason_stays_out_of_the_transcript() -> None:
    """Причина — для журнала: наружу уходит только вид результата."""
    transcriber, _ = build(error=ApiError(status_code=500, body="stack trace here"))

    result = await transcriber.transcribe(AUDIO)

    assert isinstance(result, NotTranscribed)
    assert "stack trace" not in result.reason


# ------------------------------------------------------------------ подсказки


async def test_names_go_as_hints_with_all_their_forms() -> None:
    """Каждое имя — всеми падежами, в порядке имён (§9.5)."""
    transcriber, call = build(answer=answer(SPOKEN))

    result = await transcriber.transcribe(AUDIO, names=["Юлай", "Рената"])

    assert isinstance(result, Transcript)
    assert call.keyterms == [(*forms("Юлай"), *forms("Рената"))]


async def test_no_names_means_no_hints() -> None:
    transcriber, call = build(answer=answer(SPOKEN))

    await transcriber.transcribe(AUDIO)

    assert call.keyterms == [()]


def test_names_of_the_owner_today_fit_the_limit() -> None:
    """Имена владельца на 2026-10-01 (8 имён, 37 подсказок) входят целиком."""
    memory = ["Зовут Тимофей", "Машина — Volkswagen Polo 2015 года"]
    people = [
        ("Антон Ширкалин",),
        ("Саша Уваров",),
        ("Анна",),
        ("Рената", "Александра"),
        ("Юлай",),
    ]
    terms = [form for name in known_names(memory, people) for form in forms(name)]

    assert len(terms) == 37
    assert volume(terms) <= KEYTERM_LIMIT


async def test_hints_stay_within_the_limit_and_the_log_has_numbers_not_names(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Сколько вошло и сколько нет — числами; самих имён в журнале нет (§9.5)."""
    names = [f"Тимур{first}{second}н" for first in "абвгд" for second in "абвгд"]
    transcriber, call = build(answer=answer(SPOKEN))

    with caplog.at_level(logging.INFO):
        await transcriber.transcribe(AUDIO, names=names)

    [terms] = call.keyterms
    assert 0 < volume(terms) <= KEYTERM_LIMIT
    taken = len(terms) // len(forms(names[0]))
    left_out = len(names) - taken
    assert left_out > 0
    hint_lines = [r.getMessage() for r in caplog.records if "Подсказки" in r.getMessage()]
    assert hint_lines == [
        f"Подсказки распознаванию: имён {taken}, подсказок {len(terms)}, не вошло имён {left_out}"
    ]
    assert "Тимур" not in caplog.text


async def test_rejected_hints_are_retried_once_without_them(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """400 на запрос с подсказками — тот же файл ещё раз, без них (§9.5)."""
    rejected = ApiError(status_code=400, body="Keyterm limit exceeded")
    transcriber, call = build(answer=answer(SPOKEN), hint_error=rejected)

    with caplog.at_level(logging.WARNING):
        result = await transcriber.transcribe(AUDIO, names=["Юлай"])

    assert result == Transcript(text=SPOKEN, confidence=0.93)
    assert call.calls == [AUDIO, AUDIO]
    assert call.keyterms == [tuple(forms("Юлай")), ()]
    assert "400" in caplog.text
    assert "Юлай" not in caplog.text


async def test_failed_retry_is_not_transcribed() -> None:
    """Повтор один: его отказ — обычное «не расслышал» (§9.3)."""
    transcriber, call = build(
        error=ApiError(status_code=503, body="unavailable"),
        hint_error=ApiError(status_code=400, body="Keyterm limit exceeded"),
    )

    result = await transcriber.transcribe(AUDIO, names=["Юлай"])

    assert isinstance(result, NotTranscribed)
    assert "503" in result.reason
    assert len(call.calls) == 2


async def test_bad_request_without_hints_is_not_retried() -> None:
    transcriber, call = build(error=ApiError(status_code=400, body="corrupt audio"))

    result = await transcriber.transcribe(AUDIO)

    assert isinstance(result, NotTranscribed)
    assert len(call.calls) == 1


@pytest.mark.parametrize(
    "error",
    [ApiError(status_code=503, body="unavailable"), httpx.ReadTimeout("slow")],
)
async def test_other_failures_with_hints_are_not_retried(error: Exception) -> None:
    """Повтор — только на 400 с подсказками; прочие отказы — §9.3, без повтора."""
    transcriber, call = build(hint_error=error)

    result = await transcriber.transcribe(AUDIO, names=["Юлай"])

    assert isinstance(result, NotTranscribed)
    assert len(call.calls) == 1


# ------------------------------------------------------------- настоящий вызов


class RecordingMedia:
    """`client.listen.v1.media` без сети: запоминает аргументы запроса."""

    def __init__(self) -> None:
        self.requests: list[dict[str, Any]] = []

    async def transcribe_file(self, **kwargs: Any) -> ListenV1Response:
        self.requests.append(kwargs)
        return ListenV1Response.model_construct()


class RecordingClient:
    """Клиент Deepgram без сети: `listen.v1.media` — один и тот же записывающий объект."""

    def __init__(self) -> None:
        self.media = RecordingMedia()
        self.listen = self
        self.v1 = self


async def test_request_without_hints_has_no_keyterm() -> None:
    """Имён нет — запрос как до этапа 014, без `keyterm` (§9.5)."""
    client = RecordingClient()

    await deepgram_call(cast(AsyncDeepgramClient, client))(AUDIO, ())

    [request] = client.media.requests
    # None SDK в запрос не кладёт (deepgram-sdk убирает пустые параметры).
    assert request["keyterm"] is None
    assert request["model"] == "nova-3"
    assert request["language"] == "ru"
    assert request["smart_format"] is True


async def test_request_with_hints_sends_them_as_keyterm() -> None:
    client = RecordingClient()

    await deepgram_call(cast(AsyncDeepgramClient, client))(AUDIO, ("Юлай", "Юлая"))

    [request] = client.media.requests
    assert request["keyterm"] == ["Юлай", "Юлая"]
    assert request["request"] == AUDIO
