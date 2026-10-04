"""Mock/live runtime separation and configuration tests."""

import pytest

from real_estate_mvp.config import Settings


def test_mock_mode_defaults_to_separate_mock_directory(monkeypatch):
    for name in ("DATA_DIR", "MOCK_DATA_DIR", "LIVE_DATA_DIR"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("MOCK_MODE", "true")

    settings = Settings.from_env()
    assert settings.mock_mode
    assert settings.data_dir == settings.mock_data_dir
    assert "mock" in str(settings.data_dir)
    assert settings.data_dir != settings.live_data_dir
    assert not settings.mock_points_at_live_data


def test_live_mode_defaults_to_separate_live_directory(monkeypatch):
    for name in ("DATA_DIR", "MOCK_DATA_DIR", "LIVE_DATA_DIR"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("MOCK_MODE", "false")
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "synthetic-token")
    monkeypatch.setenv("FOUNDER_CHAT_ID", "777")
    monkeypatch.setenv("GEMINI_API_KEY", "synthetic-key")

    settings = Settings.from_env()
    assert not settings.mock_mode
    assert settings.data_dir == settings.live_data_dir
    assert "live" in str(settings.data_dir)
    assert settings.data_dir != settings.mock_data_dir


def test_explicit_data_dir_is_honored(monkeypatch):
    for name in ("DATA_DIR", "MOCK_DATA_DIR", "LIVE_DATA_DIR"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("MOCK_MODE", "true")
    monkeypatch.setenv("DATA_DIR", "custom-runtime")

    settings = Settings.from_env()
    assert settings.data_dir.name == "custom-runtime"


def test_mock_pointed_at_live_data_is_detected(monkeypatch):
    for name in ("DATA_DIR", "MOCK_DATA_DIR", "LIVE_DATA_DIR"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("MOCK_MODE", "true")
    monkeypatch.setenv("DATA_DIR", "data/live")

    settings = Settings.from_env()
    assert settings.mock_points_at_live_data


def test_rate_limits_and_llm_settings_are_configurable(monkeypatch):
    monkeypatch.setenv("RATE_LIMIT_PER_HOUR", "25")
    monkeypatch.setenv("RATE_LIMIT_PER_DAY", "100")
    monkeypatch.setenv("GEMINI_MODEL", "gemini-2.5-pro")
    monkeypatch.setenv("GEMINI_TEMPERATURE", "0.2")
    monkeypatch.setenv("GEMINI_MAX_OUTPUT_TOKENS", "2048")
    monkeypatch.setenv("GEMINI_TIMEOUT_SECONDS", "45")
    monkeypatch.setenv("AUDIT_DATE_ORDER", "month_first")
    monkeypatch.setenv("AUDIT_TIMEZONE", "Asia/Kolkata")

    settings = Settings.from_env()
    assert settings.rate_limit_per_hour == 25
    assert settings.rate_limit_per_day == 100
    assert settings.gemini_model == "gemini-2.5-pro"
    assert settings.gemini_temperature == 0.2
    assert settings.gemini_max_output_tokens == 2048
    assert settings.gemini_timeout_seconds == 45
    assert settings.audit_date_order == "month_first"
    assert settings.audit_timezone == "Asia/Kolkata"


def test_invalid_audit_settings_fail_explicitly(monkeypatch):
    monkeypatch.setenv("AUDIT_DATE_ORDER", "sideways")
    with pytest.raises(ValueError, match="AUDIT_DATE_ORDER"):
        Settings.from_env()

    monkeypatch.setenv("AUDIT_DATE_ORDER", "day_first")
    monkeypatch.setenv("AUDIT_TIMEZONE", "Not/AZone")
    with pytest.raises(ValueError, match="AUDIT_TIMEZONE"):
        Settings.from_env()


def test_invalid_rate_limits_fail_explicitly(monkeypatch):
    monkeypatch.setenv("RATE_LIMIT_PER_HOUR", "-5")
    with pytest.raises(ValueError, match="RATE_LIMIT_PER_HOUR"):
        Settings.from_env()


def test_llm_settings_have_conservative_defaults(monkeypatch):
    for name in (
        "GEMINI_MODEL",
        "GEMINI_TEMPERATURE",
        "GEMINI_MAX_OUTPUT_TOKENS",
        "GEMINI_TIMEOUT_SECONDS",
    ):
        monkeypatch.delenv(name, raising=False)

    settings = Settings.from_env()
    assert settings.gemini_model == "gemini-2.5-flash"
    assert settings.gemini_temperature == 0.0
    assert settings.gemini_max_output_tokens == 1024
    assert settings.gemini_timeout_seconds == 30
