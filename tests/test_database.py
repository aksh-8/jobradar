"""Tests for the JobRadar SQLite schema and migration runner."""

from pathlib import Path

import aiosqlite
import pytest

from backend.database import (
    LATEST_SCHEMA_VERSION,
    database_connection,
    initialize_database,
)


@pytest.mark.asyncio
async def test_initialize_database_creates_schema_and_is_idempotent(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "nested" / "jobradar.db"

    first_version = await initialize_database(database_path)
    second_version = await initialize_database(database_path)

    assert first_version == LATEST_SCHEMA_VERSION
    assert second_version == LATEST_SCHEMA_VERSION
    assert database_path.is_file()

    async with database_connection(database_path) as connection:
        cursor = await connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'"
        )
        tables = {row["name"] for row in await cursor.fetchall()}
        assert {
            "schema_migrations", "jobs", "job_events", "discovery_track_runs"
        } <= tables

        cursor = await connection.execute(
            "SELECT version, name FROM schema_migrations"
        )
        migrations = await cursor.fetchall()
        assert sorted((row["version"], row["name"]) for row in migrations) == [
            (1, "create_job_tracking_schema"),
            (2, "add_digest_and_follow_up_tracking"),
            (3, "add_multisource_discovery_identity"),
            (4, "add_job_availability_tracking"),
            (5, "add_availability_audit_schedule"),
        ]


@pytest.mark.asyncio
async def test_job_constraints_reject_invalid_values(tmp_path: Path) -> None:
    database_path = tmp_path / "jobradar.db"

    async with database_connection(database_path) as connection:
        with pytest.raises(aiosqlite.IntegrityError):
            await connection.execute(
                """
                INSERT INTO jobs (
                    deduplication_key, source, url, title, company, overall_score
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                ("job-1", "test", "https://example.com/1", "Engineer", "Acme", 101),
            )

        await connection.rollback()

        with pytest.raises(aiosqlite.IntegrityError):
            await connection.execute(
                """
                INSERT INTO jobs (
                    deduplication_key, source, url, title, company,
                    salary_min_usd, salary_max_usd
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    "job-2",
                    "test",
                    "https://example.com/2",
                    "Engineer",
                    "Acme",
                    180_000,
                    140_000,
                ),
            )


@pytest.mark.asyncio
async def test_job_events_enforce_foreign_keys_and_cascade(tmp_path: Path) -> None:
    database_path = tmp_path / "jobradar.db"

    async with database_connection(database_path) as connection:
        cursor = await connection.execute(
            """
            INSERT INTO jobs (deduplication_key, source, url, title, company)
            VALUES (?, ?, ?, ?, ?)
            """,
            ("job-1", "test", "https://example.com/1", "Engineer", "Acme"),
        )
        job_id = cursor.lastrowid
        assert job_id is not None

        await connection.execute(
            "INSERT INTO job_events (job_id, event_type) VALUES (?, ?)",
            (job_id, "DISCOVERED"),
        )
        await connection.commit()

        await connection.execute("DELETE FROM jobs WHERE id = ?", (job_id,))
        await connection.commit()

        cursor = await connection.execute(
            "SELECT COUNT(*) AS event_count FROM job_events WHERE job_id = ?",
            (job_id,),
        )
        row = await cursor.fetchone()
        assert row is not None
        assert row["event_count"] == 0

        with pytest.raises(aiosqlite.IntegrityError):
            await connection.execute(
                "INSERT INTO job_events (job_id, event_type) VALUES (?, ?)",
                (999_999, "VIEWED"),
            )
