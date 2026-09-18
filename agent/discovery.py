"""Daily search, scoring, persistence, outreach, and digest orchestration."""

from __future__ import annotations

import argparse
import asyncio
import os
from dataclasses import dataclass
from itertools import zip_longest
from pathlib import Path

from dotenv import load_dotenv
from pydantic import BaseModel, ConfigDict

from agent.digest import Digest, DigestOpportunity, build_digest
from agent.email_delivery import EmailSender, create_email_sender
from agent.outreach import build_outreach
from agent.posting_extractor import (
    ClosedJobPostingError,
    PostingExtractionError,
    PostingExtractor,
)
from agent.search_plan import DiscoveryTrack, configured_discovery_tracks
from agent.search_providers import SearchProvider, SearchResult, create_search_provider
from backend.job_store import (
    purge_rejected_jobs,
    JobStatus,
    discovery_track_is_due,
    find_existing_job,
    list_availability_check_candidates,
    list_due_follow_ups,
    list_pending_digest_jobs,
    mark_job_closed_by_url,
    mark_job_availability_checked,
    mark_digest_jobs_sent,
    mark_follow_ups_sent,
    record_discovery_track_run,
    save_scored_job,
    touch_job_seen,
    update_job_status,
    weekly_missing_skills,
)
from backend.location_policy import is_us_based, us_location_sort_key
from backend.red_flag_scanner import is_internship
from backend.discovery_priority import priority_key
from backend.resume_store import (
    AUTO_PROFILE_ID,
    ResumeCatalog,
    ResumeProfile,
    ResumeStatus,
    configured_resume_catalog,
    select_resume_profile,
)
from backend.scoring_engine import (
    AllScoringProvidersFailedError,
    ScoreVerdict,
    ScoringEngine,
    create_default_scoring_engine,
)

DEFAULT_SEARCH_QUERIES = (
    '"senior software engineer" "visa sponsorship" USA',
    '"platform engineer" "visa sponsorship" USA',
    '"developer experience engineer" USA',
    '"senior SDET" "visa sponsorship" USA',
    '"senior automation engineer" "visa sponsorship" USA',
    '"AI automation engineer" USA',
    'site:jobs.apple.com "quality engineering" maps automation',
    'site:jobs.ashbyhq.com/glean "forward deployed engineer"',
    'site:jobs.lever.co/cohere "forward deployed engineer"',
    'site:job-boards.greenhouse.io/scaleai "forward deployed engineer"',
)
DEFAULT_MAX_SCORING_JOBS_PER_RUN = 50


@dataclass
class ScoringBudget:
    """Shared scheduled-run ceiling for postings that can invoke a model."""

    limit: int
    used: int = 0

    def __post_init__(self) -> None:
        if self.limit < 1:
            raise ValueError("Scoring budget limit must be at least 1.")

    def acquire(self) -> bool:
        if self.used >= self.limit:
            return False
        self.used += 1
        return True


class DiscoveryReport(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    searched_results: int
    duplicate_results: int
    extraction_failures: int
    rejected_results: int
    below_threshold_results: int
    opportunities: tuple[DigestOpportunity, ...]
    errors: tuple[str, ...]
    changed_results: int = 0
    deferred_results: int = 0
    tracks_run: tuple[str, ...] = ()
    tracks_skipped: tuple[str, ...] = ()
    failed_tracks: tuple[str, ...] = ()
    compiled_opportunities: int = 0
    follow_up_reminders: int = 0
    email_sent: bool = False


class DiscoveryAgent:
    def __init__(
        self,
        *,
        search_provider: SearchProvider,
        extractor: PostingExtractor,
        scoring_engine: ScoringEngine,
        resume: ResumeProfile | None = None,
        resume_catalog: ResumeCatalog | None = None,
        profile_id: str = AUTO_PROFILE_ID,
        discovery_track: str | None = None,
        scoring_budget: ScoringBudget | None = None,
        database_path: str | Path | None = None,
    ) -> None:
        if (resume is None) == (resume_catalog is None):
            raise ValueError("Provide exactly one resume or resume_catalog.")
        self.search_provider = search_provider
        self.extractor = extractor
        self.scoring_engine = scoring_engine
        self.resume = resume
        self.resume_catalog = resume_catalog
        self.profile_id = profile_id
        self.discovery_track = discovery_track
        self.scoring_budget = scoring_budget
        self.database_path = database_path

    async def run(
        self, queries: tuple[str, ...], *, limit: int = 20
    ) -> DiscoveryReport:
        if self.resume is not None and self.resume.status is not ResumeStatus.READY:
            raise ValueError(
                f"Resume profile {self.resume.profile_id!r} must be READY before discovery."
            )
        if self.resume_catalog is not None and not self.resume_catalog.list_profiles(
            ready_only=True
        ):
            raise ValueError("At least one READY resume profile is required for discovery.")
        if not queries or any(not query.strip() for query in queries):
            raise ValueError("At least one non-blank search query is required.")
        if limit < 1:
            raise ValueError("limit must be at least 1.")

        searched = duplicates = extraction_failures = rejected = below = changed = 0
        deferred = 0
        opportunities: list[DigestOpportunity] = []
        errors: list[str] = []
        seen_urls: set[str] = set()
        query_budgets = _query_result_budgets(len(queries), limit)
        result_groups: list[tuple[SearchResult, ...]] = []

        for query, query_limit in zip(queries, query_budgets, strict=True):
            if query_limit == 0:
                continue
            try:
                results = await self.search_provider.search(
                    query, limit=query_limit
                )
            except Exception as error:
                errors.append(f"Search query {query!r} failed: {error}")
                continue
            result_groups.append(tuple(sorted(results, key=priority_key)))

        # Interleave queries so a shared scoring ceiling cannot let the first
        # role family, location, or priority company starve every later query.
        for result_round in zip_longest(*result_groups):
            for search_result in result_round:
                if search_result is None:
                    continue
                if searched >= limit:
                    break
                searched += 1
                if search_result.url in seen_urls:
                    duplicates += 1
                    continue
                seen_urls.add(search_result.url)
                if is_internship(search_result.title):
                    rejected += 1
                    continue

                try:
                    posting = await self.extractor.fetch(search_result)
                except ClosedJobPostingError as error:
                    rejected += 1
                    await mark_job_closed_by_url(
                        search_result.url,
                        str(error),
                        source_job_id=search_result.source_job_id,
                        ats_name=search_result.ats_name,
                        database_path=self.database_path,
                    )
                    continue
                except PostingExtractionError as error:
                    extraction_failures += 1
                    errors.append(str(error))
                    continue

                if is_internship(posting.title, posting.employment_type):
                    rejected += 1
                    continue
                if not is_us_based(
                    posting.location,
                    posting.workplace_type,
                    posting.description,
                ):
                    rejected += 1
                    continue

                existing = await find_existing_job(
                    posting,
                    url=search_result.url,
                    ats_name=search_result.ats_name,
                    source_job_id=search_result.source_job_id,
                    database_path=self.database_path,
                )
                if existing is not None and not existing.changed:
                    duplicates += 1
                    await touch_job_seen(existing.job_id, database_path=self.database_path)
                    continue
                if existing is not None:
                    changed += 1

                resume = self.resume or select_resume_profile(
                    self.resume_catalog,
                    posting.searchable_text,
                    self.profile_id,
                )
                prescan = self.scoring_engine.scanner.scan(posting)
                if (
                    not prescan.rejected
                    and self.scoring_budget is not None
                    and not self.scoring_budget.acquire()
                ):
                    deferred += 1
                    continue
                try:
                    score = await self.scoring_engine.score(posting, resume)
                except AllScoringProvidersFailedError as error:
                    errors.append(f"Scoring {search_result.url} failed: {error}")
                    continue

                job_id = await save_scored_job(
                    posting,
                    score,
                    source=self.search_provider.name,
                    url=search_result.url,
                    source_job_id=search_result.source_job_id,
                    ats_name=search_result.ats_name,
                    discovery_track=self.discovery_track,
                    database_path=self.database_path,
                )
                if score.verdict is ScoreVerdict.REJECTED:
                    rejected += 1
                    await purge_rejected_jobs(database_path=self.database_path)
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
                        outreach=build_outreach(posting, score, resume),
                    )
                )
            if searched >= limit:
                break

        opportunities.sort(
            key=lambda item: (
                *us_location_sort_key(
                    item.posting.location,
                    item.posting.workplace_type,
                    item.posting.description,
                ),
                -item.score.overall_score,
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
            changed_results=changed,
            deferred_results=deferred,
        )


def configured_queries(overrides: tuple[str, ...] = ()) -> tuple[str, ...]:
    if overrides:
        return overrides
    configured = os.getenv("JOB_SEARCH_QUERIES", "")
    queries = tuple(query.strip() for query in configured.split("|") if query.strip())
    return queries or DEFAULT_SEARCH_QUERIES


def _query_result_budgets(query_count: int, limit: int) -> tuple[int, ...]:
    """Distribute the full result budget predictably across every query."""

    if query_count < 1:
        raise ValueError("query_count must be at least 1.")
    base, remainder = divmod(limit, query_count)
    return tuple(base + (1 if index < remainder else 0) for index in range(query_count))


async def audit_stored_job_availability(
    extractor: PostingExtractor,
    *,
    limit: int = 20,
    minimum_age_hours: int = 12,
    database_path: str | Path | None = None,
) -> tuple[int, tuple[str, ...]]:
    """Refresh a bounded set of valuable stored roles without blocking discovery."""

    await purge_rejected_jobs(database_path=database_path)
    candidates = await list_availability_check_candidates(
        limit=limit,
        minimum_age_hours=minimum_age_hours,
        database_path=database_path,
    )
    semaphore = asyncio.Semaphore(5)

    async def check(candidate) -> tuple[bool, str | None]:
        async with semaphore:
            try:
                await extractor.fetch(
                    SearchResult(
                        title=candidate.title,
                        url=candidate.url,
                        source=candidate.source,
                    )
                )
            except ClosedJobPostingError as error:
                await mark_job_closed_by_url(
                    candidate.url,
                    str(error),
                    database_path=database_path,
                )
                return True, None
            except PostingExtractionError as error:
                await mark_job_availability_checked(
                    candidate.id,
                    database_path=database_path,
                )
                return False, f"Availability check {candidate.url} failed: {error}"
            await mark_job_availability_checked(
                candidate.id,
                database_path=database_path,
            )
            return False, None

    outcomes = await asyncio.gather(*(check(candidate) for candidate in candidates))
    return (
        sum(1 for closed, _ in outcomes if closed),
        tuple(error for _, error in outcomes if error is not None),
    )


async def run_discovery(
    *,
    queries: tuple[str, ...],
    limit: int,
    send_email: bool,
    email_sender: EmailSender | None = None,
) -> tuple[DiscoveryReport, Digest]:
    """Run one explicitly selected legacy provider/query set."""

    catalog = configured_resume_catalog()
    agent = DiscoveryAgent(
        search_provider=create_search_provider(),
        extractor=PostingExtractor(),
        scoring_engine=create_default_scoring_engine(),
        resume_catalog=catalog,
        profile_id=os.getenv("RESUME_PROFILE_ID", AUTO_PROFILE_ID),
    )
    report = await agent.run(queries, limit=limit)
    _, audit_errors = await audit_stored_job_availability(
        agent.extractor,
        limit=int(os.getenv("AVAILABILITY_CHECK_LIMIT", "20")),
        minimum_age_hours=int(os.getenv("AVAILABILITY_CHECK_INTERVAL_HOURS", "12")),
    )
    if audit_errors:
        report = report.model_copy(update={"errors": report.errors + audit_errors})
    return await _compile_and_optionally_deliver(
        report, catalog=catalog, send_email=send_email, email_sender=email_sender
    )


async def run_scheduled_discovery(
    *,
    limit: int,
    send_email: bool,
    force_tracks: bool = False,
    email_sender: EmailSender | None = None,
    tracks: tuple[DiscoveryTrack, ...] | None = None,
) -> tuple[DiscoveryReport, Digest]:
    """Run each due discovery source and then compile one combined digest."""

    catalog = configured_resume_catalog()
    extractor = PostingExtractor()
    scoring_engine = create_default_scoring_engine()
    scoring_budget = ScoringBudget(
        int(
            os.getenv(
                "MAX_SCORING_JOBS_PER_RUN",
                str(DEFAULT_MAX_SCORING_JOBS_PER_RUN),
            )
        )
    )
    configured_tracks = tracks or configured_discovery_tracks(result_limit=limit)
    reports: list[DiscoveryReport] = []
    tracks_run: list[str] = []
    tracks_skipped: list[str] = []
    failed_tracks: list[str] = []
    orchestration_errors: list[str] = []

    if not configured_tracks:
        orchestration_errors.append(
            "No discovery tracks are configured. Add at least one ATS/career source "
            "or a real BRAVE_SEARCH_API_KEY."
        )
        failed_tracks.append("configuration")

    for track in configured_tracks:
        due = force_tracks or await discovery_track_is_due(
            track.name, track.interval_hours
        )
        if not due:
            tracks_skipped.append(track.name)
            continue
        tracks_run.append(track.name)
        await record_discovery_track_run(track.name, "RUNNING")
        try:
            agent = DiscoveryAgent(
                search_provider=track.provider,
                extractor=extractor,
                scoring_engine=scoring_engine,
                resume_catalog=catalog,
                profile_id=os.getenv("RESUME_PROFILE_ID", AUTO_PROFILE_ID),
                discovery_track=track.name,
                scoring_budget=scoring_budget,
            )
            report = await agent.run(track.queries, limit=track.result_limit)
            reports.append(report)
            all_searches_failed = (
                report.searched_results == 0
                and len(report.errors) >= len(track.queries)
            )
            if all_searches_failed:
                message = (
                    f"{len(report.errors)} search errors: "
                    + "; ".join(report.errors)
                )
                await record_discovery_track_run(track.name, "FAILED", error=message)
                failed_tracks.append(track.name)
            else:
                await record_discovery_track_run(track.name, "SUCCEEDED")
        except Exception as error:
            message = f"Discovery track {track.name!r} failed: {error}"
            orchestration_errors.append(message)
            failed_tracks.append(track.name)
            await record_discovery_track_run(track.name, "FAILED", error=str(error))

    _, audit_errors = await audit_stored_job_availability(
        extractor,
        limit=int(os.getenv("AVAILABILITY_CHECK_LIMIT", "20")),
        minimum_age_hours=int(os.getenv("AVAILABILITY_CHECK_INTERVAL_HOURS", "12")),
    )
    orchestration_errors.extend(audit_errors)

    report = _combine_reports(
        reports,
        extra_errors=tuple(orchestration_errors),
        tracks_run=tuple(tracks_run),
        tracks_skipped=tuple(tracks_skipped),
        failed_tracks=tuple(failed_tracks),
    )
    return await _compile_and_optionally_deliver(
        report, catalog=catalog, send_email=send_email, email_sender=email_sender
    )


async def _compile_and_optionally_deliver(
    report: DiscoveryReport,
    *,
    catalog: ResumeCatalog,
    send_email: bool,
    email_sender: EmailSender | None,
) -> tuple[DiscoveryReport, Digest]:
    weekly_gaps = await weekly_missing_skills()
    digest = build_digest(report.opportunities, weekly_gaps=weekly_gaps)
    if send_email:
        recipient = os.getenv("DIGEST_RECIPIENT_EMAIL", "").strip()
        if not recipient:
            raise ValueError("DIGEST_RECIPIENT_EMAIL is required with --send-email.")
        pending = await list_pending_digest_jobs()
        compiled = tuple(
            DigestOpportunity(
                job_id=job.id,
                url=job.url,
                posting=job.posting,
                score=job.score,
                outreach=build_outreach(
                    job.posting,
                    job.score,
                    _resume_for_score(catalog, job.score.resume_profile_id),
                ),
            )
            for job in pending
        )
        follow_ups = await list_due_follow_ups()
        digest = build_digest(
            compiled,
            follow_ups=follow_ups,
            weekly_gaps=weekly_gaps,
        )
        send_empty = os.getenv("SEND_EMPTY_DIGEST", "false").casefold() in {
            "1",
            "true",
            "yes",
        }
        email_sent = bool(compiled or follow_ups or send_empty)
        if email_sent:
            sender = email_sender or create_email_sender()
            await sender.send(digest, recipient)
            await mark_digest_jobs_sent(tuple(item.job_id for item in compiled))
            await mark_follow_ups_sent(tuple(item.job_id for item in follow_ups))
        report = report.model_copy(
            update={
                "compiled_opportunities": len(compiled),
                "follow_up_reminders": len(follow_ups),
                "email_sent": email_sent,
            }
        )
    return report, digest


def _resume_for_score(
    catalog: ResumeCatalog,
    profile_id: str | None,
) -> ResumeProfile | None:
    if not profile_id:
        return None
    try:
        return catalog.get(profile_id)
    except LookupError:
        return None


def _combine_reports(
    reports: list[DiscoveryReport],
    *,
    extra_errors: tuple[str, ...] = (),
    tracks_run: tuple[str, ...] = (),
    tracks_skipped: tuple[str, ...] = (),
    failed_tracks: tuple[str, ...] = (),
) -> DiscoveryReport:
    return DiscoveryReport(
        searched_results=sum(report.searched_results for report in reports),
        duplicate_results=sum(report.duplicate_results for report in reports),
        extraction_failures=sum(report.extraction_failures for report in reports),
        rejected_results=sum(report.rejected_results for report in reports),
        below_threshold_results=sum(
            report.below_threshold_results for report in reports
        ),
        changed_results=sum(report.changed_results for report in reports),
        deferred_results=sum(report.deferred_results for report in reports),
        opportunities=tuple(
            opportunity for report in reports for opportunity in report.opportunities
        ),
        errors=tuple(error for report in reports for error in report.errors)
        + extra_errors,
        tracks_run=tracks_run,
        tracks_skipped=tracks_skipped,
        failed_tracks=failed_tracks,
    )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Discover and score current jobs.")
    parser.add_argument(
        "--query",
        action="append",
        default=[],
        help="Search query; repeat for multiple queries.",
    )
    parser.add_argument("--limit", type=int, default=60)
    parser.add_argument(
        "--send-email",
        action="store_true",
        help="Send the digest using EMAIL_PROVIDER; omitted means dry-run.",
    )
    parser.add_argument(
        "--force-all-tracks",
        action="store_true",
        help="Run every configured track now, ignoring its durable interval state.",
    )
    return parser


def main() -> int:
    load_dotenv()
    args = _parser().parse_args()
    if args.query:
        operation = run_discovery(
            queries=configured_queries(tuple(args.query)),
            limit=args.limit,
            send_email=args.send_email,
        )
    else:
        operation = run_scheduled_discovery(
            limit=args.limit,
            send_email=args.send_email,
            force_tracks=args.force_all_tracks,
        )
    report, digest = asyncio.run(operation)
    print(digest.text)
    print(
        "\nRun summary: "
        f"searched={report.searched_results}, "
        f"duplicates={report.duplicate_results}, "
        f"changed={report.changed_results}, "
        f"deferred={report.deferred_results}, "
        f"qualified={len(report.opportunities)}, "
        f"rejected={report.rejected_results}, "
        f"below_threshold={report.below_threshold_results}, "
        f"errors={len(report.errors)}"
    )
    if report.tracks_run:
        print("Tracks run: " + ", ".join(report.tracks_run))
    if report.tracks_skipped:
        print("Tracks not due: " + ", ".join(report.tracks_skipped))
    if report.failed_tracks:
        print("Failed tracks: " + ", ".join(report.failed_tracks))
    for error in report.errors:
        print(f"Warning: {error}")
    if report.email_sent:
        print("Digest sent.")
    elif args.send_email:
        print("No pending roles or follow-ups; email skipped.")
    else:
        print("Dry run only; no email sent.")
    return 2 if report.failed_tracks else 0


if __name__ == "__main__":
    raise SystemExit(main())
