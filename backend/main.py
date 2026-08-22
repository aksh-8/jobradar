"""FastAPI entry point for the JobRadar scoring service."""

from __future__ import annotations

import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Request, status
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, ConfigDict

from backend.database import LATEST_SCHEMA_VERSION, database_connection, initialize_database
from backend.red_flag_scanner import JobPostingFacts
from backend.resume_store import (
    RESUME_CATALOG,
    ResumeCatalog,
    ResumeProfile,
    ResumeProfileNotFoundError,
    ResumeStatus,
)
from backend.scoring_engine import (
    AllScoringProvidersFailedError,
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
    profile_id: str


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


def _configured_secret(name: str) -> bool:
    value = os.getenv(name, "").strip()
    return bool(value and not value.startswith("replace_with_"))


def _cors_origins() -> list[str]:
    configured = os.getenv("CORS_ALLOWED_ORIGINS", "*")
    origins = [origin.strip() for origin in configured.split(",") if origin.strip()]
    return origins or ["*"]


def create_app(
    *,
    database_path: str | Path | None = None,
    scoring_engine: ScoringEngine | None = None,
    resume_catalog: ResumeCatalog | None = None,
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
    application.state.resume_catalog = resume_catalog or RESUME_CATALOG
    application.add_middleware(
        CORSMiddleware,
        allow_origins=_cors_origins(),
        allow_credentials=False,
        allow_methods=["GET", "POST", "OPTIONS"],
        allow_headers=["Content-Type"],
    )

    application.include_router(_build_routes())
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
                model=os.getenv("GEMINI_MODEL", "gemini-2.5-pro"),
            ),
            ollama=ProviderHealth(
                configured=bool(os.getenv("OLLAMA_BASE_URL", "http://127.0.0.1:11434")),
                model=os.getenv("OLLAMA_MODEL", "qwen2.5:7b"),
            ),
        )

    @router.post("/api/score", response_model=ScoringResult)
    async def score_job(
        payload: ScoreRequest,
        request: Request,
    ) -> ScoringResult:
        engine: ScoringEngine = request.app.state.scoring_engine
        catalog: ResumeCatalog = request.app.state.resume_catalog
        try:
            profile: ResumeProfile = catalog.get(payload.profile_id)
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
            return await engine.score(payload.posting, profile)
        except AllScoringProvidersFailedError as error:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail=str(error),
            ) from error

    return router


app = create_app()
