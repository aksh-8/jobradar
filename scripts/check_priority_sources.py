"""Check live priority-company search coverage without running scoring."""

from __future__ import annotations

import argparse
import asyncio

from dotenv import load_dotenv

from agent.search_plan import configured_discovery_tracks


async def main(*, google_jobs: bool = False) -> int:
    load_dotenv()
    track_name = "priority_google_jobs" if google_jobs else "priority_company_search"
    track = next(
        (
            item
            for item in configured_discovery_tracks(result_limit=10)
            if item.name == track_name
        ),
        None,
    )
    if track is None:
        print(
            f"{track_name} is not configured. Check PRIORITY_COMPANIES, "
            "CUSTOM_CAREER_PAGES, and the corresponding provider key."
        )
        return 1

    results = await asyncio.gather(
        *(track.provider.search(query, limit=3) for query in track.queries),
        return_exceptions=True,
    )
    failures = 0
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
        help="Check structured SerpAPI Google Jobs coverage instead of Brave.",
    )
    arguments = parser.parse_args()
    raise SystemExit(asyncio.run(main(google_jobs=arguments.google_jobs)))
