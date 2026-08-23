"""
NAS connector — SMB-direct variant.

Docker Desktop on Windows can't reliably bind-mount a *mapped network drive*
into its Linux VM: the VM sees the drive letter but the network share's
contents don't pass through, so the mount looks empty. Local disks and UNC
paths shared via Settings -> Resources -> File Sharing don't have this
problem, but plenty of home NAS setups only have a drive-letter mapping.

This connector sidesteps the OS mount entirely and speaks SMB2/3 straight
over the network using `smbprotocol`, gated behind NAS_MODE=smb so nothing
outside this file (and nothing in the default mount-based path) pulls the
dependency in. Same verbs, same soft-delete-to-trash semantics as the
mount-based NASConnector in nas.py — this is a drop-in alternative, not a
parallel design.
"""
from __future__ import annotations

import hashlib
import posixpath
from typing import Iterable

from ..ports import Connector, FileRecord, OpResult, NotSupported


def _quick_fingerprint_smb(smbclient, unc_path: str, size: int, chunk: int = 65536) -> str:
    """Same cheap fingerprint as the mount-based connector: size + first/last chunk hash."""
    h = hashlib.sha1()
    h.update(str(size).encode())
    with smbclient.open_file(unc_path, mode="rb") as f:
        h.update(f.read(chunk))
        if size > chunk:
            f.seek(max(0, size - chunk))
            h.update(f.read(chunk))
    return f"{size}:{h.hexdigest()}"


class SMBNASConnector(Connector):
    name = "nas"
    can_delete = True
    can_upload = True

    def __init__(self, host: str, share: str, root: str = "", trash: str | None = None,
                 username: str | None = None, password: str | None = None):
        import smbclient  # optional extra — only imported when NAS_MODE=smb

        self._smb = smbclient
        self.host = host
        self.share = share
        self._root_rel = root.strip("/\\").replace("\\", "/")
        self._trash_rel = (trash or posixpath.join(self._root_rel, "_trash")).strip("/\\").replace("\\", "/")
        self.root = self._to_unc(self._root_rel)
        self.trash = self._to_unc(self._trash_rel)

        if username:
            smbclient.register_session(host, username=username, password=password)

        try:
            if not smbclient.path.isdir(self.root):
                raise FileNotFoundError(self.root)
        except Exception as exc:
            raise FileNotFoundError(
                f"NAS SMB root does not exist or is unreachable: {self.root} ({exc})"
            ) from exc

    # --- path helpers ------------------------------------------------------ #
    def _to_unc(self, rel: str) -> str:
        rel = rel.strip("/").replace("/", "\\")
        base = f"\\\\{self.host}\\{self.share}"
        return f"{base}\\{rel}" if rel else base

    def _resolve(self, item_id: str) -> tuple[str, str]:
        """Resolve an id to (unc_path, rel_to_root). REFUSES to escape the root."""
        item_id = (item_id or "").strip("/\\").replace("\\", "/")
        combined = posixpath.normpath(posixpath.join(self._root_rel, item_id)) if item_id else self._root_rel
        combined = "" if combined == "." else combined
        if combined != self._root_rel and not combined.startswith(self._root_rel + "/"):
            raise ValueError(f"Refusing to operate outside root: {combined}")
        rel_to_root = combined[len(self._root_rel):].lstrip("/") if self._root_rel else combined
        return self._to_unc(combined), rel_to_root

    # --- read side ----------------------------------------------------------#
    def list(self, prefix: str = "", limit: int = 100) -> Iterable[FileRecord]:
        base_unc, base_rel = self._resolve(prefix) if prefix else (self.root, "")
        count = 0
        names = sorted(self._smb.listdir(base_unc), key=str.lower)
        for name in names:
            entry_unc = f"{base_unc}\\{name}"
            rel = f"{base_rel}/{name}" if base_rel else name
            if rel == self._trash_rel:
                continue  # never list the trash folder itself
            is_dir = self._smb.path.isdir(entry_unc)
            size = None if is_dir else self._smb.path.getsize(entry_unc)
            mtime = self._smb.path.getmtime(entry_unc)
            yield FileRecord(
                id=rel, name=name, source=self.name, size=size, mtime=mtime,
                mime=None, is_dir=is_dir,
            )
            count += 1
            if count >= limit:
                break

    def stat(self, item_id: str) -> FileRecord:
        unc, rel = self._resolve(item_id)
        if not self._smb.path.exists(unc):
            raise FileNotFoundError(unc)
        is_dir = self._smb.path.isdir(unc)
        size = None if is_dir else self._smb.path.getsize(unc)
        mtime = self._smb.path.getmtime(unc)
        return FileRecord(
            id=rel, name=posixpath.basename(rel) or rel, source=self.name,
            size=size, mtime=mtime, mime=None, is_dir=is_dir,
            quick_hash=None if is_dir else _quick_fingerprint_smb(self._smb, unc, size),
        )

    def read(self, item_id: str, nbytes: int = 0) -> bytes:
        unc, _ = self._resolve(item_id)
        with self._smb.open_file(unc, mode="rb") as f:
            return f.read() if nbytes <= 0 else f.read(nbytes)

    # --- write side ----------------------------------------------------------#
    def delete(self, item_id: str, commit: bool = False) -> OpResult:
        """SOFT delete: move into trash over SMB, preserving relative structure."""
        unc, rel = self._resolve(item_id)
        if not self._smb.path.exists(unc):
            raise FileNotFoundError(unc)
        dest_rel = f"{self._trash_rel}/{rel}"
        dest_unc = self._to_unc(dest_rel)
        if not commit:
            return self.dryrun_result("delete", rel, detail=f"would move -> {dest_unc}", dest=dest_unc)
        self._smb.makedirs(posixpath.dirname(dest_unc.replace("\\", "/")).replace("/", "\\"), exist_ok=True)
        self._smb.rename(unc, dest_unc)
        return OpResult(ok=True, action="delete", target=rel, committed=True,
                        detail=f"moved to trash: {dest_unc}", data={"dest": dest_unc})

    def upload(self, local_path: str, dest: str = "", commit: bool = False) -> OpResult:
        """Copy a LOCAL file onto the NAS over SMB (e.g. staging a cherry-pick)."""
        from pathlib import Path

        src = Path(local_path).expanduser().resolve()
        if not src.exists():
            raise FileNotFoundError(src)
        target_unc, target_rel = self._resolve(dest) if dest else self._resolve(src.name)
        if not commit:
            return self.dryrun_result("upload", str(src), detail=f"would copy -> {target_unc}", dest=target_unc)
        self._smb.makedirs(posixpath.dirname(target_unc.replace("\\", "/")).replace("/", "\\"), exist_ok=True)
        self._smb.shutil.copyfile(str(src), target_unc)
        return OpResult(ok=True, action="upload", target=target_unc, committed=True,
                        detail=f"copied {src.name} -> {target_unc}", data={"dest": target_unc})
