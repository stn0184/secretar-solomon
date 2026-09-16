"""Health-запрос к базе: разбирает и успех, и отказ — без трассировки наружу."""

import httpx

from solomon.config import Settings
from solomon.db.health import HealthReport, check_database, report_for_error, report_for_status

SETTINGS = Settings(
    telegram_bot_token="123456:test-token",
    owner_telegram_id=777,
    supabase_url="https://example.supabase.co",
    supabase_service_role_key="service-role-key",
)


def test_two_hundred_means_alive() -> None:
    report = report_for_status(200)

    assert report.ok
    assert "база" in report.message.lower()


def test_rejected_key_is_not_alive() -> None:
    report = report_for_status(401)

    assert not report.ok
    assert "ключ" in report.message.lower()


def test_server_error_is_not_alive() -> None:
    report = report_for_status(503)

    assert not report.ok
    assert "503" in report.message


def test_network_error_is_explained_not_raised() -> None:
    report = report_for_error(httpx.ConnectError("no route to host"))

    assert not report.ok
    assert "недоступна" in report.message.lower()


async def test_check_database_reports_success() -> None:
    async def probe(_: Settings) -> int:
        return 200

    report = await check_database(SETTINGS, probe=probe)

    assert report == HealthReport(ok=True, message=report_for_status(200).message)


async def test_check_database_swallows_exception() -> None:
    async def probe(_: Settings) -> int:
        raise httpx.ConnectTimeout("timed out")

    report = await check_database(SETTINGS, probe=probe)

    assert not report.ok
    assert report.message
