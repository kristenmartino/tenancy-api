# Demo readiness

Tenancy is a **portfolio / case-study** project (STATUS.md → "Audience class"), not a
productized SaaS. The bar for the live surfaces is therefore **"hot, predictable, and
boring" during a recording or interview** — not 24/7 uptime.

This is the pre-demo procedure. It is deliberately reliability *hardening*, **not
productization**: permanent always-on paid infra is intentionally **not** enabled
(see [Why not just keep it always-on?](#why-not-just-keep-it-always-on) below).

## The surfaces

| Surface | URL |
|---|---|
| Frontend (canonical) | https://tenancy.kristenmartino.ai |
| Frontend (Vercel default) | https://tenancy-opal.vercel.app |
| API | https://tenancy-api-production.up.railway.app |
| Demo lease | shorthand `45314996` → full id `45314996-bed0-41fe-ac67-3232476894ac` |

Both the Vercel frontend and the Railway API + Neon DB **cold-start after idle**. The
homepage chains Vercel → Railway → Neon, and any hop can add seconds (or, on a fully
slept Railway service, a 502). The whole chain must be warm before you're on camera.

## Operating modes

| Situation | What to do |
|---|---|
| Normal portfolio traffic | Nothing — the GitHub keep-warm cron is best-effort and fine |
| Casual link sharing | Same; the cron's cold-start-tolerant retry covers most misses |
| **Recording a demo video** | Manual pre-warm (below) + local warm loop running |
| **Live interview walkthrough** | Manual pre-warm + local warm loop + (optional) disable sleep for the day |
| Customer / prospect demo | Disable Railway/Neon sleep for the window, or move to real always-on |

## ~10 minutes before recording / interview

**1. Pre-warm the API.** Run twice — the first call may be a slow cold resume:

```bash
curl -fsS --max-time 10 https://tenancy-api-production.up.railway.app/health        # → {"status":"ok"}
curl -fsS --max-time 45 https://tenancy-api-production.up.railway.app/leases >/dev/null && echo OK   # touches the DB
```

Both should return in <2s on the second run. `/health` does not touch the DB; `/leases`
does — you want **both** warm.

**2. Pre-warm and eyeball the frontend.** Open the demo lease directly:

```
https://tenancy.kristenmartino.ai/leases/45314996-bed0-41fe-ac67-3232476894ac
```

Then confirm the three things you'll actually show:

- the PDF renders and a **highlighted field** is clickable (click one — e.g. the property address),
- the **exception panel** loads,
- a **Q&A prompt** returns an answer.

**3. Q&A smoke test** (optional, from a terminal). Note: Q&A is an LLM call and takes
**~20–30s even when warm** — don't be surprised by the wait on camera, and consider
pre-running it so the answer is already on screen.

```bash
curl -fsS --max-time 60 -X POST \
  https://tenancy-api-production.up.railway.app/leases/45314996-bed0-41fe-ac67-3232476894ac/query \
  -H 'Content-Type: application/json' \
  -d '{"question":"What exceptions are flagged on this lease?"}'
```

The two canonical demo prompts (also used in the MCP bonus clip, `docs/demo-mcp.md`):

- *"Pull up lease 45314996 — what's flagged?"*
- *"Edit the term start date to 2018-01-01."*

## During the recording / interview: local warm loop

The GitHub Actions cron is best-effort (often 30+ min between runs — see
`.github/workflows/keep-warm.yml`). For a live session, don't trust it. Run this in a
spare terminal and leave it going for the whole session:

```bash
while true; do
  date
  curl -fsS --max-time 30 https://tenancy-api-production.up.railway.app/leases > /dev/null \
    && echo "Tenancy API warm" \
    || echo "Tenancy API ping failed"
  sleep 60
done
```

This is the cheapest reliable way to keep it hot for the duration of the demo.

## High-stakes demos: temporarily disable sleep

If a single cold blip would be costly (recorded customer demo, live interview),
eliminate cold-starts for the window instead of racing them:

- **Railway** — with serverless / app-sleeping enabled, a service is considered inactive
  after ~10 min without outbound traffic; the first request to a slept service can be
  slow or return a **502**. Disable serverless/sleeping on the `tenancy-api-production`
  service for the window (needs a plan that allows always-on).
- **Neon** — compute can **scale to zero after ~5 min** idle; the first query after that
  pays a resume penalty. Paid plans can disable scale-to-zero on the production branch.

**Re-enable both afterward.** There's no reason to pay for always-on outside demo
windows at this stage.

## Why not just keep it always-on?

Because Tenancy is a case study, not a product with traffic. Permanent paid always-on
infra would be overbuilding. The chosen posture: best-effort warming for free, manual +
local-loop warming for demos, temporary no-sleep only for high-stakes windows. Revisit
if Tenancy starts getting real traffic or becomes an active prospect surface
(STATUS.md → "Audience class" / "Velocity").

## Related

- Keep-warm cron: `.github/workflows/keep-warm.yml` — best-effort, cold-start-tolerant
  (40s/attempt). Trigger a manual run from the Actions tab (`workflow_dispatch`) to
  warm on demand.
- DB resilience: `pool_pre_ping=True` in `db.py` transparently reconnects dropped Neon
  connections (STATUS.md → Recent decisions).
- MCP bonus demo flow: `docs/demo-mcp.md`.
