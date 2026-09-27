"""Распознавание речи: голосовое становится текстом поручения.

Источник правды — `techspec/09-voice.md`; этот модуль ему следует, а не
наоборот. Сеть трогает только `deepgram_call`: транскрайбер зовёт Deepgram
через протокол `SpeechCall`, а слой операций зовёт транскрайбер через
протокол `Transcriber`. Поэтому тесты подставляют свою функцию и ходят не
дальше памяти, а провайдер сменяется одним файлом (§9.2).

Ни один отказ наружу исключением не выходит: сообщение с файлом уже в базе
(инвариант 5), а слой выше честно отвечает «не расслышал» (§9.3).
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Protocol

import httpx
from deepgram import AsyncDeepgramClient
from deepgram.core.api_error import ApiError
from deepgram.core.parse_error import ParsingError
from deepgram.types import ListenV1Response

from solomon.config import Settings

logger = logging.getLogger(__name__)

# Русский поддержан явно; smart_format ставит пунктуацию и числа — модели
# разбора так проще (§9.2).
MODEL = "nova-3"
LANGUAGE = "ru"
# Голосовое в минуту распознаётся за секунды; дольше — что-то не так (§9.2).
TIMEOUT_SECONDS = 60.0
# Ниже этого модели говорится «качество низкое» (§9.4). Порог живёт здесь,
# а не в промпте.
LOW_CONFIDENCE = 0.6


@dataclass(frozen=True, slots=True)
class Transcript:
    """Расшифровка состоялась: текст и уверенность 0–1 (нет оценки — `None`)."""

    text: str
    confidence: float | None

    @property
    def low_confidence(self) -> bool:
        """Уверенность ниже порога — модели стоит сказать об этом (§9.4).

        Без оценки — не низкая: о качестве, которого никто не мерил, модели
        ничего не говорится.
        """
        return self.confidence is not None and self.confidence < LOW_CONFIDENCE


@dataclass(frozen=True, slots=True)
class NotTranscribed:
    """Расшифровки нет (§9.3). Причина — для журнала, не для человека."""

    reason: str


TranscriptionResult = Transcript | NotTranscribed


class Transcriber(Protocol):
    """Речь в текст — то, что подменяет тест вместо сети."""

    async def transcribe(self, audio: bytes) -> TranscriptionResult: ...


# Ответ Deepgram — ровно те поля, которые читает транскрайбер. Свойства только
# на чтение: так настоящий `ListenV1Response` подходит под протокол без
# приведения типов, как `ModelAnswer` у модели.
class SpeechAlternative(Protocol):
    @property
    def transcript(self) -> str | None: ...

    @property
    def confidence(self) -> float | None: ...


class SpeechChannel(Protocol):
    @property
    def alternatives(self) -> Sequence[SpeechAlternative] | None: ...


class SpeechResults(Protocol):
    @property
    def channels(self) -> Sequence[SpeechChannel]: ...


class SpeechAnswer(Protocol):
    @property
    def results(self) -> SpeechResults: ...


class SpeechCall(Protocol):
    """Один запрос к распознаванию — ровно то, что подменяет тест."""

    async def __call__(self, audio: bytes) -> SpeechAnswer: ...


def create_deepgram_client(settings: Settings) -> AsyncDeepgramClient:
    """Клиент Deepgram. Создаётся один раз при запуске бота (§9.2).

    Ключ приходит только из окружения (инвариант 1).
    """
    return AsyncDeepgramClient(api_key=settings.deepgram_api_key, timeout=TIMEOUT_SECONDS)


def deepgram_call(client: AsyncDeepgramClient) -> SpeechCall:
    """Настоящий запрос: pre-recorded API, `nova-3`, русский, smart_format.

    Файл уходит как есть — не режется и не сжимается; после запроса в памяти
    бота его не остаётся.
    """

    async def call(audio: bytes) -> SpeechAnswer:
        answer = await client.listen.v1.media.transcribe_file(
            request=audio,
            model=MODEL,
            language=LANGUAGE,
            smart_format=True,
            request_options={"timeout": TIMEOUT_SECONDS},
        )
        if not isinstance(answer, ListenV1Response):
            # «Принято» Deepgram отвечает только на запрос с callback, которого
            # бот не шлёт: расшифровки в таком ответе нет.
            raise ApiError(status_code=202, body=type(answer).__name__)
        return answer

    return call


def read_transcript(answer: SpeechAnswer) -> TranscriptionResult:
    """Первый вариант первого канала — или отказ, если ответ не про то (§9.3).

    Один канал и один вариант: бот не просит ни разделения дорожек, ни
    альтернатив. Пустая строка — тишина или не речь.
    """
    channels = answer.results.channels
    if not channels:
        return NotTranscribed("ответ без каналов")
    alternatives = channels[0].alternatives
    if not alternatives:
        return NotTranscribed("ответ без вариантов")
    best = alternatives[0]
    text = (best.transcript or "").strip()
    if not text:
        return NotTranscribed("пустая расшифровка")
    return Transcript(text=text, confidence=best.confidence)


class DeepgramTranscriber:
    """Распознавание через Deepgram: отказы сети и API — в результат, не наружу."""

    def __init__(self, call: SpeechCall) -> None:
        self._call = call

    @classmethod
    def with_client(cls, client: AsyncDeepgramClient) -> DeepgramTranscriber:
        """Обычная сборка: ходит в Deepgram по-настоящему."""
        return cls(deepgram_call(client))

    async def transcribe(self, audio: bytes) -> TranscriptionResult:
        """Распознать речь или честно сказать, что не вышло (§9.3)."""
        try:
            answer = await self._call(audio)
        except httpx.TimeoutException:
            return self._not_transcribed("таймаут")
        except httpx.HTTPError as error:
            return self._not_transcribed(f"сеть: {type(error).__name__}")
        except ApiError as error:
            if error.status_code in (401, 403):
                # Ошибка настройки, а не сообщения: человеку тот же ответ, в
                # журнал — имя переменной, чтобы было что чинить.
                logger.error("Ключ DEEPGRAM_API_KEY не подошёл: ответ %s", error.status_code)
                return self._not_transcribed("ключ не подошёл")
            return self._not_transcribed(f"Deepgram ответил {error.status_code}")
        except ParsingError:
            return self._not_transcribed("ответ не по схеме")

        result = read_transcript(answer)
        if isinstance(result, NotTranscribed):
            return self._not_transcribed(result.reason)
        logger.info("Распознано: знаков %s, уверенность %s", len(result.text), result.confidence)
        return result

    def _not_transcribed(self, reason: str) -> NotTranscribed:
        logger.warning("Речь не распознана: %s", reason)
        return NotTranscribed(reason=reason)
