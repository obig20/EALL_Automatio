from datetime import datetime

from real_estate_mvp.audit import (
    analyze_export,
    build_response_cycles,
    parse_export,
)
from real_estate_mvp.utils import BUSINESS_TZ


def test_parse_common_formats_and_multiline_message():
    messages = parse_export(
        "[12/08/2026, 09:15] Buyer: Is the two bedroom available?\n"
        "I can visit this week.\n"
        "12/08/2026, 09:22 - Agent: Yes, it is available."
    )

    assert len(messages) == 2
    assert messages[0].timestamp == datetime(
        2026, 8, 12, 9, 15, tzinfo=BUSINESS_TZ
    )
    assert "I can visit" in messages[0].message
    assert messages[1].sender == "Agent"


def test_consecutive_buyer_messages_count_as_one_cycle():
    messages = parse_export(
        "2026-10-02, 09:00 - Buyer: Hello\n"
        "2026-10-02, 09:03 - Buyer: What is the price?\n"
        "2026-10-02, 09:10 - Sales: It is 12 million ETB.\n"
        "2026-10-02, 09:11 - Buyer: Thank you\n"
        "2026-10-02, 09:20 - Sales: You are welcome"
    )

    cycles = build_response_cycles(messages, ["Sales"])
    assert len(cycles) == 2
    assert cycles[0].response_seconds == 600
    assert "What is the price?" in cycles[0].buyer_message


def test_metrics_include_unanswered_and_over_15_minutes():
    report = analyze_export(
        "02/10/2026, 09:00 - Buyer: Quick question\n"
        "02/10/2026, 09:04 - Agent: Answer\n"
        "02/10/2026, 10:00 - Buyer: Slow question\n"
        "02/10/2026, 10:20 - Agent: Answer\n"
        "02/10/2026, 11:00 - Buyer: Still waiting",
        agent_names=["Agent"],
    )

    assert report["total_inquiries"] == 3
    assert report["answered_inquiries"] == 2
    assert report["unanswered_inquiries"] == 1
    assert report["median_response_seconds"] == 720
    assert report["over_15_minutes"] == 1
    assert report["percentage_unanswered"] == 33.3


def test_coverage_business_uncovered_night_and_sunday():
    report = analyze_export(
        "02/10/2026, 09:00 - Buyer: Business\n"
        "02/10/2026, 09:01 - Agent: Reply\n"
        "02/10/2026, 19:00 - Buyer: Evening\n"
        "02/10/2026, 19:05 - Agent: Reply\n"
        "02/10/2026, 23:00 - Buyer: Night\n"
        "02/10/2026, 23:10 - Agent: Reply\n"
        "04/10/2026, 12:00 - Buyer: Sunday\n"
        "04/10/2026, 12:15 - Agent: Reply",
        agent_names=["Agent"],
    )

    assert report["coverage"]["business_hours"]["inquiries"] == 1
    assert report["coverage"]["uncovered_hours"]["inquiries"] == 1
    assert report["coverage"]["night"]["inquiries"] == 1
    assert report["coverage"]["sunday"]["inquiries"] == 1


def test_empty_or_malformed_export_is_safe():
    report = analyze_export("not a WhatsApp header\nrandom line")
    assert report["total_inquiries"] == 0
    assert report["percentage_unanswered"] == 0