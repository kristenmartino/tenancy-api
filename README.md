# Tenancy

**Lease abstraction agent + MCP server.** Drop a residential lease PDF in. Get structured fields, source citations, and a queue of flagged exceptions for human review. Then talk to the corpus from Claude Desktop via MCP.

Built in 48 hours as a portfolio piece. Targets the workflow that RealPage and every other multifamily operator runs at scale: turning lease documents into structured data with humans in the loop on the edges.

Live at `tenancy.kristenmartino.ai`.

---

## What it does

1. **Ingest** — accepts a residential lease PDF (TAA, NAA, or state template). Pulls text. Rasterizes scanned pages for vision fallback.
2. **Extract** — LangGraph agent calls Claude Sonnet 4.6 to populate a structured schema (parties, term, rent, deposits, utilities, addenda, special clauses). Every field carries a confidence score and source citation (page + character span).
3. **Validate** — rule-based checks (date consistency, rent math, required-field presence) generate an exception queue.
4. **Review** — Next.js UI shows PDF on the left, structured extraction on the right, source-span highlighting on click. Exception queue surfaces what needs a human.
5. **MCP** — six-tool MCP server lets Claude Desktop query the corpus, trigger new extractions, and resolve exceptions interactively.

## Architecture

```
┌─────────────────┐         ┌──────────────────────────┐
│  Next.js 15     │ ──────► │  FastAPI + LangGraph     │
│  (Vercel)       │         │  (Railway)               │
│                 │         │                          │
│  • Upload       │         │  • Ingest node           │
│  • PDF viewer   │         │  • Extract node          │
│  • Review queue │         │  • Validate node         │
└─────────────────┘         │  • Persist node          │
                            └──────────┬───────────────┘
                                       │
                                       ▼
                            ┌──────────────────────────┐
                            │  Neon Postgres           │
                            │  (documents,             │
                            │   extractions,           │
                            │   exceptions, actions)   │
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

- **Frontend:** Next.js 15 + TypeScript on Vercel. PDF.js for viewer + source-span highlighting.
- **Backend:** Python 3.12 + FastAPI + LangGraph on Railway.
- **DB:** Neon Postgres.
- **LLM:** Claude Sonnet 4.6 for extraction (vision-capable), Claude Haiku 4.5 for field-level Q&A and confidence scoring.
- **PDF:** `pypdf` for text-native, Claude vision for scanned fallback.
- **MCP:** official Python `mcp` SDK.

Mirrors the Sift stack intentionally — same two-service shape, same hosting, same DB pattern. Demonstrates a reusable architecture for unstructured-doc-to-structured-data agent systems.

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

Each field includes `value`, `confidence`, `page_number`, and `char_span`.

## MCP surface

| Tool | Purpose |
|---|---|
| `list_leases()` | Return processed leases with status and summary |
| `get_lease(lease_id)` | Full structured extraction |
| `extract_lease(pdf_url)` | Trigger extraction on a new document |
| `query_lease(lease_id, question)` | Natural-language Q&A over a single lease |
| `list_exceptions(lease_id=None)` | Pending human-review items |
| `resolve_exception(exception_id, action, correction=None)` | Approve / edit / reject |

The Claude Desktop demo: *"Show me all leases expiring in the next 12 months and flag any with early termination clauses."* Claude calls `list_leases` + `get_lease` and reasons over structured fields with source citations.

## What's real vs scaffolded

**Real:**
- End-to-end extraction pipeline on real residential lease templates
- Source-span citation per extracted field
- Exception generation from rule-based validation
- MCP server with six working tools, tested in Claude Desktop
- Review queue with approve/edit/reject actions

**Scaffolded (v2 candidates):**
- No multi-tenant accounts — demo workspace only
- Human corrections stored but not yet fed back into extraction prompts (next: closed-loop feedback so the agent observes outcomes and self-improves)
- Single document type at launch (residential); commercial and student housing on the v2 roadmap
- No re-extraction diff view — list + highlight + buttons

## 48-hour build plan

**Day 1 — backend + agent (16h)**
- 0-2: Repos, Vercel + Railway deploys, Neon DB, env wiring
- 2-4: Pull 8-10 sample lease templates (TAA + 2 state variants)
- 4-7: PDF ingestion — text extraction, page rasterization, blob storage
- 7-12: LangGraph extraction graph — sectioned structured output, citation tracking, DB writes
- 12-14: Validation node + exception generation
- 14-16: End-to-end run on all samples, fix worst failures

**Day 2 — UI + MCP + ship (16h)**
- 16-19: Next.js upload + PDF viewer + extraction panel
- 19-22: Review queue UI with source-span highlighting and resolve actions
- 22-26: MCP server, six tools, Claude Desktop integration tested
- 26-28: Case study page on `kristenmartino.ai`
- 28-30: Record 60-90s demo video
- 30-32: Send DM to Harish

16h buffer absorbed into UI work and the MCP integration.

## Repos

- Frontend: `kristenmartino/tenancy`
- Backend + MCP: `kristenmartino/tenancy-api`

---

*Built by Kristen Martino. Part of an applied AI portfolio: [kristenmartino.ai](https://kristenmartino.ai).*
