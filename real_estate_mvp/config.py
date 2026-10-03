"""Environment-backed application configuration."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

APP_DIR = Path(__file__).resolve().parent
load_dotenv(APP_DIR / ".env")


def _hour(name: str, default: int) -> int:
    try:
        value = int(os.getenv(name, str(default)))
    except ValueError as exc:
        raise ValueError(f"{name} must be an integer from 0 to 23") from exc
    if not 0 <= value <= 23:
        raise ValueError(f"{name} must be an integer from 0 to 23")
    return value


def _configured_path(name: str, default: Path) -> Path:
    raw = os.getenv(name)
    path = Path(raw).expanduser() if raw else default
    return path if path.is_absolute() else (APP_DIR / path).resolve()


@dataclass(frozen=True)
class Settings:
    telegram_bot_token: str
    founder_chat_id: str
    gemini_api_key: str
    mock_mode: bool
    business_start_hour: int
    business_end_hour: int
    uncovered_end_hour: int
    agent_names: tuple[str, ...]
    data_dir: Path
    properties_file: Path

    @classmethod
    def from_env(cls, *, validate: bool = True) -> "Settings":
        mock_raw = os.getenv("MOCK_MODE", "true").strip().lower()
        if mock_raw not in {"true", "false", "1", "0", "yes", "no"}:
            raise ValueError("MOCK_MODE must be true or false")
        mock_mode = mock_raw in {"true", "1", "yes"}
        start = _hour("BUSINESS_START_HOUR", 8)
        end = _hour("BUSINESS_END_HOUR", 18)
        uncovered_end = _hour("UNCOVERED_END_HOUR", 21)
        if not start < end <= uncovered_end:
            raise ValueError(
                "Business hours must satisfy start < end <= uncovered end"
            )

        settings = cls(
            telegram_bot_token=os.getenv("TELEGRAM_BOT_TOKEN", "").strip(),
            founder_chat_id=os.getenv("FOUNDER_CHAT_ID", "").strip(),
            gemini_api_key=os.getenv("GEMINI_API_KEY", "").strip(),
            mock_mode=mock_mode,
            business_start_hour=start,
            business_end_hour=end,
            uncovered_end_hour=uncovered_end,
            agent_names=tuple(
                name.strip()
                for name in os.getenv("AGENT_NAMES", "Agent,Sales,Admin").split(",")
                if name.strip()
            ),
            data_dir=_configured_path("DATA_DIR", APP_DIR / "data"),
            properties_file=_configured_path(
                "PROPERTIES_FILE", APP_DIR / "data" / "properties.json"
            ),
        )
        if validate:
            settings.validate()
        return settings

    def validate(self) -> None:
        if self.mock_mode:
            return
        missing = [
            name
            for name, value in (
                ("TELEGRAM_BOT_TOKEN", self.telegram_bot_token),
                ("FOUNDER_CHAT_ID", self.founder_chat_id),
                ("GEMINI_API_KEY", self.gemini_api_key),
            )
            if not value
        ]
        if missing:
            raise ValueError(
                "Live mode requires: " + ", ".join(missing) + ". Use .env; do not "
                "put credentials in source code."
            )
        try:
            founder_id = int(self.founder_chat_id)
        except ValueError as exc:
            raise ValueError("FOUNDER_CHAT_ID must be a numeric Telegram chat ID") from exc
        if founder_id <= 0:
            raise ValueError(
                "FOUNDER_CHAT_ID must be a positive private Telegram chat ID"
            )


def get_settings(*, validate: bool = True) -> Settings:
    return Settings.from_env(validate=validate)