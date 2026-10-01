"""Клиент Supabase для бота.

Бот ходит в базу с ключом service-role: он работает на сервере от отдельного
пользователя (techspec/16-server.md) и внешних соединений не принимает. В сборку
Mini App этот ключ не попадает — там только анонимный ключ и правила доступа
на стороне базы (инвариант 1).
"""

from __future__ import annotations

from supabase import Client, create_client

from solomon.config import Settings


def create_supabase_client(settings: Settings) -> Client:
    """Собрать клиент. Сетевых запросов здесь ещё нет — только конфигурация."""
    return create_client(settings.supabase_url, settings.supabase_service_role_key)
