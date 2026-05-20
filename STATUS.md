# Status

> **Active focus:** v2 highlight (bbox overlay) lands + demo recording. Backend is feature-complete for V0 (ingest → OCR → page-image-grounded extract → validate → persist), with bbox emission shipped ([tenancy-api#16](https://github.com/kristenmartino/tenancy-api/issues/16), PR [#19](https://github.com/kristenmartino/tenancy-api/pull/19)) and the image-dim cap in PR [#25](https://github.com/kristenmartino/tenancy-api/pull/25) unblocking oversized scans. Frontend interactivity is feature-complete for V0 — exception resolve UI ([tenancy#1](https://github.com/kristenmartino/tenancy/issues/1)) and Q&A panel ([tenancy#2](https://github.com/kristenmartino/tenancy/issues/2)) both shipped; resolve endpoint actions are actually distinct now ([tenancy-api#22](https://github.com/kristenmartino/tenancy-api/issues/22)). Frontend overlay renderer is in review at [tenancy#18](https://github.com/kristenmartino/tenancy/pull/18). Remaining V0 work: end-to-end verify on a freshly uploaded lease once #18 merges, then record the 60-90s demo.

> **Open question:** none in flight. Highlight direction decided (strict v1 shipped, bbox overlay v2 in progress).

## Next 3

1. **[tenancy#14]** Bbox overlay frontend — in review at [tenancy#18](https://github.com/kristenmartino/tenancy/pull/18). Renders overlay rectangles from `source.bbox`; replaces text-layer matching entirely. (`effort-week`)
2. **[#2]** Record 60-90s demo video for the case study (`effort-day`) — meaningfully better once tenancy#14 lands: overlay works on any PDF, not just text-native fillables.
3. **[tenancy#3]** Re-verify click-to-highlight end-to-end on a freshly uploaded lease (post-image-dim-cap, post-bbox-overlay). (`effort-day`)

## Later

Captured as `later`-labeled issues so they show up in `gh issue list` and on the Project board.

**Verticals** (promote when CRE lands or a real prospect surfaces):
- **[#5]** Government / public housing (HUD forms, FedRAMP)
- **[#6]** Healthcare REITs (OIG flags, HIPAA-aligned audit trail)
- **[#7]** Litigation / e-discovery (privilege detection, Bates, Relativity)
- **[#8]** EU operators (data residency, GDPR DSR support)

**Highlight precision roadmap** (the actual production architecture):
- v2: extract `source.bbox` from Claude vision + render overlay rectangles in the frontend. Backend ([tenancy-api#16](https://github.com/kristenmartino/tenancy-api/issues/16)) shipped in PR [#19](https://github.com/kristenmartino/tenancy-api/pull/19). Frontend ([tenancy#14](https://github.com/kristenmartino/tenancy/issues/14)) in review at [tenancy#18](https://github.com/kristenmartino/tenancy/pull/18) — **promoted to Next 3**.
- **[tenancy-api#17]** — v3: AWS Textract for production-grade bbox accuracy (~99%). Promote when vision-bbox approach is the bottleneck or an AWS-friendly prospect surfaces.

Other Later candidates (not yet issues — would be premature):
- Multi-tenant accounts + auth (gates everything productization-related)
- Closed-loop feedback so human corrections from the review queue feed back into extraction prompts
- Real eval set + measured accuracy (depends on the feedback loop above)
- Re-extraction diff view in the UI

## Blocked on

Nothing in flight.

## Recent decisions

- **Bbox prompt rewrite for filled / checkbox / blank-placeholder fields** — first end-to-end test of bbox overlays on a real lease (San Antonio TAA template, [tenancy#18](https://github.com/kristenmartino/tenancy/pull/18) preview) surfaced two failure modes: (1) for blank template fields ("3. Lease Term. The initial term... day of (month), ___(year)" with nothing filled in), Sonnet bbox'd the whole section header instead of the blank, putting overlays a half-inch to inch too high; (2) for checkbox/enum fields (`utilities.electric`, `compliance.lead_paint_disclosure`), Sonnet bbox'd the label text only, dropping the empty box glyph itself. New `EXTRACTION_PROMPT` rules, in order of precedence: filled value → tight to value glyphs; checkbox/enum → box glyph + adjacent label as one rect (whether checked or empty); null with visible blank placeholder → bbox the placeholder + its label, not the section header; null with no visible placeholder → bbox null. This is a behavioral change for the v2 highlight UX — empty form fields are now actionable in the review queue (click "Lease Term" → see the literal blank line where the date would go), not silently un-highlighted. Filled-field positional error (~3-4% of page height, ~half-inch on screen) remains as the v2 ceiling; v3 ([#17](https://github.com/kristenmartino/tenancy-api/issues/17), Textract) is the path to pixel accuracy.
- **Resolve endpoint actions are now distinct** ([tenancy-api#22](https://github.com/kristenmartino/tenancy-api/issues/22)) — pre-change, all three of `approve | edit | reject` were pure metadata on the exception row; nothing read `BLOCKING` downstream and `edit` never rewrote `lease.extraction`. Now: `edit` walks the extraction JSON to `exc.field_path` and replaces the leaf `.value` (confidence bumped to 1.0); `approve` clears the blocker without touching the extraction; `reject` closes the row but keeps the blocking flag material via a derived `ready_to_proceed: bool` returned on every lease (`status == "complete"` AND no blocking exception unresolved-or-rejected). Typed `Correction(value, note?)` model so the frontend's `{"value": "<text>"}` payload is validated instead of stored as opaque dict. Deferred: re-running `validate_extraction` after an edit (e.g. user fixes `end_date` to something that still violates the date-order rule) — would need to surface new exceptions in the resolve response, bigger UX shape, revisit if it bites.
- **Exception resolve UI + Q&A panel shipped** ([tenancy#1](https://github.com/kristenmartino/tenancy/issues/1), [tenancy#2](https://github.com/kristenmartino/tenancy/issues/2)) — both former Next 3 items closed in parallel sessions. Frontend interactivity is now feature-complete for V0. The resolve UI consumes the refined approve/edit/reject semantics from tenancy-api#22 above; Q&A panel posts to `/leases/{id}/query` (the 502-on-long-answers bug fixed in PR [#20](https://github.com/kristenmartino/tenancy-api/pull/20) was the prerequisite).
- **Page render cap at 1950px** ([PR #25](https://github.com/kristenmartino/tenancy-api/pull/25)) — Anthropic's many-image API limits each image to 2000px on the longest side. We send 9 section calls × N pages, so every page render is a many-image request. Letter at 150 DPI is fine (1275×1650); legal / A3 / rotated-and-OCR'd scans 400 the whole extraction with `image dimensions exceed max allowed size`. `_render_pages_to_pngs` now drops scale per page so `max(w, h) * scale ≤ 1950`. `max()` is rotation-invariant. Surfaced when a fresh upload of the rotated 2018 sample lease 400'd against PR [tenancy#18](https://github.com/kristenmartino/tenancy/pull/18)'s bbox-overlay verification flow.
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

Settling into v2 polish. Backend and frontend interactivity both feature-complete for V0. Bbox overlay frontend is in review at [tenancy#18](https://github.com/kristenmartino/tenancy/pull/18); after that lands, end-to-end re-verify and demo recording are the last V0 items.

## Audience class

Portfolio / case study now. Productizing optional second phase if the demo lands meetings.

## Repos

- Backend + MCP: this repo (`kristenmartino/tenancy-api`)
- Frontend: [`kristenmartino/tenancy`](https://github.com/kristenmartino/tenancy)

Both deploy independently (Railway for backend, Vercel for frontend). STATUS.md and CLAUDE.md are mirrored to the frontend repo. Shared user-level Project board: https://github.com/users/kristenmartino/projects/2 — spans both repos so cross-repo deps (e.g. tenancy#14 ↔ tenancy-api#16 for v2 highlights) are visible in one view.
