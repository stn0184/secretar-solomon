"""Запасная модель: переключатель Claude → GPT-5.5 (`techspec/34-fallback-gpt.md` §34.2).

Один вызов модели — функция `call(**kwargs) -> ответ`. Обёртка принимает две такие функции и сама
решает, какая отвечает:

- Claude работает — отвечает он;
- временный отказ Claude (§33.1) — тот же вызов сразу идёт в GPT;
- три временных отказа подряд — следующие 10 минут вызовы идут в GPT без ожидания Claude, потом
  Claude пробуют снова, и если ответил — он снова главный;
- постоянный отказ Claude в GPT не идёт (путь §5.4);
- отказали оба — наружу уходит исключение Claude (оно временное), и сообщение попадает в очередь
  «разобрать позже» (§33); так же, если GPT отказал постоянно, а Claude — временно.

Сети здесь нет: вызовы приходят снаружи, поэтому весь переключатель проверяется подменными
функциями. Владельцу не говорится, какая модель ответила: это видно только по `ai_model`.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class FallbackCalls:
    """Вызовы запасной модели по видам: текст и голос, снимок, переписка (§34.1)."""

    text: Callable[..., Awaitable[Any]]
    photo: Callable[..., Awaitable[Any]]
    conversation: Callable[..., Awaitable[Any]]


# Сколько временных отказов Claude подряд включают прямой путь в GPT.
FAILURES_BEFORE_COOLDOWN = 3
# На сколько Claude откладывается после серии отказов.
COOLDOWN = timedelta(minutes=10)


class FallbackSwitch:
    """Состояние переключателя, общее у всех вызовов бота: сколько отказов Claude подряд и до
    какого момента вызовы идут сразу в GPT. Живёт в памяти: после перезапуска Claude пробуют
    снова."""

    def __init__(self) -> None:
        self.failures = 0
        self.gpt_until: datetime | None = None

    def prefers_gpt(self, now: datetime) -> bool:
        return self.gpt_until is not None and now < self.gpt_until

    def claude_failed(self, now: datetime) -> None:
        self.failures += 1
        if self.failures >= FAILURES_BEFORE_COOLDOWN:
            self.gpt_until = now + COOLDOWN
            self.failures = 0
            logger.warning(
                "Claude отказал %s раза подряд: до %s вызовы идут в GPT",
                FAILURES_BEFORE_COOLDOWN,
                self.gpt_until.isoformat(),
            )

    def claude_answered(self) -> None:
        self.failures = 0
        self.gpt_until = None


def with_fallback[A](
    primary: Callable[..., Awaitable[A]],
    secondary: Callable[..., Awaitable[A]] | None,
    *,
    switch: FallbackSwitch,
    is_temporary: Callable[[BaseException], bool],
    clock: Callable[[], datetime],
) -> Callable[..., Awaitable[A]]:
    """Вызов с запасным путём. Нет запасной функции — вызов идёт как раньше, только в основную."""

    async def call(**kwargs: object) -> A:
        if secondary is None:
            return await primary(**kwargs)
        now = clock()
        if switch.prefers_gpt(now):
            try:
                return await secondary(**kwargs)
            except Exception as error:  # noqa: BLE001 - любой отказ GPT: идём в Claude
                # GPT отказал, пока Claude отложен: пробуем Claude напоследок, он мог вернуться.
                # Его ответ — наш ответ, его отказ (временный) — путь «разобрать позже» (§33).
                logger.warning(
                    "GPT отказал в прямом режиме (%s): пробуем Claude", type(error).__name__
                )
                answer = await primary(**kwargs)
                switch.claude_answered()
                return answer
        try:
            answer = await primary(**kwargs)
        except Exception as error:
            if not is_temporary(error):
                raise
            switch.claude_failed(now)
            try:
                return await secondary(**kwargs)
            except Exception as second:  # noqa: BLE001 - любой отказ GPT: наружу ошибка Claude
                # Оба отказали (или GPT — постоянно): наружу — временный отказ Claude,
                # чтобы сообщение ушло в очередь «разобрать позже», а не записалось как есть.
                logger.warning("GPT тоже отказал: %s", type(second).__name__)
                raise error from None
        switch.claude_answered()
        return answer

    return call
