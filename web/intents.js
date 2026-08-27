// Writing an intent — the web module's only way to ask the agent to do
// something (see CLAUDE.md's "intents in, facts out" and sync/intents.py).
// This file only writes; nothing here ever reads status/result back — the
// Amazon tab does that separately, since that's the one place it matters.
import { collection, doc, setDoc } from "https://www.gstatic.com/firebasejs/10.14.1/firebase-firestore.js";
import { db } from "./firebase.js";

//: Must stay a subset of what firestore.rules' intents/ `create` rule
//: allows through, and of REGISTRY in the agent's sync/intents.py.
export async function writeIntent(type, itemId, params = {}) {
  const id = crypto.randomUUID().replace(/-/g, "");
  await setDoc(doc(collection(db, "intents"), id), {
    id, type, item_id: itemId, params,
    status: "pending",
    created_at: new Date().toISOString(),
  });
  return id;
}

export function stageForAmazon(item) {
  return writeIntent("stage_for_amazon", item.item_id, { source: item.source });
}
