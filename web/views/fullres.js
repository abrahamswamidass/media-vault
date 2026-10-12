// Full-res view: what "Request full-res" (Browse/Map's photo modal, see
// intents.js's fetchFullRes()) actually produced — same read-only status
// shape as amazon.js, but for fetch_fullres intents instead of
// stage_for_amazon ones, with one extra step once an intent is done:
// resolving the *result* blob (not the item's own thumbnail_key) to an
// actual viewable/downloadable URL.
//
// Previews live under GCS `previews/`, which a bucket lifecycle rule empties
// after a day (see CLAUDE.md's "Previews expire" note) — nothing here
// accumulates. getDownloadURL() rejecting for a "done" intent almost always
// means exactly that: the result blob existed once but has since expired,
// not a real error, so that case gets its own "Expired" status rather than
// a broken image or a scary failure message.
import {
  collection, query, where, limit, getDocs,
} from "https://www.gstatic.com/firebasejs/10.14.1/firebase-firestore.js";
import { getDownloadURL, ref } from "https://www.gstatic.com/firebasejs/10.14.1/firebase-storage.js";
import { storage, db } from "../firebase.js";

const MAX_ROWS = 200;

let root = null;
let statusEl = null;
let listEl = null;

const STATUS_LABEL = {
  pending: "Waiting for the agent…",
  claimed: "Agent is working on it…",
  failed: "Failed",
};

function renderRow(intent) {
  const row = document.createElement("div");
  row.className = "amazon-row";

  const link = document.createElement("a");
  link.target = "_blank";
  link.rel = "noopener";
  const img = document.createElement("img");
  link.appendChild(img);
  row.appendChild(link);

  const info = document.createElement("div");
  info.className = "amazon-row-info";
  const name = document.createElement("div");
  name.className = "amazon-row-name";
  const variant = intent.params?.variant || "preview";
  name.textContent = `${intent.item_id} (${variant})`;
  const status = document.createElement("div");
  status.className = "amazon-row-status";
  const when = document.createElement("div");
  when.className = "amazon-row-when";
  when.textContent = intent.created_at ? new Date(intent.created_at).toLocaleString() : "";
  info.append(name, status, when);
  row.appendChild(info);

  // The key lives at result.outputs.key, not result.key -- ActionResult is
  // a dataclass with outputs as a nested field (see base.py), the same
  // shape every other action's result already uses (ColdArchiveAction,
  // ArchiveItemAction, ...). An intent completed before the agent's NoOp
  // started carrying outputs on a replay (see derive.py's
  // FetchFullResAction) can still be "done" with no key at all -- treated
  // the same as expired below, since there's equally nothing to resolve.
  const key = intent.result?.outputs?.key;
  if (intent.status === "done" && key) {
    status.classList.add("amazon-status-done");
    status.textContent = "Ready";
    getDownloadURL(ref(storage, key))
      .then((url) => {
        img.src = url;
        link.href = url;
      })
      .catch(() => {
        // Almost always expiry, not a genuine error -- see header comment.
        status.classList.remove("amazon-status-done");
        status.classList.add("amazon-status-pending");
        status.textContent = "Expired (previews are deleted after a day)";
      });
  } else if (intent.status === "done") {
    status.classList.add("amazon-status-pending");
    status.textContent = "Expired (previews are deleted after a day)";
  } else if (intent.status === "failed") {
    status.classList.add("amazon-status-failed");
    status.textContent = STATUS_LABEL.failed;
    if (intent.result?.detail) status.title = intent.result.detail;
  } else {
    status.classList.add("amazon-status-pending");
    status.textContent = STATUS_LABEL[intent.status] || intent.status;
  }

  return row;
}

async function load() {
  statusEl.textContent = "Loading…";
  try {
    const snap = await getDocs(query(
      collection(db, "intents"),
      where("type", "==", "fetch_fullres"),
      limit(MAX_ROWS),
    ));
    const intents = snap.docs.map((d) => d.data())
      .sort((a, b) => (b.created_at || "").localeCompare(a.created_at || ""));

    listEl.innerHTML = "";
    if (!intents.length) {
      statusEl.textContent = "Nothing requested yet — open a photo in Browse or "
        + "Map, then use “Request full-res” from its ⋮ menu.";
      return;
    }
    for (const intent of intents) listEl.appendChild(renderRow(intent));
    statusEl.textContent = "";
  } catch (err) {
    statusEl.textContent = `Failed to load: ${err.message}`;
    console.error(err);
  }
}

export function mount(container) {
  root = container;
  root.innerHTML = `
    <p class="view-status"></p>
    <div class="amazon-list"></div>
  `;
  statusEl = root.querySelector(".view-status");
  listEl = root.querySelector(".amazon-list");
  load();
}

export function unmount() {
  root.innerHTML = "";
}
