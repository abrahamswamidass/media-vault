"""
Media Vault agent CLI.

    mediavault doctor                          what's configured, what isn't
    mediavault index nas                       walk a source into the catalog
    mediavault dedup nas                       find duplicates (dry-run)
    mediavault dedup nas --commit              archive them
    mediavault publish nas                     preview what needs a thumbnail
    mediavault publish nas --commit            push thumbnails + metadata
    mediavault cold-archive nas                preview what's not yet in cold storage
    mediavault cold-archive nas --commit --max-items 20   push a small batch first
    mediavault stats                           what the catalog knows
    mediavault reset nas --commit              wipe local catalog data for a source (testing)

    mediavault nas list --root /data/nas       poke one connector operation
    mediavault nas delete <path> --commit

    mediavault amazon-stage <item-id> --commit stage a NAS item for Amazon (no local file needed)

    mediavault process-intents --commit        claim and run requests the web module wrote

SAFETY: everything that mutates is dry-run by default. Add --commit to apply.
NAS deletes are soft — the file moves to the trash folder and stays recoverable.
"""
from __future__ import annotations

import argparse
import json
import os
import signal
import sys
import time
from datetime import datetime, timezone
from typing import Optional

from .catalog import Catalog, dedup as dedup_mod, scanner
from .actions.amazon import StageForAmazonAction
from .actions.coldstorage import ColdArchiveAction
from .actions.dedup import ArchiveDuplicatesAction
from .actions.log import ActionLog
from .actions.maintenance import PublishAction
from .connectors import CONNECTORS, build_connector
from .ports import FileRecord, NotSupported, OpResult

CONNECTOR_COMMANDS = ["list", "stat", "read", "delete", "restore", "upload", "caps"]


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


def _coldstore_for(args):
    """A separate GCS bucket from GCS_BUCKET (thumbnails/previews) — cold
    storage holds full originals under a different lifecycle/pricing
    policy (Archive class, no expiry rule), so it can't share the same
    bucket the preview-expiry lifecycle rule already governs."""
    if os.getenv("GCS_LIVE", "0") == "1":
        from .blobstore import GCSBlobStore
        bucket = os.getenv("COLD_STORAGE_BUCKET", "")
        if not bucket:
            raise SystemExit("COLD_STORAGE_BUCKET is not set (GCS_LIVE=1 requires it).")
        return GCSBlobStore(bucket=bucket)
    from .blobstore import LocalBlobStore
    return LocalBlobStore(getattr(args, "coldstore_dir", None)
                          or os.getenv("COLDSTORE_CACHE", "/data/catalog/coldstore"))


def _intents_for(args):
    """FirestoreIntentsStore when GCS_LIVE=1, else a local folder of JSON files."""
    if os.getenv("GCS_LIVE", "0") == "1":
        from .sync.intents_store import FirestoreIntentsStore
        return FirestoreIntentsStore()
    from .sync.intents_store import LocalIntentsStore
    return LocalIntentsStore(getattr(args, "intents_dir", None) or os.getenv("INTENTS_CACHE", "/data/catalog/intents"))


class _LazyConnectors:
    """A dict-like `AgentContext.connectors` that builds (and caches) a
    connector only the first time an intent actually needs it, instead of
    eagerly building all four up front — a batch of intents that never
    touches Drive shouldn't fail just because Drive isn't configured."""

    def __init__(self):
        self._built: dict = {}

    def __contains__(self, name: str) -> bool:
        return name in CONNECTORS

    def __getitem__(self, name: str):
        if name not in self._built:
            self._built[name] = build_connector(
                name, argparse.Namespace(root=None, trash=None, permanent=False))
        return self._built[name]


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
                print(f"  {p.directory or '/':50} {p.files_indexed:>6} files")

        def file_progress(item_id, i, total):
            # A plain line per file, not an in-place \r overwrite — some
            # terminal front-ends (observed: Docker Desktop's built-in exec
            # panel) only render a line once a real newline arrives, so a
            # carriage-return-only update can sit invisible indefinitely even
            # while genuinely making progress. A bit more scroll noise, but
            # it's guaranteed visible everywhere, which is the actual point
            # of --debug.
            print(f"    {i}/{total} {item_id}", flush=True)

        def list_progress(directory):
            # Covers the resume "skip phase" — fast-forwarding to a cursor
            # re-walks every prior directory with zero other progress signal,
            # so a hang there looks identical to "just resumed, nothing yet".
            print(f"  listing: {directory or '/'}", flush=True)

        report = scanner.scan(connector, catalog, source=args.source,
                              resume=not args.restart, on_progress=progress,
                              on_file=file_progress if args.debug else None,
                              on_list=list_progress if args.debug else None)

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

        def confirm_progress(done, total, item_id):
            count = f"{done}/{total}" if total else str(done)
            print(f"  confirming {count}: {item_id}", flush=True)

        groups = dedup_mod.find_duplicates(
            catalog, args.source, connector,
            confirm=not args.no_confirm, min_size=args.min_size,
            on_confirm=confirm_progress if args.debug else None)
        summary = dedup_mod.summarize(groups)

        if args.by_folder:
            breakdown = dedup_mod.folder_breakdown(groups, depth=args.depth)
            if args.json:
                _emit({"summary": summary, "by_folder": breakdown}, True)
                return 0
            if not breakdown:
                print(f"No archivable duplicates found in '{args.source}'.")
                return 0
            print(f"Reclaimable space by folder (depth {args.depth}) in '{args.source}':\n")
            width = max(len(b["folder"]) for b in breakdown)
            for b in breakdown:
                print(f"  {b['folder']:<{width}}  {b['copies']:>5} copies  "
                      f"{_human(b['reclaimable_bytes']):>10} reclaimable")
            print(f"\n{summary['archivable_groups']} group(s), "
                  f"{_human(summary['reclaimable_bytes'])} reclaimable total.")
            return 0

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
        if args.max_groups is not None:
            actionable = actionable[:args.max_groups]

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
                          max_items=args.max_items, force=args.force,
                          mime_only=args.mime_only, debug=args.debug).run(commit=args.commit))

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
                print(f"{len(failed)} item(s) failed:")
                for f in failed[:10]:
                    print(f"  ! {f['item_id']}: {f['error']}")
                if len(failed) > 10:
                    print(f"  ... and {len(failed) - 10} more (see the journal for all of them).")
        elif not args.commit and result.status == "ok":
            _banner(False)
        return 0 if result.status != "failed" else 1


# --------------------------------------------------------------------------- #
# cold-archive
# --------------------------------------------------------------------------- #
def cmd_cold_archive(args) -> int:
    connector = _connector_for(args.source, args)
    coldstore = _coldstore_for(args)

    with _catalog(args) as catalog:
        if catalog.count(args.source) == 0:
            print(f"Nothing indexed for '{args.source}'. Run: mediavault index {args.source}")
            return 1

        rows = catalog.not_cold_archived(args.source, limit=args.max_items)
        if not rows:
            print(f"Nothing new to push for '{args.source}' — everything indexed is "
                  f"already in cold storage ({catalog.cold_archived_count(args.source)} total).")
            return 0

        total_bytes = sum(r["size"] or 0 for r in rows)
        capped = " (--max-items cap)" if args.max_items else ""
        print(f"{len(rows)} file(s) not yet in cold storage, {_human(total_bytes)}{capped}.\n")

        log = ActionLog(args.log_dir or os.getenv("ACTION_LOG", "/data/catalog/actions"))
        pushed = failed = noop = 0
        pushed_bytes = 0

        for row in rows:
            result = log.record(
                ColdArchiveAction(row["item_id"], connector, coldstore, catalog)
                .run(commit=args.commit))
            if result.status == "failed":
                failed += 1
                print(f"  ! {row['item_id']}: {result.detail}")
            elif result.status == "no-op":
                noop += 1
            elif args.commit:
                pushed += 1
                pushed_bytes += row["size"] or 0
                print(f"  + {row['item_id']} ({_human(row['size'] or 0)})")

        _banner(args.commit)
        if args.commit:
            print(f"Pushed {pushed} file(s), {_human(pushed_bytes)}, to cold storage "
                  f"({coldstore.name}). NAS originals left in place.")
            if noop:
                print(f"{noop} already there (caught up by a prior run).")
            if failed:
                print(f"{failed} failed — see the journal.")
        else:
            print(f"Would push {len(rows)} file(s), {_human(total_bytes)}.")
        return 0 if not failed else 1


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
# people
# --------------------------------------------------------------------------- #
def cmd_people(args) -> int:
    with _catalog(args) as catalog:
        people = catalog.list_people()
        if args.json:
            _emit([dict(p) for p in people], True)
            return 0
        if not people:
            print("No people yet. Run: mediavault publish nas --commit (with FACES_LIVE=1)")
            return 0
        print(f"{'id':>4}  {'name':20} {'faces':>7} {'photos':>7}  sample item")
        for p in people:
            name = p["name"] or "(unnamed)"
            print(f"{p['id']:>4}  {name:20} {p['face_count']:>7} "
                  f"{p['item_count']:>7}  {p['sample_item_id']}")
        return 0


def cmd_people_rename(args) -> int:
    with _catalog(args) as catalog:
        catalog.set_person_name(args.person_id, args.name)
    print(f"Person {args.person_id} -> {args.name!r}")
    return 0


def cmd_people_reset(args) -> int:
    with _catalog(args) as catalog:
        n_faces = catalog.conn.execute("SELECT COUNT(*) FROM faces").fetchone()[0]
        n_people = catalog.conn.execute("SELECT COUNT(*) FROM people").fetchone()[0]

        if not args.commit:
            if args.json:
                _emit({"faces": n_faces, "people": n_people, "committed": False}, True)
            else:
                print(f"DRY-RUN (no change): would delete {n_faces:,} face row(s) and "
                      f"{n_people:,} person cluster(s).")
                print("Items keep published_at — re-run publish with --force and "
                      "FACES_LIVE=1 afterward to re-detect. Re-run with --commit to apply.")
            return 0

        result = catalog.reset_people()
        if args.json:
            _emit({**result, "committed": True}, True)
            return 0

        _banner(True)
        print(f"Deleted {result['faces_deleted']:,} face row(s), "
              f"{result['people_deleted']:,} person cluster(s).")
        print("Nothing else changed — items, scans, and published facts are untouched. "
              "Re-run publish with --force and FACES_LIVE=1 to re-detect.")
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
# process-intents
# --------------------------------------------------------------------------- #
def _process_intents_once(args, intents_store, catalog) -> tuple[int, int]:
    """One claim/run/write-back pass. Returns (done, failed)."""
    from . import sync as sync_mod

    ctx = sync_mod.AgentContext(
        connectors=_LazyConnectors(), blobs=_blobs_for(args),
        catalog=catalog, facts=_facts_for(args))

    claimed = intents_store.claim_pending(limit=args.limit)
    if not claimed:
        print("No pending intents.")
        return 0, 0

    log = ActionLog(args.log_dir or os.getenv("ACTION_LOG", "/data/catalog/actions"))
    done = failed = 0
    for raw in claimed:
        intent = sync_mod.Intent(**raw)
        print(f"{intent.type} {intent.item_id} ...", flush=True)
        try:
            result = log.record(sync_mod.handle(intent, ctx, commit=True))
        except Exception as e:
            failed += 1
            intents_store.fail(intent.id, {"error": f"{type(e).__name__}: {e}"})
            print(f"  crashed: {e}")
            continue
        if result.ok:
            done += 1
            intents_store.complete(intent.id, result.to_dict())
        else:
            failed += 1
            intents_store.fail(intent.id, result.to_dict())
        print(f"  {result.status}: {result.detail}")

    print(f"{done} done, {failed} failed.")
    return done, failed


def _stop_on_sigterm(signum, frame):
    """`docker stop` sends SIGTERM, not the SIGINT Ctrl+C sends — routed to
    the same KeyboardInterrupt process-intents --watch's own try/except
    already catches, so either one breaks the loop cleanly instead of
    waiting out Docker's stop grace period and getting SIGKILLed mid-poll."""
    raise KeyboardInterrupt


def cmd_process_intents(args) -> int:
    intents_store = _intents_for(args)

    if not args.commit and not args.watch:
        pending = intents_store.peek_pending(limit=args.limit)
        if not pending:
            print("No pending intents.")
            return 0
        print(f"{len(pending)} pending intent(s):")
        for raw in pending:
            print(f"  {raw['type']} {raw['item_id']} {raw.get('params') or ''}".rstrip())
        _banner(False)
        return 0

    with _catalog(args) as catalog:
        if not args.watch:
            _, failed = _process_intents_once(args, intents_store, catalog)
            return 0 if failed == 0 else 1

        # --watch: poll forever until stopped. Run in the foreground via
        # `docker exec -it ...` for Ctrl+C (SIGINT) -- without -it, SIGINT
        # never reaches the process inside the container and it keeps
        # polling as an orphan, the same trap a stray `dedup --commit` fell
        # into before. As the container's own CMD (see Dockerfile), it's
        # PID 1's child under tini, and `docker stop` sends SIGTERM instead
        # of SIGINT -- routed to the same KeyboardInterrupt handler below so
        # either signal breaks the loop cleanly rather than waiting out
        # Docker's stop grace period and getting SIGKILLed mid-poll.
        signal.signal(signal.SIGTERM, _stop_on_sigterm)
        print(f"Watching for intents every {args.interval}s. Ctrl+C to stop.")
        try:
            while True:
                stamp = datetime.now(timezone.utc).isoformat(timespec="seconds")
                print(f"\n[{stamp}] polling...", flush=True)
                _process_intents_once(args, intents_store, catalog)
                # Best-effort: a status-doc write failing (e.g. a transient
                # Firestore hiccup) shouldn't kill an otherwise-healthy loop.
                try:
                    still_pending = len(intents_store.peek_pending(limit=1000))
                    intents_store.heartbeat(still_pending)
                except Exception as e:
                    print(f"  (heartbeat failed: {e})")
                time.sleep(args.interval)
        except KeyboardInterrupt:
            print("\nStopped.")
            return 0


# --------------------------------------------------------------------------- #
# drive-login
# --------------------------------------------------------------------------- #
def cmd_drive_login(args) -> int:
    from .connectors.drive import SCOPES

    creds_path = args.credentials or os.getenv("DRIVE_CREDENTIALS", "/secrets/drive_credentials.json")
    token_path = args.token or os.getenv("DRIVE_TOKEN", "/secrets/drive_token.json")

    if not os.path.isfile(creds_path):
        print(f"No OAuth client file at {creds_path}.")
        print("Google Cloud Console -> APIs & Services -> Credentials -> "
              "Create OAuth client ID -> Desktop app -> download the JSON there.")
        return 1

    from google_auth_oauthlib.flow import InstalledAppFlow

    flow = InstalledAppFlow.from_client_secrets_file(creds_path, SCOPES)
    # bind_addr="0.0.0.0" so the callback server inside the container accepts
    # a connection forwarded in from a published port — the browser doing the
    # actual sign-in doesn't need to run on this machine, just reach
    # localhost:<port> after Google redirects it there. host stays "localhost"
    # (the default) since that's what ends up in the redirect_uri Google sees
    # — 0.0.0.0 there gets rejected with "Access blocked: Authorization Error
    # / Error 400: invalid_request", since it isn't a real loopback address.
    creds = flow.run_local_server(bind_addr="0.0.0.0", port=args.port, open_browser=False)

    with open(token_path, "w", encoding="utf-8") as f:
        f.write(creds.to_json())
    print(f"Saved Drive token to {token_path}.")
    print("Set DRIVE_LIVE=1 and re-run `doctor` to confirm.")
    return 0


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

        elif args.command == "restore":
            _require(args.target, "target id/path")
            res = conn.restore(args.target, commit=args.commit)
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
    except (FileNotFoundError, FileExistsError, ValueError) as e:
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
    dd.add_argument("--limit", type=int, default=20, help="groups to print in the preview")
    dd.add_argument("--max-groups", type=int, default=None,
                    help="ACTUALLY cap how many groups --commit archives in this run "
                         "(distinct from --limit, which only trims the printed preview) "
                         "— e.g. for archiving a large backlog in batches")
    dd.add_argument("--by-folder", action="store_true",
                    help="summarize reclaimable space by folder instead of "
                         "listing every group — read-only, ignores --commit")
    dd.add_argument("--depth", type=int, default=3,
                    help="path segments per folder bucket with --by-folder (default: 3)")
    dd.add_argument("--debug", action="store_true",
                    help="show live progress while confirming candidates — most files "
                         "exceed the quick-hash coverage window, so confirming a large "
                         "library means thousands of full-content reads with otherwise "
                         "zero output")
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
    pub.add_argument("--force", action="store_true",
                     help="also republish already-published items — for backfilling a "
                          "fact field added after they were first published (e.g. GPS), "
                          "without a full reset + re-index. Thumbnails are untouched "
                          "either way (content-addressed, already-there ones are skipped).")
    pub.add_argument("--mime-only", action="store_true",
                     help="only items with mime already set — for a library only "
                          "partially re-indexed since mime detection was added, so "
                          "--max-items targets what a fresh index pass has actually "
                          "reached instead of the oldest-indexed items with none.")
    pub.add_argument("--commit", action="store_true",
                     help="ACTUALLY generate and push (default: preview only)")
    pub.add_argument("--debug", action="store_true",
                     help="with FACES_LIVE=1: print each detected face's nearest-person "
                          "distance and match/no-match outcome — for seeing why "
                          "clustering did or didn't group two photos together.")
    pub.set_defaults(_fn=cmd_publish, permanent=False)

    # -- cold-archive --
    ca = sub.add_parser(
        "cold-archive",
        help="push every catalog item not yet in cold storage to a GCS Archive-class bucket",
        description="Backs up originals to a separate cold-storage bucket (COLD_STORAGE_BUCKET), "
                    "keyed by their NAS-relative path so the bucket stays browsable. NAS files "
                    "are left in place — this only adds an off-site copy, it doesn't free space. "
                    "Re-running only pushes what's new since the last run.")
    ca.add_argument("source", choices=CONNECTORS)
    ca.add_argument("--root", help="override the connector root")
    ca.add_argument("--trash", help="override the NAS trash folder")
    ca.add_argument("--db", help="catalog database path")
    ca.add_argument("--log-dir", help="where to write the action journal")
    ca.add_argument("--coldstore-dir", help="local cold-storage folder (ignored if GCS_LIVE=1)")
    ca.add_argument("--max-items", type=int, default=None,
                    help="push at most this many files — e.g. a small number "
                         "to try the pipeline out before a full run")
    ca.add_argument("--commit", action="store_true",
                    help="ACTUALLY upload (default: preview only)")
    ca.set_defaults(_fn=cmd_cold_archive, permanent=False)

    # -- stats --
    s = sub.add_parser("stats", help="what the catalog currently knows")
    s.add_argument("--db", help="catalog database path")
    s.set_defaults(_fn=cmd_stats)

    # -- people --
    pe = sub.add_parser(
        "people",
        help="list detected face clusters — local catalog only, no Firestore",
        description="Every person (face cluster) publish has found so far, with a "
                    "face/photo count and one sample item to eyeball. Needs "
                    "FACES_LIVE=1 during publish to have found anything at all. "
                    "Read-only, local-catalog only — for sanity-checking clustering "
                    "quality directly; the web 'People' tab reads person_ids off "
                    "published items instead, not this local catalog.")
    pe.add_argument("--db", help="catalog database path")
    pe.set_defaults(_fn=cmd_people)

    per = sub.add_parser(
        "people-rename",
        help="name a person (local catalog only, no Firestore)",
        description="Sets a person's name in the local catalog. Does not (yet) push "
                    "anything to Firestore — there's no people/ collection for the web "
                    "module to read a name from yet, this is local-only for now.")
    per.add_argument("person_id", type=int)
    per.add_argument("name")
    per.add_argument("--db", help="catalog database path")
    per.set_defaults(_fn=cmd_people_rename)

    prs = sub.add_parser(
        "people-reset",
        help="wipe all detected faces/people (local catalog only, never touches items)",
        description="Deletes every face and person cluster from the local catalog. "
                    "Items keep published_at, so a plain publish won't re-touch them "
                    "— re-run publish --force with FACES_LIVE=1 afterward to "
                    "re-detect. For recovering from a bad clustering run without a "
                    "full reset + re-index.")
    prs.add_argument("--db", help="catalog database path")
    prs.add_argument("--commit", action="store_true",
                     help="ACTUALLY delete (default: preview only)")
    prs.set_defaults(_fn=cmd_people_reset)

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

    # -- drive-login --
    dl = sub.add_parser(
        "drive-login",
        help="one-time interactive OAuth grant for the Drive connector",
        description="Starts a local OAuth callback server inside the container and "
                    "prints a URL. Open it in any browser (doesn't need to be on this "
                    "machine — the container's port just needs to be published), sign "
                    "in, and grant access. Saves a refreshable token so future runs "
                    "never prompt again. Needs an OAuth Desktop-app client JSON first "
                    "(Google Cloud Console -> APIs & Services -> Credentials).")
    dl.add_argument("--credentials", help="OAuth client JSON path (default DRIVE_CREDENTIALS "
                                          "or /secrets/drive_credentials.json)")
    dl.add_argument("--token", help="where to save the token (default DRIVE_TOKEN or "
                                    "/secrets/drive_token.json)")
    dl.add_argument("--port", type=int, default=8080,
                    help="local callback port — must match a published container port")
    dl.set_defaults(_fn=cmd_drive_login)

    # -- process-intents --
    pi = sub.add_parser(
        "process-intents",
        help="claim and run requests the web module has written",
        description="The agent's read side of the intents/ collection — the web "
                    "module can't touch files itself, it only writes a request "
                    "(e.g. 'stage this for Amazon') and this claims pending ones, "
                    "runs them through the same Action classes every other command "
                    "uses, and writes status/result back. Without --commit this "
                    "only lists what's pending — it doesn't claim or run anything, "
                    "same dry-run-by-default rule as everywhere else.")
    pi.add_argument("--db", help="catalog database path")
    pi.add_argument("--blob-dir", help="local thumbnail folder (ignored if GCS_LIVE=1)")
    pi.add_argument("--facts-dir", help="local facts folder (ignored if GCS_LIVE=1)")
    pi.add_argument("--intents-dir", help="local intents folder (ignored if GCS_LIVE=1)")
    pi.add_argument("--log-dir", help="where to write the action journal")
    pi.add_argument("--limit", type=int, default=10, help="claim at most this many intents")
    pi.add_argument("--commit", action="store_true",
                    help="ACTUALLY claim and run them (default: preview only)")
    pi.add_argument("--watch", action="store_true",
                    help="keep running, polling Firestore every --interval seconds "
                         "instead of a single pass (implies --commit; Ctrl+C to stop)")
    pi.add_argument("--interval", type=int, default=600,
                    help="seconds between polls in --watch mode (default 600 = 10 min)")
    pi.set_defaults(_fn=cmd_process_intents)

    # -- connectors --
    for name in CONNECTORS:
        c = sub.add_parser(name, help=f"run one {name} connector operation")
        c.add_argument("command", choices=CONNECTOR_COMMANDS)
        c.add_argument("target", nargs="?", default="",
                       help="item id / path, or local file for upload")
        c.add_argument("--root", help="connector root (NAS path, Drive folder ID, ...)")
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
