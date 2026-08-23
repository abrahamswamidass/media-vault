"""Connector registry — maps a name to its class and how to build it from env/args."""
from __future__ import annotations

import os

from .nas import NASConnector
from .drive import DriveConnector
from .archive import ArchiveConnector
from .amazon import AmazonConnector


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
