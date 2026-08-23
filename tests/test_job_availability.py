"""Tests for deterministic closed and expired posting detection."""

from datetime import datetime, timezone

from backend.job_availability import closure_reason, is_closed_job


def test_detects_explicit_closed_page_banner() -> None:
    reason = closure_reason(page_text="Software Engineer This job has closed. Apply similar")

    assert reason is not None
    assert "this job has closed" in reason.casefold()


def test_detects_expired_valid_through_date() -> None:
    reason = closure_reason(
        valid_through="2026-08-22",
        now=datetime(2026, 8, 23, 12, tzinfo=timezone.utc),
    )

    assert reason == "Posting expired after 2026-08-22."


def test_valid_through_date_remains_open_through_that_day() -> None:
    assert not is_closed_job(
        valid_through="2026-08-23",
        now=datetime(2026, 8, 23, 23, tzinfo=timezone.utc),
    )


def test_does_not_treat_future_application_deadline_as_closed() -> None:
    assert closure_reason(
        page_text="Applications will no longer be accepted after September 30."
    ) is None
