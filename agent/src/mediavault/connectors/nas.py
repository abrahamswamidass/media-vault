"""
NAS connector — fully functional against a mounted filesystem path.

A mounted SMB/NFS share behaves like an ordinary directory, so this same code
runs whether you point it at a Windows drive letter, a Linux mount, or a local
test folder. Per our architecture, NAS is the source of truth, so 'delete' here
is a SOFT delete: it MOVES the file into a trash folder rather than erasing it.
"""
from __future__ import annotations

import hashlib
import os
import shutil
import mimetypes
from pathlib import Path
from typing import Iterable

from ..ports import Connector, FileRecord, OpResult, NotSupported


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
