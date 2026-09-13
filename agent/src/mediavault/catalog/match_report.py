"""
Cross-source match report — READ ONLY, and deliberately separate from dedup.py.

dedup.py's whole design (see its module docstring) is that NAS holding a file
Drive also has is the system working *correctly* — Drive is a curated cloud
copy, not a duplicate to clean up — so `duplicate_groups` is scoped to one
source and stays that way. This module answers a different, purely
informational question instead: "of what's on `source_a`, what does
`source_b` already have a copy of?" — for a person deciding by hand what
more to prune from Drive. Nothing here deletes, archives, or feeds any
action; it only reads.

A "match" is a shared `quick_hash` (size + head/tail fingerprint — the same
fingerprint dedup.py uses within one source, computed identically by every
connector's `stat()`). That's a strong signal, not a byte-exact guarantee —
same caveat CLAUDE.md gives quick_hash everywhere else it's used.

Only `source_a` gets a folder tree: its item_id is a real relative path
(true for "nas"; also true for "drive" once directory item_ids stop being
Drive's own opaque file id — see connectors/drive.py). A matched item on
`source_b` has no parent-folder chain recorded in the catalog, so it
surfaces as a name + size next to the `source_a` path it matched, not a
folder of its own.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from .store import Catalog


@dataclass
class TreeNode:
    name: str
    # Aggregate size of matched files under this node, source_a side.
    size: int = 0
    # Set only on a leaf (an actual matched file).
    match: Optional[dict] = None
    children: dict = field(default_factory=dict)

    def sorted_children(self) -> list["TreeNode"]:
        return sorted(self.children.values(), key=lambda n: n.size, reverse=True)


def find_matches(catalog: Catalog, source_a: str = "nas", source_b: str = "drive",
                  min_size: int = 1) -> list[dict]:
    """Every active `source_a` file whose quick_hash also appears, active, on
    `source_b`. `min_size` skips trivially small files, where a shared
    fingerprint is more likely coincidence than a real match (same rationale
    as `Catalog.duplicate_groups`)."""
    rows = catalog.conn.execute(
        """
        SELECT a.item_id AS a_path, a.size AS a_size,
               b.item_id AS b_id, b.name AS b_name, b.size AS b_size
        FROM items a
        JOIN items b ON a.quick_hash = b.quick_hash
        WHERE a.source = ? AND a.state = 'active'
          AND a.quick_hash IS NOT NULL AND a.size >= ?
          AND b.source = ? AND b.state = 'active'
        ORDER BY a.item_id
        """,
        (source_a, min_size, source_b),
    ).fetchall()
    return [dict(r) for r in rows]


def build_tree(matches: list[dict]) -> TreeNode:
    """Nests matches under a root by splitting each `a_path` on '/'. Item_ids
    are unique per source, so two matches can never land on the same leaf."""
    root = TreeNode(name="")
    for m in matches:
        parts = m["a_path"].split("/")
        node = root
        for part in parts[:-1]:
            node = node.children.setdefault(part, TreeNode(name=part))
        node.children[parts[-1]] = TreeNode(
            name=parts[-1], size=m["a_size"],
            match={"b_id": m["b_id"], "b_name": m["b_name"], "b_size": m["b_size"]},
        )
    _roll_up_sizes(root)
    return root


def _roll_up_sizes(node: TreeNode) -> int:
    if node.match is not None:
        return node.size
    node.size = sum(_roll_up_sizes(child) for child in node.children.values())
    return node.size


def _human(n: int) -> str:
    size = float(n)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if size < 1024 or unit == "TB":
            return f"{size:,.1f} {unit}"
        size /= 1024
    return f"{size:,.1f} TB"


def _count_leaves(node: TreeNode) -> int:
    if node.match is not None:
        return 1
    return sum(_count_leaves(c) for c in node.children.values())


def _sum_b_size(node: TreeNode) -> int:
    if node.match is not None:
        return node.match["b_size"]
    return sum(_sum_b_size(c) for c in node.children.values())


def render_tree(root: TreeNode, source_a: str = "nas", source_b: str = "drive") -> str:
    """Indented text, largest first at every level (files and subfolders sorted
    together by their own aggregate size) — a matched file's line pairs its
    `source_a` path with the `source_b` name/size it matched against."""
    lines = [
        f"{_count_leaves(root)} matched file(s): "
        f"{_human(root.size)} on {source_a} / {_human(_sum_b_size(root))} on {source_b}.",
        "",
    ]

    def walk(node: TreeNode, prefix: str):
        for child in node.sorted_children():
            if child.match is not None:
                m = child.match
                lines.append(
                    f"{prefix}{child.name:<40} {_human(child.size):>10}  <->  "
                    f"{m['b_name']:<40} {_human(m['b_size']):>10}"
                )
            else:
                lines.append(f"{prefix}{child.name}/{' ':<39}{_human(child.size):>10}")
                walk(child, prefix + "  ")

    walk(root, "")
    return "\n".join(lines)
