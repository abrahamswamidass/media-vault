"""
The Action contract — one file operation, made repeatable.

Pattern: **Command**. Each action is an object that knows how to check itself,
describe itself, and perform itself. Nothing in this project mutates a file
except through an Action, which is what makes every mutation testable, loggable,
and replayable.

Every action follows the same three beats:

    validate()  ->  is this safe to attempt?
    describe()  ->  what would it do?          (dry-run reads this)
    _execute()  ->  do it.                     (only reached with commit=True)

Concrete actions implement those three plus `inputs`. They do NOT implement
timing, error trapping, or result-building — `run()` below owns all of that, so
every action reports itself identically and no action can forget to.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from typing import Optional


# --------------------------------------------------------------------------- #
# Result envelope
# --------------------------------------------------------------------------- #
#: `status` is one of these three. "no-op" means the action was valid but there
#: was nothing to do (e.g. the preview was already in the cloud) — a success
#: that touched nothing, which is worth telling apart from a real change.
STATUS_OK = "ok"
STATUS_FAILED = "failed"
STATUS_NOOP = "no-op"


@dataclass
class ActionResult:
    """What was asked, what happened, and what changed — the unit of the audit log."""
    action_type: str                              # "delete" | "copy" | "fetch_fullres" | ...
    status: str                                   # STATUS_OK | STATUS_FAILED | STATUS_NOOP
    committed: bool                               # False => dry-run, nothing changed
    target_id: str                                # the item acted on
    detail: str                                   # human-readable summary
    duration_ms: float = 0.0
    inputs: dict = field(default_factory=dict)    # exact parameters (enough to replay)
    outputs: dict = field(default_factory=dict)   # what changed (paths, ids)
    error: Optional[str] = None                   # set iff status == STATUS_FAILED
    at: str = ""                                  # ISO-8601 UTC timestamp

    def __post_init__(self):
        if not self.at:
            self.at = datetime.now(timezone.utc).isoformat()

    @property
    def ok(self) -> bool:
        return self.status != STATUS_FAILED

    def to_dict(self) -> dict:
        return asdict(self)


class NoOp(Exception):
    """Raise from _execute() when there was legitimately nothing to do.

    Distinct from an error: the desired end state already holds. Idempotent
    actions raise this on the second run instead of doing the work twice.
    """


# --------------------------------------------------------------------------- #
# The Action contract
# --------------------------------------------------------------------------- #
class Action(ABC):
    action_type: str = "action"

    # --- implemented by each concrete action ------------------------------ #
    @property
    @abstractmethod
    def target_id(self) -> str:
        """The item this action acts on."""

    @property
    @abstractmethod
    def inputs(self) -> dict:
        """Every parameter needed to reconstruct this action later. Logged verbatim."""

    @abstractmethod
    def validate(self) -> tuple[bool, str]:
        """Check preconditions. Returns (is_safe, reason_if_not)."""

    @abstractmethod
    def describe(self) -> str:
        """One line stating what this action would do. Shown in dry-run."""

    @abstractmethod
    def _execute(self) -> dict:
        """Perform the operation and return the outputs dict.

        Raise any exception to fail the action; raise NoOp if the desired state
        already holds. Never called unless validate() passed and commit=True.
        """

    # --- the single entry point ------------------------------------------- #
    def run(self, commit: bool = False) -> ActionResult:
        """Validate, then either preview (default) or perform.

        This is the ONLY way to invoke an action. Dry-run is the default so a
        caller that forgets the flag previews instead of mutating.
        """
        is_safe, reason = self.validate()
        if not is_safe:
            return self._result(STATUS_FAILED, committed=False,
                                detail=f"validation failed: {reason}", error=reason)

        if not commit:
            return self._result(STATUS_OK, committed=False,
                                detail=f"DRY-RUN (no change): {self.describe()}")

        started = datetime.now()
        try:
            outputs = self._execute() or {}
        except NoOp as e:
            return self._result(STATUS_NOOP, committed=False,
                                detail=str(e) or "already in the desired state",
                                duration_ms=_elapsed_ms(started))
        except Exception as e:
            return self._result(STATUS_FAILED, committed=False,
                                detail=f"{type(e).__name__}: {e}", error=str(e),
                                duration_ms=_elapsed_ms(started))
        return self._result(STATUS_OK, committed=True, detail=self.describe(),
                            outputs=outputs, duration_ms=_elapsed_ms(started))

    # --- internal --------------------------------------------------------- #
    def _result(self, status: str, *, committed: bool, detail: str,
                outputs: dict | None = None, error: str | None = None,
                duration_ms: float = 0.0) -> ActionResult:
        return ActionResult(
            action_type=self.action_type,
            status=status,
            committed=committed,
            target_id=self.target_id,
            detail=detail,
            duration_ms=duration_ms,
            inputs=self.inputs,
            outputs=outputs or {},
            error=error,
        )


def _elapsed_ms(started: datetime) -> float:
    return (datetime.now() - started).total_seconds() * 1000
