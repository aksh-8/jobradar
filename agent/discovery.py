"""Daily search, scoring, persistence, outreach, and digest orchestration."""

from __future__ import annotations

import argparse
import asyncio
import os
from pathlib import Path

from dotenv import load_dotenv
from pydantic import BaseModel, ConfigDict

from agent.digest import Digest, DigestOpportunity, build_digest
from agent.email_delivery import EmailSender, create_email_sender
from agent.outreach import build_outreach
from agent.posting_extractor import PostingExtractionError, PostingExtractor
from agent.search_providers import SearchProvider, create_search_provider
from backend.job_store import JobStatus, job_exists, save_scored_job, update_job_status
from backend.resume_store import (
    DEFAULT_PROFILE_ID,
    ResumeProfile,
    ResumeStatus,
    get_resume_profile,
)
from backend.scoring_engine import (
    AllScoringProvidersFailedError,
    ScoreVerdict,
    ScoringEngine,
    create_default_scoring_engine,
)

DEFAULT_SEARCH_QUERIES = (
    '"backend engineer" "visa sponsorship"',
    '"platform engineer" "visa sponsorship"',
    '"software engineer" "visa sponsorship" "$140,000"',
)


class DiscoveryReport(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    searched_results: int
    duplicate_results: int
    extraction_failures: int
    rejected_results: int
    below_threshold_results: int
    opportunities: tuple[DigestOpportunity, ...]
    errors: tuple[str, ...]


class DiscoveryAgent:
    def __init__(
        self,
        *,
        search_provider: SearchProvider,
        extractor: PostingExtractor,
        scoring_engine: ScoringEngine,
        resume: ResumeProfile,
        database_path: str | Path | None = None,
    ) -> None:
        self.search_provider = search_provider
        self.extractor = extractor
        self.scoring_engine = scoring_engine
        self.resume = resume
        self.database_path = database_path

    async def run(
        self, queries: tuple[str, ...], *, limit: int = 20
    ) -> DiscoveryReport:
        if self.resume.status is not ResumeStatus.READY:
            raise ValueError(
                f"Resume profile {self.resume.profile_id!r} must be READY before discovery."
            )
        if not queries or any(not query.strip() for query in queries):
            raise ValueError("At least one non-blank search query is required.")
        if limit < 1:
            raise ValueError("limit must be at least 1.")

        searched = duplicates = extraction_failures = rejected = below = 0
        opportunities: list[DigestOpportunity] = []
        errors: list[str] = []
        seen_urls: set[str] = set()

        for query in queries:
            if searched >= limit:
                break
            try:
                results = await self.search_provider.search(
                    query, limit=max(1, limit - searched)
                )
            except Exception as error:
                errors.append(f"Search query {query!r} failed: {error}")
                continue
            for search_result in results:
                if searched >= limit:
                    break
                searched += 1
                if search_result.url in seen_urls or await job_exists(
                    search_result.url, database_path=self.database_path
                ):
                    duplicates += 1
                    continue
                seen_urls.add(search_result.url)

                try:
                    posting = await self.extractor.fetch(search_result)
                except PostingExtractionError as error:
                    extraction_failures += 1
                    errors.append(str(error))
                    continue

                try:
                    score = await self.scoring_engine.score(posting, self.resume)
                except AllScoringProvidersFailedError as error:
                    errors.append(f"Scoring {search_result.url} failed: {error}")
                    continue

                job_id = await save_scored_job(
                    posting,
                    score,
                    source=self.search_provider.name,
                    url=search_result.url,
                    database_path=self.database_path,
                )
                if score.verdict is ScoreVerdict.REJECTED:
                    rejected += 1
                    reason = ", ".join(flag.code.value for flag in score.hard_flags)
                    await update_job_status(
                        job_id,
                        JobStatus.SKIPPED,
                        skip_reason=reason or "Rejected by hard-filter policy",
                        database_path=self.database_path,
                    )
                    continue
                if score.verdict is ScoreVerdict.BELOW_THRESHOLD:
                    below += 1
                    await update_job_status(
                        job_id,
                        JobStatus.SKIPPED,
                        skip_reason="Below configured score threshold",
                        database_path=self.database_path,
                    )
                    continue

                opportunities.append(
                    DigestOpportunity(
                        job_id=job_id,
                        url=search_result.url,
                        posting=posting,
                        score=score,
                        outreach=build_outreach(posting, score),
                    )
                )

        return DiscoveryReport(
            searched_results=searched,
            duplicate_results=duplicates,
            extraction_failures=extraction_failures,
            rejected_results=rejected,
            below_threshold_results=below,
            opportunities=tuple(opportunities),
            errors=tuple(errors),
        )


def configured_queries(overrides: tuple[str, ...] = ()) -> tuple[str, ...]:
    if overrides:
        return overrides
    configured = os.getenv("JOB_SEARCH_QUERIES", "")
    queries = tuple(query.strip() for query in configured.split("|") if query.strip())
    return queries or DEFAULT_SEARCH_QUERIES


async def run_discovery(
    *,
    queries: tuple[str, ...],
    limit: int,
    send_email: bool,
    email_sender: EmailSender | None = None,
) -> tuple[DiscoveryReport, Digest]:
    resume = get_resume_profile(os.getenv("RESUME_PROFILE_ID", DEFAULT_PROFILE_ID))
    agent = DiscoveryAgent(
        search_provider=create_search_provider(),
        extractor=PostingExtractor(),
        scoring_engine=create_default_scoring_engine(),
        resume=resume,
    )
    report = await agent.run(queries, limit=limit)
    digest = build_digest(report.opportunities)
    if send_email:
        recipient = os.getenv("DIGEST_RECIPIENT_EMAIL", "").strip()
        if not recipient:
            raise ValueError("DIGEST_RECIPIENT_EMAIL is required with --send-email.")
        sender = email_sender or create_email_sender()
        await sender.send(digest, recipient)
    return report, digest


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Discover and score current jobs.")
    parser.add_argument(
        "--query",
        action="append",
        default=[],
        help="Search query; repeat for multiple queries.",
    )
    parser.add_argument("--limit", type=int, default=20)
    parser.add_argument(
        "--send-email",
        action="store_true",
        help="Send the digest using EMAIL_PROVIDER; omitted means dry-run.",
    )
    return parser


def main() -> int:
    load_dotenv()
    args = _parser().parse_args()
    report, digest = asyncio.run(
        run_discovery(
            queries=configured_queries(tuple(args.query)),
            limit=args.limit,
            send_email=args.send_email,
        )
    )
    print(digest.text)
    print(
        "\nRun summary: "
        f"searched={report.searched_results}, "
        f"duplicates={report.duplicate_results}, "
        f"qualified={len(report.opportunities)}, "
        f"rejected={report.rejected_results}, "
        f"below_threshold={report.below_threshold_results}, "
        f"errors={len(report.errors)}"
    )
    if args.send_email:
        print("Digest sent.")
    else:
        print("Dry run only; no email sent.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
