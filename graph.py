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
import base64
import io
import json
import os
import sys
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
    pdf_bytes: bytes | None = Field(default=None, exclude=True, repr=False)
    page_count: int = 0
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
OCR_ENABLED = os.getenv("OCR_ENABLED", "true").lower() in {"true", "1", "yes"}


def _extract_text_per_page(pdf_bytes: bytes) -> tuple[str, list[int], int]:
    """Return (joined raw_text, pages_needing_vision, page_count)."""
    reader = PdfReader(io.BytesIO(pdf_bytes))
    page_texts: list[str] = []
    pages_needing_vision: list[int] = []
    for i, page in enumerate(reader.pages, start=1):
        text = page.extract_text() or ""
        page_texts.append(f"[PAGE {i}]\n{text}")
        if len(text.strip()) < MIN_TEXT_LEN_PER_PAGE:
            pages_needing_vision.append(i)
    return "\n\n".join(page_texts), pages_needing_vision, len(reader.pages)


def _run_ocrmypdf(pdf_bytes: bytes) -> bytes:
    """Sync helper: write bytes, run ocrmypdf, read back. Heavy; call via to_thread."""
    import contextlib
    import tempfile
    from pathlib import Path

    import ocrmypdf

    with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as in_f:
        in_f.write(pdf_bytes)
        in_path = in_f.name
    out_path = in_path.replace(".pdf", ".ocr.pdf")
    try:
        ocrmypdf.ocr(
            in_path,
            out_path,
            skip_text=True,            # leave pages that already have text alone
            optimize=0,                 # skip image optimization for speed
            progress_bar=False,
            language="eng",
            jobs=2,
            output_type="pdf",          # plain PDF (not PDF/A) — PDF.js's
                                        # text-layer renderer is more reliable
                                        # against vanilla PDFs than the
                                        # default PDF/A output
        )
        return Path(out_path).read_bytes()
    finally:
        for p in (in_path, out_path):
            with contextlib.suppress(OSError):
                Path(p).unlink(missing_ok=True)


async def _maybe_ocr(pdf_bytes: bytes, pages_needing_vision: list[int]) -> bytes:
    """Run ocrmypdf if text extraction missed pages. No-op when OCR_ENABLED=false.

    Failures are logged but swallowed — vision fallback will still pick up
    the slack in extract. Logged loudly so deploy issues are visible.
    """
    if not OCR_ENABLED:
        print("[ocr] skipped: OCR_ENABLED=false", file=sys.stderr)
        return pdf_bytes
    if not pages_needing_vision:
        print("[ocr] skipped: no pages need vision", file=sys.stderr)
        return pdf_bytes
    print(
        f"[ocr] starting on {len(pages_needing_vision)} pages "
        f"({len(pdf_bytes)} bytes input)",
        file=sys.stderr,
    )
    try:
        result = await asyncio.to_thread(_run_ocrmypdf, pdf_bytes)
        print(
            f"[ocr] done — {len(result)} bytes output "
            f"({'changed' if result is not pdf_bytes else 'unchanged'})",
            file=sys.stderr,
        )
        return result
    except Exception as exc:  # noqa: BLE001 — best-effort; never fail ingest
        print(
            f"[ocr] FAILED ({type(exc).__name__}): {exc} — "
            f"continuing with original bytes",
            file=sys.stderr,
        )
        return pdf_bytes


async def _fetch_pdf(url: str) -> bytes:
    async with httpx.AsyncClient(timeout=PDF_FETCH_TIMEOUT) as client:
        resp = await client.get(url, follow_redirects=True)
        resp.raise_for_status()
        ctype = resp.headers.get("content-type", "").lower()
        if "pdf" not in ctype and not resp.content.startswith(b"%PDF-"):
            preview = resp.content[:40].decode("utf-8", errors="replace")
            raise ValueError(
                f"URL returned {ctype or 'unknown content-type'}, not a PDF "
                f"(first bytes: {preview!r}). The URL may be a login wall, "
                f"an HTML error page, or a hotlink redirect."
            )
        return resp.content


async def ingest_document(state: ExtractionState) -> ExtractionState:
    """
    Fetch the PDF (or use pre-supplied bytes), extract text via pypdf, and
    OCR any image-only pages so PDF.js (and downstream extraction) can see
    real text.

    If state.pdf_bytes is already populated (e.g. from a file upload at the
    API layer), the HTTP fetch is skipped. Otherwise we fetch state.pdf_url.

    OCR pass: when pypdf reports pages with insufficient text, we run the
    bytes through ocrmypdf to add a hidden searchable text layer, then
    re-extract. The OCR'd bytes replace state.pdf_bytes so the frontend
    viewer can use the new text layer for click-to-highlight.
    """
    if state.pdf_bytes is None:
        try:
            state.pdf_bytes = await _fetch_pdf(state.pdf_url)
        except (httpx.HTTPError, ValueError) as exc:
            state.error = f"Failed to fetch PDF: {exc}"
            state.status = "ingest_failed"
            return state

    try:
        raw_text, pages_needing_vision, page_count = _extract_text_per_page(
            state.pdf_bytes,
        )
        state.page_count = page_count

        if pages_needing_vision:
            # Some pages are image-only — run OCR, then re-extract.
            ocr_bytes = await _maybe_ocr(state.pdf_bytes, pages_needing_vision)
            if ocr_bytes is not state.pdf_bytes:
                state.pdf_bytes = ocr_bytes
                raw_text, pages_needing_vision, page_count = _extract_text_per_page(
                    ocr_bytes,
                )
                state.page_count = page_count

        state.raw_text = raw_text
        state.pages_needing_vision = pages_needing_vision
        state.status = "ingested"
    except Exception as exc:  # noqa: BLE001 — node boundary: pypdf raises a zoo of types
        state.error = f"Failed to parse PDF: {type(exc).__name__}: {exc}"
        state.status = "ingest_failed"
    return state


# ---------------------------------------------------------------------------
# Node: extract
# ---------------------------------------------------------------------------

EXTRACTION_PROMPT = """You are extracting structured data from a residential lease agreement.

You are given two views of the lease:
  1. Page images — one per page, rendered from the source PDF.
  2. OCR'd text — the lease text, after the document boundary marker.

How to use them together:
  - For dense text fields (parties, addresses, dates, dollar amounts, written
    clauses), prefer the OCR'd text. It is exact at the character level.
  - For visual fields (checkboxes, signatures, initials, X marks, hand-filled
    marks, presence/absence of stamps), ALWAYS decide from the page image, NOT
    from the OCR text. Tesseract (the OCR engine) routinely reads ink bleed,
    print/scan artifacts, and stray pixels as characters or marks — a checkbox
    with a slightly thicker top border can OCR as a checked box even when it
    is visibly empty. The pixels are the ground truth for whether a box is
    checked, a signature is present, or an initial line is filled.

Extract the specified section and return ONLY a JSON object matching the schema.
Every field must include:
  - value: the extracted value (or null if not present)
  - confidence: 0.0 to 1.0, your honest confidence
  - source: {page_number, char_start, char_end, snippet, match_type, section_label}
            pointing to where in the document this came from:
            * page_number: 1-indexed
            * char_start/char_end: character offsets in the page's OCR text
              (your best estimate; downstream code aligns the snippet against
              the OCR text layer regardless)
            * snippet: verbatim text supporting the extraction. For typed/
              printed values: the exact characters as they appear. For blanks:
              the labeling text immediately around the blank ("day of (month),
              ___(year)", "$_____ per month"). For checkboxes/enums: the label
              text adjacent to the box ("electric", "lead paint disclosure").
              This snippet is what downstream code finds in the PDF text layer
              to draw the highlight box, so it MUST appear verbatim in the
              document (modulo OCR noise).
            * match_type: how this field was located. One of:
              - "filled": a typed/printed value is visibly present (e.g.
                "1621 James Ave Waco, TX 76706" written into an address line).
              - "blank": there's a visible labeled placeholder (underline,
                empty parens, "$_____", blank line after a colon) but no
                value is filled in. Snippet should be the label + placeholder.
              - "inferred": the value is implied by surrounding prose, not a
                fillable field. Snippet should be the supporting phrase.
              - "checkbox": the value comes from a checked/unchecked box.
                Snippet should be the label adjacent to the box. ALWAYS use
                this match_type for fields decided from visual marks, whether
                the box was checked or empty — the downstream renderer needs
                to know this is a visual-only field.
              - "absent": the document doesn't address this field at all.
                Set value to null. (snippet may be empty for "absent".)
            * section_label: the document's printed heading for the section
              this field sits under, e.g. "3. Lease Term", "Utilities and
              Services", "Federally Required Lead Hazard Disclosure". Null if
              there is no clear heading.
  - notes: optional, only if ambiguity needs flagging

Do NOT emit bbox coordinates. Bbox is derived server-side by aligning your
snippet against the OCR'd PDF's word positions — your job is to tell us what
text to find, not where to draw the box.

Do not hallucinate. If a field is not stated in the document at all, set value
to null, confidence to 1.0, match_type to "absent".

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
# Resolution for the page-image render passed to the extraction call.
# 150 DPI ≈ 1275x1650 for a letter-sized page — legible for checkboxes /
# signatures without ballooning image-token cost (which grows ~quadratically
# with DPI). Higher resolutions don't measurably improve the model's read.
PAGE_RENDER_DPI = int(os.getenv("PAGE_RENDER_DPI", "150"))
# Anthropic's many-image API caps each image at 2000 pixels on its longest
# side. We send 9 section calls × N pages per lease, so every render is a
# many-image request. Anything larger 400s the whole extraction. Letter at
# 150 DPI fits comfortably; legal / A3 / oversized scans do not.
MAX_IMAGE_DIM_PX = int(os.getenv("MAX_IMAGE_DIM_PX", "1950"))


TEMPLATE_DETECTION_PROMPT = """Classify the following residential lease excerpt as one of these templates:

- taa: Texas Apartment Association
- naa: National Apartment Association
- ca_residential: California residential lease
- fl_residential: Florida residential lease
- unknown: doesn't clearly match any of the above

Return ONLY the lowercase template code. No preamble, no explanation.

Lease excerpt:
{excerpt}"""


def _render_pages_to_pngs(pdf_bytes: bytes, dpi: int = PAGE_RENDER_DPI) -> list[bytes]:
    """Render every page of the PDF to a PNG. Sync; call via to_thread.

    Used to attach visual context to the extraction call so the model can
    ground checkbox / signature / hand-fill fields in pixels rather than in
    OCR character classification (Tesseract over-reads ink bleed and stray
    pixels as marks, producing false-positive checked boxes).
    """
    import pypdfium2 as pdfium

    pdf = pdfium.PdfDocument(pdf_bytes)
    target_scale = dpi / 72  # PDF user-space unit is 1/72 inch
    pngs: list[bytes] = []
    try:
        for page in pdf:
            # Cap render so the longest side stays under Anthropic's
            # 2000-pixel many-image limit. max(w, h) is rotation-invariant
            # so this works for portrait, landscape, and rotated pages.
            page_w, page_h = page.get_size()
            longest_pt = max(page_w, page_h)
            scale = min(target_scale, MAX_IMAGE_DIM_PX / longest_pt)
            bitmap = page.render(scale=scale)
            pil = bitmap.to_pil()
            buf = io.BytesIO()
            pil.save(buf, format="PNG")
            pngs.append(buf.getvalue())
    finally:
        pdf.close()
    return pngs


async def _render_pages(pdf_bytes: bytes) -> list[bytes]:
    """Async wrapper — runs CPU-bound pdfium render off the event loop."""
    return await asyncio.to_thread(_render_pages_to_pngs, pdf_bytes)


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
    page_images: list[bytes] | None = None,
) -> BaseModel:
    """Call the LLM for one section; parse, validate, return.

    When page_images is provided, each PNG is attached as an image block
    ahead of the prompt so the model can ground visual fields (checkboxes,
    signatures, hand-fill) in pixels rather than in OCR character output.
    The OCR'd text stays in the prompt for character-level accuracy on
    dense text — the two are complementary.
    """
    prompt = _build_prompt(schema_class.model_json_schema(), document)
    content: list[dict[str, Any]] = []
    if page_images:
        for png in page_images:
            content.append(
                {
                    "type": "image",
                    "source": {
                        "type": "base64",
                        "media_type": "image/png",
                        "data": base64.b64encode(png).decode(),
                    },
                }
            )
    content.append({"type": "text", "text": prompt})
    response = await client.messages.create(
        model=EXTRACT_MODEL,
        max_tokens=EXTRACT_MAX_TOKENS,
        messages=[{"role": "user", "content": content}],  # type: ignore[typeddict-item]
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

    Each section call attaches every page rendered as a PNG image block, so the
    model can ground visual fields (checkboxes, signatures, hand-fill) in pixels
    instead of trusting OCR character classification. OCR text stays in the
    prompt for dense text — they're complementary.

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
        # Render every page to a PNG once and reuse across the 9 section calls.
        # Costs more image tokens (sent per section) but unlocks visual
        # grounding so the model doesn't trust noisy OCR reads for checkboxes
        # / signatures / hand-fill. Render failures are best-effort: fall back
        # to text-only rather than failing the whole extraction.
        page_images: list[bytes] | None = None
        if state.pdf_bytes is not None:
            try:
                page_images = await _render_pages(state.pdf_bytes)
            except Exception as exc:  # noqa: BLE001 — best-effort; never fail extraction over render
                print(
                    f"[render] FAILED ({type(exc).__name__}): {exc} — "
                    f"extraction will run text-only",
                    file=sys.stderr,
                )

        template_task = asyncio.create_task(_detect_template(client, state.raw_text))
        section_tasks = [
            _extract_section(client, model, state.raw_text, page_images)
            for model in SECTION_MODELS.values()
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

        if state.pdf_bytes is not None:
            _attach_derived_bboxes(extraction, state.pdf_bytes)

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


def _attach_derived_bboxes(extraction: LeaseExtraction, pdf_bytes: bytes) -> None:
    """For every ExtractedField with a source, replace `source.bboxes` with
    coords derived from the OCR'd PDF's text layer via snippet alignment.

    Mutates the extraction in place. Failures (no match, OCR gaps, page
    out of range) silently leave `bboxes = []`, which the frontend
    renders as "navigate to page, no overlay". match_type ∈ {absent,
    checkbox} is also a no-op here — checkbox geometry will arrive from
    a Textract follow-up; absent fields have no location to point at.
    """
    from bbox import derive_bboxes  # local import to keep startup lean

    for _path, field in _walk_extracted_fields(extraction):
        src = field.source
        if src is None or not src.snippet:
            continue
        try:
            src.bboxes = derive_bboxes(
                pdf_bytes,
                src.page_number,
                src.snippet,
                src.match_type,
            )
        except Exception as exc:  # noqa: BLE001 — bbox failures must not break extraction
            print(
                f"[bbox] derivation failed at {_path}: "
                f"{type(exc).__name__}: {exc}",
                file=sys.stderr,
            )
            src.bboxes = []


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
        if state.pdf_bytes is not None:
            lease.pdf_bytes = state.pdf_bytes
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
async def run_extraction(
    pdf_url: str,
    document_id: UUID | None = None,
    pdf_bytes: bytes | None = None,
) -> ExtractionState:
    graph = build_graph()
    initial = ExtractionState(
        document_id=document_id or uuid4(),
        pdf_url=pdf_url,
        pdf_bytes=pdf_bytes,
    )
    final_state = await graph.ainvoke(initial)
    return ExtractionState.model_validate(final_state)
