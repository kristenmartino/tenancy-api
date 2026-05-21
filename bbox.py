"""
Snippet-anchored bbox derivation from the OCR'd PDF text layer.

Replaces LLM-emitted bboxes (which had ~3-8% positional drift on Sonnet
vision) with coordinates pulled directly from the PDF text layer that
ocrmypdf wrote during ingest. The Sonnet output supplies the snippet text;
this module finds it in the page's word stream and returns one
`BoundingBox` per line — matching the PDF spec's QuadPoints highlight
model (what Adobe, Mendeley, etc. use for multi-line selections).

Industry pattern. docTR, Surya, PaddleOCR, Textract, Mindee, Landing.AI
all keep coordinates owned by the OCR/layout layer and never let the
LLM emit them. Sonnet keeps the semantics (value, snippet, match_type);
this module owns the geometry.
"""
from __future__ import annotations

import io
import re
import sys

import pdfplumber
from rapidfuzz import fuzz

from schemas import BoundingBox, SourceMatchType

# rapidfuzz partial_ratio score (0-100). 80 catches OCR noise like
# misread characters and spurious whitespace; below ~70 starts matching
# unrelated phrases.
DEFAULT_MIN_SCORE = 80

# Two word bboxes within this y-distance (PDF points; 1pt = 1/72 inch)
# are considered on the same line. ~3pt covers typical line-spacing
# wiggle without merging adjacent lines.
DEFAULT_LINE_Y_TOL_PT = 3.0

# Horizontal gap (PDF points) between matched words on the "same line"
# that forces a column-split — words on opposite sides of a wide gap
# are treated as separate lines even if their `top` matches. 50pt ≈
# 0.7 inch, comfortably larger than typical inter-word spacing.
COLUMN_GAP_PT = 50.0


def _normalize_ws(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def derive_bboxes(
    pdf_bytes: bytes,
    page_number: int,
    snippet: str,
    match_type: SourceMatchType | str,
    *,
    min_score: int = DEFAULT_MIN_SCORE,
    line_y_tol_pt: float = DEFAULT_LINE_Y_TOL_PT,
) -> list[BoundingBox]:
    """Locate `snippet` on `page_number` of `pdf_bytes`, return per-line bboxes.

    Returns `[]` for any of:
      - `match_type` is `absent` or `checkbox` (no text to align to)
      - empty / whitespace-only snippet
      - page out of range
      - no words on the page (image-only page that OCR couldn't read)
      - fuzzy-match score below `min_score` (snippet not credibly present)

    Never raises — failure modes return `[]` so the caller can fall back
    to "navigate to page, no overlay".
    """
    if match_type in {"absent", "checkbox"}:
        return []
    snippet_norm = _normalize_ws(snippet)
    if not snippet_norm:
        return []

    try:
        with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
            if page_number < 1 or page_number > len(pdf.pages):
                return []
            page = pdf.pages[page_number - 1]
            words = page.extract_words(use_text_flow=True)
            if not words:
                return []

            haystack, word_index_at_char = _build_haystack(words)
            if not haystack:
                return []

            ali = fuzz.partial_ratio_alignment(snippet_norm, haystack)
            if ali is None or ali.score < min_score:
                return []

            matched_word_idxs = {
                word_index_at_char[i]
                for i in range(ali.dest_start, ali.dest_end)
                if 0 <= i < len(word_index_at_char)
                and word_index_at_char[i] is not None
            }
            if not matched_word_idxs:
                return []

            matched_words = [words[i] for i in sorted(matched_word_idxs)]
            lines = _group_into_lines(matched_words, line_y_tol_pt)
            page_w = float(page.width)
            page_h = float(page.height)
            return [_line_to_bbox(line, page_w, page_h) for line in lines]
    except Exception as exc:  # noqa: BLE001 — never break the pipeline on a bbox failure
        print(
            f"[bbox] derive_bboxes failed (page={page_number}, "
            f"snippet={snippet[:40]!r}): {type(exc).__name__}: {exc}",
            file=sys.stderr,
        )
        return []


def _build_haystack(words: list[dict]) -> tuple[str, list[int | None]]:
    """Concatenate word texts into a single string with a parallel index
    mapping each character back to its source word index. Separator
    characters between words map to None.
    """
    parts: list[str] = []
    index: list[int | None] = []
    for i, w in enumerate(words):
        text = _normalize_ws(w["text"])
        if not text:
            continue
        if parts:
            parts.append(" ")
            index.append(None)
        for _ in text:
            index.append(i)
        parts.append(text)
    return "".join(parts), index


def _group_into_lines(
    matched_words: list[dict],
    line_y_tol_pt: float,
) -> list[list[dict]]:
    """Bucket matched words into lines by `top` coordinate; split lines
    across wide horizontal gaps (multi-column safety).
    """
    sorted_words = sorted(matched_words, key=lambda w: (w["top"], w["x0"]))
    lines: list[list[dict]] = []
    for w in sorted_words:
        if not lines:
            lines.append([w])
            continue
        current = lines[-1]
        same_line_y = abs(w["top"] - current[-1]["top"]) <= line_y_tol_pt
        if not same_line_y:
            lines.append([w])
            continue
        # Same y — but a wide horizontal gap means a different column.
        current_x1 = max(ww["x1"] for ww in current)
        if w["x0"] - current_x1 > COLUMN_GAP_PT:
            lines.append([w])
        else:
            current.append(w)
    return lines


def _line_to_bbox(line: list[dict], page_w: float, page_h: float) -> BoundingBox:
    x0 = min(w["x0"] for w in line)
    x1 = max(w["x1"] for w in line)
    top = min(w["top"] for w in line)
    bottom = max(w["bottom"] for w in line)
    x = max(0.0, min(1.0, x0 / page_w))
    y = max(0.0, min(1.0, top / page_h))
    width = max(0.001, min(1.0 - x, (x1 - x0) / page_w))
    height = max(0.001, min(1.0 - y, (bottom - top) / page_h))
    return BoundingBox(x=x, y=y, width=width, height=height)
