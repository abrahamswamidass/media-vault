// From Firebase console: Project settings -> General -> Your apps -> Web app.
// None of this is secret — Firebase API keys are safe to commit; access is
// controlled by Firestore/Storage security rules, not by hiding this key.
// https://firebase.google.com/docs/projects/api-keys
//
// NOTE: app.js loads the Firebase SDK from a CDN URL and imports this file
// as a plain ES module — it expects firebaseConfig exported as data, not
// initializeApp() called here. Pasting the console's auto-generated snippet
// as-is breaks this (it uses bare "firebase/app" specifiers meant for an
// npm/bundler project, which a browser can't resolve directly).
export const firebaseConfig = {
  apiKey: "AIzaSyCKwqoofRG3TxT1L9z2a-p7QJYqEczlb00",
  authDomain: "gcp-arch-340414.firebaseapp.com",
  projectId: "gcp-arch-340414",
};

// The name you gave your Firestore database when you created it (NOT the
// GCP project id). Matches the agent's FIRESTORE_DATABASE env var.
export const FIRESTORE_DATABASE = "media-vault-store";

// The GCS bucket the agent's GCS_BUCKET env var points at — where thumbnails
// actually live. Linked into Firebase Storage (see docs/setup.md) so
// Storage security rules gate access to it. Deliberately NOT
// gcp-arch-340414.firebasestorage.app (that's the POC's default bucket).
export const GCS_BUCKET = "mymediavault";

// The only Google accounts allowed to sign in. Client-side check here is
// only for UI state (which screen to show) — the real enforcement is the
// matching allowlist in firestore.rules and storage.rules, which run
// server-side and must be kept in sync with this list by hand.
export const ALLOWED_EMAILS = ["winfredbe@gmail.com", "percial@gmail.com"];

// The VAPID public key PushManager.subscribe() signs a subscription against —
// safe to commit, the same way a Firebase API key is (see the note at the top
// of this file): it's the PUBLIC half of the pair. The agent holds the
// PRIVATE half (VAPID_PRIVATE_KEY_FILE, see agent/.env.example) and never
// shares it. Regenerating this pair invalidates every existing subscription —
// see docs/agent.md's notifications section.
export const VAPID_PUBLIC_KEY =
  "BLW0n79y5WTdPgOF8m6MhGTAgAL9dFj0FQAJSGIoi4_6h3wZzEAIQkSyNFvlZyg0bUWqg9WuDNxJWb3a-7y66FE";
