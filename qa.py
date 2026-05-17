"""
Grounded Q&A over a lease extraction.

Single Claude Haiku call: the user's question + the structured extraction as
JSON. The model is instructed to answer only from the extraction and cite
every claim with a field_path (and source span when present). Lighter and
faster than re-reading the raw PDF for each question.
"""
from __future__ import annotations

import json
import os
from typing import Any

from anthropic import AsyncAnthropic
from pydantic import BaseModel, Field

QA_MODEL = os.getenv("QA_MODEL", "claude-haiku-4-5-20251001")
QA_MAX_TOKENS = int(os.getenv("QA_MAX_TOKENS", "1024"))


class Citation(BaseModel):
    field_path: str
    page_number: int | None = None
    snippet: str | None = None


class QAResponse(BaseModel):
    answer: str
    citations: list[Citation] = Field(default_factory=list)


QA_SYSTEM_PROMPT = """You are answering questions about a residential lease.

Use ONLY the structured extraction provided. Do NOT invent facts. If the
extraction does not contain the answer, say so honestly.

Every claim must be backed by a citation pointing to a field_path in the
extraction (e.g. "rent.base_monthly_rent", "term.end_date"). When the field
has a source span (page_number + snippet), include those in the citation.

Return ONLY a JSON object matching this shape:
{
  "answer": "plain-English answer",
  "citations": [
    {"field_path": "...", "page_number": <int or null>, "snippet": "..."}
  ]
}

No preamble, no markdown fences."""


def _strip_fences(text: str) -> str:
    text = text.strip()
    if text.startswith("```"):
        text = text.split("\n", 1)[1] if "\n" in text else text[3:]
    if text.endswith("```"):
        text = text.rsplit("```", 1)[0]
    return text.strip()


async def answer_question(
    extraction: dict[str, Any],
    question: str,
    client: AsyncAnthropic | None = None,
) -> QAResponse:
    """Call Claude Haiku grounded on the extraction; parse + validate response."""
    if client is None:
        client = AsyncAnthropic()
    user_msg = (
        f"Lease extraction:\n```json\n{json.dumps(extraction, indent=2)}\n```\n\n"
        f"Question: {question}"
    )
    response = await client.messages.create(
        model=QA_MODEL,
        max_tokens=QA_MAX_TOKENS,
        system=QA_SYSTEM_PROMPT,
        messages=[{"role": "user", "content": user_msg}],
    )
    block = response.content[0]
    if not hasattr(block, "text"):
        raise ValueError(f"Unexpected response block type: {type(block).__name__}")
    return QAResponse.model_validate_json(_strip_fences(block.text))
