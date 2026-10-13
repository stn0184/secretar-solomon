"""Вызовы запасной модели GPT-5.5 (`techspec/34-fallback-gpt.md` §34.3).

Сети здесь нет: клиент OpenAI подменён, проверяется то, что бот ему отдаёт и как читает ответ.
"""

from __future__ import annotations

from dataclasses import replace
from types import SimpleNamespace
from typing import Any

import pytest

from solomon.config import Settings
from solomon.services.openai_calls import (
    GPT_MODEL,
    create_openai_client,
    gpt_call,
    gpt_conversation_call,
    gpt_photo_call,
)
from solomon.services.understanding import (
    ConversationUnderstanding,
    MessageAnswer,
    PhotoUnderstanding,
    build_photo_content,
)


class FakeResponses:
    def __init__(self, response: Any) -> None:
        self.response = response
        self.kwargs: dict[str, Any] = {}

    async def parse(self, **kwargs: Any) -> Any:
        self.kwargs = kwargs
        return self.response


def fake_client(response: Any) -> SimpleNamespace:
    return SimpleNamespace(responses=FakeResponses(response))


def response(
    parsed: Any = None,
    *,
    status: str = "completed",
    incomplete: str | None = None,
    refusal: bool = False,
) -> SimpleNamespace:
    content = (
        [SimpleNamespace(type="refusal")] if refusal else [SimpleNamespace(type="output_text")]
    )
    return SimpleNamespace(
        output_parsed=parsed,
        status=status,
        incomplete_details=SimpleNamespace(reason=incomplete) if incomplete else None,
        output=[SimpleNamespace(type="message", content=content)],
        model="gpt-5.5-2026-10-01",
        usage=SimpleNamespace(input_tokens=120, output_tokens=45),
    )


def test_no_client_without_key_or_address(settings: Settings) -> None:
    assert (
        create_openai_client(replace(settings, openai_api_key=None, openai_base_url=None)) is None
    )
    assert create_openai_client(replace(settings, openai_api_key="k", openai_base_url=None)) is None
    assert (
        create_openai_client(replace(settings, openai_api_key=None, openai_base_url="https://x/v1"))
        is None
    )


def test_client_goes_to_the_given_address(settings: Settings) -> None:
    client = create_openai_client(
        replace(settings, openai_api_key="sk-gpt", openai_base_url="https://api.agenthello.ai/v1")
    )

    assert client is not None
    assert str(client.base_url).startswith("https://api.agenthello.ai/v1")


async def test_text_call_sends_prompt_and_schema_and_reads_the_answer() -> None:
    parsed = SimpleNamespace(as_message=lambda: "понято")
    client = fake_client(response(parsed))

    reply = await gpt_call(client)(system="правила", text="позвонить маме")  # type: ignore[arg-type]

    sent = client.responses.kwargs
    assert sent["model"] == GPT_MODEL
    assert sent["instructions"] == "правила"
    assert sent["input"] == "позвонить маме"
    assert sent["text_format"] is MessageAnswer
    assert sent["reasoning"] == {"effort": "medium"}
    assert reply.parsed_output == "понято"
    assert reply.model == "gpt-5.5-2026-10-01"
    assert (reply.usage.input_tokens, reply.usage.output_tokens) == (120, 45)
    assert reply.stop_reason == "end_turn"


async def test_photo_call_sends_image_before_text() -> None:
    client = fake_client(response("разбор"))
    content = build_photo_content(b"\x89PNG", "image/png", "что на снимке")

    reply = await gpt_photo_call(client)(system="правила", content=content)  # type: ignore[arg-type]

    sent = client.responses.kwargs
    assert sent["text_format"] is PhotoUnderstanding
    blocks = sent["input"][0]["content"]
    assert blocks[0]["type"] == "input_image"
    assert blocks[0]["image_url"].startswith("data:image/png;base64,")
    assert blocks[1] == {"type": "input_text", "text": "что на снимке"}
    assert reply.parsed_output == "разбор"


async def test_conversation_call_uses_its_schema() -> None:
    client = fake_client(response("переписка"))

    reply = await gpt_conversation_call(client)(system="правила", text="А: привет")  # type: ignore[arg-type]

    assert client.responses.kwargs["text_format"] is ConversationUnderstanding
    assert reply.parsed_output == "переписка"


@pytest.mark.parametrize(
    ("kwargs", "expected"),
    [
        ({"status": "incomplete", "incomplete": "max_output_tokens"}, "max_tokens"),
        ({"refusal": True}, "refusal"),
        ({}, "end_turn"),
    ],
)
async def test_stop_reason_is_translated_to_the_claude_words(
    kwargs: dict[str, Any], expected: str
) -> None:
    client = fake_client(response(SimpleNamespace(as_message=lambda: None), **kwargs))

    reply = await gpt_call(client)(system="s", text="t")  # type: ignore[arg-type]

    assert reply.stop_reason == expected
