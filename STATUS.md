# Status

> **Active focus:** strategic retreat on highlight precision + pivot to demo polish. Backend pipeline is solid (ingest → OCR → extract → validate → persist all working, deployed). Frontend extraction display + side-by-side PDF viewer working. The text-matching click-to-highlight was iterated 12+ times; shipped strict-match-only v1 so failures are silent and honest. Real fix is coordinate-based overlays driven by extraction-time bboxes — tracked as v2 (tenancy-api#16, tenancy#14) and v3 (tenancy-api#17). Now pivoting to ship the rest of Next 3: exception resolve UI + Q&A panel + demo video.

> **Open question:** none in flight. Highlight direction decided (strict v1 now, bbox overlay v2 next).

## Next 3

1. **[tenancy#1]** Interactive exception resolve UI — approve/edit/reject buttons hitting `POST /exceptions/{id}/resolve` (`effort-day`). Backend semantics landed in [tenancy-api#22](https://github.com/kristenmartino/tenancy-api/issues/22): `edit` rewrites the extraction, derived `ready_to_proceed` flag, `reject` keeps the blocker material so the three actions are actually distinct.
2. **[tenancy#2]** Q&A panel using the existing `/leases/{id}/query` endpoint (`effort-day`)
3. **[#2]** Record 60-90s demo video for the case study (`effort-day`) — use text-native fillable PDF (San Antonio TAA) for the highlight demo, where the strict matcher works reliably

## Later

Captured as `later`-labeled issues so they show up in `gh issue list` and on the Project board.

**Verticals** (promote when CRE lands or a real prospect surfaces):
- **[#5]** Government / public housing (HUD forms, FedRAMP)
- **[#6]** Healthcare REITs (OIG flags, HIPAA-aligned audit trail)
- **[#7]** Litigation / e-discovery (privilege detection, Bates, Relativity)
- **[#8]** EU operators (data residency, GDPR DSR support)

**Highlight precision roadmap** (the actual production architecture):
- **[tenancy-api#16]** + **[tenancy#14]** — v2: extract `source.bbox` from Claude vision + render overlay rectangles in the frontend. Removes text matching entirely. ~80% accurate. (`next` once strict matcher demo'd)
- **[tenancy-api#17]** — v3: AWS Textract for production-grade bbox accuracy (~99%). Promote when vision-bbox approach is the bottleneck or an AWS-friendly prospect surfaces.

**[#3]** CRE schema sketch — first vertical expansion (`later` for now, was Next 3 — demoted while we get the residential demo cleanly shipped).

Other Later candidates (not yet issues — would be premature):
- Multi-tenant accounts + auth (gates everything productization-related)
- Closed-loop feedback so human corrections from the review queue feed back into extraction prompts
- Real eval set + measured accuracy (depends on the feedback loop above)
- Re-extraction diff view in the UI

## Blocked on

Nothing in flight.

## Recent decisions

- **Resolve endpoint actions are now distinct** ([tenancy-api#22](https://github.com/kristenmartino/tenancy-api/issues/22)) — pre-change, all three of `approve | edit | reject` were pure metadata on the exception row; nothing read `BLOCKING` downstream and `edit` never rewrote `lease.extraction`. Now: `edit` walks the extraction JSON to `exc.field_path` and replaces the leaf `.value` (confidence bumped to 1.0); `approve` clears the blocker without touching the extraction; `reject` closes the row but keeps the blocking flag material via a derived `ready_to_proceed: bool` returned on every lease (`status == "complete"` AND no blocking exception unresolved-or-rejected). Typed `Correction(value, note?)` model so the frontend's `{"value": "<text>"}` payload is validated instead of stored as opaque dict. Deferred: re-running `validate_extraction` after an edit (e.g. user fixes `end_date` to something that still violates the date-order rule) — would need to surface new exceptions in the resolve response, bigger UX shape, revisit if it bites.
- **Page images attached to every extraction call** — fixed the false-positive checkbox class of bug (Tesseract OCR reads scan noise as a stray mark → LLM trusts the OCR'd text and reports the box as checked). Render every page to a PNG at 150 DPI via `pypdfium2` (no system deps) and attach as image blocks alongside the OCR'd text on each of the 9 section calls. Prompt updated to tell the model: image is ground truth for visual fields (checkboxes, signatures, hand-fill), OCR is ground truth for dense text. Replaces the prior `document`-block fallback that only triggered when text extraction was incomplete on a page — the bug shape was OCR'ing successfully but mis-reading the marks, so the gate never fired. Image-token cost goes up vs the document-block path (PNG per page per section call), considered acceptable for the precision win — caching the image prefix across section calls is the obvious next optimization if cost shows up in invoices.
- **Q&A `max_tokens` bumped 1024 → 4096** — Haiku was truncating mid-JSON on long answers (e.g. "list all flagged exceptions"), producing 502s on `/leases/{id}/query`. Cheapest fix; Haiku 4.5 input is far larger than the extraction so the cost delta is negligible. Proper structural fix (Anthropic tool-use so the JSON envelope is guaranteed valid) deferred — only worth doing if we see the cap hit again or want to drop the manual `_strip_fences` parse.
- **Strict highlight matcher v1** ([tenancy#13](https://github.com/kristenmartino/tenancy/pull/13)) — after 12+ heuristic iterations of fuzzy text matching, retreated to exact-normalized-match only. No fuzzy fallback. Silent failures preferred over wrong-place highlights. **The real fix is bbox overlays driven by extraction-time coordinates** (industry standard: Textract, Klippa, Rossum, Hyperscience all do this). Tracked as v2/v3 work above.
- **Path A over B / C for OCR** — `ocrmypdf` preprocessing chosen because it adds a hidden searchable text layer that PDF.js can use. Verified working end-to-end on Railway.
- **Cache-bust PDF URL + key re-mount on `updated_at` change** — fixed the "PDF stays 404 forever after pipeline completes" bug where `react-pdf` cached the initial 404.
- **`pool_pre_ping=True` + DB-touching keep-warm cron** — fixed Neon idle-disconnect causing intermittent 500s on the homepage.
- **Dockerfile over Nixpacks `aptPkgs`** — `nixpacks.toml` silently didn't install system deps on Railway; Dockerfile is explicit and works.
- **Repositioned README** around residential PM as wedge; market-expansion table sketches CRE, government, healthcare REITs, e-discovery, EU operators with what would change for each.
- **`pdf_bytes` column deferred** by default so list queries don't drag every PDF blob.
- **`BackgroundTasks` safety net** in `_run_pipeline` so a pipeline crash marks the lease `pipeline_failed` instead of stranding at `pending`.

## Velocity

Still high but settling. Backend is feature-complete for V0; frontend has two visible interactivity items left (exception resolve, Q&A panel) plus demo recording.

## Audience class

Portfolio / case study now. Productizing optional second phase if the demo lands meetings.

## Repos

- Backend + MCP: this repo (`kristenmartino/tenancy-api`)
- Frontend: [`kristenmartino/tenancy`](https://github.com/kristenmartino/tenancy)

Both deploy independently (Railway for backend, Vercel for frontend). STATUS.md and CLAUDE.md are mirrored to the frontend repo. Shared user-level Project board: https://github.com/users/kristenmartino/projects/2 — spans both repos so cross-repo deps (e.g. tenancy#14 ↔ tenancy-api#16 for v2 highlights) are visible in one view.
