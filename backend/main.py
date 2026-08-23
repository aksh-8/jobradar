"""FastAPI entry point for the JobRadar scoring service."""

from __future__ import annotations

import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from ipaddress import ip_address
from pathlib import Path
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Request, Response, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, HttpUrl

from agent.posting_extractor import (
    ClosedJobPostingError,
    PostingExtractionError,
    PostingExtractor,
)
from agent.outreach import build_outreach
from agent.search_providers import SearchResult

from backend.database import LATEST_SCHEMA_VERSION, database_connection, initialize_database
from backend.job_store import (
    JobDeletionNotAllowedError,
    JobStatus,
    StoredJob,
    delete_hard_rejected_job,
    get_availability_check_candidate,
    get_scored_job_details,
    list_jobs,
    mark_job_availability_checked,
    mark_job_closed_by_url,
    save_scored_job,
    update_job_status,
)
from backend.job_availability import closure_reason
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


def create_app(
    *,
    database_path: str | Path | None = None,
    scoring_engine: ScoringEngine | None = None,
    resume_catalog: ResumeCatalog | None = None,
    posting_extractor: PostingExtractor | None = None,
) -> FastAPI:
    """Build an application with replaceable dependencies for isolated tests."""

    @asynccontextmanager
    async def lifespan(application: FastAPI) -> AsyncIterator[None]:
        await initialize_database(database_path)
        application.state.database_path = database_path
        yield

    application = FastAPI(
        title="JobRadar API",
        version="0.1.0",
        lifespan=lifespan,
    )
    application.state.scoring_engine = scoring_engine or create_default_scoring_engine()
    application.state.resume_catalog = resume_catalog or configured_resume_catalog()
    application.state.posting_extractor = posting_extractor or PostingExtractor()
    application.add_middleware(
        CORSMiddleware,
        allow_origins=_cors_origins(),
        allow_credentials=False,
        allow_methods=["GET", "POST", "OPTIONS"],
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
        guidance = build_outreach(details.posting, details.score)
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
        try:
            deleted = await delete_hard_rejected_job(
                job_id,
                database_path=request.app.state.database_path,
            )
        except JobDeletionNotAllowedError as error:
            raise HTTPException(status_code=409, detail=str(error)) from error
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


app = create_app()
