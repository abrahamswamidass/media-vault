// Map view: every geotagged photo, pinned. Most photos have no GPS at all
// (screenshots, edited exports, cameras with location off) — a range filter
// on latitude is what actually finds the minority that do, and needs no
// composite index since both clauses are on the one field (same trick
// Browse's year-jump uses on mtime — see that file's header comment).
//
// Leaflet + OpenStreetMap tiles: no API key, no cost, loaded from a CDN only
// when this view is actually opened (not on every page load).
import {
  collection, query, where, limit, getDocs,
} from "https://www.gstatic.com/firebasejs/10.14.1/firebase-firestore.js";
import { getDownloadURL, ref } from "https://www.gstatic.com/firebasejs/10.14.1/firebase-storage.js";
import { db, storage } from "../firebase.js";
import { stageForAmazon } from "../intents.js";
import { loadHiddenPrefixes, isHidden } from "../hiddenFolders.js";

// A browser map gets sluggish with tens of thousands of individual markers —
// this is a preview of where your library has been, not a full data dump.
const MAX_PINS = 2000;

let root = null;
let statusEl = null;
let mapEl = null;
let map = null;
let L = null;

async function ensureLeaflet() {
  if (L) return L;
  if (!document.querySelector("link[data-leaflet]")) {
    const link = document.createElement("link");
    link.rel = "stylesheet";
    link.href = "https://unpkg.com/leaflet@1.9.4/dist/leaflet.css";
    link.dataset.leaflet = "1";
    document.head.appendChild(link);
  }
  L = await import("https://esm.sh/leaflet@1.9.4");
  return L;
}

async function loadGeotaggedItems() {
  const [snap, hiddenPrefixes] = await Promise.all([
    getDocs(query(
      collection(db, "items"),
      where("latitude", ">=", -90), where("latitude", "<=", 90),
      limit(MAX_PINS),
    )),
    loadHiddenPrefixes(),
  ]);
  return snap.docs.map((d) => d.data()).filter((item) => !isHidden(item.item_id, hiddenPrefixes));
}

function popupContent(item) {
  const wrap = document.createElement("div");
  wrap.className = "map-popup";
  const img = document.createElement("img");
  getDownloadURL(ref(storage, item.thumbnail_key))
    .then((url) => { img.src = url; })
    .catch((err) => console.error(item.item_id, err));
  const name = document.createElement("div");
  name.className = "map-popup-name";
  name.textContent = item.item_id;

  const stageBtn = document.createElement("button");
  stageBtn.type = "button";
  stageBtn.className = "map-popup-stage";
  stageBtn.textContent = "Stage for Amazon";
  const status = document.createElement("div");
  status.className = "map-popup-status";
  stageBtn.addEventListener("click", async () => {
    stageBtn.disabled = true;
    stageBtn.textContent = "Staging…";
    try {
      await stageForAmazon(item);
      stageBtn.textContent = "Staged ✓";
      status.textContent = "See the Amazon tab once the agent picks it up.";
    } catch (err) {
      stageBtn.disabled = false;
      stageBtn.textContent = "Stage for Amazon";
      status.textContent = `Failed: ${err.message}`;
      console.error(item.item_id, err);
    }
  });

  wrap.append(img, name, stageBtn, status);
  return wrap;
}

function addMarkers(Lmod, items) {
  for (const item of items) {
    Lmod.marker([item.latitude, item.longitude]).addTo(map).bindPopup(() => popupContent(item));
  }
}

export async function mount(container) {
  root = container;
  root.innerHTML = `
    <p class="view-status">Loading map…</p>
    <div class="map-el"></div>
  `;
  statusEl = root.querySelector(".view-status");
  mapEl = root.querySelector(".map-el");

  try {
    const Lmod = await ensureLeaflet();
    map = Lmod.map(mapEl).setView([20, 0], 2);
    Lmod.tileLayer("https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png", {
      attribution: "&copy; OpenStreetMap contributors",
      maxZoom: 19,
    }).addTo(map);

    const items = await loadGeotaggedItems();
    if (!items.length) {
      statusEl.textContent = "No geotagged photos yet — most photos have no "
        + "GPS data, or publish hasn't run with location extraction yet.";
      return;
    }

    addMarkers(Lmod, items);
    map.fitBounds(Lmod.latLngBounds(items.map((i) => [i.latitude, i.longitude])).pad(0.1));
    const capped = items.length === MAX_PINS ? ` (showing the first ${MAX_PINS})` : "";
    statusEl.textContent = `${items.length} geotagged photo${items.length === 1 ? "" : "s"}${capped}.`;
  } catch (err) {
    statusEl.textContent = `Failed to load map: ${err.message}`;
    console.error(err);
  }
}

export function unmount() {
  if (map) {
    map.remove();
    map = null;
  }
  root.innerHTML = "";
}
