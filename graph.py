"""
LangGraph extraction pipeline for residential lease abstraction.

Four-node graph:
    ingest -> extract -> validate -> persist

State flows through as ExtractionState (Pydantic). Each node is a pure-ish
function: takes state, returns updated state. Side effects (DB writes, blob
storage) are isolated to ingest and persist.

This is scaffolding. Replace the TODOs with real implementations during build.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any
from uuid import UUID, uuid4

from langgraph.graph import END, StateGraph
from pydantic import BaseModel, Field

from schemas import (
    ExceptionSeverity,
    ExceptionType,
    ExtractedField,
    LeaseException,
    LeaseExtraction,
)

# ---------------------------------------------------------------------------
# State
# ---------------------------------------------------------------------------

class ExtractionState(BaseModel):
    """Mutable state passed between nodes."""
    document_id: UUID
    pdf_url: str
    raw_text: str | None = None
    page_count: int = 0
    page_images: list[str] = Field(default_factory=list)  # Cloud blob URLs
    pages_needing_vision: list[int] = Field(default_factory=list)
    extraction: LeaseExtraction | None = None
    exceptions: list[LeaseException] = Field(default_factory=list)
    status: str = "pending"
    error: str | None = None


# ---------------------------------------------------------------------------
# Node: ingest
# ---------------------------------------------------------------------------

def ingest_document(state: ExtractionState) -> ExtractionState:
    """
    Pull the PDF, extract text, rasterize pages.

    Steps:
      1. Fetch PDF bytes from state.pdf_url
      2. Try pypdf for text-native extraction
      3. For pages with empty/garbage text, rasterize to PNG and flag for vision
      4. Upload page images to blob storage, store URLs in state.page_images
      5. Persist raw text to state.raw_text
    """
    # TODO: pypdf extraction
    # TODO: page rasterization via pdf2image
    # TODO: blob upload (Railway volume or S3)
    state.status = "ingested"
    return state


# ---------------------------------------------------------------------------
# Node: extract
# ---------------------------------------------------------------------------

EXTRACTION_PROMPT = """You are extracting structured data from a residential lease agreement.

The lease text follows the document boundary marker. Extract the specified section
and return ONLY a JSON object matching the schema. Every field must include:
  - value: the extracted value (or null if not present)
  - confidence: 0.0 to 1.0, your honest confidence
  - source: {page_number, char_start, char_end, snippet} pointing to verbatim text
  - notes: optional, only if ambiguity needs flagging

Do not hallucinate. If a field is not stated in the document, set value to null and
confidence to 1.0 (you are confident it is absent).

Schema for this section:
{schema}

Document:
---
{document}
---

Return ONLY the JSON object. No preamble, no markdown fences."""


def extract_fields(state: ExtractionState) -> ExtractionState:
    """
    Call Claude Sonnet 4.6 per section to populate the LeaseExtraction model.

    Sectioned approach (parallelizable):
      - parties
      - property
      - term
      - rent
      - deposits
      - utilities
      - pets
      - special_clauses
      - compliance

    For pages_needing_vision, attach the page image to the request and use
    Claude's vision capability.
    """
    # TODO: per-section structured output calls
    # TODO: assemble into LeaseExtraction
    # TODO: compute overall_confidence as a weighted average of field confidences
    state.status = "extracted"
    return state


# ---------------------------------------------------------------------------
# Node: validate
# ---------------------------------------------------------------------------

LOW_CONFIDENCE_THRESHOLD = 0.7
SECURITY_DEPOSIT_MAX_MULTIPLIER = 3  # Heuristic; state caps vary


def _walk_extracted_fields(obj: Any, path: str = "") -> Iterator[tuple[str, ExtractedField]]:
    """Yield (dot-path, ExtractedField) for every ExtractedField in the model tree."""
    if isinstance(obj, ExtractedField):
        yield path, obj
        return
    if isinstance(obj, BaseModel):
        for name in type(obj).model_fields:
            child_path = f"{path}.{name}" if path else name
            yield from _walk_extracted_fields(getattr(obj, name), child_path)
    elif isinstance(obj, list):
        for i, item in enumerate(obj):
            yield from _walk_extracted_fields(item, f"{path}[{i}]")


def validate_extraction(state: ExtractionState) -> ExtractionState:
    """
    Rule-based validation. Generates exceptions for the review queue.

    Checks:
      - Required-but-null values for top-level fields
      - term.end_date > term.start_date
      - rent.base_monthly_rent > 0
      - deposits.security_deposit <= 3x base_monthly_rent (heuristic; state caps vary)
      - rent.late_fee_flat paired with grace_period_days
      - Lead paint disclosure presence (federally mandated)
      - Low-confidence fields (confidence < 0.7) anywhere in the tree
    """
    if state.extraction is None:
        state.status = "validated"
        return state

    extraction = state.extraction

    def flag(
        field_path: str,
        exc_type: ExceptionType,
        severity: ExceptionSeverity,
        description: str,
        suggested: str | None = None,
    ) -> None:
        state.exceptions.append(
            LeaseException(
                exception_id=uuid4(),
                lease_id=extraction.lease_id,
                field_path=field_path,
                exception_type=exc_type,
                severity=severity,
                description=description,
                suggested_action=suggested,
            )
        )

    # Low-confidence sweep across the whole tree
    for path, field in _walk_extracted_fields(extraction):
        if field.confidence < LOW_CONFIDENCE_THRESHOLD:
            flag(
                path,
                ExceptionType.LOW_CONFIDENCE,
                ExceptionSeverity.WARNING,
                f"Confidence {field.confidence:.2f} below threshold {LOW_CONFIDENCE_THRESHOLD}",
                "Review the source citation and confirm or correct.",
            )

    # Required-but-null check on hot fields (pydantic enforces presence; this checks .value)
    required: list[tuple[str, Any]] = [
        ("property.street_address", extraction.property.street_address.value),
        ("term.start_date", extraction.term.start_date.value),
        ("term.end_date", extraction.term.end_date.value),
        ("rent.base_monthly_rent", extraction.rent.base_monthly_rent.value),
        ("deposits.security_deposit", extraction.deposits.security_deposit.value),
    ]
    for path, value in required:
        if value is None:
            flag(
                path,
                ExceptionType.MISSING_REQUIRED_FIELD,
                ExceptionSeverity.BLOCKING,
                f"{path} is required but value is null in the extraction.",
            )
    if not extraction.parties:
        flag(
            "parties",
            ExceptionType.MISSING_REQUIRED_FIELD,
            ExceptionSeverity.BLOCKING,
            "No parties extracted.",
        )

    # Date consistency
    start = extraction.term.start_date.value
    end = extraction.term.end_date.value
    if start and end and end <= start:
        flag(
            "term.end_date",
            ExceptionType.INTERNAL_INCONSISTENCY,
            ExceptionSeverity.BLOCKING,
            f"end_date ({end}) must be after start_date ({start})",
        )

    # Positive rent
    rent_value = extraction.rent.base_monthly_rent.value
    if rent_value is not None and rent_value <= 0:
        flag(
            "rent.base_monthly_rent",
            ExceptionType.INTERNAL_INCONSISTENCY,
            ExceptionSeverity.BLOCKING,
            f"base_monthly_rent ({rent_value}) must be > 0",
        )

    # Deposit cap (heuristic)
    deposit = extraction.deposits.security_deposit.value
    if rent_value and deposit and deposit > SECURITY_DEPOSIT_MAX_MULTIPLIER * rent_value:
        flag(
            "deposits.security_deposit",
            ExceptionType.UNUSUAL_CLAUSE,
            ExceptionSeverity.WARNING,
            f"Security deposit ({deposit}) exceeds {SECURITY_DEPOSIT_MAX_MULTIPLIER}x monthly "
            f"rent ({rent_value}). State caps vary.",
            "Verify against local statute (e.g. CA caps at 2x; TX has no statutory cap).",
        )

    # Late fee paired with grace period
    late_fee_field = extraction.rent.late_fee_flat
    grace_field = extraction.rent.grace_period_days
    late_fee = late_fee_field.value if late_fee_field else None
    grace_days = grace_field.value if grace_field else None
    if late_fee and grace_days is None:
        flag(
            "rent.grace_period_days",
            ExceptionType.MISSING_REQUIRED_FIELD,
            ExceptionSeverity.WARNING,
            "Late fee is set but grace period is missing.",
        )

    # Lead paint disclosure presence (required for pre-1978 properties)
    if extraction.compliance.lead_paint_disclosure.value is None:
        flag(
            "compliance.lead_paint_disclosure",
            ExceptionType.COMPLIANCE_GAP,
            ExceptionSeverity.BLOCKING,
            "Lead paint disclosure status undetermined. Required for pre-1978 properties.",
        )

    state.status = "validated"
    return state


# ---------------------------------------------------------------------------
# Node: persist
# ---------------------------------------------------------------------------

def persist_results(state: ExtractionState) -> ExtractionState:
    """
    Write extraction + exceptions to Postgres.

    Tables touched:
      - documents (status update)
      - extractions (full LeaseExtraction JSON + flat columns for the hot fields)
      - exceptions (one row per LeaseException)
    """
    # TODO: SQLAlchemy writes
    state.status = "complete"
    return state


# ---------------------------------------------------------------------------
# Graph wiring
# ---------------------------------------------------------------------------

def build_graph():
    g = StateGraph(ExtractionState)
    g.add_node("ingest", ingest_document)
    g.add_node("extract", extract_fields)
    g.add_node("validate", validate_extraction)
    g.add_node("persist", persist_results)

    g.set_entry_point("ingest")
    g.add_edge("ingest", "extract")
    g.add_edge("extract", "validate")
    g.add_edge("validate", "persist")
    g.add_edge("persist", END)

    return g.compile()


# Convenience entrypoint for the API layer
async def run_extraction(pdf_url: str) -> ExtractionState:
    graph = build_graph()
    initial = ExtractionState(document_id=uuid4(), pdf_url=pdf_url)
    final_state = await graph.ainvoke(initial)
    return ExtractionState.model_validate(final_state)
