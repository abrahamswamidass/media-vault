# Media Vault — Local Agent (Docker)

A portable, Dockerized **local agent** that reads your Synology NAS (the source of
truth), extracts rich metadata, and safely tests file operations (`list`, `stat`,
`read`, `delete`, `upload`) across four connectors: **NAS, Google Drive, exported
archives, and an Amazon staging folder**.

> **Design principle:** only this agent ever touches your files. Mutating
> operations are **dry-run by default** — nothing changes until you add `--commit`.
> NAS deletes are **soft** (move to a trash folder), so they're reversible.

---

## 📦 What's in this bundle

Two modules that never call each other, plus the contract they meet in.

```
media-vault/
├── agent/                  # MODULE 1 — local Python. The only thing that touches files.
│   ├── Dockerfile          #   image w/ exiftool + ffmpeg baked in
│   ├── docker-compose.yml  #   volume mounts (edit paths to your machine)
│   ├── .env.example        #   copy to .env and set your host paths
│   ├── pyproject.toml
│   ├── src/mediavault/
│   │   ├── ports.py        #   Connector + BlobStore interfaces
│   │   ├── connectors/     #   one adapter per source: nas, drive, archive, amazon
│   │   ├── blobstore.py    #   local (tests) and GCS (real) blob adapters
│   │   ├── imaging.py      #   thumbnail + preview derivation
│   │   ├── actions/        #   every mutation, as a Command object
│   │   ├── sync/           #   intents in, facts out
│   │   └── cli.py
│   └── tests/              #   runs with no cloud account and no Docker
├── web/                    # MODULE 2 — React on Firebase Hosting (not yet built)
├── shared/contracts/       # JSON Schema both modules validate against
├── docs/                   # setup.md · agent.md · web.md · architecture.html · command-catalog.html
├── run.sh / run.bat        # convenience wrappers (Linux-macOS / Windows)
└── sample/                 # a fake NAS so you can try it with zero setup
```

Start with [docs/setup.md](docs/setup.md) to get the container running, then
[docs/agent.md](docs/agent.md) for CLI commands and [docs/web.md](docs/web.md)
for the browser viewer. See [docs/architecture.html](docs/architecture.html)
for how the two modules stay in sync, and [CLAUDE.md](CLAUDE.md) for the design rules.

---

## 0) Prerequisites — install Docker

### Windows
1. Install **Docker Desktop for Windows**: <https://www.docker.com/products/docker-desktop/>
2. During setup, keep **“Use WSL 2 based engine”** enabled (recommended).
3. Reboot if prompted, then launch **Docker Desktop** and wait for it to say *Running*.
4. Verify in **PowerShell** or **Git Bash**:
   ```bash
   docker --version
   docker compose version
   ```
5. **Share your drives:** Docker Desktop → **Settings → Resources → File Sharing**
   and add the drive holding your NAS mount / data (e.g. `C:` or your mapped drive).

### Linux
```bash
# Docker Engine + compose plugin (Debian/Ubuntu)
sudo apt-get update
sudo apt-get install -y docker.io docker-compose-plugin
sudo systemctl enable --now docker

# run docker without sudo (log out/in afterwards)
sudo usermod -aG docker "$USER"

docker --version
docker compose version
```

---

## 1) Configure your paths

```bash
cd agent
cp .env.example .env
```

Open `.env` and point the **HOST_*** variables at your real folders. Only the
left-side host paths change; the container paths under `/data` stay fixed.

| Variable | What it is | Example (Win) | Example (Linux/macOS) |
|---|---|---|---|
| `HOST_NAS_PATH` | Your NAS share (read-only) | `//NAS-HOST/Photos` or `Z:/Photos` | `/mnt/nas/Photos` · `/Volumes/Photos` |
| `HOST_TRASH_PATH` | Where soft-deletes go (writable) | `Z:/Photos/_trash` | `/mnt/nas/Photos/_trash` |
| `HOST_AMAZON_STAGING` | Folder the Amazon app watches | `C:/AmazonUpload` | `/mnt/nas/_AmazonUpload` |
| `HOST_ARCHIVES` | Takeout / Amazon export dumps | `C:/exports` | `/mnt/exports` |
| `HOST_CATALOG` | Local SQLite + thumbnails | `./data/catalog` | `./data/catalog` |
| `HOST_SECRETS` | GCP/Drive creds (read-only) | `./secrets` | `./secrets` |

> 💡 **Windows path tips:** in `.env`, use **forward slashes** (`Z:/Photos`) or
> double backslashes. For a Synology share, a UNC path like `//DISKSTATION/Photos`
> works once Docker Desktop file sharing is enabled.

If you just want to **try it first**, leave the defaults — they point at the
included `sample/` fake NAS.

---

## 2) Build the image

```bash
cd agent
docker compose build
```

This installs `exiftool` + `ffmpeg` and your Python deps once, then caches them.

---

## 3) Run commands

The agent is a **task runner**: `./run.sh <args>`.
Use the wrappers to keep it short.

### Linux / macOS
```bash
chmod +x run.sh    # first time only

./run.sh nas caps   --root /data/nas
./run.sh nas list   --root /data/nas --prefix Photos/2026-01
./run.sh nas stat   --root /data/nas "Photos/2026-01/img_001.jpg"

# delete: dry-run, then commit (soft move to /data/nas/_trash)
./run.sh nas delete --root /data/nas "Photos/junk.jpg"
./run.sh nas delete --root /data/nas "Photos/junk.jpg" --commit
```

### Windows (PowerShell or CMD)
```bat
run.bat nas caps   --root /data/nas
run.bat nas list   --root /data/nas --prefix Photos/2026-01
run.bat nas delete --root /data/nas "Photos/junk.jpg"
run.bat nas delete --root /data/nas "Photos/junk.jpg" --commit
```

> ⚠️ **Always use container paths** (`/data/nas`, `/data/archives`, …) in the
> commands — **not** your Windows/host paths. The host↔container mapping is done
> by the volume mounts in `docker-compose.yml`.

### Without Docker
The agent core is standard-library only, so it also runs directly:
```bash
cd agent
PYTHONPATH=src python -m mediavault.cli nas list --root ../sample/nas
python -m pytest tests/ -q
```

---

## 4) The interactive catalog

Open **`docs/command-catalog.html`** in any browser (double-click it) for a visual command
reference with **one-click copy** buttons and the full system architecture diagram.
When running via Docker, just remember to swap host paths for `/data/...` container
paths shown above.

---

## Connectors at a glance

| Connector | read | delete | upload | Notes |
|-----------|:----:|:------:|:------:|-------|
| `nas`     | ✅ | ✅ soft→trash | ✅ copy-in | Source of truth. |
| `drive`   | ✅* | ✅* (perm/trash) | – | *SAFE until `DRIVE_LIVE=1` + OAuth. |
| `archive` | ✅ | – | – | Indexes Takeout / Amazon export folders. |
| `amazon`  | ✅ | – | ✅ stage | Drops files into the Amazon-watched folder. |

---

## Going live on Google Drive (later)

1. In Google Cloud Console, create an **OAuth desktop client**; download the JSON.
2. Save it to `./secrets/drive_credentials.json` (mounted read-only at `/secrets`).
3. In `.env` set `DRIVE_LIVE=1`.
4. Rebuild is **not** needed — just re-run. Keep the OAuth app in **“testing”**
   mode for personal single-user use (no Google verification required).

`--permanent` performs a real delete that reclaims paid Drive space; the default
moves to Drive trash (still counts against quota for ~30 days).

---

## Portability & moving to the NAS / a Linux box

The container **is** the portable unit. To relocate the agent from your laptop to
the Synology (Container Manager) or a NUC:

1. Copy this folder over.
2. Adjust the `HOST_*` paths in `.env` for that machine.
3. `cd agent && docker compose build`, then `./run.sh ...` from the repo root.

No code changes — the app uses POSIX paths internally and reads everything from env.

---

## Troubleshooting

| Symptom | Fix |
|---|---|
| `docker: command not found` | Docker Desktop not running (Win/macOS) or engine not started (`sudo systemctl start docker`). |
| `Error response … mounts denied` (macOS/Win) | Add the drive/folder under Docker Desktop → **Settings → Resources → File Sharing**. |
| `permission denied` writing trash | Ensure `HOST_TRASH_PATH` is writable and mounted `:rw` (it is by default). |
| Commands can’t find files | You used a host path. Use the **container** path (`/data/nas/...`). |
| NAS share won’t mount (Linux) | Mount it on the host first (`mount -t cifs //NAS/Photos /mnt/nas ...`), then point `HOST_NAS_PATH` at `/mnt/nas`. |
| `exiftool: not found` | You’re running Python directly, not the container. Inside Docker it’s pre-installed. |

---

## Safety recap

- **Dry-run by default** on `delete`/`upload`; `--commit` required to act.
- **NAS share mounted read-only**; only `_trash` and staging are writable.
- **Soft delete** = reversible move to `_trash`.
- **Non-root** user inside the container.
- **Secrets mounted read-only**, never baked into the image.
