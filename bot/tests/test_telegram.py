"""Связь с Telegram: соединение не ждут минуту, «печатает…» не держит ответ."""

from __future__ import annotations

import asyncio
import logging
import traceback
from collections.abc import AsyncGenerator
from typing import Any

import pytest
from aiogram import Bot
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.client.telegram import TelegramAPIServer
from aiogram.exceptions import TelegramNetworkError
from aiogram.methods import SendChatAction, SendMessage
from aiogram.methods.base import TelegramMethod, TelegramType
from aiohttp import ClientConnectorError, ClientOSError, ClientTimeout, ConnectionTimeoutError
from aiohttp.client_reqrep import ConnectionKey

from solomon import telegram
from solomon.telegram import TelegramSession, typing_status
from tests.conftest import OWNER_ID, TEST_TOKEN, RecordingSession

TYPING = SendChatAction(chat_id=OWNER_ID, action="typing")
# Адрес запроса, как его строит aiogram: токен бота — прямо в пути.
SECRET_URL = f"https://api.telegram.org/bot{TEST_TOKEN}/sendChatAction"
KEY = ConnectionKey("api.telegram.org", 443, True, True, None, None, None)


class Clock:
    """Часы и сон модуля без настоящего ожидания: сон двигает часы."""

    def __init__(self) -> None:
        self.now = 1000.0
        self.naps: list[float] = []

    def monotonic(self) -> float:
        return self.now

    async def sleep(self, seconds: float) -> None:
        self.naps.append(seconds)
        self.now += seconds
        await asyncio.sleep(0)


@pytest.fixture
def clock(monkeypatch: pytest.MonkeyPatch) -> Clock:
    instance = Clock()
    monkeypatch.setattr(telegram, "monotonic", instance.monotonic)
    monkeypatch.setattr(telegram, "sleep", instance.sleep)
    return instance


def connect_timeout() -> ConnectionTimeoutError:
    """Соединение не успело — текст как у aiohttp: с адресом, а в нём токен."""
    return ConnectionTimeoutError(f"Connection timeout to host {SECRET_URL}")


def refused() -> ClientConnectorError:
    """Соединение отвергнуто сразу."""
    return ClientConnectorError(KEY, ConnectionRefusedError(111, "Connect call failed"))


class Telegram:
    """Запрос aiogram к aiohttp: сбои по очереди, потом ответ.

    У сбоя — сколько секунд он занял: таймаут соединения — окно, отказ — ноль.
    """

    def __init__(self, clock: Clock, failures: list[tuple[Exception, float]]) -> None:
        self.clock = clock
        self.failures = failures
        self.timeouts: list[object] = []

    def install(self, monkeypatch: pytest.MonkeyPatch) -> None:
        async def make_request(
            session: AiohttpSession,
            bot: Bot,
            method: TelegramMethod[Any],
            timeout: object = None,
        ) -> object:
            return await self.request(method, timeout)

        monkeypatch.setattr(AiohttpSession, "make_request", make_request)

    async def request(self, method: TelegramMethod[Any], timeout: object) -> object:
        self.timeouts.append(timeout)
        if not self.failures:
            return True
        failure, spent = self.failures.pop(0)
        self.clock.now += spent
        # Как aiogram: таймаут — «Request timeout error», прочее — текст ошибки aiohttp.
        if isinstance(failure, TimeoutError):
            message = "Request timeout error"
        else:
            message = f"{type(failure).__name__}: {failure}"
        raise TelegramNetworkError(method=method, message=message) from failure


async def test_connect_timeout_is_retried_within_the_same_deadline(
    clock: Clock, bot: Bot, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Соединение не успело дважды — третья попытка проходит, срок общий."""
    fake = Telegram(clock, [(connect_timeout(), 5.0), (connect_timeout(), 5.0)])
    fake.install(monkeypatch)
    session = TelegramSession()

    assert await session.make_request(bot, TYPING) is True
    assert fake.timeouts == [
        ClientTimeout(total=60.0, connect=5.0),
        ClientTimeout(total=55.0, connect=5.0),
        ClientTimeout(total=50.0, connect=5.0),
    ]
    assert clock.naps == [0.0, 0.0]
    # Срок сессии — по-прежнему число: из него aiogram считает срок getUpdates.
    assert session.timeout == 60


async def test_instant_refusal_waits_out_the_connect_window(
    clock: Clock, bot: Bot, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Отказ мгновенный — следующая попытка через окно соединения, а не сразу."""
    fake = Telegram(clock, [(refused(), 0.0)])
    fake.install(monkeypatch)

    assert await TelegramSession().make_request(bot, TYPING) is True
    assert clock.naps == [5.0]
    assert fake.timeouts == [
        ClientTimeout(total=60.0, connect=5.0),
        ClientTimeout(total=55.0, connect=5.0),
    ]


async def test_no_connection_until_the_deadline_fails_without_the_token(
    clock: Clock, bot: Bot, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """Соединения нет весь срок — ошибка через минуту, как раньше, и без токена."""
    fake = Telegram(clock, [(connect_timeout(), 5.0) for _ in range(20)])
    fake.install(monkeypatch)

    with pytest.raises(TelegramNetworkError) as caught:
        await TelegramSession().make_request(bot, TYPING)

    assert len(fake.timeouts) == 12
    assert clock.now == 1060.0
    assert caught.value.message == "ConnectionTimeoutError: no connection in 12 attempts"
    assert caught.value.__cause__ is None
    assert TEST_TOKEN not in "".join(traceback.format_exception(caught.value))
    # В журнал — одна строка на запрос, а не на каждую попытку.
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1
    assert TEST_TOKEN not in caplog.text


async def test_short_deadline_gets_a_single_attempt(
    clock: Clock, bot: Bot, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Срок 5 с (путь к файлу) — одна попытка: повтор не растягивает срок.

    Скачивание повторяет сам `load_file`, и его худший случай не меняется.
    """
    fake = Telegram(clock, [(connect_timeout(), 5.0), (connect_timeout(), 5.0)])
    fake.install(monkeypatch)

    with pytest.raises(TelegramNetworkError):
        await TelegramSession().make_request(bot, TYPING, timeout=5)

    assert fake.timeouts == [ClientTimeout(total=5.0, connect=5.0)]
    assert clock.now == 1005.0


@pytest.mark.parametrize(
    "failure",
    [TimeoutError(), ClientOSError(104, "Connection reset by peer")],
    ids=["timeout", "reset"],
)
async def test_failure_after_connect_is_not_retried(
    clock: Clock, bot: Bot, monkeypatch: pytest.MonkeyPatch, failure: Exception
) -> None:
    """Таймаут ответа или обрыв — запрос мог дойти: повтора нет, ошибка как есть."""
    fake = Telegram(clock, [(failure, 0.0)])
    fake.install(monkeypatch)

    with pytest.raises(TelegramNetworkError) as caught:
        await TelegramSession().make_request(bot, TYPING)

    assert len(fake.timeouts) == 1
    assert caught.value.__cause__ is failure


async def test_typing_goes_out_before_the_work(bot: Bot, session: RecordingSession) -> None:
    """Первый «печатает…» уходит сразу, до работы."""
    async with typing_status(bot, OWNER_ID):
        assert session.actions == ["typing"]


async def test_typing_repeats_until_the_work_is_done(
    bot: Bot, session: RecordingSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Статус повторяется, пока идёт работа, и больше не уходит после неё."""
    monkeypatch.setattr(telegram, "TYPING_INTERVAL_SECONDS", 0.01)

    # Ждём третьего статуса, а не фиксированные 0,1 с: под нагрузкой
    # ворот цикл успевал отправить только два.
    async with typing_status(bot, OWNER_ID):
        for _ in range(500):
            if len(session.actions) >= 3:
                break
            await asyncio.sleep(0.01)
    sent = len(session.actions)
    await asyncio.sleep(0.05)

    assert sent >= 3
    assert len(session.actions) == sent


class HangingSession(RecordingSession):
    """Telegram, у которого «печатает…» повисает навсегда."""

    def __init__(self) -> None:
        super().__init__()
        self.cancelled = False

    async def make_request(
        self, bot: Bot, method: TelegramMethod[TelegramType], timeout: int | None = None
    ) -> TelegramType:
        if isinstance(method, SendChatAction):
            self.sent.append(method)
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                self.cancelled = True
                raise
        return await super().make_request(bot, method, timeout)


async def test_hung_typing_does_not_hold_the_answer(monkeypatch: pytest.MonkeyPatch) -> None:
    """Повисший «печатает…» ждут недолго, потом бросают — ответ уходит сразу."""
    monkeypatch.setattr(telegram, "TYPING_GRACE_SECONDS", 0.05)
    session = HangingSession()
    bot = Bot(token=TEST_TOKEN, session=session)
    loop = asyncio.get_running_loop()
    started = loop.time()

    async with typing_status(bot, OWNER_ID):
        pass
    await bot.send_message(chat_id=OWNER_ID, text="Записал: купить хлеб")
    await asyncio.sleep(0.01)

    assert loop.time() - started < 1
    assert session.texts == ["Записал: купить хлеб"]
    assert session.cancelled


class SlowSession(RecordingSession):
    """Telegram, у которого «печатает…» доходит за доли секунды."""

    def __init__(self) -> None:
        super().__init__()
        self.landed = 0

    async def make_request(
        self, bot: Bot, method: TelegramMethod[TelegramType], timeout: int | None = None
    ) -> TelegramType:
        if isinstance(method, SendChatAction):
            await asyncio.sleep(0.02)
            self.landed += 1
        return await super().make_request(bot, method, timeout)


async def test_typing_on_its_way_lands_before_the_answer() -> None:
    """Статус уже в пути — ответ ждёт, пока он дойдёт: «печатает…» не мелькнёт после."""
    session = SlowSession()
    bot = Bot(token=TEST_TOKEN, session=session)

    async with typing_status(bot, OWNER_ID):
        pass

    assert session.landed == 1


class FailingSession(RecordingSession):
    """Telegram, который «печатает…» не принимает."""

    async def make_request(
        self, bot: Bot, method: TelegramMethod[TelegramType], timeout: int | None = None
    ) -> TelegramType:
        if isinstance(method, SendChatAction):
            raise TelegramNetworkError(method=method, message="Request timeout error")
        return await super().make_request(bot, method, timeout)


async def test_failed_typing_does_not_stop_the_work(caplog: pytest.LogCaptureFixture) -> None:
    """«печатает…» не ушло — работа идёт, в журнале строка без подробностей."""
    caplog.set_level(logging.INFO, logger="solomon.telegram")
    bot = Bot(token=TEST_TOKEN, session=FailingSession())
    done = False

    async with typing_status(bot, OWNER_ID):
        done = True

    assert done
    assert "«печатает…» не ушло: TelegramNetworkError" in caplog.text


@pytest.fixture
async def silent_server() -> AsyncGenerator[tuple[str, list[int]], None]:
    """Сервер на этом компьютере: принимает соединение и молчит."""
    accepted: list[int] = []

    async def keep_silent(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        accepted.append(1)
        await reader.read()
        writer.close()

    server = await asyncio.start_server(keep_silent, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    yield f"127.0.0.1:{port}", accepted
    server.close()
    await server.wait_closed()


async def test_silent_handshake_is_retried_through_aiohttp(
    silent_server: tuple[str, list[int]], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Настоящий aiohttp: TLS не начался — это соединение, и оно пробуется заново.

    Сроки сжаты в 25 раз: окно 0,2 с, срок 1 с.
    """
    monkeypatch.setattr(telegram, "CONNECT_TIMEOUT_SECONDS", 0.2)
    address, accepted = silent_server
    session = TelegramSession(api=TelegramAPIServer.from_base(f"https://{address}"))
    bot = Bot(token=TEST_TOKEN, session=session)
    try:
        with pytest.raises(TelegramNetworkError) as caught:
            await session.make_request(bot, SendMessage(chat_id=OWNER_ID, text="Привет"), 1)
    finally:
        await session.close()

    assert len(accepted) >= 2
    assert caught.value.message.startswith("ConnectionTimeoutError: no connection in ")
    assert TEST_TOKEN not in "".join(traceback.format_exception(caught.value))


async def test_silent_answer_is_not_retried_through_aiohttp(
    silent_server: tuple[str, list[int]], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Настоящий aiohttp: запрос ушёл, ответа нет — повтора нет, дубля не будет."""
    monkeypatch.setattr(telegram, "CONNECT_TIMEOUT_SECONDS", 0.2)
    address, accepted = silent_server
    session = TelegramSession(api=TelegramAPIServer.from_base(f"http://{address}"))
    bot = Bot(token=TEST_TOKEN, session=session)
    try:
        with pytest.raises(TelegramNetworkError) as caught:
            await session.make_request(bot, SendMessage(chat_id=OWNER_ID, text="Привет"), 1)
    finally:
        await session.close()

    assert accepted == [1]
    assert caught.value.message == "Request timeout error"
