"""Общая обвязка похода в базу: отдельный поток и один понятный тип отказа.

Клиент Supabase синхронный, а бот асинхронный: запрос уходит в отдельный
поток — иначе на время ответа базы встал бы весь long polling.

Клиент поднимает свои исключения на любой мелочи — от разрыва сети до отказа
PostgREST. Наружу они не текут: слой выше получает один понятный тип и
переводит его в слова для человека.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import Any


class DatabaseError(Exception):
    """База не ответила или ответила не тем."""


async def ask(call: Callable[[], Any]) -> Any:
    """Сходить в базу из отдельного потока; любой отказ — свой тип."""
    try:
        return await asyncio.to_thread(call)
    except Exception as error:  # отказ клиента превращается в DatabaseError, а не в трассировку
        raise DatabaseError(f"{type(error).__name__}: {error}") from error


def single_row(data: Any) -> Any:
    """PostgREST отдаёт составную строку объектом; список из одной — тоже."""
    if isinstance(data, list):
        return data[0] if data else None
    return data
