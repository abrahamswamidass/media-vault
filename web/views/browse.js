// Browse view: chronological grid, grouped by year/month, paginated.
//
// Grouping uses `mtime` (filesystem modified time, already on every fact)
// rather than a true "date taken" — that needs EXIF extraction on the agent
// side (tracked separately) and isn't wired in yet. Fine as an approximation
// for now; swapping the sort/group key to a real date_taken field later is a
// one-line change once that lands.
import {
  collection, query, orderBy, limit, startAfter, getDocs,
} from "https://www.gstatic.com/firebasejs/10.14.1/firebase-firestore.js";
import { getDownloadURL, ref } from "https://www.gstatic.com/firebasejs/10.14.1/firebase-storage.js";
import { db, storage } from "../firebase.js";

const PAGE_SIZE = 100;

let root = null;
let statusEl = null;
let groupsEl = null;
let loadMoreBtn = null;
let modal = null;

let lastDoc = null;
let exhausted = false;
let loading = false;
const groupEls = new Map(); // "2026-08" -> <section> element, so pages merge into existing months

function groupKey(mtimeSeconds) {
  const d = new Date(mtimeSeconds * 1000);
  return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}`;
}

function groupLabel(key) {
  const [year, month] = key.split("-").map(Number);
  const d = new Date(year, month - 1, 1);
  return d.toLocaleDateString(undefined, { year: "numeric", month: "long" });
}

function groupSection(key) {
  let section = groupEls.get(key);
  if (section) return section;

  section = document.createElement("section");
  section.className = "month-group";
  const h2 = document.createElement("h2");
  h2.textContent = groupLabel(key);
  const grid = document.createElement("div");
  grid.className = "grid";
  section.append(h2, grid);
  groupEls.set(key, section);

  // Items arrive strictly newest-first (Firestore orderBy("mtime", "desc"),
  // continued across pages via startAfter) — a group key we haven't seen yet
  // is always older than every group seen so far, so appending always keeps
  // sections in chronological order with no need to search for where to insert.
  groupsEl.appendChild(section);
  return section;
}

function renderCard(item) {
  const card = document.createElement("div");
  card.className = "card";

  const img = document.createElement("img");
  img.alt = item.name || item.item_id;
  img.loading = "lazy";
  card.appendChild(img);

  const meta = document.createElement("div");
  meta.className = "meta";
  meta.textContent = item.item_id;
  meta.title = item.item_id;
  card.appendChild(meta);

  card.addEventListener("click", () => openModal(item, img.src));

  getDownloadURL(ref(storage, item.thumbnail_key))
    .then((url) => { img.src = url; })
    .catch((err) => { card.classList.add("broken"); console.error(item.item_id, err); });

  return card;
}

function openModal(item, thumbUrl) {
  modal.querySelector("img").src = thumbUrl || "";
  modal.querySelector(".modal-title").textContent = item.item_id;
  modal.querySelector(".modal-meta").textContent =
    `${(item.size / 1024).toFixed(0)} KB · ${new Date(item.mtime * 1000).toLocaleString()}`;
  modal.hidden = false;
}

async function loadPage() {
  if (loading || exhausted) return;
  loading = true;
  loadMoreBtn.textContent = "Loading…";
  loadMoreBtn.disabled = true;

  const clauses = [collection(db, "items"), orderBy("mtime", "desc"), limit(PAGE_SIZE)];
  if (lastDoc) clauses.splice(2, 0, startAfter(lastDoc));

  const snap = await getDocs(query(...clauses));
  if (snap.empty) {
    exhausted = true;
    loadMoreBtn.hidden = true;
  } else {
    lastDoc = snap.docs[snap.docs.length - 1];
    for (const doc of snap.docs) {
      const item = doc.data();
      groupSection(groupKey(item.mtime)).querySelector(".grid").appendChild(renderCard(item));
    }
    if (snap.docs.length < PAGE_SIZE) {
      exhausted = true;
      loadMoreBtn.hidden = true;
    }
  }

  loading = false;
  loadMoreBtn.textContent = "Load more";
  loadMoreBtn.disabled = false;
  statusEl.textContent = exhausted && groupEls.size === 0 ? "Nothing published yet." : "";
}

export function mount(container) {
  root = container;
  root.innerHTML = `
    <p class="view-status"></p>
    <div class="month-groups"></div>
    <button class="load-more">Load more</button>
    <div class="modal" hidden>
      <div class="modal-content">
        <button class="modal-close">&times;</button>
        <img alt="" />
        <p class="modal-title"></p>
        <p class="modal-meta"></p>
        <button class="modal-fullres" disabled
          title="Not built yet — needs the agent-side intent processor (see backlog).">
          Request full-res (coming soon)
        </button>
      </div>
    </div>
  `;
  statusEl = root.querySelector(".view-status");
  groupsEl = root.querySelector(".month-groups");
  loadMoreBtn = root.querySelector(".load-more");
  modal = root.querySelector(".modal");

  loadMoreBtn.addEventListener("click", loadPage);
  modal.querySelector(".modal-close").addEventListener("click", () => { modal.hidden = true; });
  modal.addEventListener("click", (e) => { if (e.target === modal) modal.hidden = true; });

  lastDoc = null;
  exhausted = false;
  groupEls.clear();
  loadPage().catch((err) => {
    statusEl.textContent = `Failed to load: ${err.message}`;
    console.error(err);
  });
}

export function unmount() {
  root.innerHTML = "";
}
