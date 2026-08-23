"""Tests for strict US eligibility and user-preferred location ordering."""

import pytest

from backend.location_policy import (
    UsLocationTier,
    classify_us_location,
    is_us_based,
    us_location_sort_key,
)


@pytest.mark.parametrize(
    ("location", "workplace_type", "expected"),
    (
        ("Los Angeles, CA", None, UsLocationTier.LOS_ANGELES),
        ("Santa Monica, California", None, UsLocationTier.LOS_ANGELES),
        ("Remote, United States", "Remote", UsLocationTier.REMOTE),
        ("San Francisco, CA", None, UsLocationTier.CALIFORNIA),
        ("New York, NY", None, UsLocationTier.EAST_COAST),
        ("Boston, Massachusetts", None, UsLocationTier.EAST_COAST),
        ("Plano, Texas, United States", None, UsLocationTier.REST_OF_US),
    ),
)
def test_classifies_us_location_tiers(
    location: str,
    workplace_type: str | None,
    expected: UsLocationTier,
) -> None:
    assert classify_us_location(location, workplace_type) is expected


@pytest.mark.parametrize(
    "location",
    (
        None,
        "",
        "Remote",
        "Bengaluru, India",
        "Argentina; Uruguay",
        "London, United Kingdom",
        "Toronto, Canada",
        "Remote - EMEA",
    ),
)
def test_rejects_foreign_or_unverified_locations(location: str | None) -> None:
    assert not is_us_based(location)


def test_accepts_remote_only_when_description_verifies_united_states() -> None:
    assert is_us_based(
        "Remote",
        "Remote",
        "This role is remote within the United States.",
    )
    assert not is_us_based("Remote", "Remote", "Work from anywhere in Europe.")


def test_location_sort_order_matches_user_priority() -> None:
    locations = (
        "Austin, TX",
        "New York, NY",
        "San Francisco, CA",
        "Remote, United States",
        "Los Angeles, CA",
    )

    assert sorted(locations, key=us_location_sort_key) == [
        "Los Angeles, CA",
        "Remote, United States",
        "San Francisco, CA",
        "New York, NY",
        "Austin, TX",
    ]
