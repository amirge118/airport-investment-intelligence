# Airport Investment Intelligence -- Design

A conversational diligence screen for US airport modernization leads: Gate 1 of an investment funnel
(identify leads worth diligence), not a profitability or terminal-constraint verdict. This document
covers the three required topics first, then the architecture and the pilot that support them.

1. [Scoring methodology](#1-scoring-methodology)
2. [Key tradeoffs](#2-key-tradeoffs)
3. [Where/how AI is used](#3-wherehow-ai-is-used)
4. [Architecture](#4-architecture)
5. [Pilot hypothesis](#5-pilot-hypothesis)

## 1. Scoring methodology

**Opportunity Screening Score v2** (`analytics/calculations.py:screen_v2`) is a lead score for
"which US airport deserves the next diligence step," not a profitability or congestion measure.

An airport-year is eligible only if it has 12 complete reporting months in each of 2023, 2024,
and 2025, at least 100,000 departing passengers in 2025, positive 2025 seats, 2025 seat
utilization in 0-100%, and at least 12 national peers in the same traffic-size cohort
(0.1-0.5M / 0.5-2M / 2-10M / 10M+ departing passengers in 2025 -- a scale bucket, not an FAA hub
class). Three metrics are computed per airport:

- `two_year_CAGR = (P2025 / P2023)^0.5 - 1`, clamped to +-20% before scoring (shown unclamped too)
- `seat_utilization = P2025 / seats2025`
- `annualized_incremental_passengers = (P2025 - P2023) / 2`

`score = 0.40 x peer_percentile(clamped CAGR) + 0.25 x peer_percentile(utilization)
       + 0.35 x national_percentile(annualized_incremental_passengers)`

Growth gets the largest weight (momentum signal); absolute scale is nearly as important
(investors monetize volume); utilization is weighted lowest because it is a weaker, imperfect
service-pressure proxy. CAGR and utilization percentiles are computed within the airport's
*cohort* (fair comparison across similar-size airports); incremental passengers is percentiled
*nationally* (scale should be judged against the whole market, not just same-size peers).

Every ranked row carries a **rank-reversal counterfactual**: the score if 2-year CAGR reverted
to the cohort median, so a reviewer can see how growth-dependent the lead is.
`weight_sensitivity()` reruns the score at growth weight 0.30/0.40/0.50 (utilization fixed at
`0.65 - growth_weight`, scale fixed at 0.35) and flags whether the top-5 order changes --
a sensitivity check, not a claim that the weights are optimal.

**Congestion** is the percent of scheduled BTS reporting-carrier domestic flights arriving >=15
minutes late, with the same year and denominator on both sides of a comparison. **Long-haul share**
is departures at or above a distance threshold (default 3,000 statute miles) over all eligible
passenger-service departures from one origin, cargo excluded. Both come from bundled official 2025
exports (BTS Airline Delay Cause: 354 US airports; T-100 Segment: 635 US origins), each with source
URL, retrieval time, period, and SHA-256. They are snapshots, not a claim of daily freshness. An
uncovered airport or incomparable period returns `insufficient_evidence`. Delay share is an
operational proxy, not proof of terminal crowding; long-haul is departure-weighted, not a passenger
share.

**Demand pressure** reports only observed BTS traffic plus an explicitly analyst-set uplift
scenario and a labeled list of bottleneck *hypotheses* (runway, terminal, airspace, airline
economics, weather) -- never a measured shortfall or a proven cause. **Revenue context**
multiplies an assumed incremental-passenger count by a historical FAA CATS average
revenue-per-passenger (low/base/high capture sensitivity), labeled "historical average, not
marginal contribution, NPV, or profit." No verified CATS extract was acquired, so it is
unavailable for every real airport; the math and labeling are proven against a test fixture.

## 2. Key tradeoffs

| Decision | Why | Rejected alternative | Revisit when |
| --- | --- | --- | --- |
| US-only resolver/analytics | Matches the investment mandate; spends the time budget on real US data gaps instead of a global resolver | Global entity resolution with partial worldwide metrics | Mandate expands and comparable non-US sources are verified |
| Keyword fallback is narrow and asks to rephrase rather than guessing | A wrong guess (e.g. picking a state for an ambiguous city) is worse than an honest "I didn't understand" | A broader regex/NLP heuristic that silently picks a plausible interpretation | Replaced by the live LLM path for most traffic; fallback only needs to cover safety-critical and common cases |
| Server composes the final answer text deterministically; the LLM only plans | Every number is traceable to `analytics/calculations.py`, with no risk of the model rephrasing a figure; demo and tests are reproducible without a model | Let the LLM "narrate" the final answer from the structured result | Users want more natural prose and accept the added non-determinism and latency |
| Bundle one-time official BTS exports for delay and routes | Broad, reproducible evidence without a brittle scraper or demo-time network dependency; checksum manifests make provenance inspectable | Scrape the interactive BTS form on startup, or leave two metric families unavailable | A maintained API or licensed feed exists and freshness becomes a requirement |
| Local SQLite for facts and typed context | Inspectable relational data, atomic source publication, restart continuity, no service setup | JSON-only runtime (hard to query/version) or hosted Postgres/Redis (credentials and ops) | Multi-instance writes, remote users, or stronger retention controls |
| No general answer cache | Indexed local reads and deterministic math are cheap; prompt caching risks stale versions and wrong follow-up context | Cache by raw user prompt | Latency or load evidence; then a version-keyed normalized-plan cache |
| Small single-purpose modules, functions by default | Each piece can be explained and tested alone; the pipeline is provably SQLite/HTTP-free without framework ceremony | Large mixed modules, or a full hexagonal architecture with interfaces for every collaborator | A second storage backend, multiple services, or a larger team |
| Plain stdlib Python + vanilla JS; providers behind one small interface (Groq, Gemini) | Zero dependencies and a one-command run; a provider outage or quota is a config change, not a code change | FastAPI/React or a provider SDK per vendor | More contributors, complex client state, or deployment needs |
| Foreign places detected from a global registry, not a denylist | Paris, London, Rome and others are also real US towns; a denylist refuses them | A hand-maintained list of famous foreign cities | Never, for this failure mode |
| Unsupported qualifiers (`period`, `assumption_overrides`) are rejected or flagged, never silently ignored | An ignored qualifier implies the system answered the exact question when it did not | Accept the field and drop it | Never; any new qualifier must be wired in or explicitly rejected |

## 3. Where/how AI is used

A planner LLM (Groq `openai/gpt-oss-120b` by default, or Gemini; `planning/llm.py` picks the configured
one) is used for exactly one step: turning free text into a structured intent/entity/metric proposal.
Groq uses strict JSON-schema decoding, so the reply cannot be malformed or name an unknown operation.
It never:

- **resolves an airport itself:** the server re-resolves every entity against the US registry, so a
  model-proposed "LHR" is still refused or clarified by server logic;
- **picks which function runs:** requested metrics map to a fixed allowlist of operations, and
  `planning/planner.py:validate_plan` rejects unknown names before anything executes;
- **produces a number the user sees:** every figure comes from `analytics/calculations.py` over rows read from
  SQLite;
- **runs unbounded:** at most 3 operations and 10 entities per plan, 20 ranked rows, and a 15-second
  timeout.

`planning/planner.py:parse_model_proposal` is the single gate for model output. It type-checks every field
and applies the same allowlist and bounds as the keyword planner, rejecting a malformed or
over-budget proposal **as a whole** rather than running a partial plan. A proposed geographic scope
("Texas") is resolved through the US registry like any other entity, so it can never become an
unscoped all-US ranking.

On any model failure (missing key, timeout, malformed JSON, HTTP error) or a rejected plan the system falls back to
the keyword planner, shows `planner_source` in the trace, and still returns an evidence-grounded
answer. `tests/conversation/test_orchestrator.py` (case E14) forces `LLMUnavailable` and asserts the answer is
still `computed`.

**Measured.** `scripts/run_eval.py --live` plans the predeclared cases with the real model and applies the
same checks as the deterministic run. The first run scored 3/15 correct plans; the fixes were to the
system, never to the expected labels (EVALUATION.md lists them): a metric glossary in the prompt, and
server-side normalization so the server, not the model, decides which operation computes a metric. The
next runs scored 15/15 and 14/15, with a median of about 1 second per case. The one unstable case is the
E13 injection: when the model declines to plan it, the keyword planner answers from verified data.

## 4. Architecture

```text
Question -> LLM or keyword planner -> schema/allowlist validation -> US resolver
         -> metric coverage gate -> parameterized SQLite fact query
         -> deterministic analytics -> evidence-first answer -> browser rendering
```

| Package | Owns | May depend on |
| --- | --- | --- |
| `server/` | HTTP routing, untrusted-input validation, CLI, composition root | everything (the only place that wires the graph) |
| `conversation/` | one chat turn: order of decisions, follow-up context, answer envelope | `planning`, `analytics`, `facts` |
| `planning/` | question -> validated plan; the only model call; US resolver | `facts` |
| `analytics/` | pure deterministic calculations and coverage | `facts` only |
| `ingestion/` | live adapters, bundled export parsers, install, background refresh | `storage` |
| `storage/` | SQLite schema, atomic publication, scoped reads, sessions | two constants and one type (below) |

- **Boundaries are tested.** The question pipeline depends on the `FactSource` protocol in
  `facts.py`, never on `sqlite3`, HTTP, `storage` or `ingestion`; `storage` imports only two
  row-count floors and the `TurnContext` type it persists. `tests/test_architecture.py` fails the
  build on any other direction or an import cycle.
- **Data.** SQLite is the system of record; `data/snapshot.json` is only the first-run bootstrap.
  Each source is a versioned run with a manifest and one active pointer. A refresh validates a
  complete candidate and switches the pointer in one transaction, so readers never see a partial
  dataset; a failed refresh keeps the last-good run and reports it in `/health`.
- **Queries.** Each operation runs a fixed parameterized query for only the rows it needs (the
  selected airports, or the national population a percentile ranking requires). Each request has
  its own fact bundle, so concurrent threads never share data.
- **State.** SQLite keeps only typed follow-up slots (24 hours, at most 1,000 sessions), not
  message history; the browser keeps up to 40 rendered turns per tab.
- **Boundary.** Localhost only, `Host` allowlist, JSON-only POST. No authentication or rate
  limiting: a deployed service would need both first.

## 5. Pilot hypothesis

The tool should reduce an analyst's time to a defensible diligence shortlist while maintaining
**zero unsupported terminal-constraint or profit claims**. The most expensive failure is
recommending capital work (e.g. a new terminal) when the real constraint is runway capacity,
airspace, weather, an operating agreement, or airline economics. A shadow-mode pilot against the
current manual workflow should measure:

1. time to a first shortlist of N airports;
2. analyst correction rate (how often a ranked lead is overridden or discarded);
3. false-positive constraint classifications (how often "high opportunity score" is misread as
   "the terminal is the bottleneck" before Gate 2 diligence).

This is Gate 1 of a three-gate funnel (quantify opportunity -> test the terminal constraint ->
underwrite incremental contribution). It is not, and does not claim to be, Gate 2 or Gate 3.
