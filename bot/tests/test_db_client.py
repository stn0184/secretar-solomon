"""Клиент базы: соединение ждём недолго, ответ — как раньше (techspec/16-server.md §16.5)."""

from __future__ import annotations

from solomon.config import Settings
from solomon.db import client as db_client


def test_http_client_waits_for_connection_briefly() -> None:
    http = db_client.create_http_client()
    try:
        assert http.timeout.connect == db_client.CONNECT_TIMEOUT_SECONDS == 5.0
        assert http.timeout.read == db_client.REQUEST_TIMEOUT_SECONDS == 120.0
        assert http.timeout.write == db_client.REQUEST_TIMEOUT_SECONDS
        assert http.timeout.pool == db_client.REQUEST_TIMEOUT_SECONDS
    finally:
        http.close()


def test_supabase_client_goes_to_the_database_through_that_http_client(settings: Settings) -> None:
    supabase = db_client.create_supabase_client(settings)
    session = supabase.postgrest.session
    try:
        assert session.timeout.connect == db_client.CONNECT_TIMEOUT_SECONDS
        assert session.timeout.read == db_client.REQUEST_TIMEOUT_SECONDS
    finally:
        session.close()
