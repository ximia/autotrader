"use strict";

const $ = (id) => document.getElementById(id);
const fmtUsd = (n) =>
  n == null ? "—" : "$" + Number(n).toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 });
const fmtPct = (n, decimals = 1) => (n == null ? "—" : (Number(n) * 100).toFixed(decimals) + "%");
const fmtPctRaw = (n, decimals = 1) => (n == null ? "—" : Number(n).toFixed(decimals) + "%");
const pnlClass = (n) => (n > 0 ? "pos" : n < 0 ? "neg" : "muted");
const shortWallet = (w) => (w ? w.slice(0, 6) + "…" + w.slice(-4) : "—");
const fmtTime = (iso) => (iso ? new Date(iso).toLocaleTimeString() : "—");
const fmtDateTime = (iso) =>
  iso ? new Date(iso).toLocaleString(undefined, { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" }) : "—";
const escapeHtml = (s) =>
  String(s).replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const emptyRow = (cols) =>
  `<tr><td class="empty" colspan="${cols}">No data yet — the loop will populate this shortly.</td></tr>`;

// ── HTTP helpers ──────────────────────────────────────────────────────────────

async function getJSON(url) {
  const r = await fetch(url);
  if (!r.ok) throw new Error(`${url} → ${r.status}`);
  return r.json();
}
async function postControl(action, extra = {}) {
  const r = await fetch("/api/control", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ action, ...extra }),
  });
  return r.json();
}

// ── EQUITY CHART ─────────────────────────────────────────────────────────────

let _chart = null;

function initChart() {
  const ctx = $("equity-chart").getContext("2d");
  _chart = new Chart(ctx, {
    type: "line",
    data: {
      labels: [],
      datasets: [
        {
          label: "Bankroll",
          data: [],
          borderColor: "#58a6ff",
          backgroundColor: "#58a6ff18",
          fill: true,
          tension: 0.3,
          pointRadius: 0,
          borderWidth: 1.5,
        },
        {
          label: "Total PnL",
          data: [],
          borderColor: "#3fb950",
          backgroundColor: "transparent",
          fill: false,
          tension: 0.3,
          pointRadius: 0,
          borderWidth: 1.5,
        },
      ],
    },
    options: {
      responsive: true,
      maintainAspectRatio: false,
      animation: false,
      interaction: { mode: "index", intersect: false },
      scales: {
        x: { display: false },
        y: {
          grid: { color: "#30363d55" },
          ticks: { color: "#8b949e", font: { size: 10 } },
        },
      },
      plugins: {
        legend: { display: false },
        tooltip: {
          backgroundColor: "#161b22",
          borderColor: "#30363d",
          borderWidth: 1,
          titleColor: "#8b949e",
          bodyColor: "#e6edf3",
          callbacks: {
            label: (ctx) =>
              ` ${ctx.dataset.label}: $${Number(ctx.parsed.y).toFixed(2)}`,
          },
        },
      },
    },
  });
}

function updateChart(curve) {
  if (!_chart || !curve.length) return;
  // Downsample to at most 200 points to keep rendering fast.
  const step = Math.max(1, Math.floor(curve.length / 200));
  const sampled = curve.filter((_, i) => i % step === 0);
  _chart.data.labels = sampled.map((p) => p.ts.slice(11, 16));
  _chart.data.datasets[0].data = sampled.map((p) => p.bankroll_usd);
  _chart.data.datasets[1].data = sampled.map((p) => p.total_pnl_usd);
  _chart.update("none");
}

// ── REFRESH ───────────────────────────────────────────────────────────────────

async function refresh() {
  try {
    const [status, pnlData, traders, positions, trades, signals, metrics, risk, health] =
      await Promise.all([
        getJSON("/api/status"),
        getJSON("/api/pnl"),
        getJSON("/api/traders"),
        getJSON("/api/positions"),
        getJSON("/api/trades"),
        getJSON("/api/signals"),
        getJSON("/api/metrics"),
        getJSON("/api/risk"),
        getJSON("/api/health"),
      ]);

    renderStatus(status, pnlData.summary);
    updateActivityFeed(status);
    renderChart(pnlData.curve);
    renderTraders(traders);
    renderPositions(positions);
    renderTrades(trades);
    renderSignals(signals);
    renderMetrics(metrics);
    renderRisk(risk, pnlData.summary);
    renderHealth(health);
  } catch (e) {
    console.error("refresh failed:", e);
  }
}

// ── RENDERERS ─────────────────────────────────────────────────────────────────

function renderStatus(s, sum) {
  const badge = $("mode-badge");
  if (s.live_trading) {
    badge.textContent = "● LIVE";
    badge.className = "badge badge-live";
  } else {
    badge.textContent = "PAPER";
    badge.className = "badge badge-paper";
  }

  const cbBadge = $("cb-badge");
  cbBadge.style.display = s.circuit_breaker_active ? "" : "none";
  $("btn-reset-cb").style.display = s.circuit_breaker_active ? "" : "none";

  $("s-bankroll").textContent = fmtUsd(sum.bankroll_usd);

  const pnlEl = $("s-pnl");
  pnlEl.textContent = fmtUsd(sum.total_pnl_usd);
  pnlEl.className = "value " + pnlClass(sum.total_pnl_usd);

  const upnlEl = $("s-upnl");
  upnlEl.textContent = fmtUsd(sum.unrealized_pnl_usd);
  upnlEl.className = "value " + pnlClass(sum.unrealized_pnl_usd);

  $("s-winrate").textContent = fmtPct(sum.win_rate);
  $("s-open").textContent = sum.open_positions;
  $("s-spent").textContent = fmtUsd(s.spent_today_usd) + " / " + fmtUsd(s.max_daily_spend_usd);

  const loopEl = $("s-loop");
  loopEl.textContent = s.paused ? "⏸ paused" : "● running";
  loopEl.className = "value " + (s.paused ? "neg" : "pos");

  $("s-next").textContent = fmtTime(s.next_run_at);
  $("s-signals").textContent = s.last_run_status
    ? (s.last_run_status.match(/executed=(\d+)/) || [])[1] || "0"
    : "—";
  $("s-losses").textContent =
    s.circuit_breaker_active
      ? `🛑 ${s.consecutive_losses}`
      : s.consecutive_losses;

  $("btn-pause").style.display  = s.paused ? "none" : "";
  $("btn-resume").style.display = s.paused ? "" : "none";

  // Geoblock banner — shown when this server's IP is blocked.
  const geoBanner = $("geo-banner");
  if (s.geoblock && s.geoblock.blocked) {
    geoBanner.style.display = "";
    geoBanner.className = "banner banner-err";
    geoBanner.textContent =
      `🚫 GEOBLOCKED — this server's IP (${s.geoblock.ip}) is in a restricted region ` +
      `(${s.geoblock.country}/${s.geoblock.region}). ` +
      `Move the bot to an allowed server (Ireland, Finland, Canada BC, etc.) to trade.`;
  } else {
    geoBanner.style.display = "none";
  }

  // Live readiness banner.
  const banner = $("live-banner");
  if (s.live_trading) {
    banner.style.display = "";
    if (s.live_ready) {
      banner.className = "banner banner-ok";
      banner.textContent =
        `✓ LIVE & READY — ${fmtUsd(s.usdc_available)} USDC available` +
        (s.wallet_address ? ` · ${shortWallet(s.wallet_address)}` : "");
    } else {
      banner.className = "banner banner-warn";
      banner.textContent = `⚠ LIVE but NOT trading — ${s.live_reason || "wallet not ready"}`;
    }
  } else {
    banner.style.display = "none";
  }

  // Config footer.
  const c = s.config;
  $("foot-config").textContent =
    `top ${c.top_n} · ${c.leaderboard_window} · ` +
    `consensus≥${c.min_consensus_count} conf≥${(c.min_signal_confidence * 100).toFixed(0)}% · ` +
    `kelly ${(c.kelly_fraction * 100).toFixed(0)}% · cap ${fmtUsd(c.max_per_trade_usd)} · ` +
    `poll ${s.poll_interval_min}m · TP +${(c.take_profit_pct * 100).toFixed(0)}% SL -${(c.stop_loss_pct * 100).toFixed(0)}% · ` +
    `last ${fmtTime(s.last_run_at)} (${s.last_run_status || "—"})`;
}

function renderChart(curve) {
  updateChart(curve);
}

function renderTraders(rows) {
  const tb = document.querySelector("#traders tbody");
  if (!rows.length) { tb.innerHTML = emptyRow(6); return; }
  tb.innerHTML = rows.map((t) => {
    const scoreW = Math.round((t.composite_score || 0) * 100);
    return `
    <tr>
      <td>${t.rank ?? "—"}</td>
      <td>${t.username ? escapeHtml(t.username) : `<span class="mono">${shortWallet(t.wallet)}</span>`}</td>
      <td>
        <span class="${t.composite_score >= 0.6 ? "pos" : t.composite_score >= 0.4 ? "" : "muted"}">
          ${(t.composite_score * 100).toFixed(0)}
        </span>
        <span class="score-bar" style="width:${scoreW}px;opacity:.5"></span>
      </td>
      <td class="${t.roi_estimate >= 0 ? "pos" : "neg"}">${fmtPct(t.roi_estimate)}</td>
      <td class="${pnlClass(t.pnl)}">${fmtUsd(t.pnl)}</td>
      <td>${t.copied_trades}</td>
    </tr>`;
  }).join("");
}

function renderPositions(rows) {
  const tb = document.querySelector("#positions tbody");
  if (!rows.length) { tb.innerHTML = emptyRow(7); return; }
  tb.innerHTML = rows.map((p) => `
    <tr>
      <td>${p.market ? escapeHtml(p.market) : "—"}</td>
      <td>${p.outcome ? escapeHtml(p.outcome) : "—"}</td>
      <td>${p.shares}</td>
      <td class="muted">${p.avg_price}</td>
      <td>${p.cur_price}</td>
      <td class="${pnlClass(p.unrealized_pnl_usd)}">${fmtUsd(p.unrealized_pnl_usd)}</td>
      <td class="${pnlClass(p.unrealized_pnl_pct)}">${fmtPct(p.unrealized_pnl_pct)}</td>
    </tr>`).join("");
}

function renderSignals(rows) {
  const tb = document.querySelector("#signals tbody");
  if (!rows.length) { tb.innerHTML = emptyRow(7); return; }
  tb.innerHTML = rows.map((r) => {
    const confPct = Math.round(r.confidence * 100);
    const confClass = confPct >= 70 ? "pos" : confPct >= 55 ? "" : "muted";
    return `
    <tr>
      <td class="muted">${fmtDateTime(r.ts)}</td>
      <td>${r.market ? escapeHtml(r.market) : r.token_id?.slice(0, 12) || "—"}
          ${r.outcome ? `<span class="muted">(${escapeHtml(r.outcome)})</span>` : ""}</td>
      <td><span class="tag tag-${r.side === "BUY" ? "buy" : "sell"}">${r.side}</span></td>
      <td>${r.consensus_count} trader${r.consensus_count !== 1 ? "s" : ""}</td>
      <td class="${confClass}">${confPct}%</td>
      <td>${r.executed ? fmtUsd(r.usd_executed) : "—"}</td>
      <td>${r.executed
        ? `<span class="tag tag-filled">executed</span>`
        : `<span class="tag tag-skipped" title="${r.skip_reason ? escapeHtml(r.skip_reason) : ""}">skipped</span>`
      }</td>
    </tr>`;
  }).join("");
}

function renderTrades(rows) {
  const tb = document.querySelector("#trades tbody");
  if (!rows.length) { tb.innerHTML = emptyRow(9); return; }
  tb.innerHTML = rows.map((r) => `
    <tr>
      <td class="muted">${fmtTime(r.time)}</td>
      <td class="mono">${shortWallet(r.trader)}</td>
      <td>${r.market ? escapeHtml(r.market) : "—"}
          ${r.outcome ? `<span class="muted">(${escapeHtml(r.outcome)})</span>` : ""}</td>
      <td><span class="tag tag-${r.side === "BUY" ? "buy" : "sell"}">${r.side}</span>${exitBadge(r)}</td>
      <td>${r.status === "skipped" ? "—" : fmtUsd(r.our_usd)}</td>
      <td class="muted">${r.fill_price || "—"}</td>
      <td class="muted">${r.confidence_score != null ? `${(r.confidence_score * 100).toFixed(0)}%` : "—"}</td>
      <td class="${r.slippage_pct != null && r.slippage_pct > 0.02 ? "neg" : "muted"}">
        ${r.slippage_pct != null ? (r.slippage_pct * 100).toFixed(2) + "%" : "—"}
      </td>
      <td><span class="tag tag-${r.status}" title="${r.skip_reason ? escapeHtml(r.skip_reason) : ""}">${r.status}</span></td>
    </tr>`).join("");
}

function exitBadge(r) {
  if (r.side !== "SELL" || !r.skip_reason || !r.skip_reason.startsWith("auto-exit")) return "";
  const reason = r.skip_reason.split(":")[1]?.trim() || "exit";
  const labels = {
    take_profit: "TP", stop_loss: "SL",
    trailing_stop: "Trail", mirror_exit: "follow", break_even: "BE",
  };
  const label = labels[reason] || reason;
  return ` <span class="tag tag-${reason}">${label}</span>`;
}

function renderMetrics(m) {
  if (!m.count) return;
  $("m-lat").textContent  = m.latency_ms?.mean   != null ? `${m.latency_ms.mean.toFixed(1)} ms`  : "—";
  $("m-slip").textContent = m.slippage_pct?.mean  != null ? `${(m.slippage_pct.mean * 100).toFixed(3)}%` : "—";
  $("m-conf").textContent = m.confidence?.mean    != null ? `${(m.confidence.mean * 100).toFixed(1)}%` : "—";
  $("m-cons").textContent = m.consensus_count?.mean != null ? m.consensus_count.mean.toFixed(1) : "—";
  $("m-count").textContent = m.count;
}

function renderRisk(risk, sum) {
  const startD = risk.daily_start_equity;
  const startW = risk.weekly_start_equity;
  const equity = sum.bankroll_usd;

  const dailyLoss = startD > 0 ? (startD - equity) / startD : 0;
  const weeklyLoss = startW > 0 ? (startW - equity) / startW : 0;

  $("r-daily").textContent = startD > 0
    ? `${(dailyLoss * 100).toFixed(1)}% / ${(risk.daily_loss_limit_pct * 100).toFixed(0)}% limit`
    : "—";
  $("r-daily").className = "value " + (dailyLoss >= risk.daily_loss_limit_pct * 0.8 ? "neg" : "");

  $("r-weekly").textContent = startW > 0
    ? `${(weeklyLoss * 100).toFixed(1)}% / ${(risk.weekly_loss_limit_pct * 100).toFixed(0)}% limit`
    : "—";
  $("r-weekly").className = "value " + (weeklyLoss >= risk.weekly_loss_limit_pct * 0.8 ? "neg" : "");

  $("r-cb").textContent = risk.circuit_breaker_active
    ? "🛑 ACTIVE"
    : `off (after ${risk.circuit_breaker_threshold} losses)`;
  $("r-cb").className = "value " + (risk.circuit_breaker_active ? "neg" : "muted");

  $("r-streak").textContent = risk.consecutive_losses;
  $("r-streak").className = "value " + (risk.consecutive_losses >= 3 ? "neg" : "");

  $("r-de").textContent = fmtUsd(risk.daily_start_equity);

  // Risk banner.
  const riskBanner = $("risk-banner");
  if (risk.circuit_breaker_active) {
    riskBanner.style.display = "";
    riskBanner.className = "banner banner-err";
    riskBanner.textContent = `🛑 CIRCUIT BREAKER — ${risk.consecutive_losses} consecutive losses. All trading halted. Click "Reset breaker" to resume.`;
  } else if (dailyLoss >= risk.daily_loss_limit_pct * 0.8 && startD > 0) {
    riskBanner.style.display = "";
    riskBanner.className = "banner banner-warn";
    riskBanner.textContent = `⚠ Approaching daily loss limit: down ${(dailyLoss * 100).toFixed(1)}% today`;
  } else {
    riskBanner.style.display = "none";
  }
}

function renderHealth(health) {
  const dot = $("health-dot");
  const age = health.last_run_age_s;
  const stale = age != null && age > 300;  // stale if no run in 5 min
  dot.className = "health-dot " + (health.scheduler_running && !stale ? "ok" : "err");
  dot.title = health.scheduler_running
    ? `Scheduler running · last run ${age != null ? Math.round(age) + "s ago" : "never"}`
    : "Scheduler not running";
}

// ── BUTTON HANDLERS ───────────────────────────────────────────────────────────

$("btn-run").addEventListener("click", async () => {
  $("btn-run").textContent = "⏳ Running…";
  $("btn-run").disabled = true;
  await postControl("run_now");
  setTimeout(() => {
    $("btn-run").textContent = "▶ Run now";
    $("btn-run").disabled = false;
    refresh();
  }, 2000);
});
$("btn-pause").addEventListener("click",  async () => { await postControl("pause");  refresh(); });
$("btn-resume").addEventListener("click", async () => { await postControl("resume"); refresh(); });
$("btn-reset-cb").addEventListener("click", async () => {
  if (!confirm("Reset the circuit breaker and resume trading?")) return;
  await postControl("reset_circuit_breaker");
  refresh();
});

// ── ACTIVITY FEED ─────────────────────────────────────────────────────────────

const _activityLog = [];
let _lastRunStatus = "";
let _lastRunAt = "";
let _lastRunsTotal = 0;
let _refreshCount = 0;

function pushActivity(msg, color = "#8b949e") {
  const now = new Date().toLocaleTimeString();
  _activityLog.unshift(`<span style="color:#444">[${now}]</span> <span style="color:${color}">${msg}</span>`);
  if (_activityLog.length > 20) _activityLog.pop();
  const el = $("activity-feed");
  if (el) el.innerHTML = _activityLog.join("<br>");
}

function updateActivityFeed(s) {
  // Update refresh counter
  _refreshCount++;
  const tick = $("refresh-tick");
  if (tick) tick.textContent = `auto-refreshing every 5s · refresh #${_refreshCount}`;

  // Log every completed cycle (track by runs_total counter)
  if (s.runs_total && s.runs_total !== _lastRunsTotal) {
    _lastRunsTotal = s.runs_total;
    _lastRunStatus = s.last_run_status;
    const parts = (s.last_run_status || "").match(/signals=(\d+) executed=(\d+) skipped=(\d+)/);
    if (parts) {
      const [, sigs, exec, skip] = parts;
      const color = parseInt(exec) > 0 ? "#3fb950" : parseInt(sigs) > 0 ? "#d29922" : "#8b949e";
      pushActivity(`Cycle #${s.runs_total}: ${sigs} signals → <b style="color:${color}">${exec} executed</b>, ${skip} skipped`, color);
    } else {
      pushActivity(`Cycle #${s.runs_total}: ${s.last_run_status || "complete"}`);
    }
  }

  // Show next run countdown
  if (s.next_run_at) {
    const secsUntil = Math.max(0, Math.round((new Date(s.next_run_at) - Date.now()) / 1000));
    $("s-next").textContent = secsUntil > 0 ? `in ${secsUntil}s` : "now";
  }
}

// ── BOOT ─────────────────────────────────────────────────────────────────────

initChart();
pushActivity("Dashboard connected — bot monitoring started", "#58a6ff");
refresh();
setInterval(refresh, 5000);
