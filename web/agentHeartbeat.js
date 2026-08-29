// Topbar status dot: is `process-intents --watch` actually running?
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

async function refresh() {
  let last = null;
  try {
    const snap = await getDoc(doc(db, "agent_status", "process_intents"));
    if (snap.exists()) last = snap.data().last_poll_at;
  } catch (err) {
    console.error(err);
  }

  const dot = el.querySelector(".agent-heartbeat-dot");
  const label = el.querySelector(".agent-heartbeat-label");
  const age = last ? Date.now() - new Date(last).getTime() : Infinity;
  const isLive = age <= STALE_AFTER_MS;
  dot.classList.toggle("is-live", isLive);
  dot.classList.toggle("is-stale", !isLive);
  if (!last) {
    label.textContent = "agent not watching for requests";
  } else if (isLive) {
    label.textContent = `watching — checked ${new Date(last).toLocaleTimeString()}`;
  } else {
    label.textContent = `not watching — last checked ${new Date(last).toLocaleString()}`;
  }
}

export function start(container) {
  el = container;
  el.innerHTML = `
    <span class="agent-heartbeat-dot"></span>
    <span class="agent-heartbeat-label">checking…</span>
  `;
  refresh();
  timer = setInterval(refresh, REFRESH_MS);
}

export function stop() {
  clearInterval(timer);
  timer = null;
}
