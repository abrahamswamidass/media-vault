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
//
// A single capped query (old approach: `limit(2000)`) truncates by whichever
// order Firestore happens to return docs in -- once the library passed 2000
// geotagged photos, whole locations could vanish from the map with no way to
// reach them. Instead we page in the *entire* geotagged set once (cheap: it's
// just lat/lng-bearing docs, and this is a single-user library) and keep it
// in memory, then only cluster + render the slice inside the current map
// viewport, recomputed on every pan/zoom. That's what makes pins "appear" as
// you zoom into a place and "disappear" as you zoom back out -- the fetch is
// no longer the bottleneck, the viewport is.
import {
  collection, query, where, orderBy, startAfter, limit, getDocs,
} from "https://www.gstatic.com/firebasejs/10.14.1/firebase-firestore.js";
import { db } from "../firebase.js";
import { loadHiddenPrefixes, isHidden } from "../hiddenFolders.js";
import { openPhotoAt } from "../photoModal.js";

const PAGE_SIZE = 500;
// Not a normal-use ceiling -- a personal library's geotagged set is a few
// thousand at most. Purely a safety valve against a runaway pagination loop.
const SAFETY_MAX_ITEMS = 50000;

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
let markersLayer = null;
let allItems = [];

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
  const hiddenPrefixesPromise = loadHiddenPrefixes();
  const docs = [];
  let cursor = null;
  for (;;) {
    const constraints = [
      where("latitude", ">=", -90), where("latitude", "<=", 90),
      orderBy("latitude"),
      limit(PAGE_SIZE),
    ];
    if (cursor) constraints.push(startAfter(cursor));
    // Sequential on purpose -- each page's cursor is the previous page's last doc.
    const snap = await getDocs(query(collection(db, "items"), ...constraints));
    docs.push(...snap.docs);
    if (snap.docs.length < PAGE_SIZE || docs.length >= SAFETY_MAX_ITEMS) break;
    cursor = snap.docs[snap.docs.length - 1];
  }
  const hiddenPrefixes = await hiddenPrefixesPromise;
  return docs.map((d) => d.data()).filter((item) => !isHidden(item.item_id, hiddenPrefixes));
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
// grouping: not O(n log n)-optimal, but this only ever runs over whatever's
// in the current viewport, which keeps the O(n^2) scan cheap.
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

function addMarkers(Lmod, clusters, layer) {
  for (const group of clusters) {
    // Leaflet's own default pin only applies when the `icon` option key is
    // absent entirely -- passing `icon: undefined` explicitly (even though
    // it's falsy) still overwrites that default during option merging, and
    // Leaflet then tries to call .createIcon() on undefined. So the key is
    // only ever added for an actual cluster, never included-but-empty.
    const options = group.length > 1 ? { icon: clusterIcon(Lmod, group.length) } : {};
    const marker = Lmod.marker(centroid(group), options).addTo(layer);
    // Straight to the shared modal, same click-to-open behavior Browse
    // already has, rather than a one-photo popup -- openPhotoAt gives the
    // whole cluster prev/next navigation (and swipe, on a phone) for free.
    marker.on("click", () => openPhotoAt(group, 0));
  }
}

// Re-clusters and redraws markers for whatever's currently inside the map's
// viewport. Cheap to call on every moveend -- allItems tops out in the low
// thousands for a personal library, and this replaces markers rather than
// the whole map, so there's no flicker.
function renderVisible(Lmod) {
  const bounds = map.getBounds();
  const visible = allItems.filter((item) => bounds.contains([item.latitude, item.longitude]));
  markersLayer.clearLayers();
  const clusters = clusterByLocation(visible);
  addMarkers(Lmod, clusters, markersLayer);
  const grouped = clusters.length < visible.length ? `, ${clusters.length} location(s)` : "";
  statusEl.textContent = `${visible.length} of ${allItems.length} geotagged photo${allItems.length === 1 ? "" : "s"} in view${grouped}. Pan or zoom to see more.`;
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

    allItems = await loadGeotaggedItems();
    if (!allItems.length) {
      statusEl.textContent = "No geotagged photos yet — most photos have no "
        + "GPS data, or publish hasn't run with location extraction yet.";
      return;
    }

    markersLayer = Lmod.layerGroup().addTo(map);
    map.on("moveend", () => renderVisible(Lmod));
    map.fitBounds(Lmod.latLngBounds(allItems.map((i) => [i.latitude, i.longitude])).pad(0.1));
    // fitBounds fires moveend itself once the view actually changes, but if
    // the view was already at that extent (e.g. a single point) it won't --
    // so render explicitly once too, rather than depending on that event.
    renderVisible(Lmod);
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
  markersLayer = null;
  allItems = [];
  root.innerHTML = "";
}
