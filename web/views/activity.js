// Activity view: history of every intent the web module has written,
// across all types — not just Amazon staging (see amazon.js, scoped to
// stage_for_amazon only). Mostly read-only, with one action: a completed
// "delete" gets an Undo button, which writes a fresh "restore" intent for
// the same item. The agent's own restore() is what actually makes that
// safe — every delete() in this project is a reversible trash move, never
// an unlink (see CLAUDE.md's dedup rules).
import {
  collection, query, orderBy, limit, getDocs,
} from "https://www.gstatic.com/firebasejs/10.14.1/firebase-firestore.js";
import { db } from "../firebase.js";
import { writeIntent } from "../intents.js";

const MAX_ROWS = 200;

let root = null;
let statusEl = null;
let listEl = null;

const STATUS_LABEL = {
  pending: "Waiting for the agent…",
  claimed: "Agent is working on it…",
  done: "Done",
  failed: "Failed",
};

function canUndo(intent) {
  return intent.type === "delete" && intent.status === "done";
}

function renderRow(intent) {
  const row = document.createElement("div");
  row.className = "activity-row";

  const info = document.createElement("div");
  info.className = "activity-row-info";
  const type = document.createElement("span");
  type.className = "activity-row-type";
  type.textContent = intent.type;
  const item = document.createElement("div");
  item.className = "activity-row-item";
  item.textContent = intent.item_id;
  item.title = intent.item_id;
  const status = document.createElement("div");
  status.className = `activity-row-status activity-status-${intent.status}`;
  status.textContent = STATUS_LABEL[intent.status] || intent.status;
  if (intent.status === "failed" && intent.result?.detail) {
    status.title = intent.result.detail;
  }
  const when = document.createElement("span");
  when.className = "activity-row-when";
  when.textContent = intent.created_at ? new Date(intent.created_at).toLocaleString() : "";
  info.append(type, when, item, status);
  row.appendChild(info);

  if (canUndo(intent)) {
    const undoBtn = document.createElement("button");
    undoBtn.type = "button";
    undoBtn.className = "activity-undo";
    undoBtn.textContent = "Undo";
    undoBtn.addEventListener("click", async () => {
      undoBtn.disabled = true;
      undoBtn.textContent = "Undoing…";
      try {
        await writeIntent("restore", intent.item_id, { source: intent.params?.source || "nas" });
        undoBtn.textContent = "Undo requested";
        status.textContent = "Restore requested — check back once the agent picks it up.";
      } catch (err) {
        undoBtn.disabled = false;
        undoBtn.textContent = "Undo";
        status.textContent = `Undo failed: ${err.message}`;
        console.error(intent.item_id, err);
      }
    });
    row.appendChild(undoBtn);
  }

  return row;
}

async function load() {
  statusEl.textContent = "Loading…";
  try {
    const snap = await getDocs(query(
      collection(db, "intents"), orderBy("created_at", "desc"), limit(MAX_ROWS),
    ));
    listEl.innerHTML = "";
    if (snap.empty) {
      statusEl.textContent = "No activity yet.";
      return;
    }
    for (const doc of snap.docs) listEl.appendChild(renderRow(doc.data()));
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
    <div class="activity-list"></div>
  `;
  statusEl = root.querySelector(".view-status");
  listEl = root.querySelector(".activity-list");
  load();
}

export function unmount() {
  root.innerHTML = "";
}
