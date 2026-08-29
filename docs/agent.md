# Agent guide

Assumes the container from [setup.md](setup.md) is already running and
`doctor` is green. Everything below runs as `docker exec
media-vault-container python -m mediavault.cli <command>`. See
[command-catalog.html](command-catalog.html) for a one-page visual summary of
what each command actually touches.

## Command reference

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
| `publish nas --mime-only --commit` | Only items with `mime` already set — for a library only partially re-indexed since mime detection was added (see below), so `--max-items` targets what's actually been re-indexed instead of the oldest-indexed items with none. |
| `reset nas [--commit]` | Wipe the local catalog for one source, to re-index from scratch. Add `--purge-facts` when widening the scan root. |
| `reset --all --commit` | Same, for every source. |
| `amazon-stage "<path>" --source nas --commit` | Stage a file straight off the NAS for Amazon Photos, no local copy needed. |
| `amazon upload /path/to/file --commit` | Stage a file that's already on the container's own filesystem. |
| `drive-login` | One-time interactive OAuth grant — see [Google Drive](#google-drive-optional). |
| `index drive` | Resumable walk into the catalog, same as `index nas`. Needs `DRIVE_LIVE=1` and a saved token. |
| `dedup drive [--commit]` | Preview / archive Drive duplicates — same flags as `dedup nas` (`--by-folder`, `--debug`, `--max-groups`, ...). |
| `stats` | Already covers Drive once indexed — one command, all sources together, no `drive`-specific variant needed. |
| `process-intents` | Preview what the web module has requested — read-only, claims/runs nothing. |
| `process-intents --commit` | Claim and run pending requests from the web module (e.g. "stage this for Amazon"), writing status/result back. One pass. See [web.md](web.md#staging-a-photo-for-amazon). |
| `process-intents --watch --interval 600` | Same, but loops forever polling every `interval` seconds — run with `docker exec -it` so Ctrl+C actually stops it. |
| `people` | List detected face clusters (local catalog only). Needs `FACES_LIVE=1` during publish to have found anything. See [Face detection](#face-detection-optional-agent-side-only-for-now). |
| `people-rename <id> "Name"` | Name a person — local catalog only, no Firestore yet. |

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

**This is a full wipe, not a light undo button** — see the warning in
[command-catalog.html](command-catalog.html) if you're tempted to reach for
it just to clear a small test's published state; it takes the whole source's
quick_hash/EXIF/dedup progress with it, not just publish flags.

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
one it generates a thumbnail and writes a metadata document, so the web
module has something to browse. Content-addressed by hash, so a re-run only
touches what's new; already-published items are skipped without a network
call. Without `GCS_LIVE=1`, thumbnails land in a local folder (`--blob-dir`,
default `/data/catalog/blobs`) and metadata in local JSON files (`--facts-dir`,
default `/data/catalog/facts`) — safe to run before any cloud is configured,
to see what it would do. With `GCS_LIVE=1`, both go to the real Cloud Storage
bucket and Firestore (see "Cloud mirror" below).

**HEIC (the default iPhone photo format since iOS 11) decodes out of the
box** — `pillow-heif` is baked into the image alongside Pillow itself, no
extra setup. Without it, every HEIC would fail with an unhelpful "cannot
identify image file" rather than publishing.

**Video thumbnails work the same way, one step earlier**: Pillow can't open
a `.MOV`/`.mp4` container at all, so `publish` pulls one representative
frame via `ffmpeg` (baked into the image alongside `exiftool` — no extra
setup) before handing it to the same thumbnail/preview pipeline a photo
goes through. It seeks 1 second into the clip (a phone video's very first
frame is often black or still mid-focus-hunt) and falls back to frame zero
for anything shorter. If `ffmpeg` is ever missing from the image, a video
fails publish with a clear "ffmpeg is not installed" error rather than
Pillow's unhelpful "cannot identify image file."

Each item also gets EXIF pulled from a small header read (dimensions, camera
make/model, real capture date, GPS coordinates, video duration, and shooting
settings — aperture, shutter speed, ISO, exposure compensation, focal length
(plus 35mm-equivalent), metering mode, flash — where present) via the
`exiftool`/PyExifTool already baked into the image — no extra setup needed.
It's best-effort: files with no EXIF (screenshots, some exports), no GPS
block (most photos — screenshots, edited exports, cameras with location
off), or a missing PyExifTool install just leave those fields empty rather
than failing the item. `doctor` reports whether PyExifTool is available under
"Tooling".

**Shooting settings are deliberately kept as exiftool's own print-converted
strings**, not raw numeric codes — e.g. metering mode comes back as
`"Pattern"` and flash as `"Flash, compulsory"`, not the bare EXIF integers
those map from. That's why `extract()` never passes exiftool's `-n` (numeric
mode) flag.

**The capture date tries three EXIF sources before giving up**:
`DateTimeOriginal`, then `CreateDate`, then (for video) `QuickTime:CreateDate`
— each only used if it's plausible (roughly 1995–now; a camera with a dead
clock battery resetting to a manufacture-era or epoch date is a real, common
failure mode, and a bogus date shouldn't win over a genuine one from a
different tag). If every source is missing or implausible, the web viewer's
own fallback to file-modified time still applies.

**Video duration is best-effort in a different way**: it lives in the file's
own container metadata (the moov atom for `.MOV`/`.MP4`), which some
recording tools write at the very *end* of the file — only "fast start"
files put it up front. A video whose duration didn't make it into that same
small header read just comes back with no duration, same as a photo with no
EXIF. Reading further to chase it would mean downloading arbitrarily large
video files just for a number, which is exactly what this project avoids
everywhere else (see the dedup confirmation notes above).

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

**One field `--force` alone won't backfill: `mime`.** Unlike the EXIF fields
above (all read fresh at publish time), `mime` is set once, at *index* time,
from the connector's own directory listing — so an item indexed before this
was added stays stuck with no MIME type until it's indexed again:
```powershell
docker exec media-vault-container python -m mediavault.cli index nas
```
A plain re-run like this is safe and only touches what's actually still
missing — it re-walks the tree (the `scandir()` fix above makes *resuming*
an interrupted scan's skip phase fast, but a full fresh walk from the root
still means one network round trip per directory; expect this to take
hours on a library the size of a real NAS, not the seconds a resume implies)
— and `catalog.upsert()` overwrites each row's metadata regardless of
whether anything changed, so `mime` fills in for everything without needing
a `reset` first.

**If a re-index is still in progress (or you only want to test against what
it's reached so far)**, `--mime-only` restricts `publish` to items that
already have `mime` set, so `--max-items` doesn't keep landing on the
oldest-indexed items that still have none:
```powershell
docker exec media-vault-container python -m mediavault.cli publish nas --mime-only --max-items 100 --commit
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

Two files sharing a size and matching first/last 64 KB but genuinely
differing in the middle (a coincidental fingerprint collision — RAW files
with same-model fixed header/footer layouts are the case most likely to hit
this) are left alone, with a note explaining why, rather than archived on a
false positive.

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

**`stats`'s "dup groups" / "reclaimable" numbers are a raw, pre-confirmation
estimate**, not the same number `dedup`'s preview shows. `stats` groups purely
by `quick_hash` with no full-content check, so a false-positive fingerprint
collision (see above) inflates both figures and will keep showing up there
indefinitely — it's cosmetic, not a sign of unfinished work. Run `dedup
<source>` itself for the real, confirmed picture.

### Amazon

Three ways to stage a file — pick whichever matches where it already is.

**From the web viewer**: click a photo in Browse or Map, then "Stage for
Amazon" — this writes an intent, it doesn't stage the file immediately. Run
`process-intents --commit` (a one-off, or `--watch` to poll continuously —
see [web.md](web.md#staging-a-photo-for-amazon)) for the agent to actually
pick it up. The Amazon tab shows the result once it has one.

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
add a port mapping to the `docker run` command in [setup.md](setup.md) (or
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

## Face detection (optional, agent-side only for now)

Detects faces during `publish` and clusters them — "recognize people" here
means grouping photos of the same unlabeled person together, not identifying
anyone by name out of the box; there's no model that already knows your
family. `insightface` (ONNX-based, no C++ toolchain needed) is baked into
the image already. Set the live switch to turn it on:

```powershell
  -e FACES_LIVE=1 `
```

**Model weights (`buffalo_l`, ~280MB) are baked into the image at build
time** (see the Dockerfile) — no internet access needed at runtime, and no
first-run download delay. It still costs real CPU time per photo, though:
expect a `publish` run to take noticeably longer with this on than without it.

It runs on the same full-resolution read `publish` already does for
thumbnailing — no extra NAS traffic. Video files are skipped (needs an
actual `mime` starting `image/`, so run `index nas` at least once after
upgrading to this version — see the MIME note above). Detection is
idempotent per item: a `--force` re-run to backfill an unrelated field
(e.g. GPS) won't re-detect faces or duplicate rows for an item that's
already been through it once.

**To try it on a handful of images first**, without waiting on a full
library pass, override the env var for one command and cap the batch:
```powershell
docker exec -e FACES_LIVE=1 media-vault-container python -m mediavault.cli publish nas --max-items 20 --commit
docker exec media-vault-container python -m mediavault.cli people
```
`docker exec -e` only affects that one invocation — no container recreation
needed for a quick test. There's currently no light "undo" for a test batch:
detection's idempotency guard lives in the local `faces` table, separate from
`published_at`, so clearing Firestore and re-publishing with `--force`
re-pushes the *same* already-computed clustering rather than re-running
detection. Don't reach for `reset nas` either — see the warning above.

**What's stored where** — deliberately split, for privacy as much as
architecture:

```powershell
docker exec media-vault-container python -m mediavault.cli people
docker exec media-vault-container python -m mediavault.cli people-rename 3 "Mom"
```

Face bounding boxes and the actual embedding vectors never leave the local
SQLite catalog — `people`/`people-rename` above are local-catalog-only, no
Firestore involved. Firestore only ever sees an opaque `person_ids` array on
each published item (see `item.schema.json`) — no biometric data in the
cloud database. **There's no `people/` Firestore collection yet** — that's
the piece a future "People" web tab would need (names, a cover photo per
person) and it's deliberately not built yet; `person_ids` on published items
is forward-compatible plumbing for it, not a complete feature on its own
today.

## Cloud mirror (optional)

Both `GCSBlobStore` (thumbnails) and `FirestoreFactsStore` (metadata) are real
clients, not stubs — one `GCS_LIVE=1` switch turns both on, since they use the
same service-account credentials. This is what `publish` pushes to, and
what [web.md](web.md) reads from.

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
   `/secrets` by the base `docker run` command in [setup.md](setup.md)), then
   add these env vars to that command:

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
`items` collection. From here, continue to [web.md](web.md) to set up the
browser viewer.
