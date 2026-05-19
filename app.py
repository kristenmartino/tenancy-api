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

from fastapi import BackgroundTasks, Depends, FastAPI, File, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import Response
from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import undefer

from db import AsyncSessionLocal, ExceptionRecord, LeaseRecord, get_session, init_db
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


MAX_UPLOAD_SIZE = int(os.getenv("MAX_UPLOAD_SIZE", str(20 * 1024 * 1024)))  # 20 MiB

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

def _lease_to_dict(
    lease: LeaseRecord,
    exception_count: int = 0,
    *,
    include_extraction: bool = True,
) -> dict[str, Any]:
    return {
        "lease_id": str(lease.lease_id),
        "pdf_url": lease.pdf_url,
        "status": lease.status,
        # Extraction trees can run 50KB+ per lease. Excluded from list
        # responses to keep that endpoint snappy under cold starts; the
        # detail endpoint still returns the full payload.
        "extraction": lease.extraction if include_extraction else None,
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
    return [
        _lease_to_dict(lease, count, include_extraction=False)
        for lease, count in result.all()
    ]


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


async def _run_pipeline(
    pdf_url: str,
    lease_id: UUID,
    pdf_bytes: bytes | None = None,
) -> None:
    """Background task: run the extraction graph; persist_results writes to DB.

    pdf_bytes is set when the source was an upload — ingest_document skips
    the HTTP fetch and uses the provided bytes directly.

    Wrapped in try/except so any uncaught exception (pypdf crash, LLM
    timeout, OOM, Anthropic 5xx) marks the lease as pipeline_failed instead
    of leaving it stranded at 'pending' forever.
    """
    try:
        await run_extraction(pdf_url, document_id=lease_id, pdf_bytes=pdf_bytes)
    except Exception as exc:  # noqa: BLE001 — safety net for the whole graph
        async with AsyncSessionLocal() as session:
            lease = await session.get(LeaseRecord, lease_id)
            if lease is not None:
                lease.status = "pipeline_failed"
                lease.error = f"Pipeline failed: {type(exc).__name__}: {exc}"
                await session.commit()


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


@app.post("/leases/upload", status_code=202)
async def upload_lease(
    bg: BackgroundTasks,
    session: SessionDep,
    file: Annotated[UploadFile, File(...)],
) -> dict[str, str]:
    """Multipart-upload alternative to POST /leases.

    Accepts a PDF file directly so callers don't need to host it somewhere
    publicly fetchable. Validates the magic bytes and a size cap before
    dispatching the extraction pipeline.
    """
    pdf_bytes = await file.read()
    if len(pdf_bytes) > MAX_UPLOAD_SIZE:
        raise HTTPException(
            status_code=413,
            detail=f"File exceeds max upload size ({MAX_UPLOAD_SIZE} bytes)",
        )
    if not pdf_bytes.startswith(b"%PDF-"):
        raise HTTPException(
            status_code=415,
            detail="Uploaded file is not a PDF (missing %PDF- header)",
        )

    lease_id = uuid4()
    display_url = f"upload://{file.filename or lease_id}"
    # Persist bytes up-front (not waiting for persist_results) so GET
    # /leases/{id}/pdf works the moment this returns — the frontend viewer
    # can render the PDF while extraction is still pending.
    session.add(
        LeaseRecord(
            lease_id=lease_id,
            pdf_url=display_url,
            status="pending",
            pdf_bytes=pdf_bytes,
        )
    )
    await session.commit()
    bg.add_task(_run_pipeline, display_url, lease_id, pdf_bytes)
    return {"lease_id": str(lease_id), "status": "pending"}


@app.get("/leases/{lease_id}/pdf")
async def get_lease_pdf(lease_id: UUID, session: SessionDep) -> Response:
    """Stream the source PDF bytes for the lease (if persisted)."""
    # Explicit undefer — pdf_bytes is deferred by default to keep list
    # queries lean.
    stmt = (
        select(LeaseRecord)
        .where(LeaseRecord.lease_id == lease_id)
        .options(undefer(LeaseRecord.pdf_bytes))
    )
    lease = (await session.execute(stmt)).scalar_one_or_none()
    if lease is None:
        raise HTTPException(status_code=404, detail=f"Lease {lease_id} not found")
    if lease.pdf_bytes is None:
        raise HTTPException(
            status_code=404,
            detail=f"Lease {lease_id} has no stored PDF bytes",
        )
    return Response(
        content=lease.pdf_bytes,
        media_type="application/pdf",
        headers={"Cache-Control": "private, max-age=3600"},
    )


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
