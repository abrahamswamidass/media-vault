// Folders view: browse the library the way it's actually laid out on the
// NAS, not by date or location. Firestore's `items` collection is flat
// (keyed by content hash, not a real hierarchy), so "folders" here are
// computed client-side from item_id path segments as each page loads —
// there's no cheap way to ask Firestore "what are the subfolders of X"
// without reading every item under X at least once. Bounded the same way
// Browse bounds its own cost: paginated, "Load more" to see further in.
import {
  collection, query, orderBy, where, limit, startAt, startAfter, getDocs,
} from "https://www.gstatic.com/firebasejs/10.14.1/firebase-firestore.js";
import { getDownloadURL, ref } from "https://www.gstatic.com/firebasejs/10.14.1/firebase-storage.js";
import { db, storage } from "../firebase.js";
import { openPhotoAt } from "../photoModal.js";
import { loadHiddenPrefixes, setFolderHidden } from "../hiddenFolders.js";

const PAGE_SIZE = 200; // higher than Browse's — most of a page here is cheap folder-name strings, not photo cards

const FOLDER_ICON = `<svg viewBox="0 0 20 16" width="20" height="16" aria-hidden="true">
  <path d="M1 2.5A1.5 1.5 0 0 1 2.5 1h4.4a1.5 1.5 0 0 1 1.06.44L9.4 2.9A.5.5 0 0 0 9.76 3H17.5A1.5 1.5 0 0 1 19 4.5v9A1.5 1.5 0 0 1 17.5 15h-15A1.5 1.5 0 0 1 1 13.5v-11Z"
        fill="currentColor"/>
</svg>`;

let root = null;
let statusEl = null;
let breadcrumbEl = null;
let foldersEl = null;
let filesGridEl = null;
let loadMoreBtn = null;

let hiddenPrefixes = new Set(); // path prefixes (each ending "/") hidden from Browse/Map

let path = []; // [] = root; e.g. ["percial", "Photos", "2021"]
// Value-based pagination cursor (a plain item_id string, not a document
// snapshot) -- lets a page jump straight past an entire discovered
// subfolder's contents (cursorMode "at") instead of always resuming
// immediately after the last document seen (cursorMode "after"). See
// loadPage()'s tail for which mode gets picked each time.
let cursor = null;
let cursorMode = "after";
let exhausted = false;
let loading = false;
const folderNames = new Set(); // subfolder names seen so far at the current path
const fileItems = []; // files seen so far at the current path — what the modal's prev/next moves through

function currentPrefix() {
  return path.length ? `${path.join("/")}/` : "";
}

function renderBreadcrumb() {
  breadcrumbEl.innerHTML = "";
  const home = document.createElement("button");
  home.type = "button";
  home.className = "crumb";
  home.textContent = "Home";
  home.disabled = path.length === 0;
  home.addEventListener("click", () => navigateTo([]));
  breadcrumbEl.appendChild(home);

  path.forEach((seg, i) => {
    const sep = document.createElement("span");
    sep.className = "crumb-sep";
    sep.textContent = "/";
    breadcrumbEl.appendChild(sep);

    const btn = document.createElement("button");
    btn.type = "button";
    btn.className = "crumb";
    btn.textContent = seg;
    btn.disabled = i === path.length - 1;
    btn.addEventListener("click", () => navigateTo(path.slice(0, i + 1)));
    breadcrumbEl.appendChild(btn);
  });
}

function renderFolderTile(name) {
  const folderPath = `${currentPrefix()}${name}/`;

  const tile = document.createElement("div");
  tile.className = "folder-tile";
  tile.innerHTML = `
    <label class="folder-hide" title="Hide from Browse and Map">
      <input type="checkbox" />
    </label>
    ${FOLDER_ICON}<span></span>
  `;
  tile.querySelector("span").textContent = name;
  tile.addEventListener("click", () => navigateTo([...path, name]));

  const checkbox = tile.querySelector("input");
  checkbox.checked = hiddenPrefixes.has(folderPath);
  checkbox.addEventListener("click", (e) => e.stopPropagation());
  checkbox.addEventListener("change", async () => {
    checkbox.disabled = true;
    try {
      await setFolderHidden(folderPath, checkbox.checked);
      if (checkbox.checked) hiddenPrefixes.add(folderPath);
      else hiddenPrefixes.delete(folderPath);
    } catch (err) {
      checkbox.checked = !checkbox.checked;
      console.error(folderPath, err);
      statusEl.textContent = `Failed to update: ${err.message}`;
    }
    checkbox.disabled = false;
  });

  foldersEl.appendChild(tile);
}

function renderFileCard(item) {
  const card = document.createElement("div");
  card.className = "card";

  const img = document.createElement("img");
  img.alt = item.name || item.item_id;
  img.loading = "lazy";
  card.appendChild(img);

  const meta = document.createElement("div");
  meta.className = "meta";
  meta.textContent = item.name || item.item_id;
  meta.title = item.item_id;
  card.appendChild(meta);

  const index = fileItems.length;
  fileItems.push(item);
  card.addEventListener("click", () => openPhotoAt(fileItems, index));

  getDownloadURL(ref(storage, item.thumbnail_key))
    .then((url) => { img.src = url; })
    .catch((err) => { card.classList.add("broken"); console.error(item.item_id, err); });

  filesGridEl.appendChild(card);
}

function pathToHash(newPath) {
  const encoded = newPath.map(encodeURIComponent).join("/");
  return encoded ? `#folders/${encoded}` : "#folders";
}

function pathFromHash() {
  const parts = location.hash.replace(/^#/, "").split("/").filter(Boolean);
  parts.shift(); // drop the "folders" view segment itself
  return parts.map(decodeURIComponent);
}

// The actual state change — reset pagination, re-render — with no opinion
// on the URL. Called both on a fresh navigation and by onHashChange when
// the browser's Back/Forward button (or a pasted link) lands here directly.
function applyPath(newPath) {
  path = newPath;
  cursor = null;
  cursorMode = "after";
  exhausted = false;
  folderNames.clear();
  fileItems.length = 0;
  foldersEl.innerHTML = "";
  filesGridEl.innerHTML = "";
  loadMoreBtn.hidden = false;
  renderBreadcrumb();
  loadPage().catch((err) => {
    statusEl.textContent = `Failed to load: ${err.message}`;
    console.error(err);
  });
}

// Click-driven navigation: pushes a history entry via location.hash (a
// hashchange fires back into onHashChange below, which does the actual
// work) so Back steps out one folder level at a time. Re-clicking the
// folder already open would produce an identical hash and thus no
// hashchange event, so that case applies directly instead.
function navigateTo(newPath) {
  const hash = pathToHash(newPath);
  if (location.hash === hash) applyPath(newPath);
  else location.hash = hash;
}

export function onHashChange() {
  applyPath(pathFromHash());
}

async function loadPage() {
  if (loading || exhausted) return;
  loading = true;
  loadMoreBtn.textContent = "Loading…";
  loadMoreBtn.disabled = true;

  const prefix = currentPrefix();
  // A ">=" / "<" pair on the same field being ordered by (item_id) doesn't
  // need a composite index — same range-query shape Browse's year-jump and
  // Map's geotag filter already rely on. "" sorts after any realistic
  // path character, so this is the standard Firestore "starts with" trick.
  const clauses = [
    collection(db, "items"), orderBy("item_id"),
    where("item_id", ">=", prefix), where("item_id", "<", `${prefix}`),
  ];
  if (cursor !== null) {
    clauses.push(cursorMode === "at" ? startAt(cursor) : startAfter(cursor));
  }
  clauses.push(limit(PAGE_SIZE));

  const snap = await getDocs(query(...clauses));
  if (snap.empty) {
    exhausted = true;
    loadMoreBtn.hidden = true;
  } else {
    for (const doc of snap.docs) {
      const item = doc.data();
      const rest = item.item_id.slice(prefix.length);
      const slashAt = rest.indexOf("/");
      if (slashAt === -1) {
        renderFileCard(item);
        cursor = item.item_id;
        cursorMode = "after";
      } else {
        const name = rest.slice(0, slashAt);
        if (!folderNames.has(name)) {
          folderNames.add(name);
          renderFolderTile(name);
        }
        // Jump past this entire subfolder's contents on the next page
        // instead of paging through them document-by-document -- once a
        // subfolder is known, nothing inside it can teach us about another
        // sibling at this level. "" is the same upper-bound sentinel
        // the range query above uses, so this cursor value sorts after
        // every possible item_id under `name/`.
        cursor = `${prefix}${name}/`;
        cursorMode = "at";
      }
    }
    if (snap.docs.length < PAGE_SIZE) {
      exhausted = true;
      loadMoreBtn.hidden = true;
    }
  }

  loading = false;
  loadMoreBtn.textContent = "Load more";
  loadMoreBtn.disabled = false;
  statusEl.textContent = exhausted && !folderNames.size && !fileItems.length ? "Nothing here." : "";
}

export function mount(container) {
  root = container;
  root.innerHTML = `
    <div class="folders-toolbar">
      <div class="breadcrumb"></div>
      <p class="view-status"></p>
    </div>
    <div class="folder-grid"></div>
    <div class="grid file-grid"></div>
    <button class="load-more">Load more</button>
  `;
  statusEl = root.querySelector(".view-status");
  breadcrumbEl = root.querySelector(".breadcrumb");
  foldersEl = root.querySelector(".folder-grid");
  filesGridEl = root.querySelector(".file-grid");
  loadMoreBtn = root.querySelector(".load-more");

  loadMoreBtn.addEventListener("click", loadPage);
  loadHiddenPrefixes()
    .then((prefixes) => { hiddenPrefixes = prefixes; })
    .catch((err) => console.error("hidden folders:", err))
    .finally(() => applyPath(pathFromHash()));
}

export function unmount() {
  root.innerHTML = "";
}
