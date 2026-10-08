"""Бот в MAX (`techspec/27-max.md`): разбор ответов MAX, приём, согласие,
«Принял» и опрос.

Сети нет: чистые функции получают ответы MAX словарями — поля как в
официальной схеме API (github.com/max-messenger/api-schema), клиент ходит в
`httpx.MockTransport`, сервис — в подменённый API. Переписки выдуманные —
Игорь и Олег; токен — игрушечная строка.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import ssl
from collections.abc import Callable
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

import httpx
import pytest

from solomon import texts
from solomon.cli import LOG_FORMAT, HidingFormatter, secrets_of
from solomon.config import Settings
from solomon.runner import build_max
from solomon.services import chats, max_bot
from solomon.services.chats import ChatService
from solomon.services.max_bot import (
    HttpMaxApi,
    MaxAttachment,
    MaxError,
    MaxMessage,
    MaxService,
    MaxUser,
    UpdateBatch,
)
from solomon.services.transcription import Transcript
from tests.conftest import OWNER_TIMEZONE, FakeTranscriber, make_settings
from tests.test_chats import (
    QUIET_LATER,
    FakeChatCall,
    FakeChatStore,
    FakeSender,
    analyzing_service,
    make_answer,
    make_deal,
)
from tests.test_understanding import live_settings

TZ = ZoneInfo(OWNER_TIMEZONE)
NOW = datetime(2026, 10, 7, 12, 0, tzinfo=TZ)
OWNER_MAX = 4242
IGOR_MAX = 5151
OLEG_MAX = 6161
BOT_MAX = 9090
# Личный чат бота с владельцем и с чужим, группа и чат, откуда переслано.
OWNER_DIALOG = 700001
STRANGER_DIALOG = 700002
GROUP = -70000000000005
IGOR_DIALOG = 800001


def ms(moment: datetime) -> int:
    """Время, как его пишет MAX: миллисекунды Unix, UTC."""
    return int(moment.timestamp() * 1000)


def raw_user(
    user_id: int = OWNER_MAX, first: str = "Тим", last: str | None = None
) -> dict[str, Any]:
    user: dict[str, Any] = {"user_id": user_id, "first_name": first, "is_bot": False}
    if last is not None:
        user["last_name"] = last
    return user


IGOR = raw_user(IGOR_MAX, "Игорь", "Петров")
OLEG = raw_user(OLEG_MAX, "Олег")
OWNER = raw_user(OWNER_MAX, "Тим")


def raw_message(
    text: str | None = "Позвонить Олегу в пятницу",
    *,
    mid: str = "mid.0001",
    sender: dict[str, Any] | None = None,
    chat_id: int = OWNER_DIALOG,
    chat_type: str = "dialog",
    at: datetime = NOW - timedelta(minutes=1),
    attachments: list[dict[str, Any]] | None = None,
    link: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Сообщение, как его отдаёт MAX в `message_created`."""
    recipient: dict[str, Any] = {"chat_id": chat_id, "chat_type": chat_type}
    if chat_type == "dialog":
        recipient["user_id"] = BOT_MAX
    body: dict[str, Any] = {"mid": mid, "seq": 116327994376978687, "text": text}
    if attachments is not None:
        body["attachments"] = attachments
    raw: dict[str, Any] = {
        "sender": OWNER if sender is None else sender,
        "recipient": recipient,
        "timestamp": ms(at),
        "body": body,
    }
    if link is not None:
        raw["link"] = link
    return raw


def forward(
    text: str | None = "Пришлёшь расчёт до пятницы?",
    *,
    sender: dict[str, Any] | None = None,
    chat_id: int | None = IGOR_DIALOG,
    mid: str = "mid.original",
    attachments: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Пересланное сообщение — поле `link` с `type: forward`."""
    body: dict[str, Any] = {"mid": mid, "seq": 1, "text": text}
    if attachments is not None:
        body["attachments"] = attachments
    link: dict[str, Any] = {"type": "forward", "message": body, "sender": sender or IGOR}
    if chat_id is not None:
        link["chat_id"] = chat_id
    return link


def audio(url: str = "https://vu.mycdn.me/audio/igor.ogg") -> dict[str, Any]:
    return {"type": "audio", "payload": {"url": url, "token": "audio-token"}}


def update(raw: dict[str, Any], kind: str = "message_created") -> dict[str, Any]:
    return {"update_type": kind, "timestamp": raw["timestamp"] + 5, "message": raw}


def parsed(raw: dict[str, Any]) -> MaxMessage:
    message = max_bot.parse_message(raw)
    assert message is not None
    return message


# ------------------------------------------------------- разбор ответов MAX


def test_time_is_unix_milliseconds_in_utc() -> None:
    assert max_bot.parse_time(1791356400000) == datetime(2026, 10, 7, 7, 0, tzinfo=UTC)
    assert max_bot.parse_time("1791356400000") is None
    assert max_bot.parse_time(True) is None
    assert max_bot.parse_time(None) is None


def test_user_name_is_the_first_and_last_name() -> None:
    assert max_bot.parse_user(IGOR) == MaxUser(IGOR_MAX, "Игорь Петров")
    assert max_bot.parse_user(OLEG) == MaxUser(OLEG_MAX, "Олег")
    assert max_bot.parse_user({**OLEG, "last_name": None}) == MaxUser(OLEG_MAX, "Олег")
    assert max_bot.parse_user({"first_name": "Без id"}) is None
    assert max_bot.parse_user(None) is None


def test_error_carries_the_status_and_the_code_but_not_the_text() -> None:
    error = max_bot.max_error(401, {"code": "verify.token", "message": "Invalid access_token"})

    assert isinstance(error, MaxError)
    assert error.status == 401
    assert error.reason == "MAX ответил 401, код verify.token"
    assert "Invalid" not in error.reason
    assert max_bot.max_error(503, None).reason == "MAX ответил 503"


def test_voice_photo_and_the_rest_are_marked_by_kind() -> None:
    assert max_bot.attachment_of([audio()]) == MaxAttachment(
        "voice", "https://vu.mycdn.me/audio/igor.ogg"
    )
    photo = {
        "type": "image",
        "payload": {"photo_id": 1, "url": "https://i.mycdn.me/p", "token": "t"},
    }
    assert max_bot.attachment_of([photo]) == MaxAttachment("photo", None)
    for kind in ("video", "file", "sticker", "contact", "share", "location"):
        assert max_bot.attachment_of([{"type": kind, "payload": {}}]) == MaxAttachment(
            "other", None
        ), kind


def test_keyboard_is_not_content_and_no_attachments_is_none() -> None:
    keyboard = {"type": "inline_keyboard", "payload": {"buttons": []}}

    assert max_bot.attachment_of([keyboard]) is None
    assert max_bot.attachment_of([keyboard, audio()]) == MaxAttachment(
        "voice", "https://vu.mycdn.me/audio/igor.ogg"
    )
    assert max_bot.attachment_of(None) is None
    assert max_bot.attachment_of([]) is None


def test_message_is_read_with_chat_sender_time_and_text() -> None:
    message = parsed(raw_message("Позвонить Олегу в пятницу", mid="mid.0001"))

    assert message == MaxMessage(
        mid="mid.0001",
        chat_id=OWNER_DIALOG,
        chat_type="dialog",
        sender=MaxUser(OWNER_MAX, "Тим"),
        sent_at=(NOW - timedelta(minutes=1)).astimezone(UTC),
        text="Позвонить Олегу в пятницу",
        attachment=None,
        forwarded=None,
    )


def test_forwarded_message_keeps_its_sender_and_chat() -> None:
    message = parsed(raw_message(None, link=forward("Пришлёшь расчёт до пятницы?")))

    assert message.text == ""
    assert message.forwarded == max_bot.Forwarded(
        sender=MaxUser(IGOR_MAX, "Игорь Петров"),
        chat_id=IGOR_DIALOG,
        text="Пришлёшь расчёт до пятницы?",
        attachment=None,
    )


def test_reply_is_not_a_forward() -> None:
    reply = {**forward(), "type": "reply"}

    assert parsed(raw_message("Да", link=reply)).forwarded is None


def test_message_without_id_or_time_is_none() -> None:
    no_mid = raw_message()
    no_mid["body"] = {"seq": 1, "text": "без id"}
    no_time = raw_message()
    del no_time["timestamp"]
    long_mid = raw_message(mid="m" * 201)

    assert max_bot.parse_message(no_mid) is None
    assert max_bot.parse_message(no_time) is None
    assert max_bot.parse_message(long_mid) is None
    assert max_bot.parse_message("не сообщение") is None


def test_updates_are_read_with_the_marker() -> None:
    created = update(raw_message(mid="mid.1"))
    removed = {
        "update_type": "message_removed",
        "timestamp": 1,
        "message_id": "mid.1",
        "chat_id": GROUP,
        "user_id": OLEG_MAX,
    }
    started = {"update_type": "bot_started", "timestamp": 1, "chat_id": OWNER_DIALOG, "user": OWNER}
    unknown = {"update_type": "dialog_muted", "timestamp": 1, "chat_id": 1, "user": OWNER}

    batch = max_bot.parse_updates(
        {"updates": [created, removed, started, unknown, "мусор"], "marker": 25493970}
    )

    assert batch.marker == 25493970
    assert [item.kind for item in batch.updates] == [
        "message_created",
        "message_removed",
        "bot_started",
        "dialog_muted",
    ]
    assert batch.updates[0].message is not None and batch.updates[0].message.mid == "mid.1"
    assert (batch.updates[1].message_id, batch.updates[1].chat_id) == ("mid.1", GROUP)
    assert (batch.updates[2].user_id, batch.updates[2].chat_id) == (OWNER_MAX, OWNER_DIALOG)


def test_empty_answer_has_no_marker_and_garbage_is_an_error() -> None:
    assert max_bot.parse_updates({"updates": [], "marker": None}) == max_bot.UpdateBatch([], None)
    with pytest.raises(MaxError):
        max_bot.parse_updates({"marker": 1})
    with pytest.raises(MaxError):
        max_bot.parse_updates(None)


# -------------------------------------------- приём: что куда ложится (§27.3)


def test_own_message_to_the_bot_is_a_note_from_you() -> None:
    [note] = max_bot.incoming_of(parsed(raw_message("Позвонить Олегу в пятницу")), OWNER_MAX)

    assert note.platform == "max"
    assert note.connection_id is None
    assert (note.chat_key, note.chat_name) == (chats.NOTES_KEY, "MAX: заметки")
    assert (note.direction, note.sender) == ("out", "Вы")
    assert (note.external_id, note.kind, note.text) == (
        "mid.0001",
        "text",
        "Позвонить Олегу в пятницу",
    )
    assert note.tracks_waiting is False


def test_forwarded_message_is_a_line_of_the_original_chat() -> None:
    message = parsed(raw_message(None, mid="mid.0002", link=forward("Пришлёшь расчёт до пятницы?")))

    [line] = max_bot.incoming_of(message, OWNER_MAX)

    assert (line.chat_key, line.chat_name) == (f"from:{IGOR_DIALOG}", "Игорь Петров")
    assert (line.direction, line.sender) == ("in", "Игорь Петров")
    assert (line.external_id, line.text, line.kind) == (
        "mid.0002",
        "Пришлёшь расчёт до пятницы?",
        "text",
    )
    assert line.sent_at == message.sent_at
    assert line.tracks_waiting is False
    assert line.username is None, "у пересланного нет надёжной ссылки — «Открыть чат» нет"


def test_forwarded_own_message_is_your_line_in_the_same_chat() -> None:
    message = parsed(raw_message(None, link=forward("Да, в пятницу пришлю", sender=OWNER)))

    [line] = max_bot.incoming_of(message, OWNER_MAX)

    assert line.chat_key == f"from:{IGOR_DIALOG}"
    assert line.chat_name == "", "имя чата — собеседник, а не владелец"
    assert (line.direction, line.sender) == ("out", "Вы")


def test_forward_without_its_chat_goes_by_the_sender() -> None:
    from_igor = parsed(raw_message(None, link=forward(chat_id=None)))
    from_owner = parsed(raw_message(None, link=forward("Сам себе", sender=OWNER, chat_id=None)))

    [igor] = max_bot.incoming_of(from_igor, OWNER_MAX)
    [own] = max_bot.incoming_of(from_owner, OWNER_MAX)

    assert (igor.chat_key, igor.chat_name) == (f"user:{IGOR_MAX}", "Игорь Петров")
    assert (own.chat_key, own.chat_name) == (chats.NOTES_KEY, "MAX: заметки")


def test_comment_to_a_forward_is_a_note_beside_the_line() -> None:
    message = parsed(raw_message("Напомни в пятницу", mid="mid.0003", link=forward()))

    line, note = max_bot.incoming_of(message, OWNER_MAX)

    assert (line.chat_key, line.text) == (f"from:{IGOR_DIALOG}", "Пришлёшь расчёт до пятницы?")
    assert (note.chat_key, note.text, note.direction) == (
        chats.NOTES_KEY,
        "Напомни в пятницу",
        "out",
    )
    assert line.external_id == note.external_id == "mid.0003"


def test_forwarded_voice_waits_for_deepgram() -> None:
    message = parsed(raw_message(None, link=forward(None, attachments=[audio()])))

    [line] = max_bot.incoming_of(message, OWNER_MAX)

    assert (line.kind, line.text, line.file_id) == (
        "voice",
        "",
        "https://vu.mycdn.me/audio/igor.ogg",
    )


def test_own_voice_and_photo_are_notes_by_kind() -> None:
    voice = parsed(raw_message(None, attachments=[audio("https://vu.mycdn.me/audio/own.ogg")]))
    photo = {
        "type": "image",
        "payload": {"photo_id": 1, "url": "https://i.mycdn.me/p", "token": "t"},
    }
    snap = parsed(raw_message("Чек за ремонт", attachments=[photo]))

    [heard] = max_bot.incoming_of(voice, OWNER_MAX)
    [seen] = max_bot.incoming_of(snap, OWNER_MAX)

    assert (heard.chat_key, heard.kind, heard.file_id) == (
        chats.NOTES_KEY,
        "voice",
        "https://vu.mycdn.me/audio/own.ogg",
    )
    assert (seen.kind, seen.text, seen.file_id) == ("photo", "Чек за ремонт", None)


def test_stranger_in_the_bot_dialog_is_skipped() -> None:
    """Приёмка 4: чужое сообщение в личке бота не хранится."""
    message = parsed(raw_message("Привет, бот", sender=OLEG, chat_id=STRANGER_DIALOG))

    assert max_bot.incoming_of(message, OWNER_MAX) == []


def test_empty_message_and_channel_post_give_nothing() -> None:
    empty = parsed(raw_message(""))
    post = parsed(raw_message("Новость", chat_id=-1, chat_type="channel"))

    assert max_bot.incoming_of(empty, OWNER_MAX) == []
    assert max_bot.incoming_of(post, OWNER_MAX) == []


def test_group_message_of_the_owner_is_out_and_of_others_in() -> None:
    oleg = parsed(raw_message("Кто купит уголь?", sender=OLEG, chat_id=GROUP, chat_type="chat"))
    own = parsed(raw_message("Я куплю в субботу", chat_id=GROUP, chat_type="chat"))

    [asked] = max_bot.incoming_of(oleg, OWNER_MAX, "Дача")
    [answered] = max_bot.incoming_of(own, OWNER_MAX, "Дача")

    assert (asked.chat_key, asked.chat_name) == (f"{chats.GROUP_PREFIX}{GROUP}", "Дача")
    assert (asked.direction, asked.sender) == ("in", "Олег")
    assert (answered.direction, answered.sender) == ("out", "Вы")
    assert asked.tracks_waiting is False and answered.tracks_waiting is False


def test_forward_inside_a_group_is_read_as_its_text() -> None:
    message = parsed(
        raw_message(
            None, sender=OLEG, chat_id=GROUP, chat_type="chat", link=forward("Привезу доски")
        )
    )

    [line] = max_bot.incoming_of(message, OWNER_MAX, "Дача")

    assert (line.chat_key, line.sender, line.text) == (
        f"{chats.GROUP_PREFIX}{GROUP}",
        "Олег",
        "Привезу доски",
    )


def test_no_max_message_tracks_waiting() -> None:
    """Приёмка 3: «ждёт ответа» по MAX не ведётся вовсе — ни по пересланному,
    ни по заметкам, ни по группам."""
    messages = [
        parsed(raw_message("Заметка")),
        parsed(raw_message(None, link=forward())),
        parsed(raw_message("Вопрос в группе?", sender=OLEG, chat_id=GROUP, chat_type="chat")),
    ]

    found = [
        incoming for message in messages for incoming in max_bot.incoming_of(message, OWNER_MAX)
    ]

    assert len(found) == 3
    assert not any(incoming.tracks_waiting for incoming in found)


# ------------------------------------------------- клиент: HTTP и TLS (§27.2)

MAX_TOKEN = "max-fake-token-for-tests-0042"
ROOT_FINGERPRINT = "d26d2d0231b7c39f92cc738512ba54103519e4405d68b5bd703e9788ca8ecf31"

Handler = Callable[[httpx.Request], httpx.Response]


def mock_api(handler: Handler) -> tuple[HttpMaxApi, list[httpx.Request]]:
    requests: list[httpx.Request] = []

    def record(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return handler(request)

    client = httpx.AsyncClient(transport=httpx.MockTransport(record))
    return HttpMaxApi(client, MAX_TOKEN), requests


def answer(payload: Any, status: int = 200) -> Handler:
    return lambda request: httpx.Response(status, json=payload)


async def test_updates_carry_the_token_in_the_header_and_never_in_the_address() -> None:
    api, requests = mock_api(answer({"updates": [], "marker": 7}))

    batch = await api.updates(None)
    await api.updates(7)

    first, second = requests
    assert batch == UpdateBatch([], 7)
    assert (first.method, first.url.host, first.url.path) == (
        "GET",
        "platform-api2.max.ru",
        "/updates",
    )
    assert first.headers["Authorization"] == MAX_TOKEN, "токен как есть, без Bearer"
    assert MAX_TOKEN not in str(first.url) and MAX_TOKEN not in str(second.url)
    assert dict(first.url.params) == {
        "limit": "100",
        "timeout": "90",
        "types": "message_created,message_edited,message_removed,bot_started,bot_stopped",
    }
    assert second.url.params["marker"] == "7"


async def test_rejected_token_and_unsuccessful_answers_are_errors() -> None:
    rejected, _ = mock_api(answer({"code": "verify.token", "message": "Invalid access_token"}, 401))
    unsuccessful, _ = mock_api(answer({"success": False, "message": "chat.denied"}))

    with pytest.raises(MaxError) as caught:
        await rejected.updates(None)
    with pytest.raises(MaxError):
        await unsuccessful.send_to_user(OWNER_MAX, "Принял, итог пришлю в Telegram.")

    assert caught.value.status == 401


async def test_network_failure_and_timeout_are_errors_without_the_address() -> None:
    def broken(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("certificate verify failed for platform-api2.max.ru")

    def slow(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("timed out")

    network, _ = mock_api(broken)
    timeout, _ = mock_api(slow)

    with pytest.raises(MaxError) as failed:
        await network.updates(None)
    with pytest.raises(MaxError) as waited:
        await timeout.updates(None)

    assert failed.value.reason == "сеть: ConnectError"
    assert failed.value.__cause__ is None and failed.value.__suppress_context__
    assert waited.value.reason == "таймаут"


async def test_group_title_and_membership_are_asked_by_chat() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/members"):
            members = [{**OWNER, "is_owner": True}]
            return httpx.Response(200, json={"members": members, "marker": None})
        return httpx.Response(200, json={"chat_id": GROUP, "type": "chat", "title": "Дача"})

    api, requests = mock_api(handler)

    assert await api.chat_title(GROUP) == "Дача"
    assert await api.is_member(GROUP, OWNER_MAX) is True
    assert await api.is_member(GROUP, IGOR_MAX) is False

    assert requests[0].url.path == f"/chats/{GROUP}"
    assert requests[1].url.path == f"/chats/{GROUP}/members"
    assert requests[1].url.params["user_ids"] == str(OWNER_MAX)


async def test_message_goes_only_to_a_user_by_id() -> None:
    api, requests = mock_api(answer({"message": {"body": {"mid": "mid.x", "seq": 1}}}))

    await api.send_to_user(OWNER_MAX, "Принял, итог пришлю в Telegram.")

    [request] = requests
    assert (request.method, request.url.path) == ("POST", "/messages")
    assert dict(request.url.params) == {"user_id": str(OWNER_MAX)}
    assert json.loads(request.read()) == {"text": "Принял, итог пришлю в Telegram."}


async def test_voice_is_downloaded_without_the_token_and_by_redirect() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/audio/igor.ogg":
            return httpx.Response(302, headers={"Location": "https://cdn.example/file.ogg"})
        return httpx.Response(200, content=b"OggS-voice")

    api, requests = mock_api(handler)

    assert await api.download("https://vu.mycdn.me/audio/igor.ogg") == b"OggS-voice"
    assert all("Authorization" not in request.headers for request in requests)
    assert len(requests) == 2


def test_root_certificate_is_the_one_of_the_ministry() -> None:
    """Сертификат в репозитории — корневой Минцифры, сверенный по отпечатку
    SHA-256 (Госуслуги, gosuslugi.ru/crt)."""
    pem = max_bot.CERT_PATH.read_text(encoding="ascii")
    der = ssl.PEM_cert_to_DER_cert(pem)

    assert hashlib.sha256(der).hexdigest() == ROOT_FINGERPRINT


def _fingerprints(context: ssl.SSLContext) -> set[str]:
    return {hashlib.sha256(der).hexdigest() for der in context.get_ca_certs(binary_form=True)}


def test_max_client_checks_tls_with_the_root_from_the_repository() -> None:
    """Приёмка 5: проверка TLS не отключена — сертификат и имя проверяются,
    а к обычным корневым добавлен сертификат Минцифры; другим клиентам бота
    он не добавляется."""
    context = max_bot.max_ssl_context()

    assert context.verify_mode == ssl.CERT_REQUIRED
    assert context.check_hostname is True
    assert ROOT_FINGERPRINT in _fingerprints(context)
    assert len(_fingerprints(context)) > 1, "обычные корневые остаются"
    assert ROOT_FINGERPRINT not in _fingerprints(httpx.create_ssl_context())


def test_max_client_is_built_with_that_check(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: dict[str, Any] = {}

    class Recorder:
        def __init__(self, **kwargs: Any) -> None:
            seen.update(kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", Recorder)

    max_bot.max_client()

    verify = seen["verify"]
    assert isinstance(verify, ssl.SSLContext)
    assert verify.verify_mode == ssl.CERT_REQUIRED
    assert ROOT_FINGERPRINT in _fingerprints(verify)
    assert seen["timeout"].read > max_bot.POLL_SECONDS, "чтение ждёт дольше удержания опроса"


# ------------------------------------------------- опрос и приём: сервис


class FakeMax:
    """API MAX в памяти: очередь ответов опроса, группы, файлы и что ушло."""

    def __init__(self) -> None:
        self.answers: list[UpdateBatch | MaxError] = []
        self.markers: list[int | None] = []
        self.sent: list[tuple[int, str]] = []
        self.titles: dict[int, str] = {GROUP: "Дача"}
        self.members: dict[int, set[int]] = {GROUP: {OWNER_MAX, OLEG_MAX}}
        self.files: dict[str, bytes] = {}
        self.asked: list[str] = []
        self.send_errors: list[MaxError] = []
        self.closed = False

    def say(self, *raws: dict[str, Any], marker: int | None = None) -> None:
        number = marker if marker is not None else 100 + len(self.markers) + len(self.answers)
        self.answers.append(max_bot.parse_updates({"updates": list(raws), "marker": number}))

    def fail(self, error: MaxError | None = None) -> None:
        self.answers.append(error or MaxError("сеть: ConnectError"))

    async def updates(self, marker: int | None) -> UpdateBatch:
        self.markers.append(marker)
        if not self.answers:
            return UpdateBatch([], None)
        item = self.answers.pop(0)
        if isinstance(item, MaxError):
            raise item
        return item

    async def chat_title(self, chat_id: int) -> str:
        self.asked.append(f"title:{chat_id}")
        return self.titles.get(chat_id, "")

    async def is_member(self, chat_id: int, user_id: int) -> bool:
        self.asked.append(f"member:{chat_id}")
        return user_id in self.members.get(chat_id, set())

    async def send_to_user(self, user_id: int, text: str) -> None:
        if self.send_errors:
            raise self.send_errors.pop(0)
        self.sent.append((user_id, text))

    async def download(self, url: str) -> bytes:
        return self.files[url]

    async def close(self) -> None:
        self.closed = True


class MaxBot:
    """Бот в MAX и общий путь чатов над одной подменённой базой."""

    def __init__(
        self,
        *,
        store: FakeChatStore | None = None,
        sender: FakeSender | None = None,
        transcriber: FakeTranscriber | None = None,
    ) -> None:
        self.store = store or FakeChatStore()
        self.sender = sender or FakeSender()
        self.max = FakeMax()
        self.now = NOW
        self.sleeps: list[float] = []
        self.chats = ChatService(
            settings=make_settings(),
            store=self.store,
            send=self.sender,
            transcriber=transcriber,
        )
        self.service = MaxService(
            settings=max_settings(),
            owner_max_id=OWNER_MAX,
            chats=self.chats,
            api=self.max,
            clock=lambda: self.now,
            sleep=self.sleep,
        )

    async def sleep(self, seconds: float) -> None:
        """Пауза опроса; ответов в очереди больше нет — опрос останавливается."""
        self.sleeps.append(seconds)
        if not self.max.answers:
            raise asyncio.CancelledError

    def lines(self) -> list[tuple[str, str, str, str]]:
        """Записанные сообщения: чат, направление, отправитель, текст."""
        names = {thread["id"]: key for key, thread in self.store.threads.items()}
        return [
            (names[row["thread_id"]], row["direction"], row["sender"], row["text"])
            for row in self.store.messages
        ]


def max_settings() -> Settings:
    return replace(make_settings(), max_bot_token=MAX_TOKEN, owner_max_id=OWNER_MAX)


def consented() -> FakeChatStore:
    """MAX включён, владелец уже согласился."""
    store = FakeChatStore()
    store.consent("max", None)
    return store


def from_owner(text: str | None = "Позвонить Олегу в пятницу", **fields: Any) -> dict[str, Any]:
    return update(raw_message(text, **fields))


def in_group(text: str, *, sender: dict[str, Any] = OLEG, mid: str = "mid.g1") -> dict[str, Any]:
    return update(raw_message(text, mid=mid, sender=sender, chat_id=GROUP, chat_type="chat"))


def bot_event(kind: str, user: dict[str, Any] = OWNER) -> dict[str, Any]:
    return {"update_type": kind, "timestamp": 1, "chat_id": OWNER_DIALOG, "user": user}


# ------------------------------------------ приёмка 1: без переменных MAX нет


def test_without_the_token_or_the_owner_id_max_is_not_built(
    caplog: pytest.LogCaptureFixture,
) -> None:
    service = ChatService(settings=make_settings(), store=FakeChatStore(), send=FakeSender())
    caplog.set_level(logging.WARNING)

    assert build_max(make_settings(), service) is None
    assert build_max(replace(make_settings(), max_bot_token=MAX_TOKEN), service) is None
    assert build_max(replace(make_settings(), owner_max_id=OWNER_MAX), service) is None
    assert "MAX_BOT_TOKEN и OWNER_MAX_ID" in caplog.text
    assert MAX_TOKEN not in caplog.text


async def test_with_both_variables_max_is_built_and_stops_with_the_bot() -> None:
    service = ChatService(settings=make_settings(), store=FakeChatStore(), send=FakeSender())

    built = build_max(max_settings(), service)

    assert isinstance(built, MaxService)
    await built.stop()


async def test_polling_task_is_stopped_and_the_client_closed() -> None:
    bot = MaxBot()
    stopped = asyncio.Event()

    async def wait_forever(marker: int | None) -> UpdateBatch:
        try:
            await asyncio.Event().wait()
        finally:
            stopped.set()
        raise AssertionError("unreachable")

    bot.max.updates = wait_forever  # type: ignore[method-assign]
    task = bot.service.start()
    await asyncio.sleep(0)

    await bot.service.stop()

    assert task.cancelled() and stopped.is_set()
    assert bot.max.closed


def test_the_journal_never_shows_the_max_token() -> None:
    formatter = HidingFormatter(LOG_FORMAT, secrets_of(max_settings()))
    record = logging.LogRecord("x", logging.ERROR, __file__, 1, "token %s", (MAX_TOKEN,), None)

    assert MAX_TOKEN not in formatter.format(record)


# ------------------------------------------------------- согласие (§25.5)


async def test_first_message_to_the_bot_asks_consent_and_keeps_nothing() -> None:
    bot = MaxBot()
    bot.max.say(from_owner("Позвонить Олегу в пятницу"))

    polled = await bot.service.poll()

    assert polled.complete and polled.stored == 0
    assert bot.sender.texts == [texts.consent_question("max")]
    assert bot.store.sources["max"]["connection_id"] is None
    assert bot.store.messages == [], "до «Согласен» пересланное не хранится"


async def test_bot_start_by_the_owner_asks_consent_too() -> None:
    bot = MaxBot()
    bot.max.say(bot_event("bot_started"))

    await bot.service.poll()

    assert bot.sender.texts == [texts.consent_question("max")]


async def test_after_consent_the_message_is_kept_and_asked_once() -> None:
    bot = MaxBot()
    bot.max.say(from_owner("Сначала", mid="mid.1"))
    await bot.service.poll()
    await bot.chats.answer_consent("max", True)
    bot.max.say(from_owner("Позвонить Олегу в пятницу", mid="mid.2"))

    polled = await bot.service.poll()

    assert polled.stored == 1
    assert bot.lines() == [("max:notes", "out", "Вы", "Позвонить Олегу в пятницу")]
    assert bot.sender.texts == [texts.consent_question("max")]


async def test_stopped_bot_keeps_nothing_until_started_again() -> None:
    """Владелец остановил бота в MAX — приём выключен, и группы его не
    включают; запустил снова — читается."""
    bot = MaxBot(store=consented())
    bot.max.say(bot_event("bot_stopped"), in_group("Пока не читай", mid="mid.g1"))
    await bot.service.poll()
    bot.max.say(bot_event("bot_started"), in_group("Снова читай", mid="mid.g2"))

    await bot.service.poll()

    assert [line[3] for line in bot.lines()] == ["Снова читай"]


async def test_group_alone_does_not_switch_max_on() -> None:
    """Согласие спрашивается при первом сообщении боту (§27.3): группа
    площадку не включает и до него ничего не хранится."""
    bot = MaxBot()
    bot.max.say(in_group("Кто купит уголь?"))

    polled = await bot.service.poll()

    assert polled.complete
    assert bot.store.sources == {}
    assert bot.store.messages == []
    assert bot.sender.texts == []


# ------------------------------------- приёмка 4: чужим бот не отвечает и не хранит


async def test_stranger_writing_to_the_bot_is_neither_kept_nor_answered() -> None:
    bot = MaxBot()
    bot.max.say(update(raw_message("Привет, бот", sender=OLEG, chat_id=STRANGER_DIALOG)))
    bot.max.say(bot_event("bot_started", OLEG))

    await bot.service.poll()
    await bot.service.poll()

    assert bot.store.sources == {}, "чужое сообщение не включает площадку"
    assert bot.store.messages == []
    assert bot.sender.texts == []
    assert bot.max.sent == []


# ------------------------------------------------- приём: что куда записано


async def test_forwarded_conversation_is_stored_as_the_original_chat() -> None:
    bot = MaxBot(store=consented())
    bot.max.say(
        from_owner(None, mid="mid.1", link=forward("Пришлёшь расчёт до пятницы?")),
        from_owner(None, mid="mid.2", link=forward("Да, в пятницу пришлю", sender=OWNER)),
    )

    polled = await bot.service.poll()

    chat = f"max:from:{IGOR_DIALOG}"
    assert polled.stored == 2
    assert bot.lines() == [
        (chat, "in", "Игорь Петров", "Пришлёшь расчёт до пятницы?"),
        (chat, "out", "Вы", "Да, в пятницу пришлю"),
    ]
    thread = bot.store.thread(f"from:{IGOR_DIALOG}", "max")
    assert thread["name"] == "Игорь Петров"
    assert thread["tracks_waiting"] is False


async def test_group_of_the_owner_is_read_with_its_title() -> None:
    bot = MaxBot(store=consented())
    bot.max.say(
        in_group("Кто купит уголь?", mid="mid.g1"),
        in_group("Я, в субботу", sender=OWNER, mid="mid.g2"),
    )

    await bot.service.poll()

    group = f"max:{chats.GROUP_PREFIX}{GROUP}"
    assert bot.lines() == [
        (group, "in", "Олег", "Кто купит уголь?"),
        (group, "out", "Вы", "Я, в субботу"),
    ]
    assert bot.store.thread(f"{chats.GROUP_PREFIX}{GROUP}", "max")["name"] == "Дача"
    assert bot.max.asked == [f"title:{GROUP}", f"member:{GROUP}"], "группа проверяется раз в час"


async def test_group_without_the_owner_is_not_read() -> None:
    bot = MaxBot(store=consented())
    bot.max.members[GROUP] = {OLEG_MAX, IGOR_MAX}
    bot.max.say(in_group("Чужая договорённость"))

    polled = await bot.service.poll()

    assert polled.complete
    assert bot.store.messages == []
    assert bot.store.sources["max"]["is_enabled"], "чужая группа площадку не трогает"


async def test_group_is_checked_again_after_an_hour() -> None:
    bot = MaxBot(store=consented())
    bot.max.members[GROUP] = {OLEG_MAX}
    bot.max.say(in_group("Раз", mid="mid.g1"))
    await bot.service.poll()
    bot.max.members[GROUP] = {OLEG_MAX, OWNER_MAX}
    bot.max.say(in_group("Два", mid="mid.g2"))
    await bot.service.poll()
    bot.now = NOW + timedelta(hours=1)
    bot.max.say(in_group("Три", mid="mid.g3"))

    await bot.service.poll()

    assert [line[3] for line in bot.lines()] == ["Три"]


async def test_forwarded_voice_is_downloaded_and_heard_with_the_name() -> None:
    transcriber = FakeTranscriber(Transcript(text="Пришлю договор завтра", confidence=0.9))
    bot = MaxBot(store=consented(), transcriber=transcriber)
    url = "https://vu.mycdn.me/audio/igor.ogg"
    bot.max.files[url] = b"OggS-igor"
    bot.max.say(from_owner(None, link=forward(None, attachments=[audio(url)])))

    await bot.service.poll()

    assert transcriber.calls == [b"OggS-igor"]
    assert transcriber.names == [("Игорь Петров",)]
    assert bot.lines()[0][3] == "Пришлю договор завтра"


async def test_own_voice_note_is_heard_without_a_name_hint() -> None:
    transcriber = FakeTranscriber(Transcript(text="Купить уголь в субботу", confidence=0.9))
    bot = MaxBot(store=consented(), transcriber=transcriber)
    url = "https://vu.mycdn.me/audio/own.ogg"
    bot.max.files[url] = b"OggS-own"
    bot.max.say(from_owner(None, attachments=[audio(url)]))

    await bot.service.poll()

    assert transcriber.names == [()]
    assert bot.lines() == [("max:notes", "out", "Вы", "Купить уголь в субботу")]


async def test_edit_changes_the_text_and_removal_erases_it() -> None:
    bot = MaxBot(store=consented())
    bot.max.say(
        from_owner("Позвонить Олегу в пятницу", mid="mid.1"),
        in_group("Привезу доски", mid="mid.g1"),
    )
    await bot.service.poll()
    edited = update(raw_message("Позвонить Олегу в субботу", mid="mid.1"), "message_edited")
    removed = {
        "update_type": "message_removed",
        "timestamp": 1,
        "message_id": "mid.g1",
        "chat_id": GROUP,
        "user_id": OLEG_MAX,
    }
    bot.max.say(edited, removed)

    await bot.service.poll()

    assert [line[3] for line in bot.lines()] == ["Позвонить Олегу в субботу", ""]


# ----------------------------------------------- приёмка 6: сбои и пропущенное


async def test_marker_moves_only_after_the_batch_is_stored() -> None:
    bot = MaxBot(store=consented())
    bot.max.say(from_owner("Раз", mid="mid.1"), marker=11)
    bot.max.say(from_owner("Два", mid="mid.2"), marker=12)

    await bot.service.poll()
    await bot.service.poll()
    await bot.service.poll()

    assert bot.max.markers == [None, 11, 12]


async def test_max_failure_keeps_polling_with_the_same_marker() -> None:
    """Приёмка 6: MAX не ответил — опрос продолжается с той же отметкой, и
    пропущенное забирается следующим ответом."""
    bot = MaxBot(store=consented())
    bot.max.say(from_owner("Раз", mid="mid.1"), marker=11)
    bot.max.fail()
    bot.max.fail(MaxError("MAX ответил 503", 503))
    bot.max.say(from_owner("Два", mid="mid.2"), marker=12)

    with pytest.raises(asyncio.CancelledError):
        await bot.service.run()

    assert bot.max.markers == [None, 11, 11, 11]
    assert [line[3] for line in bot.lines()] == ["Раз", "Два"]
    assert bot.sleeps == [max_bot.PACE_SECONDS, 5.0, 10.0, max_bot.PACE_SECONDS]


async def test_rejected_token_is_named_in_the_journal_and_polling_goes_on(
    caplog: pytest.LogCaptureFixture,
) -> None:
    bot = MaxBot(store=consented())
    bot.max.fail(MaxError("MAX ответил 401, код verify.token", 401))
    bot.max.say(from_owner("Раз", mid="mid.1"))

    with pytest.raises(asyncio.CancelledError):
        await bot.service.run()

    assert "MAX_BOT_TOKEN" in caplog.text
    assert [line[3] for line in bot.lines()] == ["Раз"]


async def test_database_failure_reads_the_same_batch_again() -> None:
    bot = MaxBot(store=consented())
    bot.store.broken.add("store")
    bot.max.say(from_owner("Раз", mid="mid.1"), marker=11)
    bot.max.say(from_owner("Раз", mid="mid.1"), marker=11)

    failed = await bot.service.poll()
    bot.store.broken.clear()
    again = await bot.service.poll()
    await bot.service.poll()

    assert not failed.complete and again.complete
    assert bot.max.markers == [None, None, 11]
    assert [line[3] for line in bot.lines()] == ["Раз"]


async def test_batch_that_never_stores_is_skipped_after_three_attempts() -> None:
    bot = MaxBot(store=consented())
    bot.store.broken.add("store")
    for _ in range(3):
        bot.max.say(from_owner("Раз", mid="mid.1"), marker=11)

    for _ in range(4):
        await bot.service.poll()

    assert bot.max.markers == [None, None, None, 11]


async def test_group_check_failure_reads_the_batch_again() -> None:
    bot = MaxBot(store=consented())

    async def broken(chat_id: int) -> str:
        raise MaxError("сеть: ConnectError")

    bot.max.chat_title = broken  # type: ignore[method-assign]
    bot.max.say(in_group("Кто купит уголь?"), marker=11)

    polled = await bot.service.poll()

    assert not polled.complete
    assert bot.store.messages == []


# --------------------------------------- приёмка 2 и 3: что видит владелец


async def test_forwarded_deal_reaches_the_owner_marked_max() -> None:
    """Приёмка 2: пересланная договорённость — «Из переписки с … (MAX)
    записал: …» с кнопкой «Убрать» в Telegram."""
    bot = MaxBot(store=consented())
    bot.max.say(
        from_owner(None, mid="mid.1", link=forward("Пришлёшь расчёт до пятницы?")),
        from_owner(None, mid="mid.2", link=forward("Да, в пятницу пришлю", sender=OWNER)),
    )
    await bot.service.poll()
    call = FakeChatCall(make_answer(deals=[make_deal()], with_whom="Игорем", to_whom="Игорю"))
    await analyzing_service(bot.store, call).analyze_due()

    assert await analyzing_service(bot.store, sender=bot.sender).send_reports(QUIET_LATER) == 1

    [(text, buttons)] = bot.sender.sent
    assert text == (
        "Из переписки с Игорем (MAX) записал: прислать Игорю расчёт — пятница, 9 октября "
        "(вы обещали)"
    )
    assert [button.text for button in buttons] == ["Убрать"], "у MAX «Открыть чат» нет"
    assert bot.max.sent == [(OWNER_MAX, "Принял, итог пришлю в Telegram.")], "в MAX — одно «Принял»"
    [(_, prompt)] = call.calls
    assert "Переписка в MAX, чат «Игорь Петров»." in prompt
    assert "Владелец: Да, в пятницу пришлю" in prompt


async def test_group_deal_is_recorded_and_no_unanswered_reminder_comes() -> None:
    """Приёмка 3: дело из группы записывается так же, а «Вы не ответили» по
    MAX не приходит."""
    bot = MaxBot(store=consented())
    bot.max.say(in_group("Кто купит уголь? Ответь до вечера", mid="mid.g1"))
    await bot.service.poll()
    deal = make_deal(title="Олег привезёт доски", promise="to_me", people=["Олег"])
    call = FakeChatCall(
        make_answer(
            deals=[deal],
            waiting="он спрашивал, кто купит уголь",
            with_whom="группой «Дача»",
            to_whom="группе «Дача»",
        )
    )
    await analyzing_service(bot.store, call).analyze_due()
    reports = analyzing_service(bot.store, sender=bot.sender)

    await reports.send_reports(QUIET_LATER)
    reminded = await reports.remind_waiting(NOW + timedelta(hours=4))

    assert reminded == 0
    assert bot.sender.texts == [
        "Из переписки с группой «Дача» (MAX) записал: Олег привезёт доски — пятница, "
        "9 октября (обещали вам)"
    ]
    [(_, prompt)] = call.calls
    assert "Переписка в MAX, группа «Дача»." in prompt


async def test_forwarded_and_note_chats_never_wait_for_an_answer() -> None:
    """Приёмка 3: ни пересланное, ни заметки «ждёт ответа» не заводят."""
    bot = MaxBot(store=consented())
    bot.max.say(
        from_owner(None, mid="mid.1", link=forward("Во сколько созвон?")),
        from_owner("Спросить Олега, во сколько созвон", mid="mid.2"),
    )
    await bot.service.poll()
    call = FakeChatCall(make_answer(deals=[], waiting="он спрашивал, во сколько созвон"))
    await analyzing_service(bot.store, call).analyze_due()

    reminded = await analyzing_service(bot.store, sender=bot.sender).remind_waiting(
        NOW + timedelta(hours=4)
    )

    assert reminded == 0
    assert len(call.calls) == 2
    assert all(thread["waiting_since"] is None for thread in bot.store.threads.values())


async def test_note_deal_is_reported_as_from_your_notes() -> None:
    bot = MaxBot(store=consented())
    bot.max.say(from_owner("Позвонить Олегу в пятницу"))
    await bot.service.poll()
    deal = make_deal(title="позвонить Олегу", people=["Олег"])
    call = FakeChatCall(make_answer(deals=[deal], with_whom="", to_whom=""))
    await analyzing_service(bot.store, call).analyze_due()

    await analyzing_service(bot.store, sender=bot.sender).send_reports(QUIET_LATER)

    assert bot.sender.texts == [
        "Из ваших заметок в MAX записал: позвонить Олегу — пятница, 9 октября (вы обещали)"
    ]
    [(_, prompt)] = call.calls
    assert prompt.startswith("Заметки владельца самому себе в MAX.")


# ------------------------------------------- «Принял» в MAX — раз на пачку (§27.3)

ACCEPTED = (OWNER_MAX, "Принял, итог пришлю в Telegram.")


async def test_first_batch_after_silence_gets_one_accepted() -> None:
    bot = MaxBot(store=consented())
    bot.max.say(
        from_owner(None, mid="mid.1", link=forward("Пришлёшь расчёт до пятницы?")),
        from_owner(None, mid="mid.2", link=forward("Да, в пятницу пришлю", sender=OWNER)),
    )
    await bot.service.poll()
    bot.now = NOW + timedelta(minutes=5)
    bot.max.say(from_owner("И ещё: напомни Олегу про книгу", mid="mid.3"))
    await bot.service.poll()
    bot.now = NOW + timedelta(minutes=24)
    bot.max.say(from_owner("Купить уголь", mid="mid.4"))
    await bot.service.poll()

    bot.now = NOW + timedelta(minutes=45)
    bot.max.say(from_owner("Новая пачка", mid="mid.5"))
    await bot.service.poll()

    assert bot.max.sent == [ACCEPTED, ACCEPTED], "второе — после 20 минут тишины"


async def test_nothing_kept_nothing_accepted() -> None:
    """«Принял» — только о записанном (инвариант 4): до согласия, повтор,
    группа и чужой — без ответа в MAX."""
    bot = MaxBot()
    bot.max.say(from_owner("До согласия", mid="mid.1"))
    await bot.service.poll()
    await bot.chats.answer_consent("max", True)
    bot.max.say(in_group("Кто купит уголь?"))
    bot.max.say(update(raw_message("Привет, бот", sender=OLEG, chat_id=STRANGER_DIALOG)))
    await bot.service.poll()
    await bot.service.poll()
    assert bot.max.sent == []

    bot.max.say(from_owner("Записать", mid="mid.2"))
    await bot.service.poll()
    bot.now = NOW + timedelta(hours=1)
    bot.max.say(from_owner("Записать", mid="mid.2"))
    await bot.service.poll()

    assert bot.max.sent == [ACCEPTED], "повтор того же сообщения — без нового «Принял»"


async def test_unsent_accepted_is_sent_by_the_next_poll() -> None:
    bot = MaxBot(store=consented())
    bot.max.send_errors.append(MaxError("сеть: ConnectError"))
    bot.max.say(from_owner("Раз", mid="mid.1"))

    await bot.service.poll()
    assert bot.max.sent == []
    await bot.service.poll()

    assert bot.max.sent == [ACCEPTED]


async def test_accepted_is_dropped_when_the_bot_is_stopped() -> None:
    bot = MaxBot(store=consented())
    bot.max.send_errors.append(MaxError("MAX ответил 403, код chat.denied", 403))
    bot.max.say(from_owner("Раз", mid="mid.1"))

    await bot.service.poll()
    await bot.service.poll()

    assert bot.max.sent == []


def test_max_consent_question_says_what_is_read_and_to_forward_again() -> None:
    question = texts.consent_question("max")

    assert question == (
        "Подключено чтение MAX: то, что вы пересылаете и пишете мне в MAX, и группы, куда "
        "вы меня добавили. Я читаю эти сообщения и отправляю текст и голосовые на разбор "
        "(Claude через посредника, Deepgram). Храню переписку 7 дней, записанные дела — "
        "пока не уберёте. В MAX я отвечаю только «Принял» и в группы не пишу. Пересланное "
        "до согласия я не сохранил — после «Согласен» перешлите его снова. Согласны?"
    )


def test_max_consent_answer_promises_no_unanswered_reminders() -> None:
    agreed = texts.consent_answered("max", agreed=True)

    assert agreed.endswith(
        "Договорились. Что записал из MAX — буду писать сюда; «кому вы не ответили» по MAX не веду."
    )
    assert "кому вы не ответили — буду" in texts.consent_answered("telegram", agreed=True)


def test_help_mentions_the_max_bot() -> None:
    assert "MAX" in texts.HELP
    assert "README, раздел «MAX»" in texts.HELP


@pytest.mark.live
async def test_live_max_answers_through_the_repository_certificate() -> None:
    """Вживую (§27.2): `GET /me` и `GET /updates` тем же клиентом, что опрос, —
    TLS проверяется сертификатом из репозитория, токен уходит заголовком.
    Только чтение: события читаются без отметки и не подтверждаются, в MAX
    ничего не пишется. В вывод — числа: кто писал боту в личку — по id, так
    владелец узнаёт свой `OWNER_MAX_ID` (README, раздел «MAX»). Нужен
    `MAX_BOT_TOKEN` в `.env`: `pytest -m live -k max -s`."""
    settings = live_settings()
    if settings.max_bot_token is None:
        pytest.skip("Живой прогон невозможен: в .env нет MAX_BOT_TOKEN")
    api = HttpMaxApi.create(settings.max_bot_token)
    try:
        bot = await api.me()
        batch = await api.updates(None, wait=0)
    finally:
        await api.close()
    writers = sorted(
        {
            item.message.sender.user_id
            for item in batch.updates
            if item.message is not None
            and item.message.sender is not None
            and item.message.chat_type == "dialog"
        }
    )
    print(
        f"MAX отвечает через сертификат из репозитория: id бота есть {bool(bot.user_id)}, "
        f"событий {len(batch.updates)}, в личку бота писали id: {writers or 'никто'}"
    )
