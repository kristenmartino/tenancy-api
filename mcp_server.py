"""
MCP server for Tenancy — lease abstraction agent.

Exposes six tools that let Claude Desktop (or any MCP client) query the lease
corpus, trigger new extractions, and resolve exceptions from the review queue.

The server is a thin facade over the FastAPI backend; it does not duplicate
business logic. Run as: `python mcp_server.py`. Wire it into Claude Desktop via
~/Library/Application Support/Claude/claude_desktop_config.json.
"""

from __future__ import annotations

import os
from typing import Any
from uuid import UUID

import httpx
from mcp.server.fastmcp import FastMCP

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

TENANCY_API_BASE = os.getenv("TENANCY_API_BASE", "https://api.tenancy.kristenmartino.ai")
TENANCY_API_KEY = os.getenv("TENANCY_API_KEY", "")  # If you add auth later

mcp = FastMCP("tenancy")

_headers = {"Authorization": f"Bearer {TENANCY_API_KEY}"} if TENANCY_API_KEY else {}
_client = httpx.AsyncClient(base_url=TENANCY_API_BASE, headers=_headers, timeout=60.0)


# ---------------------------------------------------------------------------
# Tools
# ---------------------------------------------------------------------------

@mcp.tool()
async def list_leases(
    status: str | None = None,
    limit: int = 50,
) -> list[dict[str, Any]]:
    """
    List processed leases.

    Args:
        status: Optional filter — "pending", "complete", "needs_review".
        limit: Max results to return (default 50).

    Returns:
        Summaries: lease_id, tenant_names, property_address, term_dates,
        base_rent, status, exception_count.
    """
    params: dict[str, Any] = {"limit": limit}
    if status:
        params["status"] = status
    r = await _client.get("/leases", params=params)
    r.raise_for_status()
    return r.json()


@mcp.tool()
async def get_lease(lease_id: str) -> dict[str, Any]:
    """
    Get the full structured extraction for a single lease.

    Args:
        lease_id: UUID of the lease.

    Returns:
        Full LeaseExtraction including every field's value, confidence, and
        source citation (page + char span).
    """
    UUID(lease_id)  # validate
    r = await _client.get(f"/leases/{lease_id}")
    r.raise_for_status()
    return r.json()


@mcp.tool()
async def extract_lease(pdf_url: str) -> dict[str, Any]:
    """
    Trigger extraction on a new lease PDF.

    Args:
        pdf_url: HTTPS URL of the PDF to abstract. Must be publicly fetchable.

    Returns:
        {lease_id, status} — extraction runs asynchronously. Poll get_lease or
        list_exceptions to see results once status is "complete".
    """
    if not pdf_url.startswith("https://"):
        raise ValueError("pdf_url must be an HTTPS URL")
    r = await _client.post("/leases", json={"pdf_url": pdf_url})
    r.raise_for_status()
    return r.json()


@mcp.tool()
async def query_lease(lease_id: str, question: str) -> dict[str, Any]:
    """
    Ask a natural-language question about a single lease.

    Args:
        lease_id: UUID of the lease.
        question: Plain English question (e.g. "What is the early termination
            fee?", "When does the lease auto-renew?").

    Returns:
        {answer: str, citations: list[{field_path, page_number, snippet}]}
        Backed by Claude Haiku grounded on the structured extraction; every
        claim points to a source span.
    """
    UUID(lease_id)
    r = await _client.post(
        f"/leases/{lease_id}/query",
        json={"question": question},
    )
    r.raise_for_status()
    return r.json()


@mcp.tool()
async def list_exceptions(
    lease_id: str | None = None,
    severity: str | None = None,
    resolved: bool = False,
) -> list[dict[str, Any]]:
    """
    List exceptions awaiting human review.

    Args:
        lease_id: Optional — restrict to a single lease.
        severity: Optional filter — "blocking", "warning", "informational".
        resolved: Include resolved exceptions if True (default False).

    Returns:
        Exceptions: exception_id, lease_id, field_path, exception_type,
        severity, description, suggested_action.
    """
    params: dict[str, Any] = {"resolved": resolved}
    if lease_id:
        UUID(lease_id)
        params["lease_id"] = lease_id
    if severity:
        params["severity"] = severity
    r = await _client.get("/exceptions", params=params)
    r.raise_for_status()
    return r.json()


@mcp.tool()
async def resolve_exception(
    exception_id: str,
    action: str,
    correction: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """
    Resolve a pending exception.

    Args:
        exception_id: UUID of the exception.
        action: One of "approve" (accept the model's extraction), "edit"
            (apply a correction), "reject" (mark as unrecoverable).
        correction: Required when action="edit". Field-typed value to apply.

    Returns:
        The updated exception record.
    """
    UUID(exception_id)
    if action not in {"approve", "edit", "reject"}:
        raise ValueError(f"Invalid action: {action}")
    if action == "edit" and correction is None:
        raise ValueError("correction is required when action='edit'")

    payload: dict[str, Any] = {"action": action}
    if correction is not None:
        payload["correction"] = correction
    r = await _client.post(f"/exceptions/{exception_id}/resolve", json=payload)
    r.raise_for_status()
    return r.json()


# ---------------------------------------------------------------------------
# Entrypoint
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    mcp.run()
