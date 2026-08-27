// Amazon view: read-only status of what's been staged. Picking a photo to
// stage happens where you're already looking at it — Browse's modal and
// Map's popups (see intents.js's stageForAmazon()) — this tab just shows
// what those requests turned into. Backed by the agent-side processor
// (mediavault process-intents), which claims a pending stage_for_amazon
// intent, runs it, and writes status/result back.
import {
  collection, query, where, limit, getDocs, doc, getDoc,
} from "https://www.gstatic.com/firebasejs/10.14.1/firebase-firestore.js";
import { getDownloadURL, ref } from "https://www.gstatic.com/firebasejs/10.14.1/firebase-storage.js";
import { db, storage } from "../firebase.js";

// A plain equality filter needs no composite index (unlike pairing it with
// orderBy on a different field, which would) — sorting by created_at happens
// client-side instead. Fine at this scale: staged-item counts are nowhere
// near the size a full library scan produces.
const MAX_ROWS = 200;

let root = null;
let statusEl = null;
let listEl = null;

const STATUS_LABEL = {
  pending: "Waiting for the agent…",
  claimed: "Agent is working on it…",
  done: "Staged",
  failed: "Failed",
};

// Mirrors sync/facts.py's doc_id() exactly — Firestore document ids can't
// contain '/', item_id is a path, so it's flattened the same way on both sides.
function itemDocId(source, itemId) {
  return `${source}__${itemId.replace(/[/\\]+/g, "_")}`;
}

async function fetchThumbnailUrl(source, itemId) {
  const snap = await getDoc(doc(db, "items", itemDocId(source, itemId)));
  if (!snap.exists()) return null;
  const key = snap.data().thumbnail_key;
  if (!key) return null;
  return getDownloadURL(ref(storage, key));
}

function renderRow(intent) {
  const row = document.createElement("div");
  row.className = "amazon-row";

  const img = document.createElement("img");
  const source = intent.params?.source || "nas";
  fetchThumbnailUrl(source, intent.item_id)
    .then((url) => { if (url) img.src = url; })
    .catch((err) => console.error(intent.item_id, err));
  row.appendChild(img);

  const info = document.createElement("div");
  info.className = "amazon-row-info";
  const name = document.createElement("div");
  name.className = "amazon-row-name";
  name.textContent = intent.item_id;
  const status = document.createElement("div");
  status.className = `amazon-row-status amazon-status-${intent.status}`;
  status.textContent = STATUS_LABEL[intent.status] || intent.status;
  if (intent.status === "failed" && intent.result?.detail) {
    status.title = intent.result.detail;
  }
  const when = document.createElement("div");
  when.className = "amazon-row-when";
  when.textContent = intent.created_at ? new Date(intent.created_at).toLocaleString() : "";
  info.append(name, status, when);
  row.appendChild(info);

  return row;
}

async function load() {
  statusEl.textContent = "Loading…";
  try {
    const snap = await getDocs(query(
      collection(db, "intents"),
      where("type", "==", "stage_for_amazon"),
      limit(MAX_ROWS),
    ));
    const intents = snap.docs.map((d) => d.data())
      .sort((a, b) => (b.created_at || "").localeCompare(a.created_at || ""));

    listEl.innerHTML = "";
    if (!intents.length) {
      statusEl.textContent = "Nothing staged yet — pick a photo in Browse or "
        + "Map and use “Stage for Amazon”.";
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
