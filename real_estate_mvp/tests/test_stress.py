from real_estate_mvp.simulation import run_stress_simulation


def test_twenty_inquiry_acceptance_stress_simulation():
    result = run_stress_simulation()
    assert result["synthetic_inquiries"] >= 20
    assert result["retry_attempts"] == 2
    assert result["founder_deliveries"] == 3
    assert result["pending_after_restart"] >= 1
    assert result["event_count"] >= 40