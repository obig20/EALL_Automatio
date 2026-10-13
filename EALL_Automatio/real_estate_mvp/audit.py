"""Offline WhatsApp-export parser and response-time audit."""

from __future__ import annotations

import argparse
import json
import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from statistics import mean, median
from typing import Iterable
from zoneinfo import ZoneInfo

from .config import get_settings
from .utils import BUSINESS_TZ, format_duration

HEADER = re.compile(
    r"^\[?(?P<date>\d{1,4}[./-]\d{1,2}[./-]\d{2,4}),\s*"
    r"(?P<time>\d{1,2}:\d{2}(?::\d{2})?\s*(?:AM|PM|am|pm)?)\]?"
    r"\s*[-–]?\s*(?P<sender>[^:]+):\s?(?P<message>.*)$"
)

PERIODS = ("business_hours", "uncovered_hours", "night", "sunday")

# Explicit date orders. "auto" requires unambiguous evidence from the
# export itself and refuses to guess when every date could be either
# day-first or month-first.
DATE_ORDER_FORMATS: dict[str, tuple[str, ...]] = {
    "day_first": ("%d/%m/%Y", "%d/%m/%y"),
    "month_first": ("%m/%d/%Y", "%m/%d/%y"),
    "year_first": ("%Y/%m/%d",),
}


class AmbiguousDateFormatError(ValueError):
    """The export's date order cannot be determined confidently."""


class InvalidTimestampError(ValueError):
    """A chat header carried a timestamp that cannot be parsed."""


@dataclass(frozen=True)
class ChatMessage:
    timestamp: datetime
    sender: str
    message: str


@dataclass(frozen=True)
class ResponseCycle:
    inquiry_at: datetime
    buyer_message: str
    response_at: datetime | None
    agent_message: str | None
    response_seconds: float | None


def _date_components(date_text: str) -> tuple[int, int, int] | None:
    normalized = date_text.replace(".", "/").replace("-", "/")
    parts = normalized.split("/")
    if len(parts) != 3:
        return None
    try:
        return int(parts[0]), int(parts[1]), int(parts[2])
    except ValueError:
        return None


def detect_date_order(text: str) -> str:
    """Determine the export's date order from unambiguous dates only.

    Returns "day_first", "month_first", or "year_first". Raises
    AmbiguousDateFormatError when no header date contains evidence
    (a component greater than 12), so ambiguous exports must be
    configured explicitly with AUDIT_DATE_ORDER instead of being
    silently reinterpreted.
    """
    for line in text.splitlines():
        match = HEADER.match(line)
        if not match:
            continue
        components = _date_components(match["date"])
        if components is None:
            continue
        first, second, _ = components
        if first > 31:
            return "year_first"
        if first > 12 >= second:
            return "day_first"
        if second > 12 >= first:
            return "month_first"
    raise AmbiguousDateFormatError(
        "Cannot determine the export's date format confidently: "
        "no date has a day or month greater than 12. Set "
        "AUDIT_DATE_ORDER explicitly (day_first, month_first, or "
        "year_first) or pass date_order to analyze_export."
    )


def _parse_timestamp(
    date_text: str, time_text: str, date_order: str
) -> datetime:
    time_text = re.sub(r"\s+", " ", time_text.strip()).upper()
    time_formats = ("%H:%M", "%H:%M:%S", "%I:%M %p", "%I:%M:%S %p")
    components = _date_components(date_text)
    if components is None:
        raise InvalidTimestampError(f"Unsupported WhatsApp date: {date_text}")
    # A leading four-digit year is unambiguous evidence of ISO order
    # regardless of the configured day/month order.
    date_formats = (
        DATE_ORDER_FORMATS["year_first"]
        if components[0] > 31
        else DATE_ORDER_FORMATS.get(date_order, DATE_ORDER_FORMATS["day_first"])
    )
    for date_format in date_formats:
        for time_format in time_formats:
            try:
                return datetime.strptime(
                    f"{date_text.replace('.', '/').replace('-', '/')} {time_text}",
                    f"{date_format} {time_format}",
                )
            except ValueError:
                continue
    raise InvalidTimestampError(
        f"Unsupported WhatsApp timestamp: {date_text}, {time_text} "
        f"(expected date order: {date_order})"
    )


def _resolve_timezone(timezone: str | ZoneInfo | None) -> ZoneInfo:
    if timezone is None:
        return BUSINESS_TZ
    if isinstance(timezone, ZoneInfo):
        return timezone
    return ZoneInfo(timezone)


def parse_export(
    text: str,
    *,
    date_order: str = "day_first",
    timezone: str | ZoneInfo | None = None,
) -> list[ChatMessage]:
    """Parse common WhatsApp text exports, retaining multiline bodies.

    ``date_order`` selects how ambiguous DD/MM dates are read. The
    application default is day-first (the operating context), "auto"
    requires unambiguous evidence, and any other value fails clearly.
    """
    if date_order == "auto":
        date_order = detect_date_order(text)
    audit_tz = _resolve_timezone(timezone)
    parsed: list[ChatMessage] = []
    for line in text.splitlines():
        match = HEADER.match(line)
        if match:
            try:
                timestamp = _parse_timestamp(match["date"], match["time"], date_order)
            except InvalidTimestampError as exc:
                # A header-shaped line with an unparseable timestamp is a
                # data problem that must surface, not be hidden as body text.
                raise InvalidTimestampError(
                    f"{exc}; line: {line[:120]}"
                ) from exc
            parsed.append(
                ChatMessage(
                    timestamp=timestamp.replace(tzinfo=audit_tz),
                    sender=match["sender"].strip(),
                    message=match["message"],
                )
            )
        elif parsed:
            previous = parsed[-1]
            parsed[-1] = ChatMessage(
                previous.timestamp, previous.sender, previous.message + "\n" + line
            )
    return parsed


def _is_agent(sender: str, agent_names: Iterable[str]) -> bool:
    sender_key = sender.strip().casefold()
    return any(sender_key == name.strip().casefold() for name in agent_names if name.strip())


def build_response_cycles(
    messages: Iterable[ChatMessage],
    agent_names: Iterable[str],
    *,
    max_buyer_gap_seconds: int = 4 * 60 * 60,
) -> list[ResponseCycle]:
    """Group nearby buyer messages; a long buyer-only gap starts a new cycle."""
    cycles: list[ResponseCycle] = []
    active_buyer: list[ChatMessage] = []
    ordered_messages = sorted(messages, key=lambda item: item.timestamp)

    def close_unanswered() -> None:
        if not active_buyer:
            return
        first = active_buyer[0]
        cycles.append(
            ResponseCycle(
                inquiry_at=first.timestamp,
                buyer_message="\n".join(item.message for item in active_buyer),
                response_at=None,
                agent_message=None,
                response_seconds=None,
            )
        )
        active_buyer.clear()

    for message in ordered_messages:
        if not _is_agent(message.sender, agent_names):
            if (
                active_buyer
                and (message.timestamp - active_buyer[-1].timestamp).total_seconds()
                > max_buyer_gap_seconds
            ):
                close_unanswered()
            active_buyer.append(message)
            continue
        if not active_buyer:
            continue
        first = active_buyer[0]
        cycles.append(
            ResponseCycle(
                inquiry_at=first.timestamp,
                buyer_message="\n".join(item.message for item in active_buyer),
                response_at=message.timestamp,
                agent_message=message.message,
                response_seconds=max(
                    0.0, (message.timestamp - first.timestamp).total_seconds()
                ),
            )
        )
        active_buyer.clear()
    close_unanswered()
    return cycles


def _period(
    timestamp: datetime,
    start: int,
    end: int,
    uncovered_end: int,
    timezone: ZoneInfo,
) -> str:
    local = timestamp.astimezone(timezone)
    if local.weekday() == 6:
        return "sunday"
    if start <= local.hour < end:
        return "business_hours"
    if end <= local.hour < uncovered_end:
        return "uncovered_hours"
    return "night"


def _metrics(cycles: list[ResponseCycle]) -> dict:
    total = len(cycles)
    answered = [cycle.response_seconds for cycle in cycles if cycle.response_seconds is not None]
    unanswered = total - len(answered)
    over_15 = sum(seconds > 15 * 60 for seconds in answered)
    return {
        "total_inquiries": total,
        "answered_inquiries": len(answered),
        "unanswered_inquiries": unanswered,
        "median_response_seconds": median(answered) if answered else None,
        "average_response_seconds": mean(answered) if answered else None,
        "fastest_response_seconds": min(answered) if answered else None,
        "slowest_response_seconds": max(answered) if answered else None,
        "over_15_minutes": over_15,
        "percentage_over_15_minutes": round(over_15 / total * 100, 1) if total else 0.0,
        "percentage_unanswered": round(unanswered / total * 100, 1) if total else 0.0,
    }


def analyze_export(
    text: str,
    *,
    agent_names: Iterable[str] = ("Agent", "Sales", "Admin"),
    business_start_hour: int = 8,
    business_end_hour: int = 18,
    uncovered_end_hour: int = 21,
    date_order: str = "day_first",
    timezone: str | ZoneInfo | None = None,
) -> dict:
    if date_order == "auto":
        date_order = detect_date_order(text)
    audit_tz = _resolve_timezone(timezone)
    messages = parse_export(text, date_order=date_order, timezone=audit_tz)
    cycles = build_response_cycles(messages, agent_names)
    overall = _metrics(cycles)
    coverage: dict[str, dict] = {}
    for period in PERIODS:
        group = [
            cycle
            for cycle in cycles
            if _period(
                cycle.inquiry_at,
                business_start_hour,
                business_end_hour,
                uncovered_end_hour,
                audit_tz,
            )
            == period
        ]
        stats = _metrics(group)
        coverage[period] = {
            "inquiries": stats["total_inquiries"],
            "answered": stats["answered_inquiries"],
            "unanswered": stats["unanswered_inquiries"],
            "median_response_seconds": stats["median_response_seconds"],
        }
    return {
        **overall,
        "coverage": coverage,
        "cycles": [
            {
                "inquiry_at": cycle.inquiry_at.isoformat(),
                "buyer_message": cycle.buyer_message,
                "response_at": (
                    cycle.response_at.isoformat() if cycle.response_at else None
                ),
                "agent_message": cycle.agent_message,
                "response_seconds": cycle.response_seconds,
                "response_time": format_duration(cycle.response_seconds),
                "period": _period(
                    cycle.inquiry_at,
                    business_start_hour,
                    business_end_hour,
                    uncovered_end_hour,
                    audit_tz,
                ),
            }
            for cycle in cycles
        ],
    }


def render_report(report: dict) -> str:
    def duration(key: str) -> str | None:
        formatted = format_duration(report.get(key))
        return formatted if formatted is not None else "N/A"

    lines = [
        "============================================",
        "REAL ESTATE RESPONSE-TIME AUDIT",
        "============================================",
        "",
        f"Total inquiries:       {report['total_inquiries']}",
        f"Answered:              {report['answered_inquiries']}",
        f"Unanswered:            {report['unanswered_inquiries']}",
        "",
        f"Median response:       {duration('median_response_seconds')}",
        f"Average response:      {duration('average_response_seconds')}",
        f"Fastest response:      {duration('fastest_response_seconds')}",
        f"Slowest response:      {duration('slowest_response_seconds')}",
        f"Over 15 minutes:       {report['over_15_minutes']}",
        f"Percentage >15 min:    {report['percentage_over_15_minutes']:.1f}%",
        f"Percentage unanswered: {report['percentage_unanswered']:.1f}%",
        "",
        "--------------------------------------------",
        "COVERAGE",
        "--------------------------------------------",
    ]
    labels = {
        "business_hours": "Business Hours",
        "uncovered_hours": "Uncovered Hours",
        "night": "Night",
        "sunday": "Sunday",
    }
    for key in PERIODS:
        group = report["coverage"][key]
        lines.extend(
            [
                "",
                labels[key],
                f"  Inquiries:           {group['inquiries']}",
                f"  Answered:            {group['answered']}",
                f"  Unanswered:          {group['unanswered']}",
                "  Median response:     "
                + (format_duration(group["median_response_seconds"]) or "N/A"),
            ]
        )
    lines.extend(["", "============================================"])
    return "\n".join(lines)


def run_audit(input_path: Path, output_path: Path | None = None) -> dict:
    settings = get_settings(validate=False)
    try:
        text = input_path.read_text(encoding="utf-8-sig")
    except OSError as exc:
        raise SystemExit(f"Unable to read export: {exc}") from exc
    report = analyze_export(
        text,
        agent_names=settings.agent_names,
        business_start_hour=settings.business_start_hour,
        business_end_hour=settings.business_end_hour,
        uncovered_end_hour=settings.uncovered_end_hour,
        date_order=settings.audit_date_order,
        timezone=settings.audit_timezone,
    )
    if output_path is None:
        output_path = Path(__file__).resolve().parent / "reports" / "audit.json"
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(render_report(report))
    print(f"\nJSON report: {output_path}")
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description="Audit a synthetic/anonymized WhatsApp export")
    parser.add_argument("input", type=Path, help="Path to WhatsApp exported .txt file")
    parser.add_argument("--output", type=Path, help="JSON report destination")
    args = parser.parse_args()
    run_audit(args.input, args.output)


if __name__ == "__main__":
    main()
