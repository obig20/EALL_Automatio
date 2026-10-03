import pytest

from real_estate_mvp.config import Settings


def test_mock_mode_needs_no_external_credentials(monkeypatch):
    for name in (
        "TELEGRAM_BOT_TOKEN",
        "FOUNDER_CHAT_ID",
        "GEMINI_API_KEY",
        "MOCK_MODE",
    ):
        monkeypatch.delenv(name, raising=False)

    settings = Settings.from_env()
    assert settings.mock_mode
    assert not settings.telegram_bot_token
    assert not settings.gemini_api_key


def test_live_mode_requires_credentials(monkeypatch):
    monkeypatch.setenv("MOCK_MODE", "false")
    for name in ("TELEGRAM_BOT_TOKEN", "FOUNDER_CHAT_ID", "GEMINI_API_KEY"):
        monkeypatch.delenv(name, raising=False)

    with pytest.raises(ValueError, match="TELEGRAM_BOT_TOKEN"):
        Settings.from_env()


def test_invalid_hours_fail_explicitly(monkeypatch):
    monkeypatch.setenv("BUSINESS_START_HOUR", "19")
    monkeypatch.setenv("BUSINESS_END_HOUR", "18")
    with pytest.raises(ValueError, match="start < end"):
        Settings.from_env()


def test_live_mode_rejects_group_chat_id(monkeypatch):
    monkeypatch.setenv("MOCK_MODE", "false")
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "synthetic-test-token")
    monkeypatch.setenv("GEMINI_API_KEY", "synthetic-test-key")
    monkeypatch.setenv("FOUNDER_CHAT_ID", "-100123456789")

    with pytest.raises(ValueError, match="positive private"):
        Settings.from_env()


def test_relative_data_paths_resolve_inside_app(monkeypatch):
    monkeypatch.setenv("DATA_DIR", "custom-data")
    monkeypatch.setenv("PROPERTIES_FILE", "custom-data/listings.json")

    settings = Settings.from_env()
    assert settings.data_dir.name == "custom-data"
    assert settings.data_dir.is_absolute()
    assert settings.properties_file.parent == settings.data_dir