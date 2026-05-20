# Status

> **Active focus:** verifying Path A (`ocrmypdf` preprocessing) actually works on Railway end-to-end. Pre-OCR, click-to-highlight in the PDF viewer worked for text-native PDFs but fell silently flat on scanned ones because PDF.js had no text-layer spans to tint. Just shipped `nixpacks.toml` declaring `tesseract` / `ghostscript` / `qpdf` system deps; need to confirm Railway's Nixpacks actually installs apt packages, then re-test that ingest → OCR → re-extract → click-to-highlight cleanly rounds out for a scanned PDF.

> **Open question:** does Nixpacks `aptPkgs` work on Railway, or do we need to switch to a Dockerfile? Fallback exists (~10-line Dockerfile), unsure which will win.

## Next 3

1. **[#1]** Verify OCR deploy on Railway + re-test click-to-highlight on a scanned PDF (`effort-day`)
2. **[#2]** Record 60-90s demo video for the case study on `kristenmartino.ai` (`effort-day`)
3. **[#3]** Sketch commercial real-estate (CRE) lease schema — the next vertical from the README's market-expansion table (`effort-weeks`)

## Later

Captured as `later`-labeled issues so they show up in `gh issue list` and on the Project board — won't get forgotten when the Next 3 churns. Promote to `next` when CRE (#3) lands or when a real prospect surfaces for one.

- **[#5]** Vertical: government / public housing (HUD forms, FedRAMP)
- **[#6]** Vertical: healthcare REITs (OIG flags, HIPAA-aligned audit trail)
- **[#7]** Vertical: litigation / e-discovery (privilege detection, Bates, Relativity)
- **[#8]** Vertical: EU operators (data residency, GDPR DSR support)

Other Later candidates (not yet issues — would be premature):
- Multi-tenant accounts + auth (gates everything productization-related)
- Closed-loop feedback so human corrections from the review queue feed back into extraction prompts
- Real eval set + measured accuracy (depends on the feedback loop above)
- Re-extraction diff view in the UI

## Blocked on

- Railway deploy of commit `f4d65fd` going green with apt packages installed. Watching deploy logs.

## Recent decisions

- **Path A over B / C** for the highlight-on-scans problem — `ocrmypdf` preprocessing chosen because it adds a hidden searchable text layer that the existing PDF.js highlight code uses unchanged. Trade-off: 3-5 min added to first Railway build, ~20-40s added to ingest of scanned PDFs. Path B (Textract for >99% bbox accuracy + per-page cost) and Path C (Claude vision bboxes for zero infra + ~70% accuracy) both punted.
- **Repositioned README** around residential PM as wedge; market-expansion table sketches CRE, government, healthcare REITs, e-discovery, EU operators with what would change for each. Architecture framed as the portable thing.
- **`pdf_bytes` column deferred** by default so list queries don't drag every PDF blob — was tripping Vercel cold-start function timeouts.
- **Vision path = native Claude PDF document blocks**, not `pdf2image` + `poppler`. Lets us skip a system-deps headache for vision (though OCR brought it back for Path A).
- **`BackgroundTasks` safety net** in `_run_pipeline` so a pipeline crash marks the lease `pipeline_failed` instead of stranding at `pending`.
- **GitHub Actions keep-warm cron** every 5 min to mitigate Railway cold starts that were tripping Vercel function timeouts.

## Velocity

High right now (~30 commits in the last 48h shipping V0). Expected to ramp down once the OCR deploy verification lands and the demo video is recorded.

## Audience class

Portfolio / case study now. Productizing optional second phase if the demo lands meetings.

## Repos

- Backend + MCP: this repo (`kristenmartino/tenancy-api`)
- Frontend: [`kristenmartino/tenancy`](https://github.com/kristenmartino/tenancy)

Both deploy independently (Railway for backend, Vercel for frontend). STATUS.md and CLAUDE.md are mirrored to the frontend repo — velocity is high enough on both that orientation needs to work in either.
