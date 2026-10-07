"""Бот в MAX: пересланное, заметки и группы.

Источник правды — `techspec/27-max.md`. Здесь только источник: что пришло
боту в MAX, уходит в общий путь личных чатов (`services/chats.py`, §25) —
хранение, согласие, разбор и сообщения владельцу в Telegram там.

- Связь (§27.2): HTTP API MAX на `platform-api2.max.ru`, токен — заголовком
  `Authorization`, обновления — long polling (`GET /updates`) своей задачей
  asyncio рядом с опросом Telegram. Сертификат сервера MAX выдан
  удостоверяющим центром Минцифры: его корневой сертификат лежит в
  репозитории (`bot/certs/`) и добавляется к проверке TLS только у этого
  клиента — проверка не отключается.
- Приём (§27.3): в личном чате с ботом — только владелец (`OWNER_MAX_ID`):
  пересланное — строки переписки исходного чата, своё — заметки; в группах,
  где есть владелец, — все сообщения. Чужое в личке бота — молча мимо.
  «Ждёт ответа» по MAX не ведётся вовсе.
- Опрос: отметка (`marker`) двигается только после того, как пачка
  записана; MAX не ответил — та же отметка снова, пропущенное заберётся.
- Ответ в MAX — только «Принял, итог пришлю в Telegram.» владельцу в личный
  чат с ботом, раз на пачку и только о записанном (инвариант 4). В группы бот
  не пишет: у API здесь и метода такого нет.

Сеть — только `HttpMaxApi` (httpx) за протоколом `MaxApi`: тесты подставляют
свой. Токен не попадает ни в журнал, ни в адрес запроса: в журнал — только
коды ответов MAX и числа, без текстов сообщений и имён.
"""

from __future__ import annotations

import asyncio
import logging
import ssl
from collections import OrderedDict
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Protocol

import httpx

from solomon import texts
from solomon.config import Settings
from solomon.db.chats import ChatKind, Platform, Stored
from solomon.services.chats import GROUP_PREFIX, NOTES_KEY, QUIET, AudioLoader, Incoming

logger = logging.getLogger(__name__)

PLATFORM: Platform = "max"
# HTTP API MAX (§27.2): новый домен; старый `platform-api.max.ru` закрывается.
API_URL = "https://platform-api2.max.ru"
# Корневой сертификат Минцифры (Russian Trusted Root CA) — из репозитория:
# bot/src/solomon/services/max_bot.py -> bot/certs/.
CERT_PATH = Path(__file__).resolve().parents[3] / "certs" / "russian_trusted_root_ca.pem"

# Long polling: MAX держит запрос до 90 с (предел API) и отвечает сразу, как
# появилось событие. Чтение ждёт дольше удержания — иначе каждый пустой опрос
# был бы таймаутом.
POLL_SECONDS = 90
POLL_LIMIT = 100
UPDATE_TYPES = (
    "message_created",
    "message_edited",
    "message_removed",
    "bot_started",
    "bot_stopped",
)
TIMEOUT = httpx.Timeout(POLL_SECONDS + 15.0, connect=5.0)
# Между опросами — не чаще раза в 300 мс (документация MAX).
PACE_SECONDS = 0.3
# MAX не ответил — пауза 5 с, дальше вдвое, но не больше минуты.
RETRY_FIRST = 5.0
RETRY_MAX = 60.0
# Пачка не записалась (база не ответила) — та же пачка ещё раз; третья неудача
# подряд — пачка пропускается, чтобы один битый кусок не остановил приём.
BATCH_ATTEMPTS = 3
# Группа: кто в ней и как называется — спрашивается раз в час.
GROUP_CHECK_EVERY = timedelta(hours=1)
# Голосовое — до 25 МБ.
AUDIO_LIMIT = 25 * 1024 * 1024
# «Принял» — раз на пачку: первое записанное сообщение владельца боту после
# 20 минут тишины в личном чате с ботом (§27.3) — та же тишина, что у разбора.
ACCEPT_AFTER = QUIET
# Сколько последних сообщений процесс помнит по чатам — для удаления.
REMEMBERED = 1000
# Id сообщения в базе — до 200 знаков (§3.13).
ID_LIMIT = 200

# Ключи чатов (§27.3): переписка, откуда переслано, — по её чату, без него —
# по отправителю; группа — `GROUP_PREFIX`, заметки — `NOTES_KEY`.
FORWARD_PREFIX = "from:"
SENDER_PREFIX = "user:"
OWNER_SENDER = "Вы"

# Вложения MAX по виду (§27.3): голосовое — на Deepgram, снимок — пометкой,
# клавиатура — не содержимое.
VOICE_TYPES = frozenset({"audio"})
PHOTO_TYPES = frozenset({"image"})
NOT_CONTENT_TYPES = frozenset({"inline_keyboard"})


class MaxError(Exception):
    """MAX не ответил или ответил отказом. `reason` — для журнала: статус и
    код ошибки MAX, без её текста и без адреса запроса."""

    def __init__(self, reason: str, status: int | None = None) -> None:
        super().__init__(reason)
        self.reason = reason
        self.status = status


def max_error(status: int, payload: Any) -> MaxError:
    """Ошибка из ответа MAX: тело — `{code, message}`; в причину — только код."""
    code = payload.get("code") if isinstance(payload, Mapping) else None
    reason = f"MAX ответил {status}"
    if isinstance(code, str) and code:
        reason += f", код {code[:60]}"
    return MaxError(reason, status)


def _int(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _text(value: Any) -> str:
    return value if isinstance(value, str) else ""


def parse_time(value: Any) -> datetime | None:
    """Время MAX — миллисекунды Unix в UTC. Не число — `None`."""
    milliseconds = _int(value)
    if milliseconds is None:
        return None
    return datetime.fromtimestamp(milliseconds / 1000, UTC)


# --- Ответы MAX (§27.3) -------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class MaxUser:
    """Человек в MAX: id и имя, как его видно в мессенджере."""

    user_id: int
    name: str


@dataclass(frozen=True, slots=True)
class MaxAttachment:
    """Вложение: голосовое (со ссылкой на файл), снимок или прочее."""

    kind: ChatKind
    url: str | None


@dataclass(frozen=True, slots=True)
class Forwarded:
    """Пересланное: исходный отправитель, исходный чат, текст и вложение.
    Времени исходного сообщения API не даёт."""

    sender: MaxUser | None
    chat_id: int | None
    text: str
    attachment: MaxAttachment | None


@dataclass(frozen=True, slots=True)
class MaxMessage:
    """Сообщение MAX: где (`dialog` — личный чат с ботом, `chat` — группа,
    `channel` — канал), кто, когда, текст, вложение и пересланное."""

    mid: str
    chat_id: int | None
    chat_type: str
    sender: MaxUser | None
    sent_at: datetime
    text: str
    attachment: MaxAttachment | None
    forwarded: Forwarded | None


@dataclass(frozen=True, slots=True)
class MaxUpdate:
    """Событие опроса: вид и то, что к нему приложено."""

    kind: str
    message: MaxMessage | None = None
    chat_id: int | None = None
    message_id: str | None = None
    user_id: int | None = None


@dataclass(frozen=True, slots=True)
class UpdateBatch:
    """Ответ `GET /updates`: события и отметка следующей пачки."""

    updates: list[MaxUpdate]
    marker: int | None


def parse_user(raw: Any) -> MaxUser | None:
    """Пользователь MAX: без id — `None`; имя — имя и фамилия."""
    if not isinstance(raw, Mapping):
        return None
    user_id = _int(raw.get("user_id"))
    if user_id is None:
        return None
    parts = (_text(raw.get("first_name")).strip(), _text(raw.get("last_name")).strip())
    name = " ".join(part for part in parts if part) or _text(raw.get("name")).strip()
    return MaxUser(user_id=user_id, name=name)


def attachment_of(raws: Any) -> MaxAttachment | None:
    """Первое вложение-содержимое: `audio` — голосовое со ссылкой на файл,
    `image` — снимок, остальное — прочее; клавиатура — не содержимое."""
    for raw in raws if isinstance(raws, list) else []:
        if not isinstance(raw, Mapping):
            continue
        kind = _text(raw.get("type"))
        if kind in NOT_CONTENT_TYPES:
            continue
        if kind in VOICE_TYPES:
            payload = raw.get("payload")
            url = _text(payload.get("url")) if isinstance(payload, Mapping) else ""
            return MaxAttachment(kind="voice", url=url or None)
        if kind in PHOTO_TYPES:
            return MaxAttachment(kind="photo", url=None)
        return MaxAttachment(kind="other", url=None)
    return None


def _forwarded(raw: Any) -> Forwarded | None:
    """`link` с `type: forward`; ответ на сообщение (`reply`) — не пересланное."""
    if not isinstance(raw, Mapping) or raw.get("type") != "forward":
        return None
    body = raw.get("message")
    body = body if isinstance(body, Mapping) else {}
    return Forwarded(
        sender=parse_user(raw.get("sender")),
        chat_id=_int(raw.get("chat_id")),
        text=_text(body.get("text")),
        attachment=attachment_of(body.get("attachments")),
    )


def parse_message(raw: Any) -> MaxMessage | None:
    """Сообщение из события; без id, времени или с id длиннее предела — `None`."""
    if not isinstance(raw, Mapping):
        return None
    body = raw.get("body")
    body = body if isinstance(body, Mapping) else {}
    mid = body.get("mid")
    sent_at = parse_time(raw.get("timestamp"))
    if not isinstance(mid, str) or not mid or len(mid) > ID_LIMIT or sent_at is None:
        return None
    recipient = raw.get("recipient")
    recipient = recipient if isinstance(recipient, Mapping) else {}
    return MaxMessage(
        mid=mid,
        chat_id=_int(recipient.get("chat_id")),
        chat_type=_text(recipient.get("chat_type")),
        sender=parse_user(raw.get("sender")),
        sent_at=sent_at,
        text=_text(body.get("text")),
        attachment=attachment_of(body.get("attachments")),
        forwarded=_forwarded(raw.get("link")),
    )


def _update(raw: Any) -> MaxUpdate | None:
    if not isinstance(raw, Mapping):
        return None
    kind = raw.get("update_type")
    if not isinstance(kind, str) or not kind:
        return None
    user = parse_user(raw.get("user"))
    message_id = raw.get("message_id")
    return MaxUpdate(
        kind=kind,
        message=parse_message(raw.get("message")),
        chat_id=_int(raw.get("chat_id")),
        message_id=message_id if isinstance(message_id, str) and message_id else None,
        user_id=user.user_id if user is not None else _int(raw.get("user_id")),
    )


def parse_updates(payload: Any) -> UpdateBatch:
    """`{updates, marker}`; без списка событий — ошибка, а не пустая пачка:
    иначе отметка сдвинулась бы мимо непрочитанного."""
    updates = payload.get("updates") if isinstance(payload, Mapping) else None
    if not isinstance(updates, list):
        raise MaxError("MAX вернул опрос без событий")
    found = (_update(raw) for raw in updates)
    marker = _int(payload.get("marker")) if isinstance(payload, Mapping) else None
    return UpdateBatch(updates=[item for item in found if item is not None], marker=marker)


# --- Приём: сообщение MAX в общий путь (§27.3) --------------------------------------


def _content(text: str, attachment: MaxAttachment | None) -> tuple[ChatKind, str, str | None]:
    """Вид, текст и файл для Deepgram: у голосового текст — расшифровка, она
    придёт позже; снимок и прочее — с подписью."""
    if attachment is None:
        return "text", text, None
    if attachment.kind == "voice":
        return "voice", "", attachment.url
    return attachment.kind, text, None


def _has_content(text: str, attachment: MaxAttachment | None) -> bool:
    return bool(text.strip()) or attachment is not None


def _incoming(
    message: MaxMessage,
    *,
    chat_key: str,
    chat_name: str,
    mine: bool,
    sender: str,
    text: str,
    attachment: MaxAttachment | None,
) -> Incoming:
    kind, body, file_id = _content(text, attachment)
    return Incoming(
        platform=PLATFORM,
        connection_id=None,
        chat_key=chat_key,
        chat_name=chat_name,
        external_id=message.mid,
        direction="out" if mine else "in",
        sender=OWNER_SENDER if mine else sender,
        sent_at=message.sent_at,
        kind=kind,
        text=body,
        file_id=file_id,
        # «Ждёт ответа» по MAX не ведётся вовсе: бот не видит, ответил ли
        # владелец в самом MAX (§27.3).
        tracks_waiting=False,
    )


def _note(message: MaxMessage, text: str, attachment: MaxAttachment | None) -> Incoming:
    return _incoming(
        message,
        chat_key=NOTES_KEY,
        chat_name=texts.MAX_NOTES,
        mine=True,
        sender=OWNER_SENDER,
        text=text,
        attachment=attachment,
    )


def _forward_line(message: MaxMessage, forwarded: Forwarded, owner_max_id: int) -> Incoming:
    """Строка пересланной переписки (§27.3): чат — исходный чат, а если MAX
    его не назвал — по отправителю; своё пересланное без чата — заметка.
    Время — время пересылки: исходного API не даёт."""
    sender = forwarded.sender
    mine = sender is not None and sender.user_id == owner_max_id
    name = sender.name if sender is not None and not mine else ""
    if forwarded.chat_id is not None:
        key = f"{FORWARD_PREFIX}{forwarded.chat_id}"
    elif sender is not None and not mine:
        key = f"{SENDER_PREFIX}{sender.user_id}"
    else:
        return _note(message, forwarded.text, forwarded.attachment)
    return _incoming(
        message,
        chat_key=key,
        chat_name=name,
        mine=mine,
        sender=name,
        text=forwarded.text,
        attachment=forwarded.attachment,
    )


def incoming_of(message: MaxMessage, owner_max_id: int, group_title: str = "") -> list[Incoming]:
    """Что из сообщения MAX уходит в общий путь (§27.3).

    Личный чат с ботом — только сообщения владельца: пересланное — строка
    переписки, своё (и подпись к пересланному) — заметка «Вы» в «MAX:
    заметки». Чужое — ничего. Группа — сообщение владельца `out`, остальных
    `in`; пересланное в группу — его текстом. Канал — ничего.
    """
    sender = message.sender
    if sender is None:
        return []
    mine = sender.user_id == owner_max_id
    if message.chat_type == "dialog":
        if not mine:
            return []
        found: list[Incoming] = []
        forwarded = message.forwarded
        if forwarded is not None and _has_content(forwarded.text, forwarded.attachment):
            found.append(_forward_line(message, forwarded, owner_max_id))
        if _has_content(message.text, message.attachment):
            found.append(_note(message, message.text, message.attachment))
        return found
    if message.chat_type == "chat" and message.chat_id is not None:
        text, attachment = message.text, message.attachment
        if not _has_content(text, attachment) and message.forwarded is not None:
            text, attachment = message.forwarded.text, message.forwarded.attachment
        if not _has_content(text, attachment):
            return []
        return [
            _incoming(
                message,
                chat_key=f"{GROUP_PREFIX}{message.chat_id}",
                chat_name=group_title,
                mine=mine,
                sender=sender.name,
                text=text,
                attachment=attachment,
            )
        ]
    return []


# --- API MAX (§27.2) ----------------------------------------------------------------


def max_ssl_context(cert: Path = CERT_PATH) -> ssl.SSLContext:
    """Проверка TLS клиента MAX: обычные корневые сертификаты httpx и к ним —
    корневой Минцифры из репозитория. Проверка имени и цепочки — как у всех
    (`CERT_REQUIRED`); другие клиенты бота этого сертификата не знают."""
    context = httpx.create_ssl_context()
    context.load_verify_locations(cafile=str(cert))
    return context


def max_client() -> httpx.AsyncClient:
    """httpx-клиент только для MAX — со своей проверкой TLS."""
    return httpx.AsyncClient(timeout=TIMEOUT, verify=max_ssl_context())


class MaxApi(Protocol):
    """API MAX — то, что подменяет тест. Отказы — `MaxError`. Писать бот
    может только человеку по его id — в группу отсюда не уходит ничего."""

    async def updates(self, marker: int | None) -> UpdateBatch: ...

    async def chat_title(self, chat_id: int) -> str: ...

    async def is_member(self, chat_id: int, user_id: int) -> bool: ...

    async def send_to_user(self, user_id: int, text: str) -> None: ...

    async def download(self, url: str) -> bytes: ...

    async def close(self) -> None: ...


class HttpMaxApi:
    """HTTP API MAX поверх httpx. Токен — заголовком к `platform-api2.max.ru`,
    и только туда: файл голосового качается без него."""

    def __init__(self, client: httpx.AsyncClient, token: str, base_url: str = API_URL) -> None:
        self._client = client
        self._token = token
        self._base = base_url.rstrip("/")

    @classmethod
    def create(cls, token: str) -> HttpMaxApi:
        return cls(max_client(), token)

    async def _call(
        self,
        method: str,
        path: str,
        params: Mapping[str, str | int] | None = None,
        body: Mapping[str, Any] | None = None,
    ) -> Any:
        """Запрос и разбор ответа. Исключение httpx не выходит наружу как есть:
        `from None` — без адреса и в трассировке."""
        try:
            response = await self._client.request(
                method,
                f"{self._base}{path}",
                params=dict(params or {}),
                json=dict(body) if body is not None else None,
                headers={"Authorization": self._token},
            )
        except httpx.TimeoutException:
            raise MaxError("таймаут") from None
        except httpx.HTTPError as error:
            raise MaxError(f"сеть: {type(error).__name__}") from None
        try:
            payload = response.json()
        except ValueError:
            payload = None
        if response.is_error:
            raise max_error(response.status_code, payload)
        if isinstance(payload, Mapping) and payload.get("success") is False:
            raise max_error(response.status_code, payload)
        return payload

    async def updates(self, marker: int | None, wait: int = POLL_SECONDS) -> UpdateBatch:
        """`GET /updates`: события после отметки. Отметка, переданная в
        запросе, подтверждает всё до неё — поэтому передаётся только
        записанная; без отметки ничего не подтверждается. `wait` — сколько
        секунд MAX держит пустой запрос."""
        params: dict[str, str | int] = {
            "limit": POLL_LIMIT,
            "timeout": wait,
            "types": ",".join(UPDATE_TYPES),
        }
        if marker is not None:
            params["marker"] = marker
        return parse_updates(await self._call("GET", "/updates", params))

    async def me(self) -> MaxUser:
        """`GET /me` — сам бот: проверка токена и связи (живой прогон)."""
        bot = parse_user(await self._call("GET", "/me"))
        if bot is None:
            raise MaxError("MAX не назвал бота")
        return bot

    async def chat_title(self, chat_id: int) -> str:
        """`GET /chats/{chatId}` — название группы."""
        payload = await self._call("GET", f"/chats/{chat_id}")
        return _text(payload.get("title")).strip() if isinstance(payload, Mapping) else ""

    async def is_member(self, chat_id: int, user_id: int) -> bool:
        """`GET /chats/{chatId}/members?user_ids=…` — есть ли человек в группе."""
        payload = await self._call("GET", f"/chats/{chat_id}/members", {"user_ids": user_id})
        members = payload.get("members") if isinstance(payload, Mapping) else None
        found = (parse_user(member) for member in (members if isinstance(members, list) else []))
        return any(member is not None and member.user_id == user_id for member in found)

    async def send_to_user(self, user_id: int, text: str) -> None:
        """`POST /messages?user_id=…` — сообщение человеку в личный чат с ботом."""
        await self._call("POST", "/messages", {"user_id": user_id}, {"text": text})

    async def download(self, url: str) -> bytes:
        """Голосовое по ссылке из вложения — без токена: ссылка ведёт не в API."""
        try:
            async with self._client.stream("GET", url, follow_redirects=True) as response:
                if response.is_error:
                    raise MaxError(f"файл не скачан: {response.status_code}")
                chunks: list[bytes] = []
                size = 0
                async for chunk in response.aiter_bytes():
                    size += len(chunk)
                    if size > AUDIO_LIMIT:
                        raise MaxError("файл больше предела")
                    chunks.append(chunk)
        except httpx.HTTPError as error:
            raise MaxError(f"файл не скачан: {type(error).__name__}") from None
        return b"".join(chunks)

    async def close(self) -> None:
        await self._client.aclose()


# --- Опрос и приём (§27.2–27.3) -----------------------------------------------------


class MaxChats(Protocol):
    """Общий путь личных чатов (`ChatService`): включение площадки, приём,
    правка и удаление."""

    async def enable(
        self, platform: Platform, connection_id: str | None = None, *, enabled: bool = True
    ) -> bool: ...

    async def receive(
        self, incoming: Incoming, load_audio: AudioLoader | None = None
    ) -> Stored | None: ...

    async def edited(self, incoming: Incoming) -> bool: ...

    async def deleted(
        self,
        *,
        platform: Platform,
        connection_id: str | None,
        chat_key: str,
        external_ids: Sequence[str],
    ) -> int: ...


@dataclass(frozen=True, slots=True)
class Group:
    """Группа, как её помнит процесс: название, есть ли в ней владелец и когда
    это проверено."""

    title: str
    with_owner: bool
    checked_at: datetime


@dataclass(frozen=True, slots=True)
class Polled:
    """Итог одного опроса: сколько записано и записана ли пачка целиком."""

    stored: int
    complete: bool


Sleep = Callable[[float], Awaitable[None]]
Clock = Callable[[], datetime]


class MaxService:
    """Бот владельца в MAX: long polling, приём в общий путь и «Принял».
    Собирается при запуске, только если заданы `MAX_BOT_TOKEN` и
    `OWNER_MAX_ID`."""

    def __init__(
        self,
        settings: Settings,
        owner_max_id: int,
        chats: MaxChats,
        api: MaxApi,
        clock: Clock | None = None,
        sleep: Sleep = asyncio.sleep,
    ) -> None:
        self._settings = settings
        self._owner = owner_max_id
        self._chats = chats
        self._api = api
        self._clock = clock or self._now
        self._sleep = sleep
        # Отметка последней записанной пачки; `None` — с первого
        # неподтверждённого события.
        self._marker: int | None = None
        self._attempts = 0
        # Площадка включена в этом процессе (§25.5): первое сообщение боту.
        self._enabled = False
        self._groups: dict[int, Group] = {}
        # Сообщение → его чаты: удаление в MAX называет только id.
        self._keys: OrderedDict[str, tuple[str, ...]] = OrderedDict()
        self._task: asyncio.Task[None] | None = None
        # Когда записано последнее сообщение владельца боту и ждёт ли пачка
        # своего «Принял».
        self._wrote_at: datetime | None = None
        self._accept_due = False

    def _now(self) -> datetime:
        return datetime.now(self._settings.owner_timezone)

    @classmethod
    def with_client(cls, settings: Settings, chats: MaxChats) -> MaxService | None:
        """Обычная сборка: настоящий API. Нет токена или id владельца — `None`:
        источник выключен, бот работает как без него (§27.1)."""
        if settings.max_bot_token is None or settings.owner_max_id is None:
            return None
        return cls(
            settings, settings.owner_max_id, chats, HttpMaxApi.create(settings.max_bot_token)
        )

    # --- Задача рядом с опросом Telegram -----------------------------------------------

    def start(self) -> asyncio.Task[None]:
        """Запустить опрос отдельной задачей asyncio (§27.2)."""
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self.run())
        return self._task

    async def stop(self) -> None:
        """Остановка вместе с ботом: опрос обрывается — отметка стоит на
        последней записанной пачке, следующий запуск дочитает; клиент
        закрывается."""
        if self._task is not None and not self._task.done():
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)
        await self._api.close()

    async def run(self) -> None:
        """Опрос без конца: MAX не ответил — пауза и та же отметка снова;
        пачка не записалась — тоже. Останавливает его только отмена."""
        logger.info("MAX: опрос запущен")
        delay = RETRY_FIRST
        while True:
            try:
                polled = await self.poll()
            except MaxError as error:
                if error.status == 401:
                    logger.error(
                        "Ключ MAX_BOT_TOKEN не подошёл (%s): опрос продолжается", error.reason
                    )
                else:
                    logger.warning(
                        "Опрос MAX не удался: %s — повторю через %.0f с", error.reason, delay
                    )
                await self._sleep(delay)
                delay = min(delay * 2, RETRY_MAX)
                continue
            except Exception:  # фоновая задача: исключение иначе остановило бы приём молча
                logger.exception("Опрос MAX упал — повторю через %.0f с", delay)
                await self._sleep(delay)
                delay = min(delay * 2, RETRY_MAX)
                continue
            if polled.complete:
                delay = RETRY_FIRST
                await self._sleep(PACE_SECONDS)
            else:
                await self._sleep(delay)
                delay = min(delay * 2, RETRY_MAX)

    async def poll(self) -> Polled:
        """Один опрос: события после отметки — в общий путь. Отметка двигается,
        когда пачка записана; не записалась — та же пачка придёт снова, а после
        третьей неудачи подряд пропускается. Отказ MAX — `MaxError` наружу."""
        batch = await self._api.updates(self._marker)
        stored = 0
        complete = True
        for item in batch.updates:
            count, done = await self._handle(item)
            stored += count
            complete = complete and done
        if complete or self._attempts + 1 >= BATCH_ATTEMPTS:
            if not complete:
                logger.error(
                    "MAX: пачка событий не записана за %s попытки — пропускаю", BATCH_ATTEMPTS
                )
            self._attempts = 0
            if batch.marker is not None:
                self._marker = batch.marker
        else:
            self._attempts += 1
            logger.warning("MAX: пачка событий записана не целиком — прочитаю её снова")
        if stored:
            logger.info("MAX: записано новых сообщений %s", stored)
        await self._accept()
        return Polled(stored=stored, complete=complete)

    async def _accept(self) -> None:
        """«Принял, итог пришлю в Telegram.» — владельцу в личный чат с ботом,
        раз на пачку. Не ушло — следующий опрос пришлёт; бот остановлен (403)
        — не пришлёт."""
        if not self._accept_due:
            return
        try:
            await self._api.send_to_user(self._owner, texts.MAX_ACCEPTED)
        except MaxError as error:
            logger.warning("MAX: «Принял» не ушло: %s", error.reason)
            if error.status == 403:
                self._accept_due = False
            return
        self._accept_due = False
        logger.info("MAX: «Принял» ушло")

    def _owner_wrote(self) -> None:
        """Записано сообщение владельца боту: после 20 минут тишины — новая
        пачка, и ей положено «Принял»."""
        now = self._clock()
        if self._wrote_at is None or now - self._wrote_at >= ACCEPT_AFTER:
            self._accept_due = True
        self._wrote_at = now

    # --- События -----------------------------------------------------------------------

    async def _handle(self, item: MaxUpdate) -> tuple[int, bool]:
        """Одно событие: сколько записано и записано ли всё, что надо."""
        if item.kind == "message_created" and item.message is not None:
            return await self._receive(item.message)
        if item.kind == "message_edited" and item.message is not None:
            await self._edit(item.message)
            return 0, True
        if item.kind == "message_removed" and item.message_id is not None:
            await self._remove(item.message_id, item.chat_id)
            return 0, True
        if item.kind in ("bot_started", "bot_stopped") and item.user_id == self._owner:
            return 0, await self._switch(enabled=item.kind == "bot_started")
        return 0, True

    async def _switch(self, *, enabled: bool) -> bool:
        """Владелец запустил бота в MAX — площадка включается (и спрашивается
        согласие, если ещё не спрашивали); остановил — приём выключается."""
        if enabled:
            return await self._enable()
        if not await self._chats.enable(PLATFORM, None, enabled=False):
            return False
        self._enabled = False
        self._accept_due = False
        return True

    async def _enable(self) -> bool:
        """Площадка включается один раз за процесс (§25.5): при первом
        сообщении боту уходит вопрос о согласии. `False` — база не ответила."""
        if not self._enabled:
            self._enabled = await self._chats.enable(PLATFORM, None)
        return self._enabled

    async def _receive(self, message: MaxMessage) -> tuple[int, bool]:
        """Новое сообщение — в общий путь; база сама сверяет согласие (§25.5).

        Площадку включает только сообщение владельца боту: группа её не
        включает — иначе после перезапуска она снимала бы остановку бота
        владельцем. Сохранено ли, решает база; отказ базы — пачка ещё раз.
        """
        title = ""
        if message.chat_type == "chat":
            try:
                group = await self._group(message)
            except MaxError as error:
                logger.warning("MAX: группа %s не проверена: %s", message.chat_id, error.reason)
                return 0, False
            if group is None:
                return 0, True
            title = group.title
        incomings = incoming_of(message, self._owner, title)
        if not incomings:
            if message.chat_type == "dialog":
                logger.info("MAX: сообщение не владельца в личке бота — не храню и не отвечаю")
            return 0, True
        if message.chat_type == "dialog" and not await self._enable():
            return 0, False
        stored = 0
        for incoming in incomings:
            result = await self._chats.receive(incoming, self._loader(incoming))
            if result is None:
                return stored, False
            if result.outcome == "stored":
                stored += 1
                self._remember(message.mid, incoming.chat_key)
                if message.chat_type == "dialog":
                    self._owner_wrote()
        return stored, True

    async def _group(self, message: MaxMessage) -> Group | None:
        """Группа, где есть владелец, — её название; без владельца — `None`:
        бота в группу мог добавить кто угодно, а читать Соломон должен только
        группы владельца (§27.3). Проверка — раз в час."""
        chat_id = message.chat_id
        if chat_id is None:
            return None
        now = self._clock()
        from_owner = message.sender is not None and message.sender.user_id == self._owner
        group = self._groups.get(chat_id)
        if group is None or now - group.checked_at >= GROUP_CHECK_EVERY:
            title = await self._api.chat_title(chat_id)
            with_owner = from_owner or await self._api.is_member(chat_id, self._owner)
            group = Group(title=title, with_owner=with_owner, checked_at=now)
            if not with_owner:
                logger.info("MAX: группа %s без владельца — не читаю", chat_id)
        elif from_owner and not group.with_owner:
            group = Group(title=group.title, with_owner=True, checked_at=group.checked_at)
        self._groups[chat_id] = group
        return group if group.with_owner else None

    def _remember(self, mid: str, chat_key: str) -> None:
        keys = self._keys.pop(mid, ())
        self._keys[mid] = (*keys, chat_key) if chat_key not in keys else keys
        while len(self._keys) > REMEMBERED:
            self._keys.popitem(last=False)

    async def _edit(self, message: MaxMessage) -> None:
        """Правка в MAX (§25.1): до разбора меняет текст, после — ничего."""
        for incoming in incoming_of(message, self._owner):
            await self._chats.edited(incoming)

    async def _remove(self, mid: str, chat_id: int | None) -> None:
        """Удаление в MAX стирает текст (§25.1). Событие называет только id
        сообщения и чат: чаты сообщения процесс помнит, группу знает по id,
        остальное — заметка."""
        keys = self._keys.get(mid)
        if keys is None:
            in_group = chat_id is not None and chat_id in self._groups
            keys = (f"{GROUP_PREFIX}{chat_id}",) if in_group else (NOTES_KEY,)
        for key in keys:
            await self._chats.deleted(
                platform=PLATFORM, connection_id=None, chat_key=key, external_ids=[mid]
            )

    def _loader(self, incoming: Incoming) -> AudioLoader | None:
        """Скачивание голосового для Deepgram (§9) — по ссылке из вложения."""
        url = incoming.file_id
        if incoming.kind != "voice" or url is None:
            return None

        async def load() -> bytes:
            return await self._api.download(url)

        return load
