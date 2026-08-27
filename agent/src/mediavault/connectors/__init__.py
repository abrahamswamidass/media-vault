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


def _smb_host_share() -> tuple[str, str]:
    host, share = os.getenv("NAS_HOST"), os.getenv("NAS_SHARE")
    if not host or not share:
        raise SystemExit("NAS_MODE=smb needs NAS_HOST and NAS_SHARE set")
    return host, share


def _smb_password() -> str | None:
    # File wins if both are set — it doesn't end up in `docker inspect` or
    # shell history the way a plain env var does.
    return _read_secret(os.getenv("NAS_PASSWORD_FILE")) or os.getenv("NAS_PASSWORD")


def build_connector(name: str, args):
    """Factory used by the CLI. Pulls paths from --root/flags or environment."""
    if name == "nas":
        if os.getenv("NAS_MODE", "mount") == "smb":
            # Direct SMB2/3 client, bypassing the OS mount. See nas_smb.py for why
            # this exists (Docker Desktop on Windows can't reliably bind-mount a
            # mapped network drive).
            from .nas_smb import SMBNASConnector

            host, share = _smb_host_share()
            return SMBNASConnector(
                host, share,
                root=args.root or os.getenv("NAS_SMB_ROOT", ""),
                # NAS_SMB_TRASH is a fixed spot relative to the SHARE (not the
                # root above), so archiving lands in the same place no matter
                # which subfolder you point NAS_SMB_ROOT at.
                trash=args.trash or os.getenv("NAS_SMB_TRASH"),
                username=os.getenv("NAS_USER"),
                password=_smb_password(),
                # Amazon staging is a share-relative operational folder too — if
                # NAS_SMB_ROOT ever covers the whole share, it must never show up
                # as regular library content (index/dedup/publish would treat
                # staged-but-not-yet-uploaded files as duplicates to archive).
                exclude=[os.getenv("AMAZON_SMB_ROOT")] if os.getenv("AMAZON_SMB_ROOT") else None,
            )
        root = args.root or os.getenv("NAS_ROOT")
        if not root:
            raise SystemExit("nas needs --root <path> (or set NAS_ROOT)")
        return NASConnector(
            root, trash=args.trash or os.getenv("NAS_TRASH"),
            exclude=[os.getenv("AMAZON_STAGING")] if os.getenv("AMAZON_STAGING") else None,
        )

    if name == "drive":
        # --root overrides which Drive folder is treated as the scan root (a
        # folder ID, not a path — Drive has no path concept). Defaults to
        # DRIVE_ROOT_FOLDER_ID, or "root" (Drive's alias for My Drive itself).
        return DriveConnector(root_folder_id=args.root, permanent=args.permanent)

    if name == "archive":
        root = args.root or os.getenv("ARCHIVE_ROOT")
        if not root:
            raise SystemExit("archive needs --root <exported-folder>")
        return ArchiveConnector(root)

    if name == "amazon":
        if os.getenv("NAS_MODE", "mount") == "smb":
            from .nas_smb import SMBNASConnector

            host, share = _smb_host_share()
            root = args.root or os.getenv("AMAZON_SMB_ROOT", "_AmazonUpload")
            fs = SMBNASConnector(
                host, share, root=root,
                # This staging folder never soft-deletes, so trash is unused —
                # a stray non-existent subpath is fine (it's only ever asked
                # for on delete(), which Amazon's connector doesn't call).
                trash=f"{root}/__never_used_trash",
                username=os.getenv("NAS_USER"),
                password=_smb_password(),
            )
            return AmazonConnector(fs=fs)
        root = args.root or os.getenv("AMAZON_STAGING")
        if not root:
            raise SystemExit("amazon needs --root <staging-folder> (the Amazon-watched folder)")
        return AmazonConnector(root)

    raise SystemExit(f"unknown connector: {name}")


CONNECTORS = ["nas", "drive", "archive", "amazon"]
