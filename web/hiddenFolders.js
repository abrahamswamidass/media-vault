// Hidden folders: a pure display preference, not a file mutation — hiding a
// folder never touches the NAS, so unlike everything in intents.js this
// writes straight to Firestore from the client. Folders is where the
// checkbox lives; Browse and Map both filter by the same prefix set so a
// hidden folder disappears everywhere consistently.
import {
  collection, doc, getDocs, setDoc, deleteDoc, serverTimestamp,
} from "https://www.gstatic.com/firebasejs/10.14.1/firebase-firestore.js";
import { db } from "./firebase.js";

const COLLECTION = "hidden_folders";

// Firestore document ids can't contain '/' — same flattening amazon.js uses
// for item ids, applied here to a folder path prefix instead.
function docId(path) {
  return path.replace(/[/\\]+/g, "_");
}

let cache = null; // Set<string> of hidden path prefixes, each ending in "/"

export async function loadHiddenPrefixes() {
  if (cache) return cache;
  const snap = await getDocs(collection(db, COLLECTION));
  cache = new Set(snap.docs.map((d) => d.data().path));
  return cache;
}

export function isHidden(itemId, prefixes) {
  for (const prefix of prefixes) {
    if (itemId.startsWith(prefix)) return true;
  }
  return false;
}

export async function setFolderHidden(path, hidden) {
  const ref = doc(db, COLLECTION, docId(path));
  if (hidden) {
    await setDoc(ref, { path, hidden_at: serverTimestamp() });
  } else {
    await deleteDoc(ref);
  }
  // Invalidate rather than patch in place — cheap re-fetch, no risk of the
  // in-memory Set drifting from what's actually in Firestore.
  cache = null;
}
