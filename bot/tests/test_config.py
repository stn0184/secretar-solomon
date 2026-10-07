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
    "DEEPGRAM_API_KEY": "dg-test",
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
        deepgram_api_key="dg-test",
        anthropic_base_url=None,
        instagram_token=None,
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


def test_instagram_token_is_optional() -> None:
    """Нет ключа — Direct не опрашивается, бот запускается как раньше (§26.1)."""
    assert load_settings(FULL_ENV).instagram_token is None
    assert load_settings({**FULL_ENV, "INSTAGRAM_TOKEN": "  "}).instagram_token is None


def test_instagram_token_is_taken_as_given() -> None:
    settings = load_settings({**FULL_ENV, "INSTAGRAM_TOKEN": " IGAA-test-token "})

    assert settings.instagram_token == "IGAA-test-token"


def test_max_is_off_without_the_token_or_the_owner_id() -> None:
    """Нет хотя бы одной переменной MAX — источник выключен, бот запускается
    как раньше (`techspec/27-max.md` §27.1)."""
    assert not load_settings(FULL_ENV).max_enabled
    only_token = load_settings({**FULL_ENV, "MAX_BOT_TOKEN": "max-test-token"})
    only_owner = load_settings({**FULL_ENV, "OWNER_MAX_ID": "4242"})
    blank = load_settings({**FULL_ENV, "MAX_BOT_TOKEN": "  ", "OWNER_MAX_ID": " "})

    assert (only_token.max_bot_token, only_token.owner_max_id) == ("max-test-token", None)
    assert (only_owner.max_bot_token, only_owner.owner_max_id) == (None, 4242)
    assert not only_token.max_enabled
    assert not only_owner.max_enabled
    assert (blank.max_bot_token, blank.owner_max_id) == (None, None)


def test_max_is_on_with_both_variables() -> None:
    settings = load_settings(
        {**FULL_ENV, "MAX_BOT_TOKEN": " max-test-token ", "OWNER_MAX_ID": "4242"}
    )

    assert settings.max_bot_token == "max-test-token"
    assert settings.owner_max_id == 4242
    assert settings.max_enabled


def test_owner_max_id_must_be_a_number() -> None:
    with pytest.raises(InvalidVariable) as caught:
        load_settings({**FULL_ENV, "MAX_BOT_TOKEN": "max-test-token", "OWNER_MAX_ID": "@tim"})

    assert caught.value.name == "OWNER_MAX_ID"
    assert "OWNER_MAX_ID" in str(caught.value)
