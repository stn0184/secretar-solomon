"""Instagram Direct (`techspec/26-instagram.md`): ключ, разбор ответов Meta,
опрос и шаг тика.

Сети нет: API Instagram подменён — чистые функции получают ответы Meta
словарями, клиент ходит в `httpx.MockTransport`, сервис — в подменённый API.
Переписки выдуманные — Игорь и Олег (@oleg); ключи — игрушечные строки.
"""

from __future__ import annotations

import json
import logging
import sys
from collections import Counter
from collections.abc import Callable
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import httpx
import pytest
from aiogram import Bot

from solomon import texts
from solomon.cli import LOG_FORMAT, HidingFormatter, secrets_of
from solomon.config import Settings
from solomon.runner import build_instagram
from solomon.services import instagram
from solomon.services.chats import ChatService
from solomon.services.instagram import (
    AccessDenied,
    Account,
    Attachment,
    Conversation,
    DirectMessage,
    HttpInstagramApi,
    InstagramError,
    InstagramService,
    KeyRejected,
    KeyState,
    StateFile,
)
from solomon.services.reminders import ReminderService
from solomon.services.transcription import Transcript
from tests.conftest import OWNER_TIMEZONE, FakeTranscriber, make_settings
from tests.test_chats import (
    QUIET_LATER,
    FakeChatCall,
    FakeChatStore,
    FakeChatTicker,
    FakeLookup,
    FakeSender,
    analyzing_service,
    make_answer,
    make_deal,
)
from tests.test_reminders import (
    SATURDAY_NOON,
    FakeAnnouncer,
    FakeClearMoved,
    FakeCloser,
    FakeDue,
    FakeMarks,
    FakeMoved,
    FakeNotifier,
)
from tests.test_understanding import live_settings

TZ = ZoneInfo(OWNER_TIMEZONE)
NOW = datetime(2026, 10, 7, 12, 0, tzinfo=TZ)
ENV_KEY = "IGAA-env-key-for-tests"
FRESH_KEY = "IGAA-refreshed-key-for-tests"
OTHER_KEY = "IGAA-another-key-for-tests"
BUSINESS = Account(user_id="17841400000000001", username="tim.shop")
OLEG_ID = "5531000000000042"
CONVERSATION = "aWdfZAG06MTpJR01lc3NhZA2VUaHJlYWQ6oleg"


def meta_time(moment: datetime) -> str:
    """Время, как его пишет Meta: `2026-10-07T07:00:00+0000`."""
    return moment.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S+0000")


def raw_message(
    text: str = "Пришлёшь расчёт до пятницы?",
    *,
    message_id: str = "m_1",
    at: datetime = NOW - timedelta(minutes=30),
    from_owner: bool = False,
    **extra: Any,
) -> dict[str, Any]:
    """Сообщение Direct, как его отдаёт Conversations API."""
    oleg = {"username": "oleg", "id": OLEG_ID}
    business = {"username": BUSINESS.username, "id": BUSINESS.user_id}
    raw: dict[str, Any] = {
        "id": message_id,
        "created_time": meta_time(at),
        "from": business if from_owner else oleg,
        "to": {"data": [oleg if from_owner else business]},
        "message": text,
    }
    raw.update(extra)
    return raw


def direct(
    text: str = "Пришлёшь расчёт до пятницы?",
    *,
    message_id: str = "m_1",
    at: datetime = NOW - timedelta(minutes=30),
    from_owner: bool = False,
    attachment: Attachment | None = None,
    story: bool = False,
    recipients: int = 1,
) -> DirectMessage:
    sender = (BUSINESS.user_id, BUSINESS.username) if from_owner else (OLEG_ID, "oleg")
    other = (OLEG_ID, "oleg") if from_owner else (BUSINESS.user_id, BUSINESS.username)
    return DirectMessage(
        id=message_id,
        sent_at=at,
        sender_id=sender[0],
        sender_name=sender[1],
        recipients=tuple([other] * recipients),
        text=text,
        attachment=attachment,
        story=story,
    )


# ------------------------------------------------------------ время и ошибки


def test_meta_time_is_read_in_both_forms() -> None:
    assert instagram.parse_time("2026-10-07T07:00:00+0000") == datetime(
        2026, 10, 7, 7, 0, tzinfo=UTC
    )
    assert instagram.parse_time(1791356400) == datetime(2026, 10, 7, 7, 0, tzinfo=UTC)
    assert instagram.parse_time("1791356400") == datetime(2026, 10, 7, 7, 0, tzinfo=UTC)
    assert instagram.parse_time("вчера") is None
    assert instagram.parse_time(None) is None


def test_expired_or_revoked_key_is_a_rejection() -> None:
    expired = {"error": {"message": "Session has expired", "type": "OAuthException", "code": 190}}

    error = instagram.meta_error(400, expired)

    assert isinstance(error, KeyRejected)
    assert "190" in error.reason


def test_missing_access_to_direct_is_not_a_rejection() -> None:
    """Код 200 — выключен доступ к сообщениям в Instagram или у ключа нет
    права: новый ключ может и не понадобиться, опрос не останавливается."""
    denied = {"error": {"message": "disabled access", "type": "OAuthException", "code": 200}}

    assert type(instagram.meta_error(403, denied)) is AccessDenied
    assert type(instagram.meta_error(400, {"error": {"code": 10}})) is AccessDenied
    assert type(instagram.meta_error(400, {"error": {"code": 102}})) is KeyRejected


def test_limits_and_server_errors_are_only_retried() -> None:
    limited = instagram.meta_error(400, {"error": {"code": 4, "message": "limit"}})
    broken = instagram.meta_error(502, "<html>Bad gateway</html>")

    assert type(limited) is InstagramError
    assert type(broken) is InstagramError
    assert "502" in broken.reason


def test_error_reason_carries_no_text_from_meta() -> None:
    """Текст ошибки Meta — не в журнал: в нём бывает что угодно (§26.3)."""
    error = instagram.meta_error(
        400, {"error": {"code": 100, "message": "Invalid parameter IGAA-env-key-for-tests"}}
    )

    assert ENV_KEY not in error.reason
    assert "100" in error.reason


# ------------------------------------------------------------ ответы Meta


def test_account_is_read_flat_or_inside_data() -> None:
    flat = {"user_id": BUSINESS.user_id, "username": "tim.shop", "id": "app-scoped"}
    wrapped = {"data": [{"user_id": BUSINESS.user_id, "username": "tim.shop"}]}

    assert instagram.parse_account(flat) == BUSINESS
    assert instagram.parse_account(wrapped) == BUSINESS


def test_account_without_an_id_is_an_error() -> None:
    try:
        instagram.parse_account({"username": "tim.shop"})
    except InstagramError:
        return
    raise AssertionError("ответ без id бизнес-аккаунта прошёл")


def test_conversations_and_the_next_page_are_read() -> None:
    payload = {
        "data": [
            {"id": "c1", "updated_time": meta_time(NOW)},
            {"id": "c2", "updated_time": "не время"},
            {"updated_time": meta_time(NOW)},
        ],
        "paging": {"cursors": {"after": "QVFI"}, "next": "https://graph.instagram.com/..."},
    }

    found, after = instagram.parse_conversations(payload)

    assert found == [instagram.Conversation(id="c1", updated_at=NOW)]
    assert after == "QVFI"


def test_last_page_has_no_cursor() -> None:
    payload = {"data": [], "paging": {"cursors": {"after": "QVFI"}}}

    assert instagram.parse_conversations(payload) == ([], None)


def test_messages_are_read_with_sender_recipients_and_text() -> None:
    payload = {
        "id": CONVERSATION,
        "messages": {
            "data": [
                raw_message("В пятницу пришлю", message_id="m_2", from_owner=True),
                raw_message(),
                {"id": "m_broken"},
            ]
        },
    }

    found = instagram.parse_messages(payload)

    assert found == [
        direct("В пятницу пришлю", message_id="m_2", from_owner=True),
        direct(),
    ]


def test_story_reply_is_marked() -> None:
    payload = {"messages": {"data": [raw_message("Огонь!", story={"reply_to": {"id": "s1"}})]}}

    [message] = instagram.parse_messages(payload)

    assert message.story is True


def test_voice_attachment_is_found_by_its_type() -> None:
    audio = {"mime_type": "audio/mpeg", "name": "audioclip-1.mp4", "file_url": "https://cdn/a"}
    clip = {"name": "audioclip-17.mp4", "video_data": {"url": "https://cdn/v"}}
    sound = {"audio_data": {"url": "https://cdn/s"}}

    assert instagram.attachment_of(audio) == Attachment(kind="voice", url="https://cdn/a")
    assert instagram.attachment_of(clip) == Attachment(kind="voice", url="https://cdn/v")
    assert instagram.attachment_of(sound) == Attachment(kind="voice", url="https://cdn/s")


def test_photo_and_other_attachments() -> None:
    photo = {"image_data": {"url": "https://cdn/p", "width": 1080}}
    jpeg = {"mime_type": "image/jpeg", "file_url": "https://cdn/j"}
    video = {"video_data": {"url": "https://cdn/v", "width": 720}}
    pdf = {"mime_type": "application/pdf", "file_url": "https://cdn/d"}

    assert instagram.attachment_of(photo).kind == "photo"
    assert instagram.attachment_of(jpeg).kind == "photo"
    assert instagram.attachment_of(video) == Attachment(kind="other", url=None)
    assert instagram.attachment_of(pdf) == Attachment(kind="other", url=None)


def test_message_with_an_attachment_keeps_the_first_one() -> None:
    raw = raw_message(
        "",
        attachments={
            "data": [
                {"mime_type": "audio/aac", "file_url": "https://cdn/voice"},
                {"image_data": {"url": "https://cdn/p"}},
            ]
        },
    )

    [message] = instagram.parse_messages({"messages": {"data": [raw]}})

    assert message.attachment == Attachment(kind="voice", url="https://cdn/voice")


# ---------------------------------------------------- сообщение в общий путь


def test_message_of_the_interlocutor_is_in_with_his_username() -> None:
    incoming = instagram.to_incoming(direct(), CONVERSATION, BUSINESS)

    assert incoming is not None
    assert incoming.platform == "instagram"
    assert incoming.connection_id == BUSINESS.user_id
    assert incoming.chat_key == CONVERSATION
    assert incoming.chat_name == "@oleg"
    assert (incoming.direction, incoming.sender) == ("in", "@oleg")
    assert (incoming.kind, incoming.text) == ("text", "Пришлёшь расчёт до пятницы?")
    assert incoming.external_id == "m_1"
    assert incoming.sent_at == NOW - timedelta(minutes=30)
    assert incoming.tracks_waiting is True
    assert incoming.username == "oleg", "по нему «Открыть чат» ведёт в Direct"


def test_message_of_the_business_account_is_out() -> None:
    incoming = instagram.to_incoming(direct("Пришлю", from_owner=True), CONVERSATION, BUSINESS)

    assert incoming is not None
    assert incoming.direction == "out"
    assert incoming.chat_name == "@oleg"
    assert incoming.username == "oleg"


def test_message_without_the_username_of_the_interlocutor_keeps_the_known_one() -> None:
    """Meta не назвала имя — `None`: площадка его не знает, в чате остаётся
    прежнее (§3.15), а не стирается."""
    message = replace(direct(), sender_name="")

    incoming = instagram.to_incoming(message, CONVERSATION, BUSINESS)

    assert incoming is not None
    assert incoming.username is None


def test_owner_is_recognised_by_username_if_the_id_differs() -> None:
    """Id в сообщении и в `/me` могут оказаться разного вида — имя тоже говорит."""
    other_id = Account(user_id="app-scoped-1", username=BUSINESS.username)

    incoming = instagram.to_incoming(direct("Пришлю", from_owner=True), CONVERSATION, other_id)

    assert incoming is not None
    assert incoming.direction == "out"


def test_voice_photo_and_empty_messages_get_their_kind() -> None:
    voice = direct("", attachment=Attachment(kind="voice", url="https://cdn/voice"))
    photo = direct("смотри", attachment=Attachment(kind="photo", url="https://cdn/p"))
    share = direct("")

    kinds = [instagram.to_incoming(message, CONVERSATION, BUSINESS) for message in (voice, photo)]
    empty = instagram.to_incoming(share, CONVERSATION, BUSINESS)

    assert [(item.kind, item.text) for item in kinds if item is not None] == [
        ("voice", ""),
        ("photo", "смотри"),
    ]
    assert empty is not None
    assert (empty.kind, empty.text) == ("other", "")


def test_story_replies_and_group_messages_are_not_read() -> None:
    """Ответы на истории и групповые чаты Direct — не читаются (§26.4)."""
    assert instagram.to_incoming(direct(story=True), CONVERSATION, BUSINESS) is None
    assert instagram.to_incoming(direct(recipients=2), CONVERSATION, BUSINESS) is None


def test_fresh_messages_are_newer_than_the_horizon_oldest_first() -> None:
    horizon = NOW - timedelta(hours=1)
    old = direct(message_id="old", at=horizon - timedelta(minutes=1))
    first = direct(message_id="first", at=horizon + timedelta(minutes=1))
    second = direct(message_id="second", at=horizon + timedelta(minutes=2))

    found, overflow = instagram.fresh([second, old, first], horizon)

    assert [message.id for message in found] == ["first", "second"]
    assert overflow is False


def test_twenty_fresh_messages_may_hide_older_ones() -> None:
    """API отдаёт подробности 20 последних: все 20 новые — старшие могли не
    войти, это переполнение (§26.3)."""
    horizon = NOW - timedelta(hours=1)
    twenty = [
        direct(message_id=str(index), at=horizon + timedelta(minutes=index + 1))
        for index in range(instagram.MESSAGES_LIMIT)
    ]

    found, overflow = instagram.fresh(twenty, horizon)

    assert len(found) == instagram.MESSAGES_LIMIT
    assert overflow is True


# ------------------------------------------------------------- ключ и файл


def test_fingerprint_does_not_reveal_the_key() -> None:
    mark = instagram.fingerprint(ENV_KEY)

    assert ENV_KEY not in mark
    assert mark == instagram.fingerprint(ENV_KEY)
    assert mark != instagram.fingerprint(OTHER_KEY)


def test_state_starts_from_the_env_key() -> None:
    state = instagram.start_state(ENV_KEY, None)

    assert state == KeyState(
        source=instagram.fingerprint(ENV_KEY),
        token=ENV_KEY,
        expires_at=None,
        rejected_at=None,
        reported_at=None,
        polled_until=None,
    )


def test_saved_state_of_the_same_env_key_is_kept() -> None:
    saved = KeyState(
        source=instagram.fingerprint(ENV_KEY),
        token=FRESH_KEY,
        expires_at=NOW + timedelta(days=50),
        rejected_at=None,
        reported_at=None,
        polled_until=NOW - timedelta(minutes=5),
    )

    assert instagram.start_state(ENV_KEY, saved) == saved


def test_new_env_key_wins_over_the_saved_state() -> None:
    """Главнее ключ из `.env` (§26.2): владелец заменил ключ и перезапустил —
    файл начинается заново, отметка «отвергнут» уходит вместе с ним."""
    saved = KeyState(
        source=instagram.fingerprint(OTHER_KEY),
        token=FRESH_KEY,
        expires_at=NOW + timedelta(days=50),
        rejected_at=NOW - timedelta(days=1),
        reported_at=NOW - timedelta(days=1),
        polled_until=NOW - timedelta(days=1),
    )

    assert instagram.start_state(ENV_KEY, saved) == instagram.start_state(ENV_KEY, None)


def test_key_is_refreshed_ten_days_before_the_end_or_when_the_end_is_unknown() -> None:
    state = instagram.start_state(ENV_KEY, None)

    assert instagram.refresh_due(state, NOW) is True, "срок неизвестен — продлить"
    soon = _with(state, expires_at=NOW + timedelta(days=9))
    later = _with(state, expires_at=NOW + timedelta(days=11))
    assert instagram.refresh_due(soon, NOW) is True
    assert instagram.refresh_due(later, NOW) is False


def _with(state: KeyState, **changes: Any) -> KeyState:
    return replace(state, **changes)


def test_state_survives_the_file_round_trip() -> None:
    state = _with(
        instagram.start_state(ENV_KEY, None),
        token=FRESH_KEY,
        expires_at=NOW + timedelta(days=60),
        polled_until=NOW,
    )

    assert instagram.state_from_json(instagram.state_to_json(state)) == state


def test_broken_state_is_no_state() -> None:
    assert instagram.state_from_json({"token": FRESH_KEY}) is None
    assert instagram.state_from_json(["не словарь"]) is None
    assert instagram.state_from_json({"source": "x", "token": "", "expires_at": None}) is None


def test_horizon_is_the_cursor_with_an_overlap_or_the_last_day() -> None:
    state = instagram.start_state(ENV_KEY, None)

    assert instagram.horizon_of(state, NOW) == NOW - instagram.LOOKBACK
    polled = _with(state, polled_until=NOW - timedelta(minutes=5))
    assert instagram.horizon_of(polled, NOW) == NOW - timedelta(minutes=5) - instagram.OVERLAP


# ------------------------------------------------------------ файл состояния


def test_state_file_round_trip_creates_its_folder(tmp_path: Path) -> None:
    file = StateFile(tmp_path / "state" / "instagram.json")
    state = _with(instagram.start_state(ENV_KEY, None), polled_until=NOW)

    assert file.load() is None
    assert file.save(state) is True
    assert file.load() == state


@pytest.mark.skipif(sys.platform == "win32", reason="права файлов — на сервере (Linux)")
def test_state_file_is_readable_only_by_the_bot(tmp_path: Path) -> None:
    path = tmp_path / "state" / "instagram.json"

    StateFile(path).save(instagram.start_state(ENV_KEY, None))

    assert path.stat().st_mode & 0o777 == 0o600
    assert path.parent.stat().st_mode & 0o777 == 0o700


def test_broken_state_file_starts_over(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    path = tmp_path / "instagram.json"
    path.write_text("{не json", encoding="utf-8")

    assert StateFile(path).load() is None
    assert "испорчен" in caplog.text


def test_unwritable_state_file_is_a_log_line(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    blocker = tmp_path / "state"
    blocker.write_text("файл, а не папка", encoding="utf-8")

    saved = StateFile(blocker / "instagram.json").save(instagram.start_state(ENV_KEY, None))

    assert saved is False
    assert "не записан" in caplog.text
    assert ENV_KEY not in caplog.text


# ------------------------------------------------------- клиент поверх httpx

Handler = Callable[[httpx.Request], httpx.Response]


def mock_api(handler: Handler) -> tuple[HttpInstagramApi, list[httpx.Request]]:
    """Клиент Instagram, чьи запросы уходят в `handler`, а не в сеть."""
    requests: list[httpx.Request] = []

    def record(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return handler(request)

    client = httpx.AsyncClient(transport=httpx.MockTransport(record))
    return HttpInstagramApi(client), requests


def answer(payload: Any, status: int = 200) -> Handler:
    return lambda request: httpx.Response(status, json=payload)


async def test_data_requests_carry_the_key_in_a_header_not_in_the_address() -> None:
    api, requests = mock_api(answer({"user_id": BUSINESS.user_id, "username": "tim.shop"}))

    assert await api.account(ENV_KEY) == BUSINESS

    [request] = requests
    assert request.method == "GET"
    assert request.url.path == "/v25.0/me"
    assert request.url.params["fields"] == "user_id,username"
    assert request.headers["Authorization"] == f"Bearer {ENV_KEY}"
    assert ENV_KEY not in str(request.url)


async def test_conversations_are_asked_for_instagram_with_the_page_cursor() -> None:
    payload = {"data": [{"id": "c1", "updated_time": meta_time(NOW)}]}
    api, requests = mock_api(answer(payload))

    found, after = await api.conversations(ENV_KEY, "QVFI")

    assert (found, after) == ([Conversation(id="c1", updated_at=NOW)], None)
    params = requests[0].url.params
    assert requests[0].url.path == "/v25.0/me/conversations"
    assert (params["platform"], params["fields"], params["after"]) == (
        "instagram",
        "id,updated_time",
        "QVFI",
    )


async def test_messages_are_asked_with_details_of_the_last_twenty() -> None:
    api, requests = mock_api(answer({"id": CONVERSATION, "messages": {"data": [raw_message()]}}))

    assert await api.messages(ENV_KEY, CONVERSATION) == [direct()]

    assert requests[0].url.path == f"/v25.0/{CONVERSATION}"
    assert requests[0].url.params["fields"] == (
        "messages.limit(20){id,created_time,from,to,message,attachments,story}"
    )


async def test_refresh_follows_the_meta_documentation() -> None:
    api, requests = mock_api(
        answer({"access_token": FRESH_KEY, "token_type": "bearer", "expires_in": 5183944})
    )

    assert await api.refresh(ENV_KEY) == (FRESH_KEY, 5183944)

    [request] = requests
    assert (request.method, request.url.path) == ("GET", "/refresh_access_token")
    assert request.url.params["grant_type"] == "ig_refresh_token"
    assert request.url.params["access_token"] == ENV_KEY


async def test_rejected_key_through_http_is_a_rejection() -> None:
    expired = {"error": {"message": "Session has expired", "type": "OAuthException", "code": 190}}
    api, _ = mock_api(answer(expired, status=400))

    with pytest.raises(KeyRejected):
        await api.account(ENV_KEY)


async def test_server_error_and_network_failure_are_plain_errors() -> None:
    broken, _ = mock_api(lambda request: httpx.Response(502, text="<html>Bad gateway</html>"))

    def offline(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError(f"cannot connect to {request.url}", request=request)

    unreachable, _ = mock_api(offline)

    with pytest.raises(InstagramError) as server:
        await broken.account(ENV_KEY)
    with pytest.raises(InstagramError) as network:
        await unreachable.refresh(ENV_KEY)

    assert type(server.value) is InstagramError
    assert network.value.reason == "сеть: ConnectError"
    assert network.value.__suppress_context__ is True, "адрес с ключом не уходит в трассировку"
    assert ENV_KEY not in str(network.value)


async def test_voice_is_downloaded_without_the_key(monkeypatch: pytest.MonkeyPatch) -> None:
    api, requests = mock_api(lambda request: httpx.Response(200, content=b"voice-bytes"))

    loaded = await api.download("https://lookaside.example/ig_messaging_cdn/?asset_id=1")

    assert loaded == b"voice-bytes"
    assert "Authorization" not in requests[0].headers
    monkeypatch.setattr(instagram, "AUDIO_LIMIT", 4)
    with pytest.raises(InstagramError):
        await api.download("https://lookaside.example/ig_messaging_cdn/?asset_id=2")


async def test_voice_link_is_followed_to_the_file() -> None:
    """Ссылка `lookaside` у Meta переадресует на сам файл."""

    def cdn(request: httpx.Request) -> httpx.Response:
        if request.url.host == "lookaside.example":
            return httpx.Response(302, headers={"Location": "https://cdn.example/voice.mp4"})
        return httpx.Response(200, content=b"voice-bytes")

    api, requests = mock_api(cdn)

    assert await api.download("https://lookaside.example/ig_messaging_cdn/?asset_id=1") == (
        b"voice-bytes"
    )
    assert [request.url.host for request in requests] == ["lookaside.example", "cdn.example"]


# ----------------------------------------------------------- подменённый Meta


class FakeMeta:
    """Instagram API в памяти: разговоры, сообщения и файлы; что и с каким
    ключом спрашивали; сбои — очередью по имени метода."""

    def __init__(self) -> None:
        self.account_of = BUSINESS
        self.threads: dict[str, list[dict[str, Any]]] = {}
        self.files: dict[str, bytes] = {}
        self.calls: list[tuple[str, str]] = []
        self.failures: dict[str, list[InstagramError]] = {}
        self.refreshed = (FRESH_KEY, 60 * 86400)
        self.closed = False

    def say(self, raw: dict[str, Any], conversation: str = CONVERSATION) -> None:
        self.threads.setdefault(conversation, []).append(raw)

    def _call(self, name: str, argument: str) -> None:
        self.calls.append((name, argument))
        queue = self.failures.get(name)
        if queue:
            raise queue.pop(0)

    def names(self) -> list[str]:
        return [name for name, _ in self.calls]

    def keys(self) -> set[str]:
        return {key for name, key in self.calls if name != "download"}

    async def account(self, token: str) -> Account:
        self._call("account", token)
        return self.account_of

    async def conversations(
        self, token: str, after: str | None
    ) -> tuple[list[Conversation], str | None]:
        self._call("conversations", token)
        found = [
            Conversation(
                id=conversation,
                updated_at=max(_moment(raw["created_time"]) for raw in raws),
            )
            for conversation, raws in self.threads.items()
        ]
        return sorted(found, key=lambda item: item.updated_at, reverse=True), None

    async def messages(self, token: str, conversation_id: str) -> list[DirectMessage]:
        self._call("messages", token)
        raws = self.threads[conversation_id]
        newest = sorted(raws, key=lambda raw: _moment(raw["created_time"]), reverse=True)
        limited = newest[: instagram.MESSAGES_LIMIT]
        return instagram.parse_messages({"messages": {"data": limited}})

    async def refresh(self, token: str) -> tuple[str, int]:
        self._call("refresh", token)
        return self.refreshed

    async def download(self, url: str) -> bytes:
        self._call("download", url)
        return self.files[url]

    async def close(self) -> None:
        self.closed = True


def _moment(value: str) -> datetime:
    moment = instagram.parse_time(value)
    assert moment is not None
    return moment


async def no_sleep(seconds: float) -> None:
    return None


class Direct:
    """Сервис Instagram и общий путь чатов над одной подменённой базой."""

    def __init__(
        self,
        tmp_path: Path,
        *,
        store: FakeChatStore | None = None,
        meta: FakeMeta | None = None,
        sender: FakeSender | None = None,
        token: str = ENV_KEY,
        transcriber: FakeTranscriber | None = None,
        lookup: FakeLookup | None = None,
        api: HttpInstagramApi | None = None,
    ) -> None:
        self.store = store or FakeChatStore()
        self.meta = meta or FakeMeta()
        self.sender = sender or FakeSender()
        self.path = tmp_path / "state" / "instagram.json"
        self.settings = replace(make_settings(), instagram_token=token)
        self.chats = ChatService(
            settings=self.settings,
            store=self.store,
            send=self.sender,
            lookup=lookup,
            transcriber=transcriber,
        )
        self.service = InstagramService(
            settings=self.settings,
            token=token,
            chats=self.chats,
            api=api or self.meta,
            send=self.sender,
            state_file=StateFile(self.path),
            clock=lambda: NOW,
            sleep=no_sleep,
        )

    def state(self) -> KeyState:
        state = StateFile(self.path).load()
        assert state is not None
        return state


def consented(store: FakeChatStore | None = None) -> FakeChatStore:
    """Instagram подключён, владелец уже согласился."""
    store = store or FakeChatStore()
    store.consent("instagram", BUSINESS.user_id)
    return store


# -------------------------------------------- согласие и что читается (§25.5)


async def test_first_poll_asks_consent_and_reads_nothing_before_it(tmp_path: Path) -> None:
    direct_ = Direct(tmp_path)
    direct_.meta.say(raw_message())

    assert await direct_.service.poll(NOW) == 0

    assert direct_.sender.texts == [texts.consent_question("instagram")]
    assert direct_.store.sources["instagram"]["connection_id"] == BUSINESS.user_id
    assert "conversations" not in direct_.meta.names(), "до согласия Direct не читается"
    assert direct_.store.messages == []


async def test_consent_question_is_asked_once_across_restarts(tmp_path: Path) -> None:
    first = Direct(tmp_path)
    await first.service.poll(NOW)
    again = Direct(tmp_path, store=first.store, sender=first.sender)

    await again.service.poll(NOW + timedelta(minutes=5))

    assert first.sender.texts == [texts.consent_question("instagram")]


async def test_after_consent_the_poll_reads_the_last_day(tmp_path: Path) -> None:
    """До «Согласен» сообщения не хранились — после него опрос дочитывает
    последние сутки (§25.5)."""
    direct_ = Direct(tmp_path)
    await direct_.service.poll(NOW)
    direct_.meta.say(raw_message("Позавчерашнее", message_id="old", at=NOW - timedelta(hours=30)))
    direct_.meta.say(raw_message(message_id="m_1", at=NOW - timedelta(hours=2)))
    await direct_.chats.answer_consent("instagram", True)

    assert await direct_.service.poll(NOW + timedelta(minutes=5)) == 1

    [stored] = direct_.store.messages
    assert (stored["external_id"], stored["direction"], stored["sender"]) == ("m_1", "in", "@oleg")
    assert direct_.store.thread(CONVERSATION, "instagram")["name"] == "@oleg"


async def test_declined_consent_reads_nothing_and_forgets_the_cursor(tmp_path: Path) -> None:
    direct_ = Direct(tmp_path, store=consented())
    await direct_.service.poll(NOW)
    assert direct_.state().polled_until is not None
    await direct_.chats.answer_consent("instagram", False)
    direct_.meta.say(raw_message())

    assert await direct_.service.poll(NOW + timedelta(minutes=5)) == 0

    assert direct_.store.messages == []
    assert direct_.state().polled_until is None, "после нового согласия — снова последние сутки"


async def test_instagram_never_asks_telegram_about_its_connection(tmp_path: Path) -> None:
    lookup = FakeLookup({BUSINESS.user_id: 777})
    direct_ = Direct(tmp_path, lookup=lookup)
    incoming = instagram.to_incoming(direct(), CONVERSATION, BUSINESS)
    assert incoming is not None

    stored = await direct_.chats.receive(incoming)

    assert stored is not None and stored.outcome == "no_source"
    assert lookup.calls == []


# ------------------------------------ приёмка 2: разбор и что видит владелец


async def test_direct_deal_reaches_the_owner_marked_instagram(tmp_path: Path) -> None:
    direct_ = Direct(tmp_path, store=consented())
    direct_.meta.say(raw_message("Пришлёшь расчёт до пятницы?", message_id="m_1"))
    direct_.meta.say(
        raw_message(
            "Да, в пятницу пришлю",
            message_id="m_2",
            at=NOW - timedelta(minutes=29),
            from_owner=True,
        )
    )

    assert await direct_.service.poll(NOW) == 2
    deal = make_deal(title="прислать Олегу расчёт", people=["Олег"])
    call = FakeChatCall(make_answer(deals=[deal], with_whom="@oleg", to_whom="@oleg"))
    await analyzing_service(direct_.store, call).analyze_due()
    reports = analyzing_service(direct_.store, sender=direct_.sender)

    assert await reports.send_reports(QUIET_LATER) == 1

    assert direct_.sender.texts == [
        "Из переписки с @oleg (Instagram) записал: "
        "прислать Олегу расчёт — пятница, 9 октября (вы обещали)"
    ]
    assert [message["direction"] for message in direct_.store.messages] == ["in", "out"]
    [(_, text)] = call.calls
    assert "Переписка в Instagram, чат «@oleg»." in text


async def test_unanswered_direct_question_is_reminded_with_instagram(tmp_path: Path) -> None:
    direct_ = Direct(tmp_path, store=consented())
    direct_.meta.say(raw_message("Во сколько созвон?", message_id="m_1"))
    await direct_.service.poll(NOW)
    call = FakeChatCall(
        make_answer(deals=[], waiting="он спрашивал, во сколько созвон", to_whom="@oleg")
    )
    await analyzing_service(direct_.store, call).analyze_due()

    reminded = await analyzing_service(direct_.store, sender=direct_.sender).remind_waiting(
        NOW + timedelta(hours=3)
    )

    assert reminded == 1
    assert direct_.sender.texts == [
        "Вы не ответили @oleg (Instagram) — он спрашивал, во сколько созвон."
    ]


async def test_tick_polls_every_five_minutes_in_the_background(tmp_path: Path) -> None:
    """Приёмка 2: новое сообщение Direct попадает в общий путь не позже чем
    через 5 минут; опрос — в фоне, тик его не ждёт."""
    direct_ = Direct(tmp_path, store=consented())

    assert await direct_.service.tick(NOW) == 0
    await direct_.service.wait()
    direct_.meta.say(raw_message(message_id="m_1", at=NOW + timedelta(minutes=1)))
    await direct_.service.tick(NOW + timedelta(minutes=4))
    await direct_.service.wait()
    assert direct_.meta.names().count("conversations") == 1
    assert direct_.store.messages == []

    await direct_.service.tick(NOW + timedelta(minutes=5))
    await direct_.service.wait()

    assert direct_.meta.names().count("conversations") == 2
    assert [message["external_id"] for message in direct_.store.messages] == ["m_1"]


async def test_cursor_skips_conversations_read_before(tmp_path: Path) -> None:
    direct_ = Direct(tmp_path, store=consented())
    direct_.meta.say(raw_message(message_id="m_1", at=NOW - timedelta(hours=1)))
    await direct_.service.poll(NOW)

    await direct_.service.poll(NOW + timedelta(minutes=30))

    assert direct_.meta.names().count("messages") == 1
    assert direct_.state().polled_until == NOW + timedelta(minutes=30)


async def test_voice_is_downloaded_and_transcribed(tmp_path: Path) -> None:
    url = "https://lookaside.example/ig_messaging_cdn/?asset_id=7&signature=x"
    transcriber = FakeTranscriber(Transcript(text="Перезвоню завтра в десять", confidence=0.9))
    direct_ = Direct(tmp_path, store=consented(), transcriber=transcriber)
    direct_.meta.files[url] = b"voice-bytes"
    voice = {"data": [{"mime_type": "audio/mpeg", "file_url": url}]}
    direct_.meta.say(raw_message("", attachments=voice))

    assert await direct_.service.poll(NOW) == 1

    [stored] = direct_.store.messages
    assert (stored["kind"], stored["text"]) == ("voice", "Перезвоню завтра в десять")
    assert transcriber.calls == [b"voice-bytes"]
    assert transcriber.names == [("oleg",)]


async def test_photo_is_kept_and_story_reply_is_not_read(tmp_path: Path) -> None:
    direct_ = Direct(tmp_path, store=consented())
    photo = {"data": [{"image_data": {"url": "https://cdn.example/p"}}]}
    direct_.meta.say(raw_message("смотри", message_id="p", attachments=photo))
    direct_.meta.say(raw_message("Огонь!", message_id="s", story={"reply_to": {"id": "st"}}))

    assert await direct_.service.poll(NOW) == 1

    assert [(row["external_id"], row["kind"]) for row in direct_.store.messages] == [("p", "photo")]


async def test_more_than_twenty_new_messages_is_an_overflow_in_the_log(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    direct_ = Direct(tmp_path, store=consented())
    for index in range(instagram.MESSAGES_LIMIT + 1):
        at = NOW - timedelta(minutes=index + 1)
        direct_.meta.say(raw_message(f"№{index}", message_id=f"m_{index}", at=at))

    assert await direct_.service.poll(NOW) == instagram.MESSAGES_LIMIT

    assert "больше 20 новых сообщений" in caplog.text


# ------------------------------------------------------------ приёмка 3: ключ


async def test_key_is_refreshed_ten_days_before_the_end_and_used_after(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO)
    direct_ = Direct(tmp_path, store=consented())
    StateFile(direct_.path).save(
        _with(instagram.start_state(ENV_KEY, None), expires_at=NOW + timedelta(days=9))
    )
    direct_.meta.say(raw_message())

    assert await direct_.service.poll(NOW) == 1

    assert direct_.meta.calls[0] == ("refresh", ENV_KEY)
    assert {key for _, key in direct_.meta.calls[1:]} == {FRESH_KEY}
    state = direct_.state()
    assert (state.token, state.expires_at) == (FRESH_KEY, NOW + timedelta(days=60))
    assert state.source == instagram.fingerprint(ENV_KEY)
    for key in (ENV_KEY, FRESH_KEY):
        assert key not in caplog.text, "ключ не в журнале"
        assert key not in repr(vars(direct_.store)), "ключ не в базе"


async def test_key_far_from_the_end_is_not_refreshed(tmp_path: Path) -> None:
    direct_ = Direct(tmp_path, store=consented())
    StateFile(direct_.path).save(
        _with(instagram.start_state(ENV_KEY, None), expires_at=NOW + timedelta(days=11))
    )

    await direct_.service.poll(NOW)

    assert "refresh" not in direct_.meta.names()
    assert direct_.meta.keys() == {ENV_KEY}


async def test_failed_refresh_keeps_the_key_and_retries_hours_later(tmp_path: Path) -> None:
    """Ключ моложе суток Meta не продлевает — опрос идёт с прежним, попытка
    повторится через 6 часов, а не каждые 5 минут."""
    direct_ = Direct(tmp_path, store=consented())
    direct_.meta.failures["refresh"] = [InstagramError("Meta ответила 400, код 100")]

    await direct_.service.poll(NOW)
    await direct_.service.poll(NOW + timedelta(minutes=5))
    assert direct_.meta.names().count("refresh") == 1
    assert direct_.meta.keys() == {ENV_KEY}

    await direct_.service.poll(NOW + instagram.REFRESH_RETRY)

    assert direct_.meta.names().count("refresh") == 2
    assert direct_.state().token == FRESH_KEY


async def test_new_env_key_replaces_the_state_file(tmp_path: Path) -> None:
    """Главнее ключ из `.env`: владелец заменил ключ — продлённый старый не
    используется, файл переписан (§26.2)."""
    direct_ = Direct(tmp_path, store=consented())
    StateFile(direct_.path).save(
        _with(
            instagram.start_state(OTHER_KEY, None),
            token=FRESH_KEY,
            expires_at=NOW + timedelta(days=50),
            rejected_at=NOW - timedelta(days=1),
        )
    )
    direct_.meta.refreshed = ("IGAA-refreshed-from-env", 60 * 86400)

    await direct_.service.poll(NOW)

    assert FRESH_KEY not in direct_.meta.keys()
    assert direct_.meta.calls[0] == ("refresh", ENV_KEY)
    state = direct_.state()
    assert (state.source, state.token, state.rejected_at) == (
        instagram.fingerprint(ENV_KEY),
        "IGAA-refreshed-from-env",
        None,
    )


async def test_the_journal_never_shows_a_key_even_from_httpx(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """Приёмки 3 и 5 через настоящий клиент httpx: ключ продления — в адресе,
    его вырезает формат журнала; данные — заголовком; запросы только `GET`, и
    ни одного — в отправку сообщений Direct."""
    caplog.set_level(logging.INFO)
    url = "https://lookaside.example/ig_messaging_cdn/?asset_id=9"
    voice = {"data": [{"mime_type": "audio/aac", "file_url": url}]}
    routes: dict[str, Any] = {
        "/refresh_access_token": {"access_token": FRESH_KEY, "expires_in": 5183944},
        "/v25.0/me": {"user_id": BUSINESS.user_id, "username": BUSINESS.username},
        "/v25.0/me/conversations": {
            "data": [{"id": CONVERSATION, "updated_time": meta_time(NOW - timedelta(minutes=1))}]
        },
        f"/v25.0/{CONVERSATION}": {
            "messages": {
                "data": [raw_message(), raw_message("", message_id="m_2", attachments=voice)]
            }
        },
    }

    def meta(request: httpx.Request) -> httpx.Response:
        if request.url.host == "lookaside.example":
            return httpx.Response(200, content=b"voice")
        return httpx.Response(200, json=routes[request.url.path])

    api, requests = mock_api(meta)
    direct_ = Direct(tmp_path, store=consented(), transcriber=FakeTranscriber(), api=api)

    assert await direct_.service.poll(NOW) == 2
    await direct_.service.stop()

    assert {request.method for request in requests} == {"GET"}
    assert not any(request.url.path.endswith("/messages") for request in requests)
    journal = HidingFormatter(LOG_FORMAT, secrets_of(direct_.settings))
    lines = "\n".join(journal.format(record) for record in caplog.records)
    assert "HTTP Request: GET https://graph.instagram.com/refresh_access_token" in lines
    for key in (ENV_KEY, FRESH_KEY):
        assert key not in lines
    assert json.loads(direct_.path.read_text(encoding="utf-8"))["token"] == FRESH_KEY


# ------------------------------------------------------ приёмка 4: отказ ключа


def rejected() -> list[InstagramError]:
    return [KeyRejected("Meta ответила 400, код 190")]


async def test_rejected_key_stops_polling_and_tells_the_owner_once(tmp_path: Path) -> None:
    direct_ = Direct(tmp_path, store=consented())
    direct_.meta.failures["conversations"] = rejected()

    assert await direct_.service.poll(NOW) == 0
    asked = len(direct_.meta.calls)
    assert await direct_.service.tick(NOW) == 1
    assert await direct_.service.tick(NOW + timedelta(minutes=10)) == 0
    await direct_.service.wait()

    assert direct_.sender.texts == [texts.INSTAGRAM_REJECTED]
    assert len(direct_.meta.calls) == asked, "опрос стоит"
    assert direct_.state().rejected_at == NOW


async def test_rejected_key_stays_stopped_after_a_restart_with_the_same_key(
    tmp_path: Path,
) -> None:
    first = Direct(tmp_path, store=consented())
    first.meta.failures["account"] = rejected()
    await first.service.poll(NOW)
    await first.service.tick(NOW)
    again = Direct(tmp_path, store=first.store)

    assert await again.service.tick(NOW + timedelta(hours=1)) == 0
    await again.service.wait()

    assert again.meta.calls == []
    assert again.sender.texts == []


async def test_new_key_after_a_rejection_polls_again(tmp_path: Path) -> None:
    first = Direct(tmp_path, store=consented())
    first.meta.failures["conversations"] = rejected()
    await first.service.poll(NOW)
    renewed = Direct(tmp_path, store=first.store, token=OTHER_KEY)

    await renewed.service.tick(NOW + timedelta(hours=1))
    await renewed.service.wait()

    assert "conversations" in renewed.meta.names()
    assert renewed.state().rejected_at is None


async def test_night_rejection_is_told_in_the_morning(tmp_path: Path) -> None:
    night = datetime(2026, 10, 7, 23, 0, tzinfo=TZ)
    direct_ = Direct(tmp_path, store=consented())
    direct_.meta.failures["conversations"] = rejected()
    await direct_.service.poll(night)

    assert await direct_.service.tick(night + timedelta(minutes=30)) == 0
    assert await direct_.service.tick(datetime(2026, 10, 8, 8, 0, tzinfo=TZ)) == 1


async def test_unsent_rejection_notice_is_retried(tmp_path: Path) -> None:
    direct_ = Direct(tmp_path, store=consented())
    direct_.meta.failures["conversations"] = rejected()
    await direct_.service.poll(NOW)
    direct_.sender.broken = True

    assert await direct_.service.tick(NOW) == 0
    assert direct_.state().reported_at is None
    direct_.sender.broken = False
    assert await direct_.service.tick(NOW + timedelta(minutes=1)) == 1


async def test_no_access_to_direct_is_told_once_and_polling_goes_on(tmp_path: Path) -> None:
    direct_ = Direct(tmp_path, store=consented())
    direct_.meta.say(raw_message(message_id="m_1"))
    denied = AccessDenied("Meta ответила 403, код 200")
    direct_.meta.failures["conversations"] = [denied, denied]

    assert await direct_.service.poll(NOW) == 0
    assert await direct_.service.tick(NOW) == 1
    await direct_.service.wait()
    assert await direct_.service.tick(NOW + timedelta(minutes=5)) == 0
    await direct_.service.wait()

    assert direct_.sender.texts == [texts.INSTAGRAM_NO_ACCESS]
    assert direct_.state().rejected_at is None, "ключ не отвергнут"
    assert [row["external_id"] for row in direct_.store.messages] == ["m_1"], "доступ вернулся"


# --------------------------------------- приёмка 6: сбой Meta и базы не теряет


async def test_meta_failure_is_caught_up_by_the_next_poll(tmp_path: Path) -> None:
    direct_ = Direct(tmp_path, store=consented())
    direct_.meta.say(raw_message(message_id="m_1", at=NOW - timedelta(minutes=2)))
    direct_.meta.failures["messages"] = [InstagramError("Meta ответила 500")]

    assert await direct_.service.poll(NOW) == 0
    assert direct_.store.messages == []
    assert direct_.state().polled_until is None
    direct_.meta.say(raw_message(message_id="m_2", at=NOW + timedelta(minutes=3)))

    assert await direct_.service.poll(NOW + timedelta(minutes=5)) == 2

    assert [row["external_id"] for row in direct_.store.messages] == ["m_1", "m_2"]


async def test_network_failure_on_the_list_is_caught_up_too(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    direct_ = Direct(tmp_path, store=consented())
    direct_.meta.say(raw_message(message_id="m_1"))
    direct_.meta.failures["conversations"] = [InstagramError("сеть: ConnectError")]

    assert await direct_.service.poll(NOW) == 0
    assert "Опрос Instagram не удался: сеть: ConnectError" in caplog.text

    assert await direct_.service.poll(NOW + timedelta(minutes=5)) == 1


async def test_database_failure_keeps_the_cursor(tmp_path: Path) -> None:
    store = consented()
    direct_ = Direct(tmp_path, store=store)
    direct_.meta.say(raw_message(message_id="m_1"))
    store.broken.add("store")

    assert await direct_.service.poll(NOW) == 0
    store.broken.discard("store")

    assert await direct_.service.poll(NOW + timedelta(minutes=5)) == 1


async def test_repeated_messages_in_the_overlap_are_not_stored_twice(tmp_path: Path) -> None:
    direct_ = Direct(tmp_path, store=consented())
    direct_.meta.say(raw_message(message_id="m_1", at=NOW - timedelta(minutes=1)))
    await direct_.service.poll(NOW)

    assert await direct_.service.poll(NOW + timedelta(minutes=5)) == 0

    assert len(direct_.store.messages) == 1


# ----------------------------------------------------- сборка и шаг тика (§6.2)


async def test_without_a_key_instagram_is_not_built(bot: Bot) -> None:
    """Приёмка 1: нет `INSTAGRAM_TOKEN` — Instagram не опрашивается."""
    settings = make_settings()
    chats = ChatService(settings=settings, store=FakeChatStore(), send=FakeSender())

    assert build_instagram(settings, bot, chats) is None


async def test_with_a_key_instagram_is_built(bot: Bot) -> None:
    settings: Settings = replace(make_settings(), instagram_token=ENV_KEY)
    chats = ChatService(settings=settings, store=FakeChatStore(), send=FakeSender())

    service = build_instagram(settings, bot, chats)

    assert isinstance(service, InstagramService)
    await service.stop()


def reminder_service(step: FakeChatTicker) -> ReminderService:
    return ReminderService(
        settings=make_settings(),
        due=FakeDue([]),
        mark_sent=FakeMarks(),
        close_task=FakeCloser(),
        notify=FakeNotifier(),
        moved=FakeMoved([]),
        clear_moved=FakeClearMoved(),
        announce=FakeAnnouncer(),
        instagram=step,
    )


async def test_reminder_tick_runs_the_instagram_step_and_counts_its_message() -> None:
    step = FakeChatTicker(sent=1)

    assert await reminder_service(step).tick(SATURDAY_NOON) == 1
    assert step.moments == [SATURDAY_NOON]


async def test_reminder_tick_survives_a_broken_instagram_step(
    caplog: pytest.LogCaptureFixture,
) -> None:
    step = FakeChatTicker(broken=True)

    assert await reminder_service(step).tick(SATURDAY_NOON) == 0
    assert "Шаг Instagram" in caplog.text


def test_help_mentions_instagram_direct() -> None:
    assert "Direct бизнес-аккаунта в Instagram" in texts.HELP
    assert "В Direct не отвечаю" in texts.HELP


async def test_stop_closes_the_client(tmp_path: Path) -> None:
    direct_ = Direct(tmp_path, store=consented())
    direct_.service.launch(NOW)

    await direct_.service.stop()

    assert direct_.meta.closed is True


# --------------------------------------------------------------- живой прогон


@pytest.mark.live
async def test_live_direct_is_read_by_the_conversations_api() -> None:
    """Вживую (§26.3): бизнес-аккаунт, разговоры и сообщения трёх свежих —
    тем же клиентом, что опрос. Только чтение: ключ не продлевается, в базу
    ничего не пишется, в вывод — только числа и виды, без текстов и имён.
    Нужен `INSTAGRAM_TOKEN` в `.env`: `pytest -m live -k instagram -s`."""
    settings = live_settings()
    if settings.instagram_token is None:
        pytest.skip("Живой прогон невозможен: в .env нет INSTAGRAM_TOKEN")
    token = settings.instagram_token
    api = HttpInstagramApi.create()
    try:
        account = await api.account(token)
        conversations, after = await api.conversations(token, None)
        kinds: Counter[str] = Counter()
        for conversation in conversations[:3]:
            for message in await api.messages(token, conversation.id):
                incoming = instagram.to_incoming(message, conversation.id, account)
                kinds[incoming.kind if incoming is not None else "не читается"] += 1
                if incoming is not None:
                    kinds[incoming.direction] += 1
        print(
            f"бизнес-аккаунт есть: {bool(account.user_id)}, разговоров на странице "
            f"{len(conversations)}, следующая страница {'есть' if after else 'нет'}, "
            f"сообщения по видам {dict(kinds)}"
        )
    finally:
        await api.close()
