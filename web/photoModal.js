// The photo modal — shared by every view that shows a grid of photos
// (Browse, Folders, and eventually anything else). One singleton instance
// appended to <body> the first time it's needed, rather than each view
// building/tearing down its own copy: the modal's state (open item, swipe
// tracking, body-scroll lock) doesn't belong to any one view, and a
// singleton means a view switch can't leave a stale lock or duplicate
// keydown listener behind — see closePhotoModal(), which app.js's router
// calls on every navigation.
import { getDownloadURL, ref } from "https://www.gstatic.com/firebasejs/10.14.1/firebase-storage.js";
import { storage } from "./firebase.js";
import { stageForAmazon } from "./intents.js";

let modal = null;
let items = [];
let currentIndex = -1;
let savedScrollY = 0;
let touchStartX = 0;
let touchStartY = 0;

function effectiveDate(item) {
  return item.date_taken ?? item.mtime;
}

function formatDuration(seconds) {
  const total = Math.round(seconds);
  const h = Math.floor(total / 3600);
  const m = Math.floor((total % 3600) / 60);
  const s = total % 60;
  const mm = String(m).padStart(h ? 2 : 1, "0");
  const ss = String(s).padStart(2, "0");
  return h ? `${h}:${mm}:${ss}` : `${mm}:${ss}`;
}

function renderDetails(item) {
  const dl = modal.querySelector(".modal-details");
  dl.innerHTML = "";
  const camera = [item.camera_make, item.camera_model].filter(Boolean).join(" ");
  const dims = item.width && item.height ? `${item.width}×${item.height}` : null;
  const rows = [
    ["Name", item.name],
    ["Path", item.item_id],
    ["Taken", new Date(effectiveDate(item) * 1000).toLocaleString()],
    ["Dimensions", dims],
    // Absent for most videos, not just photos — see metadata.py's file
    // header on why a video's duration atom can legitimately fall outside
    // the head-read window this comes from.
    ["Duration", item.duration_seconds ? formatDuration(item.duration_seconds) : null],
    ["Size", item.size ? `${(item.size / 1024).toFixed(0)} KB` : null],
    ["Camera", camera || null],
    ["Source", item.source],
    ["Type", item.mime],
    ["Quick hash", item.quick_hash],
    ["Thumbnail key", item.thumbnail_key],
    ["Modified", item.mtime ? new Date(item.mtime * 1000).toLocaleString() : null],
  ];
  for (const [label, value] of rows) {
    if (!value) continue;
    const dt = document.createElement("dt");
    dt.textContent = label;
    const dd = document.createElement("dd");
    dd.textContent = value;
    dl.append(dt, dd);
  }
}

function closeMenu() {
  modal.querySelector(".modal-menu").hidden = true;
}

function toggleDetails() {
  modal.querySelector(".modal-details").hidden = !modal.querySelector(".modal-details").hidden;
}

// iOS Safari lets a touch-scroll gesture over a `position: fixed` overlay
// fall through and scroll the page underneath it — `.modal-content`'s own
// `overflow-y: auto` doesn't stop that. Freezing the body in place (and
// remembering where it was) while the modal is open is the standard
// workaround; plain `overflow: hidden` on body alone doesn't reliably work
// on iOS Safari specifically.
function lockBodyScroll() {
  savedScrollY = window.scrollY;
  document.body.style.position = "fixed";
  document.body.style.top = `-${savedScrollY}px`;
  document.body.style.width = "100%";
}

function unlockBodyScroll() {
  document.body.style.position = "";
  document.body.style.top = "";
  document.body.style.width = "";
  window.scrollTo(0, savedScrollY);
}

function closeModal() {
  if (!modal || modal.hidden) return;
  modal.hidden = true;
  unlockBodyScroll();
}

function render(item) {
  if (modal.hidden) lockBodyScroll(); // only on the closed -> open transition, not on prev/next

  const img = modal.querySelector("img");
  img.src = "";
  getDownloadURL(ref(storage, item.thumbnail_key))
    .then((url) => { img.src = url; })
    .catch((err) => console.error(item.item_id, err));

  renderDetails(item);
  modal.querySelector(".modal-details").hidden = true;
  closeMenu();
  modal.querySelector(".modal-prev").disabled = currentIndex <= 0;
  modal.querySelector(".modal-next").disabled = currentIndex >= items.length - 1;
  const stageBtn = modal.querySelector(".modal-stage-amazon");
  stageBtn.disabled = false;
  stageBtn.textContent = "Stage for Amazon";
  modal.querySelector(".modal-stage-status").textContent = "";
  modal.hidden = false;
}

async function handleStageForAmazon() {
  const item = items[currentIndex];
  if (!item) return;
  closeMenu();
  const btn = modal.querySelector(".modal-stage-amazon");
  const status = modal.querySelector(".modal-stage-status");
  btn.disabled = true;
  btn.textContent = "Staging…";
  try {
    await stageForAmazon(item);
    btn.textContent = "Staged ✓";
    status.textContent = "Waiting for the agent to pick it up — see the Amazon tab.";
  } catch (err) {
    btn.disabled = false;
    btn.textContent = "Stage for Amazon";
    status.textContent = `Failed: ${err.message}`;
    console.error(item.item_id, err);
  }
}

function showAt(index) {
  if (index < 0 || index >= items.length) return;
  currentIndex = index;
  render(items[index]);
}

function handleKeydown(e) {
  if (!modal || modal.hidden) return;
  if (e.key === "ArrowLeft") showAt(currentIndex - 1);
  else if (e.key === "ArrowRight") showAt(currentIndex + 1);
  else if (e.key === "Escape") closeModal();
}

// Swipe left/right on the photo — the expected way to move between photos
// on a phone, buttons/arrow keys are the fallback for anyone not touching
// the screen. Only reacts to a mostly-horizontal drag past a real threshold
// (50px, and at least 1.5x more horizontal than vertical movement) so a
// slightly-diagonal scroll attempt or an accidental tap-and-drag doesn't
// misfire as a page change.
function handleTouchStart(e) {
  touchStartX = e.touches[0].clientX;
  touchStartY = e.touches[0].clientY;
}

function handleTouchEnd(e) {
  const dx = e.changedTouches[0].clientX - touchStartX;
  const dy = e.changedTouches[0].clientY - touchStartY;
  if (Math.abs(dx) > 50 && Math.abs(dx) > Math.abs(dy) * 1.5) {
    showAt(currentIndex + (dx < 0 ? 1 : -1));
  }
}

function build() {
  const el = document.createElement("div");
  el.className = "modal";
  el.hidden = true;
  el.innerHTML = `
    <div class="modal-content">
      <div class="modal-topbar">
        <div class="modal-topbar-spacer"></div>
        <button class="modal-nav modal-prev" type="button" aria-label="Previous">&lsaquo;</button>
        <button class="modal-nav modal-next" type="button" aria-label="Next">&rsaquo;</button>
        <div class="modal-menu-wrap">
          <button class="modal-nav modal-menu-toggle" type="button" aria-label="More actions">&#8942;</button>
          <div class="modal-menu" hidden>
            <button class="modal-fullres" disabled
              title="Not wired up yet — the agent-side processor now exists (process-intents), this button just doesn't call it yet.">
              Request full-res (coming soon)
            </button>
            <button class="modal-stage-amazon" type="button">Stage for Amazon</button>
          </div>
        </div>
        <button class="modal-nav modal-close" type="button" aria-label="Close">&times;</button>
      </div>
      <div class="modal-media">
        <img alt="" title="Tap for details" />
      </div>
      <p class="modal-stage-status"></p>
      <dl class="modal-details" hidden></dl>
    </div>
  `;
  document.body.appendChild(el);
  modal = el;

  modal.querySelector(".modal-close").addEventListener("click", closeModal);
  modal.addEventListener("click", (e) => {
    // Outside the ⋮ menu (but still inside the modal) closes just the menu,
    // not the whole modal — e.g. tapping the photo while the menu is open.
    if (!modal.querySelector(".modal-menu-wrap").contains(e.target)) closeMenu();
    if (e.target === modal) closeModal();
  });
  modal.querySelector("img").addEventListener("click", toggleDetails);
  modal.querySelector(".modal-media").addEventListener("touchstart", handleTouchStart, { passive: true });
  modal.querySelector(".modal-media").addEventListener("touchend", handleTouchEnd, { passive: true });
  modal.querySelector(".modal-menu-toggle").addEventListener("click", (e) => {
    e.stopPropagation(); // don't let the modal-level listener above immediately re-close it
    modal.querySelector(".modal-menu").hidden = !modal.querySelector(".modal-menu").hidden;
  });
  modal.querySelector(".modal-prev").addEventListener("click", () => showAt(currentIndex - 1));
  modal.querySelector(".modal-next").addEventListener("click", () => showAt(currentIndex + 1));
  modal.querySelector(".modal-stage-amazon").addEventListener("click", handleStageForAmazon);
  // Registered once, ever — the modal is a body-level singleton, not
  // recreated per view mount, so this never needs a matching removeListener.
  document.addEventListener("keydown", handleKeydown);
}

/** Open the modal on `list[index]`. `list` becomes the set prev/next/swipe
 * move through — pass whatever the calling view currently has rendered
 * (Browse's loaded items, one folder's files, ...). */
export function openPhotoAt(list, index) {
  if (!modal) build();
  items = list;
  showAt(index);
}

/** Force the modal closed — called by app.js's router on every navigation,
 * so switching tabs while a photo is open can't leave it (and the body-
 * scroll lock) stuck open over a different view. */
export function closePhotoModal() {
  closeModal();
}
