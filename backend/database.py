"""SQLite connection management and schema migrations for JobRadar.

All application components use this module to open the same database.  Schema
changes are applied in order and recorded in ``schema_migrations`` so startup is
safe to repeat.
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path

import aiosqlite

DEFAULT_DATABASE_PATH = Path("data/jobradar.db")


class DatabaseVersionError(RuntimeError):
    """Raised when a database was created by a newer JobRadar version."""


@dataclass(frozen=True)
class Migration:
    """One atomic database schema migration."""

    version: int
    name: str
    statements: Sequence[str]


MIGRATIONS = (
    Migration(
        version=1,
        name="create_job_tracking_schema",
        statements=(
            """
            CREATE TABLE jobs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                deduplication_key TEXT NOT NULL UNIQUE
                    CHECK (length(trim(deduplication_key)) > 0),
                source TEXT NOT NULL CHECK (length(trim(source)) > 0),
                source_job_id TEXT,
                url TEXT NOT NULL CHECK (length(trim(url)) > 0),
                title TEXT NOT NULL CHECK (length(trim(title)) > 0),
                company TEXT NOT NULL CHECK (length(trim(company)) > 0),
                location TEXT,
                description TEXT,
                employment_type TEXT,
                workplace_type TEXT,
                salary_min_usd INTEGER CHECK (salary_min_usd >= 0),
                salary_max_usd INTEGER CHECK (salary_max_usd >= 0),
                company_size INTEGER CHECK (company_size >= 0),
                sponsorship_status TEXT NOT NULL DEFAULT 'UNKNOWN'
                    CHECK (sponsorship_status IN ('YES', 'NO', 'UNKNOWN')),
                overall_score INTEGER CHECK (overall_score BETWEEN 0 AND 100),
                score_details TEXT,
                status TEXT NOT NULL DEFAULT 'DISCOVERED'
                    CHECK (status IN ('DISCOVERED', 'VIEWED', 'SKIPPED', 'APPLIED')),
                skip_reason TEXT,
                first_discovered_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                last_seen_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                UNIQUE (source, source_job_id),
                CHECK (
                    salary_min_usd IS NULL
                    OR salary_max_usd IS NULL
                    OR salary_max_usd >= salary_min_usd
                )
            )
            """,
            """
            CREATE TABLE job_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                job_id INTEGER NOT NULL,
                event_type TEXT NOT NULL
                    CHECK (event_type IN ('DISCOVERED', 'VIEWED', 'SKIPPED', 'APPLIED')),
                details TEXT,
                occurred_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (job_id) REFERENCES jobs(id) ON DELETE CASCADE
            )
            """,
            """
            CREATE INDEX idx_jobs_status_score
            ON jobs(status, overall_score DESC)
            """,
            """
            CREATE INDEX idx_jobs_last_seen
            ON jobs(last_seen_at DESC)
            """,
            """
            CREATE INDEX idx_job_events_job_time
            ON job_events(job_id, occurred_at DESC)
            """,
        ),
    ),
)

LATEST_SCHEMA_VERSION = MIGRATIONS[-1].version


def resolve_database_path(database_path: str | Path | None = None) -> Path:
    """Resolve an explicit path or the ``DATABASE_PATH`` environment setting."""

    configured_path = database_path or os.getenv("DATABASE_PATH") or DEFAULT_DATABASE_PATH
    return Path(configured_path).expanduser().resolve()


async def connect_database(
    database_path: str | Path | None = None,
) -> aiosqlite.Connection:
    """Open a configured SQLite connection without changing its schema."""

    path = resolve_database_path(database_path)
    path.parent.mkdir(parents=True, exist_ok=True)

    connection = await aiosqlite.connect(path)
    connection.row_factory = aiosqlite.Row
    await connection.execute("PRAGMA foreign_keys = ON")
    await connection.execute("PRAGMA busy_timeout = 5000")
    await connection.execute("PRAGMA journal_mode = WAL")
    return connection


async def migrate_database(connection: aiosqlite.Connection) -> int:
    """Apply all pending migrations atomically and return the schema version."""

    await connection.execute("BEGIN IMMEDIATE")
    try:
        await connection.execute(
            """
            CREATE TABLE IF NOT EXISTS schema_migrations (
                version INTEGER PRIMARY KEY CHECK (version > 0),
                name TEXT NOT NULL UNIQUE,
                applied_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        cursor = await connection.execute(
            "SELECT version, name FROM schema_migrations ORDER BY version"
        )
        applied_rows = await cursor.fetchall()
        applied = {int(row["version"]): str(row["name"]) for row in applied_rows}

        if applied and max(applied) > LATEST_SCHEMA_VERSION:
            raise DatabaseVersionError(
                "Database schema version "
                f"{max(applied)} is newer than supported version {LATEST_SCHEMA_VERSION}."
            )

        for migration in MIGRATIONS:
            applied_name = applied.get(migration.version)
            if applied_name is not None:
                if applied_name != migration.name:
                    raise DatabaseVersionError(
                        f"Migration {migration.version} is recorded as "
                        f"{applied_name!r}, expected {migration.name!r}."
                    )
                continue

            for statement in migration.statements:
                await connection.execute(statement)
            await connection.execute(
                "INSERT INTO schema_migrations (version, name) VALUES (?, ?)",
                (migration.version, migration.name),
            )

        await connection.commit()
    except BaseException:
        await connection.rollback()
        raise

    return LATEST_SCHEMA_VERSION


async def initialize_database(database_path: str | Path | None = None) -> int:
    """Create or upgrade a JobRadar database and close the setup connection."""

    connection = await connect_database(database_path)
    try:
        return await migrate_database(connection)
    finally:
        await connection.close()


@asynccontextmanager
async def database_connection(
    database_path: str | Path | None = None,
) -> AsyncIterator[aiosqlite.Connection]:
    """Yield an initialized database connection and always close it."""

    connection = await connect_database(database_path)
    try:
        await migrate_database(connection)
        yield connection
    finally:
        await connection.close()
