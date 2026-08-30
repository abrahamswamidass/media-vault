# Setup

Everything runs as one long-lived container, started once with `docker run`,
then driven with `docker exec`. No repo checkout, no `.env` file, no compose —
just the published image plus environment variables on the run command.

This page only covers getting the container running and confirming it's
healthy. Once `doctor` is green, go to:

- **[agent.md](agent.md)** — every CLI command (`index`, `dedup`, `publish`,
  `process-intents`, ...), what each one actually touches, Google Drive,
  face detection, and the Cloud Storage/Firestore mirror.
- **[web.md](web.md)** — setting up and using the Firebase-hosted web viewer
  (Browse/Map/Folders/Amazon), once you have something published to look at.
- **[command-catalog.html](command-catalog.html)** — a one-page visual
  reference for which command touches the NAS, the local catalog, or the
  cloud mirror, if you'd rather see it at a glance than read the prose.

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
  ghcr.io/abrahamswamidass/media-vault/agent:latest
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
  in [agent.md](agent.md)). Create it first if it doesn't exist. Safe to
  mount even if empty.
- No trailing command is needed: the image's own default (see the
  Dockerfile) is `process-intents --watch --interval 600`, so the container
  starts polling for web-module requests immediately and keeps running —
  you can still `exec` into it repeatedly for one-off commands (`index`,
  `dedup`, `publish`, ...) the same way regardless of what its main process
  is doing. This is safe to leave running even before any cloud config is
  set up — with no Firestore configured yet, it just polls an empty local
  intents folder every 10 minutes, effectively free either way. `docker
  stop`/`docker restart` cleanly stop and restart the watcher along with the
  container. Append ` sleep infinity` instead if you'd rather the container
  just idle and drive everything by hand via `process-intents --commit`.

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

## Next steps

Once `doctor` is green: [agent.md](agent.md) covers indexing, deduplication,
publishing, Google Drive, and face detection. [web.md](web.md) covers standing
up the browser viewer once you have something published.
