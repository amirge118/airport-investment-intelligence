# Airport Investment Intelligence (US-only)

A conversational diligence screen for US airport modernization leads. It turns a question into a
validated plan, resolves US airports and regions, checks installed source coverage, and computes
every number deterministically. Each answer is labeled as computed, partial, missing evidence,
ambiguous, unsupported metric, or out of scope.

This is deliberately **Gate 1** of an investment funnel: identify leads worth diligence. It does not
claim that a terminal is the binding constraint, that unserved demand was measured, or that a project
is profitable.

## Quick start

Requirements: Python 3.9+ (the macOS system `python3` works). No packages, no build step, no API key.

```bash
python3 -m airport_intel.server --offline
```

Open <http://127.0.0.1:8008/> and try one of the example questions. The first boot builds
`data/airport_intel.sqlite3` from the bundled data in about 4 seconds.

## What it does

- Questions and follow-ups about US airports, cities, states, and regions; follow-ups keep context
  ("And at 3,500 miles?", "What about JFK instead of SNA?").
- Opportunity Screening Score v2 and passenger-growth ranking from BTS 2023-2025.
- Delays (BTS 2025, 354 airports), long-haul route share (BTS T-100 2025, 635 origins), FAA TAF
  FY2025-FY2030 outlook (712 airports), and FAA NPIAS 2025-2029 development context (794 airports).
- Optional LLM intent planning (Groq `openai/gpt-oss-120b` with strict JSON schema, or Gemini) with a
  bounded deterministic fallback; the model never produces a number.
- SQLite runtime store with versioned, validated source runs; a failed refresh keeps last-good data.
- Airport Explorer, evidence cards, and optional voice input (browser Web Speech API).

**Not claimed:** FAA CATS airport financials are not installed, so there is no profit, NPV, marginal
contribution, measured unserved demand, or proven terminal bottleneck. NPIAS is a five-year
all-development planning estimate, never terminal-only CAPEX or funding.

## Verify

```bash
python3 -m unittest discover
python3 scripts/run_eval.py
python3 scripts/run_eval.py --live   # also plans every case with the configured model
```

Expected: 265 tests, and all 16 predeclared evaluation cases pass on both data sources (32 runs). The evaluation uses a throw-away
database and never calls a model unless `--live` is given; results are written to [EVALUATION.md](EVALUATION.md). One test
rebuilds the public tables from the real bundled files and reconciles known ANC, LAX/SNA, and SFO
values.

## Run options

```bash
python3 -m airport_intel.server                 # also refreshes BTS, OurAirports and FAA TAF online
python3 -m airport_intel.server --port 8010
curl -s http://127.0.0.1:8008/health | python3 -m json.tool
```

The server binds to localhost only, accepts only `localhost`/`127.0.0.1` `Host` headers, and requires
JSON on POST. It has **no authentication or rate limiting**: it is a local demo, not a deployable
service.

**Optional model planner.** Copy `.env.local.example` to `.env.local` and set `GROQ_API_KEY`
(preferred) or `GEMINI_API_KEY`; `PLANNER_PROVIDER` pins one. Keys go only in a request header and are
never logged. Any model failure or rejected plan falls back visibly to the keyword planner, and the
trace records why (`planner_fallback_reason`).

**Inspect the data.** The model has no SQL access; each validated operation runs a fixed
parameterized query. To look at what it reads:

```bash
sqlite3 data/airport_intel.sqlite3 \
  "SELECT source, retrieved_at, row_count FROM source_runs WHERE status='active';"
sqlite3 data/airport_intel.sqlite3 \
  "SELECT code, year, SUM(flights), SUM(delayed) FROM active_airport_delay_month WHERE code IN ('LAX','SNA') GROUP BY code, year;"
```

`scripts/refresh_*.py` rebuild the bundled `data/snapshot.json` from the live sources.

## Project map

```text
airport_intel/
  server/                 HTTP boundary + composition root
    __main__.py           CLI (`python3 -m airport_intel.server`)
    app.py                routing, static files, structured request logs
    request_validation.py untrusted input: Host allowlist, JSON type, size, session id, message
    runtime.py            build_runtime(): wires storage, refresh and the conversation once
  conversation/           one chat turn
    orchestrator.py       plan -> early outcomes -> resolve -> context -> facts -> answer
    context.py            follow-up context: which slots a follow-up inherits
    operations.py         rank / compare / long-haul / demand+revenue -> evidence-first answer
    answers.py            the shared answer envelope; refusal, clarification, unsupported
    narrative.py          summary and key takeaway text
  planning/               question -> validated plan
    planner.py            typed Plan contract, allowlists/bounds, keyword fallback
    llm.py                picks the planner model (Groq or Gemini); the only path to a model
    llm_contract.py       shared prompt, strict JSON schema, failure type, key loading
    groq.py, gemini.py    provider clients
    resolver.py           US airport/city/state/region resolution; spots foreign places
  analytics/              pure and deterministic, no I/O
    calculations.py       score, comparison, long-haul, demand, revenue math
    highlights.py         airport preview highlights
    coverage.py           installed vs missing metric families; /health counts
  ingestion/              getting public data in
    live_sources.py       bounded BTS, OurAirports, and FAA TAF adapters
    bundled_exports.py    bounded parsers for the bundled official CSV/XLSX exports
    install.py            checksum-verified one-time install of the bundled exports
    refresh.py            background startup refresh + per-source status
  storage/                SQLite: schema, atomic source publication, scoped reads, sessions
  facts.py                FactSource contract between the pipeline and storage
web/                      buildless UI (vanilla HTML/CSS/JS)
data/                     bootstrap snapshot + official exports with SHA-256 manifest
scripts/                  snapshot refresh and evaluation runner
tests/                    mirrors the packages above, plus test_architecture.py (package boundaries)
```

See [DESIGN.md](DESIGN.md) for scoring, architecture, AI boundaries, trade-offs and the pilot.
