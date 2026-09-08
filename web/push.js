// Web Push subscribe/unsubscribe — the browser side of the flow the agent's
// notify.py answers (see docs/agent.md's notifications section). A
// subscription is a client-owned preference, like hidden_folders/, not an
// intent: registering a device never touches a file, so it's written
// straight to Firestore rather than round-tripped through the agent.
import {
  collection, doc, setDoc, deleteDoc,
} from "https://www.gstatic.com/firebasejs/10.14.1/firebase-firestore.js";
import { db } from "./firebase.js";
import { VAPID_PUBLIC_KEY } from "./firebase-config.js";

const COLLECTION = "push_subscriptions";

export function isSupported() {
  return "serviceWorker" in navigator && "PushManager" in window;
}

// PushManager.subscribe() wants applicationServerKey as a Uint8Array, not
// the base64url string VAPID keys are normally handed around as — standard
// conversion, unique to the Web Push API.
function urlBase64ToUint8Array(base64url) {
  const padding = "=".repeat((4 - (base64url.length % 4)) % 4);
  const base64 = (base64url + padding).replace(/-/g, "+").replace(/_/g, "/");
  const raw = atob(base64);
  return Uint8Array.from([...raw].map((c) => c.charCodeAt(0)));
}

// A subscription's own endpoint URL is the natural unique key for it (one
// device/browser installation = one endpoint), but Firestore document ids
// can't hold a raw URL — hashed the same way item.schema.json's content
// hash keys a blob, so re-subscribing the same device overwrites its own
// doc instead of piling up duplicates.
async function endpointDocId(endpoint) {
  const bytes = new TextEncoder().encode(endpoint);
  const digest = await crypto.subtle.digest("SHA-256", bytes);
  return [...new Uint8Array(digest)].map((b) => b.toString(16).padStart(2, "0")).join("");
}

/** True if this browser currently holds a live push subscription. Doesn't
 * check Firestore — just whether the browser-side half exists, which is
 * what the header toggle needs to render its on/off state. */
export async function isSubscribed() {
  if (!isSupported() || Notification.permission !== "granted") return false;
  const registration = await navigator.serviceWorker.getRegistration();
  if (!registration) return false;
  return !!(await registration.pushManager.getSubscription());
}

/** Ask permission (if needed), subscribe this browser, and register it with
 * the agent by writing its subscription to Firestore. Throws on denial or
 * any failure — the caller (app.js) is expected to show that as a status
 * message, same as every other intent-writing action in this app. */
export async function subscribe() {
  if (!isSupported()) {
    throw new Error("This browser doesn't support push notifications.");
  }
  const permission = await Notification.requestPermission();
  if (permission !== "granted") {
    throw new Error("Notification permission was not granted.");
  }

  const registration = await navigator.serviceWorker.register("sw.js");
  const subscription = await registration.pushManager.subscribe({
    userVisibleOnly: true,
    applicationServerKey: urlBase64ToUint8Array(VAPID_PUBLIC_KEY),
  });

  const json = subscription.toJSON();
  const id = await endpointDocId(json.endpoint);
  await setDoc(doc(collection(db, COLLECTION), id), {
    endpoint: json.endpoint, keys: json.keys,
    created_at: new Date().toISOString(),
  });
}

/** Unsubscribe this browser and remove its Firestore registration. Safe to
 * call even if already unsubscribed (e.g. two tabs racing the toggle). */
export async function unsubscribe() {
  const registration = await navigator.serviceWorker.getRegistration();
  const subscription = registration && (await registration.pushManager.getSubscription());
  if (!subscription) return;

  const endpoint = subscription.endpoint;
  await subscription.unsubscribe();
  const id = await endpointDocId(endpoint);
  await deleteDoc(doc(collection(db, COLLECTION), id));
}
