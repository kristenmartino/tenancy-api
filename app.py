"""
FastAPI app for the tenancy backend.

Exposes the six endpoints the MCP server calls. Backed by SQLAlchemy + SQLite
locally; swap DATABASE_URL to a Postgres URL for prod. Extraction runs in a
background task so POST /leases returns 202 immediately.
"""
from __future__ import annotations

import os
import sys
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Annotated, Any
from uuid import UUID, uuid4

from fastapi import BackgroundTasks, Depends, FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from db import ExceptionRecord, LeaseRecord, get_session, init_db
from graph import run_extraction
from qa import answer_question
from schemas import ReviewAction


@asynccontextmanager
async def _lifespan(_app: FastAPI) -> AsyncIterator[None]:
    if not os.getenv("ANTHROPIC_API_KEY"):
        print(
            "WARNING: ANTHROPIC_API_KEY is not set. extract_fields will fail "
            "with anthropic.AuthenticationError on every lease.",
            file=sys.stderr,
        )
    await init_db()
    yield


app = FastAPI(lifespan=_lifespan, title="tenancy-api", version="0.1.0")

# CORS — open by default for the portfolio demo. Tighten by setting
# CORS_ORIGINS to a comma-separated list of allowed origins for prod.
_cors_origins = os.getenv("CORS_ORIGINS", "*").split(",")
app.add_middleware(
    CORSMiddleware,
    allow_origins=[o.strip() for o in _cors_origins],
    allow_methods=["*"],
    allow_headers=["*"],
)

SessionDep = Annotated[AsyncSession, Depends(get_session)]


# ---------------------------------------------------------------------------
# Request models
# ---------------------------------------------------------------------------

class ExtractRequest(BaseModel):
    pdf_url: str


class QueryRequest(BaseModel):
    question: str


class ResolveRequest(BaseModel):
    action: str
    correction: dict[str, Any] | None = None


# ---------------------------------------------------------------------------
# Serializers
# ---------------------------------------------------------------------------

def _lease_to_dict(lease: LeaseRecord, exception_count: int = 0) -> dict[str, Any]:
    return {
        "lease_id": str(lease.lease_id),
        "pdf_url": lease.pdf_url,
        "status": lease.status,
        "extraction": lease.extraction,
        "error": lease.error,
        "exception_count": exception_count,
        "created_at": lease.created_at.isoformat(),
        "updated_at": lease.updated_at.isoformat(),
    }


def _exception_to_dict(exc: ExceptionRecord) -> dict[str, Any]:
    return {
        "exception_id": str(exc.exception_id),
        "lease_id": str(exc.lease_id),
        "field_path": exc.field_path,
        "exception_type": exc.exception_type,
        "severity": exc.severity,
        "description": exc.description,
        "suggested_action": exc.suggested_action,
        "resolved": exc.resolved,
        "resolution": exc.resolution,
        "correction": exc.correction,
        "created_at": exc.created_at.isoformat(),
    }


# ---------------------------------------------------------------------------
# Health
# ---------------------------------------------------------------------------

@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


# ---------------------------------------------------------------------------
# Leases
# ---------------------------------------------------------------------------

@app.get("/leases")
async def list_leases(
    session: SessionDep,
    status: str | None = None,
    limit: int = 50,
) -> list[dict[str, Any]]:
    count_sq = (
        select(func.count(ExceptionRecord.exception_id))
        .where(ExceptionRecord.lease_id == LeaseRecord.lease_id)
        .where(ExceptionRecord.resolved.is_(False))
        .correlate(LeaseRecord)
        .scalar_subquery()
    )
    stmt = select(LeaseRecord, count_sq.label("exc_count"))
    if status:
        stmt = stmt.where(LeaseRecord.status == status)
    stmt = stmt.order_by(LeaseRecord.created_at.desc()).limit(limit)
    result = await session.execute(stmt)
    return [_lease_to_dict(lease, count) for lease, count in result.all()]


@app.get("/leases/{lease_id}")
async def get_lease(lease_id: UUID, session: SessionDep) -> dict[str, Any]:
    lease = await session.get(LeaseRecord, lease_id)
    if lease is None:
        raise HTTPException(status_code=404, detail=f"Lease {lease_id} not found")
    count_result = await session.execute(
        select(func.count(ExceptionRecord.exception_id))
        .where(ExceptionRecord.lease_id == lease_id)
        .where(ExceptionRecord.resolved.is_(False))
    )
    return _lease_to_dict(lease, count_result.scalar_one())


async def _run_pipeline(pdf_url: str, lease_id: UUID) -> None:
    """Background task: run the extraction graph; persist_results writes to DB."""
    await run_extraction(pdf_url, document_id=lease_id)


@app.post("/leases", status_code=202)
async def create_lease(
    req: ExtractRequest,
    bg: BackgroundTasks,
    session: SessionDep,
) -> dict[str, str]:
    if not req.pdf_url.startswith("https://"):
        raise HTTPException(status_code=400, detail="pdf_url must be an HTTPS URL")
    lease_id = uuid4()
    session.add(LeaseRecord(lease_id=lease_id, pdf_url=req.pdf_url, status="pending"))
    # Explicit commit: post-yield code in a FastAPI dep runs AFTER bg tasks
    # finish, so without this the bg task's persist_results would race the
    # parent INSERT and the exception rows would FK-violate.
    await session.commit()
    bg.add_task(_run_pipeline, req.pdf_url, lease_id)
    return {"lease_id": str(lease_id), "status": "pending"}


@app.post("/leases/{lease_id}/query")
async def query_lease(
    lease_id: UUID,
    req: QueryRequest,
    session: SessionDep,
) -> dict[str, Any]:
    lease = await session.get(LeaseRecord, lease_id)
    if lease is None:
        raise HTTPException(status_code=404, detail=f"Lease {lease_id} not found")
    if lease.extraction is None:
        raise HTTPException(
            status_code=409,
            detail=f"Lease {lease_id} has no extraction yet (status={lease.status})",
        )
    try:
        result = await answer_question(lease.extraction, req.question)
    except Exception as exc:  # noqa: BLE001 — surface upstream LLM failures as 502
        raise HTTPException(
            status_code=502, detail=f"Q&A failed: {type(exc).__name__}: {exc}"
        ) from exc
    return result.model_dump()


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------

@app.get("/exceptions")
async def list_exceptions(
    session: SessionDep,
    lease_id: UUID | None = None,
    severity: str | None = None,
    resolved: bool = False,
) -> list[dict[str, Any]]:
    stmt = select(ExceptionRecord).where(ExceptionRecord.resolved.is_(resolved))
    if lease_id is not None:
        stmt = stmt.where(ExceptionRecord.lease_id == lease_id)
    if severity:
        stmt = stmt.where(ExceptionRecord.severity == severity)
    stmt = stmt.order_by(ExceptionRecord.created_at.desc())
    result = await session.execute(stmt)
    return [_exception_to_dict(e) for e in result.scalars().all()]


@app.post("/exceptions/{exception_id}/resolve")
async def resolve_exception(
    exception_id: UUID,
    req: ResolveRequest,
    session: SessionDep,
) -> dict[str, Any]:
    exc = await session.get(ExceptionRecord, exception_id)
    if exc is None:
        raise HTTPException(status_code=404, detail=f"Exception {exception_id} not found")
    if req.action not in {"approve", "edit", "reject"}:
        raise HTTPException(status_code=400, detail=f"Invalid action: {req.action}")
    if req.action == "edit" and req.correction is None:
        raise HTTPException(status_code=400, detail="correction required when action='edit'")
    exc.resolved = True
    exc.resolution = ReviewAction(req.action).value
    exc.correction = req.correction
    await session.commit()
    return _exception_to_dict(exc)
