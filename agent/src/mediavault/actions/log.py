"""
The action journal — append-only record of everything that happened.

One JSON object per line (JSONL), never rewritten. That format is deliberate:
it survives a crash mid-write losing at most the last line, it's greppable
without a parser, and it appends without reading what's already there.

The log is what turns "the agent did something" into "the agent did this, with
these inputs, at this time, and here is how to undo it".
"""
from __future__ import annotations

import json
from dataclasses import fields
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator, Optional

from .base import ActionResult, STATUS_FAILED

_FIELDS = {f.name for f in fields(ActionResult)}


class ActionLog:
    """Append-only JSONL journal of ActionResults."""

    def __init__(self, log_dir: str = "/data/catalog/actions"):
        self.log_dir = Path(log_dir).expanduser()
        self.log_dir.mkdir(parents=True, exist_ok=True)
        self.path = self.log_dir / "actions.jsonl"

    # --- write ------------------------------------------------------------ #
    def record(self, result: ActionResult) -> ActionResult:
        """Append one result. Returns it, so callers can log inline:

            result = log.record(action.run(commit=True))
        """
        with self.path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(result.to_dict(), default=str) + "\n")
        return result

    # --- read ------------------------------------------------------------- #
    def __iter__(self) -> Iterator[ActionResult]:
        """Every recorded result, oldest first. Streams — safe on a large log."""
        if not self.path.exists():
            return
        with self.path.open("r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    entry = json.loads(line)
                except json.JSONDecodeError:
                    continue          # a torn final line from a crash — skip it
                yield ActionResult(**{k: v for k, v in entry.items() if k in _FIELDS})

    def since(self, timestamp: str) -> list[ActionResult]:
        """Results recorded at or after an ISO-8601 timestamp."""
        return [r for r in self if r.at >= timestamp]

    def for_item(self, item_id: str) -> list[ActionResult]:
        """Everything that ever touched one item, oldest first."""
        return [r for r in self if r.target_id == item_id]

    def last_for(self, item_id: str) -> Optional[ActionResult]:
        """The most recent action on one item — the starting point for an undo."""
        history = self.for_item(item_id)
        return history[-1] if history else None

    def failures(self) -> list[ActionResult]:
        """Everything that went wrong. What you read after an unattended run."""
        return [r for r in self if r.status == STATUS_FAILED]

    def summary(self) -> dict:
        """Counts by action type and status — the one-line health check."""
        out: dict = {"total": 0, "by_type": {}, "by_status": {}}
        for r in self:
            out["total"] += 1
            out["by_type"][r.action_type] = out["by_type"].get(r.action_type, 0) + 1
            out["by_status"][r.status] = out["by_status"].get(r.status, 0) + 1
        return out
