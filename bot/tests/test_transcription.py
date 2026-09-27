"""Распознавание речи: ответ Deepgram, тишина и отказы. Сети здесь нет.

Deepgram подменён протоколом `SpeechCall`: проверяется, что бот делает с
каждым видом ответа и отказа (`techspec/09-voice.md` §9.3) — результат,
а не исключение.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

import httpx
import pytest
from deepgram.core.api_error import ApiError
from deepgram.core.parse_error import ParsingError

from solomon.services.transcription import (
    LOW_CONFIDENCE,
    DeepgramTranscriber,
    NotTranscribed,
    SpeechAnswer,
    Transcript,
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
    """Один запрос к распознаванию: либо готовый ответ, либо заготовленный отказ."""

    def __init__(self, answer: FakeAnswer | None = None, error: Exception | None = None) -> None:
        self.answer = answer
        self.error = error
        self.calls: list[bytes] = []

    async def __call__(self, audio: bytes) -> SpeechAnswer:
        self.calls.append(audio)
        if self.error is not None:
            raise self.error
        assert self.answer is not None
        return self.answer


def build(
    answer: FakeAnswer | None = None, error: Exception | None = None
) -> tuple[DeepgramTranscriber, FakeCall]:
    call = FakeCall(answer=answer, error=error)
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
