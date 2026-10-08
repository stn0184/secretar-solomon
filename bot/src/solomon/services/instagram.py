"""Instagram Direct: опрос бизнес-аккаунта владельца.

Источник правды — `techspec/26-instagram.md`. Здесь только источник: что
пришло в Direct, уходит в общий путь личных чатов (`services/chats.py`,
§25) — хранение, согласие, разбор, «ждёт ответа» и сообщения владельцу там.

- Ключ (§26.2): долгоживущий ключ из `INSTAGRAM_TOKEN`; продлённый живёт в
  файле состояния вне папки кода (`STATE_PATH`, права 600). Главнее ключ из
  `.env`: файл помнит, из какого ключа начат, и другой ключ в `.env` начинает
  его заново. Продление — когда до конца меньше 10 дней или конец
  неизвестен. Ключ отвергнут — опрос стоит до перезапуска с новым ключом,
  владельцу один раз «Instagram отключился…».
- Опрос (§26.3): раз в 5 минут шагом тика, в фоне — тик его не ждёт.
  Разговоры, обновлённые после прошлого опроса, и их новые сообщения —
  в общий путь; курсор двигается только после удачного опроса, поэтому сбой
  Meta не теряет сообщений. До согласия Direct не читается вовсе.
- Сеть — только `HttpInstagramApi` (httpx) за протоколом `InstagramApi`:
  тесты подставляют свой. Запросы только `GET` — **в Direct отсюда не
  уходит ничего** (§26.4).

Ключ не попадает ни в базу, ни в журнал: в журнал — только коды ответов
Meta и числа, без текста ошибок и адресов. Данные запросы несут ключ
заголовком, а не в адресе; адрес продления несёт его по документации Meta —
его вырезает `cli.HidingFormatter`.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Protocol

import httpx

from solomon import texts
from solomon.config import Settings
from solomon.db.chats import ChatKind, Platform, Stored
from solomon.services.chats import AudioLoader, Incoming, OwnerSender, in_window

logger = logging.getLogger(__name__)

PLATFORM: Platform = "instagram"
# Instagram API with Instagram Login (§26.1): хост и версия — из документации
# Meta на 2026-10-07; продление ключа — без версии.
API_URL = "https://graph.instagram.com"
API_VERSION = "v25.0"
# Файл состояния — вне клона репозитория, в папке служебного пользователя
# (`techspec/16-server.md` §16.2).
STATE_PATH = Path("/opt/solomon/state/instagram.json")

# Опрос раз в 5 минут (§26.3); первый опрос после запуска «дочитывает»
# последние сутки — так после согласия приходят сообщения, пришедшие до него.
POLL_EVERY = timedelta(minutes=5)
LOOKBACK = timedelta(days=1)
# Перекрытие с прошлым опросом: Meta показывает новое не мгновенно, а часы
# сервера и Meta расходятся. Повтор сообщения база не хранит дважды.
OVERLAP = timedelta(minutes=10)
# Ключ живёт 60 дней; продлевается, когда осталось меньше 10. Не вышло —
# новая попытка не раньше чем через 6 часов.
REFRESH_BEFORE = timedelta(days=10)
REFRESH_RETRY = timedelta(hours=6)
# API отдаёт подробности только 20 последних сообщений разговора (§26.3).
MESSAGES_LIMIT = 20
MESSAGE_FIELDS = "id,created_time,from,to,message,attachments,story"
# Страниц разговоров за опрос и разговоров за опрос — предел на случай
# долгого простоя; остальное — следующим опросом или в журнал.
CONVERSATION_PAGES = 5
CONVERSATIONS_PER_POLL = 30
# Чтение разговоров у Meta — не больше двух запросов в секунду.
PACE_SECONDS = 0.5
TIMEOUT = httpx.Timeout(20.0, connect=5.0)
# Голосовое Direct — до 25 МБ (предел Meta на аудио).
AUDIO_LIMIT = 25 * 1024 * 1024

# Коды Meta. 190 и 102 — ключ истёк, отозван или испорчен: нужен новый
# (§26.2). 10 и 200–299 — доступа к Direct нет: у ключа нет права или в
# Instagram выключено «Разрешить доступ к сообщениям» (#200). Новый ключ тут
# может и не понадобиться — опрос продолжается и возобновится сам.
KEY_CODES = frozenset({102, 190})
ACCESS_CODES = frozenset({10, *range(200, 300)})


class InstagramError(Exception):
    """Meta не ответила или ответила отказом. `reason` — для журнала: код и
    статус, без текста ошибки и без адреса запроса."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class KeyRejected(InstagramError):
    """Ключ отвергнут — истёк или отозван (§26.2)."""


class AccessDenied(InstagramError):
    """Ключ принят, но Direct читать нельзя: нет права у ключа или доступ к
    сообщениям выключен в настройках Instagram."""


def _int(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def meta_error(status: int, payload: Any) -> InstagramError:
    """Ошибка из ответа Meta: отказ ключа — `KeyRejected`, нет доступа к
    Direct — `AccessDenied`, остальное (лимит, сбой сервера, кривой запрос) —
    `InstagramError`, следующий опрос."""
    error = payload.get("error") if isinstance(payload, Mapping) else None
    code = _int(error.get("code")) if isinstance(error, Mapping) else None
    if code is None:
        return InstagramError(f"Meta ответила {status}")
    subcode = _int(error.get("error_subcode")) if isinstance(error, Mapping) else None
    reason = f"Meta ответила {status}, код {code}" + (f"/{subcode}" if subcode else "")
    if code in KEY_CODES:
        return KeyRejected(reason)
    if code in ACCESS_CODES:
        return AccessDenied(reason)
    return InstagramError(reason)


def parse_time(value: Any) -> datetime | None:
    """Время Meta: `2026-10-07T07:00:00+0000` или секунды Unix. Не то — `None`."""
    seconds = _int(value)
    if seconds is None and isinstance(value, str) and value.isascii() and value.isdigit():
        seconds = int(value)
    if seconds is not None:
        return datetime.fromtimestamp(seconds, UTC)
    if not isinstance(value, str):
        return None
    try:
        moment = datetime.fromisoformat(value)
    except ValueError:
        return None
    return moment if moment.tzinfo is not None else None


# --- Ответы Meta (§26.3) ---------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Account:
    """Бизнес-аккаунт владельца: id профессионального аккаунта и имя."""

    user_id: str
    username: str


@dataclass(frozen=True, slots=True)
class Conversation:
    """Разговор Direct и когда в нём было последнее сообщение."""

    id: str
    updated_at: datetime


@dataclass(frozen=True, slots=True)
class Attachment:
    """Вложение: голосовое (со ссылкой на файл), снимок или прочее."""

    kind: ChatKind
    url: str | None


@dataclass(frozen=True, slots=True)
class DirectMessage:
    """Сообщение Direct: кто, кому (id и имя), когда, текст и вложение."""

    id: str
    sent_at: datetime
    sender_id: str
    sender_name: str
    recipients: tuple[tuple[str, str], ...]
    text: str
    attachment: Attachment | None
    story: bool


def _text(value: Any) -> str:
    return value if isinstance(value, str) else ""


def _first(payload: Any) -> Any:
    """Ответ Meta бывает объектом или списком в `data` — первая запись."""
    if isinstance(payload, Mapping) and isinstance(payload.get("data"), list):
        data = payload["data"]
        return data[0] if data else None
    return payload


def parse_account(payload: Any) -> Account:
    """`/me?fields=user_id,username` — id бизнес-аккаунта обязателен."""
    row = _first(payload)
    user_id = row.get("user_id") if isinstance(row, Mapping) else None
    if isinstance(user_id, int) and not isinstance(user_id, bool):
        user_id = str(user_id)
    if not isinstance(user_id, str) or not user_id:
        raise InstagramError("Meta не назвала бизнес-аккаунт")
    return Account(user_id=user_id, username=_text(row.get("username")))


def parse_conversations(payload: Any) -> tuple[list[Conversation], str | None]:
    """Страница разговоров и курсор следующей (нет следующей — `None`)."""
    data = payload.get("data") if isinstance(payload, Mapping) else None
    found: list[Conversation] = []
    for row in data if isinstance(data, list) else []:
        if not isinstance(row, Mapping):
            continue
        conversation_id = row.get("id")
        updated_at = parse_time(row.get("updated_time"))
        if isinstance(conversation_id, str) and conversation_id and updated_at is not None:
            found.append(Conversation(id=conversation_id, updated_at=updated_at))
    paging = payload.get("paging") if isinstance(payload, Mapping) else None
    after = None
    if found and isinstance(paging, Mapping) and paging.get("next"):
        cursors = paging.get("cursors")
        if isinstance(cursors, Mapping) and isinstance(cursors.get("after"), str):
            after = cursors["after"] or None
    return found, after


def _url(value: Any) -> str | None:
    return value if isinstance(value, str) and value else None


def _data_url(value: Any) -> str | None:
    return _url(value.get("url")) if isinstance(value, Mapping) else None


def attachment_of(raw: Mapping[str, Any]) -> Attachment:
    """Вид вложения (§26.3): голосовое — по типу файла, имени `audioclip` или
    `audio_data`; снимок — по типу или `image_data`; остальное — прочее.

    Поля вложения Meta описывает по-разному для разных путей (`file_url`,
    `image_data`, `video_data`), поэтому ссылка берётся из того, что есть.
    """
    mime = _text(raw.get("mime_type")).lower()
    name = _text(raw.get("name")).lower()
    audio = raw.get("audio_data")
    if mime.startswith("audio/") or name.startswith("audioclip") or isinstance(audio, Mapping):
        url = _url(raw.get("file_url")) or _data_url(audio) or _data_url(raw.get("video_data"))
        return Attachment(kind="voice", url=url)
    image = raw.get("image_data")
    if mime.startswith("image/") or isinstance(image, Mapping):
        return Attachment(kind="photo", url=_data_url(image) or _url(raw.get("file_url")))
    return Attachment(kind="other", url=None)


def _person(value: Any) -> tuple[str, str] | None:
    if not isinstance(value, Mapping):
        return None
    person_id = value.get("id")
    if isinstance(person_id, int) and not isinstance(person_id, bool):
        person_id = str(person_id)
    if not isinstance(person_id, str) or not person_id:
        return None
    return person_id, _text(value.get("username"))


def _message(raw: Any) -> DirectMessage | None:
    """Одно сообщение из ответа Meta; без id, времени или отправителя — `None`."""
    if not isinstance(raw, Mapping):
        return None
    message_id = raw.get("id")
    sent_at = parse_time(raw.get("created_time"))
    sender = _person(raw.get("from"))
    if not isinstance(message_id, str) or not message_id or sent_at is None or sender is None:
        return None
    to = raw.get("to")
    people = to.get("data") if isinstance(to, Mapping) else None
    recipients = tuple(
        person for person in map(_person, people if isinstance(people, list) else []) if person
    )
    attachments = raw.get("attachments")
    files = attachments.get("data") if isinstance(attachments, Mapping) else None
    first = files[0] if isinstance(files, list) and files else None
    return DirectMessage(
        id=message_id,
        sent_at=sent_at,
        sender_id=sender[0],
        sender_name=sender[1],
        recipients=recipients,
        text=_text(raw.get("message")),
        attachment=attachment_of(first) if isinstance(first, Mapping) else None,
        story=bool(raw.get("story")),
    )


def parse_messages(payload: Any) -> list[DirectMessage]:
    """Сообщения разговора (`/<разговор>?fields=messages…`); кривые — мимо."""
    messages = payload.get("messages") if isinstance(payload, Mapping) else None
    data = messages.get("data") if isinstance(messages, Mapping) else None
    found = (_message(raw) for raw in (data if isinstance(data, list) else []))
    return [message for message in found if message is not None]


def _handle(username: str) -> str:
    return f"@{username}" if username else ""


def to_incoming(message: DirectMessage, conversation_id: str, account: Account) -> Incoming | None:
    """Сообщение Direct в общий путь (§26.3): от бизнес-аккаунта — `out`, иначе
    `in`; чат — разговор, имя — `@username` собеседника. Ответ на историю и
    групповой разговор — `None`: их Соломон не читает (§26.4).

    `username` без «@» — для «Открыть чат» в Direct (§25.4); Meta его не
    назвала — `None`: в чате остаётся прежнее."""
    if message.story or len(message.recipients) > 1:
        return None
    mine = message.sender_id == account.user_id or (
        bool(account.username) and message.sender_name == account.username
    )
    if mine:
        counterpart = message.recipients[0][1] if message.recipients else ""
    else:
        counterpart = message.sender_name
    attachment = message.attachment
    if attachment is not None:
        kind: ChatKind = attachment.kind
        text = "" if kind == "voice" else message.text
    else:
        kind = "text" if message.text.strip() else "other"
        text = message.text
    return Incoming(
        platform=PLATFORM,
        connection_id=account.user_id,
        chat_key=conversation_id,
        chat_name=_handle(counterpart),
        external_id=message.id,
        direction="out" if mine else "in",
        sender=_handle(message.sender_name),
        sent_at=message.sent_at,
        kind=kind,
        text=text,
        file_id=attachment.url if attachment is not None and kind == "voice" else None,
        username=counterpart or None,
    )


def fresh(messages: Sequence[DirectMessage], horizon: datetime) -> tuple[list[DirectMessage], bool]:
    """Сообщения новее горизонта, старшие первыми, и переполнение: API отдал
    предел, и все они новые — старшие могли не войти (§26.3)."""
    ordered = sorted(messages, key=lambda message: message.sent_at)
    found = [message for message in ordered if message.sent_at > horizon]
    overflow = len(ordered) >= MESSAGES_LIMIT and len(found) == len(ordered)
    return found, overflow


# --- Ключ и файл состояния (§26.2) ---------------------------------------------


@dataclass(frozen=True, slots=True)
class KeyState:
    """Файл состояния: из какого ключа `.env` начат (отпечаток), действующий
    ключ и его конец, отказ ключа и ушло ли о нём сообщение, курсор опроса."""

    source: str
    token: str
    expires_at: datetime | None
    rejected_at: datetime | None
    reported_at: datetime | None
    polled_until: datetime | None


def fingerprint(token: str) -> str:
    """Отпечаток ключа `.env`: по нему видно, что ключ сменили, а сам ключ —
    нет."""
    return "sha256:" + hashlib.sha256(token.encode("utf-8")).hexdigest()


def start_state(env_token: str, saved: KeyState | None) -> KeyState:
    """Состояние при запуске: файл того же ключа `.env` — как есть; нет файла
    или ключ в `.env` другой — заново с ключа `.env` (§26.2)."""
    source = fingerprint(env_token)
    if saved is not None and saved.source == source:
        return saved
    return KeyState(
        source=source,
        token=env_token,
        expires_at=None,
        rejected_at=None,
        reported_at=None,
        polled_until=None,
    )


def refresh_due(state: KeyState, now: datetime) -> bool:
    """Пора продлевать: конец неизвестен или до него меньше 10 дней."""
    return state.expires_at is None or state.expires_at - now < REFRESH_BEFORE


def horizon_of(state: KeyState, now: datetime) -> datetime:
    """С какого времени читать: курсор прошлого опроса с перекрытием или —
    без курсора — последние сутки."""
    if state.polled_until is None:
        return now - LOOKBACK
    return state.polled_until - OVERLAP


_MOMENTS = ("expires_at", "rejected_at", "reported_at", "polled_until")


def state_to_json(state: KeyState) -> dict[str, str | None]:
    data: dict[str, str | None] = {"source": state.source, "token": state.token}
    for field in _MOMENTS:
        value: datetime | None = getattr(state, field)
        data[field] = None if value is None else value.isoformat()
    return data


def state_from_json(data: Any) -> KeyState | None:
    """Состояние из файла; испорченное — `None`, и бот начнёт с ключа `.env`."""
    if not isinstance(data, Mapping):
        return None
    source, token = data.get("source"), data.get("token")
    if not isinstance(source, str) or not source or not isinstance(token, str) or not token:
        return None
    moments: dict[str, datetime | None] = {}
    for field in _MOMENTS:
        value = data.get(field)
        if value is None:
            moments[field] = None
            continue
        moment = parse_time(value)
        if moment is None:
            return None
        moments[field] = moment
    return KeyState(source=source, token=token, **moments)


class StateFile:
    """Файл состояния на диске: папка 700, файл 600, запись через временный
    файл — оборванная запись не портит прежний. Сбой — строка в журнал."""

    def __init__(self, path: Path = STATE_PATH) -> None:
        self._path = path

    def load(self) -> KeyState | None:
        try:
            text = self._path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return None
        except OSError as error:
            logger.error("Файл состояния Instagram не прочитан: %s", type(error).__name__)
            return None
        try:
            state = state_from_json(json.loads(text))
        except ValueError:
            state = None
        if state is None:
            logger.error("Файл состояния Instagram испорчен: начинаю с ключа из .env")
        return state

    def save(self, state: KeyState) -> bool:
        temp = self._path.with_name(self._path.name + ".tmp")
        try:
            self._path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            handle = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(handle, "w", encoding="utf-8") as file:
                json.dump(state_to_json(state), file)
            os.chmod(temp, 0o600)
            os.replace(temp, self._path)
        except OSError as error:
            logger.error("Файл состояния Instagram не записан: %s", type(error).__name__)
            return False
        return True


# --- API Instagram (§26.3) -------------------------------------------------------


class InstagramApi(Protocol):
    """Instagram API — то, что подменяет тест. Отказы — `InstagramError`."""

    async def account(self, token: str) -> Account: ...

    async def conversations(
        self, token: str, after: str | None
    ) -> tuple[list[Conversation], str | None]: ...

    async def messages(self, token: str, conversation_id: str) -> list[DirectMessage]: ...

    async def refresh(self, token: str) -> tuple[str, int]: ...

    async def download(self, url: str) -> bytes: ...

    async def close(self) -> None: ...


class HttpInstagramApi:
    """Instagram API with Instagram Login поверх httpx. Только `GET`."""

    def __init__(
        self, client: httpx.AsyncClient, base_url: str = API_URL, version: str = API_VERSION
    ) -> None:
        self._client = client
        self._base = base_url.rstrip("/")
        self._version = version

    @classmethod
    def create(cls) -> HttpInstagramApi:
        return cls(httpx.AsyncClient(timeout=TIMEOUT))

    async def _get(
        self, url: str, params: Mapping[str, str], headers: Mapping[str, str] | None = None
    ) -> Any:
        """Запрос и разбор ответа. Исключение httpx не выходит наружу как есть:
        в его тексте адрес — `from None`, чтобы и в трассировке его не было."""
        try:
            response = await self._client.get(url, params=dict(params), headers=headers)
        except httpx.TimeoutException:
            raise InstagramError("таймаут") from None
        except httpx.HTTPError as error:
            raise InstagramError(f"сеть: {type(error).__name__}") from None
        try:
            payload = response.json()
        except ValueError:
            payload = None
        if response.is_error or (isinstance(payload, Mapping) and "error" in payload):
            raise meta_error(response.status_code, payload)
        return payload

    async def _graph(self, path: str, token: str, params: Mapping[str, str]) -> Any:
        """Запрос к данным: ключ — заголовком, в адресе его нет."""
        url = f"{self._base}/{self._version}/{path}"
        return await self._get(url, params, {"Authorization": f"Bearer {token}"})

    async def account(self, token: str) -> Account:
        return parse_account(await self._graph("me", token, {"fields": "user_id,username"}))

    async def conversations(
        self, token: str, after: str | None
    ) -> tuple[list[Conversation], str | None]:
        params = {"platform": PLATFORM, "fields": "id,updated_time"}
        if after is not None:
            params["after"] = after
        return parse_conversations(await self._graph("me/conversations", token, params))

    async def messages(self, token: str, conversation_id: str) -> list[DirectMessage]:
        fields = f"messages.limit({MESSAGES_LIMIT}){{{MESSAGE_FIELDS}}}"
        return parse_messages(await self._graph(conversation_id, token, {"fields": fields}))

    async def refresh(self, token: str) -> tuple[str, int]:
        """Продление (§26.2): по документации Meta ключ — в адресе; его прячет
        журнал (`cli.HidingFormatter`)."""
        payload = await self._get(
            f"{self._base}/refresh_access_token",
            {"grant_type": "ig_refresh_token", "access_token": token},
        )
        fresh_token = payload.get("access_token") if isinstance(payload, Mapping) else None
        expires_in = _int(payload.get("expires_in")) if isinstance(payload, Mapping) else None
        if not isinstance(fresh_token, str) or not fresh_token or not expires_in:
            raise InstagramError("Meta не вернула продлённый ключ")
        return fresh_token, expires_in

    async def download(self, url: str) -> bytes:
        """Голосовое по ссылке CDN Meta — без ключа: ссылка подписана сама и
        ведёт переадресацией на сам файл."""
        try:
            async with self._client.stream("GET", url, follow_redirects=True) as response:
                if response.is_error:
                    raise InstagramError(f"файл не скачан: {response.status_code}")
                chunks: list[bytes] = []
                size = 0
                async for chunk in response.aiter_bytes():
                    size += len(chunk)
                    if size > AUDIO_LIMIT:
                        raise InstagramError("файл больше предела")
                    chunks.append(chunk)
        except httpx.HTTPError as error:
            raise InstagramError(f"файл не скачан: {type(error).__name__}") from None
        return b"".join(chunks)

    async def close(self) -> None:
        await self._client.aclose()


# --- Опрос и шаг тика (§26.2–26.3) -----------------------------------------------


class ChatSink(Protocol):
    """Общий путь личных чатов (`ChatService`): включение площадки, согласие и
    приём сообщения."""

    async def enable(
        self, platform: Platform, connection_id: str | None = None, *, enabled: bool = True
    ) -> bool: ...

    async def reading(self, platform: Platform) -> bool | None: ...

    async def receive(
        self, incoming: Incoming, load_audio: AudioLoader | None = None
    ) -> Stored | None: ...


Sleep = Callable[[float], Awaitable[None]]
Clock = Callable[[], datetime]


class InstagramService:
    """Direct владельца: ключ, опрос раз в 5 минут и сообщение об отказе
    ключа. Собирается при запуске, только если `INSTAGRAM_TOKEN` задан."""

    def __init__(
        self,
        settings: Settings,
        token: str,
        chats: ChatSink,
        api: InstagramApi,
        send: OwnerSender,
        state_file: StateFile | None = None,
        clock: Clock | None = None,
        sleep: Sleep = asyncio.sleep,
    ) -> None:
        self._settings = settings
        self._env_token = token
        self._chats = chats
        self._api = api
        self._send = send
        self._file = state_file or StateFile()
        self._clock = clock or self._now
        self._sleep = sleep
        self._state: KeyState | None = None
        self._account: Account | None = None
        self._enabled = False
        # Память процесса: когда пробовали продлить и когда начат опрос.
        self._refresh_tried_at: datetime | None = None
        self._launched_at: datetime | None = None
        self._worker: asyncio.Task[None] | None = None
        # Нет доступа к Direct: опрос идёт дальше, владельцу — один раз за
        # процесс, пока доступ не вернётся.
        self._denied = False
        self._denied_told = False

    def _now(self) -> datetime:
        return datetime.now(self._settings.owner_timezone)

    @classmethod
    def with_client(
        cls, settings: Settings, chats: ChatSink, send: OwnerSender
    ) -> InstagramService | None:
        """Обычная сборка: настоящий API и файл состояния. Ключа нет — `None`:
        источник выключен, бот работает как без него (§26.1)."""
        if settings.instagram_token is None:
            return None
        return cls(settings, settings.instagram_token, chats, HttpInstagramApi.create(), send)

    # --- Состояние ---------------------------------------------------------------

    def _current(self) -> KeyState:
        """Состояние процесса; первый раз — из файла или с ключа `.env`."""
        if self._state is None:
            saved = self._file.load()
            state = start_state(self._env_token, saved)
            if saved is not None and saved.source != state.source:
                logger.info("Ключ Instagram в .env другой: файл состояния начат заново")
            self._state = state
            if state != saved:
                self._file.save(state)
        return self._state

    def _save(self, state: KeyState) -> None:
        self._state = state
        self._file.save(state)

    # --- Шаг тика ------------------------------------------------------------------

    async def tick(self, now: datetime | None = None) -> int:
        """Шаг минутного тика: сообщение об отказе ключа, если оно не ушло (с
        08:00 до 22:00), и раз в 5 минут — опрос в фоне. Возвращает, сколько
        сообщений ушло владельцу."""
        moment = now or self._clock()
        state = self._current()
        sent = await self._report_rejection(state, moment)
        sent += await self._report_no_access(moment)
        if state.rejected_at is None and self._due(moment):
            self.launch(moment)
        return sent

    def _due(self, moment: datetime) -> bool:
        if self._worker is not None and not self._worker.done():
            return False
        return self._launched_at is None or moment - self._launched_at >= POLL_EVERY

    def launch(self, moment: datetime | None = None) -> bool:
        """Опрос в фоне: тик его не ждёт. `False` — опрос уже идёт."""
        if self._worker is not None and not self._worker.done():
            return False
        self._launched_at = moment or self._clock()
        self._worker = asyncio.create_task(self._work(self._launched_at))
        return True

    async def _work(self, moment: datetime) -> None:
        try:
            await self.poll(moment)
        except Exception:  # фоновая задача: исключение иначе потерялось бы молча
            logger.exception("Опрос Instagram упал")

    async def wait(self) -> None:
        """Дождаться фонового опроса."""
        if self._worker is not None:
            await asyncio.gather(self._worker, return_exceptions=True)

    async def stop(self) -> None:
        """Остановка бота: опрос обрывается — курсор стоит на последнем целиком
        прочитанном разговоре, следующий запуск дочитает; клиент закрывается."""
        if self._worker is not None and not self._worker.done():
            self._worker.cancel()
        await self.wait()
        await self._api.close()

    async def _report_rejection(self, state: KeyState, moment: datetime) -> int:
        """«Instagram отключился…» — один раз (§26.2), с 08:00 до 22:00, как всё
        о чатах (§25.4): «отправить → пометить» в файле состояния."""
        if state.rejected_at is None or state.reported_at is not None:
            return 0
        if not in_window(moment, self._settings.owner_timezone):
            return 0
        try:
            await self._send(text=texts.INSTAGRAM_REJECTED)
        except Exception as error:  # noqa: BLE001 - отказ Telegram: следующий тик пришлёт
            logger.warning("Сообщение об отказе ключа Instagram не ушло: %s", type(error).__name__)
            return 0
        logger.info("Сообщение об отказе ключа Instagram ушло")
        self._save(replace(state, reported_at=moment))
        return 1

    async def _report_no_access(self, moment: datetime) -> int:
        """«Instagram не пускает к сообщениям…» — один раз за процесс, с 08:00
        до 22:00; опрос тем временем идёт и сам увидит, что доступ вернулся."""
        if not self._denied or self._denied_told:
            return 0
        if not in_window(moment, self._settings.owner_timezone):
            return 0
        try:
            await self._send(text=texts.INSTAGRAM_NO_ACCESS)
        except Exception as error:  # noqa: BLE001 - отказ Telegram: следующий тик пришлёт
            logger.warning("Сообщение о доступе к Direct не ушло: %s", type(error).__name__)
            return 0
        self._denied_told = True
        return 1

    # --- Опрос ---------------------------------------------------------------------

    async def poll(self, now: datetime | None = None) -> int:
        """Один опрос (§26.3): ключ (и продление), бизнес-аккаунт и включение
        площадки, согласие, новые сообщения. Возвращает, сколько сообщений
        записано. Сбой Meta — строка в журнал, курсор не сдвинут: следующий
        опрос заберёт пропущенное. Отказ ключа — опрос стоит."""
        moment = now or self._clock()
        if self._current().rejected_at is not None:
            return 0
        try:
            token = await self._token(moment)
            account = await self._connect(token)
            if account is None:
                return 0
            reading = await self._chats.reading(PLATFORM)
            if reading is None:
                return 0
            if not reading:
                # Согласия нет — Direct не читается; после согласия опрос
                # начнёт с последних суток (§25.5).
                self._forget_cursor()
                return 0
            stored = await self._read(token, account, moment)
        except AccessDenied as error:
            logger.error(
                "Instagram не пускает к Direct (%s): права ключа или «Разрешить доступ к "
                "сообщениям» в Instagram — опрос продолжается",
                error.reason,
            )
            self._denied = True
            return 0
        except KeyRejected as error:
            logger.error(
                "Ключ Instagram отвергнут (%s): опрос остановлен до перезапуска с новым ключом",
                error.reason,
            )
            self._save(replace(self._current(), rejected_at=moment))
            return 0
        except InstagramError as error:
            logger.warning("Опрос Instagram не удался: %s — повторю следующим", error.reason)
            return 0
        self._denied = self._denied_told = False
        return stored

    async def _token(self, moment: datetime) -> str:
        """Действующий ключ; пора — продлить (§26.2). Не продлился — прежний:
        отказ ключа заметит сам опрос."""
        state = self._current()
        if not refresh_due(state, moment):
            return state.token
        tried = self._refresh_tried_at
        if tried is not None and moment - tried < REFRESH_RETRY:
            return state.token
        self._refresh_tried_at = moment
        try:
            token, expires_in = await self._api.refresh(state.token)
        except InstagramError as error:
            logger.warning("Ключ Instagram не продлён: %s", error.reason)
            return state.token
        self._save(replace(state, token=token, expires_at=moment + timedelta(seconds=expires_in)))
        logger.info("Ключ Instagram продлён: действует ещё %s дн.", expires_in // 86400)
        return token

    async def _connect(self, token: str) -> Account | None:
        """Бизнес-аккаунт — один раз за процесс — и включение площадки: при
        первом опросе с ключом уходит вопрос о согласии (§25.5). База не
        ответила — `None`, следующий опрос попробует снова."""
        if self._account is None:
            self._account = await self._api.account(token)
        if not self._enabled:
            if not await self._chats.enable(PLATFORM, self._account.user_id):
                return None
            self._enabled = True
        return self._account

    def _forget_cursor(self) -> None:
        state = self._current()
        if state.polled_until is not None:
            self._save(replace(state, polled_until=None))

    def _advance(self, until: datetime | None) -> None:
        state = self._current()
        if until is not None and (state.polled_until is None or until > state.polled_until):
            self._save(replace(state, polled_until=until))

    async def _read(self, token: str, account: Account, moment: datetime) -> int:
        """Разговоры, обновлённые после горизонта, от старших к новым; их новые
        сообщения — в общий путь. Курсор: всё прочитано — момент начала
        опроса; оборвалось — последний целиком прочитанный разговор."""
        horizon = horizon_of(self._current(), moment)
        conversations, complete = await self._updated_since(token, horizon)
        ordered = sorted(conversations, key=lambda conversation: conversation.updated_at)
        if len(ordered) > CONVERSATIONS_PER_POLL:
            ordered, complete = ordered[:CONVERSATIONS_PER_POLL], False
        stored = 0
        reached: datetime | None = None
        try:
            for conversation in ordered:
                await self._sleep(PACE_SECONDS)
                taken = await self._take(token, account, conversation, horizon)
                if taken is None:
                    break
                stored += taken
                reached = conversation.updated_at
            else:
                if complete:
                    reached = moment
        finally:
            self._advance(reached)
        if stored:
            logger.info("Instagram: записано новых сообщений %s", stored)
        return stored

    async def _updated_since(
        self, token: str, horizon: datetime
    ) -> tuple[list[Conversation], bool]:
        """Разговоры новее горизонта: страницы идут от свежих, первая же с
        разговором старше горизонта — последняя. Страниц больше предела —
        в журнал (`False`)."""
        found: list[Conversation] = []
        after: str | None = None
        for page in range(CONVERSATION_PAGES):
            if page:
                await self._sleep(PACE_SECONDS)
            batch, after = await self._api.conversations(token, after)
            found.extend(item for item in batch if item.updated_at > horizon)
            if after is None or any(item.updated_at <= horizon for item in batch):
                return found, True
        logger.warning(
            "Instagram: обновлённых разговоров больше %s страниц — старшие не прочитаны",
            CONVERSATION_PAGES,
        )
        return found, False

    async def _take(
        self, token: str, account: Account, conversation: Conversation, horizon: datetime
    ) -> int | None:
        """Новые сообщения одного разговора — в общий путь, по порядку. База не
        записала (сбой или согласие сняли) — `None`: курсор стоит, следующий
        опрос попробует снова."""
        messages = await self._api.messages(token, conversation.id)
        new, overflow = fresh(messages, horizon)
        if overflow:
            logger.warning(
                "Instagram: в разговоре больше %s новых сообщений — старшие не прочитаны",
                MESSAGES_LIMIT,
            )
        stored = 0
        for message in new:
            incoming = to_incoming(message, conversation.id, account)
            if incoming is None:
                continue
            result = await self._chats.receive(incoming, self._loader(incoming))
            if result is None or result.outcome not in ("stored", "repeat"):
                return None
            if result.outcome == "stored":
                stored += 1
        return stored

    def _loader(self, incoming: Incoming) -> AudioLoader | None:
        """Скачивание голосового для Deepgram (§9) — по ссылке из сообщения."""
        url = incoming.file_id
        if incoming.kind != "voice" or url is None:
            return None

        async def load() -> bytes:
            return await self._api.download(url)

        return load
