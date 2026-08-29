// People view: browse detected faces, grouped by person. Read-only, view
// only — naming stays a `people-rename` CLI-only step for now, since names
// have no Firestore home yet (see CLAUDE.md's web/ section and the
// tracking issue for web-side labeling).
//
// Aggregated entirely client-side: Firestore has no "array length > 0"
// query, so there's no way to ask it directly for "items with a detected
// face." This scans a bounded window of recent items (by mtime, the same
// field Browse orders by) and buckets by person_id — the same
// client-side-aggregation shape Folders already uses for path segments.
// A person whose only appearances fall outside that window won't show up
// here; SCAN_LIMIT is generous for a personal library, not a guarantee.
import {
  collection, query, orderBy, limit, getDocs,
} from "https://www.gstatic.com/firebasejs/10.14.1/firebase-firestore.js";
import { getDownloadURL, ref } from "https://www.gstatic.com/firebasejs/10.14.1/firebase-storage.js";
import { db, storage } from "../firebase.js";
import { openPhotoAt } from "../photoModal.js";

const SCAN_LIMIT = 5000;

let root = null;
let breadcrumbEl = null;
let statusEl = null;
let gridEl = null;
let people = new Map(); // personId -> { items: [] }

function personLabel(personId, count) {
  return `Person ${personId} · ${count} photo${count === 1 ? "" : "s"}`;
}

function renderBreadcrumb(personId) {
  breadcrumbEl.innerHTML = "";
  const home = document.createElement("button");
  home.type = "button";
  home.className = "crumb";
  home.textContent = "All people";
  home.disabled = !personId;
  home.addEventListener("click", renderPeopleGrid);
  breadcrumbEl.appendChild(home);
  if (!personId) return;

  const sep = document.createElement("span");
  sep.className = "crumb-sep";
  sep.textContent = "/";
  const current = document.createElement("button");
  current.type = "button";
  current.className = "crumb";
  current.textContent = `Person ${personId}`;
  current.disabled = true;
  breadcrumbEl.append(sep, current);
}

function renderPersonTile(personId, info) {
  const tile = document.createElement("div");
  tile.className = "card person-card";
  const img = document.createElement("img");
  img.alt = `Person ${personId}`;
  img.loading = "lazy";
  const label = document.createElement("div");
  label.className = "person-label";
  label.textContent = personLabel(personId, info.items.length);
  tile.append(img, label);
  tile.addEventListener("click", () => openPerson(personId));

  getDownloadURL(ref(storage, info.items[0].thumbnail_key))
    .then((url) => { img.src = url; })
    .catch((err) => { tile.classList.add("broken"); console.error(personId, err); });

  gridEl.appendChild(tile);
}

function renderPhotoCard(item, index, personItems) {
  const card = document.createElement("div");
  card.className = "card";
  const img = document.createElement("img");
  img.alt = item.name || item.item_id;
  img.loading = "lazy";
  card.appendChild(img);
  card.addEventListener("click", () => openPhotoAt(personItems, index));

  getDownloadURL(ref(storage, item.thumbnail_key))
    .then((url) => { img.src = url; })
    .catch((err) => { card.classList.add("broken"); console.error(item.item_id, err); });

  gridEl.appendChild(card);
}

function renderPeopleGrid() {
  renderBreadcrumb(null);
  gridEl.innerHTML = "";
  gridEl.className = "grid people-grid";
  statusEl.textContent = people.size
    ? `${people.size} ${people.size === 1 ? "person" : "people"} detected.`
    : "No faces detected yet — publish with FACES_LIVE=1 to find some.";
  // Most-photographed first — the people actually worth looking at tend to
  // be the ones with the most photos, not whatever order Firestore returned.
  const sorted = [...people.entries()].sort((a, b) => b[1].items.length - a[1].items.length);
  for (const [personId, info] of sorted) renderPersonTile(personId, info);
}

function openPerson(personId) {
  const info = people.get(personId);
  renderBreadcrumb(personId);
  gridEl.innerHTML = "";
  gridEl.className = "grid";
  statusEl.textContent = personLabel(personId, info.items.length);
  info.items.forEach((item, i) => renderPhotoCard(item, i, info.items));
}

async function load() {
  statusEl.textContent = "Loading…";
  try {
    const snap = await getDocs(query(
      collection(db, "items"), orderBy("mtime", "desc"), limit(SCAN_LIMIT),
    ));
    people = new Map();
    for (const doc of snap.docs) {
      const item = doc.data();
      for (const personId of item.person_ids || []) {
        if (!people.has(personId)) people.set(personId, { items: [] });
        people.get(personId).items.push(item);
      }
    }
    renderPeopleGrid();
  } catch (err) {
    statusEl.textContent = `Failed to load: ${err.message}`;
    console.error(err);
  }
}

export function mount(container) {
  root = container;
  root.innerHTML = `
    <div class="folders-toolbar">
      <div class="breadcrumb"></div>
      <p class="view-status"></p>
    </div>
    <div class="grid people-grid"></div>
  `;
  breadcrumbEl = root.querySelector(".breadcrumb");
  statusEl = root.querySelector(".view-status");
  gridEl = root.querySelector(".people-grid");
  load();
}

export function unmount() {
  root.innerHTML = "";
}
