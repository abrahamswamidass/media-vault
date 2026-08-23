"""Connector registry — maps a name to its class and how to build it from env/args."""
from __future__ import annotations

import os

from .nas import NASConnector
from .drive import DriveConnector
from .archive import ArchiveConnector
from .amazon import AmazonConnector


def _read_secret(path: str | None) -> str | None:
    """Read a one-line secret (e.g. a password) from a file, or None if unset."""
    if not path:
        return None
    with open(path, "r", encoding="utf-8") as f:
        return f.read().strip()


def build_connector(name: str, args):
    """Factory used by the CLI. Pulls paths from --root/flags or environment."""
    if name == "nas":
        if os.getenv("NAS_MODE", "mount") == "smb":
            # Direct SMB2/3 client, bypassing the OS mount. See nas_smb.py for why
            # this exists (Docker Desktop on Windows can't reliably bind-mount a
            # mapped network drive).
            from .nas_smb import SMBNASConnector

            host = os.getenv("NAS_HOST")
            share = os.getenv("NAS_SHARE")
            if not host or not share:
                raise SystemExit("NAS_MODE=smb needs NAS_HOST and NAS_SHARE set")
            return SMBNASConnector(
                host, share,
                root=args.root or os.getenv("NAS_SMB_ROOT", ""),
                trash=args.trash or os.getenv("NAS_SMB_TRASH"),
                username=os.getenv("NAS_USER"),
                # File wins if both are set — it doesn't end up in `docker inspect`
                # or shell history the way a plain env var does.
                password=_read_secret(os.getenv("NAS_PASSWORD_FILE")) or os.getenv("NAS_PASSWORD"),
            )
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
