"""Audit date-order and timezone regression tests."""

from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from real_estate_mvp.audit import (
    AmbiguousDateFormatError,
    InvalidTimestampError,
    analyze_export,
    detect_date_order,
    parse_export,
)
from real_estate_mvp.utils import BUSINESS_TZ


def test_expected_day_first_export_parses():
    messages = parse_export(
        "25/08/2026, 09:15 - Buyer: Is the two bedroom available?\n"
        "I can visit this week.\n"
        "25/08/2026, 09:22 - Agent: Yes, it is available."
    )
    assert len(messages) == 2
    assert messages[0].timestamp == datetime(2026, 8, 25, 9, 15, tzinfo=BUSINESS_TZ)
    assert "I can visit" in messages[0].message
    assert messages[1].sender == "Agent"


def test_iso_year_first_dates_parse_under_any_order():
    messages = parse_export(
        "2026-10-02, 09:00 - Buyer: Hello\n"
        "2026-10-02, 09:10 - Agent: Hi there",
        date_order="month_first",
    )
    assert len(messages) == 2
    assert messages[0].timestamp == datetime(2026, 10, 2, 9, 0, tzinfo=BUSINESS_TZ)


def test_explicit_date_order_resolves_ambiguous_date():
    # 02/10/2026 is ambiguous: 2 October (day-first) or 10 February.
    day_first = parse_export("02/10/2026, 09:00 - Buyer: Hello", date_order="day_first")
    month_first = parse_export(
        "02/10/2026, 09:00 - Buyer: Hello", date_order="month_first"
    )
    assert day_first[0].timestamp.month == 10
    assert month_first[0].timestamp.month == 2


def test_auto_detection_uses_unambiguous_evidence():
    text = (
        "25/08/2026, 09:00 - Buyer: Day greater than 12\n"
        "02/10/2026, 09:05 - Agent: Ambiguous but consistent"
    )
    assert detect_date_order(text) == "day_first"
    messages = parse_export(text, date_order="auto")
    assert messages[1].timestamp.month == 10

    month_first_text = (
        "08/25/2026, 09:00 - Buyer: Month greater than 12\n"
        "02/10/2026, 09:05 - Agent: Ambiguous but consistent"
    )
    assert detect_date_order(month_first_text) == "month_first"


def test_auto_detection_refuses_ambiguous_exports():
    text = (
        "02/10/2026, 09:00 - Buyer: Quick question\n"
        "04/10/2026, 09:05 - Agent: Answer"
    )
    with pytest.raises(AmbiguousDateFormatError, match="AUDIT_DATE_ORDER"):
        parse_export(text, date_order="auto")
    with pytest.raises(AmbiguousDateFormatError):
        detect_date_order(text)


def test_invalid_timestamp_fails_clearly():
    with pytest.raises(InvalidTimestampError, match="Unsupported WhatsApp"):
        parse_export("32/13/2026, 09:00 - Buyer: Impossible date")

    with pytest.raises(InvalidTimestampError):
        parse_export("02/10/2026, 99:99 - Buyer: Impossible time")


def test_timezone_is_configurable():
    # Export wall-clock timestamps are interpreted in the configured
    # audit timezone, so the same export carries a different zone
    # offset and local hour under each configuration.
    export = (
        "02/10/2026, 02:00 - Buyer: Late night question\n"
        "02/10/2026, 02:05 - Agent: Answer"
    )
    nairobi_report = analyze_export(export, agent_names=["Agent"])
    kabul_report = analyze_export(
        export, agent_names=["Agent"], timezone=ZoneInfo("Asia/Kabul")
    )
    new_york_report = analyze_export(
        export, agent_names=["Agent"], timezone=ZoneInfo("America/New_York")
    )
    assert nairobi_report["cycles"][0]["inquiry_at"].endswith("+03:00")
    assert kabul_report["cycles"][0]["inquiry_at"].endswith("+04:30")
    assert new_york_report["cycles"][0]["inquiry_at"].endswith("-04:00")
    # 02:00 local is night in every configured zone.
    assert nairobi_report["coverage"]["night"]["inquiries"] == 1
    assert kabul_report["cycles"][0]["period"] == "night"
    assert new_york_report["cycles"][0]["period"] == "night"


def test_timezone_changes_weekday_classification():
    # 00:30 on 05/10/2026 is Sunday morning in Kabul (+04:30) but
    # still Saturday night in New York (-04:00) for the same wall
    # clock read in each configured zone... both classify by their
    # own local weekday, so verify the sunday split explicitly.
    export = "04/10/2026, 12:00 - Buyer: Sunday question\n"
    for timezone in ("Africa/Nairobi", "Asia/Kabul", "America/New_York"):
        report = analyze_export(
            export, agent_names=["Agent"], timezone=ZoneInfo(timezone)
        )
        assert report["coverage"]["sunday"]["inquiries"] == 1, timezone


def test_malformed_export_without_headers_is_safe():
    report = analyze_export("not a WhatsApp header\nrandom line")
    assert report["total_inquiries"] == 0
    assert report["percentage_unanswered"] == 0.0


def test_analysis_reports_date_order_used():
    report = analyze_export(
        "25/08/2026, 09:00 - Buyer: Question\n"
        "25/08/2026, 09:02 - Agent: Answer",
        agent_names=["Agent"],
    )
    assert report["total_inquiries"] == 1
    assert report["median_response_seconds"] == 120
