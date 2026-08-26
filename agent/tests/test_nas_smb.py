"""
SMBNASConnector path-safety tests.

`smbclient` needs a real SMB server, so these inject a minimal fake module
into sys.modules rather than mocking the network — only the pure path logic
in `_resolve()` is under test here, which is where the actual bug lived.
"""
from __future__ import annotations

import sys
import types

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
