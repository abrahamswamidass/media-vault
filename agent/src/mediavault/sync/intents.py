"""
Intents — how the web module asks the agent to do something.

The web module cannot touch your files. It can only write an *intent*: a small
document saying "I would like this to happen". The agent picks it up, turns it
into an Action, runs it, and writes the result back. This is the **Transactional
Outbox / polling relay** shape, and it's what keeps a single writer on each side:

    web  ──writes──>  intents/{id}  ──read──>  agent
    web  <──result──  intents/{id}  <─writes─  agent

An intent moves through four states and never goes backwards:

    pending ──claim──> claimed ──run──> done
                          └────────────> failed

`REGISTRY` below is the whole surface area the web module gets. If an intent type
isn't in that table, the agent refuses it. Adding a capability to the UI means
adding a line here — deliberately, and in one place.
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from typing import Callable, Optional

from ..actions.amazon import StageForAmazonAction
from ..actions.base import Action, ActionResult
from ..actions.derive import FetchFullResAction, ThumbnailAction
from ..actions.file_ops import CopyAction, DeleteAction, MoveAction
from ..actions.maintenance import DedupSourceAction, IndexAction, PublishAction

# --------------------------------------------------------------------------- #
# States
# --------------------------------------------------------------------------- #
PENDING = "pending"
CLAIMED = "claimed"
DONE = "done"
FAILED = "failed"

#: An intent claimed longer ago than this is assumed abandoned (agent crashed
#: mid-run) and may be reclaimed. Generous, because a full-res read of a large
#: file over a slow mount is legitimately slow.
CLAIM_LEASE_SECONDS = 600


@dataclass
class Intent:
    """One request from the web module. The wire format between the two modules."""
    type: str                                   # key into REGISTRY
    item_id: str                                # what to act on
    params: dict = field(default_factory=dict)  # type-specific extras (e.g. variant)
    id: str = ""
    status: str = PENDING
    created_at: str = ""
    claimed_at: Optional[str] = None
    result: Optional[dict] = None               # the ActionResult, once it ran

    def __post_init__(self):
        if not self.id:
            self.id = uuid.uuid4().hex
        if not self.created_at:
            self.created_at = datetime.now(timezone.utc).isoformat()

    def to_dict(self) -> dict:
        return asdict(self)


# --------------------------------------------------------------------------- #
# What the web module is allowed to ask for
# --------------------------------------------------------------------------- #
#: intent type -> (builder, human description)
#:
#: Each builder takes (intent, context) and returns an Action. `context` carries
#: the things only the agent has: live connectors and a blob store.
REGISTRY: dict[str, tuple[Callable[[Intent, "AgentContext"], Action], str]] = {
    "fetch_fullres": (
        lambda i, ctx: FetchFullResAction(
            i.item_id, ctx.connector(i.params.get("source", "nas")), ctx.blobs,
            variant=i.params.get("variant", "preview"),
        ),
        "put a viewable copy of one item in the cloud for a day",
    ),
    "thumbnail": (
        lambda i, ctx: ThumbnailAction(
            i.item_id, ctx.connector(i.params.get("source", "nas")), ctx.blobs,
            force=bool(i.params.get("force", False)),
        ),
        "derive and push one thumbnail",
    ),
    "delete": (
        lambda i, ctx: DeleteAction(
            i.item_id, ctx.connector(i.params.get("source", "nas")),
        ),
        "soft-delete one item (moves to trash)",
    ),
    "copy": (
        lambda i, ctx: CopyAction(
            i.item_id, ctx.connector(i.params.get("source", "nas")),
            ctx.connector(i.params["dest"]), dest_path=i.params.get("dest_path", ""),
        ),
        "copy one item to another connector",
    ),
    "move": (
        lambda i, ctx: MoveAction(
            i.item_id, ctx.connector(i.params.get("source", "nas")),
            ctx.connector(i.params["dest"]), dest_path=i.params.get("dest_path", ""),
        ),
        "copy one item to another connector, then trash the original",
    ),

    # --- library-wide. item_id carries the source name, not a path. -------- #
    "index": (
        lambda i, ctx: IndexAction(
            i.item_id, ctx.connector(i.item_id), ctx.require_catalog(),
            restart=bool(i.params.get("restart", False)),
        ),
        "walk one source into the catalog, resuming any interrupted pass",
    ),
    "dedup_source": (
        lambda i, ctx: DedupSourceAction(
            i.item_id, ctx.connector(i.item_id), ctx.require_catalog(),
            confirm=bool(i.params.get("confirm", True)),
            min_size=int(i.params.get("min_size", 1)),
            max_groups=i.params.get("max_groups"),
        ),
        "archive identical copies WITHIN one source — never across sources",
    ),
    "publish": (
        lambda i, ctx: PublishAction(
            i.item_id, ctx.connector(i.item_id), ctx.require_catalog(),
            ctx.blobs, ctx.require_facts(),
            max_items=i.params.get("max_items"),
        ),
        "push a thumbnail + metadata fact for every item not yet published",
    ),
    "stage_for_amazon": (
        lambda i, ctx: StageForAmazonAction(
            i.item_id, ctx.connector(i.params.get("source", "nas")), ctx.connector("amazon"),
        ),
        "copy an item into Amazon's dated staging folder, no local file needed",
    ),
}


class UnknownIntent(Exception):
    """The web module asked for something not in REGISTRY."""


@dataclass
class AgentContext:
    """The capabilities an intent builder is allowed to reach for."""
    connectors: dict            # name -> Connector
    blobs: object               # BlobStore
    catalog: object = None      # Catalog, for the library-wide intents
    facts: object = None        # FactsStore, for the "publish" intent

    def connector(self, name: str):
        if name not in self.connectors:
            raise UnknownIntent(f"no connector named {name!r}")
        return self.connectors[name]

    def require_catalog(self):
        if self.catalog is None:
            raise UnknownIntent(
                "this intent needs the catalog, but no catalog was configured")
        return self.catalog

    def require_facts(self):
        if self.facts is None:
            raise UnknownIntent(
                "this intent needs a facts store, but none was configured")
        return self.facts


# --------------------------------------------------------------------------- #
# Dispatch
# --------------------------------------------------------------------------- #
def build_action(intent: Intent, ctx: AgentContext) -> Action:
    """Turn an intent into the Action that satisfies it."""
    if intent.type not in REGISTRY:
        raise UnknownIntent(
            f"unknown intent type {intent.type!r} — known: {sorted(REGISTRY)}"
        )
    builder, _ = REGISTRY[intent.type]
    return builder(intent, ctx)


def handle(intent: Intent, ctx: AgentContext, commit: bool = False) -> ActionResult:
    """Build the action for an intent and run it.

    Dry-run by default, matching every other entry point in this project: an
    agent started without `--commit` shows you a full run's worth of intended
    changes and performs none of them.
    """
    return build_action(intent, ctx).run(commit=commit)


def capabilities() -> dict[str, str]:
    """What this agent build can do — published so the UI can grey out the rest."""
    return {name: desc for name, (_, desc) in REGISTRY.items()}
