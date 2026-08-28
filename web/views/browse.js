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
import { stageForAmazon } from "../intents.js";

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

// Flat list in the exact order cards were appended — which is also visual
// reading order (rows fill left-to-right within a grid), so "next"/"prev"
// in the modal matches what the eye would do without it. Only covers items
// actually rendered so far; navigating past the last one just disables
// "next" rather than triggering a fetch, so it never surprises with a
// network call the user didn't ask for.
const renderedItems = [];
let currentIndex = -1;

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
  card.addEventListener("click", () => showAt(index));

  getDownloadURL(ref(storage, item.thumbnail_key))
    .then((url) => { img.src = url; })
    .catch((err) => { card.classList.add("broken"); console.error(item.item_id, err); });

  return card;
}

// Everything about an item lives in one collapsed-by-default panel — a full
// path read as a page title (the old layout) doesn't mean anything at a
// glance, and ate most of the screen on a phone before the photo itself was
// even visible. name/path here, not a headline.
function renderDetails(item) {
  const dl = modal.querySelector(".modal-details");
  dl.innerHTML = "";
  const camera = [item.camera_make, item.camera_model].filter(Boolean).join(" ");
  const dims = item.width && item.height ? `${item.width}×${item.height}` : null;
  const rows = [
    ["Name", item.name],
    ["Path", item.item_id],
    ["Taken", new Date(effectiveDate(item) * 1000).toLocaleString()],
    ["Dimensions", dims],
    ["Size", item.size ? `${(item.size / 1024).toFixed(0)} KB` : null],
    ["Camera", camera || null],
    ["Source", item.source],
    ["Type", item.mime],
    ["Quick hash", item.quick_hash],
    ["Thumbnail key", item.thumbnail_key],
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

function closeMenu() {
  modal.querySelector(".modal-menu").hidden = true;
}

function toggleDetails() {
  modal.querySelector(".modal-details").hidden = !modal.querySelector(".modal-details").hidden;
}

function openModal(item) {
  const img = modal.querySelector("img");
  img.src = "";
  getDownloadURL(ref(storage, item.thumbnail_key))
    .then((url) => { img.src = url; })
    .catch((err) => console.error(item.item_id, err));

  renderDetails(item);
  modal.querySelector(".modal-details").hidden = true;
  closeMenu();
  modal.querySelector(".modal-prev").disabled = currentIndex <= 0;
  modal.querySelector(".modal-next").disabled = currentIndex >= renderedItems.length - 1;
  const stageBtn = modal.querySelector(".modal-stage-amazon");
  stageBtn.disabled = false;
  stageBtn.textContent = "Stage for Amazon";
  modal.querySelector(".modal-stage-status").textContent = "";
  modal.hidden = false;
}

async function handleStageForAmazon() {
  const item = renderedItems[currentIndex];
  if (!item) return;
  closeMenu();
  const btn = modal.querySelector(".modal-stage-amazon");
  const status = modal.querySelector(".modal-stage-status");
  btn.disabled = true;
  btn.textContent = "Staging…";
  try {
    await stageForAmazon(item);
    btn.textContent = "Staged ✓";
    status.textContent = "Waiting for the agent to pick it up — see the Amazon tab.";
  } catch (err) {
    btn.disabled = false;
    btn.textContent = "Stage for Amazon";
    status.textContent = `Failed: ${err.message}`;
    console.error(item.item_id, err);
  }
}

function showAt(index) {
  if (index < 0 || index >= renderedItems.length) return;
  currentIndex = index;
  openModal(renderedItems[index]);
}

function handleKeydown(e) {
  if (modal.hidden) return;
  if (e.key === "ArrowLeft") showAt(currentIndex - 1);
  else if (e.key === "ArrowRight") showAt(currentIndex + 1);
  else if (e.key === "Escape") modal.hidden = true;
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
  currentIndex = -1;
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
        <div class="modal-topbar">
          <div class="modal-topbar-spacer"></div>
          <button class="modal-nav modal-prev" type="button" aria-label="Previous">&lsaquo;</button>
          <button class="modal-nav modal-next" type="button" aria-label="Next">&rsaquo;</button>
          <div class="modal-menu-wrap">
            <button class="modal-nav modal-menu-toggle" type="button" aria-label="More actions">&#8942;</button>
            <div class="modal-menu" hidden>
              <button class="modal-fullres" disabled
                title="Not wired up yet — the agent-side processor now exists (process-intents), this button just doesn't call it yet.">
                Request full-res (coming soon)
              </button>
              <button class="modal-stage-amazon" type="button">Stage for Amazon</button>
            </div>
          </div>
          <button class="modal-nav modal-close" type="button" aria-label="Close">&times;</button>
        </div>
        <div class="modal-media">
          <img alt="" title="Tap for details" />
        </div>
        <p class="modal-stage-status"></p>
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
  modal.addEventListener("click", (e) => {
    // Outside the ⋮ menu (but still inside the modal) closes just the menu,
    // not the whole modal — e.g. tapping the photo while the menu is open.
    if (!modal.querySelector(".modal-menu-wrap").contains(e.target)) closeMenu();
    if (e.target === modal) modal.hidden = true;
  });
  modal.querySelector("img").addEventListener("click", toggleDetails);
  modal.querySelector(".modal-menu-toggle").addEventListener("click", (e) => {
    e.stopPropagation(); // don't let the modal-level listener above immediately re-close it
    modal.querySelector(".modal-menu").hidden = !modal.querySelector(".modal-menu").hidden;
  });
  modal.querySelector(".modal-prev").addEventListener("click", () => showAt(currentIndex - 1));
  modal.querySelector(".modal-next").addEventListener("click", () => showAt(currentIndex + 1));
  modal.querySelector(".modal-stage-amazon").addEventListener("click", handleStageForAmazon);
  // Attached to document (not the modal) since arrow keys should work
  // regardless of what currently has focus. Removed in unmount() — a
  // document-level listener outlives this view's own DOM otherwise, and
  // remounting Browse (nav away and back) would stack a duplicate on top of
  // it instead of replacing it, each one firing on every keypress after that.
  document.addEventListener("keydown", handleKeydown);
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
  document.removeEventListener("keydown", handleKeydown);
  root.innerHTML = "";
}
