// Browse view: chronological grid, grouped by year/month, paginated.
//
// Sorting/pagination is by `mtime` — every fact has it, and Firestore's
// orderBy silently excludes documents missing the ordered field, so ordering
// by date_taken would drop anything without EXIF (screenshots, older items
// published before EXIF extraction existed) from the list entirely.
//
// Grouping/display prefers `date_taken` (real EXIF capture date) per item
// when present, falling back to `mtime` otherwise — the more accurate field
// without risking dropped items. One accepted tradeoff: since the sort key
// (mtime) and group key (date_taken) can diverge slightly — e.g. a file
// copied weeks after it was taken — a group can rarely appear slightly out
// of strict order. Cosmetic only; not worth the complexity of reordering
// sections to fully eliminate.
import {
  collection, query, orderBy, limit, startAfter, where, getDocs,
} from "https://www.gstatic.com/firebasejs/10.14.1/firebase-firestore.js";
import { getDownloadURL, ref } from "https://www.gstatic.com/firebasejs/10.14.1/firebase-storage.js";
import { db, storage } from "../firebase.js";
import { openPhotoAt } from "../photoModal.js";

const PAGE_SIZE = 100;

let root = null;
let statusEl = null;
let groupsEl = null;
let loadMoreBtn = null;
let yearSelect = null;

let lastDoc = null;
let exhausted = false;
let loading = false;
// Jump target, in the same field/units the query actually orders by (mtime,
// not effectiveDate — see the file header on why those can diverge). null
// means "start from the newest item", same as the original behavior.
let jumpBeforeMtime = null;
const groupEls = new Map(); // "2026-08" -> <section> element, so pages merge into existing months

// Flat list in the exact order cards were appended — which is also visual
// reading order (rows fill left-to-right within a grid), so the modal's
// prev/next matches what the eye would do without it. Passed to
// openPhotoAt() so the modal knows what set to move through.
const renderedItems = [];

function effectiveDate(item) {
  return item.date_taken ?? item.mtime;
}

function groupKey(dateSeconds) {
  const d = new Date(dateSeconds * 1000);
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

  const index = renderedItems.length;
  renderedItems.push(item);
  card.addEventListener("click", () => openPhotoAt(renderedItems, index));

  getDownloadURL(ref(storage, item.thumbnail_key))
    .then((url) => { img.src = url; })
    .catch((err) => { card.classList.add("broken"); console.error(item.item_id, err); });

  return card;
}

async function loadPage() {
  if (loading || exhausted) return;
  loading = true;
  loadMoreBtn.textContent = "Loading…";
  loadMoreBtn.disabled = true;

  // A "<=" filter on the same field being ordered by (mtime) doesn't need a
  // composite index in Firestore — this is the standard range-query shape.
  const clauses = [collection(db, "items"), orderBy("mtime", "desc")];
  if (jumpBeforeMtime !== null) clauses.push(where("mtime", "<=", jumpBeforeMtime));
  if (lastDoc) clauses.push(startAfter(lastDoc));
  clauses.push(limit(PAGE_SIZE));

  const snap = await getDocs(query(...clauses));
  if (snap.empty) {
    exhausted = true;
    loadMoreBtn.hidden = true;
  } else {
    lastDoc = snap.docs[snap.docs.length - 1];
    for (const doc of snap.docs) {
      const item = doc.data();
      groupSection(groupKey(effectiveDate(item))).querySelector(".grid").appendChild(renderCard(item));
    }
    if (snap.docs.length < PAGE_SIZE) {
      exhausted = true;
      loadMoreBtn.hidden = true;
    }
  }

  loading = false;
  loadMoreBtn.textContent = "Load more";
  loadMoreBtn.disabled = false;
  statusEl.textContent = exhausted && groupEls.size === 0 ? "Nothing here." : "";
}

async function fetchYearRange() {
  // Range reflects mtime (the field actually queried), not effectiveDate —
  // consistent with how the jump filter itself has to work. Two 1-doc reads,
  // cheap regardless of library size.
  const [newestSnap, oldestSnap] = await Promise.all([
    getDocs(query(collection(db, "items"), orderBy("mtime", "desc"), limit(1))),
    getDocs(query(collection(db, "items"), orderBy("mtime", "asc"), limit(1))),
  ]);
  if (newestSnap.empty || oldestSnap.empty) return null;
  return {
    maxYear: new Date(newestSnap.docs[0].data().mtime * 1000).getFullYear(),
    minYear: new Date(oldestSnap.docs[0].data().mtime * 1000).getFullYear(),
  };
}

function populateYearSelect(range) {
  yearSelect.innerHTML = '<option value="">Newest</option>';
  if (!range) return;
  for (let y = range.maxYear; y >= range.minYear; y--) {
    const opt = document.createElement("option");
    opt.value = String(y);
    opt.textContent = String(y);
    yearSelect.appendChild(opt);
  }
}

function resetAndLoad() {
  lastDoc = null;
  exhausted = false;
  groupEls.clear();
  groupsEl.innerHTML = "";
  renderedItems.length = 0;
  loadMoreBtn.hidden = false;
  loadPage().catch((err) => {
    statusEl.textContent = `Failed to load: ${err.message}`;
    console.error(err);
  });
}

export function mount(container) {
  root = container;
  root.innerHTML = `
    <div class="browse-toolbar">
      <label>Jump to <select class="year-jump"><option value="">Newest</option></select></label>
      <p class="view-status"></p>
    </div>
    <div class="month-groups"></div>
    <button class="load-more">Load more</button>
  `;
  statusEl = root.querySelector(".view-status");
  groupsEl = root.querySelector(".month-groups");
  loadMoreBtn = root.querySelector(".load-more");
  yearSelect = root.querySelector(".year-jump");

  loadMoreBtn.addEventListener("click", loadPage);
  yearSelect.addEventListener("change", () => {
    const year = yearSelect.value;
    // "<= end of that year" (Dec 31, 23:59:59 local) so the jump lands on
    // the most recent item in the chosen year, not before it.
    jumpBeforeMtime = year ? new Date(Number(year), 11, 31, 23, 59, 59).getTime() / 1000 : null;
    resetAndLoad();
  });

  jumpBeforeMtime = null;
  resetAndLoad();
  fetchYearRange().then(populateYearSelect).catch((err) => console.error("year range:", err));
}

export function unmount() {
  root.innerHTML = "";
}
