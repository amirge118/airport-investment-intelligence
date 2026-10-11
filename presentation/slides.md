# Airport Investment Intelligence — interview deck

Twelve-slide narrative for a 12–15 minute presentation. The editable PowerPoint is generated from
`presentation/build/build_deck.mjs`; supporting detail lives in `speaker-notes.md` and in the PPTX
speaker notes.

## 1. Find the next airport diligence lead

*A conversational screen for US airport modernization*

- Gate 1: identify leads worth the next diligence step
- Boundary: no terminal ROI or binding-constraint verdict

## 2. The first job is a defensible shortlist

The costly failure is funding a terminal when the binding constraint sits elsewhere.

1. Find leads — quantify demand signals
2. Test the constraint — terminal, runway, airspace, weather
3. Underwrite returns — CAPEX, contribution, NPV

## 3. One interface supports several diligence questions

- Rank airports in a state or region
- Compare airports on one consistent definition and period
- Calculate route-distance shares
- Assess demand pressure with explicit assumptions

## 4. Coverage is broad, but each metric has its own footprint

| Installed metric | Airports |
| --- | ---: |
| US registry | 1,005 |
| FAA outlook | 712 |
| Long-haul routes | 635 |
| Delay | 354 |
| Score eligible | 226 |

**Rule:** missing is never zero.

## 5. The score balances momentum with investable scale

`score = 0.40 × Growth + 0.35 × Scale + 0.25 × Utilization`

- Growth: 2-year CAGR vs same-size peers
- Scale: annual passengers added vs the US market
- Utilization: seat load vs same-size peers

## 6. The leading candidates survive reasonable weight changes

| Growth weight | Top five |
| --- | --- |
| 0.30 | PDX, GRR, SFO, ORD, TYS |
| 0.40 | PDX, GRR, SFO, ORD, TYS |
| 0.50 | PDX, ORD, GRR, SFO, PVD |

PDX remains first and four airports remain in every top five.

## 7. AI interprets the question. Code owns the answer

`Question → LLM plan → validation → deterministic tools → evidence-first answer`

- The model never sees source data and never computes a visible figure
- Invalid proposals fail as a whole
- The server re-resolves airports and selects functions from a fixed metric map

## 8. A data gap becomes a typed result, not a fabricated answer

`computed` · `partial` · `insufficient_evidence` · `clarification_required` · `out_of_scope`

Every visible figure must trace to a structured field, a user input, or a documented constant.

## 9. Versioned source runs keep the demo reproducible

`Official source → candidate run → validation → active pointer → scoped facts`

- Atomic publication keeps readers away from partial data
- A failed refresh keeps the last-good run
- SQLite is the system of record; `snapshot.json` only bootstraps first run

## 10. The design favors traceability and a dependable demo

| Decision | Why now | Replacement trigger |
| --- | --- | --- |
| Workflow over autonomous agent | Reproducible numbers | Multi-step research demand |
| Bundled official exports | No demo-time scraping | Freshness becomes material |
| SQLite on one node | Inspectable, atomic, zero setup | Multi-instance writes |
| No answer cache | Reads are cheap; stale context is costly | Measured latency pressure |

## 11. Evaluation measures planning and numeric faithfulness

- Deterministic gate: **32/32** case runs
- Live model runs: **3 → 12 → 15 → 14**, gate requires 13
- **0** untraceable numbers across evaluated answers
- **0** safety violations in the latest live run
- Median end-to-end latency: **0.97s**

## 12. The lead screen works. The pilot tests business value

**Proven:** evidence-first answers, generic routing, measured fallback behavior.

**Open:** verified FAA CATS revenue data, deployment auth/rate limiting, airport-specific causal
capacity diligence.

**Pilot metrics:** time to shortlist, analyst correction rate, unsupported terminal or profit claims.
