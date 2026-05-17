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

import asyncio
import io
import json
import os
from collections.abc import Iterator
from typing import Any
from uuid import UUID, uuid4

import httpx
from anthropic import AsyncAnthropic
from langgraph.graph import END, StateGraph
from pydantic import BaseModel, Field
from pypdf import PdfReader

from schemas import (
    ComplianceDisclosures,
    Deposits,
    ExceptionSeverity,
    ExceptionType,
    ExtractedField,
    LeaseException,
    LeaseExtraction,
    LeaseTemplate,
    Party,
    Pets,
    Property,
    Rent,
    SpecialClauses,
    Term,
    Utilities,
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

PDF_FETCH_TIMEOUT = float(os.getenv("PDF_FETCH_TIMEOUT", "30"))
MIN_TEXT_LEN_PER_PAGE = int(os.getenv("MIN_TEXT_LEN_PER_PAGE", "50"))


async def _fetch_pdf(url: str) -> bytes:
    async with httpx.AsyncClient(timeout=PDF_FETCH_TIMEOUT) as client:
        resp = await client.get(url, follow_redirects=True)
        resp.raise_for_status()
        return resp.content


async def ingest_document(state: ExtractionState) -> ExtractionState:
    """
    Fetch the PDF, extract text per page via pypdf, flag low-text pages.

    Pages with extracted text below MIN_TEXT_LEN_PER_PAGE are added to
    state.pages_needing_vision for the extract node to handle via vision.
    Page rasterization to PNG + blob upload is still TODO (needs poppler).
    """
    try:
        pdf_bytes = await _fetch_pdf(state.pdf_url)
    except httpx.HTTPError as exc:
        state.error = f"Failed to fetch PDF: {exc}"
        state.status = "ingest_failed"
        return state

    reader = PdfReader(io.BytesIO(pdf_bytes))
    state.page_count = len(reader.pages)

    page_texts: list[str] = []
    pages_needing_vision: list[int] = []
    for i, page in enumerate(reader.pages, start=1):
        text = page.extract_text() or ""
        page_texts.append(f"[PAGE {i}]\n{text}")
        if len(text.strip()) < MIN_TEXT_LEN_PER_PAGE:
            pages_needing_vision.append(i)

    state.raw_text = "\n\n".join(page_texts)
    state.pages_needing_vision = pages_needing_vision
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


EXTRACT_MODEL = os.getenv("EXTRACT_MODEL", "claude-sonnet-4-6")
EXTRACT_MAX_TOKENS = int(os.getenv("EXTRACT_MAX_TOKENS", "4096"))
TEMPLATE_MODEL = os.getenv("TEMPLATE_MODEL", "claude-haiku-4-5-20251001")
TEMPLATE_DETECTION_MAX_CHARS = 4000


TEMPLATE_DETECTION_PROMPT = """Classify the following residential lease excerpt as one of these templates:

- taa: Texas Apartment Association
- naa: National Apartment Association
- ca_residential: California residential lease
- fl_residential: Florida residential lease
- unknown: doesn't clearly match any of the above

Return ONLY the lowercase template code. No preamble, no explanation.

Lease excerpt:
{excerpt}"""


async def _detect_template(client: AsyncAnthropic, text: str) -> LeaseTemplate:
    """Classify lease text against the LeaseTemplate enum via a quick Haiku call."""
    prompt = TEMPLATE_DETECTION_PROMPT.format(excerpt=text[:TEMPLATE_DETECTION_MAX_CHARS])
    response = await client.messages.create(
        model=TEMPLATE_MODEL,
        max_tokens=32,
        messages=[{"role": "user", "content": prompt}],
    )
    block = response.content[0]
    if not hasattr(block, "text"):
        return LeaseTemplate.UNKNOWN
    try:
        return LeaseTemplate(block.text.strip().lower())
    except ValueError:
        return LeaseTemplate.UNKNOWN


class PartiesSection(BaseModel):
    """Wrapper so parties has an object-shaped JSON contract for the LLM."""
    parties: list[Party]


SECTION_MODELS: dict[str, type[BaseModel]] = {
    "parties": PartiesSection,
    "property": Property,
    "term": Term,
    "rent": Rent,
    "deposits": Deposits,
    "utilities": Utilities,
    "pets": Pets,
    "special_clauses": SpecialClauses,
    "compliance": ComplianceDisclosures,
}


def _build_prompt(schema: dict[str, Any], document: str) -> str:
    # String replacement (not .format) because the prompt contains literal
    # braces in the {page_number, char_start, char_end, snippet} description.
    return EXTRACTION_PROMPT.replace("{schema}", json.dumps(schema, indent=2)).replace(
        "{document}", document
    )


def _strip_fences(text: str) -> str:
    text = text.strip()
    if text.startswith("```"):
        text = text.split("\n", 1)[1] if "\n" in text else text[3:]
    if text.endswith("```"):
        text = text.rsplit("```", 1)[0]
    return text.strip()


async def _extract_section(
    client: AsyncAnthropic,
    schema_class: type[BaseModel],
    document: str,
) -> BaseModel:
    """Call the LLM for one section; parse, validate, return."""
    prompt = _build_prompt(schema_class.model_json_schema(), document)
    response = await client.messages.create(
        model=EXTRACT_MODEL,
        max_tokens=EXTRACT_MAX_TOKENS,
        messages=[{"role": "user", "content": prompt}],
    )
    block = response.content[0]
    if not hasattr(block, "text"):
        raise ValueError(f"Unexpected response block type: {type(block).__name__}")
    return schema_class.model_validate(json.loads(_strip_fences(block.text)))


async def extract_fields(
    state: ExtractionState,
    client: AsyncAnthropic | None = None,
) -> ExtractionState:
    """
    Call Claude per section, in parallel, to populate the LeaseExtraction model.

    Vision support for pages_needing_vision is not yet implemented — text-only.
    The optional client parameter exists for dependency injection in tests.
    """
    if state.error:
        return state
    if not state.raw_text:
        state.status = "extracted"
        return state

    if client is None:
        client = AsyncAnthropic()

    try:
        template_task = asyncio.create_task(_detect_template(client, state.raw_text))
        section_tasks = [
            _extract_section(client, model, state.raw_text) for model in SECTION_MODELS.values()
        ]
        section_results = await asyncio.gather(*section_tasks)
        template = await template_task
        sections = dict(zip(SECTION_MODELS.keys(), section_results, strict=True))

        parties_section = sections["parties"]
        assert isinstance(parties_section, PartiesSection)

        extraction = LeaseExtraction(
            lease_id=state.document_id,
            template_detected=template,
            parties=parties_section.parties,
            property=sections["property"],  # type: ignore[arg-type]
            term=sections["term"],  # type: ignore[arg-type]
            rent=sections["rent"],  # type: ignore[arg-type]
            deposits=sections["deposits"],  # type: ignore[arg-type]
            utilities=sections["utilities"],  # type: ignore[arg-type]
            pets=sections["pets"],  # type: ignore[arg-type]
            special_clauses=sections["special_clauses"],  # type: ignore[arg-type]
            compliance=sections["compliance"],  # type: ignore[arg-type]
            overall_confidence=0.0,  # set below from the assembled tree
        )
        confidences = [f.confidence for _, f in _walk_extracted_fields(extraction)]
        extraction.overall_confidence = sum(confidences) / max(len(confidences), 1)

        state.extraction = extraction
        state.status = "extracted"
    except Exception as exc:  # noqa: BLE001 — node boundary: surface failures via state.error
        state.error = f"Extraction failed: {type(exc).__name__}: {exc}"
        state.status = "extract_failed"
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
    if state.error:
        return state
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

async def persist_results(state: ExtractionState) -> ExtractionState:
    """
    Upsert the lease record + insert exceptions via SQLAlchemy.

    Uses the shared async engine from db.py. Idempotent on lease_id: re-running
    an extraction updates the existing row in place. Exceptions are inserted
    fresh per run (assumes the caller cleared prior unresolved exceptions if
    re-extracting; resolved exceptions are preserved for audit).
    """
    from db import AsyncSessionLocal, ExceptionRecord, LeaseRecord

    async with AsyncSessionLocal() as session:
        lease = await session.get(LeaseRecord, state.document_id)
        if lease is None:
            lease = LeaseRecord(
                lease_id=state.document_id,
                pdf_url=state.pdf_url,
                status=state.status,
            )
            session.add(lease)
        lease.status = state.status if state.error else "complete"
        lease.raw_text = state.raw_text
        lease.error = state.error
        if state.extraction is not None:
            lease.extraction = state.extraction.model_dump(mode="json")

        for exc in state.exceptions:
            session.add(
                ExceptionRecord(
                    exception_id=exc.exception_id,
                    lease_id=exc.lease_id,
                    field_path=exc.field_path,
                    exception_type=exc.exception_type.value,
                    severity=exc.severity.value,
                    description=exc.description,
                    suggested_action=exc.suggested_action,
                    resolved=exc.resolved,
                    resolution=exc.resolution.value if exc.resolution else None,
                    correction=exc.correction,
                )
            )

        await session.commit()

    state.status = lease.status
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
async def run_extraction(pdf_url: str, document_id: UUID | None = None) -> ExtractionState:
    graph = build_graph()
    initial = ExtractionState(document_id=document_id or uuid4(), pdf_url=pdf_url)
    final_state = await graph.ainvoke(initial)
    return ExtractionState.model_validate(final_state)
