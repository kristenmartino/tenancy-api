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

from uuid import UUID, uuid4

from langgraph.graph import END, StateGraph
from pydantic import BaseModel, Field

from schemas import LeaseException, LeaseExtraction

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

def validate_extraction(state: ExtractionState) -> ExtractionState:
    """
    Rule-based validation. Generates exceptions for the review queue.

    Checks:
      - Required fields present (parties, property, term, rent.base_monthly_rent,
        deposits.security_deposit, compliance.lead_paint_disclosure)
      - term.end_date > term.start_date
      - rent.base_monthly_rent > 0
      - deposits.security_deposit <= 3x base_monthly_rent (state caps vary; flag if exceeded)
      - rent.late_fee_flat consistent with grace_period_days
      - For pre-1978 properties: lead_paint_disclosure must be True
      - Low-confidence fields (confidence < 0.7) -> LOW_CONFIDENCE exception
      - Unusual clauses (e.g. waiver of military clause) -> UNUSUAL_CLAUSE exception
    """
    # TODO: implement rules; emit LeaseException for each violation
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
