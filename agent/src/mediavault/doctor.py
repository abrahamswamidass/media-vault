"""
`mediavault doctor` — tell me what's configured and what isn't.

Configuration for this system is mostly paths, and paths break quietly: a NAS
share drops overnight, a trash folder is mounted read-only, a credentials file
never got downloaded. Every one of those turns into a confusing failure halfway
through a scan.

So: one command that checks everything, says which of the four things you
actually need to set up are done, and for anything missing gives the exact fix.
Run it before a scan, and run it first when something behaves oddly.
"""
from __future__ import annotations

import os
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

OK, WARN, FAIL = "ok", "warn", "fail"

#: Cosmetic only — the CLI prints these next to each result.
MARKS = {OK: "✓", WARN: "!", FAIL: "✗"}


@dataclass
class Check:
    group: str
    name: str
    status: str
    detail: str
    fix: str = ""

    @property
    def blocking(self) -> bool:
        return self.status == FAIL


def _dir_check(group: str, name: str, path: str | None, *, need_write: bool,
               required: bool, fix: str) -> Check:
    status_if_missing = FAIL if required else WARN

    if not path:
        return Check(group, name, status_if_missing, "not configured", fix)

    p = Path(path).expanduser()
    if not p.exists():
        return Check(group, name, status_if_missing, f"does not exist: {p}", fix)
    if not p.is_dir():
        return Check(group, name, status_if_missing, f"not a directory: {p}", fix)
    if not os.access(p, os.R_OK):
        return Check(group, name, FAIL, f"not readable: {p}",
                     "Check the mount and its permissions.")
    if need_write and not os.access(p, os.W_OK):
        return Check(group, name, FAIL, f"not writable: {p}",
                     "Soft-deletes land here, so this path must be writable. "
                     "In docker-compose.yml it is mounted :rw.")

    entries = sum(1 for _ in p.iterdir()) if os.access(p, os.R_OK) else 0
    return Check(group, name, OK, f"{p} ({entries} entries)")


def _file_check(group: str, name: str, path: str | None, *, required: bool,
                fix: str) -> Check:
    status_if_missing = FAIL if required else WARN
    if not path:
        return Check(group, name, status_if_missing, "not configured", fix)
    p = Path(path).expanduser()
    if not p.is_file():
        return Check(group, name, status_if_missing, f"missing: {p}", fix)
    return Check(group, name, OK, str(p))


def _binary_check(group: str, name: str, binary: str, fix: str) -> Check:
    found = shutil.which(binary)
    if found:
        return Check(group, name, OK, found)
    return Check(group, name, WARN, f"{binary} not on PATH", fix)


def run_checks() -> list[Check]:
    """Every check, grouped by the thing being configured."""
    checks: list[Check] = []
    env = os.environ.get

    # --- 1. NAS: the source of truth. Nothing works without this. --------- #
    if env("NAS_MODE", "mount") == "smb":
        host, share = env("NAS_HOST"), env("NAS_SHARE")
        if host and share:
            checks.append(Check("NAS", "share", OK, f"smb://{host}/{share}/{env('NAS_SMB_ROOT', '')}"))
        else:
            checks.append(Check(
                "NAS", "share", FAIL, "NAS_HOST and/or NAS_SHARE not set",
                "NAS_MODE=smb needs NAS_HOST (e.g. 192.168.6.110) and NAS_SHARE "
                "(e.g. homes) set in agent/.env."))
        if env("NAS_PASSWORD_FILE") or env("NAS_PASSWORD"):
            checks.append(Check(
                "NAS", "smb credentials", OK,
                "via NAS_PASSWORD_FILE" if env("NAS_PASSWORD_FILE") else "via NAS_PASSWORD env var"))
        else:
            checks.append(Check(
                "NAS", "smb credentials", WARN, "not configured",
                "Set NAS_USER and either NAS_PASSWORD_FILE (a file under secrets/, "
                "preferred) or NAS_PASSWORD (plain env var) in agent/.env if the "
                "share needs auth."))
    else:
        checks.append(_dir_check(
            "NAS", "share", env("NAS_ROOT"), need_write=False, required=True,
            fix="Set HOST_NAS_PATH in agent/.env to your share, e.g. /mnt/nas/Photos "
                "or //DISKSTATION/Photos. It is mounted read-only on purpose. If a "
                "mapped Windows drive mounts empty in Docker Desktop, try "
                "NAS_MODE=smb instead (talks SMB directly, no OS mount needed)."))
        checks.append(_dir_check(
            "NAS", "trash", env("NAS_TRASH"), need_write=True, required=True,
            fix="Set HOST_TRASH_PATH in agent/.env. Deletes and archived duplicates "
                "move here, so it must be writable and on the same volume as the share."))

    # --- 2. Google Drive: OAuth, one-time, interactive. ------------------- #
    drive_live = env("DRIVE_LIVE", "0") == "1"
    creds = env("DRIVE_CREDENTIALS") or "/secrets/drive_credentials.json"
    if drive_live:
        checks.append(_file_check(
            "Google Drive", "oauth client", creds, required=True,
            fix="Google Cloud Console -> APIs & Services -> Credentials -> "
                "Create OAuth client ID -> Desktop app. Download the JSON to "
                "secrets/drive_credentials.json. Keep the consent screen in "
                "Testing mode; personal single-user use needs no verification."))
        checks.append(_file_check(
            "Google Drive", "saved token", env("DRIVE_TOKEN") or "/secrets/drive_token.json",
            required=False,
            fix="Created automatically the first time you authorise. "
                "Run: mediavault drive-login"))
    else:
        checks.append(Check(
            "Google Drive", "live mode", WARN, "DRIVE_LIVE=0 — Drive runs read-only "
            "and every write is a dry-run",
            "Set DRIVE_LIVE=1 in agent/.env once the OAuth client JSON is in place."))

    # --- 3. Amazon: no API, no keys. Just a watched folder. -------------- #
    if env("NAS_MODE", "mount") == "smb":
        checks.append(Check(
            "Amazon", "staging folder", OK,
            f"smb share-relative: {env('AMAZON_SMB_ROOT', '_AmazonUpload')}"))
    else:
        checks.append(_dir_check(
            "Amazon", "staging folder", env("AMAZON_STAGING"), need_write=True,
            required=False,
            fix="Set HOST_AMAZON_STAGING in agent/.env to the folder the Amazon Photos "
                "desktop app watches. There are no Amazon credentials — the agent copies "
                "files in and Amazon's own app uploads them."))

    # --- 4. Google Cloud mirror: service account. ------------------------ #
    gcs_live = env("GCS_LIVE", "0") == "1"
    if gcs_live:
        checks.append(Check(
            "Cloud mirror", "bucket", OK if env("GCS_BUCKET") else FAIL,
            env("GCS_BUCKET") or "GCS_BUCKET not set",
            "Set GCS_BUCKET in agent/.env. Add a lifecycle rule deleting "
            "previews/ after 1 day — without it, full-res fetches accumulate."))
        checks.append(_file_check(
            "Cloud mirror", "service account", env("GOOGLE_APPLICATION_CREDENTIALS"),
            required=True,
            fix="Google Cloud Console -> IAM -> Service Accounts -> create one with "
                "Storage Object Admin + Cloud Datastore User. Download the JSON key "
                "to secrets/ and point GOOGLE_APPLICATION_CREDENTIALS at it."))
    else:
        checks.append(Check(
            "Cloud mirror", "live mode", WARN,
            "GCS_LIVE=0 — nothing is pushed to the cloud",
            "Set GCS_LIVE=1 in agent/.env once the bucket and service account exist. "
            "Everything else works without this."))

    # --- 5. Local state -------------------------------------------------- #
    catalog_db = env("CATALOG_DB", "/data/catalog/catalog.sqlite")
    catalog_dir = Path(catalog_db).parent
    checks.append(_dir_check(
        "Local state", "catalog folder", str(catalog_dir), need_write=True,
        required=True,
        fix="Set HOST_CATALOG in agent/.env. The index, action journal, and "
            "thumbnail cache live here and must survive between runs."))

    # --- 6. Optional tooling --------------------------------------------- #
    checks.append(_binary_check(
        "Tooling", "exiftool", "exiftool",
        "Baked into the Docker image. Only missing if you are running outside it."))
    checks.append(_binary_check(
        "Tooling", "ffprobe", "ffprobe",
        "Baked into the Docker image. Needed for video metadata."))
    try:
        import PIL  # noqa: F401
        checks.append(Check("Tooling", "Pillow", OK, "available"))
    except ImportError:
        checks.append(Check(
            "Tooling", "Pillow", WARN, "not installed",
            "Needed only for thumbnails and previews: pip install Pillow"))

    return checks


def report(checks: list[Check]) -> dict:
    counts = {OK: 0, WARN: 0, FAIL: 0}
    for c in checks:
        counts[c.status] += 1
    return {
        "ok": counts[OK],
        "warnings": counts[WARN],
        "failures": counts[FAIL],
        "ready": counts[FAIL] == 0,
    }
