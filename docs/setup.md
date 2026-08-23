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
  -e NAS_USER=winfredbe `
  -e 'NAS_PASSWORD=your-password-here' `
  -v "C:\mediavault\catalog:/data/catalog" `
  ghcr.io/abrahamswamidass/media-vault/agent:latest `
  sleep infinity
```

Notes:

- **`NAS_HOST`** — your NAS's IP or hostname.
- **`NAS_SHARE`** — the SMB share name (e.g. `homes`).
- **`NAS_SMB_ROOT`** — the path *inside* that share to index, forward slashes,
  no leading slash (e.g. `winfredbe/nov2025-cafc`).
- **`NAS_SMB_TRASH`** — optional; defaults to `<NAS_SMB_ROOT>/_trash`.
- **`NAS_PASSWORD`** — wrap in **single quotes** in PowerShell. Without quotes,
  a `$` in the password gets silently interpreted as a variable and the
  connection fails with `STATUS_LOGON_FAILURE`.
- **`C:\mediavault\catalog`** — any local folder; holds the index database
  between runs. Create it first if it doesn't exist.
- `sleep infinity` keeps the container running so you can `exec` into it
  repeatedly instead of creating a new container per command.

To use the Amazon staging step too, add a volume and env var for it:

```powershell
  -v "Z:\_AmazonUpload:/data/amazon_staging" `
  -e AMAZON_STAGING=/data/amazon_staging `
```

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
```

Indexing a terabyte takes a while and checkpoints after every directory. If it
dies, run the same command again and it resumes where it stopped.

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
docker exec media-vault-container python -m mediavault.cli amazon upload /data/nas/Photos/2026-01/img_001.jpg --commit
```

Stages the file into the watched folder. Amazon's desktop app picks it up and
it appears on your Fire TV. No Amazon API or credentials involved.

---

## Google Drive (optional, not yet implemented)

> `connectors/drive.py` is a guarded stub today — `_service()` still raises
> `NotSupported` even with `DRIVE_LIVE=1`. A synced Drive folder mounted as a
> local path (e.g. `G:\`) won't work either — the connector talks to the
> Drive API, not the filesystem. Skip this until `_service()` is filled in.

## Cloud mirror (optional)

Needed only for thumbnails in a future browser UI.

1. **Cloud Storage → Create bucket.** Single region, Standard class.
2. **Add a lifecycle rule: delete objects under `previews/` after 1 day.**
   This one is load-bearing — without it, full-res fetches accumulate forever.
3. **IAM → Service Accounts → Create.** Grant *Storage Object Admin* and
   *Cloud Datastore User*.
4. Download the key JSON, mount it into the container, and add:

```powershell
  -v "C:\mediavault\secrets:/secrets" `
  -e GCS_LIVE=1 `
  -e GCS_BUCKET=your-bucket-name `
  -e GOOGLE_APPLICATION_CREDENTIALS=/secrets/your-key.json `
```
