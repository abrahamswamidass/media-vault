# Setup

Four things to configure. Only **two of them involve credentials** — NAS is just
paths, and Amazon has no API at all.

| | Needs | Effort |
|---|---|---|
| NAS | a path | edit `.env` |
| Amazon | a path | edit `.env` |
| Google Drive | OAuth client JSON | one-time, ~3 min |
| Cloud mirror | service-account JSON | one-time, ~3 min |

At any point, `doctor` tells you what's done and what isn't.

---

## 1. Get the image

Either pull what CI built:

```bash
docker pull ghcr.io/OWNER/media-vault/agent:latest      # lowercase OWNER
```

Or build it yourself:

```bash
cd agent && docker compose build
```

To use the pulled image with compose, set `MEDIAVAULT_IMAGE` in `.env`.

## 2. Point it at your folders

Two ways to do this — pick whichever matches how you're running the image.

### Option A — Docker Desktop GUI (no terminal, no repo checkout needed)

Click the **▶ Run** button on the image in **Images**, expand **Optional
settings**, and fill in **Volumes** and **Environment variables** directly —
no `.env` file required:

| Host path | Container path |
|---|---|
| your NAS folder, e.g. `Z:\winfredbe\nov2025-cafc` | `/data/nas` |
| a writable trash folder on the same drive, e.g. `Z:\_trash` | `/data/nas/_trash` |
| your Amazon staging folder, e.g. `Z:\_AmazonUpload` | `/data/amazon_staging` |
| a local folder for the index DB, e.g. `C:\mediavault\catalog` | `/data/catalog` |
| a local folder for credentials, e.g. `C:\mediavault\secrets` | `/secrets` |

Environment variables:

```
DRIVE_LIVE=0
GCS_LIVE=0
```

Set the container's command to `doctor` and click **Run**.

> Start with a small test folder (a subfolder, not the whole NAS root) to
> confirm everything works before pointing it at the full share.

### Option B — `.env` file + `run.sh` / `run.bat` (repo checked out locally)

```bash
cd agent
cp .env.example .env
```

Edit the four `HOST_*` paths. Only the left side of each volume mapping changes;
the container paths under `/data` stay fixed.

```ini
HOST_NAS_PATH=/mnt/nas/Photos          # or Z:/Photos, //DISKSTATION/Photos
HOST_TRASH_PATH=/mnt/nas/Photos/_trash # must be writable, same volume
HOST_AMAZON_STAGING=/mnt/nas/_AmazonUpload
HOST_CATALOG=../data/catalog
```

> On Windows use forward slashes, and add the drive under Docker Desktop →
> Settings → Resources → File Sharing.

## 3. Check it

Docker Desktop GUI: re-run the container (command `doctor`) and read the logs
in **Containers**.

CLI:

```bash
./run.sh doctor
```

Green means ready. Anything red prints the exact fix beneath it. Everything below
is optional — indexing and dedup work with just the NAS configured.

### If the NAS mount is empty on Windows

Docker Desktop can't reliably bind-mount a **mapped network drive** (`Z:\...`)
into its Linux VM — the container sees the mount point but the share's actual
files don't come through, so `doctor` reports a real directory with 0 entries
and `index` finds nothing. Local disks (`C:\...`) don't have this problem.

If that's what you're hitting, switch the NAS connector to **SMB-direct
mode**: it skips the OS mount entirely and talks SMB2/3 to the NAS over the
network from inside the container.

```ini
NAS_MODE=smb
NAS_HOST=192.168.6.110      # your NAS's IP or hostname
NAS_SHARE=homes             # the SMB share name
NAS_SMB_ROOT=winfredbe/nov2025-cafc   # path inside the share
NAS_SMB_TRASH=winfredbe/nov2025-cafc/_trash   # optional; defaults to <root>/_trash
NAS_USER=winfredbe
NAS_PASSWORD_FILE=/secrets/nas_password.txt   # preferred: a file, not the password itself
# NAS_PASSWORD=your-password-here             # simpler alternative to the file above
```

Set `NAS_PASSWORD_FILE` (pointing at a plain-text, one-line file under
`secrets/`) or `NAS_PASSWORD` directly — not both. The file is preferred since
it doesn't show up in `docker inspect` or shell history, but the plain env var
works if you'd rather skip managing a secrets file. With `NAS_MODE=smb`,
`HOST_NAS_PATH`/`HOST_TRASH_PATH` and the NAS volume mounts in
`docker-compose.yml` are ignored. Re-run `doctor` — it now checks
`NAS_HOST`/`NAS_SHARE`/credentials instead of the mount path.

---

## Google Drive (optional)

> **Not yet implemented.** `connectors/drive.py` is a guarded stub today — the
> OAuth wiring below is the intended shape, but `_service()` still raises
> `NotSupported` even with `DRIVE_LIVE=1`. A synced Drive folder mounted as a
> local path (e.g. `G:\`) will not work either — the connector talks to the
> Drive API, not the filesystem. Skip this section until `_service()` is filled in.

Drive is the one cloud you can fully automate, so it gets real OAuth.

1. [Google Cloud Console](https://console.cloud.google.com) → **APIs & Services →
   Library** → enable **Google Drive API**.
2. **Credentials → Create credentials → OAuth client ID → Desktop app**.
3. Download the JSON to `secrets/drive_credentials.json`.
4. In `.env`: `DRIVE_LIVE=1`.

Keep the consent screen in **Testing** mode and add yourself as a test user —
personal single-user apps need no Google verification.

```bash
./run.sh doctor          # confirms the client JSON is found
```

## Cloud mirror (optional)

Needed only when you want thumbnails in the browser.

1. **Cloud Storage → Create bucket.** Single region, Standard class.
2. **Add a lifecycle rule: delete objects under `previews/` after 1 day.**
   This one is load-bearing — without it, full-res fetches accumulate forever.
3. **IAM → Service Accounts → Create.** Grant *Storage Object Admin* and
   *Cloud Datastore User*.
4. Download the key JSON to `secrets/`, then in `.env`:

```ini
GCS_LIVE=1
GCS_BUCKET=your-bucket-name
GOOGLE_APPLICATION_CREDENTIALS=/secrets/your-key.json
```

## Amazon

Nothing to configure beyond the folder path. There is no Amazon API here by
design — the agent copies files into the folder the Amazon Photos desktop app
watches, and Amazon's own app does the upload. No keys, nothing to break.

---

## Daily use

```bash
./run.sh doctor                  # is everything reachable?
./run.sh index nas               # walk the NAS into the catalog (resumable)
./run.sh stats                   # what's indexed, what's duplicated
./run.sh dedup nas               # preview: which copies would be archived
./run.sh dedup nas --commit      # archive them (to trash — recoverable)
```

Indexing a terabyte takes a while and checkpoints after every directory. If it
dies, run the same command again and it resumes where it stopped.

### Deduplication

Duplicates are found **within one source only**. The same photo on the NAS and in
Drive is this system working as designed — Drive is your curated cloud copy — so
that pair is never touched.

```bash
./run.sh dedup nas               # NAS internal duplicates
./run.sh dedup drive             # Drive internal duplicates (needs DRIVE_LIVE=1)
```

Every group keeps exactly one copy: the **oldest**, tie-broken by the shallowest
path. Files over 128 KB are fully hashed before anything is archived, because
sharing a size and both end-chunks is not proof of being identical. Archived
copies move to trash — the NAS trash folder, or Drive's 30-day trash — and stay
recoverable.

### Amazon

```bash
./run.sh amazon upload /data/nas/Photos/2026-01/img_001.jpg --commit
```

Stages the file into a dated album folder. Amazon's desktop app picks it up and
it appears on your Fire TV.
