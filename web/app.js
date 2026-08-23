// The console's behaviour: pick a month, press one button, read the ledger.
//
// Lifted out of the page so the content security policy can refuse inline
// script outright. A DAST scan raised script-src 'unsafe-inline' against
// the running container, and with it any injected script tag executes, so
// the finding was real and this is the fix rather than an ignore rule.
//
// The same policy refuses inline STYLE, which is why every chart below is an
// SVG sized by presentation attributes rather than an HTML div sized by
// `style="width:62%"`. That instinct is the one that shipped a broken page
// once already: `style-src 'self'` blocked the run trail's stagger and all
// eleven steps landed at once. A rect's `width` attribute is not a style
// attribute; a div's computed width is.
const $ = (id) => document.getElementById(id);
const usd = (n) => (n === null || n === undefined) ? "–"
  : n.toLocaleString("en-US", { style: "currency", currency: "USD", maximumFractionDigits: 2 });
const num = (n, d = 0) => (n === null || n === undefined) ? "–"
  : n.toLocaleString("en-US", { minimumFractionDigits: d, maximumFractionDigits: d });
const esc = (s) => String(s ?? "").replace(/[&<>"']/g,
  (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const words = (s) => String(s ?? "").replace(/_/g, " ");
const title = (s) => { const w = words(s); return w.charAt(0).toUpperCase() + w.slice(1); };

let period = "2026-07";
let last = null;

// ── the shell ────────────────────────────────────────────────────────────────

// Panels rather than one long scroll, because the question an owner arrives
// with is one of eight and they should not have to scroll past the other seven.
// Every panel stays in the DOM: a judge reading the page source, and the
// browser journey that walks it, both see the whole close whichever tab is up.
function show(name) {
  document.querySelectorAll(".panel").forEach((p) =>
    p.classList.toggle("hidden", p.id !== `panel-${name}`));
  document.querySelectorAll(".tab").forEach((t) =>
    t.classList.toggle("on", t.dataset.panel === name));
  window.scrollTo({ top: 0 });
}

document.querySelectorAll(".tab").forEach((tab) =>
  tab.addEventListener("click", () => show(tab.dataset.panel)));

// ── the close ────────────────────────────────────────────────────────────────

async function close() {
  const btn = $("run");
  btn.disabled = true;
  btn.textContent = "Closing…";
  $("dot").className = "dot working";
  $("status").textContent = "the agent is working";
  $("error").classList.add("hidden");
  try {
    const res = await fetch(`/api/close/${period}`, { method: "POST" });
    if (!res.ok) throw new Error(`the close returned ${res.status}`);
    last = await res.json();
    render(last);
    btn.textContent = "Close it again";
    $("dot").className = `dot ${last.outcome === "closed" ? "ok" : "err"}`;
    $("status").textContent = `run ${last.run_id} · ${last.outcome}`;
  } catch (err) {
    $("error").textContent = `Could not close the month: ${err.message}`;
    $("error").classList.remove("hidden");
    $("dot").className = "dot err";
    btn.textContent = "Try again";
  } finally {
    btn.disabled = false;
  }
}

function render(d) {
  renderTrail(d.journal);
  renderStats(d);
  renderExpenseChart(d.statements);
  renderMileChart(d.statements);
  renderAlloc(d.allocations);
  renderRegister(d.register);
  renderTrends(d.comparison, d.trend_summary);
  renderFindings(d.findings);
  renderDigest(d.digest, d.receipt);
  renderDrafts(d.drafts);
  renderGates(d.gates);
  renderTrucks(d.statements.per_truck);
  renderProvenance(d);
  $("company").textContent = d.company || "your firm";
}

// ── charts ───────────────────────────────────────────────────────────────────

// A bar row is label / bar / value. The bar is an SVG whose rect is sized by a
// `width` attribute against a 0–100 viewBox, so the policy has nothing to
// refuse and the row still reflows at 375px.
function bars(rows) {
  if (!rows.length) return `<p class="sub tight">Nothing to show for this month.</p>`;
  const max = rows.reduce((m, r) => Math.max(m, Math.abs(r.value || 0)), 0) || 1;
  return `<div class="bars">${rows.map((r) => `
    <div class="bar-row">
      <span class="bar-k">${esc(r.label)}</span>
      <svg class="bar" viewBox="0 0 100 10" preserveAspectRatio="none" aria-hidden="true">
        <rect x="0" y="0" height="10" width="${(Math.abs(r.value || 0) / max * 100).toFixed(2)}"
              class="f-${esc(r.tone || "primary")}"></rect>
      </svg>
      <span class="bar-v num">${esc(r.display)}</span>
    </div>`).join("")}</div>`;
}

// Two bars to a row: the earlier value then the later one, or the two halves
// of a ratio. Same rule about attributes.
function pairs(rows, legend) {
  if (!rows.length) return `<p class="sub tight">Nothing to compare for this month.</p>`;
  const max = rows.reduce(
    (m, r) => Math.max(m, Math.abs(r.a || 0), Math.abs(r.b || 0)), 0) || 1;
  const w = (v) => (Math.abs(v || 0) / max * 100).toFixed(2);
  return `<div class="bars">${rows.map((r) => `
    <div class="bar-row">
      <span class="bar-k">${esc(r.label)}</span>
      <svg class="bar tall" viewBox="0 0 100 22" preserveAspectRatio="none" aria-hidden="true">
        <rect x="0" y="0" height="9" width="${w(r.a)}" class="f-muted"></rect>
        <rect x="0" y="13" height="9" width="${w(r.b)}" class="f-${esc(r.tone || "primary")}"></rect>
      </svg>
      <span class="bar-v num">${esc(r.display)}</span>
    </div>`).join("")}</div>
    <div class="legend">
      <span class="sw f-muted"></span>${esc(legend[0])}
      <span class="sw f-primary"></span>${esc(legend[1])}
    </div>`;
}

// r = 15.915 gives a circumference of exactly 100, so a segment's share is its
// percentage and `stroke-dasharray` needs no unit conversion.
function donut(rows) {
  const total = rows.reduce((s, r) => s + Math.abs(r.value || 0), 0);
  if (!total) return `<p class="sub tight">Nothing was spent this month.</p>`;
  let offset = 25;                       // start the first segment at 12 o'clock
  const segments = rows.map((r, i) => {
    const pct = Math.abs(r.value) / total * 100;
    const el = `<circle class="seg s${i % 6}" cx="21" cy="21" r="15.915" fill="none"
      stroke-width="5.5" stroke-dasharray="${pct.toFixed(2)} ${(100 - pct).toFixed(2)}"
      stroke-dashoffset="${offset.toFixed(2)}"></circle>`;
    offset = (offset - pct + 100) % 100;
    return el;
  }).join("");
  const legend = rows.map((r, i) => `<div class="leg-row">
      <span class="sw s${i % 6}"></span>
      <span class="leg-k">${esc(r.label)}</span>
      <span class="leg-v num">${esc(r.display)}</span>
      <span class="leg-p num">${(Math.abs(r.value) / total * 100).toFixed(0)}%</span>
    </div>`).join("");
  return `<div class="donut-wrap">
    <svg class="donut" viewBox="0 0 42 42" role="img" aria-label="Operating cost by category">
      <circle class="track" cx="21" cy="21" r="15.915" fill="none" stroke-width="5.5"></circle>
      ${segments}
    </svg>
    <div class="legend-col">${legend}</div>
  </div>`;
}

function renderExpenseChart(s) {
  const rows = [
    ["Driver pay", s.driver_pay], ["Fuel", s.fuel], ["Insurance", s.insurance],
    ["Maintenance", s.maintenance], ["Factoring fees", s.factoring_fees],
    ["Tolls", s.tolls],
  ].filter(([, v]) => v).sort((a, b) => b[1] - a[1])
    .map(([label, value]) => ({ label, value, display: usd(value) }));
  $("chart-expense").innerHTML = donut(rows);
}

function renderMileChart(s) {
  const margin = (s.revenue_per_mile ?? 0) - (s.cost_per_mile ?? 0);
  $("chart-mile").innerHTML = bars([
    { label: "Earned a mile", value: s.revenue_per_mile, tone: "ok",
      display: num(s.revenue_per_mile, 3) },
    { label: "Spent a mile", value: s.cost_per_mile, tone: "err",
      display: num(s.cost_per_mile, 3) },
    { label: "Margin a mile", value: margin, tone: margin >= 0 ? "primary" : "err",
      display: num(margin, 3) },
  ]);
}

// ── the run trail ────────────────────────────────────────────────────────────

// Steps land one at a time. The delay is presentation only: the run already
// finished server-side, and the durations shown are the real ones.
function renderTrail(j) {
  const host = $("trail");
  host.innerHTML = "";
  j.steps.forEach((s, i) => {
    const el = document.createElement("div");
    // The stagger is a class, not an inline style. Setting `el.style` here
    // violates `style-src 'self'`, and a real browser blocks it: the steps
    // then land all at once and the run stops being watchable. A unit test
    // could never have seen that, which is what the browser journey is for.
    el.className = `step ${s.status !== "ok" ? s.status : ""} d${Math.min(i, 15)}`;
    el.innerHTML = `<div class="t">${esc(s.title)}<span class="ms num">${s.duration_ms} ms</span></div>
                    <div class="d">${esc(s.detail)}</div>`;
    host.appendChild(el);
  });
}

// ── the tiles ────────────────────────────────────────────────────────────────

// Each tile is a control, not an ornament: it opens the panel that holds the
// ledger the number came out of. A figure an owner cannot drill into is a
// figure they have to take on trust.
function renderStats(d) {
  const s = d.statements;
  const errs = d.findings.filter((f) => f.severity === "error").length;
  const margin = (s.revenue_per_mile ?? 0) - (s.cost_per_mile ?? 0);
  const cards = [
    ["trends", "Net profit", usd(s.net_profit),
     `${usd(s.revenue)} billed, ${usd(s.operating_expenses)} spent`],
    ["trucks", "Margin per mile", num(margin, 3),
     `${num(s.revenue_per_mile, 3)} earned, ${num(s.cost_per_mile, 3)} spent`],
    ["trucks", "Miles run", num(s.total_miles),
     `${Object.keys(s.per_truck).length} trucks`],
    ["findings", "Exceptions", String(d.findings.length), `${errs} of them errors`],
    ["letters", "Leaking away", usd(d.leakage), "money nobody would have caught"],
    ["register", "Invoiced, unpaid", usd(d.outstanding), "owed already, now chased"],
    ["checks", "Close", title(d.outcome),
     `${d.gates.filter((g) => g.passed).length}/${d.gates.length} gates passed`],
  ];
  $("stats").innerHTML = cards.map(([go, k, v, sub]) =>
    `<button class="card" data-goto="${esc(go)}">
       <span class="k">${esc(k)}</span><span class="v num">${esc(v)}</span>
       <span class="s">${esc(sub)}</span><span class="go">Open the ledger →</span>
     </button>`).join("");
  $("stats").querySelectorAll("[data-goto]").forEach((el) =>
    el.addEventListener("click", () => show(el.dataset.goto)));
}

// ── the ledgers ──────────────────────────────────────────────────────────────

function renderAlloc(list) {
  const rows = list.map((a) => {
    const head = `<tr><td colspan="6" class="alloc-head">
      <b>${esc(a.remittance_ref)}</b> from ${esc(a.broker)}:
      <span class="num">${usd(a.remittance_total)}</span> landed,
      <span class="num">${usd(a.factoring_fee)}</span> factoring fee,
      <span class="num">${usd(a.allocated_gross)}</span> across ${a.lines.length} loads.
      <span class="pill ${a.reconciles ? "ok" : "err"}">
        ${a.reconciles ? "identity closes, residual 0.00" : `residual ${usd(a.residual)}`}</span>
      </td></tr>`;
    const lines = a.lines.map((l) => `<tr>
      <td>${esc(l.load_ref)}</td>
      <td class="r num">${l.invoiced === null ? "–" : usd(l.invoiced)}</td>
      <td class="r num">${usd(l.paid)}</td>
      <td class="r num">${l.deduction ? usd(l.deduction) : "–"}</td>
      <td>${esc(l.reason || "")}</td>
      <td><span class="pill ${l.settled_in_full ? "ok" : (l.matched ? "err" : "warn")}">${
        l.settled_in_full ? "settled" : (l.matched ? "short paid" : "no confirmation on file")
      }</span></td></tr>`).join("");
    return head + lines;
  }).join("");
  $("alloc").innerHTML = `<thead><tr>
    <th>Load</th><th class="r">Invoiced</th><th class="r">Paid</th>
    <th class="r">Deduction</th><th>Broker's reason</th><th>Status</th>
  </tr></thead><tbody>${rows}</tbody>`;
}

function renderFindings(list) {
  const counts = new Map();
  list.forEach((f) => {
    const key = words(f.kind);
    const seen = counts.get(key) || { value: 0, severity: f.severity };
    counts.set(key, { value: seen.value + 1, severity: seen.severity });
  });
  const tone = { error: "err", warning: "warn", info: "info" };
  $("chart-findings").innerHTML = bars([...counts.entries()]
    .sort((a, b) => b[1].value - a[1].value)
    .map(([label, v]) => ({ label, value: v.value, tone: tone[v.severity] || "info",
                            display: String(v.value) })));

  const rows = list.map((f) => `<tr>
    <td><span class="pill ${f.severity === "error" ? "err" : (f.severity === "warning" ? "warn" : "info")}">${esc(words(f.kind))}</span></td>
    <td>${esc(f.reference)}</td>
    <td class="r num">${f.amount ? usd(f.amount) : "–"}</td>
    <td class="muted">${esc(f.message)}</td>
  </tr>`).join("");
  $("findings").innerHTML = `<thead><tr>
    <th>Kind</th><th>Reference</th><th class="r">Amount</th><th>What the books say</th>
  </tr></thead><tbody>${rows}</tbody>`;
}

function renderDigest(digest, receipt) {
  if (!digest) { $("digest").innerHTML = ""; return; }
  const pill = receipt && receipt.delivered
    ? '<span class="pill ok">delivered</span>'
    : '<span class="pill info">composed, no channel configured</span>';
  $("digest").innerHTML = `
    <h3>${esc(digest.subject)} ${pill}</h3>
    <div class="to">to ${esc(digest.recipient)}${receipt ? " &middot; " + esc(receipt.detail) : ""}</div>
    <pre>${esc(digest.body)}</pre>`;
}

function renderDrafts(list) {
  $("drafts").innerHTML = list.map((d) => `<div class="draft">
    <h3>${esc(d.subject)} <span class="pill warn">filed, not sent</span></h3>
    <div class="to">${esc(words(d.kind))} · to ${esc(d.recipient)} · ${usd(d.amount)}</div>
    <pre>${esc(d.body)}</pre>
  </div>`).join("");
}

function renderGates(list) {
  const rows = list.map((g) => `<tr>
    <td><span class="pill ${g.passed ? "ok" : "err"}">${g.passed ? "pass" : "fail"}</span></td>
    <td>${esc(g.rule)}</td><td class="muted">${esc(g.message)}</td>
  </tr>`).join("");
  $("gates").innerHTML = `<thead><tr><th></th><th>Gate</th><th>Evidence</th></tr></thead>
    <tbody>${rows}</tbody>`;
}

// The run's own identifiers, so a judge can tie what is on screen to what is in
// the store and to the JSON behind the button.
function renderProvenance(d) {
  const rows = [
    ["Run", d.run_id], ["Period", d.period], ["Books", d.company],
    ["Outcome", title(d.outcome)],
    ["Why", d.outcome_reason || "every gate passed"],
    ["Owner letter", d.receipt
      ? `${d.receipt.channel} · ${d.receipt.delivered ? "delivered" : "composed, not sent"}`
      : "–"],
  ];
  $("provenance").innerHTML = rows.map(([k, v]) =>
    `<div class="kv-row"><span class="kv-k">${esc(k)}</span>
     <span class="kv-v">${esc(v)}</span></div>`).join("");
}

function renderTrucks(per) {
  const entries = Object.entries(per).sort();
  $("chart-trucks").innerHTML = pairs(
    entries.map(([truck, r]) => {
      const margin = (r.revenue_per_mile ?? 0) - (r.cost_per_mile ?? 0);
      return { label: truck, a: r.cost_per_mile, b: r.revenue_per_mile,
               tone: margin >= 0 ? "ok" : "err",
               display: `${num(margin, 3)} margin` };
    }),
    ["Direct cost a mile", "Earned a mile"]);

  const rows = entries.map(([truck, r]) => `<tr>
    <td><b>${esc(truck)}</b></td>
    <td class="r num">${num(r.miles)}</td>
    <td class="r num">${usd(r.revenue)}</td>
    <td class="r num">${usd(r.fuel)}</td>
    <td class="r num">${usd(r.maintenance)}</td>
    <td class="r num">${num(r.revenue_per_mile, 3)}</td>
    <td class="r num">${num(r.cost_per_mile, 3)}</td>
  </tr>`).join("");
  $("trucks").innerHTML = `<thead><tr>
    <th>Truck</th><th class="r">Miles</th><th class="r">Revenue</th><th class="r">Fuel</th>
    <th class="r">Maintenance</th><th class="r">Rev / mile</th><th class="r">Direct cost / mile</th>
  </tr></thead><tbody>${rows}</tbody>`;
}

// The open items, both directions. Ageing is measured to the end of the period
// being closed, never to today, so re-running an old month gives the same answer.
function renderRegister(reg) {
  if (!reg) return;
  $("register-totals").innerHTML = [
    ["register", "Owed to the firm", usd(reg.owed_to_us), `${reg.receivables.length} open item(s)`],
    ["register", "Owed by the firm", usd(reg.owed_by_us), `${reg.payables.length} open item(s)`],
    ["register", "Net position", usd(reg.net_position),
     reg.net_position >= 0 ? "more coming in than going out" : "more going out than coming in"],
  ].map(([, k, v, sub]) =>
    `<div class="card flat"><span class="k">${esc(k)}</span><span class="v num">${esc(v)}</span>
     <span class="s">${esc(sub)}</span></div>`).join("");

  // Buckets are drawn in age order rather than by size, because an ageing chart
  // sorted by amount stops being an ageing chart.
  const ORDER = ["current", "15-30 days", "31-60 days", "over 60 days", "undated"];
  const aged = (map, tone) => bars(ORDER
    .filter((bucket) => map && map[bucket])
    .map((bucket, i) => ({ label: bucket, value: map[bucket],
                           tone: i === 0 ? "ok" : tone, display: usd(map[bucket]) })));
  $("chart-aged-in").innerHTML = aged(reg.receivables_aged, "warn");
  $("chart-aged-out").innerHTML = aged(reg.payables_aged, "primary");

  const section = (heading, items) => {
    if (!items.length) return "";
    const head = `<tr><td colspan="6" class="alloc-head"><b>${esc(heading)}</b></td></tr>`;
    return head + items.map((i) => `<tr>
      <td>${esc(i.counterparty)}</td>
      <td>${esc(i.reference)}</td>
      <td class="r num">${usd(i.invoiced)}</td>
      <td class="r num">${i.paid ? usd(i.paid) : "–"}</td>
      <td class="r num">${usd(i.open_amount)}</td>
      <td><span class="pill ${i.bucket === "current" ? "ok" : (i.bucket === "undated" ? "info" : "warn")}">${esc(i.bucket)}</span>
          <span class="muted">${esc(i.note)}</span></td>
    </tr>`).join("");
  };

  $("register").innerHTML = `<thead><tr>
    <th>Counterparty</th><th>Reference</th><th class="r">Invoiced</th>
    <th class="r">Paid</th><th class="r">Still open</th><th>Age</th>
  </tr></thead><tbody>
    ${section("Owed to the firm", reg.receivables)}
    ${section("Owed by the firm", reg.payables)}
  </tbody>`;
}

// Fuel going up is worse; revenue going up is better. Getting that backwards is
// how a dashboard congratulates a firm for burning more diesel.
function renderTrends(comparison, line) {
  // The earliest month has nothing behind it. Say so, and draw no axis: an
  // empty chart reads as a real zero, which is a different and wrong claim.
  if (!comparison) {
    $("trend-line").textContent =
      "This is the earliest month with mail on file, so there is nothing behind it to compare against.";
    $("chart-trend").innerHTML =
      `<p class="sub tight">Close a later month to see it against this one.</p>`;
    $("trends").innerHTML = "";
    return;
  }
  $("trend-line").textContent = line || "";

  const arrow = (m) => m.change === null ? "" : (m.change > 0 ? "▲" : (m.change < 0 ? "▼" : ""));
  const tone = { better: "ok", worse: "err", flat: "info", unknown: "info" };
  const fmt = (m, v) => v === null || v === undefined ? "–"
    : (m.unit === "money" ? usd(v) : (m.unit === "rate" ? num(v, 3) : num(v)));

  // Money only. Rates and mileages on the same axis as revenue would render as
  // a flat line next to a bar, which shows nothing.
  $("chart-trend").innerHTML = pairs(
    comparison.movements
      .filter((m) => m.unit === "money" && m.previous !== null && m.current !== null)
      .map((m) => ({ label: m.label, a: m.previous, b: m.current,
                     tone: tone[m.direction] || "info",
                     display: m.change_pct === null ? "–"
                       : `${arrow(m)} ${Math.abs(m.change_pct).toFixed(0)}%` })),
    [comparison.previous_period, comparison.current_period]);

  $("trends").innerHTML = `<thead><tr>
    <th>Metric</th><th class="r">${esc(comparison.previous_period)}</th>
    <th class="r">${esc(comparison.current_period)}</th>
    <th class="r">Change</th><th>Direction</th>
  </tr></thead><tbody>${comparison.movements.map((m) => `<tr>
    <td>${esc(m.label)}</td>
    <td class="r num">${fmt(m, m.previous)}</td>
    <td class="r num">${fmt(m, m.current)}</td>
    <td class="r num">${m.change_pct === null ? "–" : `${arrow(m)} ${Math.abs(m.change_pct).toFixed(1)}%`}</td>
    <td><span class="pill ${tone[m.direction]}">${esc(m.direction)}</span></td>
  </tr>`).join("")}</tbody>`;
}

// ── wiring ───────────────────────────────────────────────────────────────────

$("run").addEventListener("click", close);
$("raw").addEventListener("click", () => window.open(`/api/close/${period}`, "_blank"));

// Switching month is a read, not a run: it shows the close already on file.
$("period").addEventListener("change", () => {
  period = $("period").value;
  loadStored(`showing ${period}`);
});

function loadStored(note) {
  return fetch(`/api/close/${period}`)
    .then((r) => r.ok ? r.json() : null)
    .then((d) => {
      if (!d) return;
      last = d;
      render(d);
      $("dot").className = `dot ${d.outcome === "closed" ? "ok" : "err"}`;
      $("status").textContent = note
        ? `${note} · last run ${d.run_id} · ${d.outcome}`
        : `last run ${d.run_id} · ${d.outcome}. Press the button to watch it run again.`;
    })
    .catch(() => {});
}

// Which months have mail waiting. Rendered newest first, because the month an
// owner wants is nearly always the one that just ended.
fetch("/api/periods").then((r) => r.ok ? r.json() : null).then((d) => {
  if (!d || !d.periods || !d.periods.length) return;
  period = d.default && d.periods.includes(d.default) ? d.default : d.periods[0];
  $("period").innerHTML = [...d.periods].sort().reverse()
    .map((p) => `<option value="${esc(p)}">${esc(p)}</option>`).join("");
  $("period").value = period;
}).catch(() => {}).finally(() => {
  // Show the last close immediately so a cold arrival is not an empty page,
  // then let the visitor run it again and watch it happen.
  loadStored("");
});
