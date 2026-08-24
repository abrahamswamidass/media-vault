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

## Daily use

Every command below runs the same way — `docker exec media-vault-container
python -m mediavault.cli <command>`:

```powershell
docker exec media-vault-container python -m mediavault.cli doctor
docker exec media-vault-container python -m mediavault.cli index nas
docker exec media-vault-container python -m mediavault.cli stats
docker exec media-vault-container python -m mediavault.cli dedup nas
docker exec media-vault-container python -m mediavault.cli dedup nas --commit
docker exec media-vault-container python -m mediavault.cli publish nas
docker exec media-vault-container python -m mediavault.cli publish nas --commit
```

Indexing a terabyte takes a while and checkpoints after every directory. If it
dies, run the same command again and it resumes where it stopped.

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

### Deduplication

Duplicates are found **within one source only**. The same photo on the NAS and
in Drive is this system working as designed — Drive is your curated cloud
copy — so that pair is never touched.

Every group keeps exactly one copy: the **oldest**, tie-broken by the
shallowest path. Files over 128 KB are fully hashed before anything is
archived, because sharing a size and both end-chunks is not proof of being
identical. Archived copies move to trash — the NAS trash folder, or Drive's
30-day trash — and stay recoverable.

### Amazon

```powershell
docker exec media-vault-container python -m mediavault.cli amazon upload /path/to/local/img_001.jpg --commit
```

Stages the file into the watched folder (`AMAZON_SMB_ROOT`, dated by month).
Amazon's desktop app picks it up and it appears on your Fire TV. No Amazon API
or credentials involved. The source path here must be reachable *inside the
container's own filesystem* — not a NAS path, since `upload` copies a local
file onto the share. If the file you want to stage only exists on the NAS,
mount it in (`-v` a local copy, or a small folder) rather than passing an SMB
path directly.

---

## Google Drive (optional, not yet implemented)

> `connectors/drive.py` is a guarded stub today — `_service()` still raises
> `NotSupported` even with `DRIVE_LIVE=1`. A synced Drive folder mounted as a
> local path (e.g. `G:\`) won't work either — the connector talks to the
> Drive API, not the filesystem. Skip this until `_service()` is filled in.

## Cloud mirror (optional)

Both `GCSBlobStore` (thumbnails) and `FirestoreFactsStore` (metadata) are real
clients, not stubs — one `GCS_LIVE=1` switch turns both on, since they use the
same service-account credentials. This is what `publish` (see "Daily use"
above) pushes to.

1. **Cloud Storage → Create bucket.** Single region, Standard class.
2. **Add a lifecycle rule: delete objects under `previews/` after 1 day.**
   This one is load-bearing — without it, full-res fetches accumulate forever.
3. **Firestore → Create database**, Native mode, same project/region as the bucket.
4. **IAM → Service Accounts → Create.** Grant *Storage Object Admin* and
   *Cloud Datastore User*.
5. Download the key JSON into `C:\mediavault\secrets` (already mounted at
   `/secrets` by the base `docker run` command in step 2), then add these env
   vars to that command:

```powershell
  -e GCS_LIVE=1 `
  -e GCS_BUCKET=your-bucket-name `
  -e GOOGLE_APPLICATION_CREDENTIALS=/secrets/your-key.json `
```

6. `doctor` confirms the bucket and key are found. Then:

```powershell
docker exec media-vault-container python -m mediavault.cli publish nas --commit
```

pushes real thumbnails to the bucket and metadata documents to Firestore's
`items` collection.
