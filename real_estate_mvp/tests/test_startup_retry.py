"""Live startup must survive a transient Telegram bootstrap timeout.

A live run showed that python-telegram-bot's bootstrap retry loop has
bootstrap_retries=0 by default, so a transient ConnectTimeout during
get_me()/initialize() aborts the process. The entry point must wrap
polling in an outer retry loop so a temporary network blip at startup
does not kill the bot.
"""

from pathlib import Path

import pytest

from real_estate_mvp import main as main_module
from real_estate_mvp.config import Settings

PROPERTIES_FILE = Path(__file__).parents[1] / "data" / "properties.json"


def _settings(tmp_path) -> Settings:
    return Settings(
        telegram_bot_token="synthetic-test-token",
        founder_chat_id="777",
        gemini_api_key="synthetic-test-key",
        mock_mode=False,
        business_start_hour=8,
        business_end_hour=18,
        uncovered_end_hour=21,
        agent_names=("Agent", "Sales", "Admin"),
        data_dir=tmp_path,
        properties_file=PROPERTIES_FILE,
    )


class _FakeApplication:
    """Minimal stand-in: run_polling raises whatever the behaviour says.

    The behaviour receives a shared counter so rebuilt instances can
    coordinate a deterministic exit condition.
    """

    _counter = {"count": 0}

    def __init__(self, settings, behaviour):
        self.settings = settings
        self.behaviour = behaviour

    def run_polling(self, drop_pending_updates=False):
        _FakeApplication._counter["count"] += 1
        return self.behaviour(_FakeApplication._counter["count"])

    @staticmethod
    def reset_counter():
        _FakeApplication._counter["count"] = 0


def test_live_polling_restarts_after_transient_bootstrap_timeout(tmp_path, monkeypatch):
    """A transient timeout during polling must trigger a restart, not exit."""
    settings = _settings(tmp_path)
    _FakeApplication.reset_counter()

    def behaviour(count):
        if count == 1:
            raise TimeoutError("synthetic bootstrap timeout")
        raise SystemExit(0)

    def fake_build(s):
        return _FakeApplication(s, behaviour)

    monkeypatch.setattr(main_module, "build_application", fake_build)
    monkeypatch.setattr(main_module.time, "sleep", lambda *_: None)

    with pytest.raises(SystemExit):
        main_module.run_live_polling(_FakeApplication(settings, behaviour), settings)

    assert _FakeApplication._counter["count"] == 2


def test_live_polling_propagates_keyboard_interrupt(tmp_path, monkeypatch):
    """Ctrl-C must still stop the process; only transient errors retry."""
    settings = _settings(tmp_path)
    _FakeApplication.reset_counter()

    def behaviour(count):
        raise KeyboardInterrupt

    monkeypatch.setattr(main_module, "build_application", lambda s: _FakeApplication(s, behaviour))
    monkeypatch.setattr(main_module.time, "sleep", lambda *_: None)

    with pytest.raises(KeyboardInterrupt):
        main_module.run_live_polling(_FakeApplication(settings, behaviour), settings)


def test_live_polling_rebuilds_application_between_retries(tmp_path, monkeypatch):
    """After a transient failure the application must be rebuilt so a
    corrupted bot instance cannot be reused."""
    settings = _settings(tmp_path)
    _FakeApplication.reset_counter()
    built = []

    def fake_build(s):
        built.append(s)
        return _FakeApplication(s, lambda c: (_ for _ in ()).throw(SystemExit(0)))

    monkeypatch.setattr(main_module, "build_application", fake_build)
    monkeypatch.setattr(main_module.time, "sleep", lambda *_: None)

    original = _FakeApplication(settings, lambda c: (_ for _ in ()).throw(TimeoutError("x")))

    with pytest.raises(SystemExit):
        main_module.run_live_polling(original, settings)

    assert len(built) >= 1
    assert built[-1] is not original