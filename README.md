# Tenancy

**Citation-backed lease abstraction for multifamily operators.** Ingest a portfolio of residential leases. Get structured fields with per-field source citations and an exception queue gated for human review. Every claim is auditable back to the page and character span in the original PDF.

Built as a portfolio piece. Demos against the workflow that RealPage, AppFolio, and every multifamily operator runs at portfolio scale: turning lease PDFs into structured data with humans on the edges where extraction can't be trusted blindly.

Live at `tenancy.kristenmartino.ai`. API at https://tenancy-api-production.up.railway.app (Railway) backed by Neon Postgres.

```bash
# Try it
curl -X POST https://tenancy-api-production.up.railway.app/leases \
  -H 'Content-Type: application/json' \
  -d '{"pdf_url":"https://www.sanantonio.gov/Portals/0/Files/NHSD/Programs/FairHousing/LeaseAgreement.pdf"}'
# {"lease_id":"...", "status":"pending"}  → poll GET /leases/{id} until status=complete
```

---

## The wedge

Multifamily operators acquire portfolios of 1K-50K leases on tight close timelines. Manual abstraction is the bottleneck. Incumbent SaaS (RealPage, Yardi) handles bulk processing but the outputs are opaque — when extraction is wrong, there's no breadcrumb back to the source. Disputes default to "trust the system" or "re-abstract by hand."

This system inverts that:

- **Every extracted field carries a source citation** (page + character span + verbatim snippet)
- **Confidence is per-field** and the model is prompted to honestly report `confidence: 1.0, value: null` when something isn't in the document
- **Validation rules** (date math, rent positivity, deposit caps by state, required-field presence) flag inconsistencies into an exception queue
- **Nothing reaches the system of record** until a human approves, edits, or rejects each flagged item
- **Click any extracted field in the UI** → the PDF jumps to the source page and highlights the snippet

The architecture — agent + cited extraction + rule-based validation + gated exception queue + audit trail — is the portable thing. Residential PMs are the demo. Adjacent verticals below.

## Demo target: residential PM (multifamily / SFR)

Why this market first:

| | |
|---|---|
| **Schema fit** | TAA (Texas Apartment Association) is the anchor template, generalized to NAA, CA, FL variants. Schema in `schemas.py`. |
| **State compliance** | TX has no statutory deposit cap; CA caps at 2x; NY rent stabilization; FL deposit-return timelines. Validation rules can be state-aware. |
| **Buyer pain** | Portfolio acquisitions = abstract 5-50K leases on a tight close. Existing SaaS is opaque; manual review is the rate-limiter. |
| **Integration target** | Yardi, RealPage, AppFolio, Entrata. PMS-of-record is the sync target. |
| **Visible audit trail** | Disputes (security deposit returns, late fees) end up in court more often than people think. "Show me where" matters. |

## What it does

1. **Ingest** — accepts a residential lease PDF (TAA, NAA, or state template) via URL or direct upload. Pulls text with pypdf. Flags low-text pages for vision fallback.
2. **Extract** — LangGraph agent calls Claude Sonnet 4.6 across nine sections in parallel (parties, property, term, rent, deposits, utilities, pets, special clauses, compliance). Each call attaches every page rendered as a PNG (via `pypdfium2`, ~150 DPI) alongside the OCR'd text — the model grounds visual fields (checkboxes, signatures, X marks, hand-fill) in pixels and uses OCR for character-exact dense text (parties, addresses, dollar amounts). This avoids the OCR-noise → false-positive-checkbox class of bug, where Tesseract reads a scan artifact as a stray mark. Every field carries `value`, `confidence`, and a source span. Claude Haiku 4.5 runs in parallel to classify the lease against the template enum.
3. **Validate** — seven deterministic rules (date consistency, rent math, deposit-cap heuristic, late-fee/grace-period pairing, lead-paint disclosure presence, required-but-null check, and a recursive low-confidence sweep) emit `LeaseException` rows with severity (`BLOCKING`, `WARNING`, `INFORMATIONAL`).
4. **Persist** — full extraction + exceptions written to Postgres. Source PDF bytes persisted (deferred-load column) so the viewer can re-render the document later.
5. **Review** — Next.js UI shows PDF on the left, structured extraction on the right. Click any extracted field → PDF jumps to that page and tints the source snippet. Exception queue lives below.
6. **Q&A** — `POST /leases/{id}/query` runs grounded Q&A via Claude Haiku 4.5 over the structured extraction; every answer carries field-path citations.
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
- **Ops:** GitHub Actions cron pings `/health` every 5 min to keep Railway warm; CORS open by default (`CORS_ORIGINS` env var to lock down).

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

Residential leases anchor on the Texas Apartment Association (TAA) template. The extraction schema generalizes across TAA, NAA, California, and Florida variants. See `schemas.py` for the full Pydantic models.

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

The same agent, accessible from Claude Desktop. Six tools, one auth boundary, all six wrap the same Railway-deployed FastAPI backend that powers the [SaaS UI](https://github.com/kristenmartino/tenancy).

📹 **30s bonus demo** (script ready, recording forthcoming): [`docs/demo-mcp.md`](docs/demo-mcp.md). Shows a multi-tool resolve flow: *"Pull up lease 45314996 — what's flagged?"* → `list_exceptions` + `get_lease` → *"Edit the term start date to 2018-01-01."* → `resolve_exception`.

_(A screenshot of the six tools loaded in Claude Desktop will land at `docs/mcp-tools.png` alongside the recording.)_

| Tool | Purpose |
|---|---|
| `list_leases(status=None, limit=50)` | Return processed leases with status and summary |
| `get_lease(lease_id)` | Full structured extraction with per-field source citations |
| `extract_lease(pdf_url)` | Trigger extraction on a new document — runs async; poll `get_lease` for results |
| `query_lease(lease_id, question)` | Natural-language Q&A grounded on the structured extraction, with citations |
| `list_exceptions(lease_id=None, severity=None, resolved=False)` | Pending human-review items |
| `resolve_exception(exception_id, action, correction=None)` | Approve / edit / reject — see semantics below |

Two example prompts:

- *"Show me all leases expiring in the next 12 months and flag any with early termination clauses."* → Claude calls `list_leases` + `get_lease` per match, reasons over structured fields with source citations.
- *"Pull up lease 45314996 — what's flagged?"* → `list_exceptions` for the review queue + `get_lease` for context. Follow-up: *"Edit the term start date to 2018-01-01."* → `resolve_exception(action="edit", correction={value: "2018-01-01"})`. That's the [bonus demo](docs/demo-mcp.md).

### Resolve semantics

`POST /exceptions/{id}/resolve` accepts `{action, correction?}` where `action ∈ {approve, edit, reject}` and `correction` is `{value, note?}` (required when `action == "edit"`).

- **edit** — `lease.extraction` is rewritten at the exception's `field_path`: the leaf `ExtractedField.value` is replaced with `correction.value` and `confidence` is bumped to `1.0` (a human said so). The blocker clears and the lease becomes ready.
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

## Market expansion

The architecture — agent + cited extraction + validation rules + exception queue + audit trail — is reusable. Each adjacent vertical needs its own schema, integrations, and (sometimes) hosting story. Sketched honestly:

| Vertical | What changes | Why the architecture fits |
|---|---|---|
| **Corporate real estate** (F500 tenants leasing offices, warehouses, datacenters) | Commercial lease schema (TI allowances, escalation, renewal options, percentage rent, exclusive-use). Integration with LeaseAccelerator / Visual Lease / CoStar. ASC 842 / IFRS 16 compliance flags. | Most regulatory exposure of any segment; citations matter more, not less. Largest TAM after multifamily. Obvious v2. |
| **Government / public housing** | HUD lease forms (50059, voucher programs). FedRAMP/FISMA. Self-hosted deploy via `ocrmypdf` instead of vision API. | Hard procurement requirements rule out most SaaS. Self-hosting + citation trail fits the compliance shape. Long sales cycle (9-18 mo). |
| **Healthcare REITs** | Ground leases + medical office buildings. OIG anti-kickback flags. HIPAA-aligned audit trail. | Lease terms intersect with regulated provider conduct; "show me where" matters for OIG audits. |
| **Litigation / e-discovery** | Privilege detection, Bates numbering, Relativity integration. | Same architecture, different domain rules. The exception queue becomes the privilege review queue. |
| **EU operators** (any of the above, post-GDPR) | Data residency, DSR support. Schema unchanged. | Hosting model becomes a feature (EU region pinning, on-prem option). |

Residential is the wedge because the schema, validation rules, and demo are concrete and the buyer pain is universal at scale. CRE is the natural second vertical — most regulatory exposure and largest TAM after multifamily. The others each require real product work on top of the shared architecture, but the bones are right.

## What's real vs scaffolded

**Real:**
- End-to-end extraction pipeline on real residential lease templates, deployed and reachable
- Source-span citation per extracted field; click-to-highlight on the PDF in the UI (OCR-anchored bbox overlays)
- Interactive exception resolve UI — approve / edit / reject wired to the resolve endpoint; edit writes the corrected value back into the extraction
- Seven-rule validation + recursive low-confidence sweep generating `LeaseException` rows
- MCP server with six working tools, tested against Claude Desktop
- Review queue listing exceptions with severity gating
- Direct PDF upload (`POST /leases/upload`) alongside URL ingest
- Grounded Q&A endpoint with field-path citations
- Postgres persistence (Neon) with PDF blob storage (deferred-loaded so list queries stay fast)
- Background-task safety net so no lease ever strands at `pending` on a pipeline crash
- GitHub Actions keep-warm cron

**Scaffolded (v2 candidates):**
- No multi-tenant accounts — demo workspace only
- Human corrections stored but not yet fed back into extraction prompts (next: closed-loop feedback so the agent observes outcomes and self-improves)
- Single document type at launch (residential); commercial and student housing on the v2 roadmap
- Checkbox / signature highlight geometry (OCR-anchored bboxes cover text fields; visual-mark geometry is deferred to an opt-in Textract path, not yet merged or verified)
- No re-extraction diff view — list + highlight + buttons
- No state-aware validation rules yet (CA 2x deposit cap is a code constant, not a per-state lookup table)
- No proper accuracy evals — confidence numbers are self-reported by the model, not measured against a ground-truth corpus. Real evals require accumulating labeled examples from the review queue (closed-loop feedback above).

## 48-hour build plan

**Day 1 — backend + agent (16h)**
- 0-2: Repos, Vercel + Railway deploys, Neon DB, env wiring
- 2-4: Pull 8-10 sample lease templates (TAA + state variants)
- 4-7: PDF ingestion — text extraction, vision fallback via native Claude PDF blocks
- 7-12: LangGraph extraction graph — sectioned structured output, citation tracking, DB writes
- 12-14: Validation node + exception generation
- 14-16: End-to-end run, fix worst failures

**Day 2 — UI + MCP + ship (16h)**
- 16-19: Next.js upload + PDF viewer + extraction panel
- 19-22: Review queue UI with source-span click-to-highlight
- 22-26: MCP server, six tools, Claude Desktop integration tested
- 26-28: Case study page on `kristenmartino.ai`
- 28-30: Record 60-90s demo video
- 30-32: Send DM to Harish

16h buffer absorbed into UI polish, deploy hardening (CORS, deferred columns, keep-warm cron), and MCP integration.

## Repos

- Frontend: `kristenmartino/tenancy`
- Backend + MCP: `kristenmartino/tenancy-api`

---

*Built by Kristen Martino. Part of an applied AI portfolio: [kristenmartino.ai](https://kristenmartino.ai).*
