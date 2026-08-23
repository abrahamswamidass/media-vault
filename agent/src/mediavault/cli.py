"""
Test harness CLI — poke one connector operation at a time.

Usage:
    python -m harness.cli <connector> <command> [args] [flags]

Connectors : nas | drive | archive | amazon
Commands   : list | stat | read | delete | upload | caps

SAFETY: destructive/mutating commands (delete, upload) are DRY-RUN by default.
Add --commit to actually perform them. NAS delete is a soft move-to-trash.

Examples:
    python -m harness.cli nas list --root /mnt/nas --prefix Photos
    python -m harness.cli nas stat  --root /mnt/nas "Photos/img_001.jpg"
    python -m harness.cli nas read  --root /mnt/nas "Photos/img_001.jpg" --peek 32
    python -m harness.cli nas delete --root /mnt/nas "Photos/junk.jpg"            # dry-run
    python -m harness.cli nas delete --root /mnt/nas "Photos/junk.jpg" --commit   # real (to trash)
    python -m harness.cli amazon upload --root /mnt/nas/_AmazonUpload ./pic.jpg --commit
    python -m harness.cli drive delete --root creds.json "<fileId>" --permanent   # safe-mode dry-run
"""
from __future__ import annotations

import argparse
import json
import sys

from .connectors import build_connector, CONNECTORS
from .ports import NotSupported, OpResult, FileRecord


def _emit(obj, as_json: bool):
    if as_json:
        if isinstance(obj, (OpResult, FileRecord)):
            obj = obj.to_dict()
        print(json.dumps(obj, indent=2, default=str))
    else:
        print(obj)


def main(argv=None):
    p = argparse.ArgumentParser(prog="harness", description="Connector operation test harness")
    p.add_argument("connector", choices=CONNECTORS)
    p.add_argument("command", choices=["list", "stat", "read", "delete", "upload", "caps"])
    p.add_argument("target", nargs="?", default="", help="item id / path / local file for upload")

    p.add_argument("--root", help="connector root (NAS/archive/amazon path, or Drive creds.json)")
    p.add_argument("--trash", help="NAS trash folder (default <root>/_trash)")
    p.add_argument("--prefix", default="", help="list: subpath/prefix to list under")
    p.add_argument("--limit", type=int, default=100, help="list: max items")
    p.add_argument("--peek", type=int, default=64, help="read: bytes to show (0 = whole file)")
    p.add_argument("--dest", default="", help="upload: destination sub-path")
    p.add_argument("--commit", action="store_true", help="ACTUALLY perform a mutating op (default: dry-run)")
    p.add_argument("--permanent", action="store_true", help="drive delete: permanent vs trash")
    p.add_argument("--json", action="store_true", help="machine-readable output")

    # Robust parsing: allow the target to appear in ANY position (before or after
    # flags) and across all Python versions. parse_known_args won't choke on a
    # positional that follows options; we then fold any leftover into `target`.
    args, extra = p.parse_known_args(argv)
    leftover = [x for x in extra if not x.startswith("-")]
    unknown_flags = [x for x in extra if x.startswith("-")]
    if unknown_flags:
        p.error(f"unrecognized flag(s): {' '.join(unknown_flags)}")
    if leftover:
        if args.target:
            p.error(f"too many positional arguments: {args.target!r} and {leftover!r}")
        args.target = leftover[0]
        if len(leftover) > 1:
            p.error(f"too many positional arguments: {leftover[1:]}")

    conn = build_connector(args.connector, args)

    try:
        if args.command == "caps":
            _emit({"connector": conn.name, "can_delete": conn.can_delete,
                   "can_upload": conn.can_upload}, args.json)

        elif args.command == "list":
            rows = list(conn.list(args.prefix, args.limit))
            if args.json:
                _emit([r.to_dict() for r in rows], True)
            else:
                for r in rows:
                    tag = "DIR " if r.is_dir else "FILE"
                    size = "" if r.size is None else f"{r.size:>12,d}"
                    print(f"{tag} {size}  {r.id}")
                print(f"\n{len(rows)} item(s).")

        elif args.command == "stat":
            _require_target(args)
            _emit(conn.stat(args.target), args.json or True)  # stat is always structured

        elif args.command == "read":
            _require_target(args)
            data = conn.read(args.target, args.peek)
            preview = data[: args.peek] if args.peek else data
            print(f"read {len(data)} byte(s). First {len(preview)}:")
            print(preview.hex(" ") if not args.json else json.dumps({"hex": preview.hex()}))

        elif args.command == "delete":
            _require_target(args)
            res = conn.delete(args.target, commit=args.commit)
            _banner(res)
            _emit(res, args.json)

        elif args.command == "upload":
            _require_target(args, what="local file path")
            res = conn.upload(args.target, dest=args.dest, commit=args.commit)
            _banner(res)
            _emit(res, args.json)

    except NotSupported as e:
        print(f"[not-supported] {e}", file=sys.stderr)
        sys.exit(2)
    except (FileNotFoundError, ValueError) as e:
        print(f"[error] {e}", file=sys.stderr)
        sys.exit(1)


def _require_target(args, what="target id/path"):
    if not args.target:
        raise SystemExit(f"'{args.command}' needs a {what}")


def _banner(res: OpResult):
    if not res.committed:
        print("‑‑ DRY-RUN — nothing changed. Re-run with --commit to apply. ‑‑")
    else:
        print("** COMMITTED **")


if __name__ == "__main__":
    main()
