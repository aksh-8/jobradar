"""Check live priority-company search coverage without running scoring."""

from __future__ import annotations

import argparse
import asyncio

from dotenv import load_dotenv

from agent.search_plan import configured_discovery_tracks


async def main(*, google_jobs: bool = False) -> int:
    load_dotenv()
    expected = (
        ("priority_google_jobs",)
        if google_jobs
        else ("apple_careers_search", "rotating_priority_search")
    )
    tracks = tuple(
        item
        for item in configured_discovery_tracks(result_limit=10)
        if item.name in expected
    )
    if not tracks:
        print(
            f"{', '.join(expected)} is not configured. Check PRIORITY_COMPANIES, "
            "CUSTOM_CAREER_PAGES, and the corresponding provider key."
        )
        return 1

    failures = 0
    for track in tracks:
        results = await asyncio.gather(
            *(track.provider.search(query, limit=3) for query in track.queries),
            return_exceptions=True,
        )
        for query, rows in zip(track.queries, results, strict=True):
            company = getattr(track.provider, "pages", {}).get(query, query)
            if isinstance(rows, Exception):
                failures += 1
                print(f"{company}: ERROR - {rows}")
                continue
            titles = " | ".join(row.title for row in rows) or "no matching roles"
            print(f"{company}: {len(rows)} - {titles}")
    return 1 if failures else 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--google-jobs",
        action="store_true",
        help="Check optional legacy SerpAPI coverage instead of scheduled Brave.",
    )
    arguments = parser.parse_args()
    raise SystemExit(asyncio.run(main(google_jobs=arguments.google_jobs)))
