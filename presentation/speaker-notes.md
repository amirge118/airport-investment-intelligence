# Speaker notes

Detail behind the 12-slide interview deck. The methodology source of truth is `DESIGN.md`; measured
results come from `EVALUATION.md`. Counts were verified on 2026-10-11.

## 1. Title

Open with the customer outcome: the product helps an analyst decide which US airport deserves the
next diligence step. State the boundary early. It does not claim terminal ROI and does not prove
that the terminal is the binding constraint.

## 2. Business framing

Airport growth can be real while a terminal expansion remains the wrong intervention. Gate 1 finds
leads, Gate 2 tests the actual bottleneck, and Gate 3 underwrites incremental returns. The product
owns only Gate 1.

## 3. Product experience

The brief's questions are capability probes, not hardcoded templates. The same planner handles novel
US airports, states, regions and follow-up qualifiers. The browser keeps rendered turns in the current
tab; SQLite retains typed follow-up slots for 24 hours, not full message history.

## 4. Coverage

Installed coverage: 1,005 registry airports, 712 with FAA TAF outlook, 635 T-100 origins, 354 with
delay evidence, and 226 eligible for the score. Each metric has separate airport and period coverage.
An uncovered airport produces a named evidence gap rather than a zero or a substitute metric.

## 5. Score

Growth receives the largest weight because momentum is the primary opportunity signal. Scale is
nearly as important because investors monetize volume. Utilization is the weakest proxy, so it gets
the lowest weight. Growth and utilization use peer cohorts; incremental passengers use the national
distribution. The weights are a documented prior, not a fitted truth.

## 6. Sensitivity

The sensitivity varies growth from 0.30 to 0.50, fixes scale at 0.35, and gives the remainder to
utilization. PDX remains first and four of five names persist. This supports robustness, not optimality.
Each row also carries a counterfactual score if growth returns to the cohort median.

## 7. AI and control plane

The model receives the question, typed follow-up context, and the JSON schema. It never sees source
facts or tool output. Validation checks the schema, allowlist, grounding and bounds. The server then
re-resolves airports and maps metrics to fixed operations. A rejected or unavailable model triggers a
narrow, labeled fallback.

## 8. Evidence states

A partial answer preserves valid evidence and names the missing part. Demand pressure is partial by
design because public sources do not measure turned-away passengers. Unsupported qualifiers are
rejected or flagged rather than silently ignored.

## 9. Data lifecycle

Every import is a versioned source run with URL, period, retrieval time, checksum and row count. The
importer validates a complete candidate before switching the active pointer in one transaction. A
failed refresh keeps the last-good run. SQLite provides inspectable relational facts and restart
continuity without a hosted service.

## 10. Tradeoffs

The architecture is intentionally time-boxed. Revisit the workflow when users need multi-step
research, bundled exports when freshness becomes material, SQLite when multi-instance writes appear,
and caching only after latency or load evidence. US-only scope matches the investment mandate.

## 11. Evaluation

Sixteen predeclared cases run against both missing and full coverage for 32 deterministic runs. All
pass. Live model planning improved from 3 to 12 to 15 correct plans, then 14 on the stability rerun.
The gate requires 13. Median latency is 0.97 seconds and the maximum is 1.76 seconds. Four runs are a
smoke test, not an accuracy estimate. The broader suite reports 265 tests.

## 12. Close and pilot

The prototype proves generic routing, evidence-first answers and measured fallback behavior. The
largest product gap is verified FAA CATS revenue context. A deployment also needs authentication and
rate limiting. Pilot in shadow mode and measure time to shortlist, analyst correction rate, and any
unsupported terminal or profit claim.
