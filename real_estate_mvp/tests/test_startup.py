"""Live startup safety regression tests.

The live bot must never start in mock mode, and every live
startup must invoke durable-state recovery so a crash between
persistence and processing cannot leave inquiries stuck.
"""

from pathlib import Path

import pytest

from real_estate_mvp.bot import build_application, recover_pending_inquiries
from real_estate_mvp.config import Settings


def _settings(tmp_path, *, mock_mode: bool) -> Settings:
    return Settings(
        telegram_bot_token="synthetic-test-token",
        founder_chat_id="777",
        gemini_api_key="synthetic-test-key",
        mock_mode=mock_mode,
        business_start_hour=8,
        business_end_hour=18,
        uncovered_end_hour=21,
        agent_names=("Agent", "Sales", "Admin"),
        data_dir=tmp_path,
        properties_file=Path(__file__).parents[1] / "data" / "properties.json",
    )


def test_live_startup_refuses_to_start_in_mock_mode(tmp_path):
    settings = _settings(tmp_path, mock_mode=True)
    with pytest.raises(ValueError, match="Mock mode"):
        build_application(settings)


def test_live_startup_wires_durable_recovery_hook(tmp_path):
    # run_polling() invokes post_init during initialize(); the
    # hook must be the durable-state recovery routine so restarted
    # bots recover interrupted inquiries instead of leaving them
    # stuck in received/matched/drafting/sending states.
    settings = _settings(tmp_path, mock_mode=False)
    application = build_application(settings)
    assert application.post_init is recover_pending_inquiries


def test_live_startup_loads_runtime_state_into_bot_data(tmp_path):
    settings = _settings(tmp_path, mock_mode=False)
    application = build_application(settings)
    assert application.bot_data["settings"] is settings
    assert application.bot_data["store"].data_dir == tmp_path
    assert application.bot_data["queue"] is not None
    assert application.bot_data["rate_limiter"] is not None
