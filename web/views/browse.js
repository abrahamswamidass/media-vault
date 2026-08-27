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

const PAGE_SIZE = 100;

let root = null;
let statusEl = null;
let groupsEl = null;
let loadMoreBtn = null;
let modal = null;
let yearSelect = null;

let lastDoc = null;
let exhausted = false;
let loading = false;
// Jump target, in the same field/units the query actually orders by (mtime,
// not effectiveDate — see the file header on why those can diverge). null
// means "start from the newest item", same as the original behavior.
let jumpBeforeMtime = null;
const groupEls = new Map(); // "2026-08" -> <section> element, so pages merge into existing months

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

  card.addEventListener("click", () => openModal(item, img.src));

  getDownloadURL(ref(storage, item.thumbnail_key))
    .then((url) => { img.src = url; })
    .catch((err) => { card.classList.add("broken"); console.error(item.item_id, err); });

  return card;
}

//: field, label — in display order. Blank/undefined values are skipped.
const DETAIL_FIELDS = [
  ["source", "Source"],
  ["mime", "Type"],
  ["quick_hash", "Quick hash"],
  ["thumbnail_key", "Thumbnail key"],
];

function renderDetails(item) {
  const dl = modal.querySelector(".modal-details");
  dl.innerHTML = "";
  const rows = [
    ...DETAIL_FIELDS.map(([field, label]) => [label, item[field]]),
    ["Modified", item.mtime ? new Date(item.mtime * 1000).toLocaleString() : null],
  ];
  for (const [label, value] of rows) {
    if (!value) continue;
    const dt = document.createElement("dt");
    dt.textContent = label;
    const dd = document.createElement("dd");
    dd.textContent = value;
    dl.append(dt, dd);
  }
}

function openModal(item, thumbUrl) {
  modal.querySelector("img").src = thumbUrl || "";
  modal.querySelector(".modal-title").textContent = item.item_id;
  const camera = [item.camera_make, item.camera_model].filter(Boolean).join(" ");
  const dims = item.width && item.height ? `${item.width}×${item.height} · ` : "";
  modal.querySelector(".modal-meta").textContent =
    `${dims}${(item.size / 1024).toFixed(0)} KB · `
    + `${new Date(effectiveDate(item) * 1000).toLocaleString()}`
    + (camera ? ` · ${camera}` : "");
  renderDetails(item);
  modal.querySelector(".modal-details").hidden = true;
  modal.querySelector(".modal-details-toggle").textContent = "Details ▾";
  modal.hidden = false;
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
        <button class="modal-details-toggle" type="button">Details ▾</button>
        <dl class="modal-details" hidden></dl>
      </div>
    </div>
  `;
  statusEl = root.querySelector(".view-status");
  groupsEl = root.querySelector(".month-groups");
  loadMoreBtn = root.querySelector(".load-more");
  modal = root.querySelector(".modal");
  yearSelect = root.querySelector(".year-jump");

  loadMoreBtn.addEventListener("click", loadPage);
  modal.querySelector(".modal-close").addEventListener("click", () => { modal.hidden = true; });
  modal.addEventListener("click", (e) => { if (e.target === modal) modal.hidden = true; });
  modal.querySelector(".modal-details-toggle").addEventListener("click", () => {
    const details = modal.querySelector(".modal-details");
    details.hidden = !details.hidden;
    modal.querySelector(".modal-details-toggle").textContent = details.hidden ? "Details ▾" : "Details ▴";
  });
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
