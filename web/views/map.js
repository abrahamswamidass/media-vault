// Map view: every geotagged photo, pinned. Most photos have no GPS at all
// (screenshots, edited exports, cameras with location off) — a range filter
// on latitude is what actually finds the minority that do, and needs no
// composite index since both clauses are on the one field (same trick
// Browse's year-jump uses on mtime — see that file's header comment).
//
// Photos taken within a few dozen meters of each other (the same room, the
// same event) get grouped under one marker rather than stacking unusable
// individual pins on top of each other — clicking it opens the shared photo
// modal on the whole group, giving the same left/right (or swipe)
// navigation Browse already has, instead of a one-photo-at-a-time popup.
//
// Leaflet + OpenStreetMap tiles: no API key, no cost, loaded from a CDN only
// when this view is actually opened (not on every page load).
import {
  collection, query, where, limit, getDocs,
} from "https://www.gstatic.com/firebasejs/10.14.1/firebase-firestore.js";
import { db } from "../firebase.js";
import { loadHiddenPrefixes, isHidden } from "../hiddenFolders.js";
import { openPhotoAt } from "../photoModal.js";

// A browser map gets sluggish with tens of thousands of individual markers —
// this is a preview of where your library has been, not a full data dump.
const MAX_PINS = 2000;

// Close enough to be "the same spot" (a room, a booth, a grave site) without
// merging genuinely different nearby locations (the next building over).
// Real-world meters, not raw lat/lng degrees -- a degree of longitude is a
// very different distance depending on latitude, so a naive coordinate
// threshold would cluster inconsistently around the world.
const CLUSTER_RADIUS_METERS = 75;

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

// Haversine distance in meters -- real-world distance, not raw coordinate
// difference (see CLUSTER_RADIUS_METERS above for why that matters).
function metersBetween(a, b) {
  const R = 6371000;
  const toRad = (d) => (d * Math.PI) / 180;
  const dLat = toRad(b.latitude - a.latitude);
  const dLng = toRad(b.longitude - a.longitude);
  const lat1 = toRad(a.latitude);
  const lat2 = toRad(b.latitude);
  const h = Math.sin(dLat / 2) ** 2 + Math.cos(lat1) * Math.cos(lat2) * Math.sin(dLng / 2) ** 2;
  return 2 * R * Math.asin(Math.sqrt(h));
}

// Greedy proximity clustering, same shape as duplicates.js's near-dup
// grouping: not O(n log n)-optimal, but MAX_PINS caps this at a size where
// the simple O(n^2) scan is cheap enough not to matter.
function clusterByLocation(items) {
  const used = new Set();
  const clusters = [];
  for (let i = 0; i < items.length; i++) {
    if (used.has(i)) continue;
    const group = [items[i]];
    used.add(i);
    for (let j = i + 1; j < items.length; j++) {
      if (used.has(j)) continue;
      if (metersBetween(items[i], items[j]) <= CLUSTER_RADIUS_METERS) {
        group.push(items[j]);
        used.add(j);
      }
    }
    clusters.push(group);
  }
  return clusters;
}

function centroid(group) {
  const lat = group.reduce((sum, i) => sum + i.latitude, 0) / group.length;
  const lng = group.reduce((sum, i) => sum + i.longitude, 0) / group.length;
  return [lat, lng];
}

// A small numbered badge for a cluster of more than one photo, so it's
// obvious before clicking that there's more than one there.
function clusterIcon(Lmod, count) {
  return Lmod.divIcon({
    className: "map-cluster-icon",
    html: `<span>${count}</span>`,
    iconSize: [28, 28],
  });
}

function addMarkers(Lmod, clusters) {
  for (const group of clusters) {
    // Leaflet's own default pin only applies when the `icon` option key is
    // absent entirely -- passing `icon: undefined` explicitly (even though
    // it's falsy) still overwrites that default during option merging, and
    // Leaflet then tries to call .createIcon() on undefined. So the key is
    // only ever added for an actual cluster, never included-but-empty.
    const options = group.length > 1 ? { icon: clusterIcon(Lmod, group.length) } : {};
    const marker = Lmod.marker(centroid(group), options).addTo(map);
    // Straight to the shared modal, same click-to-open behavior Browse
    // already has, rather than a one-photo popup -- openPhotoAt gives the
    // whole cluster prev/next navigation (and swipe, on a phone) for free.
    marker.on("click", () => openPhotoAt(group, 0));
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

    const clusters = clusterByLocation(items);
    addMarkers(Lmod, clusters);
    map.fitBounds(Lmod.latLngBounds(items.map((i) => [i.latitude, i.longitude])).pad(0.1));
    const capped = items.length === MAX_PINS ? ` (showing the first ${MAX_PINS})` : "";
    const grouped = clusters.length < items.length ? `, ${clusters.length} location(s)` : "";
    statusEl.textContent = `${items.length} geotagged photo${items.length === 1 ? "" : "s"}${grouped}${capped}.`;
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
