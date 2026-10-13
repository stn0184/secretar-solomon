"""Запасная модель — переключатель Claude → GPT (`techspec/34-fallback-gpt.md` §34.2).

Сети здесь нет: «модели» — подменные функции, а «временный отказ» — своё исключение. Проверяется
только то, что решает переключатель: куда идёт вызов и что вернуть, когда отказали оба.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from solomon.services.fallback import (
    COOLDOWN,
    FAILURES_BEFORE_COOLDOWN,
    FallbackSwitch,
    with_fallback,
)

NOW = datetime(2026, 10, 13, 12, 0, tzinfo=UTC)


class Temporary(Exception):
    """Временный отказ (посредник, сеть, 5xx)."""


class Permanent(Exception):
    """Постоянный отказ (400, не по схеме, ключ)."""


def is_temporary(error: BaseException) -> bool:
    return isinstance(error, Temporary)


class Model:
    """Подменная модель: считает вызовы, отвечает или отказывает."""

    def __init__(self, name: str, *, fail: BaseException | None = None) -> None:
        self.name = name
        self.fail = fail
        self.calls = 0

    async def __call__(self, *, system: str, text: str) -> str:
        self.calls += 1
        if self.fail is not None:
            raise self.fail
        return f"{self.name}:{text}"


def wrap(primary: Model, secondary: Model | None, switch: FallbackSwitch, clock: list[datetime]):
    return with_fallback(
        primary, secondary, switch=switch, is_temporary=is_temporary, clock=lambda: clock[0]
    )


async def test_claude_works_goes_to_claude_only() -> None:
    claude, gpt = Model("claude"), Model("gpt")
    call = wrap(claude, gpt, FallbackSwitch(), [NOW])
    assert await call(system="s", text="a") == "claude:a"
    assert (claude.calls, gpt.calls) == (1, 0)


async def test_temporary_failure_goes_to_gpt_with_same_arguments() -> None:
    claude, gpt = Model("claude", fail=Temporary()), Model("gpt")
    call = wrap(claude, gpt, FallbackSwitch(), [NOW])
    assert await call(system="s", text="a") == "gpt:a"
    assert (claude.calls, gpt.calls) == (1, 1)


async def test_permanent_failure_does_not_go_to_gpt() -> None:
    claude, gpt = Model("claude", fail=Permanent()), Model("gpt")
    call = wrap(claude, gpt, FallbackSwitch(), [NOW])
    with pytest.raises(Permanent):
        await call(system="s", text="a")
    assert gpt.calls == 0


async def test_three_temporary_failures_in_a_row_send_next_ten_minutes_straight_to_gpt() -> None:
    claude, gpt = Model("claude", fail=Temporary()), Model("gpt")
    clock = [NOW]
    call = wrap(claude, gpt, FallbackSwitch(), clock)
    for _ in range(FAILURES_BEFORE_COOLDOWN):
        await call(system="s", text="a")
    claude_calls = claude.calls
    clock[0] = NOW + COOLDOWN - timedelta(seconds=1)
    assert await call(system="s", text="b") == "gpt:b"
    assert claude.calls == claude_calls  # Claude не ждали


async def test_after_ten_minutes_claude_is_tried_again_and_wins_back() -> None:
    claude, gpt = Model("claude", fail=Temporary()), Model("gpt")
    clock = [NOW]
    call = wrap(claude, gpt, FallbackSwitch(), clock)
    for _ in range(FAILURES_BEFORE_COOLDOWN):
        await call(system="s", text="a")
    claude.fail = None
    clock[0] = NOW + COOLDOWN + timedelta(seconds=1)
    assert await call(system="s", text="c") == "claude:c"
    assert await call(system="s", text="d") == "claude:d"


async def test_success_of_claude_resets_the_failure_count() -> None:
    claude, gpt = Model("claude", fail=Temporary()), Model("gpt")
    clock = [NOW]
    call = wrap(claude, gpt, FallbackSwitch(), clock)
    await call(system="s", text="a")
    await call(system="s", text="a")
    claude.fail = None
    await call(system="s", text="ok")  # две неудачи, потом успех: счёт сброшен
    claude.fail = Temporary()
    await call(system="s", text="a")
    await call(system="s", text="a")
    before = claude.calls
    await call(system="s", text="still-claude")  # третья после сброса — ещё не десять минут GPT
    assert claude.calls == before + 1


async def test_both_failed_raises_the_claude_error_so_the_message_goes_to_the_later_queue() -> None:
    claude_error = Temporary("claude")
    claude, gpt = Model("claude", fail=claude_error), Model("gpt", fail=Temporary("gpt"))
    call = wrap(claude, gpt, FallbackSwitch(), [NOW])
    with pytest.raises(Temporary) as raised:
        await call(system="s", text="a")
    assert raised.value is claude_error


async def test_mixed_case_temporary_claude_permanent_gpt_raises_claude_error() -> None:
    claude_error = Temporary("claude")
    claude, gpt = Model("claude", fail=claude_error), Model("gpt", fail=Permanent("gpt"))
    call = wrap(claude, gpt, FallbackSwitch(), [NOW])
    with pytest.raises(Temporary) as raised:
        await call(system="s", text="a")
    assert raised.value is claude_error


async def test_without_gpt_everything_works_as_before() -> None:
    claude = Model("claude", fail=Temporary())
    call = wrap(claude, None, FallbackSwitch(), [NOW])
    with pytest.raises(Temporary):
        await call(system="s", text="a")
    claude.fail = None
    assert await call(system="s", text="b") == "claude:b"


async def test_cooldown_does_not_start_while_gpt_is_missing() -> None:
    claude = Model("claude", fail=Temporary())
    switch = FallbackSwitch()
    call = wrap(claude, None, switch, [NOW])
    for _ in range(FAILURES_BEFORE_COOLDOWN + 2):
        with pytest.raises(Temporary):
            await call(system="s", text="a")
    assert claude.calls == FAILURES_BEFORE_COOLDOWN + 2  # Claude пробуют всегда


async def test_gpt_failure_during_cooldown_falls_back_to_claude() -> None:
    claude, gpt = Model("claude", fail=Temporary()), Model("gpt")
    clock = [NOW]
    call = wrap(claude, gpt, FallbackSwitch(), clock)
    for _ in range(FAILURES_BEFORE_COOLDOWN):
        await call(system="s", text="a")
    claude.fail = None
    gpt.fail = Temporary("gpt")
    clock[0] = NOW + timedelta(minutes=1)
    assert await call(system="s", text="x") == "claude:x"


async def test_both_failed_during_cooldown_raises_the_temporary_claude_error() -> None:
    claude, gpt = Model("claude", fail=Temporary()), Model("gpt")
    clock = [NOW]
    call = wrap(claude, gpt, FallbackSwitch(), clock)
    for _ in range(FAILURES_BEFORE_COOLDOWN):
        await call(system="s", text="a")
    gpt.fail = Permanent("gpt")
    clock[0] = NOW + timedelta(minutes=1)
    with pytest.raises(Temporary):
        await call(system="s", text="x")
