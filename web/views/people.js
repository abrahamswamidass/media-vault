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

// insightface's det_score, roughly 0-1. Below this, a detection is more
// likely a false positive (a pattern/texture mistaken for a face) than a
// genuine low-quality photo of a real one — clusters this unsure about,
// or with only one photo ever (too little evidence either way), get
// pulled out of the main grid into one Unsorted bucket instead of
// cluttering it with zoomed-in crops of clothing or textures.
const LOW_SCORE_THRESHOLD = 0.5;

let root = null;
let breadcrumbEl = null;
let statusEl = null;
let gridEl = null;
let people = new Map(); // personId -> { entries: [{item, bbox, score}] }
let unsorted = []; // entries pulled out of `people` per isUnsorted() below

function personLabel(personId, count) {
  return `Person ${personId} · ${count} photo${count === 1 ? "" : "s"}`;
}

// A cluster this project has too little confidence in to treat as a real,
// distinct person: either the evidence is thin (a single photo, never
// matched again) or the representative detection itself scored low.
function isUnsorted(info) {
  if (info.entries.length === 1) return true;
  const score = info.entries[0].score;
  return typeof score === "number" && score < LOW_SCORE_THRESHOLD;
}

// Zooms the thumbnail toward the specific face this tile represents, using
// item.faces' normalized [x1,y1,x2,y2] (see maintenance.py's PublishAction).
//
// Sets BOTH object-position and a matching transform-origin, not
// transform-origin alone: object-fit:cover's own crop happens first and
// defaults to centering on the image's geometric middle, which can throw
// away the face entirely before any zoom ever runs -- a face near the edge
// of a landscape group photo, cropped into a square tile, can end up
// outside the cover-fitted region altogether, so a transform later has
// nothing but shoulders/clothing left to zoom into. Setting object-position
// to the face's own center makes the cover-crop itself center on the right
// spot first; the scale on top then zooms further into that same point.
function applyFaceCrop(img, bbox) {
  if (!bbox) return;
  const [x1, y1, x2, y2] = bbox;
  const cx = (x1 + x2) / 2;
  const cy = (y1 + y2) / 2;
  const faceSpan = Math.max(x2 - x1, y2 - y1, 0.05);
  // Zoom so the face's longer side fills ~60% of the tile, capped 1x-3x --
  // enough to actually isolate a face in a big group shot without going
  // absurd on a tiny, low-resolution detection.
  const zoom = Math.min(3, Math.max(1, 0.6 / faceSpan));
  img.style.objectPosition = `${cx * 100}% ${cy * 100}%`;
  img.style.transformOrigin = `${cx * 100}% ${cy * 100}%`;
  img.style.transform = `scale(${zoom})`;
}

function renderBreadcrumb(label) {
  breadcrumbEl.innerHTML = "";
  const home = document.createElement("button");
  home.type = "button";
  home.className = "crumb";
  home.textContent = "All people";
  home.disabled = !label;
  home.addEventListener("click", renderPeopleGrid);
  breadcrumbEl.appendChild(home);
  if (!label) return;

  const sep = document.createElement("span");
  sep.className = "crumb-sep";
  sep.textContent = "/";
  const current = document.createElement("button");
  current.type = "button";
  current.className = "crumb";
  current.textContent = label;
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
  label.textContent = personLabel(personId, info.entries.length);
  tile.append(img, label);
  tile.addEventListener("click", () => openPerson(personId));

  const cover = info.entries[0];
  applyFaceCrop(img, cover.bbox);
  getDownloadURL(ref(storage, cover.item.thumbnail_key))
    .then((url) => { img.src = url; })
    .catch((err) => { tile.classList.add("broken"); console.error(personId, err); });

  gridEl.appendChild(tile);
}

// Same shape as a person tile, but a plain (no face-crop) preview and a
// dashed border -- this isn't one person, it's a catch-all, and shouldn't
// look like an ordinary result at a glance.
function renderUnsortedTile() {
  const tile = document.createElement("div");
  tile.className = "card person-card unsorted";
  const img = document.createElement("img");
  img.alt = "Unsorted";
  img.loading = "lazy";
  const label = document.createElement("div");
  label.className = "person-label";
  label.textContent = `Unsorted · ${unsorted.length} photo${unsorted.length === 1 ? "" : "s"}`;
  tile.append(img, label);
  tile.addEventListener("click", openUnsorted);

  getDownloadURL(ref(storage, unsorted[0].item.thumbnail_key))
    .then((url) => { img.src = url; })
    .catch((err) => { tile.classList.add("broken"); console.error("unsorted", err); });

  gridEl.appendChild(tile);
}

function renderPhotoCard(entry, index, personItems) {
  const card = document.createElement("div");
  card.className = "card";
  const img = document.createElement("img");
  img.alt = entry.item.name || entry.item.item_id;
  img.loading = "lazy";
  card.appendChild(img);
  card.addEventListener("click", () => openPhotoAt(personItems, index));

  // No face crop here, deliberately -- unlike the "All people" cover tile,
  // you already know whose gallery this is once you've clicked in; zooming
  // every photo to just their face would throw away the actual photo
  // (the scene, who else is in it) for no benefit.
  getDownloadURL(ref(storage, entry.item.thumbnail_key))
    .then((url) => { img.src = url; })
    .catch((err) => { card.classList.add("broken"); console.error(entry.item.item_id, err); });

  gridEl.appendChild(card);
}

function renderPeopleGrid() {
  renderBreadcrumb(null);
  gridEl.innerHTML = "";
  gridEl.className = "grid people-grid";
  const total = people.size + (unsorted.length ? 1 : 0);
  statusEl.textContent = total
    ? `${people.size} ${people.size === 1 ? "person" : "people"} detected`
      + (unsorted.length ? `, ${unsorted.length} photo(s) unsorted.` : ".")
    : "No faces detected yet — publish with FACES_LIVE=1 to find some.";
  // Most-photographed first — the people actually worth looking at tend to
  // be the ones with the most photos, not whatever order Firestore returned.
  const sorted = [...people.entries()].sort((a, b) => b[1].entries.length - a[1].entries.length);
  for (const [personId, info] of sorted) renderPersonTile(personId, info);
  if (unsorted.length) renderUnsortedTile();
}

function openPerson(personId) {
  const info = people.get(personId);
  renderBreadcrumb(`Person ${personId}`);
  gridEl.innerHTML = "";
  gridEl.className = "grid";
  statusEl.textContent = personLabel(personId, info.entries.length);
  // openPhotoAt (prev/next navigation) needs the plain item list, not the
  // {item, bbox} pairs each grid tile itself needs for its own crop.
  const rawItems = info.entries.map((e) => e.item);
  info.entries.forEach((entry, i) => renderPhotoCard(entry, i, rawItems));
}

function openUnsorted() {
  renderBreadcrumb("Unsorted");
  gridEl.innerHTML = "";
  gridEl.className = "grid";
  statusEl.textContent = `${unsorted.length} unsorted photo${unsorted.length === 1 ? "" : "s"} `
    + "— a single appearance, or a low-confidence detection (possibly not a face at all).";
  const rawItems = unsorted.map((e) => e.item);
  unsorted.forEach((entry, i) => renderPhotoCard(entry, i, rawItems));
}

async function load() {
  statusEl.textContent = "Loading…";
  try {
    const snap = await getDocs(query(
      collection(db, "items"), orderBy("mtime", "desc"), limit(SCAN_LIMIT),
    ));
    const grouped = new Map();
    for (const doc of snap.docs) {
      const item = doc.data();
      // Prefer `faces` (person_id + this face's own bbox/score, see
      // maintenance.py's PublishAction) -- falls back to the older
      // `person_ids`-only shape for anything published before that field
      // existed, so those items keep showing up here, just without a
      // face-aware crop or a confidence score until republished.
      const faceList = item.faces && item.faces.length
        ? item.faces
        : (item.person_ids || []).map((personId) => ({ person_id: personId, bbox: null, score: null }));
      for (const face of faceList) {
        if (!grouped.has(face.person_id)) grouped.set(face.person_id, { entries: [] });
        grouped.get(face.person_id).entries.push({ item, bbox: face.bbox, score: face.score });
      }
    }

    people = new Map();
    unsorted = [];
    for (const [personId, info] of grouped) {
      if (isUnsorted(info)) unsorted.push(...info.entries);
      else people.set(personId, info);
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
