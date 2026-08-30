"""Actions — every mutation this agent can perform, as Command objects."""
from .base import Action, ActionResult, NoOp, STATUS_OK, STATUS_FAILED, STATUS_NOOP
from .file_ops import CopyAction, DeleteAction, MoveAction, RestoreAction
from .derive import FetchFullResAction, ThumbnailAction
from .dedup import ArchiveDuplicatesAction
from .maintenance import DedupSourceAction, IndexAction, PublishAction
from .amazon import StageForAmazonAction
from .log import ActionLog

__all__ = [
    "Action", "ActionResult", "NoOp",
    "STATUS_OK", "STATUS_FAILED", "STATUS_NOOP",
    "CopyAction", "DeleteAction", "MoveAction", "RestoreAction",
    "FetchFullResAction", "ThumbnailAction",
    "ArchiveDuplicatesAction", "DedupSourceAction", "IndexAction", "PublishAction",
    "StageForAmazonAction",
    "ActionLog",
]
