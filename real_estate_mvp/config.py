"""Environment-backed application configuration."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from dotenv import load_dotenv

APP_DIR = Path(__file__).resolve().parent
load_dotenv(APP_DIR / ".env")

DATE_ORDERS = {"auto", "day_first", "month_first", "year_first"}


def _hour(name: str, default: int) -> int:
    try:
        value = int(os.getenv(name, str(default)))
    except ValueError as exc:
        raise ValueError(f"{name} must be an integer from 0 to 23") from exc
    if not 0 <= value <= 23:
        raise ValueError(f"{name} must be an integer from 0 to 23")
    return value


def _integer(name: str, default: int, *, minimum: int = 0) -> int:
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    try:
        value = int(raw)
    except ValueError as exc:
        raise ValueError(f"{name} must be an integer >= {minimum}") from exc
    if value < minimum:
        raise ValueError(f"{name} must be an integer >= {minimum}")
    return value


def _number(name: str, default: float, *, minimum: float = 0.0) -> float:
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    try:
        value = float(raw)
    except ValueError as exc:
        raise ValueError(f"{name} must be a number >= {minimum}") from exc
    if value < minimum:
        raise ValueError(f"{name} must be a number >= {minimum}")
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
    # Runtime storage separation: mock and live traffic never share a directory
    # unless an explicit DATA_DIR override points them at the same place.
    mock_data_dir: Path = APP_DIR / "data" / "mock"
    live_data_dir: Path = APP_DIR / "data" / "live"
    # Process-local anti-flood limits (0 disables the limit).
    rate_limit_per_hour: int = 10
    rate_limit_per_day: int = 50
    # Offline WhatsApp audit parsing.
    audit_date_order: str = "day_first"
    audit_timezone: str = "Africa/Nairobi"
    # Live LLM drafting configuration.
    gemini_model: str = "gemini-2.5-flash"
    gemini_temperature: float = 0.0
    gemini_max_output_tokens: int = 1024
    gemini_timeout_seconds: int = 30

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

        # Mock and live traffic use separate runtime directories by default.
        # DATA_DIR is the explicit override for advanced setups.
        default_dir = (
            _configured_path("MOCK_DATA_DIR", APP_DIR / "data" / "mock")
            if mock_mode
            else _configured_path("LIVE_DATA_DIR", APP_DIR / "data" / "live")
        )
        data_dir = _configured_path("DATA_DIR", default_dir)
        mock_data_dir = _configured_path("MOCK_DATA_DIR", APP_DIR / "data" / "mock")
        live_data_dir = _configured_path("LIVE_DATA_DIR", APP_DIR / "data" / "live")

        audit_date_order = os.getenv("AUDIT_DATE_ORDER", "day_first").strip().lower()
        if audit_date_order not in DATE_ORDERS:
            raise ValueError(
                "AUDIT_DATE_ORDER must be one of: " + ", ".join(sorted(DATE_ORDERS))
            )
        audit_timezone = os.getenv("AUDIT_TIMEZONE", "Africa/Nairobi").strip()
        try:
            ZoneInfo(audit_timezone)
        except (ZoneInfoNotFoundError, ValueError) as exc:
            raise ValueError(
                f"AUDIT_TIMEZONE must be an IANA timezone name: {exc}"
            ) from exc

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
            data_dir=data_dir,
            properties_file=_configured_path(
                "PROPERTIES_FILE", APP_DIR / "data" / "properties.json"
            ),
            mock_data_dir=mock_data_dir,
            live_data_dir=live_data_dir,
            rate_limit_per_hour=_integer("RATE_LIMIT_PER_HOUR", 10),
            rate_limit_per_day=_integer("RATE_LIMIT_PER_DAY", 50),
            audit_date_order=audit_date_order,
            audit_timezone=audit_timezone,
            gemini_model=os.getenv("GEMINI_MODEL", "gemini-2.5-flash").strip()
            or "gemini-2.5-flash",
            gemini_temperature=_number("GEMINI_TEMPERATURE", 0.0, minimum=0.0),
            gemini_max_output_tokens=_integer("GEMINI_MAX_OUTPUT_TOKENS", 1024),
            gemini_timeout_seconds=_integer(
                "GEMINI_TIMEOUT_SECONDS", 30, minimum=1
            ),
        )
        if validate:
            settings.validate()
        return settings

    @property
    def mock_points_at_live_data(self) -> bool:
        """True when mock mode would write into the live runtime directory."""
        return self.mock_mode and self.data_dir.resolve() == self.live_data_dir.resolve()

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
