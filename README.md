# Tenancy

**Residential lease extraction and review prototype.** Upload a lease PDF, inspect structured fields and available source references, and resolve flagged exceptions. Source references are optional in the schema; missing or incorrect citations still require checking against the PDF.

Built as a portfolio demonstration of PDF extraction and human review. It has no measured portfolio-scale throughput, labeled accuracy benchmark, or property-management-system integration.

Live at `tenancy.kristenmartino.ai`. API at https://tenancy-api-production.up.railway.app (Railway) backed by Neon Postgres.

```bash
# Try it
curl -X POST https://tenancy-api-production.up.railway.app/leases \
  -H 'Content-Type: application/json' \
  -d '{"pdf_url":"https://www.sanantonio.gov/Portals/0/Files/NHSD/Programs/FairHousing/LeaseAgreement.pdf"}'
# {"lease_id":"...", "status":"pending"}  → poll GET /leases/{id} until status=complete
```

---

## Current scope and review boundary

- Extracts residential lease fields and asks the model for page, character-span, and snippet references. `ExtractedField.source` may be `None`; a reference is not proof that the value is correct.
- Reports model-supplied confidence. These scores are not calibrated accuracy estimates, including `1.0` for absent values or human edits.
- Runs deterministic checks for dates, amounts, required fields, and low confidence. The deposit check is a fixed heuristic, not a state-law compliance engine.
- **Persists extraction and exceptions before human review.** `ready_to_proceed` reflects extraction status and blocking-exception resolution. It does not gate initial database writes or export to an external system.
- Provides text highlighting where source matching yields coordinates. Visual-mark geometry and extraction accuracy evaluation remain incomplete.

The demo uses residential lease templates. Operator demand, acquisition volumes, competitor capabilities, and savings have not been established by this repository. Yardi, RealPage, AppFolio, and Entrata are possible integration targets, not implemented connectors.

## What it does

1. **Ingest** — accepts a residential lease PDF (TAA, NAA, or state template) via URL or direct upload. Pulls text with pypdf. Flags low-text pages for vision fallback.
2. **Extract** — LangGraph agent calls Claude Sonnet 4.6 across nine sections in parallel (parties, property, term, rent, deposits, utilities, pets, special clauses, compliance). Each call attaches every page rendered as a PNG (via `pypdfium2`, ~150 DPI) alongside the OCR'd text — the model grounds visual fields (checkboxes, signatures, X marks, hand-fill) in pixels and uses OCR for character-exact dense text (parties, addresses, dollar amounts). Page images give the model visual context, but do not eliminate OCR or checkbox errors. Fields carry `value`, model-reported `confidence`, and an optional source span. Claude Haiku 4.5 runs in parallel to classify the lease against the template enum.
3. **Validate** — seven deterministic rules (date consistency, rent math, deposit-cap heuristic, late-fee/grace-period pairing, lead-paint disclosure presence, required-but-null check, and a recursive low-confidence sweep) emit `LeaseException` rows with severity (`BLOCKING`, `WARNING`, `INFORMATIONAL`).
4. **Persist** — full extraction + exceptions written to Postgres. Source PDF bytes persisted (deferred-load column) so the viewer can re-render the document later.
5. **Review** — Next.js UI shows PDF on the left, structured extraction on the right. Fields with usable source references can navigate to the source page; text highlights require resolved bounding boxes. Checkbox/signature geometry is deferred. Exception queue lives below.
6. **Q&A** — `POST /leases/{id}/query` runs grounded Q&A via Claude Haiku 4.5 over the structured extraction; the prompt requests field-path citations. Responses can contain an empty citation list, and citation entailment is not independently verified.
7. **MCP** — six-tool MCP server lets Claude Desktop query the corpus, trigger new extractions, and resolve exceptions interactively.

## Architecture

```
┌─────────────────┐         ┌──────────────────────────┐
│  Next.js 16     │ ──────► │  FastAPI + LangGraph     │
│  (Vercel)       │         │  (Railway)               │
│                 │         │                          │
│  • Upload (URL  │         │  • Ingest                │
│    or file)     │         │  • Template detect       │
│  • PDF viewer   │         │  • Extract (9 sections   │
│  • Click-to-    │         │    in parallel)          │
│    highlight    │         │  • Validate              │
│  • Review queue │         │  • Persist               │
└─────────────────┘         └──────────┬───────────────┘
                                       │
                                       ▼
                            ┌──────────────────────────┐
                            │  Neon Postgres           │
                            │  (leases, exceptions,    │
                            │   pdf_bytes deferred)    │
                            └──────────┬───────────────┘
                                       │
                            ┌──────────▼───────────────┐
                            │  MCP Server              │
                            │  (Python mcp SDK)        │
                            │                          │
                            │  • list_leases           │
                            │  • get_lease             │
                            │  • extract_lease         │
                            │  • query_lease           │
                            │  • list_exceptions       │
                            │  • resolve_exception     │
                            └──────────────────────────┘
                                       ▲
                                       │ stdio
                            ┌──────────┴───────────────┐
                            │  Claude Desktop          │
                            └──────────────────────────┘
```

## Stack

- **Frontend:** Next.js 16 (App Router) + TypeScript + Tailwind 4 on Vercel. `react-pdf` for canvas rendering; click-to-highlight uses backend-emitted `bboxes` rendered as absolutely-positioned overlays, not PDF.js text-layer matching.
- **Backend:** Python 3.12 + FastAPI + LangGraph on Railway. Procfile-based deploy.
- **DB:** Neon Postgres via asyncpg (pooler-compatible: `prepared_statement_cache_size=0`).
- **LLM:** Claude Sonnet 4.6 for extraction (vision-capable), Claude Haiku 4.5 for template detection + grounded Q&A.
- **PDF:** `pypdf` for text extraction, `pypdfium2` for page-image rendering (no system deps — embedded native lib), `ocrmypdf` (+ Tesseract) for scanned PDFs. Page images attached to every extraction call so Claude grounds visual fields (checkboxes, signatures) in pixels rather than OCR output. `pdfplumber` + `rapidfuzz` for the OCR-anchored bbox derivation — Sonnet returns the snippet text per field, the backend aligns that snippet against pdfplumber's word-level positions in the OCR'd PDF and emits one bbox per line (PDF QuadPoints model). LLM never emits coordinates; geometry is owned by the OCR layer.
- **MCP:** official Python `mcp` SDK.
- **Ops:** GitHub Actions cron pings `/leases` to keep Railway warm — `/leases` rather than `/health` because it touches the Neon connection pool, so it also surfaces idle-disconnects a static health check would miss. Scheduled `*/5`, but GitHub deprioritizes short-interval crons, so it effectively fires ~30 min apart (1-2 h overnight); it is best-effort warming, not uptime monitoring. CORS open by default (`CORS_ORIGINS` env var to lock down).

Mirrors the Sift stack intentionally — same two-service shape, same hosting, same DB pattern.

## Configuration

Required:
- `ANTHROPIC_API_KEY` — Sonnet 4.6 for extraction, Haiku 4.5 for template detection and Q&A.

Optional:
- `DATABASE_URL` — defaults to `sqlite+aiosqlite:///tenancy.db` for local dev. For Neon: `postgresql+asyncpg://user:pass@host/dbname?ssl=require` (asyncpg uses `ssl=`, not libpq's `sslmode=`).
- `EXTRACT_MODEL`, `QA_MODEL`, `TEMPLATE_MODEL` — model overrides.
- `EXTRACT_MAX_TOKENS`, `QA_MAX_TOKENS` — token caps per call. `QA_MAX_TOKENS` defaults to 4096 so long answers (e.g. "list all flagged exceptions") fit the JSON envelope without truncating mid-string.
- `PDF_FETCH_TIMEOUT` — seconds (default 30).
- `MIN_TEXT_LEN_PER_PAGE` — pages below this much extracted text are flagged for vision fallback (default 50 chars).
- `PAGE_RENDER_DPI` — DPI for the page-image render attached to each extraction call (default 150). Higher gets diminishing returns on legibility and grows image-token cost ~quadratically.
- `MAX_UPLOAD_SIZE` — bytes (default 20 MiB).
- `CORS_ORIGINS` — comma-separated allowed origins (default `*`).

```bash
python3.12 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
export ANTHROPIC_API_KEY=sk-ant-...
uvicorn app:app --reload
```

## The schema

Residential leases anchor on the Texas Apartment Association (TAA) template. The schema includes fields intended for TAA, NAA, California, and Florida variants; coverage and accuracy across those templates have not been benchmarked. See `schemas.py` for the full Pydantic models.

High-level groups:
- **Parties** — tenants, co-signers, landlord entity, property manager
- **Property** — address, unit, parking, sq ft
- **Term** — start, end, rollover, lease type
- **Rent** — base, prorations, due date, late fees, NSF
- **Deposits** — security, pet, other
- **Utilities** — responsibility breakdown
- **Pets, parking, addenda** — structured booleans + details
- **Special clauses** — early termination, military, renewal, sublet
- **Compliance disclosures** — lead paint, mold, bed bug, etc.

Each field is wrapped in `ExtractedField[T]` which carries `value`, `confidence`, `source: SourceSpan | None`, and `notes`. `SourceSpan` is `{page_number, char_start, char_end, snippet, match_type, section_label, bboxes}`. `match_type` discriminates how the field was located (`filled` / `blank` / `inferred` / `checkbox` / `absent`); `bboxes` is a list of `BoundingBox` (normalized 0-1 coords, top-left origin) — one per line, matching the PDF `/Highlight` QuadPoints model. Coordinates are derived server-side from the OCR'd PDF's word positions via `pdfplumber` + `rapidfuzz`, not emitted by the LLM.

## MCP surface

The same agent, accessible from Claude Desktop. Six tools over one backend — all six wrap the same Railway-deployed FastAPI backend that powers the [SaaS UI](https://github.com/kristenmartino/tenancy). No auth boundary today — multi-tenant accounts and auth are a deferred productization gate (see [STATUS.md](STATUS.md)).

📹 **30s bonus demo** (script ready, recording forthcoming): [`docs/demo-mcp.md`](docs/demo-mcp.md). Shows a multi-tool resolve flow: *"Pull up lease 45314996 — what's flagged?"* → `list_exceptions` + `get_lease` → *"Edit the term start date to 2018-01-01."* → `resolve_exception`.

_(A screenshot of the six tools loaded in Claude Desktop will land at `docs/mcp-tools.png` alongside the recording.)_

| Tool | Purpose |
|---|---|
| `list_leases(status=None, limit=50)` | Return processed leases with status and summary |
| `get_lease(lease_id)` | Full structured extraction with available per-field source references |
| `extract_lease(pdf_url)` | Trigger extraction on a new document — runs async; poll `get_lease` for results |
| `query_lease(lease_id, question)` | Natural-language Q&A over the extraction, with model-supplied citations when available |
| `list_exceptions(lease_id=None, severity=None, resolved=False)` | Pending human-review items |
| `resolve_exception(exception_id, action, correction=None)` | Approve / edit / reject — see semantics below |

Two example prompts:

- *"Show me all leases expiring in the next 12 months and flag any with early termination clauses."* → Claude calls `list_leases` + `get_lease` per match, reasons over structured fields with source citations.
- *"Pull up lease 45314996 — what's flagged?"* → `list_exceptions` for the review queue + `get_lease` for context. Follow-up: *"Edit the term start date to 2018-01-01."* → `resolve_exception(action="edit", correction={value: "2018-01-01"})`. That's the [bonus demo](docs/demo-mcp.md).

### Resolve semantics

`POST /exceptions/{id}/resolve` accepts `{action, correction?}` where `action ∈ {approve, edit, reject}` and `correction` is `{value, note?}` (required when `action == "edit"`).

- **edit** — `lease.extraction` is rewritten at the exception's `field_path`: the leaf `ExtractedField.value` is replaced with `correction.value` and `confidence` is set to `1.0` by convention for a human correction, not as a measured probability. The blocker clears; readiness still depends on all other blocking exceptions.
- **approve** — accept the current state as-is (possibly blank). The exception clears; the blocker clears. *"I accept the gap."*
- **reject** — exception row closes for audit, but the blocking flag stays material. The lease does **not** become ready. *"I won't bless this field."*

Lease responses include `ready_to_proceed: bool`, derived as `status == "complete"` AND no blocking exception is unresolved-or-rejected.

### Wiring it into Claude Desktop

Edit `~/Library/Application Support/Claude/claude_desktop_config.json`:

```json
{
  "mcpServers": {
    "tenancy": {
      "command": "/Users/you/tenancy-api/.venv/bin/python",
      "args": ["/Users/you/tenancy-api/mcp_server.py"],
      "env": {
        "TENANCY_API_BASE": "https://tenancy-api-production.up.railway.app"
      }
    }
  }
}
```

Restart Claude Desktop. The six tools above show up under the MCP server icon.

## Possible extensions

Commercial leases and other document types would need separate schemas, labeled evaluations, domain review, and integrations. These are unvalidated ideas. This prototype does not establish regulatory compliance, suitability for regulated workflows, market size, or customer demand.

## What's real vs scaffolded

**Real:**
- End-to-end extraction pipeline on real residential lease templates, deployed and reachable
- Optional source spans and OCR-anchored text highlights when matching succeeds
- Interactive exception resolve UI — approve / edit / reject wired to the resolve endpoint; edit writes the corrected value back into the extraction
- Seven-rule validation + recursive low-confidence sweep generating `LeaseException` rows
- MCP server with six working tools, tested against Claude Desktop
- Review queue listing exceptions with severity gating
- Direct PDF upload (`POST /leases/upload`) alongside URL ingest
- Q&A endpoint prompted to cite extraction field paths; no independent citation verifier
- Postgres persistence (Neon) with PDF blob storage (deferred-loaded so list queries stay fast)
- Background-task error handling marks caught pipeline failures; process termination can still interrupt in-process work
- GitHub Actions keep-warm cron

**Scaffolded (v2 candidates):**
- No multi-tenant accounts — demo workspace only
- Human corrections stored but not yet fed back into extraction prompts (next: closed-loop feedback so the agent observes outcomes and self-improves)
- Single document type at launch (residential); commercial and student housing on the v2 roadmap
- Checkbox / signature highlight geometry (OCR-anchored bboxes cover text fields; visual-mark geometry is deferred to an opt-in Textract path, not yet merged or verified)
- No re-extraction diff view — list + highlight + buttons
- No state-aware legal validation: the deposit rule uses a fixed 2× rent heuristic, not current jurisdiction-specific law
- No proper accuracy evals — confidence numbers are self-reported by the model, not measured against a ground-truth corpus. Real evals require accumulating labeled examples from the review queue (closed-loop feedback above).

## Delivery notes

The original build plan was an estimate, not an execution log or evidence of a completed demo recording. Current implementation limits are listed above; recording scripts are linked in the MCP section.

## Repos

- Frontend: `kristenmartino/tenancy`
- Backend + MCP: `kristenmartino/tenancy-api`

---

*Built by Kristen Martino. Part of an applied AI portfolio: [kristenmartino.ai](https://kristenmartino.ai).*
