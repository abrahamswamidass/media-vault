// Folders view: browse the library the way it's actually laid out on the
// NAS, not by date or location. Firestore's `items` collection is flat
// (keyed by content hash, not a real hierarchy), so "folders" here are
// computed client-side from item_id path segments as each page loads —
// there's no cheap way to ask Firestore "what are the subfolders of X"
// without reading every item under X at least once. Bounded the same way
// Browse bounds its own cost: paginated, "Load more" to see further in.
import {
  collection, query, orderBy, where, limit, startAfter, getDocs,
} from "https://www.gstatic.com/firebasejs/10.14.1/firebase-firestore.js";
import { getDownloadURL, ref } from "https://www.gstatic.com/firebasejs/10.14.1/firebase-storage.js";
import { db, storage } from "../firebase.js";
import { openPhotoAt } from "../photoModal.js";

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

let path = []; // [] = root; e.g. ["percial", "Photos", "2021"]
let lastDoc = null;
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
  const tile = document.createElement("div");
  tile.className = "folder-tile";
  tile.innerHTML = `${FOLDER_ICON}<span></span>`;
  tile.querySelector("span").textContent = name;
  tile.addEventListener("click", () => navigateTo([...path, name]));
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

function navigateTo(newPath) {
  path = newPath;
  lastDoc = null;
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
      const rest = item.item_id.slice(prefix.length);
      const slashAt = rest.indexOf("/");
      if (slashAt === -1) {
        renderFileCard(item);
      } else {
        const name = rest.slice(0, slashAt);
        if (!folderNames.has(name)) {
          folderNames.add(name);
          renderFolderTile(name);
        }
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
  navigateTo([]);
}

export function unmount() {
  root.innerHTML = "";
}
