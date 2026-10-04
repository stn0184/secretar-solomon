"""Клиент Supabase для бота.

Бот ходит в базу с ключом service-role: он работает на сервере от отдельного
пользователя (techspec/16-server.md) и внешних соединений не принимает. В сборку
Mini App этот ключ не попадает — там только анонимный ключ и правила доступа
на стороне базы (инвариант 1).

Соединения с базой бот ждёт недолго (techspec/16-server.md §16.5): у адреса
базы два IP, и с сервера один из них временами не отвечает. Клиент по
умолчанию ждал его 120 с и только потом брал второй — тик напоминаний
опаздывал на две минуты. Чтение и запись ждут, как раньше.
"""

from __future__ import annotations

import httpx
from supabase import Client, ClientOptions, create_client

from solomon.config import Settings

# Сколько ждать соединения с одним IP базы, прежде чем взять следующий.
CONNECT_TIMEOUT_SECONDS = 5.0
# Чтение, запись и очередь за соединением — как у клиента по умолчанию.
REQUEST_TIMEOUT_SECONDS = 120.0
# Не ответил ни один IP — ещё одна попытка: запрос не ушёл, повтор безопасен.
CONNECT_RETRIES = 1


def create_http_client() -> httpx.Client:
    """HTTP-клиент для запросов к базе: короткое ожидание соединения."""
    return httpx.Client(
        timeout=httpx.Timeout(REQUEST_TIMEOUT_SECONDS, connect=CONNECT_TIMEOUT_SECONDS),
        transport=httpx.HTTPTransport(http2=True, retries=CONNECT_RETRIES),
        follow_redirects=True,
    )


def create_supabase_client(settings: Settings) -> Client:
    """Собрать клиент. Сетевых запросов здесь ещё нет — только конфигурация."""
    options = ClientOptions(httpx_client=create_http_client())
    return create_client(settings.supabase_url, settings.supabase_service_role_key, options)
