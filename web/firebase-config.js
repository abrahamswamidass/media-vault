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
// actually live. Used to build public thumbnail URLs directly.
export const GCS_BUCKET = "mymediavault";
