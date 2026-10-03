"""Offline WhatsApp-export parser and response-time audit."""

from __future__ import annotations

import argparse
import json
import re
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from statistics import mean, median
from typing import Iterable

from .config import get_settings
from .utils import BUSINESS_TZ, format_duration

HEADER = re.compile(
    r"^\[?(?P<date>\d{1,4}[./-]\d{1,2}[./-]\d{2,4}),\s*"
    r"(?P<time>\d{1,2}:\d{2}(?::\d{2})?\s*(?:AM|PM|am|pm)?)\]?"
    r"\s*[-–]?\s*(?P<sender>[^:]+):\s?(?P<message>.*)$"
)

PERIODS = ("business_hours", "uncovered_hours", "night", "sunday")


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


def _parse_timestamp(date_text: str, time_text: str) -> datetime:
    date_text = date_text.replace(".", "/").replace("-", "/")
    time_text = re.sub(r"\s+", " ", time_text.strip()).upper()
    time_formats = ("%H:%M", "%H:%M:%S", "%I:%M %p", "%I:%M:%S %p")
    date_formats = (
        "%Y/%m/%d",
        "%d/%m/%Y",
        "%m/%d/%Y",
        "%d/%m/%y",
        "%m/%d/%y",
    )
    for date_format in date_formats:
        for time_format in time_formats:
            try:
                return datetime.strptime(
                    f"{date_text} {time_text}", f"{date_format} {time_format}"
                ).replace(tzinfo=BUSINESS_TZ)
            except ValueError:
                continue
    raise ValueError(f"Unsupported WhatsApp timestamp: {date_text}, {time_text}")


def parse_export(text: str) -> list[ChatMessage]:
    """Parse common WhatsApp text exports, retaining multiline message bodies."""
    parsed: list[ChatMessage] = []
    for line in text.splitlines():
        match = HEADER.match(line)
        if match:
            try:
                timestamp = _parse_timestamp(match["date"], match["time"])
            except ValueError:
                # Treat an unsupported header-looking line as body text when possible.
                if parsed:
                    previous = parsed[-1]
                    parsed[-1] = ChatMessage(
                        previous.timestamp, previous.sender, previous.message + "\n" + line
                    )
                continue
            parsed.append(
                ChatMessage(
                    timestamp=timestamp,
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


def _period(timestamp: datetime, start: int, end: int, uncovered_end: int) -> str:
    local = timestamp.astimezone(BUSINESS_TZ)
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
) -> dict:
    messages = parse_export(text)
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
                ),
            }
            for cycle in cycles
        ],
    }


def render_report(report: dict) -> str:
    def duration(key: str) -> str:
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