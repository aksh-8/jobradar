"""SQLite cache for public professional contact suggestions."""

from __future__ import annotations

from pathlib import Path

from agent.contact_discovery import ContactSuggestion, ContactType
from backend.database import database_connection


async def get_cached_contact_suggestions(
    job_id: int,
    *,
    max_age_days: int = 7,
    database_path: str | Path | None = None,
) -> tuple[ContactSuggestion, ...] | None:
    if max_age_days < 1:
        raise ValueError("Contact cache age must be positive.")
    async with database_connection(database_path) as connection:
        cursor = await connection.execute(
            """
            SELECT 1
            FROM job_contact_searches
            WHERE job_id = ?
              AND datetime(searched_at) >= datetime('now', ?)
            """,
            (job_id, f"-{max_age_days} days"),
        )
        if await cursor.fetchone() is None:
            return None
        cursor = await connection.execute(
            """
            SELECT name, professional_title, contact_type, profile_url,
                   source, confidence, evidence
            FROM job_contacts
            WHERE job_id = ?
              AND datetime(discovered_at) >= datetime('now', ?)
            ORDER BY
              CASE contact_type
                WHEN 'RECRUITER' THEN 0
                WHEN 'HIRING_MANAGER' THEN 1
                ELSE 2
              END,
              confidence DESC,
              name
            """,
            (job_id, f"-{max_age_days} days"),
        )
        rows = await cursor.fetchall()
    return tuple(
        ContactSuggestion(
            name=row["name"],
            title=row["professional_title"],
            contact_type=ContactType(row["contact_type"]),
            profile_url=row["profile_url"],
            source=row["source"],
            confidence=row["confidence"],
            evidence=row["evidence"],
        )
        for row in rows
    )


async def replace_contact_suggestions(
    job_id: int,
    suggestions: tuple[ContactSuggestion, ...],
    *,
    database_path: str | Path | None = None,
) -> None:
    async with database_connection(database_path) as connection:
        cursor = await connection.execute("SELECT 1 FROM jobs WHERE id = ?", (job_id,))
        if await cursor.fetchone() is None:
            raise ValueError("Job not found.")
        await connection.execute("DELETE FROM job_contacts WHERE job_id = ?", (job_id,))
        await connection.executemany(
            """
            INSERT INTO job_contacts (
                job_id, name, professional_title, contact_type, profile_url,
                source, confidence, evidence
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            tuple(
                (
                    job_id,
                    suggestion.name,
                    suggestion.title,
                    suggestion.contact_type.value,
                    suggestion.profile_url,
                    suggestion.source,
                    suggestion.confidence,
                    suggestion.evidence,
                )
                for suggestion in suggestions
            ),
        )
        await connection.execute(
            """
            INSERT INTO job_contact_searches (job_id, searched_at, result_count)
            VALUES (?, CURRENT_TIMESTAMP, ?)
            ON CONFLICT(job_id) DO UPDATE SET
                searched_at = CURRENT_TIMESTAMP,
                result_count = excluded.result_count
            """,
            (job_id, len(suggestions)),
        )
        await connection.commit()
