// Shared Firebase init — every view module imports from here instead of
// calling initializeApp() itself, so there's exactly one app/auth/db/storage
// instance regardless of how many view modules get loaded.
import { initializeApp } from "https://www.gstatic.com/firebasejs/10.14.1/firebase-app.js";
import { getAuth } from "https://www.gstatic.com/firebasejs/10.14.1/firebase-auth.js";
import { getFirestore } from "https://www.gstatic.com/firebasejs/10.14.1/firebase-firestore.js";
import { getStorage } from "https://www.gstatic.com/firebasejs/10.14.1/firebase-storage.js";
import { firebaseConfig, FIRESTORE_DATABASE, GCS_BUCKET } from "./firebase-config.js";

export const app = initializeApp(firebaseConfig);
export const auth = getAuth(app);
export const db = getFirestore(app, FIRESTORE_DATABASE);
export const storage = getStorage(app, `gs://${GCS_BUCKET}`);
