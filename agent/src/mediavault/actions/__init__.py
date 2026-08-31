"""Actions — every mutation this agent can perform, as Command objects."""
from .base import Action, ActionResult, NoOp, STATUS_OK, STATUS_FAILED, STATUS_NOOP
from .file_ops import ArchiveItemAction, CopyAction, DeleteAction, MoveAction, RestoreAction
from .derive import FetchFullResAction, ThumbnailAction
from .dedup import ArchiveDuplicatesAction
from .maintenance import DedupSourceAction, IndexAction, PublishAction
from .amazon import StageForAmazonAction
from .coldstorage import ColdArchiveAction
from .log import ActionLog

__all__ = [
    "Action", "ActionResult", "NoOp",
    "STATUS_OK", "STATUS_FAILED", "STATUS_NOOP",
    "ArchiveItemAction", "CopyAction", "DeleteAction", "MoveAction", "RestoreAction",
    "FetchFullResAction", "ThumbnailAction",
    "ArchiveDuplicatesAction", "DedupSourceAction", "IndexAction", "PublishAction",
    "StageForAmazonAction", "ColdArchiveAction",
    "ActionLog",
]
