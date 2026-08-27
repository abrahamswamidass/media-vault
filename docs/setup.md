# Setup

Everything runs as one long-lived container, started once with `docker run`,
then driven with `docker exec`. No repo checkout, no `.env` file, no compose —
just the published image plus environment variables on the run command.

## 1. Pull the image

```powershell
docker pull ghcr.io/abrahamswamidass/media-vault/agent:latest
```

## 2. Start the container

The NAS is reached directly over SMB (not an OS-level mount — Docker Desktop
on Windows can't reliably bind-mount a mapped network drive, so this sidesteps
that entirely). `--user root` avoids permission errors writing the local
catalog folder.

```powershell
docker run -d --name media-vault-container --user root `
  -e NAS_MODE=smb `
  -e NAS_HOST=192.168.6.110 `
  -e NAS_SHARE=homes `
  -e NAS_SMB_ROOT=winfredbe/nov2025-cafc `
  -e NAS_SMB_TRASH=_trash `
  -e AMAZON_SMB_ROOT=_AmazonUpload `
  -e NAS_USER=winfredbe `
  -e 'NAS_PASSWORD=your-password-here' `
  -v "C:\mediavault\catalog:/data/catalog" `
  -v "C:\mediavault\secrets:/secrets" `
  ghcr.io/abrahamswamidass/media-vault/agent:latest `
  sleep infinity
```

Notes:

- **`NAS_HOST`** — your NAS's IP or hostname.
- **`NAS_SHARE`** — the SMB share name (e.g. `homes`).
- **`NAS_SMB_ROOT`** — the path *inside* that share to index, forward slashes,
  no leading slash (e.g. `winfredbe/nov2025-cafc`). Point this at any subfolder
  you want to scan — trash and Amazon staging below don't move when this does.
- **`NAS_SMB_TRASH`** and **`AMAZON_SMB_ROOT`** — both relative to the **share**
  (`homes`), not to `NAS_SMB_ROOT`. That's deliberate: wherever you point
  `NAS_SMB_ROOT` (a different album, a different year), archived duplicates and
  staged Amazon uploads always land in the same fixed place —
  `\\192.168.6.110\homes\_trash` and `\\192.168.6.110\homes\_AmazonUpload` here.
  Omit `NAS_SMB_TRASH` to default to `<NAS_SMB_ROOT>/_trash` instead (nested,
  moves with the root); omit `AMAZON_SMB_ROOT` to default to `_AmazonUpload` at
  the share's top level. Both are automatically excluded from `index`/`dedup`/
  `publish` — this matters once `NAS_SMB_ROOT` covers the whole share (see
  below), where they'd otherwise show up as ordinary subfolders and get
  cataloged like any other content.
- **`NAS_PASSWORD`** — wrap in **single quotes** in PowerShell. Without quotes,
  a `$` in the password gets silently interpreted as a variable and the
  connection fails with `STATUS_LOGON_FAILURE`.
- **`C:\mediavault\catalog`** — any local folder; holds the index database
  between runs. Create it first if it doesn't exist.
- **`C:\mediavault\secrets`** — any local folder; mounted read-only at
  `/secrets` for credential files (e.g. the GCS service-account key used
  below). Create it first if it doesn't exist. Safe to mount even if empty.
- `sleep infinity` keeps the container running so you can `exec` into it
  repeatedly instead of creating a new container per command.

No volume mount or extra env var is needed for Amazon staging beyond
`AMAZON_SMB_ROOT` above — it goes over the same SMB connection as the NAS.

### Scanning the whole drive instead of one folder

Set `NAS_SMB_ROOT=` (empty) to index the entire share from its root, once
you're done testing against a subfolder. Nothing else changes — `NAS_SMB_TRASH`
and `AMAZON_SMB_ROOT` are already share-relative and already excluded from
`index`/`dedup`/`publish` (see the note above), so widening the scan doesn't
pull trash or staged Amazon uploads into the library.

## 3. Check it

```powershell
docker exec media-vault-container python -m mediavault.cli doctor
```

Green means ready. Anything red or `!` prints the exact fix beneath it.

## Command reference

Every command below runs the same way — `docker exec media-vault-container
python -m mediavault.cli <command>`. This table is the lookup; the prose
after it (and the sections further down) explain the *why* behind each one.

| Command | What it does |
|---|---|
| `doctor` | Preflight check — what's configured, what isn't. |
| `index nas` | Resumable walk into the catalog. Add `--debug` for live per-file/per-directory progress. |
| `stats` | Summary of what's in the catalog. |
| `dedup nas` | Preview duplicate groups. |
| `dedup nas --commit` | Archive every duplicate found (see [Deduplication](#deduplication) for `--max-groups` to do this in batches). |
| `dedup nas --by-folder [--depth N]` | Summarize reclaimable space by folder instead of listing every group. Always read-only. |
| `dedup nas --debug` | Show live progress during the full-content confirmation pass. |
| `publish nas` | Preview pushing thumbnails + metadata for unpublished items. |
| `publish nas --commit` | Actually push them (local files without `GCS_LIVE=1`, real GCS/Firestore with it). |
| `publish nas --force --commit` | Also republish already-published items — backfills a fact field (e.g. GPS) added after they were first published, no reset/re-index needed. |
| `reset nas [--commit]` | Wipe the local catalog for one source, to re-index from scratch. Add `--purge-facts` when widening the scan root. |
| `reset --all --commit` | Same, for every source. |
| `amazon-stage "<path>" --source nas --commit` | Stage a file straight off the NAS for Amazon Photos, no local copy needed. |
| `amazon upload /path/to/file --commit` | Stage a file that's already on the container's own filesystem. |
| `drive-login` | One-time interactive OAuth grant — see [Google Drive](#google-drive-optional). |
| `index drive` | Resumable walk into the catalog, same as `index nas`. Needs `DRIVE_LIVE=1` and a saved token. |
| `dedup drive [--commit]` | Preview / archive Drive duplicates — same flags as `dedup nas` (`--by-folder`, `--debug`, `--max-groups`, ...). |
| `stats` | Already covers Drive once indexed — one command, all sources together, no `drive`-specific variant needed. |

Indexing a terabyte takes a while and checkpoints after every directory. If it
dies, run the same command again and it resumes where it stopped.

**A large directory (hundreds/thousands of files — Google Photos exports are
notorious for this) can take minutes with zero visible output**, since
progress only prints once a whole directory finishes. `index --debug` (see
the table above) shows what it's actually doing right now, file by file —
without it, a big directory and a genuinely stuck scan look identical.

**Resuming after an interruption has its own silent phase, even with
`--debug`**: fast-forwarding to the saved cursor re-walks (and re-lists)
every directory that comes before it, and that "skip phase" produces zero
output on its own — a hang during it looks exactly like "just resumed,
nothing's happened yet," even though the actual problem directory might be
completely different from the one named in "Resuming interrupted scan
from: ...". `--debug` also prints `listing: <directory>` for each one as it's
re-walked, so a stall shows you precisely which directory it's stuck on.

**A dropped SMB session (idle timeout, brief network hiccup) no longer
crashes the scan.** `index`/`publish`/`dedup` automatically reconnect and
retry (a few attempts, a few seconds apart) before giving up. If you still
see a crash instead of a brief pause, it's a persistent connectivity problem
worth investigating on the NAS/network side, not something a re-run alone
will fix.

**`dedup`'s confirmation pass is hardened the same way.** Every duplicate
candidate over 128 KB gets a full-content read to confirm it (see
"Deduplication" below) — on a large library that's thousands of reads over
the same connection that can drop. A connector's own connection-drop
exception used to slip past this step's narrower error handling and crash
the whole `dedup` run; now it's treated as "can't confirm this one, leave it
alone" instead — safe by construction, since an unconfirmed group never
archives.

**Confirmed hashes are cached in the catalog, per file, as they're computed**
— not just at the end of a run. An interrupted `dedup` (a crash, a killed
container, Ctrl+C) never loses that work: whatever was confirmed before the
interruption is already saved, and re-running only reads the files it hasn't
confirmed yet. A file that's re-indexed with different content automatically
invalidates its cached hash, so this never risks reusing a stale result.

**The skip phase itself is also much faster now.** It used to fetch full
metadata (type, size, modified time) for every file with 3 separate network
calls each — wasted work for files, since the skip phase only needs to know
which entries are directories. It now gets everything from the single
directory-listing response instead, which is the difference between a
directory-heavy resume taking 30+ minutes and taking seconds.

### Starting over (testing)

While testing against a small folder, you'll often want to wipe the local
catalog and re-index from scratch rather than accumulate test data:

```powershell
docker exec media-vault-container python -m mediavault.cli reset nas          # preview
docker exec media-vault-container python -m mediavault.cli reset nas --commit # actually wipe
docker exec media-vault-container python -m mediavault.cli reset --all --commit  # every source
```

This only clears the local SQLite catalog (item rows + scan checkpoints) — it
never touches the NAS or anything already pushed to GCS/Firestore. Since
thumbnails are content-addressed by file hash, re-publishing after a reset +
re-index finds the old blob still there and skips straight to writing the
fact — no wasted re-upload.

**If you're widening the scan root** (e.g. a subfolder → the whole share),
add `--purge-facts` too. Facts are keyed by `item_id`, which is a path
relative to whatever root you scanned — widening the root changes that path
for the same files, so old facts don't get overwritten, they go stale
alongside new ones:

```powershell
docker exec media-vault-container python -m mediavault.cli reset nas --purge-facts --commit
```

This deletes matching Firestore documents (or local JSON files) too. GCS
thumbnails are still left alone — no reason to touch them, they're
content-addressed and re-publishing reuses them regardless of path.

### Publish (thumbnails + metadata)

`publish` walks the catalog for items that haven't been pushed yet — for each
one it generates a thumbnail and writes a metadata document, so the (not yet
built) web module has something to browse. Content-addressed by hash, so a
re-run only touches what's new; already-published items are skipped without a
network call. Without `GCS_LIVE=1`, thumbnails land in a local folder
(`--blob-dir`, default `/data/catalog/blobs`) and metadata in local JSON files
(`--facts-dir`, default `/data/catalog/facts`) — safe to run before any cloud
is configured, to see what it would do. With `GCS_LIVE=1`, both go to the real
Cloud Storage bucket and Firestore (see "Cloud mirror" below).

Each item also gets EXIF pulled from a small header read (dimensions, camera
make/model, real capture date, GPS coordinates where present) via the
`exiftool`/PyExifTool already baked into the image — no extra setup needed.
It's best-effort: files with no EXIF (screenshots, some exports), no GPS block
(most photos — screenshots, edited exports, cameras with location off), or a
missing PyExifTool install just leave those fields empty rather than failing
the item. `doctor` reports whether PyExifTool is available under "Tooling".

**Already-published items are skipped on a normal run** — that's what makes
re-runs cheap, but it also means a fact field added after your library was
first published (like GPS, above) won't show up on old items just by running
`publish` again. `--force` re-processes already-published items too, so they
get the new field without a full `reset` + re-index. Thumbnails are
unaffected either way: they're content-addressed and unchanged, so
`--force` doesn't regenerate them.
```powershell
docker exec media-vault-container python -m mediavault.cli publish nas --force --commit
```

### Deduplication

Duplicates are found **within one source only**. The same photo on the NAS and
in Drive is this system working as designed — Drive is your curated cloud
copy — so that pair is never touched.

Every group keeps exactly one copy: the **oldest**, tie-broken by the
shallowest path. Files over 128 KB are fully hashed before anything is
archived, because sharing a size and both end-chunks is not proof of being
identical. Archived copies **move** (not copy) into trash — the NAS trash
folder, or Drive's 30-day trash — preserving their relative path, so trash
grows its own mirrored subfolder structure as things get archived. Fully
recoverable by moving a file back to where it came from.

**`--limit` only trims the printed preview — it does not cap what `--commit`
actually archives.** On a library with thousands of groups, `dedup nas
--commit` archives *all* of them in one run regardless of `--limit`. To
archive in batches instead, use `--max-groups`:
```powershell
docker exec media-vault-container python -m mediavault.cli dedup nas --max-groups 500 --commit
```

**Confirmation (the full-content read step above) is silent by default on a
large library** — the same silent-but-working problem `index --debug` solved
for scanning. Add `--debug` to see each candidate as it's confirmed:
```powershell
docker exec media-vault-container python -m mediavault.cli dedup nas --debug
```

**On a large library, `dedup nas` listing every group one at a time isn't a
useful way to sanity-check things before committing** — thousands of groups
is too much to read. `--by-folder` summarizes reclaimable space by location
instead:
```powershell
docker exec media-vault-container python -m mediavault.cli dedup nas --by-folder
docker exec media-vault-container python -m mediavault.cli dedup nas --by-folder --depth 4
```
`--depth` controls how many path segments form each bucket (default 3, e.g.
`winfredbe/Photos/2019`). Read-only — ignores `--commit` — so it's safe to run
anytime just to see where the duplication actually is before archiving
anything.

### Amazon

Two ways to stage a file — pick whichever matches where it already is.

**From the NAS directly** (the common case — no local file needed):
```powershell
docker exec media-vault-container python -m mediavault.cli amazon-stage "Photos/2026-01/img_001.jpg" --source nas --commit
```
Reads the item straight off the NAS connector and stages it, preserving the
original filename under a dated album folder (`AMAZON_SMB_ROOT/YYYY-MM/`).

**From a file already on the container's own filesystem** (e.g. something
you separately mounted in):
```powershell
docker exec media-vault-container python -m mediavault.cli amazon upload /path/to/local/img_001.jpg --commit
```

Either way, Amazon's desktop app picks up the staged file and it appears on
your Fire TV. No Amazon API or credentials involved.

---

## Google Drive (optional)

Read + soft-delete (trash) only — nothing in this project ever uploads *to*
Drive. Duplicates are found and archived within Drive the same way as the
NAS: `index drive`, then `dedup drive [--commit]`.

### 1. Create an OAuth client

**Google Cloud Console → APIs & Services → Credentials → Create OAuth client
ID → Desktop app.** Download the JSON into `C:\mediavault\secrets` (already
mounted at `/secrets`) as `drive_credentials.json`. Keep the OAuth consent
screen in **Testing** mode — personal single-user use needs no Google
verification.

### 2. Publish a port for the one-time sign-in

The login step below runs a local callback server *inside* the container —
add a port mapping to the `docker run` command in step 2 of this guide (or
recreate the container with it added):

```powershell
  -p 8080:8080 `
```

### 3. Sign in

```powershell
docker exec media-vault-container python -m mediavault.cli drive-login
```

Prints a URL — open it in **any** browser (doesn't need to be this machine),
sign in, and grant access. After you approve, Google redirects the browser to
`http://localhost:8080/...`, which the published port forwards straight into
the container's waiting server. Saves a refreshable token to
`/secrets/drive_token.json` — this step is one-time; the token renews itself
after that.

### 4. Go live

Add to the `docker run` command:

```powershell
  -e DRIVE_LIVE=1 `
```

Recreate the container, then confirm with `doctor` (see "Command reference"
above) — it reports the OAuth client and saved token under "Google Drive".
From there, `index drive` and `dedup drive` work exactly like their NAS
equivalents.

**One difference from the NAS connector**: `--root` here is a Drive **folder
ID**, not a path — Drive has no path concept, files are related to folders by
ID. Omit it to scan from "My Drive" itself (the default, via Drive's own
`root` alias).

A synced Drive folder mounted as a local path (e.g. `G:\`) is a different,
simpler option if you'd rather point the *mount-based* connector at it
instead of using the API — but it only sees whatever your Drive desktop app
has synced locally, not your whole Drive.

## Cloud mirror (optional)

Both `GCSBlobStore` (thumbnails) and `FirestoreFactsStore` (metadata) are real
clients, not stubs — one `GCS_LIVE=1` switch turns both on, since they use the
same service-account credentials. This is what `publish` (see "Daily use"
above) pushes to.

1. **Cloud Storage → Create bucket.** Single region, Standard class.
2. **Add a lifecycle rule: delete objects under `previews/` after 1 day.**
   This one is load-bearing — without it, full-res fetches accumulate forever.
3. **Firestore → Create database**, Native mode, same project/region as the
   bucket. If you name it anything other than the default (e.g.
   `media-vault-store`), you must set `FIRESTORE_DATABASE` to match below —
   the client looks for a database literally named `(default)` otherwise and
   fails with `NOT_FOUND`.
4. **IAM → Service Accounts → Create.** Grant *Storage Object Admin* and
   *Cloud Datastore User* — the latter covers Firestore Native mode too, it's
   built on the same underlying API.
5. Download the key JSON into `C:\mediavault\secrets` (already mounted at
   `/secrets` by the base `docker run` command in step 2), then add these env
   vars to that command:

```powershell
  -e GCS_LIVE=1 `
  -e GCS_BUCKET=your-bucket-name `
  -e FIRESTORE_DATABASE=your-database-name `
  -e GOOGLE_APPLICATION_CREDENTIALS=/secrets/your-key.json `
```

(omit `FIRESTORE_DATABASE` if you kept the default database name)

6. `doctor` confirms the bucket, key, and Firestore database name are all set. Then:

```powershell
docker exec media-vault-container python -m mediavault.cli publish nas --commit
```

pushes real thumbnails to the bucket and metadata documents to Firestore's
`items` collection.

---

## Web viewer

`web/` is a minimal static page — no build step, single-user Google
sign-in — that reads the `items` Firestore collection and shows a thumbnail
grid. It's an MVP placeholder, not the eventual React app. A second tab,
**Map**, pins every geotagged item on a Leaflet/OpenStreetMap map — no API
key, no cost, loaded from a CDN only when that tab is opened. Most photos
have no GPS at all (screenshots, edited exports, location off), so an empty
or sparse map is normal, not a sign anything's broken. Access is gated
server-side by `firestore.rules` and `storage.rules`, both hardcoded to one
owner email — not by hiding the URL. GCS's `allUsers` IAM grant explicitly
cannot be scoped to a prefix (Google rejects conditions on public principals),
so this is the actual private-by-default path, not a workaround.

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

Both files already have `winfredbe@gmail.com` hardcoded as the only allowed
reader — edit that line in both files first if it should be a different
account.

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
export const OWNER_EMAIL = "winfredbe@gmail.com";
```

None of this is secret — Firebase web API keys are safe to commit; access is
controlled by the security rules in step 4, not by hiding this file.
`OWNER_EMAIL` here only controls which screen the page shows (signed-in vs.
"not authorized") — it is not what makes this private.

### 6. Preview locally (optional but fast)

```powershell
cd web
python -m http.server 8000
```

Open `http://localhost:8000`, click **Sign in with Google**, and use the
`winfredbe@gmail.com` account — if steps 1–5 are right, you'll see the
thumbnail grid, no deploy needed yet. Any other Google account gets a clean
"not authorized" message, and the security rules reject it either way even if
the client-side check were somehow bypassed.

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
