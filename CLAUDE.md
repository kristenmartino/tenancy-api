# Claude (this repo)

Conventions for any Claude session working in `kristenmartino/tenancy-api`.

## Pre-session ritual

Before starting any work in this repo, run:

```bash
cat STATUS.md                                       # active focus + open question + Next 3
gh pr list --repo kristenmartino/tenancy-api        # in-flight PRs
gh issue list --repo kristenmartino/tenancy-api     # current backlog
```

If `STATUS.md` hasn't been touched in ≥2 weeks, treat the "Active focus" line as stale — confirm current priority with the user before picking up work.

## End-of-PR ritual

Before opening (or asking to open) a PR, check:

1. **User-visible behavior change?** Update `README.md` accordingly (the relevant section, not just a changelog entry).
2. **Schema, env var, or deploy config change?** Update README's `Configuration`, `The schema`, or `Stack` section.
3. **Resolves a `Next 3` item in `STATUS.md`?** Check it off in the same PR, promote a later item, and commit the `STATUS.md` change together with the code.
4. **Introduces or closes an `Open question`?** Reflect it in `STATUS.md` — keep the question current.
5. **Adds a deferred decision?** Add it to `STATUS.md` under `Recent decisions` with the trade-off briefly noted (so future-you knows why this branch was taken).

The repo only stays coherent if docs move with code.

## Repo shape

- `app.py` — FastAPI app, lifespan, endpoints
- `graph.py` — LangGraph extraction pipeline (ingest → extract → validate → persist)
- `schemas.py` — Pydantic models, `ExtractedField[T]` with source provenance
- `db.py` — SQLAlchemy 2.0 async, `LeaseRecord` + `ExceptionRecord`
- `qa.py` — grounded Q&A over a stored extraction (Claude Haiku)
- `mcp_server.py` — six-tool MCP facade for Claude Desktop

Tests pattern: inline `python -c` smoke tests via `.venv/bin/python <<'PY' ... PY` heredocs, mocked Anthropic clients. No `pytest` infra yet.
