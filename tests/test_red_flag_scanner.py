"""Tests for JobRadar's deterministic hard-filter scanner."""

import pytest
from pydantic import ValidationError

from backend.red_flag_scanner import (
    JobPostingFacts,
    RedFlagCode,
    RedFlagScanner,
    ScanResult,
    SponsorshipStatus,
)


def make_posting(**overrides: object) -> JobPostingFacts:
    values: dict[str, object] = {
        "title": "Senior Software Engineer",
        "company": "Commercial Software Company",
        "description": "Build cloud products for business customers.",
        "sponsorship_status": SponsorshipStatus.YES,
        "company_size": 500,
        "base_salary_min_usd": 150_000,
        "base_salary_max_usd": 190_000,
    }
    values.update(overrides)
    return JobPostingFacts.model_validate(values)


def flag_codes(result: ScanResult) -> list[RedFlagCode]:
    return [flag.code for flag in result.hard_flags]


def test_qualified_commercial_job_passes_without_flags() -> None:
    result = RedFlagScanner().scan(make_posting())

    assert result.rejected is False
    assert result.requires_manual_review is False
    assert result.hard_flags == ()
    assert result.review_flags == ()


@pytest.mark.parametrize(
    "description",
    (
        "No visa sponsorship is available for this role.",
        "We are unable to offer sponsorship now or in the future.",
        "Candidates must be authorized to work in the US without sponsorship.",
        "This position is not eligible for employment sponsorship.",
    ),
)
def test_explicit_no_sponsorship_language_is_a_hard_filter(
    description: str,
) -> None:
    result = RedFlagScanner().scan(
        make_posting(
            description=description,
            sponsorship_status=SponsorshipStatus.UNKNOWN,
        )
    )

    assert flag_codes(result) == [RedFlagCode.NO_SPONSORSHIP]
    assert result.rejected is True
    assert result.review_flags == ()
    assert "sponsor" in result.hard_flags[0].evidence.casefold()


def test_structured_no_sponsorship_is_not_duplicated_by_text() -> None:
    result = RedFlagScanner().scan(
        make_posting(
            description="We will not provide visa sponsorship.",
            sponsorship_status=SponsorshipStatus.NO,
        )
    )

    assert flag_codes(result) == [RedFlagCode.NO_SPONSORSHIP]
    assert result.hard_flags[0].evidence == "sponsorship_status=NO"


def test_unknown_sponsorship_requires_review_but_does_not_reject() -> None:
    result = RedFlagScanner().scan(
        make_posting(sponsorship_status=SponsorshipStatus.UNKNOWN)
    )

    assert result.rejected is False
    assert result.requires_manual_review is True
    assert result.hard_flags == ()
    assert [flag.code for flag in result.review_flags] == [
        RedFlagCode.SPONSORSHIP_UNKNOWN
    ]


@pytest.mark.parametrize(
    ("description", "expected_code"),
    (
        (
            "This position is subject to International Traffic in Arms Regulations.",
            RedFlagCode.ITAR_RESTRICTED,
        ),
        (
            "Candidates must have an active Top Secret security clearance.",
            RedFlagCode.SECURITY_CLEARANCE,
        ),
        (
            "Develop command software for Department of Defense programs.",
            RedFlagCode.DEFENSE_RELATED,
        ),
    ),
)
def test_restricted_industry_text_is_a_hard_filter(
    description: str,
    expected_code: RedFlagCode,
) -> None:
    result = RedFlagScanner().scan(make_posting(description=description))

    assert expected_code in flag_codes(result)
    assert result.rejected is True


def test_unrelated_security_and_defense_terms_do_not_match() -> None:
    result = RedFlagScanner().scan(
        make_posting(
            description=(
                "No security clearance is required. Build commercial "
                "defense-in-depth cybersecurity controls."
            )
        )
    )

    assert result.hard_flags == ()


@pytest.mark.parametrize(
    ("company_size", "rejected"),
    ((49, True), (50, False), (None, False)),
)
def test_company_size_threshold_uses_known_values_only(
    company_size: int | None,
    rejected: bool,
) -> None:
    result = RedFlagScanner().scan(make_posting(company_size=company_size))

    assert (RedFlagCode.COMPANY_TOO_SMALL in flag_codes(result)) is rejected


@pytest.mark.parametrize(
    ("salary_min", "salary_max", "rejected"),
    (
        (120_000, 139_999, True),
        (120_000, 140_000, False),
        (120_000, 160_000, False),
        (130_000, None, False),
        (None, None, False),
    ),
)
def test_salary_threshold_uses_known_maximum_base_salary(
    salary_min: int | None,
    salary_max: int | None,
    rejected: bool,
) -> None:
    result = RedFlagScanner().scan(
        make_posting(
            base_salary_min_usd=salary_min,
            base_salary_max_usd=salary_max,
        )
    )

    assert (RedFlagCode.BASE_SALARY_TOO_LOW in flag_codes(result)) is rejected


def test_multiple_rules_are_reported_once_in_policy_order() -> None:
    result = RedFlagScanner().scan(
        make_posting(
            description=(
                "No visa sponsorship. ITAR restrictions apply. "
                "An active Secret clearance is required for this defense contractor."
            ),
            sponsorship_status=SponsorshipStatus.UNKNOWN,
            company_size=25,
            base_salary_min_usd=100_000,
            base_salary_max_usd=120_000,
        )
    )

    assert flag_codes(result) == [
        RedFlagCode.NO_SPONSORSHIP,
        RedFlagCode.ITAR_RESTRICTED,
        RedFlagCode.SECURITY_CLEARANCE,
        RedFlagCode.DEFENSE_RELATED,
        RedFlagCode.COMPANY_TOO_SMALL,
        RedFlagCode.BASE_SALARY_TOO_LOW,
    ]
    assert result.review_flags == ()


def test_invalid_salary_range_is_rejected_at_input_boundary() -> None:
    with pytest.raises(ValidationError, match="base_salary_max_usd"):
        make_posting(
            base_salary_min_usd=180_000,
            base_salary_max_usd=140_000,
        )


@pytest.mark.parametrize(
    "description",
    (
        "Only USC/GC candidates are eligible.",
        "This position requires a U.S. Person.",
        "The work is subject to export-control requirements.",
    ),
)
def test_work_authorization_and_export_control_language_rejects(
    description: str,
) -> None:
    result = RedFlagScanner().scan(make_posting(description=description))

    assert result.rejected


def test_manual_execution_is_rejected_only_when_primary_signals_accumulate() -> None:
    scanner = RedFlagScanner()

    primary = scanner.scan(
        make_posting(
            description="Run manual test cases in TestRail and execute regression suites."
        )
    )
    incidental = scanner.scan(
        make_posting(description="Own an automation framework that integrates TestRail.")
    )

    assert RedFlagCode.MANUAL_TEST_EXECUTION in {
        flag.code for flag in primary.hard_flags
    }
    assert RedFlagCode.MANUAL_TEST_EXECUTION not in {
        flag.code for flag in incidental.hard_flags
    }


def test_unknown_salary_requires_review() -> None:
    result = RedFlagScanner().scan(
        make_posting(base_salary_min_usd=None, base_salary_max_usd=None)
    )

    assert RedFlagCode.SALARY_UNKNOWN in {flag.code for flag in result.review_flags}


def test_known_sponsor_silence_does_not_create_sponsorship_review() -> None:
    scanner = RedFlagScanner(known_sponsor_companies=("Apple",))
    result = scanner.scan(
        make_posting(
            company="Apple Inc.",
            sponsorship_status=SponsorshipStatus.UNKNOWN,
        )
    )

    assert RedFlagCode.SPONSORSHIP_UNKNOWN not in {
        flag.code for flag in result.review_flags
    }


def test_known_sponsor_never_overrides_explicit_role_restriction() -> None:
    scanner = RedFlagScanner(known_sponsor_companies=("Apple",))
    result = scanner.scan(
        make_posting(
            company="Apple",
            description="Applicants must be authorized without future sponsorship.",
            sponsorship_status=SponsorshipStatus.UNKNOWN,
        )
    )

    assert RedFlagCode.NO_SPONSORSHIP in {flag.code for flag in result.hard_flags}


def test_known_sponsor_matching_uses_exact_corporate_identity() -> None:
    scanner = RedFlagScanner(known_sponsor_companies=("Apple",))
    legal_name = scanner.scan(
        make_posting(
            company="Apple Inc.", sponsorship_status=SponsorshipStatus.UNKNOWN
        )
    )
    unrelated_prefix = scanner.scan(
        make_posting(
            company="Apple Orchard Labs",
            sponsorship_status=SponsorshipStatus.UNKNOWN,
        )
    )

    assert RedFlagCode.SPONSORSHIP_UNKNOWN not in {
        flag.code for flag in legal_name.review_flags
    }
    assert RedFlagCode.SPONSORSHIP_UNKNOWN in {
        flag.code for flag in unrelated_prefix.review_flags
    }
