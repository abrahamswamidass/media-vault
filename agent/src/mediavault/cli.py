"""
Media Vault agent CLI.

    mediavault doctor                          what's configured, what isn't
    mediavault index nas                       walk a source into the catalog
    mediavault dedup nas                       find duplicates (dry-run)
    mediavault dedup nas --commit              archive them
    mediavault publish nas                     preview what needs a thumbnail
    mediavault publish nas --commit            push thumbnails + metadata
    mediavault stats                           what the catalog knows
    mediavault reset nas --commit              wipe local catalog data for a source (testing)

    mediavault nas list --root /data/nas       poke one connector operation
    mediavault nas delete <path> --commit

    mediavault amazon-stage <item-id> --commit stage a NAS item for Amazon (no local file needed)

SAFETY: everything that mutates is dry-run by default. Add --commit to apply.
NAS deletes are soft — the file moves to the trash folder and stays recoverable.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from typing import Optional

from .catalog import Catalog, dedup as dedup_mod, scanner
from .actions.amazon import StageForAmazonAction
from .actions.dedup import ArchiveDuplicatesAction
from .actions.log import ActionLog
from .actions.maintenance import PublishAction
from .connectors import CONNECTORS, build_connector
from .ports import FileRecord, NotSupported, OpResult

CONNECTOR_COMMANDS = ["list", "stat", "read", "delete", "upload", "caps"]


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def _emit(obj, as_json: bool):
    if as_json:
        if isinstance(obj, (OpResult, FileRecord)):
            obj = obj.to_dict()
        print(json.dumps(obj, indent=2, default=str))
    else:
        print(obj)


def _human(n: int) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(n) < 1024 or unit == "TB":
            return f"{n:,.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n} B"


def _banner(committed: bool):
    print("-- DRY-RUN — nothing changed. Re-run with --commit to apply. --"
          if not committed else "** COMMITTED **")


def _catalog(args) -> Catalog:
    return Catalog(args.db or os.getenv("CATALOG_DB", "/data/catalog/catalog.sqlite"))


def _blobs_for(args):
    """GCSBlobStore when GCS_LIVE=1, else a local folder — same live-switch the
    connectors use, so `publish` is safe to run before any cloud is configured."""
    if os.getenv("GCS_LIVE", "0") == "1":
        from .blobstore import GCSBlobStore
        return GCSBlobStore()
    from .blobstore import LocalBlobStore
    return LocalBlobStore(getattr(args, "blob_dir", None) or os.getenv("BLOB_CACHE", "/data/catalog/blobs"))


def _facts_for(args):
    """FirestoreFactsStore when GCS_LIVE=1, else a local folder of JSON files."""
    if os.getenv("GCS_LIVE", "0") == "1":
        from .sync.facts import FirestoreFactsStore
        return FirestoreFactsStore()
    from .sync.facts import LocalFactsStore
    return LocalFactsStore(getattr(args, "facts_dir", None) or os.getenv("FACTS_CACHE", "/data/catalog/facts"))


def _connector_for(source: str, args):
    """Build a connector for a source, letting --root override the environment."""
    ns = argparse.Namespace(
        root=getattr(args, "root", None), trash=getattr(args, "trash", None),
        permanent=getattr(args, "permanent", False))
    return build_connector(source, ns)


# --------------------------------------------------------------------------- #
# doctor
# --------------------------------------------------------------------------- #
def cmd_doctor(args) -> int:
    from .doctor import MARKS, FAIL, WARN, report, run_checks

    checks = run_checks()
    if args.json:
        _emit({"checks": [c.__dict__ for c in checks], **report(checks)}, True)
        return 0 if report(checks)["ready"] else 1

    group = None
    for check in checks:
        if check.group != group:
            group = check.group
            print(f"\n{group}")
        print(f"  {MARKS[check.status]} {check.name:18} {check.detail}")
        if check.status in (FAIL, WARN) and check.fix:
            for line in _wrap(check.fix, 66):
                print(f"      {line}")

    summary = report(checks)
    print()
    if summary["ready"]:
        print(f"Ready. {summary['ok']} ok, {summary['warnings']} optional item(s) not set up.")
    else:
        print(f"Not ready — {summary['failures']} blocking issue(s). Fix those first.")
    return 0 if summary["ready"] else 1


def _wrap(text: str, width: int) -> list[str]:
    words, lines, current = text.split(), [], ""
    for w in words:
        if len(current) + len(w) + 1 > width:
            lines.append(current)
            current = w
        else:
            current = f"{current} {w}".strip()
    if current:
        lines.append(current)
    return lines


# --------------------------------------------------------------------------- #
# index
# --------------------------------------------------------------------------- #
def cmd_index(args) -> int:
    connector = _connector_for(args.source, args)
    with _catalog(args) as catalog:
        state = catalog.scan_state(args.source)
        if state and not state["complete"] and not args.restart:
            print(f"Resuming interrupted scan from: {state['cursor'] or '(start)'}")

        def progress(p):
            if not args.quiet:
                # A leading newline moves past --debug's in-place file line,
                # which never printed one of its own so it can overwrite itself.
                prefix = "\n" if args.debug else ""
                print(f"{prefix}  {p.directory or '/':50} {p.files_indexed:>6} files")

        def file_progress(item_id, i, total):
            # \r + no newline: overwrites in place rather than spamming one
            # line per file, but still gives you a live "what's it doing right
            # now" view — the exact thing missing when a big directory
            # (hundreds/thousands of files) looks like it might be stuck.
            print(f"\r    {i}/{total} {item_id[:70]:<70}", end="", flush=True)

        report = scanner.scan(connector, catalog, source=args.source,
                              resume=not args.restart, on_progress=progress,
                              on_file=file_progress if args.debug else None)

        if args.json:
            _emit(report.__dict__, True)
            return 0 if report.ok else 1

        print(f"\nIndexed {report.files_indexed:,} files across "
              f"{report.directories:,} directories in '{args.source}'.")
        if report.resumed_from:
            print(f"(resumed from {report.resumed_from})")
        if report.errors:
            print(f"\n{report.errors} error(s):")
            for sample in report.error_samples:
                print(f"  ! {sample}")
        return 0 if report.ok else 1


# --------------------------------------------------------------------------- #
# dedup
# --------------------------------------------------------------------------- #
def cmd_dedup(args) -> int:
    connector = _connector_for(args.source, args)

    with _catalog(args) as catalog:
        if catalog.count(args.source) == 0:
            print(f"Nothing indexed for '{args.source}'. Run: mediavault index {args.source}")
            return 1

        groups = dedup_mod.find_duplicates(
            catalog, args.source, connector,
            confirm=not args.no_confirm, min_size=args.min_size)
        summary = dedup_mod.summarize(groups)

        if args.json:
            _emit({
                "summary": summary,
                "groups": [{
                    "quick_hash": g.quick_hash,
                    "keeper": g.keeper["item_id"],
                    "keeper_reason": g.keeper_reason,
                    "losers": [r["item_id"] for r in g.losers],
                    "confirmed": g.confirmed,
                    "note": g.confirm_note,
                    "reclaimable_bytes": g.reclaimable_bytes,
                } for g in groups],
            }, True)
            return 0

        if not groups:
            print(f"No duplicates found in '{args.source}'.")
            return 0

        print(f"Duplicates within '{args.source}' "
              f"(never compared against other sources):\n")
        for g in groups[:args.limit]:
            mark = "•" if g.safe_to_archive else "!"
            print(f"{mark} {len(g.losers) + 1} copies · {_human(g.reclaimable_bytes)} reclaimable")
            print(f"    keep    {g.keeper['item_id']}   ({g.keeper_reason})")
            for row in g.losers:
                print(f"    archive {row['item_id']}")
            if not g.safe_to_archive or g._split:
                print(f"    note    {g.confirm_note}")
            print()

        if len(groups) > args.limit:
            print(f"... and {len(groups) - args.limit} more group(s). "
                  f"Use --limit to see more.\n")

        print(f"{summary['archivable_groups']} group(s) ready to archive, "
              f"{summary['redundant_copies']} redundant copies, "
              f"{_human(summary['reclaimable_bytes'])} reclaimable.")
        if summary["unconfirmed_groups"]:
            print(f"{summary['unconfirmed_groups']} group(s) unconfirmed and will be skipped.")
        if summary["split_by_verification"]:
            print(f"{summary['split_by_verification']} file(s) shared a fingerprint but "
                  f"differed in content — left alone.")

        # --- act ---------------------------------------------------------- #
        actionable = [g for g in groups if g.safe_to_archive]
        if not actionable:
            return 0

        log = ActionLog(args.log_dir or os.getenv("ACTION_LOG", "/data/catalog/actions"))
        archived = failed = 0
        reclaimed = 0

        print()
        for g in actionable:
            result = log.record(
                ArchiveDuplicatesAction(g, connector, catalog).run(commit=args.commit))
            if result.status == "failed":
                failed += 1
                print(f"  ✗ {result.detail}")
            elif args.commit:
                archived += 1
                reclaimed += result.outputs.get("bytes_reclaimed", 0)

        _banner(args.commit)
        if args.commit:
            print(f"Archived {archived} group(s), reclaimed {_human(reclaimed)}. "
                  f"Copies moved to trash and are recoverable.")
            if failed:
                print(f"{failed} group(s) failed — see the journal.")
        return 0


# --------------------------------------------------------------------------- #
# publish
# --------------------------------------------------------------------------- #
def cmd_publish(args) -> int:
    connector = _connector_for(args.source, args)
    blobs = _blobs_for(args)
    facts = _facts_for(args)

    with _catalog(args) as catalog:
        if catalog.count(args.source) == 0:
            print(f"Nothing indexed for '{args.source}'. Run: mediavault index {args.source}")
            return 1

        log = ActionLog(args.log_dir or os.getenv("ACTION_LOG", "/data/catalog/actions"))
        result = log.record(
            PublishAction(args.source, connector, catalog, blobs, facts,
                          max_items=args.max_items).run(commit=args.commit))

        if args.json:
            _emit(result.to_dict(), True)
            return 0 if result.status != "failed" else 1

        print(result.detail)
        if result.status == "ok" and result.committed:
            _banner(True)
            print(f"Published {result.outputs.get('published', 0)} item(s) "
                  f"(thumbnails -> {blobs.name}, metadata -> {facts.name}).")
            failed = result.outputs.get("failed") or []
            if failed:
                print(f"{len(failed)} item(s) failed — see the journal.")
        elif not args.commit and result.status == "ok":
            _banner(False)
        return 0 if result.status != "failed" else 1


# --------------------------------------------------------------------------- #
# stats
# --------------------------------------------------------------------------- #
def cmd_stats(args) -> int:
    with _catalog(args) as catalog:
        sources = catalog.sources()
        if not sources:
            print("Catalog is empty. Run: mediavault index nas")
            return 0

        rows = []
        for source in sources:
            rows.append({
                "source": source,
                "indexed": catalog.count(source),
                "archived": catalog.count(source, state="archived"),
                "published": catalog.published_count(source),
                "duplicate_groups": len(catalog.duplicate_groups(source)),
                "reclaimable_bytes": catalog.wasted_bytes(source),
            })

        if args.json:
            _emit(rows, True)
            return 0

        print(f"{'source':10} {'indexed':>10} {'archived':>10} {'published':>10} "
              f"{'dup groups':>12} {'reclaimable':>13}")
        for r in rows:
            print(f"{r['source']:10} {r['indexed']:>10,} {r['archived']:>10,} "
                  f"{r['published']:>10,} {r['duplicate_groups']:>12,} "
                  f"{_human(r['reclaimable_bytes']):>13}")
        return 0


# --------------------------------------------------------------------------- #
# reset
# --------------------------------------------------------------------------- #
def cmd_reset(args) -> int:
    if not args.all and not args.source:
        raise SystemExit("reset needs a source (e.g. 'nas'), or --all")

    target = "the entire catalog" if args.all else f"'{args.source}'"
    source = None if args.all else args.source

    with _catalog(args) as catalog:
        where = "" if args.all else "WHERE source = ?"
        params = () if args.all else (args.source,)
        n = catalog.conn.execute(f"SELECT COUNT(*) FROM items {where}", params).fetchone()[0]

        if not args.commit:
            if args.json:
                _emit({"target": target, "item_rows": n, "purge_facts": args.purge_facts,
                      "committed": False}, True)
            else:
                print(f"DRY-RUN (no change): would delete {n:,} item row(s) and reset "
                      f"scan checkpoints for {target}.")
                if args.purge_facts:
                    print("Would also delete every published fact (Firestore/local) "
                          f"for {target}.")
                print("Re-run with --commit to apply.")
            return 0

        result = catalog.reset(source)

        facts_deleted = None
        if args.purge_facts:
            facts_deleted = _facts_for(args).purge(source)

        if args.json:
            out = {**result, "target": target, "committed": True}
            if facts_deleted is not None:
                out["facts_deleted"] = facts_deleted
            _emit(out, True)
            return 0

        _banner(True)
        print(f"Reset {target}: deleted {result['items_deleted']:,} item row(s), "
              f"{result['scans_deleted']:,} scan checkpoint(s).")
        if facts_deleted is not None:
            print(f"Also purged {facts_deleted:,} published fact(s) for {target}.")
        print("Nothing on the NAS or in GCS changed — thumbnails already pushed are "
              "content-addressed, so re-publishing after a re-index finds them and "
              "skips straight to (re)writing the fact.")
        return 0


# --------------------------------------------------------------------------- #
# amazon-stage
# --------------------------------------------------------------------------- #
def cmd_amazon_stage(args) -> int:
    empty_args = argparse.Namespace(root=None, trash=None, permanent=False)
    source = build_connector(args.source, empty_args)
    amazon = build_connector("amazon", empty_args)

    log = ActionLog(args.log_dir or os.getenv("ACTION_LOG", "/data/catalog/actions"))
    result = log.record(
        StageForAmazonAction(args.item_id, source, amazon).run(commit=args.commit))

    if args.json:
        _emit(result.to_dict(), True)
        return 0 if result.status != "failed" else 1

    print(result.detail)
    if result.committed:
        _banner(True)
    elif result.status == "ok":
        _banner(False)
    return 0 if result.status != "failed" else 1


# --------------------------------------------------------------------------- #
# connector passthrough
# --------------------------------------------------------------------------- #
def cmd_connector(args) -> int:
    conn = build_connector(args.source, args)
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
            _require(args.target, "target id/path")
            _emit(conn.stat(args.target), True)

        elif args.command == "read":
            _require(args.target, "target id/path")
            data = conn.read(args.target, args.peek)
            preview = data[: args.peek] if args.peek else data
            print(f"read {len(data)} byte(s). First {len(preview)}:")
            print(preview.hex(" ") if not args.json else json.dumps({"hex": preview.hex()}))

        elif args.command == "delete":
            _require(args.target, "target id/path")
            res = conn.delete(args.target, commit=args.commit)
            _banner(res.committed)
            _emit(res, args.json)

        elif args.command == "upload":
            _require(args.target, "local file path")
            res = conn.upload(args.target, dest=args.dest, commit=args.commit)
            _banner(res.committed)
            _emit(res, args.json)

    except NotSupported as e:
        print(f"[not-supported] {e}", file=sys.stderr)
        return 2
    except (FileNotFoundError, ValueError) as e:
        print(f"[error] {e}", file=sys.stderr)
        return 1
    return 0


def _require(value, what: str):
    if not value:
        raise SystemExit(f"this command needs a {what}")


# --------------------------------------------------------------------------- #
# Parser
# --------------------------------------------------------------------------- #
def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="mediavault",
        description="Media Vault local agent — the only thing that touches your files.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__)
    p.add_argument("--json", action="store_true", help="machine-readable output")
    sub = p.add_subparsers(dest="_cmd", required=True)

    # -- doctor --
    d = sub.add_parser("doctor", help="check configuration and report what's missing")
    d.set_defaults(_fn=cmd_doctor)

    # -- index --
    i = sub.add_parser("index", help="walk a source into the local catalog (resumable)")
    i.add_argument("source", choices=CONNECTORS)
    i.add_argument("--root", help="override the connector root")
    i.add_argument("--trash", help="override the NAS trash folder")
    i.add_argument("--db", help="catalog database path")
    i.add_argument("--restart", action="store_true",
                   help="ignore any checkpoint and scan from the beginning")
    i.add_argument("--quiet", action="store_true", help="suppress per-directory progress")
    i.add_argument("--debug", action="store_true",
                   help="show live per-file progress within each directory "
                        "(what it's stat-ing right now) — useful for telling "
                        "a big directory apart from a genuinely stuck scan")
    i.set_defaults(_fn=cmd_index, permanent=False)

    # -- dedup --
    dd = sub.add_parser(
        "dedup",
        help="find identical copies WITHIN one source and archive the extras",
        description="Duplicates are only ever compared inside a single source. "
                    "The same photo on the NAS and in Drive is this system working "
                    "as designed, and is never touched.")
    dd.add_argument("source", choices=CONNECTORS)
    dd.add_argument("--root", help="override the connector root")
    dd.add_argument("--trash", help="override the NAS trash folder")
    dd.add_argument("--db", help="catalog database path")
    dd.add_argument("--log-dir", help="where to write the action journal")
    dd.add_argument("--commit", action="store_true",
                    help="ACTUALLY archive the duplicates (default: preview only)")
    dd.add_argument("--no-confirm", action="store_true",
                    help="skip full-content verification (faster, and nothing "
                         "unverified will be archived)")
    dd.add_argument("--min-size", type=int, default=1,
                    help="ignore files smaller than this many bytes")
    dd.add_argument("--limit", type=int, default=20, help="groups to print")
    dd.set_defaults(_fn=cmd_dedup, permanent=False)

    # -- publish --
    pub = sub.add_parser(
        "publish",
        help="push a thumbnail + metadata fact for every catalog item not yet published",
        description="Walks the catalog for items with no thumbnail/metadata pushed "
                    "yet. Thumbnails go to GCSBlobStore (GCS_LIVE=1) or a local "
                    "folder; metadata facts go to Firestore (GCS_LIVE=1) or a local "
                    "folder of JSON files — same live-switch the connectors use.")
    pub.add_argument("source", choices=CONNECTORS)
    pub.add_argument("--root", help="override the connector root")
    pub.add_argument("--trash", help="override the NAS trash folder")
    pub.add_argument("--db", help="catalog database path")
    pub.add_argument("--log-dir", help="where to write the action journal")
    pub.add_argument("--blob-dir", help="local thumbnail folder (ignored if GCS_LIVE=1)")
    pub.add_argument("--facts-dir", help="local facts folder (ignored if GCS_LIVE=1)")
    pub.add_argument("--max-items", type=int, help="publish at most this many items")
    pub.add_argument("--commit", action="store_true",
                     help="ACTUALLY generate and push (default: preview only)")
    pub.set_defaults(_fn=cmd_publish, permanent=False)

    # -- stats --
    s = sub.add_parser("stats", help="what the catalog currently knows")
    s.add_argument("--db", help="catalog database path")
    s.set_defaults(_fn=cmd_stats)

    # -- reset --
    rs = sub.add_parser(
        "reset",
        help="wipe local catalog data for testing (never touches the NAS or GCS)",
        description="Deletes catalog rows and scan checkpoints so the next index "
                    "starts fresh. Thumbnails in GCS are left alone regardless — "
                    "content-addressed, so republishing after a re-index costs "
                    "nothing extra. Published Firestore/local facts are left alone "
                    "too unless --purge-facts is given; pass it when the scan root "
                    "changed (e.g. a subfolder -> the whole share), since that "
                    "changes item_id and leaves the old facts stale rather than "
                    "overwritten.")
    rs.add_argument("source", nargs="?", choices=CONNECTORS,
                    help="source to reset (omit with --all)")
    rs.add_argument("--all", action="store_true", help="reset every source")
    rs.add_argument("--db", help="catalog database path")
    rs.add_argument("--purge-facts", action="store_true",
                    help="also delete every published fact (Firestore/local) for this source")
    rs.add_argument("--facts-dir", help="local facts folder override (ignored if GCS_LIVE=1)")
    rs.add_argument("--commit", action="store_true",
                    help="ACTUALLY delete (default: preview only)")
    rs.set_defaults(_fn=cmd_reset)

    # -- amazon-stage --
    st = sub.add_parser(
        "amazon-stage",
        help="stage an item from another connector (e.g. NAS) for Amazon — no local file needed",
        description="Reads the item straight from --source (default: nas) and stages "
                    "it into Amazon's dated album folder, same as 'mediavault amazon "
                    "upload' but without needing the file on the container's own "
                    "filesystem first.")
    st.add_argument("item_id", help="source connector item id (path)")
    st.add_argument("--source", choices=CONNECTORS, default="nas")
    st.add_argument("--log-dir", help="where to write the action journal")
    st.add_argument("--commit", action="store_true",
                    help="ACTUALLY stage it (default: preview only)")
    st.set_defaults(_fn=cmd_amazon_stage)

    # -- connectors --
    for name in CONNECTORS:
        c = sub.add_parser(name, help=f"run one {name} connector operation")
        c.add_argument("command", choices=CONNECTOR_COMMANDS)
        c.add_argument("target", nargs="?", default="",
                       help="item id / path, or local file for upload")
        c.add_argument("--root", help="connector root, or Drive credentials JSON")
        c.add_argument("--trash", help="NAS trash folder (default <root>/_trash)")
        c.add_argument("--prefix", default="", help="list: subpath to list under")
        c.add_argument("--limit", type=int, default=100, help="list: max items")
        c.add_argument("--peek", type=int, default=64, help="read: bytes to show")
        c.add_argument("--dest", default="", help="upload: destination sub-path")
        c.add_argument("--commit", action="store_true",
                       help="ACTUALLY perform a mutating op (default: dry-run)")
        c.add_argument("--permanent", action="store_true",
                       help="drive delete: permanent instead of Drive trash")
        c.set_defaults(_fn=cmd_connector, source=name)

    return p


def main(argv: Optional[list[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if not hasattr(args, "json"):
        args.json = False
    return args._fn(args) or 0


if __name__ == "__main__":
    sys.exit(main())
