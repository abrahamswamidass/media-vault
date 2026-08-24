// Fill these in from your Firebase project settings (Project settings ->
// General -> Your apps -> Web app -> SDK setup and configuration).
// None of this is secret — Firebase API keys are safe to commit; access is
// controlled by Firestore/Storage security rules, not by hiding this key.
// https://firebase.google.com/docs/projects/api-keys
export const firebaseConfig = {
  apiKey: "PASTE_ME",
  authDomain: "PASTE_ME",
  projectId: "PASTE_ME",
};

// The name you gave your Firestore database when you created it (NOT the
// GCP project id). Matches the agent's FIRESTORE_DATABASE env var.
export const FIRESTORE_DATABASE = "media-vault-store";

// The GCS bucket the agent's GCS_BUCKET env var points at — where thumbnails
// actually live. Linked into Firebase Storage (see docs/setup.md) so
// Storage security rules gate access to it.
export const GCS_BUCKET = "mymediavault";

// The only Google account allowed to sign in. Client-side check here is only
// for UI state (which screen to show) — the real enforcement is the matching
// check in firestore.rules and storage.rules, which run server-side.
export const OWNER_EMAIL = "winfredbe@gmail.com";
