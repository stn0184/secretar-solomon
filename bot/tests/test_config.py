"""Чтение настроек из окружения: полный набор, пропуск, мусор в значении."""

import pytest

from solomon.config import InvalidVariable, MissingVariable, Settings, load_settings

FULL_ENV = {
    "TELEGRAM_BOT_TOKEN": "123456:test-token",
    "OWNER_TELEGRAM_ID": "777",
    "SUPABASE_URL": "https://example.supabase.co",
    "SUPABASE_SERVICE_ROLE_KEY": "service-role-key",
}


def test_full_env_gives_settings() -> None:
    settings = load_settings(FULL_ENV)

    assert settings == Settings(
        telegram_bot_token="123456:test-token",
        owner_telegram_id=777,
        supabase_url="https://example.supabase.co",
        supabase_service_role_key="service-role-key",
    )


def test_trailing_slash_in_url_is_dropped() -> None:
    settings = load_settings({**FULL_ENV, "SUPABASE_URL": "https://example.supabase.co/"})

    assert settings.supabase_url == "https://example.supabase.co"


@pytest.mark.parametrize("name", sorted(FULL_ENV))
def test_missing_variable_is_named(name: str) -> None:
    env = {k: v for k, v in FULL_ENV.items() if k != name}

    with pytest.raises(MissingVariable) as caught:
        load_settings(env)

    assert caught.value.name == name
    assert name in str(caught.value)


@pytest.mark.parametrize("name", sorted(FULL_ENV))
def test_empty_value_counts_as_missing(name: str) -> None:
    with pytest.raises(MissingVariable) as caught:
        load_settings({**FULL_ENV, name: "   "})

    assert caught.value.name == name


def test_owner_id_must_be_a_number() -> None:
    with pytest.raises(InvalidVariable) as caught:
        load_settings({**FULL_ENV, "OWNER_TELEGRAM_ID": "@tim"})

    assert caught.value.name == "OWNER_TELEGRAM_ID"
    assert "OWNER_TELEGRAM_ID" in str(caught.value)
