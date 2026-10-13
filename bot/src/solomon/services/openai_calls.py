"""Вызовы запасной модели GPT-5.5 (`techspec/34-fallback-gpt.md` §34.3).

Тот же промпт и те же схемы, что у Claude: официальный SDK `openai`, Responses API,
структурированный ответ по Pydantic-модели (строгая схема строится SDK автоматически).
Возвращаются те же по форме ответы (`parsed_output`, `stop_reason`, `model`, `usage`), поэтому
сервис не отличает GPT от Claude: переключает их `services/fallback.py`.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, cast

from openai import AsyncOpenAI
from openai.types.shared_params import Reasoning

from solomon.config import Settings
from solomon.services.understanding import (
    MAX_TOKENS,
    PHOTO_MAX_TOKENS,
    PHOTO_TIMEOUT_SECONDS,
    TIMEOUT_SECONDS,
    ConversationUnderstanding,
    MessageAnswer,
    PhotoBlock,
    PhotoUnderstanding,
)

# Модель по умолчанию у посредника (§34.3); глубина — как у Claude, `medium`.
GPT_MODEL = "gpt-5.5"
REASONING: Reasoning = {"effort": "medium"}


def create_openai_client(settings: Settings) -> AsyncOpenAI | None:
    """Клиент GPT или `None`: без ключа или без адреса посредника запасного пути нет (§34.3)."""
    if not settings.openai_api_key or not settings.openai_base_url:
        return None
    return AsyncOpenAI(api_key=settings.openai_api_key, base_url=settings.openai_base_url)


@dataclass(frozen=True)
class GptUsage:
    input_tokens: int
    output_tokens: int


@dataclass(frozen=True)
class GptReply:
    """Ответ GPT в форме ответа Claude: что разобрано, почему остановился, кто и за сколько."""

    parsed_output: Any
    stop_reason: str | None
    model: str
    usage: GptUsage


def stop_reason_of(response: Any) -> str:
    """Слова Claude для причины остановки: `max_tokens`, `refusal`, иначе `end_turn`."""
    details = getattr(response, "incomplete_details", None)
    if getattr(response, "status", None) == "incomplete" and getattr(details, "reason", "") == (
        "max_output_tokens"
    ):
        return "max_tokens"
    for item in getattr(response, "output", None) or []:
        for part in getattr(item, "content", None) or []:
            if getattr(part, "type", None) == "refusal":
                return "refusal"
    return "end_turn"


def reply_of(response: Any, parsed: Any) -> GptReply:
    usage = getattr(response, "usage", None)
    return GptReply(
        parsed_output=parsed,
        stop_reason=stop_reason_of(response),
        model=str(getattr(response, "model", GPT_MODEL)),
        usage=GptUsage(
            input_tokens=int(getattr(usage, "input_tokens", 0) or 0),
            output_tokens=int(getattr(usage, "output_tokens", 0) or 0),
        ),
    )


def gpt_call(client: AsyncOpenAI, model: str = GPT_MODEL) -> Any:
    """Разбор текста и голоса: схема `MessageAnswer`, ответ — `MessageUnderstanding` (§23.2)."""

    async def call(*, system: str, text: str) -> GptReply:
        response = await client.responses.parse(
            model=model,
            instructions=system,
            input=text,
            text_format=MessageAnswer,
            reasoning=REASONING,
            max_output_tokens=MAX_TOKENS,
            timeout=TIMEOUT_SECONDS,
        )
        parsed = response.output_parsed
        return reply_of(response, None if parsed is None else parsed.as_message())

    return call


def photo_input(content: Sequence[PhotoBlock]) -> list[dict[str, Any]]:
    """Блоки снимка Claude (`image` base64 и `text`) — блоками Responses API (§34.3)."""
    blocks: list[dict[str, Any]] = []
    for block in content:
        if block["type"] == "image":
            source = cast(dict[str, str], block["source"])
            url = f"data:{source['media_type']};base64,{source['data']}"
            blocks.append({"type": "input_image", "image_url": url})
        else:
            blocks.append({"type": "input_text", "text": block["text"]})
    return [{"role": "user", "content": blocks}]


def gpt_photo_call(client: AsyncOpenAI, model: str = GPT_MODEL) -> Any:
    """Разбор снимка (§14.3): схема `PhotoUnderstanding`, картинка идёт раньше текста."""

    async def call(*, system: str, content: Sequence[PhotoBlock]) -> GptReply:
        response = await client.responses.parse(
            model=model,
            instructions=system,
            input=cast(Any, photo_input(content)),
            text_format=PhotoUnderstanding,
            reasoning=REASONING,
            max_output_tokens=PHOTO_MAX_TOKENS,
            timeout=PHOTO_TIMEOUT_SECONDS,
        )
        return reply_of(response, response.output_parsed)

    return call


def gpt_conversation_call(client: AsyncOpenAI, model: str = GPT_MODEL) -> Any:
    """Разбор пересланной переписки (§18.2): схема `ConversationUnderstanding`."""

    async def call(*, system: str, text: str) -> GptReply:
        response = await client.responses.parse(
            model=model,
            instructions=system,
            input=text,
            text_format=ConversationUnderstanding,
            reasoning=REASONING,
            max_output_tokens=MAX_TOKENS,
            timeout=TIMEOUT_SECONDS,
        )
        return reply_of(response, response.output_parsed)

    return call
