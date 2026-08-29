// App shell: sign-in gate + hash-based router between views. No framework —
// each view module exports mount(container)/unmount(container); the router
// just swaps which one owns #view.
import {
  GoogleAuthProvider, signInWithPopup, signOut, onAuthStateChanged,
} from "https://www.gstatic.com/firebasejs/10.14.1/firebase-auth.js";
import { auth } from "./firebase.js";
import { ALLOWED_EMAILS } from "./firebase-config.js";
import { closePhotoModal } from "./photoModal.js";
import * as agentHeartbeat from "./agentHeartbeat.js";
import * as browseView from "./views/browse.js";
import * as mapView from "./views/map.js";
import * as foldersView from "./views/folders.js";
import * as duplicatesView from "./views/duplicates.js";
import * as amazonView from "./views/amazon.js";

const VIEWS = {
  browse: browseView, map: mapView, folders: foldersView,
  duplicates: duplicatesView, amazon: amazonView,
};
const DEFAULT_VIEW = "browse";

const statusEl = document.getElementById("status");
const navEl = document.getElementById("nav");
const viewEl = document.getElementById("view");
const signinEl = document.getElementById("signin");
const signinBtn = document.getElementById("signin-btn");
const signoutBtn = document.getElementById("signout-btn");
const heartbeatEl = document.getElementById("agent-heartbeat");

let currentView = null;

signinBtn.addEventListener("click", () => {
  signInWithPopup(auth, new GoogleAuthProvider()).catch((err) => {
    statusEl.textContent = `Sign-in failed: ${err.message}`;
  });
});
signoutBtn.addEventListener("click", () => signOut(auth));

function viewNameFromHash() {
  const name = location.hash.replace("#", "");
  return VIEWS[name] ? name : DEFAULT_VIEW;
}

function route() {
  const name = viewNameFromHash();
  if (name === currentView) return;

  // The photo modal is a body-level singleton (see photoModal.js), not
  // owned by any one view — without this, it could stay open floating
  // over whichever view you navigate to next.
  closePhotoModal();
  if (currentView) VIEWS[currentView].unmount(viewEl);
  currentView = name;
  for (const link of navEl.querySelectorAll("a")) {
    link.classList.toggle("active", link.dataset.view === name);
  }
  VIEWS[name].mount(viewEl);
}

window.addEventListener("hashchange", route);

onAuthStateChanged(auth, (user) => {
  const signedIn = !!user && ALLOWED_EMAILS.includes(user.email);

  signinEl.hidden = signedIn;
  navEl.hidden = !signedIn;
  viewEl.hidden = !signedIn;
  signoutBtn.hidden = !signedIn;
  heartbeatEl.hidden = !signedIn;

  if (!user) {
    agentHeartbeat.stop();
    statusEl.textContent = "Sign in to view your library.";
    return;
  }
  if (!signedIn) {
    agentHeartbeat.stop();
    statusEl.textContent = `${user.email} isn't authorized for this gallery.`;
    signOut(auth);
    return;
  }
  statusEl.textContent = "";
  agentHeartbeat.start(heartbeatEl);
  if (!location.hash) location.hash = `#${DEFAULT_VIEW}`;
  route();
});
