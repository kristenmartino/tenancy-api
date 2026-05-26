# Bonus demo — Tenancy as an MCP server

**Length:** 25–35s.
**Goal:** Show the same lease abstraction agent accessible from Claude Desktop, doing a multi-tool dance in one natural-language exchange.
**Audience:** Anthropic-aligned engineers, agent builders, Claude Desktop power users (~20% of portfolio viewers — they self-select to click this clip).
**Recording:** Claude Desktop on macOS, screen-record (QuickTime / OBS / Loom). Window sized so the side panel is fully visible.

---

## Pre-record setup

- [ ] `~/Library/Application Support/Claude/claude_desktop_config.json` has the `tenancy` MCP server entry (see [README](../README.md#wiring-it-into-claude-desktop)).
- [ ] `TENANCY_API_BASE=https://tenancy-api-production.up.railway.app` in the env block.
- [ ] Restart Claude Desktop after editing config. The tools panel should show six tools under "tenancy."
- [ ] Source lease `45314996-bed0-41fe-ac67-3232476894ac` is still on production with 8 unresolved exceptions (or pick a fresh one and substitute below).
- [ ] Fresh chat session — no prior context.

---

## Shot list

| # | t | Beat | Screen action | Voice (optional — can be silent) |
|---|---|---|---|---|
| 1 | 0–4s | **Open Claude Desktop** | Fresh chat. Hover the MCP indicator at the bottom of the input box — the "tenancy" server expands showing six tool names: `list_leases`, `get_lease`, `extract_lease`, `query_lease`, `list_exceptions`, `resolve_exception`. | "Same backend, different surface — Claude Desktop with the Tenancy MCP server loaded." |
| 2 | 4–7s | **Type the prompt** | User types: `Pull up lease 45314996 — what's flagged for human review?` Hit send. | — |
| 3 | 7–18s | **Tool calls render** | Claude shows a tool-call card for `list_exceptions(lease_id="45314996…", resolved=False)`. Result expands inline — 8 exceptions. Claude follows up with `get_lease("45314996…")` to pull context. Response summary lands in the chat: "Eight exceptions: four blocking (security deposit, base monthly rent, term start date, term end date — all missing required values), four warnings (low-confidence on the same term dates plus the resident name and address)." | "Claude calls `list_exceptions`, then `get_lease`, then summarizes." |
| 4 | 18–28s | **Resolve via natural language** | User: `Edit the term start date to January 1st 2018 — the contract date on page one says that.` Claude calls `resolve_exception(exception_id="…", action="edit", correction={value:"2018-01-01"})`. Result card shows the resolved row. Claude confirms: "Done. The lease's `term.start_date` is now `2018-01-01` with confidence 1.0, and that blocker is cleared." | "Then natural-language resolve — Claude picks the right tool, the right exception, formats the correction." |
| 5 | 28–32s | **Closing card** | Cut to a static card: "Same agent. SaaS UI for property managers. MCP for engineers." with `github.com/kristenmartino/tenancy-api` underneath. | "Same agent — two surfaces." |

**Total: ~30s.**

---

## Why this prompt

- **One natural-language ask → multi-tool execution.** That's the MCP value prop in 11 seconds.
- **`list_exceptions` + `get_lease` chained.** Shows that the agent reasons over multiple tool outputs before responding.
- **`resolve_exception` with a structured correction parsed from natural-language input.** Demonstrates that the MCP isn't just read-only — it's a complete write API too.
- **The correction is non-trivial.** `term.start_date = 2018-01-01` requires Claude to read the contract-date cell on page 1 (from the `get_lease` response) and infer the start date is the same. That's the human-in-the-loop story but with Claude as the human-substitute.

## What the result card / chat will roughly look like

Claude's response shape (paraphrased — actual output varies):

```
[tool: list_exceptions]
  → 8 exceptions returned

[tool: get_lease]
  → LeaseExtraction with 9 sections

Eight exceptions are flagged for review:

Blocking (4):
  • deposits.security_deposit — required but null
  • rent.base_monthly_rent — required but null
  • term.start_date — required but null
  • term.end_date — required but null

Warnings (4):
  • term.end_date — confidence 0.60 below threshold 0.7
  • term.start_date — confidence 0.60 below threshold 0.7
  • parties[0].address — confidence 0.60 below threshold 0.7
  • parties[0].name — confidence 0.60 below threshold 0.7

The "Date of Lease Contract" on page 1 reads "January 1, 2018",
so the term.start_date exception is recoverable from context.
```

Then on the follow-up:

```
[tool: resolve_exception]
  → resolved: action=edit, correction={value: "2018-01-01"}

Done. term.start_date is now "2018-01-01" with confidence 1.0.
The blocking flag on that field is cleared.
```

---

## Surfaces touched

- Claude Desktop's tool-call UI (collapsible tool cards inline in the chat)
- The MCP server's six tools (4 of them exercised: `list_exceptions`, `get_lease`, `resolve_exception`, and the registration of all six visible in the side panel)
- The same Railway backend the SaaS UI hits — `TENANCY_API_BASE` env var

## What this clip is NOT

- Not the main demo. The 90s primary demo at [`tenancy-web/docs/demo-script.md`](../../tenancy-web/docs/demo-script.md) covers the SaaS-UI story.
- Not a tutorial on MCP setup. That belongs in the README ([Wiring it into Claude Desktop](../README.md#wiring-it-into-claude-desktop)).

## After recording

- Drop the mp4 in `tenancy-api/docs/demo-mcp.mp4`.
- Add a "Also see: [30s MCP demo](docs/demo-mcp.mp4)" link in the README right under the `## MCP surface` section heading.
- Take a static screenshot of Claude Desktop's tool panel showing the six tenancy tools and save as `docs/mcp-tools.png`. Embed in the README MCP section.
- Cross-link from `tenancy-web/README.md` so visitors to either repo find both demos.
