"""
Google Drive connector — the ONE cloud you can fully automate (read + real delete).

This ships as a guarded stub: the operation shapes and safety gating are real and
testable today, but the live Drive API calls are behind an explicit credential
check so you never accidentally hit Google before you've wired OAuth.

To go live:
  1. pip install google-api-python-client google-auth-oauthlib
  2. Create an OAuth desktop client, download credentials.json
  3. Implement _service() to build the Drive v3 client
  4. Flip LIVE = True (or set DRIVE_LIVE=1)

Delete mode: files.delete = PERMANENT (reclaims paid space).  <-- what you want
             files.trash  = 30-day recycle (still counts against quota).
"""
from __future__ import annotations

import os
from typing import Iterable

from ..base import Connector, FileRecord, OpResult, NotSupported

LIVE = os.getenv("DRIVE_LIVE", "0") == "1"


class DriveConnector(Connector):
    name = "drive"
    can_delete = True
    can_upload = False   # we don't push files INTO Drive in this project

    def __init__(self, credentials_path: str | None = None, permanent: bool = False):
        self.credentials_path = credentials_path or os.getenv("DRIVE_CREDENTIALS")
        self.permanent = permanent          # False => trash, True => hard delete
        self._svc = None

    def _service(self):
        if not LIVE:
            raise NotSupported(
                "Drive is in SAFE mode (DRIVE_LIVE!=1). List/stat/delete run as dry-run only. "
                "Wire OAuth and set DRIVE_LIVE=1 to go live."
            )
        if self._svc is None:
            # --- real wiring goes here (kept out of the safe path) ---
            # from googleapiclient.discovery import build
            # creds = <load/refresh from self.credentials_path>
            # self._svc = build("drive", "v3", credentials=creds)
            raise NotSupported("Drive live client not yet implemented — add build() in _service().")
        return self._svc

    # --- read side -------------------------------------------------------- #
    def list(self, prefix: str = "", limit: int = 100) -> Iterable[FileRecord]:
        if not LIVE:
            # Return nothing but don't crash — lets you smoke-test the CLI plumbing.
            return iter(())
        svc = self._service()
        # q = f"'{prefix}' in parents" if prefix else None
        # resp = svc.files().list(pageSize=limit, fields="files(id,name,size,mimeType,modifiedTime)", q=q).execute()
        # for f in resp.get("files", []): yield FileRecord(...)
        raise NotSupported("Drive live list not yet implemented.")

    def stat(self, item_id: str) -> FileRecord:
        if not LIVE:
            return FileRecord(id=item_id, name="<safe-mode>", source=self.name,
                              extra={"note": "DRIVE_LIVE!=1"})
        raise NotSupported("Drive live stat not yet implemented.")

    # --- write side ------------------------------------------------------- #
    def delete(self, item_id: str, commit: bool = False) -> OpResult:
        mode = "permanent delete" if self.permanent else "move to Drive trash"
        if not commit or not LIVE:
            why = "dry-run" if not commit else "SAFE mode (DRIVE_LIVE!=1)"
            return self.dryrun_result("delete", item_id,
                                      detail=f"would {mode} [{why}]", mode=mode)
        svc = self._service()
        # if self.permanent: svc.files().delete(fileId=item_id).execute()
        # else:              svc.files().update(fileId=item_id, body={"trashed": True}).execute()
        raise NotSupported("Drive live delete not yet implemented.")
