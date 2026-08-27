"""
SMBNASConnector path-safety tests.

`smbclient` needs a real SMB server, so these inject a minimal fake module
into sys.modules rather than mocking the network — only the pure path logic
in `_resolve()` is under test here, which is where the actual bug lived.
"""
from __future__ import annotations

import sys
import types
from datetime import datetime, timezone

import pytest

smbprotocol = pytest.importorskip("smbprotocol", reason="reconnect tests need smbprotocol's exception types")
import smbprotocol.exceptions as smb_exc  # noqa: E402

from mediavault.connectors import nas_smb  # noqa: E402
from mediavault.connectors.nas_smb import SMBNASConnector  # noqa: E402


@pytest.fixture
def fake_smbclient(monkeypatch):
    fake = types.SimpleNamespace(
        register_session=lambda *a, **k: None,
        path=types.SimpleNamespace(isdir=lambda p: True),
    )
    monkeypatch.setitem(sys.modules, "smbclient", fake)
    return fake


def _connector(fake_smbclient, root: str, trash: str | None = None) -> SMBNASConnector:
    return SMBNASConnector("nashost", "homes", root=root, trash=trash)


# --------------------------------------------------------------------------- #
# The bug: an empty root (scanning the whole share) broke every child lookup
# --------------------------------------------------------------------------- #
def test_empty_root_resolves_top_level_children(fake_smbclient):
    """Regression: NAS_SMB_ROOT="" used to raise 'Refusing to operate outside
    root' for every top-level folder (admin, winfredbe, ...)."""
    conn = _connector(fake_smbclient, root="")

    unc, rel = conn._resolve("winfredbe")
    assert rel == "winfredbe"
    assert unc == "\\\\nashost\\homes\\winfredbe"


def test_empty_root_resolves_nested_children(fake_smbclient):
    conn = _connector(fake_smbclient, root="")

    unc, rel = conn._resolve("winfredbe/nov2025-cafc/IMG_8831.CR2")
    assert rel == "winfredbe/nov2025-cafc/IMG_8831.CR2"


def test_empty_root_still_blocks_escape_attempts(fake_smbclient):
    conn = _connector(fake_smbclient, root="")

    with pytest.raises(ValueError, match="Refusing to operate outside root"):
        conn._resolve("../outside")


# --------------------------------------------------------------------------- #
# Non-empty root: the existing, already-working behaviour
# --------------------------------------------------------------------------- #
def test_subfolder_root_resolves_children(fake_smbclient):
    conn = _connector(fake_smbclient, root="winfredbe/nov2025-cafc")

    unc, rel = conn._resolve("IMG_8831.CR2")
    assert rel == "IMG_8831.CR2"
    assert unc == "\\\\nashost\\homes\\winfredbe\\nov2025-cafc\\IMG_8831.CR2"


def test_subfolder_root_blocks_escape_attempts(fake_smbclient):
    conn = _connector(fake_smbclient, root="winfredbe/nov2025-cafc")

    with pytest.raises(ValueError, match="Refusing to operate outside root"):
        conn._resolve("../../etc/passwd")


def test_subfolder_root_blocks_sibling_folder(fake_smbclient):
    """A folder that merely shares a prefix (nov2025-cafc-old) must not pass."""
    conn = _connector(fake_smbclient, root="winfredbe/nov2025-cafc")

    with pytest.raises(ValueError, match="Refusing to operate outside root"):
        conn._resolve("../nov2025-cafc-old/img.jpg")


# --------------------------------------------------------------------------- #
# Reconnect-and-retry on a dropped session (#11)
# --------------------------------------------------------------------------- #
def test_retry_reconnects_and_succeeds_after_a_dropped_session(fake_smbclient, monkeypatch):
    conn = _connector(fake_smbclient, root="")
    monkeypatch.setattr(nas_smb, "_RETRY_DELAY_SECONDS", 0)
    reconnected = []
    monkeypatch.setattr(conn, "_reconnect", lambda: reconnected.append(True))

    calls = {"n": 0}

    def flaky():
        calls["n"] += 1
        if calls["n"] == 1:
            raise smb_exc.SMBConnectionClosed("SMB socket was closed")
        return "ok"

    assert conn._retry(flaky) == "ok"
    assert calls["n"] == 2
    assert reconnected == [True]


def test_retry_does_not_retry_non_retryable_errors(fake_smbclient, monkeypatch):
    conn = _connector(fake_smbclient, root="")
    monkeypatch.setattr(nas_smb, "_RETRY_DELAY_SECONDS", 0)
    reconnected = []
    monkeypatch.setattr(conn, "_reconnect", lambda: reconnected.append(True))

    def not_found():
        raise FileNotFoundError("no such file")

    with pytest.raises(FileNotFoundError):
        conn._retry(not_found)
    assert reconnected == []  # never even tried to reconnect for a real 404


def test_retry_gives_up_after_max_attempts(fake_smbclient, monkeypatch):
    conn = _connector(fake_smbclient, root="")
    monkeypatch.setattr(nas_smb, "_RETRY_DELAY_SECONDS", 0)
    monkeypatch.setattr(conn, "_reconnect", lambda: None)

    def always_drops():
        raise smb_exc.SMBConnectionClosed("still down")

    with pytest.raises(smb_exc.SMBConnectionClosed):
        conn._retry(always_drops)


# --------------------------------------------------------------------------- #
# scandir()-based metadata (no extra per-entry round trips) — #11
# --------------------------------------------------------------------------- #
def _fake_scandir_entry(name, is_dir, end_of_file=0, last_write_time=None):
    info = types.SimpleNamespace(end_of_file=end_of_file, last_write_time=last_write_time)
    return types.SimpleNamespace(name=name, is_dir=lambda: is_dir, smb_info=info)


def test_entry_meta_from_scandir_file(fake_smbclient):
    conn = _connector(fake_smbclient, root="")
    # A naive datetime representing a UTC instant, per FILETIME's own
    # definition (100ns intervals since 1601-01-01 UTC) — not local time.
    naive_utc = datetime(2026, 1, 15, 10, 30, 0)
    entry = _fake_scandir_entry("img.jpg", is_dir=False, end_of_file=12345,
                                last_write_time=naive_utc)

    is_dir, size, mtime = conn._entry_meta_from_scandir(entry)

    assert is_dir is False
    assert size == 12345
    assert mtime == naive_utc.replace(tzinfo=timezone.utc).timestamp()


def test_entry_meta_from_scandir_directory_has_no_size(fake_smbclient):
    conn = _connector(fake_smbclient, root="")
    entry = _fake_scandir_entry("Photos", is_dir=True, end_of_file=999,
                                last_write_time=datetime(2026, 1, 1))

    is_dir, size, _mtime = conn._entry_meta_from_scandir(entry)

    assert is_dir is True
    assert size is None


class _FakeScandirContextManager:
    def __init__(self, entries):
        self._entries = entries

    def __enter__(self):
        return iter(self._entries)

    def __exit__(self, *exc):
        return False


def test_list_gets_metadata_from_scandir_with_no_extra_calls(fake_smbclient):
    """Regression: list() used to call isdir()/getsize()/getmtime() separately
    per entry — up to 3 extra SMB round trips each, discarded immediately for
    any entry that turned out to be a file during a directory-only walk. Over
    a tree with thousands of files, that's what made the resume 'skip phase'
    take 30+ minutes instead of seconds (#11)."""
    conn = _connector(fake_smbclient, root="")
    dt = datetime(2026, 6, 1, 12, 0, 0)
    entries = [
        _fake_scandir_entry("Photos", is_dir=True, end_of_file=0, last_write_time=dt),
        _fake_scandir_entry("img.jpg", is_dir=False, end_of_file=500, last_write_time=dt),
    ]
    fake_smbclient.scandir = lambda path: _FakeScandirContextManager(entries)

    def boom(*a, **k):
        raise AssertionError("must not fall back to per-entry isdir/getsize/getmtime")

    fake_smbclient.path.isdir = boom
    fake_smbclient.path.getsize = boom
    fake_smbclient.path.getmtime = boom

    records = {r.name: r for r in conn.list()}

    assert records["Photos"].is_dir is True
    assert records["Photos"].size is None
    assert records["img.jpg"].is_dir is False
    assert records["img.jpg"].size == 500
    assert records["img.jpg"].mtime == dt.replace(tzinfo=timezone.utc).timestamp()


# --------------------------------------------------------------------------- #
# upload() — regression: smbclient.shutil.copyfile() raised AttributeError
# on the very first real use (staging a NAS item for Amazon over SMB).
# smbclient.shutil is a real submodule, but plain `import smbclient` (used
# everywhere in this file) never makes it accessible as smbclient.shutil —
# that needs its own `import smbclient.shutil` this codebase never did.
# --------------------------------------------------------------------------- #
class _FakeRemoteFile:
    def __init__(self, store, unc, mode):
        self._store = store
        self._unc = unc
        self._mode = mode
        self._buf = b""

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        if "w" in self._mode:
            self._store[self._unc] = self._buf

    def write(self, data: bytes) -> None:
        self._buf += data


@pytest.fixture
def fake_smbclient_writable(fake_smbclient):
    store: dict[str, bytes] = {}
    fake_smbclient.makedirs = lambda *a, **k: None
    fake_smbclient.open_file = lambda unc, mode: _FakeRemoteFile(store, unc, mode)
    return fake_smbclient, store


def test_upload_writes_bytes_without_touching_smbclient_shutil(fake_smbclient_writable, tmp_path):
    fake, store = fake_smbclient_writable
    conn = _connector(fake, root="")
    # No `.shutil` attribute on the fake at all — if upload() ever reaches
    # for it again, this raises AttributeError exactly like the real bug did.
    assert not hasattr(fake, "shutil")

    src = tmp_path / "img.jpg"
    src.write_bytes(b"fake jpeg bytes")

    result = conn.upload(str(src), dest="Photos/img.jpg", commit=True)

    assert result.committed
    assert store[conn._to_unc("Photos/img.jpg")] == b"fake jpeg bytes"


def test_upload_dry_run_writes_nothing(fake_smbclient_writable, tmp_path):
    fake, store = fake_smbclient_writable
    conn = _connector(fake, root="")
    src = tmp_path / "img.jpg"
    src.write_bytes(b"fake jpeg bytes")

    result = conn.upload(str(src), dest="Photos/img.jpg", commit=False)

    assert not result.committed
    assert store == {}
