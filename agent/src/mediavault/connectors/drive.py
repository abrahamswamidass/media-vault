"""
Google Drive connector — the ONE cloud you can fully automate (read + real delete).

Gated behind DRIVE_LIVE=1 so nothing here reaches Google until you've actually
run through auth. Two files under /secrets make it live:

  1. `drive_credentials.json` — an OAuth Desktop-app client, from Google Cloud
     Console -> APIs & Services -> Credentials. One-time, per Google Cloud project.
  2. `drive_token.json` — the actual per-user grant. Created by running
     `mediavault drive-login` once; refreshed automatically after that.

Delete mode: trash (recoverable, 30 days) by default — matches the NAS connector's
soft-delete semantics and CLAUDE.md's rule that archiving is always reversible.
`permanent=True` skips the trash and reclaims quota immediately; nothing in this
project's dedup path ever sets that.
"""
from __future__ import annotations

import hashlib
import os
from datetime import datetime
from typing import Iterable

from ..ports import Connector, FileRecord, OpResult, NotSupported

LIVE = os.getenv("DRIVE_LIVE", "0") == "1"

#: Full read/write scope — narrower scopes (e.g. drive.file) only see files the
#: app itself created, which is useless for indexing an existing library.
SCOPES = ["https://www.googleapis.com/auth/drive"]

_FOLDER_MIME = "application/vnd.google-apps.folder"
_FIELDS = "id,name,size,mimeType,modifiedTime"


def _parse_rfc3339(value: str) -> float:
    """Drive's modifiedTime, e.g. "2024-05-01T12:34:56.789Z" -> epoch seconds."""
    return datetime.fromisoformat(value).timestamp()


def _load_credentials(credentials_path: str, token_path: str):
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials

    if not os.path.isfile(token_path):
        raise NotSupported(
            f"No saved Drive token at {token_path} — run: mediavault drive-login")
    creds = Credentials.from_authorized_user_file(token_path, SCOPES)
    if creds.expired and creds.refresh_token:
        creds.refresh(Request())
        with open(token_path, "w", encoding="utf-8") as f:
            f.write(creds.to_json())
    return creds


def _download_range(svc, file_id: str, start: int | None, end: int | None) -> bytes:
    """One ranged (or whole-file, if start is None) media download."""
    request = svc.files().get_media(fileId=file_id)
    if start is not None:
        request.headers["Range"] = f"bytes={start}-{end}"
    return request.execute()


def _quick_fingerprint_drive(svc, file_id: str, size: int, chunk: int = 65536) -> str:
    """Same cheap fingerprint as the NAS connector: size + first/last chunk hash.

    Two ranged downloads instead of NAS's single open-file-and-seek, since
    Drive has no local file handle to seek within — a Range header per chunk
    is the Drive-native equivalent.
    """
    h = hashlib.sha1()
    h.update(str(size).encode())
    if size:
        head = _download_range(svc, file_id, 0, min(chunk, size) - 1)
        h.update(head)
        if size > chunk:
            tail = _download_range(svc, file_id, size - chunk, size - 1)
            h.update(tail)
    return f"{size}:{h.hexdigest()}"


class DriveConnector(Connector):
    name = "drive"
    can_delete = True
    can_upload = False   # we don't push files INTO Drive in this project

    def __init__(self, credentials_path: str | None = None, token_path: str | None = None,
                 root_folder_id: str | None = None, permanent: bool = False):
        self.credentials_path = credentials_path or os.getenv(
            "DRIVE_CREDENTIALS", "/secrets/drive_credentials.json")
        self.token_path = token_path or os.getenv("DRIVE_TOKEN", "/secrets/drive_token.json")
        # "root" is Drive's own alias for the user's My Drive root — no lookup needed.
        self.root_folder_id = root_folder_id or os.getenv("DRIVE_ROOT_FOLDER_ID", "root")
        self.permanent = permanent          # False => trash, True => hard delete
        self._svc = None

    def _service(self):
        if not LIVE:
            raise NotSupported(
                "Drive is in SAFE mode (DRIVE_LIVE!=1). List/stat/delete run as dry-run only. "
                "Run drive-login and set DRIVE_LIVE=1 to go live."
            )
        if self._svc is None:
            from googleapiclient.discovery import build

            creds = _load_credentials(self.credentials_path, self.token_path)
            self._svc = build("drive", "v3", credentials=creds, cache_discovery=False)
        return self._svc

    # --- read side -------------------------------------------------------- #
    def list(self, prefix: str = "", limit: int = 100) -> Iterable[FileRecord]:
        if not LIVE:
            # Return nothing but don't crash — lets you smoke-test the CLI plumbing.
            return iter(())
        return self._list_folder(self._service(), prefix or self.root_folder_id, limit)

    def _list_folder(self, svc, folder_id: str, limit: int) -> Iterable[FileRecord]:
        count = 0
        page_token = None
        while True:
            resp = svc.files().list(
                q=f"'{folder_id}' in parents and trashed = false",
                fields=f"nextPageToken, files({_FIELDS})",
                pageSize=min(1000, limit - count) if limit else 1000,
                pageToken=page_token,
            ).execute()
            for f in resp.get("files", []):
                is_dir = f["mimeType"] == _FOLDER_MIME
                yield FileRecord(
                    id=f["id"], name=f["name"], source=self.name,
                    size=None if is_dir else int(f.get("size", 0)),
                    mtime=_parse_rfc3339(f["modifiedTime"]),
                    mime=None if is_dir else f["mimeType"], is_dir=is_dir,
                )
                count += 1
                if limit and count >= limit:
                    return
            page_token = resp.get("nextPageToken")
            if not page_token:
                return

    def stat(self, item_id: str) -> FileRecord:
        if not LIVE:
            return FileRecord(id=item_id, name="<safe-mode>", source=self.name,
                              extra={"note": "DRIVE_LIVE!=1"})
        svc = self._service()
        f = svc.files().get(fileId=item_id, fields=_FIELDS).execute()
        is_dir = f["mimeType"] == _FOLDER_MIME
        size = None if is_dir else int(f.get("size", 0))
        return FileRecord(
            id=f["id"], name=f["name"], source=self.name, size=size,
            mtime=_parse_rfc3339(f["modifiedTime"]),
            mime=None if is_dir else f["mimeType"], is_dir=is_dir,
            quick_hash=None if is_dir else _quick_fingerprint_drive(svc, f["id"], size),
        )

    def read(self, item_id: str, nbytes: int = 0) -> bytes:
        if not LIVE:
            raise NotSupported("Drive is in SAFE mode (DRIVE_LIVE!=1) — nothing to read.")
        svc = self._service()
        if nbytes <= 0:
            return _download_range(svc, item_id, None, None)
        return _download_range(svc, item_id, 0, nbytes - 1)

    # --- write side ------------------------------------------------------- #
    def delete(self, item_id: str, commit: bool = False) -> OpResult:
        mode = "permanent delete" if self.permanent else "move to Drive trash"
        if not commit or not LIVE:
            why = "dry-run" if not commit else "SAFE mode (DRIVE_LIVE!=1)"
            return self.dryrun_result("delete", item_id,
                                      detail=f"would {mode} [{why}]", mode=mode)
        svc = self._service()
        if self.permanent:
            svc.files().delete(fileId=item_id).execute()
        else:
            svc.files().update(fileId=item_id, body={"trashed": True}).execute()
        return OpResult(ok=True, action="delete", target=item_id, committed=True,
                        detail=f"{mode} done", data={"mode": mode})
