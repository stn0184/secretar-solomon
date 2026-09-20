"""Чтение настроек из окружения: полный набор, пропуск, мусор в значении."""

from zoneinfo import ZoneInfo

import pytest

from solomon.config import InvalidVariable, MissingVariable, Settings, load_settings

FULL_ENV = {
    "TELEGRAM_BOT_TOKEN": "123456:test-token",
    "OWNER_TELEGRAM_ID": "777",
    "OWNER_TIMEZONE": "Asia/Yekaterinburg",
    "SUPABASE_URL": "https://example.supabase.co",
    "SUPABASE_SERVICE_ROLE_KEY": "service-role-key",
    "ANTHROPIC_API_KEY": "sk-ant-test",
}


def test_full_env_gives_settings() -> None:
    settings = load_settings(FULL_ENV)

    assert settings == Settings(
        telegram_bot_token="123456:test-token",
        owner_telegram_id=777,
        owner_timezone=ZoneInfo("Asia/Yekaterinburg"),
        supabase_url="https://example.supabase.co",
        supabase_service_role_key="service-role-key",
        anthropic_api_key="sk-ant-test",
        anthropic_base_url=None,
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


def test_timezone_must_be_iana() -> None:
    # Пояс не проверить «на глаз»: «Москва» и «UTC+5» — не названия IANA,
    # и молча превратить их в UTC значило бы поставить сроки не на те часы.
    with pytest.raises(InvalidVariable) as caught:
        load_settings({**FULL_ENV, "OWNER_TIMEZONE": "Москва"})

    assert caught.value.name == "OWNER_TIMEZONE"
    assert "OWNER_TIMEZONE" in str(caught.value)


def test_anthropic_base_url_is_optional() -> None:
    settings = load_settings({**FULL_ENV, "ANTHROPIC_BASE_URL": "   "})

    assert settings.anthropic_base_url is None


def test_anthropic_base_url_is_taken_as_given() -> None:
    settings = load_settings({**FULL_ENV, "ANTHROPIC_BASE_URL": "https://api.agenthello.ai/v1/"})

    assert settings.anthropic_base_url == "https://api.agenthello.ai/v1"
