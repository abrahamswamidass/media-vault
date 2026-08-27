"""
DriveConnector tests — no real Google API client needed.

`_service()` is the one seam that talks to `googleapiclient`/`google-auth`
(both optional extras, gated behind DRIVE_LIVE=1). Every test here monkeypatches
`_service()` directly with a small fake that mimics the handful of Drive v3
call shapes actually used, so these tests run without those packages installed
— consistent with the project's stdlib-only-core rule.
"""
from __future__ import annotations

import pytest

from mediavault.connectors import drive as drive_mod
from mediavault.connectors.drive import DriveConnector
from mediavault.ports import NotSupported


class _FilesCall:
    """Mimics one googleapiclient HttpRequest: has .headers and .execute()."""
    def __init__(self, result):
        self._result = result
        self.headers = {}

    def execute(self):
        return self._result(self.headers) if callable(self._result) else self._result


class FakeFiles:
    def __init__(self, entries: dict[str, dict], content: dict[str, bytes]):
        self._entries = entries       # file_id -> Drive file dict
        self._content = content       # file_id -> full bytes
        self.calls = []                # recorded (method, kwargs) for assertions

    def list(self, **kwargs):
        self.calls.append(("list", kwargs))
        folder_id = kwargs["q"].split("'")[1]
        matches = [f for f in self._entries.values() if f.get("parents") == [folder_id]]
        page_size = kwargs.get("pageSize", 1000)
        token = int(kwargs.get("pageToken") or 0)
        page = matches[token:token + page_size]
        next_token = str(token + page_size) if token + page_size < len(matches) else None
        return _FilesCall({"files": page, **({"nextPageToken": next_token} if next_token else {})})

    def get(self, fileId, fields=None):
        self.calls.append(("get", {"fileId": fileId}))
        return _FilesCall(self._entries[fileId])

    def get_media(self, fileId):
        self.calls.append(("get_media", {"fileId": fileId}))
        data = self._content[fileId]

        def _resolve(headers):
            rng = headers.get("Range")
            if not rng:
                return data
            start, end = (int(x) for x in rng.removeprefix("bytes=").split("-"))
            return data[start:end + 1]
        return _FilesCall(_resolve)

    def update(self, fileId, body):
        self.calls.append(("update", {"fileId": fileId, "body": body}))
        return _FilesCall({})

    def delete(self, fileId):
        self.calls.append(("delete", {"fileId": fileId}))
        return _FilesCall({})


class FakeService:
    def __init__(self, entries, content):
        self._files = FakeFiles(entries, content)

    def files(self):
        return self._files


def _connector(monkeypatch, entries, content, **kwargs) -> tuple[DriveConnector, FakeService]:
    monkeypatch.setattr(drive_mod, "LIVE", True)
    conn = DriveConnector(**kwargs)
    svc = FakeService(entries, content)
    monkeypatch.setattr(conn, "_service", lambda: svc)
    return conn, svc


# --------------------------------------------------------------------------- #
# list()
# --------------------------------------------------------------------------- #
def test_list_returns_files_and_folders_under_a_folder_id(monkeypatch):
    entries = {
        "f1": {"id": "f1", "name": "a.jpg", "mimeType": "image/jpeg",
               "size": "100", "modifiedTime": "2024-01-01T00:00:00Z", "parents": ["root"]},
        "d1": {"id": "d1", "name": "sub", "mimeType": drive_mod._FOLDER_MIME,
               "modifiedTime": "2024-01-01T00:00:00Z", "parents": ["root"]},
        "f2": {"id": "f2", "name": "elsewhere.jpg", "mimeType": "image/jpeg",
               "size": "5", "modifiedTime": "2024-01-01T00:00:00Z", "parents": ["d1"]},
    }
    conn, _ = _connector(monkeypatch, entries, {})

    records = list(conn.list())
    ids = {r.id for r in records}
    assert ids == {"f1", "d1"}
    folder = next(r for r in records if r.id == "d1")
    assert folder.is_dir and folder.size is None


def test_list_with_no_limit_still_terminates_and_returns_everything(monkeypatch):
    entries = {
        f"f{i}": {"id": f"f{i}", "name": f"{i}.jpg", "mimeType": "image/jpeg",
                  "size": "1", "modifiedTime": "2024-01-01T00:00:00Z", "parents": ["root"]}
        for i in range(5)
    }
    conn, _ = _connector(monkeypatch, entries, {})

    records = list(conn.list(limit=1_000_000))
    assert {r.id for r in records} == set(entries)


def test_list_respects_limit(monkeypatch):
    entries = {
        f"f{i}": {"id": f"f{i}", "name": f"{i}.jpg", "mimeType": "image/jpeg",
                  "size": "1", "modifiedTime": "2024-01-01T00:00:00Z", "parents": ["root"]}
        for i in range(5)
    }
    conn, _ = _connector(monkeypatch, entries, {})

    assert len(list(conn.list(limit=2))) == 2


# --------------------------------------------------------------------------- #
# stat() / quick_hash
# --------------------------------------------------------------------------- #
def test_stat_small_file_hashes_the_whole_thing_in_one_read(monkeypatch):
    content = b"x" * 1000
    entries = {"f1": {"id": "f1", "name": "a.bin", "mimeType": "application/octet-stream",
                      "size": "1000", "modifiedTime": "2024-01-01T00:00:00Z"}}
    conn, svc = _connector(monkeypatch, entries, {"f1": content})

    record = conn.stat("f1")
    assert record.size == 1000
    assert record.quick_hash.startswith("1000:")
    media_calls = [c for c in svc._files.calls if c[0] == "get_media"]
    assert len(media_calls) == 1  # size <= chunk => one ranged read, no separate tail


def test_stat_large_file_hashes_head_and_tail_separately(monkeypatch):
    size = 200_000
    content = bytes(range(256)) * (size // 256 + 1)
    content = content[:size]
    entries = {"f1": {"id": "f1", "name": "a.bin", "mimeType": "application/octet-stream",
                      "size": str(size), "modifiedTime": "2024-01-01T00:00:00Z"}}
    conn, svc = _connector(monkeypatch, entries, {"f1": content})

    record = conn.stat("f1")
    media_calls = [c for c in svc._files.calls if c[0] == "get_media"]
    assert len(media_calls) == 2  # head + tail, no whole-file download


def test_quick_hash_is_stable_and_size_prefixed(monkeypatch):
    content = b"y" * 1000
    entries = {"f1": {"id": "f1", "name": "a.bin", "mimeType": "application/octet-stream",
                      "size": "1000", "modifiedTime": "2024-01-01T00:00:00Z"}}
    conn, _ = _connector(monkeypatch, entries, {"f1": content})

    h1 = conn.stat("f1").quick_hash
    h2 = conn.stat("f1").quick_hash
    assert h1 == h2 == f"1000:{h1.split(':')[1]}"


# --------------------------------------------------------------------------- #
# read()
# --------------------------------------------------------------------------- #
def test_read_whole_file_uses_no_range_header(monkeypatch):
    content = b"abcdef"
    entries = {"f1": {"id": "f1", "name": "a.bin", "mimeType": "application/octet-stream",
                      "size": "6", "modifiedTime": "2024-01-01T00:00:00Z"}}
    conn, _ = _connector(monkeypatch, entries, {"f1": content})

    assert conn.read("f1") == content


def test_read_partial_uses_range_header(monkeypatch):
    content = b"abcdef"
    entries = {"f1": {"id": "f1", "name": "a.bin", "mimeType": "application/octet-stream",
                      "size": "6", "modifiedTime": "2024-01-01T00:00:00Z"}}
    conn, _ = _connector(monkeypatch, entries, {"f1": content})

    assert conn.read("f1", nbytes=3) == b"abc"


# --------------------------------------------------------------------------- #
# delete()
# --------------------------------------------------------------------------- #
def test_delete_dry_run_never_calls_the_api(monkeypatch):
    entries = {"f1": {"id": "f1", "name": "a.bin", "mimeType": "application/octet-stream",
                      "size": "1", "modifiedTime": "2024-01-01T00:00:00Z"}}
    conn, svc = _connector(monkeypatch, entries, {})

    result = conn.delete("f1", commit=False)
    assert not result.committed
    assert svc._files.calls == []


def test_delete_commit_trashes_by_default(monkeypatch):
    entries = {"f1": {"id": "f1", "name": "a.bin", "mimeType": "application/octet-stream",
                      "size": "1", "modifiedTime": "2024-01-01T00:00:00Z"}}
    conn, svc = _connector(monkeypatch, entries, {}, permanent=False)

    result = conn.delete("f1", commit=True)
    assert result.committed
    assert svc._files.calls == [("update", {"fileId": "f1", "body": {"trashed": True}})]


def test_delete_commit_permanent_calls_hard_delete(monkeypatch):
    entries = {"f1": {"id": "f1", "name": "a.bin", "mimeType": "application/octet-stream",
                      "size": "1", "modifiedTime": "2024-01-01T00:00:00Z"}}
    conn, svc = _connector(monkeypatch, entries, {}, permanent=True)

    result = conn.delete("f1", commit=True)
    assert result.committed
    assert svc._files.calls == [("delete", {"fileId": "f1"})]


# --------------------------------------------------------------------------- #
# safe mode (DRIVE_LIVE=0, the default) — never touches _service()
# --------------------------------------------------------------------------- #
def test_safe_mode_list_returns_empty_without_a_service(monkeypatch):
    monkeypatch.setattr(drive_mod, "LIVE", False)
    conn = DriveConnector()
    assert list(conn.list()) == []


def test_safe_mode_read_raises_not_supported(monkeypatch):
    monkeypatch.setattr(drive_mod, "LIVE", False)
    conn = DriveConnector()
    with pytest.raises(NotSupported):
        conn.read("anything")


def test_safe_mode_delete_is_always_a_dry_run(monkeypatch):
    monkeypatch.setattr(drive_mod, "LIVE", False)
    conn = DriveConnector()
    result = conn.delete("f1", commit=True)
    assert not result.committed
