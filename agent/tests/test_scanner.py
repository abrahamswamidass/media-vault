"""
Regression coverage for the scanner's junk-file filter.

Thumbs.db (and its cousins) used to get indexed like any other file and then
fail every single publish attempt forever -- Pillow can't decode a folder
thumbnail cache as an image, and nothing marks a permanently-undecodable item
done, so it silently re-consumed a slot in every future publish batch. See
scanner.py's `_JUNK_NAMES` docstring.
"""
from __future__ import annotations

import argparse

import pytest

from mediavault.catalog import Catalog, scanner
from mediavault.connectors.nas import NASConnector


def _write(root, rel, data):
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(data)
    return p


@pytest.fixture
def catalog(tmp_path):
    with Catalog(str(tmp_path / "cat.sqlite")) as c:
        yield c


def test_known_junk_files_are_never_indexed(tmp_path, catalog):
    nas = tmp_path / "nas"
    _write(nas, "img.jpg", b"pretend-jpeg" * 100)
    _write(nas, "Thumbs.db", b"windows folder thumbnail cache")
    _write(nas, "desktop.ini", b"[.ShellClassInfo]")
    _write(nas, ".DS_Store", b"macos folder metadata")
    _write(nas, "sub/Thumbs.db", b"nested copy too")

    scanner.scan(NASConnector(str(nas)), catalog, source="nas")

    assert catalog.count("nas") == 1
    assert catalog.get("nas", "img.jpg") is not None
    assert catalog.get("nas", "Thumbs.db") is None
    assert catalog.get("nas", "desktop.ini") is None
    assert catalog.get("nas", ".DS_Store") is None
    assert catalog.get("nas", "sub/Thumbs.db") is None


def test_junk_filter_is_case_insensitive(tmp_path, catalog):
    nas = tmp_path / "nas"
    _write(nas, "img.jpg", b"pretend-jpeg" * 100)
    _write(nas, "THUMBS.DB", b"windows folder thumbnail cache, oddly cased")

    scanner.scan(NASConnector(str(nas)), catalog, source="nas")

    assert catalog.count("nas") == 1
    assert catalog.get("nas", "THUMBS.DB") is None
