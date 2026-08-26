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
import time
from datetime import timezone
from typing import Iterable

from ..ports import Connector, FileRecord, OpResult, NotSupported

#: A dropped SMB session (idle timeout, network hiccup) used to crash the
#: whole scan instead of costing a brief pause — see GitHub #11. Retried a
#: bounded number of times, reconnecting in between, before giving up.
_RETRY_ATTEMPTS = 3
_RETRY_DELAY_SECONDS = 3


def _is_retryable_smb_error(exc: BaseException) -> bool:
    import smbprotocol.exceptions as smb_exc  # lazy — only needed on this path

    retryable = [
        smb_exc.SMBConnectionClosed,
        smb_exc.SMBAuthenticationError,
        ConnectionResetError,
        ConnectionAbortedError,
        BrokenPipeError,
        TimeoutError,
    ]
    try:
        # A dropped session sometimes surfaces as smbclient's own internal
        # reconnect attempt losing track of credentials (observed:
        # BadMechanismError, "no username or password was specified") rather
        # than a clean SMBAuthenticationError — spnego is a smbprotocol
        # dependency, so it's available whenever smbclient is.
        import spnego.exceptions as spnego_exc
        retryable.append(spnego_exc.SpnegoError)
    except ImportError:
        pass
    return isinstance(exc, tuple(retryable))


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
                 username: str | None = None, password: str | None = None,
                 exclude: list[str] | None = None):
        import smbclient  # optional extra — only imported when NAS_MODE=smb

        self._smb = smbclient
        self.host = host
        self.share = share
        self._root_rel = self._strip_share_prefix(root)
        # Trash defaults to nested under root, but an explicit NAS_SMB_TRASH is
        # a path relative to the SHARE, not the root — so it can sit at a fixed
        # spot (e.g. "_trash" at the share's top level) no matter what NAS_SMB_ROOT
        # points at. Full UNC strings (\\host\share\...) are accepted too; the
        # host/share prefix is stripped since this connector is already scoped
        # to one host+share.
        self._trash_rel = self._strip_share_prefix(trash) if trash else posixpath.join(self._root_rel, "_trash")
        self.root = self._to_unc(self._root_rel)
        self.trash = self._to_unc(self._trash_rel)
        # Operational folders (trash, Amazon staging) never count as library
        # content — matters once NAS_SMB_ROOT is the whole share and they show
        # up as ordinary subfolders. A path outside the scanned root just never
        # matches, so it's harmless to always include it.
        self._excluded_rel = {self._trash_rel} | {self._strip_share_prefix(p) for p in (exclude or [])}
        # Kept for _reconnect() — a dropped session needs to re-authenticate
        # the same way, not just retry the failed call.
        self._username = username
        self._password = password

        if username:
            smbclient.register_session(host, username=username, password=password)

        try:
            if not smbclient.path.isdir(self.root):
                raise FileNotFoundError(self.root)
        except Exception as exc:
            raise FileNotFoundError(
                f"NAS SMB root does not exist or is unreachable: {self.root} ({exc})"
            ) from exc

    # --- reconnect on a dropped session (#11) ------------------------------ #
    def _reconnect(self) -> None:
        try:
            self._smb.reset_connection_cache()
        except Exception:
            pass  # best-effort — the register_session below is what matters
        if self._username:
            self._smb.register_session(self.host, username=self._username,
                                       password=self._password)

    def _retry(self, fn, *args, **kwargs):
        """Run fn(*args, **kwargs), reconnecting and retrying if the SMB
        session dropped mid-call. Idle timeouts and brief network hiccups are
        expected over a multi-hour scan — this is the difference between that
        costing a few seconds and crashing the whole run (#11)."""
        last_exc: BaseException = None
        for attempt in range(_RETRY_ATTEMPTS):
            try:
                return fn(*args, **kwargs)
            except Exception as e:
                if not _is_retryable_smb_error(e) or attempt == _RETRY_ATTEMPTS - 1:
                    raise
                last_exc = e
                time.sleep(_RETRY_DELAY_SECONDS)
                self._reconnect()
        raise last_exc  # pragma: no cover - loop always returns or raises above

    # --- path helpers ------------------------------------------------------ #
    def _strip_share_prefix(self, value: str) -> str:
        """Accept either a bare share-relative path or a full \\\\host\\share\\... UNC string."""
        v = value.strip()
        for prefix in (f"\\\\{self.host}\\{self.share}", f"//{self.host}/{self.share}"):
            if v.lower().startswith(prefix.lower()):
                v = v[len(prefix):]
                break
        return v.strip("/\\").replace("\\", "/")

    def _to_unc(self, rel: str) -> str:
        rel = rel.strip("/").replace("/", "\\")
        base = f"\\\\{self.host}\\{self.share}"
        return f"{base}\\{rel}" if rel else base

    def _resolve(self, item_id: str) -> tuple[str, str]:
        """Resolve an id to (unc_path, rel_to_root). REFUSES to escape the root."""
        item_id = (item_id or "").strip("/\\").replace("\\", "/")
        combined = posixpath.normpath(posixpath.join(self._root_rel, item_id)) if item_id else self._root_rel
        combined = "" if combined == "." else combined
        # An empty root_rel means the root IS the share, so every non-escaping
        # path is "under" it — the old `combined.startswith(root_rel + "/")`
        # check broke this case, since root_rel + "/" is just "/" and no
        # relative path starts with a leading slash. Still explicitly block a
        # ".." escape attempt, since normpath can't collapse one against an
        # empty root the way it does against a real subfolder root.
        escaping = combined == ".." or combined.startswith("../")
        under_root = (
            not escaping
            and (not self._root_rel or combined == self._root_rel
                 or combined.startswith(self._root_rel + "/"))
        )
        if not under_root:
            raise ValueError(f"Refusing to operate outside root: {combined}")
        rel_to_root = combined[len(self._root_rel):].lstrip("/") if self._root_rel else combined
        return self._to_unc(combined), rel_to_root

    # --- read side ----------------------------------------------------------#
    def list(self, prefix: str = "", limit: int = 100) -> Iterable[FileRecord]:
        base_unc, base_rel = self._resolve(prefix) if prefix else (self.root, "")
        count = 0
        entries = self._retry(self._scandir_once, base_unc)
        for entry in entries:
            name = entry.name
            entry_unc = f"{base_unc}\\{name}"
            # Excluded paths are relative to the SHARE, not the scanned root (so
            # a fixed trash/Amazon spot stays excluded no matter what NAS_SMB_ROOT
            # points at) — compare on that basis, not on the root-relative `rel`.
            if self._strip_share_prefix(entry_unc) in self._excluded_rel:
                continue
            rel = f"{base_rel}/{name}" if base_rel else name
            is_dir, size, mtime = self._entry_meta_from_scandir(entry)
            yield FileRecord(
                id=rel, name=name, source=self.name, size=size, mtime=mtime,
                mime=None, is_dir=is_dir,
            )
            count += 1
            if count >= limit:
                break

    def _scandir_once(self, base_unc: str) -> list:
        # scandir()'s single FIND response already carries file-type and
        # basic attributes for every entry — sorting the materialized list
        # (rather than the raw iterator) lets the `with` block close the SMB
        # directory handle before we start using the results.
        with self._smb.scandir(base_unc) as it:
            return sorted(it, key=lambda e: e.name.lower())

    def _entry_meta_from_scandir(self, entry) -> tuple[bool, int | None, float]:
        """is_dir/size/mtime straight from the scandir() FIND response — no
        extra round trips. The previous version called isdir()/getsize()/
        getmtime() separately per entry (up to 3 extra SMB calls each,
        discarded immediately for any entry that turned out to be a file
        during a directory-only walk); over a tree with thousands of files
        that's what made the resume "skip phase" take 30+ minutes (#11)."""
        is_dir = entry.is_dir()
        info = entry.smb_info
        size = None if is_dir else info.end_of_file
        # last_write_time is a naive datetime that represents a UTC instant
        # (FILETIME's own definition) — attach the tzinfo explicitly before
        # calling timestamp(), which otherwise assumes local time and would
        # silently produce a wrong epoch value shifted by the local UTC offset.
        mtime = info.last_write_time.replace(tzinfo=timezone.utc).timestamp()
        return is_dir, size, mtime

    def stat(self, item_id: str) -> FileRecord:
        unc, rel = self._resolve(item_id)
        return self._retry(self._stat_once, unc, rel)

    def _stat_once(self, unc: str, rel: str) -> FileRecord:
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
        return self._retry(self._read_once, unc, nbytes)

    def _read_once(self, unc: str, nbytes: int) -> bytes:
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
