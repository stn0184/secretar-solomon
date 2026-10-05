"""Связь с Telegram: соединение не ждут минуту, «печатает…» не держит ответ.

С сервера соединение с Telegram временами не устанавливается
(`techspec/16-server.md` §16.5): запрос висит до конца своего срока —
минуту, — а такой же через несколько секунд проходит. Поэтому соединение
ждут `CONNECT_TIMEOUT_SECONDS` и пробуют заново, пока не выйдет срок
запроса. Повтор — только когда соединения не было: запрос до Telegram не
дошёл, и дубля не будет. Ответа на ушедший запрос ждут, как раньше.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from asyncio import sleep
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from time import monotonic

from aiogram import Bot
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.enums import ChatAction
from aiogram.exceptions import TelegramAPIError, TelegramNetworkError
from aiogram.methods.base import TelegramMethod, TelegramType
from aiohttp import ClientConnectorError, ClientTimeout, ConnectionTimeoutError

logger = logging.getLogger(__name__)

# Соединение с Telegram — доли секунды: за 5 с не установилось — уже не
# установится. Это и шаг повтора: отказ бывает мгновенным, а чаще раза
# в 5 с пробовать незачем.
CONNECT_TIMEOUT_SECONDS = 5.0
# «печатает…» Telegram показывает 5 с — статус повторяется с тем же шагом.
TYPING_INTERVAL_SECONDS = 5.0
# Статус, который уже в пути, обычно доходит за доли секунды; столько ответ
# его ждёт, чтобы «печатает…» не мелькнуло после ответа.
TYPING_GRACE_SECONDS = 1.0

# Повтор «печатает…» живёт отдельной задачей; ссылка держит её до конца.
_typing: set[asyncio.Task[None]] = set()


def not_connected(error: TelegramNetworkError) -> bool:
    """Соединение не установилось: запрос до Telegram не дошёл, повтор безопасен.

    aiogram заворачивает ошибку aiohttp в `TelegramNetworkError`, исходная
    остаётся причиной: `ConnectionTimeoutError` — соединение не успело,
    `ClientConnectorError` — отказ. Обрыв и таймаут после соединения — другие
    ошибки: запрос мог дойти.
    """
    return isinstance(error.__cause__, (ConnectionTimeoutError, ClientConnectorError))


class TelegramSession(AiohttpSession):
    """Сессия aiogram, которая ждёт соединения `CONNECT_TIMEOUT_SECONDS`, а не весь срок.

    Срок запроса прежний — таймаут сессии (минута) или переданный: у
    `getUpdates` aiogram передаёт 70 с, у пути к файлу бот — 5 с. Внутри срока
    соединение пробуется заново; новая попытка — только если до срока
    осталось целое окно соединения, так что срок не растёт.
    """

    async def make_request(
        self,
        bot: Bot,
        method: TelegramMethod[TelegramType],
        timeout: int | None = None,  # noqa: ASYNC109 — сигнатура aiogram
    ) -> TelegramType:
        begun = monotonic()
        deadline = begun + (self.timeout if timeout is None else timeout)
        attempt = 1
        while True:
            started = monotonic()
            left = deadline - started
            limits = ClientTimeout(total=left, connect=min(CONNECT_TIMEOUT_SECONDS, left))
            try:
                # aiogram отдаёт срок в aiohttp как есть, а тот принимает и ClientTimeout.
                result = await super().make_request(bot, method, timeout=limits)  # type: ignore[arg-type]
            except TelegramNetworkError as error:
                if not not_connected(error):
                    raise
                failure = type(error.__cause__).__name__
            else:
                if attempt > 1:
                    logger.info(
                        "Telegram %s: соединение с попытки %s, через %.1f с",
                        method.__api_method__,
                        attempt,
                        monotonic() - begun,
                    )
                return result
            retry_at = max(monotonic(), started + CONNECT_TIMEOUT_SECONDS)
            if deadline - retry_at < CONNECT_TIMEOUT_SECONDS:
                # Ошибка новая, а не цепочка от старой: в тексте ошибки aiohttp —
                # адрес запроса, а в адресе — токен бота.
                raise TelegramNetworkError(
                    method=method, message=f"{failure}: no connection in {attempt} attempts"
                )
            if attempt == 1:
                logger.warning(
                    "Telegram %s: соединение не установилось (%s), пробую ещё",
                    method.__api_method__,
                    failure,
                )
            await sleep(max(0.0, retry_at - monotonic()))
            attempt += 1


@asynccontextmanager
async def typing_status(bot: Bot, chat_id: int) -> AsyncIterator[None]:
    """«печатает…» в чате, пока идёт работа; повисший статус ответ не держит.

    Вместо `ChatActionSender` из aiogram: тот на выходе ждёт, пока вернётся
    текущий `send_chat_action`, и повисший статус держал ответ до минуты
    (журнал 2026-10-05). Здесь статус в пути ждут не дольше
    `TYPING_GRACE_SECONDS`, потом повтор отменяется — ответ уходит, а
    «печатает…» Telegram снимает сам, когда приходит сообщение.
    """
    done = asyncio.Event()
    task = asyncio.create_task(_repeat_typing(bot, chat_id, done))
    _typing.add(task)
    task.add_done_callback(_typing.discard)
    try:
        # Первый статус уходит сразу, до работы, а не на первом её ожидании.
        await asyncio.sleep(0)
        yield
    finally:
        done.set()
        await asyncio.wait({task}, timeout=TYPING_GRACE_SECONDS)
        task.cancel()


async def _repeat_typing(bot: Bot, chat_id: int, done: asyncio.Event) -> None:
    """Слать «печатает…» раз в `TYPING_INTERVAL_SECONDS`, пока работа не кончится."""
    while not done.is_set():
        started = monotonic()
        try:
            await bot.send_chat_action(chat_id=chat_id, action=ChatAction.TYPING)
        except TelegramAPIError as error:
            # Статус — не дело: сбой работе не мешает, через шаг уйдёт новый.
            logger.info("«печатает…» не ушло: %s", type(error).__name__)
        with contextlib.suppress(TimeoutError):
            pause = max(0.0, started + TYPING_INTERVAL_SECONDS - monotonic())
            await asyncio.wait_for(done.wait(), pause)
