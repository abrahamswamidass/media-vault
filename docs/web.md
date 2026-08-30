# Web viewer

Assumes you've already got the agent publishing to the real Cloud Storage
bucket and Firestore (the last step of [agent.md](agent.md)'s "Cloud mirror"
section) — this page is about the browser-facing side that reads what
`publish` pushes there.

## What it is

`web/` is a minimal static page — no build step, Google sign-in gated to a
small hardcoded allowlist — that reads the `items` Firestore collection and
shows a thumbnail grid. It's an MVP placeholder, not the eventual React app.
The nav groups three ways to look at the same library, left of a divider:
**Browse** (chronological, the default), **Map** (every geotagged item pinned
on a Leaflet/OpenStreetMap map — no API key, no cost, loaded from a CDN only
when that tab is opened; most photos have no GPS at all, so an empty or
sparse map is normal, not a sign anything's broken), **Folders** (drills
into the same NAS folder structure the agent indexed — Firestore has no real
hierarchy, so "folders" are computed on the fly from item_id path segments as
each page loads, the same client-side, paginated approach Browse uses for
"Load more"), and **People** (see below). Right of the divider: **Duplicates**
(near-duplicate review, see below), **Amazon** (read-only staging status),
and **Activity** (see below) — maintenance/status tools, not library views.
Clicking a photo in Browse, Folders, People, or Duplicates opens the same
modal (they share one instance — see `CLAUDE.md`'s web/ section).

## People

Groups published items by the `person_ids` array `publish` already writes
when `FACES_LIVE=1` was on (see [agent.md](agent.md#face-detection-optional-agent-side-only-for-now)) —
click a person's cover tile to see every photo they appear in. Firestore has
no way to ask "items where this array isn't empty," so this scans a bounded
window of the most recent 5000 items (by `mtime`, same as Browse) and
buckets by person id entirely client-side — a person whose only appearances
fall outside that window won't show up, which is fine at personal-library
scale but worth knowing if it looks incomplete on a very large library.

**View-only, deliberately** — there's no way to name someone from the
browser yet. Naming today only exists as `people-rename` on the CLI, which
writes to the local SQLite catalog, not Firestore, so a name given that way
doesn't reach this tab at all. Bridging that gap (letting a name entered
here, or given via the CLI, show up consistently in both places) is tracked
as a separate issue rather than folded into this view.

## Duplicates

Groups visually similar photos for review — a resize, a re-compression, a
burst-sequence shot — from the perceptual hash (`phash`) `publish` writes
for every photo (see [agent.md](agent.md), no live switch, always on). Same
client-side-scan shape as People: Firestore can't ask "items within Hamming
distance N of each other," so this scans a bounded window of recent items
and buckets ones whose hash is close *and* whose capture time falls within
an hour of each other — near-duplicates are almost always from the same
event, so the time window keeps this cheap and matches how they actually
occur, rather than comparing every item against every other one.

**Review-only, deliberately — nothing here ever auto-picks a "keeper."**
Unlike exact dedup (`mediavault dedup`, a full-content SHA-256 match, safe
to auto-archive), a near-duplicate pair often differs in ways that matter —
one might be the full-resolution original, the other a messenger-app copy —
so each photo in a group gets its own **Archive** button. Clicking it writes
a `delete` intent for that one photo; the agent picks it up the same way it
does a "Stage for Amazon" click. That intent does two things, not just one:
moves the file to trash *and* removes its Firestore fact, so the photo
actually disappears from Browse/Map/Folders/People/Duplicates once the
agent's caught up — not just from the NAS while a stale entry keeps showing
everywhere. Nothing is archived until you explicitly click something, one
photo at a time.

## Activity

History of every intent the web module has written, any type — not scoped
to Amazon staging the way the Amazon tab is. Each row shows what was
requested, its status, and when. **A completed `delete` gets an Undo
button** — clicking it writes a fresh `restore` intent for the same item,
which the agent's `restore()` turns into moving the file back out of trash
(NAS) or clearing Drive's own trashed flag. This is what makes archiving
from Duplicates (or anywhere else) a safe, reversible click rather than a
one-way door: every `delete` in this project already moves to trash instead
of unlinking, and now there's a UI path to undo it without touching the NAS
by hand.

**Undo brings the file back, but not (yet) its place in the web viewer.**
The file is genuinely safe and restored on the NAS the moment Undo
completes, but the photo itself won't reappear in Browse/Map/Folders/People
until someone runs `publish --force` afterward — undo doesn't automatically
re-derive the thumbnail/metadata fact it removed on archive (see [agent.md](agent.md)'s
`--force` note, and the tracking issue for closing this loop automatically).

## Hiding a folder

A checkbox on each folder tile in Folders removes everything under it from
Browse and Map, without touching a single file on the NAS. It's a pure
display preference (`hidden_folders/` in Firestore, written straight from
the browser, no intent round-trip: see `web/hiddenFolders.js`), not a
mutation, so it needs none of the "web writes intents, agent decides"
machinery everything else here goes through. Folders itself always shows
every folder regardless of its hidden state — it's the control panel for
this feature, so unhiding has to stay reachable from it. Unchecking the box
brings a folder's contents right back everywhere.

## Access

Gated server-side by `firestore.rules` and `storage.rules`, both hardcoded to
the same allowlist of emails — not by hiding the URL. GCS's `allUsers` IAM
grant explicitly cannot be scoped to a prefix (Google rejects conditions on
public principals), so this is the actual private-by-default path, not a
workaround.

## Staging a photo for Amazon

Works from Browse or Map's own photo view (click a photo → "Stage for
Amazon"). That button only *writes a request* — the web module can't touch
the NAS itself (see `CLAUDE.md`'s three rules). The agent picks it up on its
own: `process-intents --watch` is the container's default command (see
[setup.md](setup.md)), polling every 10 minutes for anything the web module
has asked for — not just Amazon staging, every intent type (`fetch_fullres`,
`delete`, `restore`, `copy`, `index`, `dedup_source`, `publish`,
`stage_for_amazon`). Each poll is one small Firestore query, so this costs
effectively $0/month regardless of how long it runs. Nothing to start by
hand under normal operation.

The **Amazon** tab is read-only — it lists what's been requested and its
current status (waiting / working / staged / failed), not a picker of its
own.

**A green/red dot in the page header** shows whether the watch loop is
actually running right now — useful because the container's default command
*can* be overridden (see setup.md), or the container could simply be
stopped. `--watch` writes a heartbeat (`agent_status/process_intents`:
last-poll time + pending count) each time it polls; the dot goes red if that
heartbeat is more than 20 minutes old or missing entirely — meaning any
request, of any type, would just sit pending until the container (or a
manual `process-intents --commit`) picks it up.

**If you ever do need to run it by hand** — the container was started with
`sleep infinity` instead, or you're troubleshooting — use `docker exec -it`,
not a bare `docker exec`:
```powershell
docker exec -it media-vault-container python -m mediavault.cli process-intents --watch --interval 600
```
**The `-it` matters.** Without it, Ctrl+C only kills the local `docker exec`
client, not the polling loop inside the container — it silently keeps
running as an orphaned background process, the same trap a stray `dedup
--commit` can fall into (see [agent.md](agent.md)'s dedup notes). With
`-it`, Ctrl+C reaches the process directly and it stops cleanly, no orphan
left behind.

## Add it to your phone's home screen

For a proper app-like icon — `manifest.json` and the `apple-touch-icon` are
already wired in. On iOS: Safari → Share → **Add to Home Screen**. Opens
without the browser's address bar, same as a real app.

## Firebase project setup

### 1. Add Firebase to the project

1. [Firebase console](https://console.firebase.google.com) → **Add project**
   → pick your *existing* GCP project (don't create a new one).
2. Project settings → **Your apps** → **Add app** → **Web**.
3. Copy the `firebaseConfig` object it shows you.

### 2. Enable Google sign-in

**Authentication → Sign-in method → Add new provider → Google** → enable it.
No allowlist needed here — the rules below do the actual gating.

### 3. Link the existing GCS bucket into Firebase Storage

**Storage** → if this is the first time enabling Storage, it prompts you to
pick a Cloud Storage bucket — choose the one the agent already uses (e.g.
`mymediavault`) rather than letting it create a new default bucket. If
Storage is already enabled with a different default bucket, use the bucket
dropdown at the top of the Storage page → **Add Cloud Storage bucket** →
select the agent's bucket there instead.

### 4. Publish both rule sets

- **Firestore → Rules** (on your database, e.g. `media-vault-store`) → paste
  in `firestore.rules` from the repo root → **Publish**.
- **Storage → Rules** → make sure the bucket selector at the top is set to
  the agent's bucket (from step 3) → paste in `storage.rules` from the repo
  root → **Publish**.

Both files already have an allowlist (`winfredbe@gmail.com`,
`percial@gmail.com`) hardcoded — edit the `in [...]` line in both files to
add, remove, or replace accounts. Keep `web/firebase-config.js`'s
`ALLOWED_EMAILS` (step 5) in sync by hand — nothing enforces the two lists
matching, they just both need to.

**If you set this up before Amazon staging existed**, re-paste
`firestore.rules` — it now also covers the `intents/`, `agent_status/`, and
`hidden_folders/` collections, which an older ruleset would silently reject.

### 5. Fill in the config

> The Firebase console's "Add app" screen offers to auto-generate this whole
> file for you (it even calls it `firebase-config.js`) — **don't use that
> snippet as-is.** It uses bare `import "firebase/app"` specifiers meant for
> an npm/bundler project, which a plain browser can't resolve, and it calls
> `initializeApp()` itself instead of exporting the config for `app.js` to
> use. Take just the `apiKey`/`authDomain`/`projectId` values out of it and
> paste them into the shape below instead.

Edit `web/firebase-config.js` in the repo:

```js
export const firebaseConfig = {
  apiKey: "...",        // from step 1
  authDomain: "...",    // from step 1
  projectId: "...",     // from step 1
};
export const FIRESTORE_DATABASE = "your-database-name"; // e.g. media-vault-store
export const GCS_BUCKET = "your-bucket-name";
export const ALLOWED_EMAILS = ["winfredbe@gmail.com", "percial@gmail.com"];
```

None of this is secret — Firebase web API keys are safe to commit; access is
controlled by the security rules in step 4, not by hiding this file.
`ALLOWED_EMAILS` here only controls which screen the page shows (signed-in
vs. "not authorized") — it is not what makes this private.

### 6. Preview locally (optional but fast)

```powershell
cd web
python -m http.server 8000
```

Open `http://localhost:8000`, click **Sign in with Google**, and use one of
the allowlisted accounts — if steps 1–5 are right, you'll see the thumbnail
grid, no deploy needed yet. Any other Google account gets a clean "not
authorized" message, and the security rules reject it either way even if the
client-side check were somehow bypassed.

### 7. Deploy

```powershell
npm install -g firebase-tools
firebase login
firebase use --add          # pick your project when prompted
firebase deploy --only hosting
```

Prints a live `https://your-project.web.app` URL when done. Also add that
exact URL to **Authentication → Settings → Authorized domains** in the
Firebase console if sign-in fails there with an unauthorized-domain error
(localhost and the default `web.app` domain are usually pre-authorized).
