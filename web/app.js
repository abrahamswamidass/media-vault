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
import * as push from "./push.js";
import * as browseView from "./views/browse.js";
import * as mapView from "./views/map.js";
import * as foldersView from "./views/folders.js";
import * as peopleView from "./views/people.js";
import * as duplicatesView from "./views/duplicates.js";
import * as amazonView from "./views/amazon.js";
import * as activityView from "./views/activity.js";

// Plain stroked outlines (fill: none, stroke: currentColor) instead of the
// 🔔/🔕 emoji characters — those two render in Apple's full-color emoji
// font no matter what CSS color says (there's no text-only fallback iOS
// actually honors here), which clashed hard with the rest of this app's
// monochrome icon set (the ☰ hamburger, the LED grid). An inline SVG
// inherits `color` like any other text, so it stays monochrome.
const BELL_ON_SVG = `<svg viewBox="0 0 24 24" width="18" height="18" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M18 8a6 6 0 0 0-12 0c0 7-3 9-3 9h18s-3-2-3-9"/><path d="M13.73 21a2 2 0 0 1-3.46 0"/></svg>`;
const BELL_OFF_SVG = `<svg viewBox="0 0 24 24" width="18" height="18" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M13.73 21a2 2 0 0 1-3.46 0"/><path d="M18.63 13A17.89 17.89 0 0 1 18 8"/><path d="M6.26 6.26A5.86 5.86 0 0 0 6 8c0 7-3 9-3 9h14"/><path d="M18 8a6 6 0 0 0-9.33-5"/><line x1="1" y1="1" x2="23" y2="23"/></svg>`;

const VIEWS = {
  browse: browseView, map: mapView, folders: foldersView, people: peopleView,
  duplicates: duplicatesView, amazon: amazonView, activity: activityView,
};
const DEFAULT_VIEW = "browse";

const statusEl = document.getElementById("status");
const navMenuWrapEl = document.getElementById("nav-menu-wrap");
const navToggleBtn = document.getElementById("nav-toggle");
const navEl = document.getElementById("nav");
const viewEl = document.getElementById("view");
const signinEl = document.getElementById("signin");
const signinBtn = document.getElementById("signin-btn");
const signoutBtn = document.getElementById("signout-btn");
const heartbeatEl = document.getElementById("agent-heartbeat");
const notifyToggleBtn = document.getElementById("notify-toggle");

let currentView = null;

signinBtn.addEventListener("click", () => {
  signInWithPopup(auth, new GoogleAuthProvider()).catch((err) => {
    statusEl.textContent = `Sign-in failed: ${err.message}`;
  });
});
signoutBtn.addEventListener("click", () => signOut(auth));

// Hamburger dropdown: closed by default, opened by the toggle button,
// closed again by picking anything inside it (a tab, Sign out) or by
// clicking anywhere outside — same interaction shape as the photo modal's
// own ⋮ "more actions" menu (see photoModal.js), just in the header instead.
navToggleBtn.addEventListener("click", (e) => {
  e.stopPropagation();
  navEl.hidden = !navEl.hidden;
});
navEl.addEventListener("click", () => { navEl.hidden = true; });
document.addEventListener("click", (e) => {
  if (!navEl.hidden && !navMenuWrapEl.contains(e.target)) navEl.hidden = true;
});

async function refreshNotifyToggle() {
  const on = await push.isSubscribed();
  // Icon-only (see index.html) — the label moves to title/aria-label
  // instead of visible text, same info, just not taking up header space.
  notifyToggleBtn.innerHTML = on ? BELL_ON_SVG : BELL_OFF_SVG;
  const label = on ? "Notifications on — tap to disable" : "Enable notifications";
  notifyToggleBtn.title = label;
  notifyToggleBtn.setAttribute("aria-label", label);
}

notifyToggleBtn.addEventListener("click", async () => {
  notifyToggleBtn.disabled = true;
  try {
    if (await push.isSubscribed()) await push.unsubscribe();
    else await push.subscribe();
  } catch (err) {
    statusEl.textContent = `Notifications: ${err.message}`;
    console.error(err);
  }
  await refreshNotifyToggle();
  notifyToggleBtn.disabled = false;
});

// Only the first "/"-separated segment picks the view; anything after it is
// that view's own business (Folders' path, People's open person) — see each
// view's onHashChange for how it reads its own sub-path back out.
function viewNameFromHash() {
  const name = location.hash.replace("#", "").split("/")[0];
  return VIEWS[name] ? name : DEFAULT_VIEW;
}

function route() {
  const name = viewNameFromHash();
  if (name === currentView) {
    // Same top-level view, but the hash still changed underneath it (a
    // folder drilled into, a person opened, or the browser's Back/Forward
    // button) — let the view sync itself instead of a full remount, so a
    // click three folders deep doesn't re-fetch from the root every time.
    VIEWS[name].onHashChange?.();
    return;
  }

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
  navMenuWrapEl.hidden = !signedIn;
  navEl.hidden = true; // always start collapsed, whether signing in or out
  viewEl.hidden = !signedIn;
  heartbeatEl.hidden = !signedIn;
  notifyToggleBtn.hidden = !signedIn || !push.isSupported();

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
  if (notifyToggleBtn.hidden === false) refreshNotifyToggle();
  if (!location.hash) location.hash = `#${DEFAULT_VIEW}`;
  route();
});
