"""FastAPI entry point for the JobRadar scoring service."""

from __future__ import annotations

import os
import asyncio
import logging
import re
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from ipaddress import ip_address
from pathlib import Path
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Request, Response, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import PlainTextResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, HttpUrl, model_validator

from agent.cover_letter import (
    CoverLetterGenerationError,
    GeminiCoverLetterGenerator,
)
from agent.contact_discovery import (
    ContactDiscoveryError,
    ContactSuggestion,
    PublicContactFinder,
)
from agent.outreach import build_outreach
from agent.posting_extractor import (
    ClosedJobPostingError,
    PostingExtractionError,
    PostingExtractor,
)
from agent.search_providers import SearchResult

from backend.contact_store import (
    get_cached_contact_suggestions,
    replace_contact_suggestions,
)
from backend.database import LATEST_SCHEMA_VERSION, database_connection, initialize_database
from backend.job_availability import closure_reason
from backend.job_store import (
    purge_rejected_jobs,
    JobStatus,
    StoredJob,
    delete_stored_job,
    get_availability_check_candidate,
    get_scored_job_details,
    list_jobs,
    mark_job_availability_checked,
    mark_job_closed_by_url,
    save_scored_job,
    update_job_status,
)
from backend.location_policy import is_us_based
from backend.red_flag_scanner import JobPostingFacts
from backend.resume_store import (
    AUTO_PROFILE_ID,
    ResumeCatalog,
    ResumeProfile,
    ResumeProfileNotFoundError,
    ResumeStatus,
    configured_resume_catalog,
    select_resume_profile,
)
from backend.scoring_engine import (
    AllScoringProvidersFailedError,
    DEFAULT_GEMINI_MODEL,
    ScoringEngine,
    ScoringResult,
    create_default_scoring_engine,
)

load_dotenv()


class ApiModel(BaseModel):
    """Strict base model for public API payloads."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class ScoreRequest(ApiModel):
    """A posting and the verified resume profile used to score it."""

    posting: JobPostingFacts
    profile_id: str = AUTO_PROFILE_ID
    source: str = "api"
    source_job_id: str | None = None
    url: str | None = None


class UrlScoreRequest(ApiModel):
    """A public job page to extract, score, and save."""

    url: HttpUrl
    profile_id: str = AUTO_PROFILE_ID


class ScoreResponse(ScoringResult):
    """Scoring outcome with an ID when the caller supplied a job URL."""

    job_id: int | None = None


class StatusUpdate(ApiModel):
    status: JobStatus
    skip_reason: str | None = None


class ProviderHealth(ApiModel):
    """Configuration state for one scoring provider."""

    configured: bool
    model: str


class HealthResponse(ApiModel):
    """Non-secret readiness information for local clients."""

    status: str
    database: str
    schema_version: int
    gemini: ProviderHealth
    ollama: ProviderHealth


class OutreachResponse(ApiModel):
    """Dashboard outreach content generated from persisted scoring evidence."""

    job_id: int
    title: str
    company: str
    location: str | None
    overall_score: int
    recommended_resume: str | None
    strategy: str
    recruiter_message: str
    referral_request: str
    cold_email: str
    missing_skills: tuple[str, ...]
    questions: tuple[str, ...]


class AvailabilityResponse(ApiModel):
    job_id: int
    closed: bool
    reason: str | None = None


class ContactSearchRequest(ApiModel):
    """Controls whether public contact results may be served from the local cache."""

    refresh: bool = False


class ContactSuggestionsResponse(ApiModel):
    """Ranked public leads for manual verification and outreach."""

    job_id: int
    cached: bool
    suggestions: tuple[ContactSuggestion, ...]


class CoverLetterRequest(ApiModel):
    """Either a stored job or the score payload already held by the extension."""

    job_id: int | None = None
    posting: JobPostingFacts | None = None
    score: ScoringResult | None = None

    @model_validator(mode="after")
    def source_is_complete(self):
        stored = self.job_id is not None
        inline = self.posting is not None or self.score is not None
        if stored == inline or (inline and (self.posting is None or self.score is None)):
            raise ValueError("Provide either job_id, or both posting and score.")
        return self


class DiscoveryHealthResponse(ApiModel):
    """Non-secret state from the most recent scheduled discovery invocation."""

    status: str
    last_run_at: str | None = None
    exit_code: int | None = None
    message: str


def _configured_secret(name: str) -> bool:
    value = os.getenv(name, "").strip()
    return bool(value and not value.startswith("replace_with_"))


def _cors_origins() -> list[str]:
    configured = os.getenv("CORS_ALLOWED_ORIGINS", "*")
    origins = [origin.strip() for origin in configured.split(",") if origin.strip()]
    return origins or ["*"]


def _validate_public_job_url(url: HttpUrl) -> str:
    """Reject obvious local-network targets from the URL-fetching endpoint."""

    hostname = (url.host or "").casefold().rstrip(".")
    if hostname == "localhost" or hostname.endswith(
        (".localhost", ".local", ".internal")
    ):
        raise HTTPException(
            status_code=422,
            detail="Enter a public job-posting URL.",
        )
    try:
        address = ip_address(hostname)
    except ValueError:
        pass
    else:
        if not address.is_global:
            raise HTTPException(
                status_code=422,
                detail="Enter a public job-posting URL.",
            )
    return str(url)


def _discovery_health(log_directory: Path) -> DiscoveryHealthResponse:
    status_path = log_directory / "last_run_status.txt"
    if not status_path.is_file():
        return DiscoveryHealthResponse(
            status="unknown",
            message="No scheduled discovery run has been recorded yet.",
        )
    rendered = status_path.read_text(encoding="utf-8").strip()
    match = re.search(r"^\[([^]]+)]\s+Exit code:\s+(-?\d+)", rendered)
    if match:
        exit_code = int(match.group(2))
        return DiscoveryHealthResponse(
            status="ok" if exit_code == 0 else "failed",
            last_run_at=match.group(1),
            exit_code=exit_code,
            message=(
                "Scheduled discovery completed successfully."
                if exit_code == 0
                else "Scheduled discovery failed. Check logs/errors_in_last_run.txt."
            ),
        )
    failure = re.search(r"^\[([^]]+)]\s+Runner failure:", rendered)
    return DiscoveryHealthResponse(
        status="failed" if failure else "unknown",
        last_run_at=failure.group(1) if failure else None,
        message=(
            "The discovery runner failed before it could start."
            if failure
            else "The latest discovery status could not be parsed."
        ),
    )


def create_app(
    *,
    database_path: str | Path | None = None,
    scoring_engine: ScoringEngine | None = None,
    resume_catalog: ResumeCatalog | None = None,
    posting_extractor: PostingExtractor | None = None,
    contact_finder: PublicContactFinder | None = None,
    cover_letter_generator: GeminiCoverLetterGenerator | None = None,
    runtime_log_directory: str | Path | None = None,
) -> FastAPI:
    """Build an application with replaceable dependencies for isolated tests."""

    @asynccontextmanager
    async def lifespan(application: FastAPI) -> AsyncIterator[None]:
        await initialize_database(database_path)
        await purge_rejected_jobs(database_path=database_path)
        application.state.database_path = database_path
        async def maintain_availability():
            from agent.discovery import audit_stored_job_availability
            while True:
                try:
                    await audit_stored_job_availability(
                        application.state.posting_extractor,
                        limit=int(os.getenv("AVAILABILITY_CHECK_LIMIT", "40")),
                        minimum_age_hours=2,
                        database_path=database_path,
                    )
                except Exception:
                    logging.getLogger(__name__).exception("Availability maintenance failed")
                await asyncio.sleep(900)

        maintenance = (asyncio.create_task(maintain_availability())
                       if database_path is None and posting_extractor is None else None)
        try:
            yield
        finally:
            if maintenance:
                maintenance.cancel()
                await asyncio.gather(maintenance, return_exceptions=True)

    application = FastAPI(
        title="JobRadar API",
        version="0.1.0",
        lifespan=lifespan,
    )
    application.state.scoring_engine = scoring_engine or create_default_scoring_engine()
    application.state.resume_catalog = resume_catalog or configured_resume_catalog()
    application.state.posting_extractor = posting_extractor or PostingExtractor()
    application.state.contact_finder = contact_finder or PublicContactFinder()
    application.state.cover_letter_generator = (
        cover_letter_generator or GeminiCoverLetterGenerator()
    )
    application.state.runtime_log_directory = Path(
        runtime_log_directory
        or Path(__file__).resolve().parent.parent / "logs"
    )
    application.add_middleware(
        CORSMiddleware,
        allow_origins=_cors_origins(),
        allow_credentials=False,
        allow_methods=["GET", "POST", "PATCH", "DELETE", "OPTIONS"],
        allow_headers=["Content-Type"],
    )

    application.include_router(_build_routes())
    dashboard_path = Path(__file__).resolve().parent.parent / "dashboard"
    if dashboard_path.is_dir():
        application.mount(
            "/dashboard",
            StaticFiles(directory=dashboard_path, html=True),
            name="dashboard",
        )
    return application


def _build_routes():
    from fastapi import APIRouter

    router = APIRouter()

    @router.get("/api/shortlist")
    async def shortlist(request: Request):
        from backend.shortlist import build_shortlist
        return await build_shortlist(request.app.state.resume_catalog, request.app.state.database_path)

    @router.get("/api/outlook/monthly")
    async def outlook(request: Request):
        from backend.monthly_outlook import monthly_outlook
        return await monthly_outlook(request.app.state.database_path)

    @router.get("/health", response_model=HealthResponse)
    async def health(request: Request) -> HealthResponse:
        try:
            async with database_connection(request.app.state.database_path) as connection:
                cursor = await connection.execute("SELECT 1 AS ready")
                row = await cursor.fetchone()
                database_status = "ready" if row and row["ready"] == 1 else "error"
        except Exception as error:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="Database health check failed.",
            ) from error

        return HealthResponse(
            status="ok",
            database=database_status,
            schema_version=LATEST_SCHEMA_VERSION,
            gemini=ProviderHealth(
                configured=_configured_secret("GEMINI_API_KEY"),
                model=os.getenv("GEMINI_MODEL", DEFAULT_GEMINI_MODEL),
            ),
            ollama=ProviderHealth(
                configured=bool(os.getenv("OLLAMA_BASE_URL", "http://127.0.0.1:11434")),
                model=os.getenv("OLLAMA_MODEL", "qwen2.5:7b"),
            ),
        )

    @router.get("/api/discovery/status", response_model=DiscoveryHealthResponse)
    async def discovery_status(request: Request) -> DiscoveryHealthResponse:
        return _discovery_health(request.app.state.runtime_log_directory)

    @router.get("/api/jobs", response_model=list[StoredJob])
    async def jobs(
        request: Request,
        status_filter: JobStatus | None = None,
        us_only: bool = True,
    ):
        return await list_jobs(
            status=status_filter,
            us_only=us_only,
            database_path=request.app.state.database_path,
        )

    @router.get("/api/jobs/{job_id}/outreach", response_model=OutreachResponse)
    async def job_outreach(job_id: int, request: Request) -> OutreachResponse:
        details = await get_scored_job_details(
            job_id,
            database_path=request.app.state.database_path,
        )
        if details is None:
            raise HTTPException(status_code=404, detail="US-based scored job not found.")
        profile = _profile_for_score(
            request.app.state.resume_catalog,
            details.score,
        )
        guidance = build_outreach(details.posting, details.score, profile)
        return OutreachResponse(
            job_id=details.id,
            title=details.posting.title,
            company=details.posting.company,
            location=details.posting.location,
            overall_score=details.score.overall_score,
            recommended_resume=details.score.recommended_resume,
            strategy=guidance.strategy,
            recruiter_message=guidance.recruiter_message,
            referral_request=guidance.referral_message,
            cold_email=guidance.cold_email,
            missing_skills=details.score.missing_requirements,
            questions=guidance.questions,
        )

    @router.post(
        "/api/jobs/{job_id}/contacts",
        response_model=ContactSuggestionsResponse,
    )
    async def job_contacts(
        job_id: int,
        payload: ContactSearchRequest,
        request: Request,
    ) -> ContactSuggestionsResponse:
        details = await get_scored_job_details(
            job_id,
            database_path=request.app.state.database_path,
        )
        if details is None:
            raise HTTPException(status_code=404, detail="US-based scored job not found.")

        cache_days = max(1, int(os.getenv("CONTACT_CACHE_DAYS", "7")))
        if not payload.refresh:
            cached = await get_cached_contact_suggestions(
                job_id,
                max_age_days=cache_days,
                database_path=request.app.state.database_path,
            )
            if cached is not None:
                return ContactSuggestionsResponse(
                    job_id=job_id,
                    cached=True,
                    suggestions=cached,
                )

        finder: PublicContactFinder = request.app.state.contact_finder
        try:
            suggestions = await finder.find(
                details.posting,
                job_url=details.url,
                limit=max(1, int(os.getenv("CONTACT_RESULTS_PER_JOB", "3"))),
            )
        except ContactDiscoveryError as error:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail=str(error),
            ) from error
        await replace_contact_suggestions(
            job_id,
            suggestions,
            database_path=request.app.state.database_path,
        )
        return ContactSuggestionsResponse(
            job_id=job_id,
            cached=False,
            suggestions=suggestions,
        )

    @router.post("/api/cover-letter", response_class=PlainTextResponse)
    async def cover_letter(
        payload: CoverLetterRequest,
        request: Request,
    ) -> PlainTextResponse:
        if payload.job_id is not None:
            details = await get_scored_job_details(
                payload.job_id,
                database_path=request.app.state.database_path,
            )
            if details is None:
                raise HTTPException(
                    status_code=404,
                    detail="US-based scored job not found.",
                )
            posting = details.posting
            score = details.score
        else:
            posting = payload.posting
            score = payload.score
        assert posting is not None and score is not None

        profile = _profile_for_score(request.app.state.resume_catalog, score)
        if profile is None:
            raise HTTPException(
                status_code=422,
                detail="The selected READY resume profile is unavailable.",
            )
        generator: GeminiCoverLetterGenerator = (
            request.app.state.cover_letter_generator
        )
        try:
            letter = await generator.generate(posting, score, profile)
        except CoverLetterGenerationError as error:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail=str(error),
            ) from error
        return PlainTextResponse(letter)

    @router.post(
        "/api/jobs/{job_id}/check-availability",
        response_model=AvailabilityResponse,
    )
    async def check_job_availability(
        job_id: int,
        request: Request,
    ) -> AvailabilityResponse:
        candidate = await get_availability_check_candidate(
            job_id,
            database_path=request.app.state.database_path,
        )
        if candidate is None:
            raise HTTPException(status_code=404, detail="Open job not found.")
        extractor: PostingExtractor = request.app.state.posting_extractor
        try:
            await extractor.fetch(
                SearchResult(
                    title=candidate.title,
                    url=candidate.url,
                    source=candidate.source,
                )
            )
        except ClosedJobPostingError as error:
            reason = str(error)
            await mark_job_closed_by_url(
                candidate.url,
                reason,
                database_path=request.app.state.database_path,
            )
            return AvailabilityResponse(job_id=job_id, closed=True, reason=reason)
        except PostingExtractionError as error:
            raise HTTPException(
                status_code=422,
                detail=f"Could not verify job availability: {error}",
            ) from error
        await mark_job_availability_checked(
            job_id,
            database_path=request.app.state.database_path,
        )
        return AvailabilityResponse(job_id=job_id, closed=False)

    @router.patch("/api/jobs/{job_id}/status", response_model=StoredJob)
    async def change_job_status(
        job_id: int,
        payload: StatusUpdate,
        request: Request,
    ) -> StoredJob:
        try:
            job = await update_job_status(
                job_id,
                payload.status,
                skip_reason=payload.skip_reason,
                database_path=request.app.state.database_path,
            )
        except ValueError as error:
            raise HTTPException(status_code=422, detail=str(error)) from error
        if job is None:
            raise HTTPException(status_code=404, detail="Job not found.")
        return job

    @router.delete("/api/jobs/{job_id}", status_code=status.HTTP_204_NO_CONTENT)
    async def delete_job(job_id: int, request: Request) -> Response:
        deleted = await delete_stored_job(
            job_id,
            database_path=request.app.state.database_path,
        )
        if not deleted:
            raise HTTPException(status_code=404, detail="Job not found.")
        return Response(status_code=status.HTTP_204_NO_CONTENT)

    @router.post("/api/score", response_model=ScoreResponse)
    async def score_job(
        payload: ScoreRequest,
        request: Request,
    ) -> ScoreResponse:
        if reason := closure_reason(
            valid_through=payload.posting.valid_through,
            page_text=payload.posting.description,
        ):
            raise HTTPException(
                status_code=422,
                detail=f"This job is closed or expired: {reason}",
            )
        if not is_us_based(
            payload.posting.location,
            payload.posting.workplace_type,
            payload.posting.description,
        ):
            raise HTTPException(
                status_code=422,
                detail=(
                    "JobRadar could not verify that this role is based in the "
                    "United States. Only verified US roles are accepted."
                ),
            )
        engine: ScoringEngine = request.app.state.scoring_engine
        catalog: ResumeCatalog = request.app.state.resume_catalog
        try:
            profile: ResumeProfile = select_resume_profile(
                catalog,
                payload.posting.searchable_text,
                payload.profile_id,
            )
        except ResumeProfileNotFoundError as error:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=str(error),
            ) from error

        if profile.status is not ResumeStatus.READY:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=(
                    f"Resume profile {profile.profile_id!r} is still a draft; "
                    "add verified scoring content before using it."
                ),
            )

        try:
            result = await engine.score(payload.posting, profile)
        except AllScoringProvidersFailedError as error:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail=str(error),
            ) from error

        job_id = None
        if payload.url:
            job_id = await save_scored_job(
                payload.posting,
                result,
                source=payload.source,
                source_job_id=payload.source_job_id,
                url=payload.url,
                database_path=request.app.state.database_path,
            )
            if result.verdict.value == 'REJECTED' or result.hard_flags:
                await purge_rejected_jobs(database_path=request.app.state.database_path)
                job_id = None
        return ScoreResponse(**result.model_dump(), job_id=job_id)

    @router.post("/api/score-url", response_model=ScoreResponse)
    async def score_job_url(
        payload: UrlScoreRequest,
        request: Request,
    ) -> ScoreResponse:
        url = _validate_public_job_url(payload.url)
        extractor: PostingExtractor = request.app.state.posting_extractor
        try:
            posting = await extractor.fetch(
                SearchResult(
                    title="Job posting",
                    url=url,
                    source="dashboard-url",
                )
            )
        except ClosedJobPostingError as error:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=f"This job is closed or expired and will not be listed: {error}",
            ) from error
        except PostingExtractionError as error:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=(
                    f"Could not read this job page: {error} "
                    "If the site blocks automated access, open it and use the "
                    "JobRadar browser extension instead."
                ),
            ) from error

        if not is_us_based(
            posting.location,
            posting.workplace_type,
            posting.description,
        ):
            raise HTTPException(
                status_code=422,
                detail=(
                    "JobRadar could not verify that this role is based in the "
                    "United States. Only verified US roles are accepted."
                ),
            )

        return await score_job(
            ScoreRequest(
                posting=posting,
                profile_id=payload.profile_id,
                source="dashboard-url",
                url=url,
            ),
            request,
        )

    return router


def _profile_for_score(
    catalog: ResumeCatalog,
    score: ScoringResult,
) -> ResumeProfile | None:
    if not score.resume_profile_id:
        return None
    try:
        profile = catalog.get(score.resume_profile_id)
    except ResumeProfileNotFoundError:
        return None
    return profile if profile.status is ResumeStatus.READY else None


app = create_app()
