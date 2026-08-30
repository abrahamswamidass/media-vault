// Duplicates view: near-duplicate photos grouped for review — a resize, a
// re-compression, a burst-sequence shot. Unlike exact dedup (mediavault
// dedup, full-content SHA-256, safe to auto-archive), a near-duplicate pair
// often differs in ways that matter — one might be the full-res original,
// the other a messenger-app copy — so this only ever shows groups; picking
// which (if any) to archive is a person's call, made one photo at a time.
//
// Grouped entirely client-side from the phash publish already writes (see
// imaging.phash()) — Firestore can't express "Hamming distance <= N", so
// this scans a bounded recent window (same shape People/Map/Folders already
// use), sorts by time, and buckets items within a time window whose hashes
// are close. Near-duplicates are overwhelmingly from the same moment/event,
// so the time window both keeps this cheap (no O(n^2) over the whole
// library) and matches how near-dups actually occur in practice.
import {
  collection, query, orderBy, limit, getDocs,
} from "https://www.gstatic.com/firebasejs/10.14.1/firebase-firestore.js";
import { getDownloadURL, ref } from "https://www.gstatic.com/firebasejs/10.14.1/firebase-storage.js";
import { db, storage } from "../firebase.js";
import { writeIntent } from "../intents.js";
import { openPhotoAt } from "../photoModal.js";

const SCAN_LIMIT = 5000;
const TIME_WINDOW_SECONDS = 3600; // near-dups are overwhelmingly same-event
const HAMMING_THRESHOLD = 10;     // out of 64 bits — a real re-compression/burst shot differs by well under this

let root = null;
let statusEl = null;
let listEl = null;
let groups = [];

function hamming(hexA, hexB) {
  let x = BigInt(`0x${hexA}`) ^ BigInt(`0x${hexB}`);
  let count = 0;
  while (x) {
    count += Number(x & 1n);
    x >>= 1n;
  }
  return count;
}

// Same split Browse/the photo modal already use: the Firestore query orders
// by mtime (guaranteed present — Firestore silently drops documents missing
// whatever field orderBy names), but grouping/display prefers the real
// capture time when EXIF gave one. A burst sequence is defined by capture
// time, not by whenever the files happened to land on the NAS -- an import
// or sync can compress or spread out mtimes in ways that don't reflect when
// the photos were actually taken.
function effectiveDate(item) {
  return item.date_taken ?? item.mtime ?? 0;
}

function groupNearDuplicates(items) {
  const withHash = items
    .filter((it) => it.phash)
    .sort((a, b) => effectiveDate(a) - effectiveDate(b));
  const used = new Set();
  const found = [];
  for (let i = 0; i < withHash.length; i++) {
    if (used.has(i)) continue;
    const base = withHash[i];
    const group = [base];
    for (let j = i + 1; j < withHash.length; j++) {
      if (used.has(j)) continue;
      const cand = withHash[j];
      // Sorted by effectiveDate — once a candidate is past the window,
      // everything after it is too, so this can stop scanning this base early.
      if (effectiveDate(cand) - effectiveDate(base) > TIME_WINDOW_SECONDS) break;
      if (hamming(base.phash, cand.phash) <= HAMMING_THRESHOLD) {
        group.push(cand);
        used.add(j);
      }
    }
    if (group.length > 1) found.push(group);
    used.add(i);
  }
  // Biggest groups (most redundant copies) first — the ones most worth a look.
  return found.sort((a, b) => b.length - a.length);
}

function renderPhoto(item, group) {
  const card = document.createElement("div");
  card.className = "card dup-card";

  const img = document.createElement("img");
  img.alt = item.name || item.item_id;
  img.loading = "lazy";
  img.addEventListener("click", () => openPhotoAt(group, group.indexOf(item)));
  card.appendChild(img);

  getDownloadURL(ref(storage, item.thumbnail_key))
    .then((url) => { img.src = url; })
    .catch((err) => { card.classList.add("broken"); console.error(item.item_id, err); });

  const archiveBtn = document.createElement("button");
  archiveBtn.type = "button";
  archiveBtn.className = "dup-archive";
  archiveBtn.textContent = "Archive";
  const status = document.createElement("div");
  status.className = "dup-archive-status";
  archiveBtn.addEventListener("click", async () => {
    archiveBtn.disabled = true;
    archiveBtn.textContent = "Archiving…";
    try {
      await writeIntent("delete", item.item_id, { source: item.source });
      archiveBtn.textContent = "Archived ✓";
      status.textContent = "Moved to trash once the agent picks it up — undo any time from Activity.";
    } catch (err) {
      archiveBtn.disabled = false;
      archiveBtn.textContent = "Archive";
      status.textContent = `Failed: ${err.message}`;
      console.error(item.item_id, err);
    }
  });
  card.append(archiveBtn, status);

  return card;
}

function renderGroup(group) {
  const section = document.createElement("section");
  section.className = "dup-group";
  const h2 = document.createElement("h2");
  h2.textContent = `${group.length} similar photos`;
  const grid = document.createElement("div");
  grid.className = "grid";
  for (const item of group) grid.appendChild(renderPhoto(item, group));
  section.append(h2, grid);
  return section;
}

async function load() {
  statusEl.textContent = "Scanning for near-duplicates…";
  listEl.innerHTML = "";
  try {
    const snap = await getDocs(query(
      collection(db, "items"), orderBy("mtime", "desc"), limit(SCAN_LIMIT),
    ));
    const items = snap.docs.map((d) => d.data());
    groups = groupNearDuplicates(items);

    if (!groups.length) {
      statusEl.textContent = items.length === SCAN_LIMIT
        ? `No near-duplicates found in the most recent ${SCAN_LIMIT.toLocaleString()} items.`
        : "No near-duplicates found.";
      return;
    }
    for (const group of groups) listEl.appendChild(renderGroup(group));
    statusEl.textContent = `${groups.length} group(s) of similar photos.`;
  } catch (err) {
    statusEl.textContent = `Failed to load: ${err.message}`;
    console.error(err);
  }
}

export function mount(container) {
  root = container;
  root.innerHTML = `
    <p class="view-status"></p>
    <div class="dup-groups"></div>
  `;
  statusEl = root.querySelector(".view-status");
  listEl = root.querySelector(".dup-groups");
  load();
}

export function unmount() {
  root.innerHTML = "";
}
