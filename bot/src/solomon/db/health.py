"""Health-запрос: жива ли база.

Пингуется корень PostgREST — он отвечает и когда в базе ещё нет ни одной
таблицы, поэтому проверка не зависит от схемы данных. Отказ разбирается в
сообщение для человека: трассировка наружу не выходит.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass

import httpx

from solomon.config import Settings

REST_PING_PATH = "/rest/v1/"
TIMEOUT_SECONDS = 10.0

Probe = Callable[[Settings], Awaitable[int]]


@dataclass(frozen=True, slots=True)
class HealthReport:
    """Готовый ответ на вопрос «жива ли база»."""

    ok: bool
    message: str


def report_for_status(status: int) -> HealthReport:
    """Разобрать код ответа базы."""
    if 200 <= status < 300:
        return HealthReport(ok=True, message="База отвечает: адрес и ключ подошли.")
    if status in (401, 403):
        return HealthReport(
            ok=False,
            message=f"База ответила {status}: ключ не подошёл — проверьте "
            "SUPABASE_SERVICE_ROLE_KEY.",
        )
    if status == 404:
        return HealthReport(
            ok=False,
            message=f"База ответила {status}: по этому адресу нет Supabase — "
            "проверьте SUPABASE_URL.",
        )
    return HealthReport(ok=False, message=f"База ответила {status}: запрос не прошёл.")


def report_for_error(error: Exception) -> HealthReport:
    """Разобрать сетевую ошибку — вместо трассировки человеку нужна причина."""
    return HealthReport(
        ok=False,
        message=f"База недоступна: {type(error).__name__}. Проверьте SUPABASE_URL и сеть.",
    )


async def ping_rest(settings: Settings) -> int:
    """Один GET к корню PostgREST. Возвращает код ответа."""
    headers = {
        "apikey": settings.supabase_service_role_key,
        "Authorization": f"Bearer {settings.supabase_service_role_key}",
    }
    async with httpx.AsyncClient(timeout=TIMEOUT_SECONDS) as client:
        response = await client.get(f"{settings.supabase_url}{REST_PING_PATH}", headers=headers)
    return response.status_code


async def check_database(settings: Settings, probe: Probe | None = None) -> HealthReport:
    """Спросить базу, жива ли она, и вернуть ответ словами."""
    ask = probe or ping_rest
    try:
        status = await ask(settings)
    except Exception as error:  # noqa: BLE001 - health-запрос не падает трассировкой
        return report_for_error(error)
    return report_for_status(status)
