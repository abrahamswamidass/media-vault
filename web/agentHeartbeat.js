// Topbar status: is the agent's background loop alive, and are its two
// periodic maintenance jobs (weekly index, weekly cold-archive) healthy?
// All three read from the one `agent_status/process_intents` doc the
// watch loop already writes every poll -- `schedules` is a bonus field on
// that same doc (see IntentsStore.heartbeat), not a separate collection.
// Not scoped to any one view — the watch loop processes every intent type
// in the REGISTRY (fetch_fullres, delete, copy, index, dedup_source,
// publish, stage_for_amazon, ...), not just Amazon staging, so this belongs
// in the shared header rather than the Amazon tab.
import { doc, getDoc } from "https://www.gstatic.com/firebasejs/10.14.1/firebase-firestore.js";
import { db } from "./firebase.js";

// Written once per poll (default --interval 600s) by IntentsStore.heartbeat().
// Twice that default is a generous margin before calling it stale — a single
// slow poll, or a run at a longer --interval, shouldn't flip the dot red.
const STALE_AFTER_MS = 20 * 60 * 1000;
const REFRESH_MS = 30 * 1000;

let el = null;
let timer = null;

// `schedules` is keyed by a dynamic name (e.g. "index_nas") that follows
// whatever *_SOURCE env var the agent is configured with — there's only
// ever one of each kind in this single-source-at-a-time design, so this
// just finds it by prefix rather than assuming "nas".
function pickByPrefix(schedules, prefix) {
  if (!schedules) return null;
  const key = Object.keys(schedules).find((k) => k.startsWith(prefix));
  return key ? schedules[key] : null;
}

function scheduleState(entry) {
  if (!entry || !entry.enabled) {
    return { cls: "is-off", text: "off" };
  }
  if (!entry.last_run_at) {
    return { cls: "is-stale", text: "not run yet" };
  }
  const ageDays = (Date.now() - new Date(entry.last_run_at).getTime()) / 86400000;
  // 1.5x the configured interval before calling it overdue -- same
  // generous-margin reasoning as STALE_AFTER_MS above, just scaled to a
  // multi-day cadence instead of a multi-minute one.
  const isLive = ageDays <= (entry.interval_days || 7) * 1.5;
  return {
    cls: isLive ? "is-live" : "is-stale",
    text: isLive ? "ok" : "overdue",
  };
}

function scheduleTitle(label, entry) {
  if (!entry || !entry.enabled) {
    const envVar = label === "Index" ? "INDEX_SCHEDULE" : "COLD_ARCHIVE_SCHEDULE";
    return `${label}: not enabled (set ${envVar}=1)`;
  }
  if (!entry.last_run_at) {
    return `${label}: enabled (every ${entry.interval_days}d), hasn't run yet`;
  }
  const when = new Date(entry.last_run_at).toLocaleString();
  return `${label}: last ran ${when} (every ${entry.interval_days}d)\n${entry.detail || ""}`.trim();
}

async function refresh() {
  let data = null;
  try {
    const snap = await getDoc(doc(db, "agent_status", "process_intents"));
    if (snap.exists()) data = snap.data();
  } catch (err) {
    console.error(err);
  }

  // -- process-intents row --
  const last = data?.last_poll_at;
  const dot = el.querySelector('[data-row="intents"] .agent-heartbeat-dot');
  const label = el.querySelector('[data-row="intents"] .agent-heartbeat-label');
  const age = last ? Date.now() - new Date(last).getTime() : Infinity;
  const isLive = age <= STALE_AFTER_MS;
  dot.className = `agent-heartbeat-dot ${isLive ? "is-live" : "is-stale"}`;
  if (!last) {
    label.textContent = "agent not watching for requests";
    dot.title = "No heartbeat recorded yet.";
  } else if (isLive) {
    label.textContent = `watching — checked ${new Date(last).toLocaleTimeString()}`;
    dot.title = `Watching for requests. Last checked ${new Date(last).toLocaleString()}.`;
  } else {
    label.textContent = `not watching — last checked ${new Date(last).toLocaleString()}`;
    dot.title = `No heartbeat in over ${Math.round(STALE_AFTER_MS / 60000)} minutes.`;
  }

  // -- index / cold-archive rows --
  for (const [row, prefix, displayName] of [
    ["index", "index_", "Index"],
    ["cold-archive", "cold_archive_", "Cold-archive"],
  ]) {
    const entry = pickByPrefix(data?.schedules, prefix);
    const state = scheduleState(entry);
    const rowDot = el.querySelector(`[data-row="${row}"] .agent-heartbeat-dot`);
    const rowLabel = el.querySelector(`[data-row="${row}"] .agent-heartbeat-label`);
    rowDot.className = `agent-heartbeat-dot ${state.cls}`;
    rowDot.title = scheduleTitle(displayName, entry);
    rowLabel.textContent = `${displayName}: ${state.text}`;
  }
}

export function start(container) {
  el = container;
  el.innerHTML = `
    <span class="agent-heartbeat-row" data-row="intents">
      <span class="agent-heartbeat-dot"></span>
      <span class="agent-heartbeat-label">checking…</span>
    </span>
    <span class="agent-heartbeat-row" data-row="index">
      <span class="agent-heartbeat-dot"></span>
      <span class="agent-heartbeat-label">Index: checking…</span>
    </span>
    <span class="agent-heartbeat-row" data-row="cold-archive">
      <span class="agent-heartbeat-dot"></span>
      <span class="agent-heartbeat-label">Cold-archive: checking…</span>
    </span>
  `;
  refresh();
  timer = setInterval(refresh, REFRESH_MS);
}

export function stop() {
  clearInterval(timer);
  timer = null;
}
