"""The local catalog — a SQLite index that makes the agent answerable offline."""
from .store import Catalog
from .scanner import ScanReport, scan, walk_directories
from .dedup import DuplicateGroup, find_duplicates, folder_breakdown, summarize
from .people import assign_person

__all__ = [
    "Catalog",
    "scan", "walk_directories", "ScanReport",
    "find_duplicates", "summarize", "folder_breakdown", "DuplicateGroup",
    "assign_person",
]
