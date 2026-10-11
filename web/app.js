/*
 * Airport Investment Intelligence: browser client.
 *
 * Buildless and dependency-free. The server's static allowlist serves exactly
 * one script (STATIC_ROUTES in airport_intel/server.py), so responsibilities
 * are kept apart as sections of this file rather than as separate modules:
 *
 *   1. DOM and formatting helpers
 *   2. Session id and same-tab transcript (sessionStorage)
 *   3. Data coverage overview and status        GET  /health
 *   4. Answer rendering                         POST /api/chat response envelope
 *   5. Airport explorer                         GET  /api/airports, /api/airports/{code}/preview
 *   6. Chat flow, event wiring and start-up     POST /api/chat, /api/session/clear
 *
 * The client formats server-owned values; it never derives a new figure.
 * Server text is always inserted with textContent, never as HTML.
 */
(() => {
  "use strict";

  // ---------------------------------------------------------------------------
  // 1. DOM and formatting helpers
  // ---------------------------------------------------------------------------

  const byId = (id) => document.getElementById(id);
  const ui = {
    conversation: byId("conversation"),
    input: byId("composer-input"),
    sendButton: byId("send-button"),
    micButton: byId("mic-button"),
    turnCount: byId("turn-count"),
    modelStatus: byId("model-status"),
    dataStatus: byId("data-status"),
    identityCount: byId("identity-count"),
    clearChat: byId("clear-chat"),
    airportSearch: byId("airport-search"),
    airportResults: byId("airport-results"),
    airportPreview: byId("airport-preview"),
  };

  /** Create an element. attrs: class, text, or any attribute; null children are skipped. */
  function el(tag, attrs, children) {
    const node = document.createElement(tag);
    if (attrs) for (const [key, value] of Object.entries(attrs)) {
      if (key === "class") node.className = value;
      else if (key === "text") node.textContent = value;
      else node.setAttribute(key, value);
    }
    (children || []).forEach((child) => { if (child) node.appendChild(child); });
    return node;
  }

  const humanize = (name) => String(name).replace(/_/g, " ");
  const count = (value) => Number(value).toLocaleString("en-US");
  const signedCount = (value) => `${value > 0 ? "+" : ""}${count(value)}`;
  const signedPct = (value) => `${value > 0 ? "+" : ""}${value}%`;
  const compact = (value) => new Intl.NumberFormat("en-US", { notation: "compact", maximumFractionDigits: 1 }).format(value);
  const plural = (n, word) => `${n} ${word}${n === 1 ? "" : "s"}`;

  // ---------------------------------------------------------------------------
  // 2. Session id and same-tab transcript
  // ---------------------------------------------------------------------------
  // The server keeps typed follow-up context keyed by this opaque id. The tab
  // keeps the rendered turns so a refresh re-renders them without a new API call.

  const SESSION_KEY = "airport-intel-session-id";
  const TRANSCRIPT_KEY = "airport-intel-transcript";
  const LAST_ACTIVE_KEY = "airport-intel-last-active";
  const MAX_TRANSCRIPT_ENTRIES = 40;
  const SERVER_CONTEXT_TTL_MS = 24 * 60 * 60 * 1000; // mirrors storage/sessions.py SESSION_TTL_HOURS

  function newSessionId() {
    if (window.crypto && crypto.randomUUID) return "web-" + crypto.randomUUID();
    const bytes = crypto.getRandomValues(new Uint8Array(16));
    return "web-" + Array.from(bytes, (b) => b.toString(16).padStart(2, "0")).join("");
  }

  function loadTranscript() {
    try {
      const stored = JSON.parse(sessionStorage.getItem(TRANSCRIPT_KEY) || "[]");
      return Array.isArray(stored) ? stored.slice(-MAX_TRANSCRIPT_ENTRIES) : [];
    } catch (_err) {
      sessionStorage.removeItem(TRANSCRIPT_KEY);
      return [];
    }
  }

  let sessionId = sessionStorage.getItem(SESSION_KEY);
  if (!sessionId) {
    sessionId = newSessionId();
    sessionStorage.setItem(SESSION_KEY, sessionId);
  }
  let transcript = loadTranscript();
  let turns = transcript.filter((entry) => entry && entry.role === "user").length;

  function persistTranscript(entry) {
    if (entry) transcript.push(entry);
    transcript = transcript.slice(-MAX_TRANSCRIPT_ENTRIES);
    try {
      sessionStorage.setItem(TRANSCRIPT_KEY, JSON.stringify(transcript));
    } catch (_err) {
      // Storage quota: keep the most recent turns rather than losing all of them.
      transcript = transcript.slice(-12);
      try { sessionStorage.setItem(TRANSCRIPT_KEY, JSON.stringify(transcript)); } catch (_ignored) { /* best effort */ }
    }
  }

  function forgetSession() {
    [SESSION_KEY, TRANSCRIPT_KEY, LAST_ACTIVE_KEY].forEach((key) => sessionStorage.removeItem(key));
  }

  // ---------------------------------------------------------------------------
  // 3. Data coverage overview and status
  // ---------------------------------------------------------------------------
  // Each sidebar overview item ([data-coverage]) gets a badge derived from the
  // /health coverage report: what is installed today, never what is planned.

  function familyBadge(family, noun) {
    if (!family) return { tone: "unknown", text: "?" };
    if (family.installed === true) {
      const covered = family.covered_airports;
      return {
        tone: "available",
        text: covered ? `${count(covered)} ${noun || "airports"}` : "✓",
        title: [covered ? `${count(covered)} airports covered` : "Installed", family.period ? `period ${family.period}` : ""]
          .filter(Boolean).join(" · "),
      };
    }
    if (family.installed === "partial") return { tone: "partial", text: "Partial", title: family.reason || "" };
    return { tone: "missing", text: "—", title: `Not installed${family.reason ? ": " + family.reason : ""}` };
  }

  function forecastBadge(demand) {
    const covered = demand && demand.forecast_covered_airports;
    if (!demand) return { tone: "unknown", text: "?" };
    if (!covered) return { tone: "missing", text: "—", title: `Not installed${demand.reason ? ": " + demand.reason : ""}` };
    return { tone: "available", text: `${count(covered)} airports`, title: `${count(covered)} airports with an FAA forecast` };
  }

  const COVERAGE_BADGES = {
    passenger_trend: (c) => familyBadge(c.absolute_passenger_change, "airports"),
    absolute_change: (c) => familyBadge(c.absolute_passenger_change),
    seat_utilization: (c) => familyBadge(c.opportunity_score),
    screening_score: (c) => familyBadge(c.opportunity_score, "airports"),
    faa_forecast: (c) => forecastBadge(c.demand_pressure),
    congestion: (c) => familyBadge(c.congestion),
    route_mix: (c) => familyBadge(c.longhaul_share),
    revenue: (c) => familyBadge(c.revenue_context),
  };

  /** Summarize per-source refresh state; never call data fresh unless every source refreshed this run. */
  function refreshSummary(refresh) {
    const states = Object.values(refresh || {}).map((item) => item.state);
    const all = (state) => states.length > 0 && states.every((item) => item === state);
    const refreshing = states.some((state) => state === "refreshing" || state === "queued");
    if (refreshing) return { refreshing, tone: "partial", badge: "Refreshing", label: "refreshing sources" };
    if (states.includes("failed_last_good")) return { refreshing, tone: "partial", badge: "Last-good", label: "last-good data; refresh failed" };
    if (all("offline")) return { refreshing, tone: "available", badge: "Offline", label: "offline · bundled/last-good data" };
    if (all("fresh")) return { refreshing, tone: "available", badge: "Refreshed", label: "sources refreshed" };
    return { refreshing, tone: "partial", badge: "Bundled", label: "bundled/last-good data (not yet refreshed)" };
  }

  function setBadge(key, badge) {
    const target = document.querySelector(`[data-coverage="${key}"] .coverage-badge`);
    if (!target) return;
    target.textContent = badge.text;
    target.className = `coverage-badge tone-${badge.tone}`;
    if (badge.title) target.title = badge.title; else target.removeAttribute("title");
  }

  function setStatusLine(node, text) {
    node.replaceChildren(el("span", { class: "status-dot", "aria-hidden": "true" }), el("span", { text }));
  }

  async function loadHealth() {
    try {
      const response = await fetch("/health");
      if (!response.ok) throw new Error(`health ${response.status}`);
      const data = await response.json();
      const coverage = data.coverage || {};

      setStatusLine(ui.modelStatus, data.model_status === "keyless_fallback"
        ? "Keyless fallback mode" : `Live: ${data.model_status}`);

      const refresh = refreshSummary(data.refresh);
      ui.dataStatus.textContent = `SQLite · ${refresh.label} · years ${(coverage.bts_years_installed || []).join(", ")}`;
      ui.dataStatus.title = Object.entries(data.source_runs || {}).map(([name, run]) =>
        `${name}: retrieved ${run.retrieved_at || "unknown"}${run.period ? ` · period ${run.period}` : ""}`).join("\n");

      const identity = (coverage.us_airport_identity || {}).covered_airports;
      ui.identityCount.textContent = identity ? `${count(identity)} US airports in the registry` : "US airport registry loaded";

      Object.entries(COVERAGE_BADGES).forEach(([key, badgeFor]) => setBadge(key, badgeFor(coverage)));
      setBadge("freshness", { tone: refresh.tone, text: refresh.badge, title: ui.dataStatus.title });

      // Keep the status honest while the background refresh is still running.
      if (refresh.refreshing) setTimeout(loadHealth, 5000);
    } catch (_err) {
      setStatusLine(ui.modelStatus, "Status unavailable");
      ui.dataStatus.textContent = "Data status unavailable. Answers still state their own sources.";
      ui.identityCount.textContent = "registry status unavailable";
      [...Object.keys(COVERAGE_BADGES), "freshness"].forEach((key) => setBadge(key, { tone: "unknown", text: "?" }));
    }
  }

  // ---------------------------------------------------------------------------
  // 4. Answer rendering
  // ---------------------------------------------------------------------------
  // Renders the single answer envelope from airport_intel/answers.py. Every
  // result_state uses the same keys, so no shape is special-cased.

  const RESULT_STATES = {
    computed: { label: "Computed", tone: "available" },
    partial: { label: "Partial", tone: "partial" },
    clarification_required: { label: "Needs clarification", tone: "info" },
    insufficient_evidence: { label: "Insufficient evidence", tone: "partial" },
    unsupported_metric: { label: "Unsupported metric", tone: "unknown" },
    out_of_scope: { label: "Out of scope", tone: "unknown" },
  };
  const stateLabel = (state) => (RESULT_STATES[state] || {}).label || state;

  const signedCompact = (value) => `${value > 0 ? "+" : value < 0 ? "−" : ""}${compact(Math.abs(value))}`;
  const pct1 = (value) => `${Number(value).toFixed(1)}%`;
  const signedPct1 = (value) => `${value > 0 ? "+" : value < 0 ? "−" : ""}${Math.abs(Number(value)).toFixed(1)}%`;
  const toneOf = (value) => (value > 0 ? "up" : value < 0 ? "down" : "neutral");

  /** One figure: label and detail on the left, the value on the right. opts: tone, title (full value). */
  function metricTile(label, value, caption, opts = {}) {
    return el("div", { class: `metric tone-${opts.tone || "neutral"}`, ...(opts.title ? { title: opts.title } : {}) }, [
      el("div", { class: "metric-text" }, [
        el("span", { class: "metric-label", text: label }),
        caption ? el("small", { text: caption }) : null,
      ]),
      el("span", { class: "metric-value", text: value }),
    ]);
  }

  // One row per metric per airport, so a comparison always shows each
  // airport's actual figure, never just the word "computed".
  function metricTiles(metric) {
    if (metric.status !== "computed") {
      return [metricTile(humanize(metric.name), metric.status || "n/a", metric.reason || "")];
    }
    const data = metric.data;
    if (metric.name === "congestion" && data && data.airports) {
      return data.airports.map((a) => metricTile(
        `${a.code} · delayed arrivals`, pct1(a.delayed_pct),
        `On-time ${pct1(a.on_time_pct)} · cancelled ${pct1(a.cancelled_pct)} · BTS On-Time`,
      ));
    }
    if (metric.name === "absolute_passenger_change" && Array.isArray(data)) {
      return data.map((row) => metricTile(
        `${row.code} · passengers added 2023→25`, signedCompact(row.absolute_change),
        `${compact(row.passengers_2023)} → ${compact(row.passengers_2025)} departing · ${signedPct1(row.two_year_cagr_pct)} a year`,
        { tone: toneOf(row.absolute_change), title: `${signedCount(row.absolute_change)} departing passengers` },
      ));
    }
    if (metric.name === "longhaul_share" && data) {
      return [metricTile(`of departures fly ${count(data.threshold_miles)}+ miles`, `${data.percentage}%`,
        `${count(data.long_departures)} of ${count(data.total_departures)} scheduled passenger departures`)];
    }
    if (metric.name === "demand_pressure" && data) {
      const observed = data.observed;
      const tiles = [metricTile("Departing passengers · 2025", compact(observed.passengers_2025),
        `${signedPct1(observed.two_year_cagr_pct)} a year since 2023 · BTS T-100`,
        { tone: toneOf(observed.two_year_cagr_pct), title: `${count(observed.passengers_2025)} departing passengers` })];
      const forecast = data.forecast;
      tiles.push(forecast.available
        ? metricTile(`FAA outlook · ${forecast.period.replace("-", " → ")}`, signedPct1(forecast.enplanement_change_pct),
          `${compact(forecast.enplanements_2025)} → ${compact(forecast.enplanements_2030)} enplanements · FAA TAF`,
          { tone: toneOf(forecast.enplanement_change_pct),
            title: `${count(forecast.enplanements_2025)} → ${count(forecast.enplanements_2030)} enplanements` })
        : metricTile("FAA outlook", "—", forecast.reason));
      return tiles;
    }
    if (metric.name === "revenue_context" && data) {
      return [metricTile("Indicative revenue · base case", `$${compact(data.scenarios.base)}`,
        `Low $${compact(data.scenarios.low)} · high $${compact(data.scenarios.high)} · historical average, not profit`,
        { title: `$${count(data.scenarios.base)}` })];
    }
    if (data && typeof data === "object" && "code" in data && "value" in data) {
      return [metricTile(humanize(metric.name), `${data.code}: ${data.value}`)];
    }
    return [metricTile(humanize(metric.name), "computed")];
  }

  // Opportunity Screening Score v2 drivers, in the order the legend shows them.
  // Weights and percentiles come from each server row; the bar only draws them.
  const SCORE_PARTS = [
    { key: "growth", field: "growth_percentile", label: "Growth vs similar-size peers" },
    { key: "scale", field: "incremental_percentile", label: "Passengers added (national)" },
    { key: "utilization", field: "utilization_percentile", label: "Seat utilization vs peers" },
  ];

  /** "2M–10M departures" (departing-passenger size class) -> "2M–10M passengers / yr". */
  const sizeClass = (cohort) => (cohort ? `${String(cohort).replace(/\s*departures$/, "")} passengers / yr` : "");

  function scoreLegend(weights) {
    return el("div", { class: "score-legend" }, [
      el("p", {}, [
        el("strong", { text: "How the score works. " }),
        el("span", { text: "0–100, built from three drivers. Growth and utilization are ranked against airports of the same size class; passengers added is ranked nationally." }),
      ]),
      el("ul", {}, SCORE_PARTS.map((part) => el("li", {}, [
        el("span", { class: `swatch seg-${part.key}`, "aria-hidden": "true" }),
        el("span", { text: `${part.label} · ${Math.round(weights[part.key] * 100)}%` }),
      ]))),
    ]);
  }

  function scoreCell(row) {
    const cell = el("td", { class: "score-cell" }, [el("span", { class: "score-number", text: String(row.score) })]);
    if (row.weights) {
      cell.appendChild(el("span", { class: "score-bar", "aria-hidden": "true" }, SCORE_PARTS.map((part) =>
        el("span", { class: `score-seg seg-${part.key}`, style: `width:${row.weights[part.key] * row[part.field]}%` }))));
    }
    return cell;
  }

  /** Row tooltip: each driver's peer percentile, then the server's counterfactual. */
  function rowExplanation(row) {
    const lines = SCORE_PARTS.filter((part) => row[part.field] !== undefined)
      .map((part) => `${part.label}: ${row[part.field]}th percentile`);
    if (row.rank_reversal_counterfactual) lines.push("", row.rank_reversal_counterfactual);
    return lines.join("\n");
  }

  function metricGrid(metrics) {
    const tiles = metrics.flatMap(metricTiles);
    if (!tiles.length) return null;
    return el("div", { class: tiles.length === 1 ? "metric-grid is-single" : "metric-grid stat-list" }, tiles);
  }

  function rankingTable(rows) {
    // Absolute-change rankings have no score; never label that column "Score".
    const isAbsoluteChange = rows[0].absolute_change !== undefined && rows[0].score === undefined;
    const headers = isAbsoluteChange
      ? ["Airport", "Passengers added (2023→2025)", "2-yr CAGR", "2025 passengers"]
      : ["Airport", "Score", "Growth / yr", "Seats filled", "Peer set"];
    const pctCell = (value, digits) => (value === undefined || value === null ? "-"
      : `${typeof value === "number" ? value.toFixed(digits) : value}%`);

    const body = el("tbody", {}, rows.map((row, index) => {
      const airport = el("th", { class: "airport-cell", scope: "row" }, [
        el("span", { class: "rank", text: String(index + 1) }),
        el("span", {}, [
          el("strong", { text: `${row.name || row.code} (${row.code})` }),
          el("small", { text: sizeClass(row.cohort) }),
        ]),
      ]);
      const cells = isAbsoluteChange
        ? [el("td", { class: "score-cell", text: signedCount(row.absolute_change) }),
          el("td", { text: pctCell(row.two_year_cagr_pct, 1) }),
          el("td", { text: count(row.passengers_2025) })]
        : [scoreCell(row),
          el("td", { text: pctCell(row.two_year_cagr_pct, 1) }),
          el("td", { text: pctCell(row.seat_utilization_pct, 1) }),
          el("td", { class: "muted-cell", text: row.peer_count !== undefined ? `${row.peer_count} airports` : "-" })];
      const tr = el("tr", {}, [airport, ...cells]);
      if (!isAbsoluteChange) tr.title = rowExplanation(row);
      return tr;
    }));
    const table = el("table", { class: "results-table" }, [
      el("thead", {}, [el("tr", {}, headers.map((text) => el("th", { scope: "col", text })))]),
      body,
    ]);
    return el("div", { class: "table-block" }, [
      el("div", { class: "table-wrap" }, [table]),
      isAbsoluteChange || !rows[0].weights ? null : scoreLegend(rows[0].weights),
      isAbsoluteChange ? null : el("p", {
        class: "table-caption",
        text: "Hover a row for its driver breakdown and what would change its score.",
      }),
    ]);
  }

  /** One "read this with care" panel: the scenario inputs used and what the data cannot say. */
  function limitsPanel(answer) {
    const groups = [
      ["assumption", "Assumptions", answer.assumptions],
      ["limit", "What the data can't tell you", answer.caveats],
    ].filter(([, , items]) => items && items.length);
    if (!groups.length) return null;
    return el("section", { class: "limits", "aria-label": "Assumptions and limitations" }, groups.map(([kind, title, items]) =>
      el("div", { class: `limits-row limits-${kind}` }, [
        el("h3", { text: title }),
        el("ul", {}, items.map((item) => el("li", { text: item }))),
      ])));
  }

  function sourceList(evidence) {
    if (!evidence || !evidence.length) return null;
    const links = evidence.map((item) => {
      const source = item.source;
      const rawUrl = source && source.url;
      // Only https links from the server are rendered as links (also guards stored transcripts).
      const url = typeof rawUrl === "string" && /^https:\/\//.test(rawUrl) ? rawUrl : null;
      const name = (source && source.label) || (typeof source === "string" ? source : item.metric);
      const label = item.period ? `${name} · ${item.period}` : name;
      return url
        ? el("a", { href: url, target: "_blank", rel: "noopener noreferrer", text: label })
        : el("span", { class: "source-tag", text: label });
    });
    return el("div", { class: "source-box" }, [el("span", { class: "source-label", text: "Data" }), ...links]);
  }

  /** Next questions that the planner is known to route, built from codes already in the answer. */
  function followUps(answer) {
    if (!["computed", "partial"].includes(answer.result_state)) return [];
    const rows = answer.ranking_rows || [];
    const codes = (answer.follow_up_context && answer.follow_up_context.airport_codes) || [];
    const unmet = (code) => `What is the unmet demand at ${code} and why?`;
    if (answer.answer_kind === "ranking" && rows.length) {
      const [top, second] = rows;
      return [
        second ? `Compare ${top.code} and ${second.code} passenger change` : null,
        unmet(top.code),
        `Would a new terminal at ${top.code} be profitable?`,
      ].filter(Boolean);
    }
    if (answer.answer_kind === "comparison" && codes.length === 2) return codes.map(unmet);
    if (answer.answer_kind === "demand" && answer.resolved_scope) {
      const code = answer.resolved_scope;
      const wantsRevenue = (answer.metrics || []).some((m) => m.name === "revenue_context");
      // The operational evidence a reviewer checks next: is the airport delayed, and how long-haul is it.
      return [
        wantsRevenue ? null : `Would a new terminal at ${code} be profitable?`,
        `How delayed was ${code} in 2025?`,
        `What share of ${code} departures go at least 3,000 miles?`,
      ].filter(Boolean);
    }
    if (answer.answer_kind === "metric" && (answer.metrics || []).some((m) => m.name === "longhaul_share")) {
      return ["What about 2,500 miles?", "What about 4,000 miles?"];
    }
    if (answer.answer_kind === "metric" && codes.length === 1
        && (answer.metrics || []).some((m) => m.name === "congestion")) {
      return [unmet(codes[0]), `What share of ${codes[0]} departures go at least 3,000 miles?`];
    }
    return [];
  }

  function followUpBar(answer, questions = followUps(answer), label = "Ask next") {
    if (!questions.length) return null;
    return el("footer", { class: "follow-ups" }, [
      el("span", { class: "follow-ups-label", text: label }),
      ...questions.map((question) => {
        const button = el("button", { type: "button", class: "follow-up-chip", text: question });
        button.addEventListener("click", () => ask(question));
        return button;
      }),
    ]);
  }

  function candidateList(candidates) {
    if (!candidates || !candidates.length) return null;
    const buttons = candidates.slice(0, 6).map((candidate) => {
      const label = `${candidate.name} (${candidate.code}${candidate.state ? ", " + candidate.state : ""})`;
      const button = el("button", { type: "button", class: "candidate-button", text: label });
      button.addEventListener("click", () => ask(label));
      return button;
    });
    return el("div", { class: "candidates", role: "group", "aria-label": "Possible matches" }, buttons);
  }

  const ANSWER_KINDS = {
    ranking: "Opportunity ranking", comparison: "Airport comparison", metric: "Airport metric",
    demand: "Demand outlook", clarification: "Clarification", refusal: "Outside scope", unsupported: "Not supported",
  };

  /** A passenger-change "ranking" is not an opportunity score; one row is not a ranking at all. */
  function kindLabel(answer) {
    const metric = ((answer.metrics || [])[0] || {}).name;
    if (answer.answer_kind === "ranking" && metric === "absolute_passenger_change") {
      return (answer.ranking_rows || []).length === 1 ? "Passenger change" : "Passenger change ranking";
    }
    return ANSWER_KINDS[answer.answer_kind] || "Answer";
  }

  /** "region scope: new england" -> "New England"; "airports: BOS, PVD" -> "BOS, PVD". */
  function scopeTitle(scope) {
    if (!scope) return null;
    const value = String(scope).replace(/^(region|state) scope:\s*/i, "").replace(/^airports:\s*/i, "");
    return value === value.toLowerCase() ? value.replace(/\b\w/g, (c) => c.toUpperCase()) : value;
  }

  function insightBlock(answer) {
    const insight = answer.insight;
    if (!insight) return el("p", { class: "answer-summary", text: answer.message });
    return el("div", { class: "insight" }, [
      el("p", { class: "insight-summary", text: insight.summary }),
      insight.takeaway ? el("aside", { class: "takeaway" }, [
        el("span", { class: "takeaway-label", text: "Key takeaway" }),
        el("p", { text: insight.takeaway }),
      ]) : null,
    ]);
  }

  // Replies that ask or decline rather than answer: no analysis was run, so they
  // read as one chat message, without a headline, data panels or an audit trail.
  const CONVERSATIONAL_KINDS = new Set(["clarification", "refusal", "unsupported"]);
  const REPHRASE_EXAMPLES = [
    "Which New England airports deserve a closer look for expansion?",
    "Compare PDX and SEA on passenger growth",
    "How much unserved demand is there at SFO, and why?",
  ];

  function renderReply(answer) {
    const hasCandidates = answer.candidates && answer.candidates.length;
    return el("article", { class: "answer-card reply-card" }, [
      el("div", { class: "reply-body" }, [
        el("div", { class: "avatar agent-avatar", "aria-hidden": "true", text: "✈" }),
        el("p", { class: "reply-text", text: answer.message }),
      ]),
      candidateList(answer.candidates),
      answer.answer_kind === "refusal" || hasCandidates ? null
        : (answer.suggested_questions || []).length
          ? followUpBar(answer, answer.suggested_questions, "Did you mean")
          : followUpBar(answer, REPHRASE_EXAMPLES, "Try asking"),
    ]);
  }

  function renderAnswer(answer, remember = true) {
    hideWelcome();
    if (CONVERSATIONAL_KINDS.has(answer.answer_kind)) {
      ui.conversation.appendChild(renderReply(answer));
      ui.conversation.scrollTop = ui.conversation.scrollHeight;
      if (remember) persistTranscript({ role: "assistant", answer });
      return;
    }
    const state = RESULT_STATES[answer.result_state] || { label: answer.result_state, tone: "unknown" };
    const hasRanking = answer.ranking_rows && answer.ranking_rows.length;
    // A ranking table already shows every figure, and a metric that was not
    // computed is explained in the summary and "What to verify next".
    const metrics = hasRanking ? [] : (answer.metrics || []).filter((m) => m.status === "computed");
    const periods = [...new Set((answer.evidence || []).map((item) => item.period).filter(Boolean))];
    const eyebrow = [kindLabel(answer), periods.length ? `Public data ${periods.join(", ")}` : null]
      .filter(Boolean).join(" · ");
    const title = (answer.insight && answer.insight.headline) || scopeTitle(answer.resolved_scope)
      || kindLabel(answer);

    const card = el("article", { class: "answer-card" }, [
      el("header", { class: "answer-top" }, [
        el("div", { class: "avatar agent-avatar", "aria-hidden": "true", text: "✈" }),
        el("div", { class: "answer-head" }, [
          el("p", { class: "answer-eyebrow" }, [
            el("span", { text: eyebrow }),
            // A fully computed answer needs no badge; anything weaker is flagged up front.
            answer.result_state !== "computed" ? el("span", { class: `state-pill tone-${state.tone}`, text: state.label }) : null,
          ]),
          el("h2", { text: title }),
        ]),
      ]),
      insightBlock(answer),
      hasRanking ? rankingTable(answer.ranking_rows) : null,
      metricGrid(metrics),
      candidateList(answer.candidates),
      limitsPanel(answer),
      sourceList(answer.evidence),
      followUpBar(answer),
    ]);
    ui.conversation.appendChild(card);
    ui.conversation.scrollTop = ui.conversation.scrollHeight;
    if (remember) persistTranscript({ role: "assistant", answer });
    if (remember) speakAnswer(answer);
  }

  function hideWelcome() {
    const welcome = ui.conversation.querySelector(".welcome");
    if (welcome) welcome.remove();
  }

  function addUserMessage(text, remember = true) {
    hideWelcome();
    ui.conversation.appendChild(el("div", { class: "user-message" }, [
      el("p", { text }),
      el("div", { class: "avatar user-avatar", "aria-hidden": "true", text: "You" }),
    ]));
    if (remember) persistTranscript({ role: "user", text });
  }

  function addNote(text, kind) {
    const note = el("p", { class: kind === "error" ? "conversation-note is-error" : "conversation-note", text });
    ui.conversation.appendChild(note);
    ui.conversation.scrollTop = ui.conversation.scrollHeight;
    return note;
  }

  // ---------------------------------------------------------------------------
  // 5. Airport explorer
  // ---------------------------------------------------------------------------

  const EXPLORER_QUESTIONS = [
    ["Passenger trend", (code) => `How did departing passenger volume at ${code} change since 2023?`],
    ["Demand pressure", (code) => `How much unserved demand is there at ${code}, and why?`],
    ["Profitability boundary", (code) => `Would a new terminal at ${code} be profitable?`],
  ];

  function setPreviewMessage(kind, text) {
    ui.airportPreview.className = `airport-preview is-${kind}`;
    ui.airportPreview.textContent = text;
  }

  /** Clickable years: the selected year drives the headline figure and its change vs the prior year. */
  function yearSelector(series) {
    const peak = Math.max(...series.map((row) => row.passengers), 1);
    const figure = el("strong");
    const caption = el("span");
    const chip = el("em");
    const bars = series.map((row) => {
      const button = el("button", { type: "button", class: "year-bar", "aria-label": `${row.year}: ${count(row.passengers)} departing passengers` }, [
        el("small", { class: "bar-value", text: compact(row.passengers) }),
        el("span", { class: "bar-track" }, [el("span", { class: "bar", style: `height:${Math.max(4, Math.round(row.passengers / peak * 100))}%` })]),
        el("small", { class: "bar-year", text: String(row.year) }),
      ]);
      button.addEventListener("click", () => select(row, button));
      return button;
    });
    function select(row, button) {
      bars.forEach((bar) => bar.setAttribute("aria-pressed", String(bar === button)));
      figure.textContent = compact(row.passengers);
      caption.textContent = `departing passengers · ${row.year}`;
      const change = row.change_pct;
      chip.hidden = change === null || change === undefined;
      if (!chip.hidden) {
        chip.className = change >= 0 ? "is-up" : "is-down";
        chip.textContent = `${signedPct(change)} vs ${row.year - 1}`;
      }
    }
    select(series[series.length - 1], bars[bars.length - 1]);
    return el("div", { class: "traffic-hero" }, [
      el("div", { class: "hero-figure", "aria-live": "polite" }, [figure, caption, chip]),
      el("div", { class: "traffic-bars", role: "group", "aria-label": "Select a year" }, bars),
    ]);
  }

  /** A titled list of label/value rows; rows without a value are dropped, an empty list renders nothing. */
  function detailRows(title, rows) {
    const present = rows.filter(([, value]) => value !== null && value !== undefined && value !== "");
    if (!present.length) return null;
    return el("section", { class: "preview-section" }, [
      el("h4", { text: title }),
      el("dl", {}, present.flatMap(([term, value]) => [el("dt", { text: term }), el("dd", { text: String(value) })])),
    ]);
  }

  function renderAirportPreview(data) {
    const airport = data.airport;
    const traffic = [...(data.traffic || [])].sort((a, b) => a.year - b.year);
    const capabilities = Object.entries(data.capabilities || {});

    const h = data.highlights || {};
    // Year series from the server; an older server without highlights falls back to raw traffic rows.
    const series = h.yearly || traffic.map((row) => ({ year: row.year, passengers: row.passengers }));
    const trafficBlock = series.length ? yearSelector(series) : el("p", { class: "preview-missing", text: "No BTS passenger traffic is installed for this airport." });

    const usd = (v) => `$${compact(v)}`;
    const kpis = [
      ["Growth", h.two_year_cagr_pct, (v) => `${signedPct(v)}`, "per year, 2023–25"],
      ["Seats filled", h.seat_utilization_pct, (v) => `${v}%`, "load factor"],
      ["Delayed", h.delayed_arrivals_pct, (v) => `${Number(v).toFixed(1)}%`, h.delay_minutes_per_arrival ? `${Math.round(h.delay_minutes_per_arrival)} min avg` : "arrivals"],
      ["International", h.international_departure_share_pct, (v) => `${v}%`, "of departures"],
      ["Destinations", h.nonstop_destinations, String, h.top_route ? `top: ${h.top_route.destination}` : "nonstop"],
      ["Long-haul", h.longhaul_share_pct, (v) => `${Number(v).toFixed(1)}%`, `≥ ${count(h.longhaul_threshold_miles || 3000)} mi`],
    ].filter(([, value]) => value !== null && value !== undefined);
    const kpiGrid = kpis.length ? el("div", { class: "kpi-grid" }, kpis.map(([label, value, fmt, note]) => el("div", { class: "kpi" }, [
      el("small", { text: label }), el("strong", { text: fmt(value) }), note ? el("span", { text: note }) : null,
    ]))) : null;

    const forecastBlock = data.forecast
      ? el("div", { class: "forecast-line" }, [
        el("span", { text: "FAA TAF FY25→FY30" }),
        el("strong", { text: signedPct(data.forecast.change_pct) }),
      ])
      : el("p", { class: "preview-missing", text: "FAA TAF outlook not installed for this airport." });

    const profile = detailRows("Airport profile", [
      ["ICAO", airport.icao],
      ["FAA hub size", h.hub_size],
      ["Ownership", h.ownership],
      ["Service level", h.service_level],
      ["FAA 5-yr dev. estimate", h.development_estimate_usd != null ? usd(h.development_estimate_usd) : null],
      ["Based aircraft", h.based_aircraft !== null && h.based_aircraft !== undefined ? count(h.based_aircraft) : null],
      ["International passengers", h.international_passenger_share_pct != null ? pct1(h.international_passenger_share_pct) : null],
    ]);
    const routes = h.top_routes ? detailRows(`Top routes ${h.year || ""}`.trim(),
      h.top_routes.map((route, index) => [`${index + 1}. ${airport.code} → ${route.destination}`, `${compact(route.passengers)} pax`])) : null;
    const operations = detailRows(`Operations ${h.delay_period || ""}`.trim(), [
      ["On-time arrivals", h.on_time_pct != null ? pct1(h.on_time_pct) : null],
      ["Cancelled", h.cancelled_pct != null ? pct1(h.cancelled_pct) : null],
      ["Delay minutes: airspace/NAS", h.nas_delay_share_pct != null ? pct1(h.nas_delay_share_pct) : null],
      ["Delay minutes: weather", h.weather_delay_share_pct != null ? pct1(h.weather_delay_share_pct) : null],
    ]);

    // The figures above already show what is installed; only gaps need a line.
    const missing = capabilities.filter(([, item]) => !item.available);
    const capabilityList = missing.length ? el("p", { class: "preview-gaps" }, [
      el("span", { text: "Not available: " }),
      el("span", { text: missing.map(([name]) => humanize(name)).join(", ") }),
    ]) : null;

    const askButtons = el("div", { class: "preview-actions", role: "group", "aria-label": `Ask about ${airport.code}` },
      EXPLORER_QUESTIONS.map(([label, question]) => {
        const button = el("button", { type: "button", text: label });
        button.addEventListener("click", () => ask(question(airport.code)));
        return button;
      }));

    ui.airportPreview.className = "airport-preview";
    ui.airportPreview.replaceChildren(...[
      el("div", { class: "preview-title" }, [
        el("div", {}, [
          el("strong", { text: `${airport.code} · ${airport.city || airport.name}` }),
          el("small", { text: airport.name }),
        ]),
        el("span", { class: "preview-state", text: [h.hub_size, (airport.region || "US").replace("US-", "")].filter(Boolean).join(" · ") }),
      ]),
      trafficBlock,
      kpiGrid,
      forecastBlock,
      profile,
      routes,
      operations,
      capabilityList,
      el("p", { class: "preview-actions-label", text: `Ask about ${airport.code}` }),
      askButtons,
    ].filter(Boolean));
  }

  async function selectAirport(code) {
    closeSearchResults();
    setPreviewMessage("loading", `Loading ${code}…`);
    try {
      const response = await fetch(`/api/airports/${encodeURIComponent(code)}/preview`);
      if (!response.ok) throw new Error("preview unavailable");
      renderAirportPreview(await response.json());
    } catch (_err) {
      setPreviewMessage("empty", `No preview available for ${code}. Only US airports in the registry can be previewed.`);
    }
  }

  function closeSearchResults() {
    ui.airportResults.replaceChildren();
  }

  let searchTimer;
  let latestSearch = 0;

  function runAirportSearch() {
    clearTimeout(searchTimer);
    const query = ui.airportSearch.value.trim();
    if (query.length < 2) { closeSearchResults(); return; }
    searchTimer = setTimeout(async () => {
      const searchId = ++latestSearch;
      try {
        const response = await fetch(`/api/airports?query=${encodeURIComponent(query)}`);
        if (!response.ok) throw new Error(`search ${response.status}`);
        const data = await response.json();
        if (searchId !== latestSearch) return; // a newer query is already in flight
        const buttons = (data.airports || []).slice(0, 6).map((airport) => {
          const button = el("button", { type: "button" }, [
            el("strong", { text: airport.code }),
            el("span", { text: `${airport.city || airport.name} · ${airport.country}` }),
          ]);
          button.addEventListener("click", () => {
            ui.airportSearch.value = airport.code;
            selectAirport(airport.code);
          });
          return button;
        });
        ui.airportResults.replaceChildren(...(buttons.length ? buttons : [el("p", { text: "No matching airport" })]));
      } catch (_err) {
        if (searchId === latestSearch) ui.airportResults.replaceChildren(el("p", { text: "Search is unavailable right now." }));
      }
    }, 220);
  }

  /** Arrow keys move between the search box and its result buttons; Escape closes the list. */
  function handleSearchKeys(event) {
    const options = [...ui.airportResults.querySelectorAll("button")];
    const index = options.indexOf(document.activeElement);
    if (event.key === "Escape") {
      closeSearchResults();
      ui.airportSearch.focus();
    } else if (event.key === "ArrowDown" && options.length) {
      event.preventDefault();
      options[Math.min(index + 1, options.length - 1)].focus();
    } else if (event.key === "ArrowUp" && index >= 0) {
      event.preventDefault();
      if (index === 0) ui.airportSearch.focus(); else options[index - 1].focus();
    }
  }

  // ---------------------------------------------------------------------------
  // 6. Chat flow, event wiring and start-up
  // ---------------------------------------------------------------------------

  function updateTurnCount() {
    ui.turnCount.textContent = `${plural(turns, "turn")} this session`;
  }

  // ---------------------------------------------------------------------------
  // Voice: browser speech recognition fills and sends the question; an answer to
  // a spoken question is read back (summary and takeaway only, never the tables).
  // Uses the browser's Web Speech API: no dependency, hidden where unsupported.
  // ---------------------------------------------------------------------------

  const SpeechRecognition = window.SpeechRecognition || window.webkitSpeechRecognition;
  let recognition = null;
  let replyAloud = false;

  function setVoiceState(listening) {
    ui.micButton.classList.toggle("is-listening", listening);
    ui.micButton.setAttribute("aria-pressed", String(listening));
    ui.micButton.title = listening ? "Listening… click to stop" : "Ask by voice";
    ui.input.placeholder = listening ? "Listening…" : "Ask about a US airport, city, state or region…";
  }

  function setUpVoice() {
    if (!SpeechRecognition) return;
    ui.micButton.hidden = false;
    ui.micButton.addEventListener("click", () => {
      if (recognition) { recognition.stop(); return; }
      if (window.speechSynthesis) window.speechSynthesis.cancel();
      recognition = new SpeechRecognition();
      recognition.lang = "en-US";
      recognition.interimResults = true;
      recognition.maxAlternatives = 1;
      let finalText = "";
      recognition.onresult = (event) => {
        const transcript = [...event.results].map((result) => result[0].transcript).join(" ").trim();
        ui.input.value = transcript;
        if (event.results[event.results.length - 1].isFinal) finalText = transcript;
      };
      recognition.onerror = (event) => {
        if (event.error === "not-allowed" || event.error === "service-not-allowed") {
          addNote("Microphone access was blocked. Allow it in the browser to ask by voice.", "error");
        } else if (event.error !== "no-speech" && event.error !== "aborted") {
          addNote(`Voice input stopped (${event.error}). You can type the question instead.`, "error");
        }
      };
      recognition.onend = () => {
        recognition = null;
        setVoiceState(false);
        if (finalText) {
          replyAloud = true;
          sendMessage();
        }
      };
      setVoiceState(true);
      recognition.start();
    });
  }

  function speakAnswer(answer) {
    if (!replyAloud || !window.speechSynthesis) return;
    replyAloud = false;
    const insight = answer.insight;
    const text = insight ? [insight.summary, insight.takeaway].filter(Boolean).join(" ") : answer.message;
    if (!text) return;
    const utterance = new SpeechSynthesisUtterance(text);
    utterance.lang = "en-US";
    window.speechSynthesis.cancel();
    window.speechSynthesis.speak(utterance);
  }

  function ask(question) {
    replyAloud = false;
    ui.input.value = question;
    sendMessage();
  }

  async function sendMessage() {
    const text = ui.input.value.trim();
    if (!text || ui.sendButton.disabled) return;
    ui.input.value = "";
    ui.sendButton.disabled = true;
    ui.conversation.setAttribute("aria-busy", "true");
    addUserMessage(text);
    const pending = addNote("Planning and checking source coverage…");
    turns += 1;
    updateTurnCount();
    try {
      const response = await fetch("/api/chat", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ session_id: sessionId, message: text }),
      });
      const data = await response.json().catch(() => ({}));
      pending.remove();
      if (!response.ok) {
        addNote(`Request error: ${data.detail || data.error || `HTTP ${response.status}`}`, "error");
        return;
      }
      if (!data.result_state) {
        addNote("The server returned an answer this page cannot read.", "error");
        return;
      }
      renderAnswer(data);
      sessionStorage.setItem(LAST_ACTIVE_KEY, String(Date.now()));
    } catch (_err) {
      pending.remove();
      addNote("Network error reaching the local server.", "error");
    } finally {
      ui.sendButton.disabled = false;
      ui.conversation.removeAttribute("aria-busy");
    }
  }

  async function startNewAnalysis() {
    try {
      await fetch("/api/session/clear", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ session_id: sessionId }),
      });
    } catch (_err) {
      // The server context expires on its own; the tab still starts fresh.
    } finally {
      forgetSession();
      window.location.reload();
    }
  }

  function restoreTranscript() {
    if (!transcript.length) return;
    try {
      [...transcript].forEach((entry) => {
        if (entry.role === "user" && typeof entry.text === "string") addUserMessage(entry.text, false);
        if (entry.role === "assistant" && entry.answer) renderAnswer(entry.answer, false);
      });
    } catch (_err) {
      // A stale or foreign stored transcript must never break page load.
      sessionStorage.removeItem(TRANSCRIPT_KEY);
      transcript = [];
      turns = 0;
      ui.conversation.querySelectorAll(".user-message, .answer-card").forEach((node) => node.remove());
    }
    const lastActive = Number(sessionStorage.getItem(LAST_ACTIVE_KEY) || 0);
    if (transcript.length && lastActive && Date.now() - lastActive > SERVER_CONTEXT_TTL_MS) {
      addNote("This transcript is older than the server's 24-hour follow-up window, so the next question starts without earlier context.", "error");
    }
  }

  setUpVoice();
  // Typed questions get a silent answer; only a spoken question is read back.
  const sendTyped = () => { replyAloud = false; sendMessage(); };
  ui.sendButton.addEventListener("click", sendTyped);
  ui.input.addEventListener("keydown", (event) => {
    if (event.key === "Enter" && !event.shiftKey && !event.isComposing) {
      event.preventDefault();
      sendTyped();
    }
  });
  document.querySelectorAll("[data-q]").forEach((button) => {
    button.addEventListener("click", () => ask(button.dataset.q));
  });
  document.querySelectorAll("[data-airport]").forEach((button) => {
    button.addEventListener("click", () => selectAirport(button.dataset.airport));
  });
  ui.airportSearch.addEventListener("input", runAirportSearch);
  ui.airportSearch.addEventListener("keydown", handleSearchKeys);
  ui.airportResults.addEventListener("keydown", handleSearchKeys);
  ui.clearChat.addEventListener("click", startNewAnalysis);

  restoreTranscript();
  updateTurnCount();
  loadHealth();
  selectAirport("SFO");
})();
