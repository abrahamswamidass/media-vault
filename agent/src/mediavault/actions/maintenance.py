"""
Library-wide actions — the two that operate on a whole source rather than one file.

    IndexAction        walk a source into the catalog. Resumable.
    DedupSourceAction  find identical copies within a source and archive the extras.

Both are Actions rather than loose functions so they inherit the same dry-run gate
and the same journal entry as everything else. A dedup run triggered from the web
module leaves exactly the record a dedup run typed at the terminal does.
"""
from __future__ import annotations

from typing import Optional

from ..catalog import dedup as dedup_mod, scanner
from ..catalog.store import Catalog
from ..ports import Connector
from .base import Action, NoOp
from .dedup import ArchiveDuplicatesAction


class IndexAction(Action):
    """Walk one source into the catalog, resuming any interrupted pass."""
    action_type = "index"

    def __init__(self, source: str, connector: Connector, catalog: Catalog,
                 restart: bool = False):
        self.source = source
        self.connector = connector
        self.catalog = catalog
        self.restart = restart

    @property
    def target_id(self) -> str:
        return self.source

    @property
    def inputs(self) -> dict:
        return {"source": self.source, "connector": self.connector.name,
                "restart": self.restart}

    def validate(self) -> tuple[bool, str]:
        try:
            next(iter(self.connector.list("", limit=1)), None)
        except (FileNotFoundError, ValueError, PermissionError) as e:
            return False, f"cannot read {self.connector.name}: {e}"
        return True, ""

    def describe(self) -> str:
        state = self.catalog.scan_state(self.source)
        if state and not state["complete"] and not self.restart:
            return (f"resume indexing {self.source} from {state['cursor'] or 'the start'} "
                    f"({state['items_seen']:,} files already recorded)")
        return f"index {self.source} from the beginning"

    def _execute(self) -> dict:
        report = scanner.scan(self.connector, self.catalog, source=self.source,
                              resume=not self.restart)
        return {
            "files_indexed": report.files_indexed,
            "directories": report.directories,
            "errors": report.errors,
            "resumed_from": report.resumed_from,
            "error_samples": report.error_samples,
        }


class DedupSourceAction(Action):
    """Archive every confirmed duplicate within one source.

    Composes `ArchiveDuplicatesAction` once per group, so each group is validated
    independently and one bad group cannot take the rest down with it.

    Never compares across sources. The NAS holding everything and Drive holding a
    curated copy of the good things means cross-source overlap is correct, and the
    grouping query is scoped to one source precisely so that overlap is unreachable.
    """
    action_type = "dedup_source"

    def __init__(self, source: str, connector: Connector, catalog: Catalog,
                 *, confirm: bool = True, min_size: int = 1,
                 max_groups: Optional[int] = None):
        self.source = source
        self.connector = connector
        self.catalog = catalog
        self.confirm = confirm
        self.min_size = min_size
        self.max_groups = max_groups
        self._groups = None

    @property
    def target_id(self) -> str:
        return self.source

    @property
    def inputs(self) -> dict:
        return {"source": self.source, "connector": self.connector.name,
                "confirm": self.confirm, "min_size": self.min_size,
                "max_groups": self.max_groups}

    def validate(self) -> tuple[bool, str]:
        if not self.connector.can_delete:
            return False, f"{self.connector.name} cannot archive files"
        if self.catalog.count(self.source) == 0:
            return False, f"nothing indexed for {self.source} — run an index first"

        groups = dedup_mod.find_duplicates(
            self.catalog, self.source, self.connector,
            confirm=self.confirm, min_size=self.min_size)
        self._groups = [g for g in groups if g.safe_to_archive]
        if self.max_groups is not None:
            self._groups = self._groups[: self.max_groups]
        return True, ""

    def describe(self) -> str:
        groups = self._groups or []
        copies = sum(len(g.losers) for g in groups)
        freed = sum(g.reclaimable_bytes for g in groups)
        if not groups:
            return f"no confirmed duplicates to archive in {self.source}"
        return (f"archive {copies} redundant "
                f"{'copy' if copies == 1 else 'copies'} across {len(groups)} "
                f"group(s) in {self.source}, reclaiming {freed:,} bytes "
                f"(one copy of each is always kept)")

    def _execute(self) -> dict:
        if not self._groups:
            raise NoOp(f"no confirmed duplicates in {self.source}")

        archived, failed, freed = [], [], 0
        for group in self._groups:
            result = ArchiveDuplicatesAction(
                group, self.connector, self.catalog).run(commit=True)
            if result.status == "ok":
                archived.append({"kept": result.outputs.get("kept"),
                                 "archived": len(result.outputs.get("archived", []))})
                freed += result.outputs.get("bytes_reclaimed", 0)
            else:
                failed.append({"kept": group.keeper["item_id"], "error": result.error})

        if not archived:
            raise NoOp("no groups could be archived")
        return {"groups_archived": len(archived), "bytes_reclaimed": freed,
                "failed": failed, "detail": archived}
