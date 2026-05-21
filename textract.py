"""
Textract-backed bbox derivation for checkbox / selection-element fields.

Companion to `bbox.py`. The OCR-anchored path in `bbox.py` covers
typed/printed text fields by aligning Sonnet's `snippet` against
pdfplumber's word positions. That path returns `[]` for
`match_type=checkbox` because there is no text glyph to anchor to — the
box itself is a visual mark Tesseract can't reason about.

This module fills that gap. Amazon Textract's `AnalyzeDocument` with
`FeatureTypes=["FORMS"]` emits `SELECTION_ELEMENT` blocks with
normalized geometry + a `SelectionStatus`. We extract the requested page
as a single-page PDF, send it to Textract, and for each checkbox field:

  1. find the LINE block whose text best fuzzy-matches Sonnet's snippet
     (which the prompt asks to be the label adjacent to the box)
  2. find the SELECTION_ELEMENT block nearest to that line
  3. union the two boxes so the highlight covers checkbox + label as
     a single rect — matches the v1 prompt's "checkbox tightness rule"
     intent (the human reviewer needs to see both the box and what it
     labels in a single glance)

Opt-in via `TEXTRACT_ENABLED=1`. Disabled by default — keeps unit tests,
local dev, and any deploy without AWS creds zero-cost (no boto3 client,
no AWS calls). Requires `AWS_REGION`, `AWS_ACCESS_KEY_ID`,
`AWS_SECRET_ACCESS_KEY` (or IAM role). Defaults to `us-east-1` if
`AWS_REGION` is unset.

Pricing as of 2026: AnalyzeDocument Forms = $0.05 / page. For the
demo's typical 10-page lease that's $0.50 per upload — fine for a
portfolio demo, would want batched async analysis (`start_document_analysis`)
in production.

UN-SMOKE-TESTED IN THE PR THAT INTRODUCED THIS FILE. The Claude sandbox
that wrote it had no AWS credentials. Verify against a real lease with
checkbox fields before relying on it for the demo.
"""
from __future__ import annotations

import io
import os
import sys
from typing import Any

from rapidfuzz import fuzz

from schemas import BoundingBox

# Override the default `us-east-1` region. Textract is regional —
# pick whichever is closest to Railway's deploy region.
DEFAULT_AWS_REGION = "us-east-1"

# rapidfuzz partial_ratio threshold for matching Sonnet's snippet
# against a LINE block's text. Looser than `bbox.py`'s 80 because
# Textract's text-extraction differs from Tesseract's — different
# pre-processing, different whitespace, different OCR — and we're
# matching short labels ("electric", "gas", "lead paint disclosure")
# where false-positive risk is lower.
DEFAULT_MIN_LINE_SCORE = 70


def textract_enabled() -> bool:
    """Whether to attempt a Textract call. Gate is opt-in so no AWS bill
    surprises on fresh deploys."""
    return os.getenv("TEXTRACT_ENABLED", "").lower() in {"1", "true", "yes"}


def derive_checkbox_bbox(
    pdf_bytes: bytes,
    page_number: int,
    snippet: str,
    *,
    min_line_score: int = DEFAULT_MIN_LINE_SCORE,
) -> BoundingBox | None:
    """Locate the SELECTION_ELEMENT on `page_number` whose adjacent label
    text fuzzy-matches `snippet`. Return a single BoundingBox covering
    both the checkbox glyph and its label (unioned), or None on any
    failure (Textract disabled, snippet not found, AWS error, etc.).

    Returns a SINGLE BoundingBox, not a list. The caller wraps it in a
    list when assigning to `SourceSpan.bboxes` to match the per-line
    array contract for typed fields. A checkbox is logically one mark,
    not a multi-line span.

    Never raises — all failure modes return None so the caller falls
    back to "navigate to page, no overlay".
    """
    if not textract_enabled():
        return None
    if not snippet or not snippet.strip():
        return None

    try:
        import boto3
    except ImportError as exc:
        print(
            f"[textract] boto3 not installed but TEXTRACT_ENABLED=1: {exc}",
            file=sys.stderr,
        )
        return None

    try:
        single_page_pdf = _single_page_pdf_bytes(pdf_bytes, page_number)
        if single_page_pdf is None:
            return None

        client = boto3.client(
            "textract",
            region_name=os.getenv("AWS_REGION", DEFAULT_AWS_REGION),
        )
        response = client.analyze_document(
            Document={"Bytes": single_page_pdf},
            FeatureTypes=["FORMS"],
        )
        blocks: list[dict[str, Any]] = response.get("Blocks", [])

        selection_blocks = [
            b for b in blocks if b.get("BlockType") == "SELECTION_ELEMENT"
        ]
        line_blocks = [b for b in blocks if b.get("BlockType") == "LINE"]
        if not selection_blocks or not line_blocks:
            return None

        best_line = _best_matching_line(line_blocks, snippet, min_line_score)
        if best_line is None:
            return None

        nearest_selection = _nearest_block(selection_blocks, best_line)
        if nearest_selection is None:
            return None

        return _union_bbox(
            best_line["Geometry"]["BoundingBox"],
            nearest_selection["Geometry"]["BoundingBox"],
        )
    except Exception as exc:  # noqa: BLE001 — never break the pipeline on a bbox failure
        print(
            f"[textract] derive_checkbox_bbox failed "
            f"(page={page_number}, snippet={snippet[:40]!r}): "
            f"{type(exc).__name__}: {exc}",
            file=sys.stderr,
        )
        return None


def _single_page_pdf_bytes(pdf_bytes: bytes, page_number: int) -> bytes | None:
    """Extract `page_number` as a fresh single-page PDF. Textract's sync
    AnalyzeDocument accepts single-page PDFs as `Bytes`; multi-page would
    require S3 + the async start_document_analysis flow.
    """
    from pypdf import PdfReader, PdfWriter

    reader = PdfReader(io.BytesIO(pdf_bytes))
    if page_number < 1 or page_number > len(reader.pages):
        return None
    writer = PdfWriter()
    writer.add_page(reader.pages[page_number - 1])
    buf = io.BytesIO()
    writer.write(buf)
    return buf.getvalue()


def _best_matching_line(
    line_blocks: list[dict[str, Any]],
    snippet: str,
    min_score: int,
) -> dict[str, Any] | None:
    best_block = None
    best_score = 0
    for block in line_blocks:
        text = (block.get("Text") or "").strip()
        if not text:
            continue
        score = fuzz.partial_ratio(snippet, text)
        if score > best_score:
            best_score = score
            best_block = block
    return best_block if best_score >= min_score else None


def _nearest_block(
    candidates: list[dict[str, Any]],
    reference: dict[str, Any],
) -> dict[str, Any] | None:
    """Return the candidate whose bbox center is closest to the
    reference's bbox center (squared Euclidean, normalized coords)."""
    ref_bb = reference["Geometry"]["BoundingBox"]
    ref_cx = ref_bb["Left"] + ref_bb["Width"] / 2
    ref_cy = ref_bb["Top"] + ref_bb["Height"] / 2

    def _dist_sq(c: dict[str, Any]) -> float:
        bb = c["Geometry"]["BoundingBox"]
        cx = bb["Left"] + bb["Width"] / 2
        cy = bb["Top"] + bb["Height"] / 2
        return (cx - ref_cx) ** 2 + (cy - ref_cy) ** 2

    return min(candidates, key=_dist_sq) if candidates else None


def _union_bbox(
    a: dict[str, float],
    b: dict[str, float],
) -> BoundingBox:
    """Smallest rect containing both `a` and `b`. Textract gives Left/Top/
    Width/Height in normalized 0-1 coords — same convention as our
    BoundingBox model, just renamed fields."""
    left = min(a["Left"], b["Left"])
    top = min(a["Top"], b["Top"])
    right = max(a["Left"] + a["Width"], b["Left"] + b["Width"])
    bottom = max(a["Top"] + a["Height"], b["Top"] + b["Height"])
    return BoundingBox(
        x=max(0.0, min(1.0, left)),
        y=max(0.0, min(1.0, top)),
        width=max(0.001, min(1.0 - left, right - left)),
        height=max(0.001, min(1.0 - top, bottom - top)),
    )
