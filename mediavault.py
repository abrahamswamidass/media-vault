#!/usr/bin/env python3
"""
Media Vault - SINGLE-FILE test harness (no package layout needed).
Run it flat:  python3 mediavault.py nas list --root /path
Target path works in ANY position. Same safety model as the package.
"""
from __future__ import annotations

# ===== base =====
"""
Core contracts shared by every connector.

The whole harness is built around ONE idea: every source (NAS, Drive, an
exported archive, the Amazon staging folder) exposes the same small set of
verbs. That way the CLI can test any operation against any connector with
identical syntax, and the real agent later reuses these exact classes.
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass, field, asdict
from typing import Iterable, Optional
import time


# --------------------------------------------------------------------------- #
# Data shapes
# --------------------------------------------------------------------------- #
@dataclass
class FileRecord:
    """A single item as seen by a connector. Not every field is always known."""
    id: str                       # connector-native id (NAS: path, Drive: fileId)
    name: str
    source: str                   # "nas" | "drive" | "archive" | "amazon"
    size: Optional[int] = None    # bytes
    mtime: Optional[float] = None # epoch seconds
    mime: Optional[str] = None
    is_dir: bool = False
    quick_hash: Optional[str] = None   # cheap fingerprint (size + head/tail)
    extra: dict = field(default_factory=dict)  # raw metadata blob (exif, etc.)

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class OpResult:
    """Uniform result envelope so the CLI can print success/failure the same way."""
    ok: bool
    action: str                   # "delete" | "upload" | ...
    target: str
    committed: bool               # False => dry-run, nothing actually changed
    detail: str = ""
    data: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return asdict(self)


class NotSupported(Exception):
    """Raised when a connector doesn't implement an operation (e.g. delete on a read-only archive)."""


# --------------------------------------------------------------------------- #
# The interface every connector implements
# --------------------------------------------------------------------------- #
class Connector(ABC):
    name: str = "base"
    can_delete: bool = False
    can_upload: bool = False

    # --- read side -------------------------------------------------------- #
    @abstractmethod
    def list(self, prefix: str = "", limit: int = 100) -> Iterable[FileRecord]:
        """List items under a path/prefix. Should be cheap and streamable."""

    @abstractmethod
    def stat(self, item_id: str) -> FileRecord:
        """Full metadata for a single item."""

    def read(self, item_id: str, nbytes: int = 0) -> bytes:
        """Return file bytes. nbytes=0 => whole file; >0 => just the first N (safe peek)."""
        raise NotSupported(f"{self.name}: read not implemented")

    # --- write side (guarded) -------------------------------------------- #
    def delete(self, item_id: str, commit: bool = False) -> OpResult:
        raise NotSupported(f"{self.name}: delete not supported")

    def upload(self, local_path: str, dest: str = "", commit: bool = False) -> OpResult:
        raise NotSupported(f"{self.name}: upload not supported")

    # --- helpers ---------------------------------------------------------- #
    def dryrun_result(self, action: str, target: str, detail: str = "", **data) -> OpResult:
        return OpResult(ok=True, action=action, target=target, committed=False,
                        detail=f"DRY-RUN (no change): {detail}", data=data)
# ===== nas =====
"""
NAS connector — fully functional against a mounted filesystem path.

A mounted SMB/NFS share behaves like an ordinary directory, so this same code
runs whether you point it at a Windows drive letter, a Linux mount, or a local
test folder. Per our architecture, NAS is the source of truth, so 'delete' here
is a SOFT delete: it MOVES the file into a trash folder rather than erasing it.
"""

import hashlib
import os
import shutil
import mimetypes
from pathlib import Path
from typing import Iterable



def quick_fingerprint(path: Path, chunk: int = 65536) -> str:
    """Cheap dedup fingerprint: size + hash of first & last chunk. Avoids full reads over the mount."""
    size = path.stat().st_size
    h = hashlib.sha1()
    h.update(str(size).encode())
    with path.open("rb") as f:
        h.update(f.read(chunk))
        if size > chunk:
            f.seek(max(0, size - chunk))
            h.update(f.read(chunk))
    return f"{size}:{h.hexdigest()}"


class NASConnector(Connector):
    name = "nas"
    can_delete = True
    can_upload = True   # used for the Amazon staging-folder pattern too

    def __init__(self, root: str, trash: str | None = None):
        self.root = Path(root).expanduser().resolve()
        # Default trash lives *inside* the root so it stays on the same volume (instant moves).
        self.trash = Path(trash).expanduser().resolve() if trash else self.root / "_trash"
        if not self.root.exists():
            raise FileNotFoundError(f"NAS root does not exist: {self.root}")

    # --- path safety ------------------------------------------------------ #
    def _resolve(self, item_id: str) -> Path:
        """Resolve an id to an absolute path and REFUSE to escape the root (no ../ tricks)."""
        p = (self.root / item_id).resolve() if not os.path.isabs(item_id) else Path(item_id).resolve()
        if self.root not in p.parents and p != self.root:
            raise ValueError(f"Refusing to operate outside root: {p}")
        return p

    # --- read side -------------------------------------------------------- #
    def list(self, prefix: str = "", limit: int = 100) -> Iterable[FileRecord]:
        base = self._resolve(prefix) if prefix else self.root
        count = 0
        for entry in sorted(base.iterdir(), key=lambda e: (not e.is_dir(), e.name.lower())):
            if entry.resolve() == self.trash:
                continue  # never list the trash folder itself
            st = entry.stat()
            yield FileRecord(
                id=str(entry.relative_to(self.root)),
                name=entry.name,
                source=self.name,
                size=st.st_size,
                mtime=st.st_mtime,
                mime=mimetypes.guess_type(entry.name)[0],
                is_dir=entry.is_dir(),
            )
            count += 1
            if count >= limit:
                break

    def stat(self, item_id: str) -> FileRecord:
        p = self._resolve(item_id)
        if not p.exists():
            raise FileNotFoundError(p)
        st = p.stat()
        return FileRecord(
            id=str(p.relative_to(self.root)),
            name=p.name,
            source=self.name,
            size=st.st_size,
            mtime=st.st_mtime,
            mime=mimetypes.guess_type(p.name)[0],
            is_dir=p.is_dir(),
            quick_hash=None if p.is_dir() else quick_fingerprint(p),
        )

    def read(self, item_id: str, nbytes: int = 0) -> bytes:
        p = self._resolve(item_id)
        with p.open("rb") as f:
            return f.read() if nbytes <= 0 else f.read(nbytes)

    # --- write side ------------------------------------------------------- #
    def delete(self, item_id: str, commit: bool = False) -> OpResult:
        """SOFT delete: move into trash, preserving relative structure. Reversible."""
        p = self._resolve(item_id)
        if not p.exists():
            raise FileNotFoundError(p)
        rel = p.relative_to(self.root)
        dest = self.trash / rel
        if not commit:
            return self.dryrun_result("delete", str(rel),
                                      detail=f"would move -> {dest}", dest=str(dest))
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(p), str(dest))
        return OpResult(ok=True, action="delete", target=str(rel), committed=True,
                        detail=f"moved to trash: {dest}", data={"dest": str(dest)})

    def upload(self, local_path: str, dest: str = "", commit: bool = False) -> OpResult:
        """Copy a file INTO the NAS (e.g. staging a cherry-pick for Amazon)."""
        src = Path(local_path).expanduser().resolve()
        if not src.exists():
            raise FileNotFoundError(src)
        target = self._resolve(dest) if dest else self.root / src.name
        if not commit:
            return self.dryrun_result("upload", str(src),
                                      detail=f"would copy -> {target}", dest=str(target))
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(str(src), str(target))
        return OpResult(ok=True, action="upload", target=str(target), committed=True,
                        detail=f"copied {src.name} -> {target}", data={"dest": str(target)})
# ===== drive =====
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

import os
from typing import Iterable


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
# ===== archive =====
"""
Archive connector — read-only indexer for exported dumps.

Google Photos (Takeout) and Amazon ("Request My Data" / bulk web download) are
locked-down clouds: you can't index them live, so you export a folder and point
this connector at it. It reuses the NAS reader under the hood and additionally
picks up Takeout-style JSON metadata sidecars when present.

By design there is NO delete/upload here — an export is a read-only snapshot.
"""

import json
from pathlib import Path
from typing import Iterable



class ArchiveConnector(Connector):
    name = "archive"
    can_delete = False
    can_upload = False

    def __init__(self, root: str):
        # Lean on the NAS reader; an export is just a folder tree.
        self._fs = NASConnector(root, trash=str(Path(root) / "__never_used_trash"))

    def list(self, prefix: str = "", limit: int = 100) -> Iterable[FileRecord]:
        for rec in self._fs.list(prefix, limit):
            rec.source = self.name
            yield rec

    def stat(self, item_id: str) -> FileRecord:
        rec = self._fs.stat(item_id)
        rec.source = self.name
        # Takeout drops a "<file>.json" (or "<file>.supplemental-metadata.json") sidecar.
        for cand in (item_id + ".json",
                     item_id + ".supplemental-metadata.json"):
            side = self._fs.root / cand
            if side.exists():
                try:
                    rec.extra["takeout"] = json.loads(side.read_text(encoding="utf-8"))
                except Exception as e:
                    rec.extra["takeout_error"] = str(e)
                break
        return rec

    def read(self, item_id: str, nbytes: int = 0) -> bytes:
        return self._fs.read(item_id, nbytes)
# ===== amazon =====
"""
Amazon connector — write-only, "drop into a watched folder" pattern.

We deliberately DON'T talk to Amazon's (unofficial) API. Instead the agent copies
cherry-picked files into a staging folder that Amazon's official desktop app
watches and auto-uploads. So 'upload' here just means "stage the file"; Amazon's
own app owns the actual upload. Fire-and-forget, zero fragile code.
"""

from datetime import datetime
from pathlib import Path



class AmazonConnector(Connector):
    name = "amazon"
    can_delete = False
    can_upload = True

    def __init__(self, staging_root: str, album_by_month: bool = True):
        self.album_by_month = album_by_month
        self._fs = NASConnector(staging_root, trash=str(Path(staging_root) / "__never_used_trash"))

    def list(self, prefix: str = "", limit: int = 100):
        # Lets you inspect what's currently staged/waiting for the app to pick up.
        for rec in self._fs.list(prefix, limit):
            rec.source = self.name
            yield rec

    def stat(self, item_id: str) -> FileRecord:
        rec = self._fs.stat(item_id)
        rec.source = self.name
        return rec

    def upload(self, local_path: str, dest: str = "", commit: bool = False) -> OpResult:
        """Stage a file into a dated album subfolder the Amazon app will auto-upload."""
        if not dest:
            album = datetime.now().strftime("%Y-%m") if self.album_by_month else ""
            dest = str(Path(album) / Path(local_path).name) if album else Path(local_path).name
        res = self._fs.upload(local_path, dest, commit=commit)
        res.action = "stage-for-amazon"
        if commit:
            res.detail = f"staged for Amazon auto-upload -> {res.data.get('dest')}"
        else:
            res.detail = res.detail.replace("upload", "stage-for-amazon")
        return res
# ===== registry =====
"""Connector registry — maps a name to its class and how to build it from env/args."""

import os



def build_connector(name: str, args):
    """Factory used by the CLI. Pulls paths from --root/flags or environment."""
    if name == "nas":
        root = args.root or os.getenv("NAS_ROOT")
        if not root:
            raise SystemExit("nas needs --root <path> (or set NAS_ROOT)")
        return NASConnector(root, trash=args.trash or os.getenv("NAS_TRASH"))

    if name == "drive":
        return DriveConnector(
            credentials_path=args.root or os.getenv("DRIVE_CREDENTIALS"),
            permanent=args.permanent,
        )

    if name == "archive":
        root = args.root or os.getenv("ARCHIVE_ROOT")
        if not root:
            raise SystemExit("archive needs --root <exported-folder>")
        return ArchiveConnector(root)

    if name == "amazon":
        root = args.root or os.getenv("AMAZON_STAGING")
        if not root:
            raise SystemExit("amazon needs --root <staging-folder> (the Amazon-watched folder)")
        return AmazonConnector(root)

    raise SystemExit(f"unknown connector: {name}")


CONNECTORS = ["nas", "drive", "archive", "amazon"]
# ===== cli =====
"""
Test harness CLI — poke one connector operation at a time.

Usage:
    python -m harness.cli <connector> <command> [args] [flags]

Connectors : nas | drive | archive | amazon
Commands   : list | stat | read | delete | upload | caps

SAFETY: destructive/mutating commands (delete, upload) are DRY-RUN by default.
Add --commit to actually perform them. NAS delete is a soft move-to-trash.

Examples:
    python -m harness.cli nas list --root /mnt/nas --prefix Photos
    python -m harness.cli nas stat  --root /mnt/nas "Photos/img_001.jpg"
    python -m harness.cli nas read  --root /mnt/nas "Photos/img_001.jpg" --peek 32
    python -m harness.cli nas delete --root /mnt/nas "Photos/junk.jpg"            # dry-run
    python -m harness.cli nas delete --root /mnt/nas "Photos/junk.jpg" --commit   # real (to trash)
    python -m harness.cli amazon upload --root /mnt/nas/_AmazonUpload ./pic.jpg --commit
    python -m harness.cli drive delete --root creds.json "<fileId>" --permanent   # safe-mode dry-run
"""

import argparse
import json
import sys



def _emit(obj, as_json: bool):
    if as_json:
        if isinstance(obj, (OpResult, FileRecord)):
            obj = obj.to_dict()
        print(json.dumps(obj, indent=2, default=str))
    else:
        print(obj)


def main(argv=None):
    p = argparse.ArgumentParser(prog="harness", description="Connector operation test harness")
    p.add_argument("connector", choices=CONNECTORS)
    p.add_argument("command", choices=["list", "stat", "read", "delete", "upload", "caps"])
    p.add_argument("target", nargs="?", default="", help="item id / path / local file for upload")

    p.add_argument("--root", help="connector root (NAS/archive/amazon path, or Drive creds.json)")
    p.add_argument("--trash", help="NAS trash folder (default <root>/_trash)")
    p.add_argument("--prefix", default="", help="list: subpath/prefix to list under")
    p.add_argument("--limit", type=int, default=100, help="list: max items")
    p.add_argument("--peek", type=int, default=64, help="read: bytes to show (0 = whole file)")
    p.add_argument("--dest", default="", help="upload: destination sub-path")
    p.add_argument("--commit", action="store_true", help="ACTUALLY perform a mutating op (default: dry-run)")
    p.add_argument("--permanent", action="store_true", help="drive delete: permanent vs trash")
    p.add_argument("--json", action="store_true", help="machine-readable output")

    # Robust parsing: allow the target to appear in ANY position (before or after
    # flags) and across all Python versions. parse_known_args won't choke on a
    # positional that follows options; we then fold any leftover into `target`.
    args, extra = p.parse_known_args(argv)
    leftover = [x for x in extra if not x.startswith("-")]
    unknown_flags = [x for x in extra if x.startswith("-")]
    if unknown_flags:
        p.error(f"unrecognized flag(s): {' '.join(unknown_flags)}")
    if leftover:
        if args.target:
            p.error(f"too many positional arguments: {args.target!r} and {leftover!r}")
        args.target = leftover[0]
        if len(leftover) > 1:
            p.error(f"too many positional arguments: {leftover[1:]}")

    conn = build_connector(args.connector, args)

    try:
        if args.command == "caps":
            _emit({"connector": conn.name, "can_delete": conn.can_delete,
                   "can_upload": conn.can_upload}, args.json)

        elif args.command == "list":
            rows = list(conn.list(args.prefix, args.limit))
            if args.json:
                _emit([r.to_dict() for r in rows], True)
            else:
                for r in rows:
                    tag = "DIR " if r.is_dir else "FILE"
                    size = "" if r.size is None else f"{r.size:>12,d}"
                    print(f"{tag} {size}  {r.id}")
                print(f"\n{len(rows)} item(s).")

        elif args.command == "stat":
            _require_target(args)
            _emit(conn.stat(args.target), args.json or True)  # stat is always structured

        elif args.command == "read":
            _require_target(args)
            data = conn.read(args.target, args.peek)
            preview = data[: args.peek] if args.peek else data
            print(f"read {len(data)} byte(s). First {len(preview)}:")
            print(preview.hex(" ") if not args.json else json.dumps({"hex": preview.hex()}))

        elif args.command == "delete":
            _require_target(args)
            res = conn.delete(args.target, commit=args.commit)
            _banner(res)
            _emit(res, args.json)

        elif args.command == "upload":
            _require_target(args, what="local file path")
            res = conn.upload(args.target, dest=args.dest, commit=args.commit)
            _banner(res)
            _emit(res, args.json)

    except NotSupported as e:
        print(f"[not-supported] {e}", file=sys.stderr)
        sys.exit(2)
    except (FileNotFoundError, ValueError) as e:
        print(f"[error] {e}", file=sys.stderr)
        sys.exit(1)


def _require_target(args, what="target id/path"):
    if not args.target:
        raise SystemExit(f"'{args.command}' needs a {what}")


def _banner(res: OpResult):
    if not res.committed:
        print("‑‑ DRY-RUN — nothing changed. Re-run with --commit to apply. ‑‑")
    else:
        print("** COMMITTED **")


if __name__ == "__main__":
    main()