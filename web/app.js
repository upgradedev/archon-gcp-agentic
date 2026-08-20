// The page's behaviour: press one button, watch a month close.
//
// Lifted out of the page so the content security policy can refuse inline
// script outright. A DAST scan raised script-src 'unsafe-inline' against
// the running container, and with it any injected script tag executes, so
// the finding was real and this is the fix rather than an ignore rule.
const $ = (id) => document.getElementById(id);
const PERIOD = "2026-07";
const usd = (n) => (n === null || n === undefined) ? "–"
  : n.toLocaleString("en-US", { style: "currency", currency: "USD", maximumFractionDigits: 2 });
const num = (n, d = 0) => (n === null || n === undefined) ? "–"
  : n.toLocaleString("en-US", { minimumFractionDigits: d, maximumFractionDigits: d });
const esc = (s) => String(s ?? "").replace(/[&<>"']/g,
  (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const words = (s) => String(s ?? "").replace(/_/g, " ");

let last = null;

async function close() {
  const btn = $("run");
  btn.disabled = true;
  btn.textContent = "Closing…";
  $("status").textContent = "the agent is working";
  $("error").classList.add("hidden");
  try {
    const res = await fetch(`/api/close/${PERIOD}`, { method: "POST" });
    if (!res.ok) throw new Error(`the close returned ${res.status}`);
    last = await res.json();
    render(last);
    btn.textContent = "Close it again";
    $("status").textContent = `run ${last.run_id} · ${last.outcome}`;
  } catch (err) {
    $("error").textContent = `Could not close the month: ${err.message}`;
    $("error").classList.remove("hidden");
    btn.textContent = "Try again";
  } finally {
    btn.disabled = false;
  }
}

function render(d) {
  $("out").classList.remove("hidden");
  renderTrail(d.journal);
  renderStats(d);
  $("summary").textContent = d.summary;
  renderAlloc(d.allocations);
  renderFindings(d.findings);
  renderDigest(d.digest, d.receipt);
  renderDrafts(d.drafts);
  renderGates(d.gates);
  renderTrucks(d.statements.per_truck);
}

// Steps land one at a time. The delay is presentation only: the run already
// finished server-side, and the durations shown are the real ones.
function renderTrail(j) {
  const host = $("trail");
  host.innerHTML = "";
  j.steps.forEach((s, i) => {
    const el = document.createElement("div");
    el.className = `step ${s.status !== "ok" ? s.status : ""}`;
    el.style.animationDelay = `${i * 190}ms`;
    el.innerHTML = `<div class="t">${esc(s.title)}<span class="ms num">${s.duration_ms} ms</span></div>
                    <div class="d">${esc(s.detail)}</div>`;
    host.appendChild(el);
  });
}

function renderStats(d) {
  const s = d.statements;
  const errs = d.findings.filter((f) => f.severity === "error").length;
  const cards = [
    ["Net profit", usd(s.net_profit), `${usd(s.revenue)} billed, ${usd(s.operating_expenses)} spent`],
    ["Margin per mile", num(s.revenue_per_mile - s.cost_per_mile, 3),
     `${num(s.revenue_per_mile, 3)} earned, ${num(s.cost_per_mile, 3)} spent`],
    ["Miles run", num(s.total_miles), `${Object.keys(s.per_truck).length} trucks`],
    ["Exceptions", String(d.findings.length), `${errs} of them errors`],
    ["Being chased", usd(d.recoverable), `${d.drafts.length} letters filed, none sent`],
    ["Close", words(d.outcome), `${d.gates.filter((g) => g.passed).length}/${d.gates.length} gates passed`],
  ];
  $("stats").innerHTML = cards.map(([k, v, sub]) =>
    `<div class="card"><div class="k">${esc(k)}</div><div class="v num">${esc(v)}</div>
     <div class="s">${esc(sub)}</div></div>`).join("");
}

function renderAlloc(list) {
  const rows = list.map((a) => {
    const head = `<tr><td colspan="6" style="background:var(--card-hi)">
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

function renderTrucks(per) {
  const rows = Object.entries(per).sort().map(([truck, r]) => `<tr>
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

$("run").addEventListener("click", close);
$("raw").addEventListener("click", () => window.open(`/api/close/${PERIOD}`, "_blank"));

// Show the last close immediately so a cold arrival is not an empty page, then
// let the visitor run it again and watch it happen.
fetch(`/api/close/${PERIOD}`).then((r) => r.ok ? r.json() : null).then((d) => {
  if (d && !last) { last = d; render(d); $("status").textContent =
    `last run ${d.run_id} · ${d.outcome}. Press the button to watch it run again.`; }
}).catch(() => {});
