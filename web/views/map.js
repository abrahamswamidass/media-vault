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
// reach them. Instead we page in the *entire* geotagged set once and keep it
// in memory, then only cluster + render the slice inside the current map
// viewport, recomputed on every pan/zoom. That's what makes pins "appear" as
// you zoom into a place and "disappear" as you zoom back out.
//
// The geotagged set itself keeps growing as `publish` works through the
// library (~2.5K -> 11K+ within a couple weeks), so "the fetch is no longer
// the bottleneck" doesn't hold forever -- paging in ~20+ batches of 500 is a
// real, growing wait now. Pins appear page by page as that wait happens
// (see loadGeotaggedItems's onProgress) rather than only once everything's
// in, which is what makes the wait visibly progress instead of looking
// stuck. That's only safe because of GRID_CLUSTER_MAX_ZOOM below: the
// initial view is a wide world view, and re-clustering a growing
// multi-thousand-item "in view" set after every page would be a real O(n^2)
// cost if it ran the precise clusterByLocation() -- so wide zoom uses a
// cheap O(n) grid bucketing instead, and only switches to precise,
// real-meters clustering once zoomed in far enough that "in view" is
// naturally small.
import {
  collection, query, where, orderBy, startAfter, limit, getDocs,
} from "https://www.gstatic.com/firebasejs/10.14.1/firebase-firestore.js";
import { db } from "../firebase.js";
import { loadHiddenPrefixes, isHidden } from "../hiddenFolders.js";
import { openPhotoAt } from "../photoModal.js";

const PAGE_SIZE = 500;
// Not a normal-use ceiling -- even an actively-growing personal library's
// geotagged set (11K+ and counting) is well under this. Purely a safety
// valve against a runaway pagination loop.
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

// onProgress(itemsSoFar), if given, fires after each page with the
// growing items array itself (same reference, just longer) -- a growing
// library's geotagged set has gone from ~2.5K to over 11K in a couple
// weeks, meaning "Loading map..." with zero feedback for a real stretch of
// many sequential page-fetches started looking indistinguishable from
// stuck (see GitHub -- same "silent but working" problem index/dedup's
// own --debug already solved elsewhere). The caller uses this to both
// update the status text AND render pins as they arrive -- see
// GRID_CLUSTER_MAX_ZOOM for why that render is cheap even at 11K+ items.
// Skipped on a page that added nothing (including the very first page of
// a zero-geotagged library), so mount()'s "no geotagged photos yet"
// message never gets clobbered by an empty progress render.
async function loadGeotaggedItems(onProgress) {
  const hiddenPrefixes = await loadHiddenPrefixes();
  const items = [];
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
    for (const d of snap.docs) {
      const item = d.data();
      if (!isHidden(item.item_id, hiddenPrefixes)) items.push(item);
    }
    if (onProgress && items.length) onProgress(items);
    if (snap.docs.length < PAGE_SIZE || items.length >= SAFETY_MAX_ITEMS) break;
    cursor = snap.docs[snap.docs.length - 1];
  }
  return items;
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

// Below this zoom (roughly: a wide region, a country, or the whole world),
// swap the precise-but-O(n^2) clusterByLocation() for a cheap O(n) grid
// bucketing (below). Two independent reasons this matters, not one: it's
// what makes it safe to re-render on every incoming page during the
// initial load (see mount()) now that the geotagged set runs 11K+ and
// rising -- an O(n^2) pass that wide, that often, would visibly stall a
// phone. And separately, at a wide zoom a real 75m cluster radius is
// sub-pixel anyway, so a coarse grid loses nothing worth seeing. Once
// zoomed in past this, "in view" is naturally small (geography bounds it,
// not the total loaded count), so the precise version is cheap again.
const GRID_CLUSTER_MAX_ZOOM = 6;

// Degrees per grid cell, coarser at lower zoom -- same "good enough, not
// geographically exact" spirit as CLUSTER_RADIUS_METERS staying a flat
// 75m regardless of latitude.
function gridCellSizeForZoom(zoom) {
  if (zoom <= 2) return 10;
  if (zoom <= 4) return 5;
  return 2; // zoom 5-6
}

// O(n): bucket by lat/lng grid cell instead of comparing every point
// against every other. See GRID_CLUSTER_MAX_ZOOM for why this replaces
// clusterByLocation() at wide zoom rather than running everywhere.
function clusterByGrid(items, zoom) {
  const cell = gridCellSizeForZoom(zoom);
  const buckets = new Map();
  for (const item of items) {
    const key = `${Math.floor(item.latitude / cell)}:${Math.floor(item.longitude / cell)}`;
    let group = buckets.get(key);
    if (!group) {
      group = [];
      buckets.set(key, group);
    }
    group.push(item);
  }
  return [...buckets.values()];
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
// viewport, on every moveend and on every incoming page during the initial
// load -- replaces markers rather than the whole map, so there's no
// flicker. Below GRID_CLUSTER_MAX_ZOOM this is O(n) (grid bucketing); at or
// above it, it's the precise O(n^2) clusterByLocation(), safe because
// "in view" at that zoom is bounded by geography, not by how much of the
// library has loaded.
function renderVisible(Lmod) {
  // Guards the now-multi-page load: unmount() (navigating to another tab
  // mid-load) nulls map out from under a still-in-flight loadGeotaggedItems
  // page, and its onProgress callback would otherwise call this on a dead map.
  if (!map) return;
  const bounds = map.getBounds();
  const zoom = map.getZoom();
  const visible = allItems.filter((item) => bounds.contains([item.latitude, item.longitude]));
  markersLayer.clearLayers();
  const clusters = zoom <= GRID_CLUSTER_MAX_ZOOM
    ? clusterByGrid(visible, zoom)
    : clusterByLocation(visible);
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

    markersLayer = Lmod.layerGroup().addTo(map);
    map.on("moveend", () => renderVisible(Lmod));

    allItems = await loadGeotaggedItems((soFar) => {
      allItems = soFar;
      renderVisible(Lmod);
    });
    if (!allItems.length) {
      statusEl.textContent = "No geotagged photos yet — most photos have no "
        + "GPS data, or publish hasn't run with location extraction yet.";
      return;
    }

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
