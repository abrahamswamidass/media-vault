// Minimal read-only viewer: query the agent's `items` collection in Firestore
// and render a grid of thumbnails from Cloud Storage. No auth, no writes — the
// web module only ever reads what the agent already published (see the
// agent's `mediavault publish` command). This is a placeholder MVP; CLAUDE.md
// still calls for a proper React app on Firebase Hosting eventually.
import { initializeApp } from "https://www.gstatic.com/firebasejs/10.14.1/firebase-app.js";
import {
  getFirestore, collection, query, orderBy, limit, getDocs,
} from "https://www.gstatic.com/firebasejs/10.14.1/firebase-firestore.js";
import { firebaseConfig, FIRESTORE_DATABASE, GCS_BUCKET } from "./firebase-config.js";

const PAGE_SIZE = 300;

const statusEl = document.getElementById("status");
const gridEl = document.getElementById("grid");

function thumbnailUrl(thumbnailKey) {
  // thumbs/ is the only public-read prefix on the bucket (see docs/setup.md) —
  // previews/ and originals stay private.
  return `https://storage.googleapis.com/${GCS_BUCKET}/${thumbnailKey}`;
}

function renderItems(items) {
  gridEl.innerHTML = "";
  for (const item of items) {
    const card = document.createElement("div");
    card.className = "card";

    const img = document.createElement("img");
    img.src = thumbnailUrl(item.thumbnail_key);
    img.alt = item.name || item.item_id;
    img.loading = "lazy";
    card.appendChild(img);

    const meta = document.createElement("div");
    meta.className = "meta";
    meta.textContent = item.item_id;
    meta.title = item.item_id;
    card.appendChild(meta);

    gridEl.appendChild(card);
  }
}

async function main() {
  try {
    const app = initializeApp(firebaseConfig);
    const db = getFirestore(app, FIRESTORE_DATABASE);

    const q = query(collection(db, "items"), orderBy("mtime", "desc"), limit(PAGE_SIZE));
    const snap = await getDocs(q);
    const items = snap.docs.map((d) => d.data());

    statusEl.textContent = `${items.length} item(s)`;
    renderItems(items);
  } catch (err) {
    statusEl.textContent = `Failed to load: ${err.message}`;
    console.error(err);
  }
}

main();
