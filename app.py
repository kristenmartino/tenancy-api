"""
FastAPI app for the tenancy backend.

Exposes the six endpoints the MCP server calls. In-memory stores stand in for
Postgres until the persistence layer lands. Extraction is dispatched as a
background task so POST /leases returns 202 immediately.
"""
from __future__ import annotations

import os
import sys
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any
from uuid import UUID, uuid4

from fastapi import BackgroundTasks, FastAPI, HTTPException
from pydantic import BaseModel

from graph import run_extraction
from schemas import LeaseException, ReviewAction


@asynccontextmanager
async def _lifespan(_app: FastAPI) -> AsyncIterator[None]:
    if not os.getenv("ANTHROPIC_API_KEY"):
        print(
            "WARNING: ANTHROPIC_API_KEY is not set. extract_fields will fail "
            "with anthropic.AuthenticationError on every lease.",
            file=sys.stderr,
        )
    yield


app = FastAPI(lifespan=_lifespan, title="tenancy-api", version="0.1.0")

# In-memory stores (replace with Postgres in v1)
_leases: dict[UUID, dict[str, Any]] = {}
_exceptions: dict[UUID, LeaseException] = {}


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
    status: str | None = None,
    limit: int = 50,
) -> list[dict[str, Any]]:
    items = list(_leases.values())
    if status:
        items = [x for x in items if x["status"] == status]
    return items[:limit]


@app.get("/leases/{lease_id}")
async def get_lease(lease_id: UUID) -> dict[str, Any]:
    record = _leases.get(lease_id)
    if record is None:
        raise HTTPException(status_code=404, detail=f"Lease {lease_id} not found")
    return record


async def _run_and_store(pdf_url: str, lease_id: UUID) -> None:
    """Background task: run the extraction graph and persist results."""
    state = await run_extraction(pdf_url)
    record = _leases[lease_id]
    record["status"] = state.status
    if state.extraction is not None:
        record["extraction"] = state.extraction.model_dump(mode="json")
    for exc in state.exceptions:
        _exceptions[exc.exception_id] = exc
    record["exception_count"] = sum(1 for e in state.exceptions if not e.resolved)


@app.post("/leases", status_code=202)
async def create_lease(req: ExtractRequest, bg: BackgroundTasks) -> dict[str, str]:
    if not req.pdf_url.startswith("https://"):
        raise HTTPException(status_code=400, detail="pdf_url must be an HTTPS URL")
    lease_id = uuid4()
    _leases[lease_id] = {
        "lease_id": str(lease_id),
        "pdf_url": req.pdf_url,
        "status": "pending",
        "exception_count": 0,
    }
    bg.add_task(_run_and_store, req.pdf_url, lease_id)
    return {"lease_id": str(lease_id), "status": "pending"}


@app.post("/leases/{lease_id}/query")
async def query_lease(lease_id: UUID, req: QueryRequest) -> dict[str, Any]:
    if lease_id not in _leases:
        raise HTTPException(status_code=404, detail=f"Lease {lease_id} not found")
    # TODO: Claude Haiku call grounded on the structured extraction
    return {
        "answer": f"Q&A not yet implemented. Asked: {req.question}",
        "citations": [],
    }


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------

@app.get("/exceptions")
async def list_exceptions(
    lease_id: UUID | None = None,
    severity: str | None = None,
    resolved: bool = False,
) -> list[dict[str, Any]]:
    items = [e for e in _exceptions.values() if e.resolved == resolved]
    if lease_id is not None:
        items = [e for e in items if e.lease_id == lease_id]
    if severity:
        items = [e for e in items if e.severity.value == severity]
    return [e.model_dump(mode="json") for e in items]


@app.post("/exceptions/{exception_id}/resolve")
async def resolve_exception(
    exception_id: UUID,
    req: ResolveRequest,
) -> dict[str, Any]:
    exc = _exceptions.get(exception_id)
    if exc is None:
        raise HTTPException(status_code=404, detail=f"Exception {exception_id} not found")
    if req.action not in {"approve", "edit", "reject"}:
        raise HTTPException(status_code=400, detail=f"Invalid action: {req.action}")
    if req.action == "edit" and req.correction is None:
        raise HTTPException(status_code=400, detail="correction required when action='edit'")
    exc.resolved = True
    exc.resolution = ReviewAction(req.action)
    exc.correction = req.correction
    return exc.model_dump(mode="json")
