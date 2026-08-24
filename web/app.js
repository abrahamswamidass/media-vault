// Single-user viewer: sign in with Google, then read the agent's `items`
// Firestore collection and render thumbnails via Firebase Storage. Access is
// enforced server-side by firestore.rules / storage.rules — this file's
// OWNER_EMAIL check is only for picking which screen to show, not security.
import { initializeApp } from "https://www.gstatic.com/firebasejs/10.14.1/firebase-app.js";
import {
  getFirestore, collection, query, orderBy, limit, getDocs,
} from "https://www.gstatic.com/firebasejs/10.14.1/firebase-firestore.js";
import {
  getAuth, GoogleAuthProvider, signInWithPopup, signOut, onAuthStateChanged,
} from "https://www.gstatic.com/firebasejs/10.14.1/firebase-auth.js";
import {
  getStorage, ref, getDownloadURL,
} from "https://www.gstatic.com/firebasejs/10.14.1/firebase-storage.js";
import { firebaseConfig, FIRESTORE_DATABASE, GCS_BUCKET, OWNER_EMAIL } from "./firebase-config.js";

const PAGE_SIZE = 300;

const app = initializeApp(firebaseConfig);
const auth = getAuth(app);
const db = getFirestore(app, FIRESTORE_DATABASE);
const storage = getStorage(app, `gs://${GCS_BUCKET}`);

const statusEl = document.getElementById("status");
const gridEl = document.getElementById("grid");
const signinEl = document.getElementById("signin");
const signinBtn = document.getElementById("signin-btn");
const signoutBtn = document.getElementById("signout-btn");

signinBtn.addEventListener("click", () => {
  signInWithPopup(auth, new GoogleAuthProvider()).catch((err) => {
    statusEl.textContent = `Sign-in failed: ${err.message}`;
  });
});
signoutBtn.addEventListener("click", () => signOut(auth));

async function renderItems(items) {
  gridEl.innerHTML = "";
  for (const item of items) {
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

    gridEl.appendChild(card);

    // Fetched after the card is in the DOM so a slow/broken URL for one item
    // never blocks the rest of the grid from rendering.
    getDownloadURL(ref(storage, item.thumbnail_key))
      .then((url) => { img.src = url; })
      .catch((err) => { card.classList.add("broken"); console.error(item.item_id, err); });
  }
}

async function loadGallery() {
  statusEl.textContent = "Loading…";
  const q = query(collection(db, "items"), orderBy("mtime", "desc"), limit(PAGE_SIZE));
  const snap = await getDocs(q);
  const items = snap.docs.map((d) => d.data());
  statusEl.textContent = `${items.length} item(s)`;
  renderItems(items);
}

onAuthStateChanged(auth, (user) => {
  const signedIn = !!user && user.email === OWNER_EMAIL;

  signinEl.hidden = signedIn;
  gridEl.hidden = !signedIn;
  signoutBtn.hidden = !signedIn;

  if (!user) {
    statusEl.textContent = "Sign in to view your library.";
    return;
  }
  if (!signedIn) {
    statusEl.textContent = `${user.email} isn't authorized for this gallery.`;
    signOut(auth);
    return;
  }
  loadGallery().catch((err) => {
    statusEl.textContent = `Failed to load: ${err.message}`;
    console.error(err);
  });
});
