"""
Face clustering — matching a newly detected face to an existing person, or
starting a new one.

Online/incremental: a person's "centroid" is just their first-ever detected
face's embedding, not a running average across every face they have. Simple
and deterministic, and needs no re-computation as faces are added. A full
pairwise re-cluster (comparing every face against every other) is a
plausible future improvement if this greedy nearest-match approach turns out
to fragment one real person into several near-duplicate people — not built
until that's an observed problem, not a hypothetical one.

Nothing here ever assigns a *name*. Clustering only decides which faces
belong together; naming a cluster is something a person does once, later,
through whatever picks that up (a CLI command today, a "People" web tab
eventually).
"""
from __future__ import annotations

import struct
from typing import Callable, Optional

from .store import Catalog

#: Euclidean distance below which two face embeddings are treated as the
#: same person. ArcFace-family embeddings (what insightface's buffalo_l
#: model produces) are L2-normalized 512-d vectors — this threshold is
#: conservative on purpose: a missed match (the same person split across two
#: people) is a one-time merge to fix later; a wrong match (two different
#: people fused into one) is much more confusing to notice and undo.
MATCH_THRESHOLD = 0.9


def _unpack(embedding: bytes) -> list[float]:
    n = len(embedding) // 4
    return list(struct.unpack(f"{n}f", embedding))


def _distance(a: list[float], b: list[float]) -> float:
    return sum((x - y) ** 2 for x, y in zip(a, b)) ** 0.5


def recluster(catalog: Catalog, *, threshold: float = MATCH_THRESHOLD,
              on_progress: Optional[Callable[[int, int], None]] = None) -> dict:
    """Re-cluster every stored face from scratch, matching a face against
    ANY existing member of a cluster it's being compared to -- not just
    that cluster's first-ever face, the way the online assign_person()
    above works. Fixes the failure mode that design leaves open: one
    unrepresentative first photo (bad angle, poor lighting) anchoring a
    person's centroid, silently rejecting every genuine later match
    against it even though they'd clearly match each other.

    Needs no re-detection -- only compares embeddings already sitting in
    the local `faces` table, so this is seconds even for thousands of
    faces, not the hours a `publish --force` re-run would cost. Purely
    advisory: returns the proposed assignment, doesn't touch the catalog
    itself (see Catalog.apply_recluster for that, gated behind --commit
    the same way every mutating command in this project is).

    Deterministic: faces are processed oldest-detected first (same order
    Catalog.all_faces_with_embeddings returns), so re-running at the same
    threshold always proposes the same clusters -- same reasoning as
    dedup's keeper selection being order-independent-but-reproducible.

    `on_progress(done, total)`, if given, fires after each face -- this is
    O(faces^2) (every face compared against every prior one), which is
    still fast at a personal-library scale (thousands, not millions) but
    isn't instant, so `people-recluster --debug` has something to show.
    """
    faces = catalog.all_faces_with_embeddings()
    cluster_vecs: list[list[list[float]]] = []
    assignments: dict[int, int] = {}
    for i, face in enumerate(faces):
        vec = _unpack(face["embedding"])
        best_idx, best_dist = None, None
        for idx, members in enumerate(cluster_vecs):
            for m in members:
                dist = _distance(vec, m)
                if best_dist is None or dist < best_dist:
                    best_idx, best_dist = idx, dist
        if best_dist is not None and best_dist <= threshold:
            cluster_vecs[best_idx].append(vec)
            assignments[face["id"]] = best_idx
        else:
            cluster_vecs.append([vec])
            assignments[face["id"]] = len(cluster_vecs) - 1
        if on_progress:
            on_progress(i + 1, len(faces))
    return {"face_count": len(faces), "cluster_count": len(cluster_vecs),
            "assignments": assignments}


def assign_person(catalog: Catalog, embedding: bytes,
                  on_match: Optional[Callable[[Optional[int], Optional[float], bool], None]] = None
                  ) -> int:
    """Return the id of the person `embedding` belongs to — the nearest
    existing person under MATCH_THRESHOLD, or a freshly created (unnamed)
    one if nothing is close enough.

    `on_match`, if given, is called with (nearest_person_id, nearest_dist,
    matched) before returning — `publish --debug` uses this to print the
    actual distance behind each decision, since "why didn't this match"
    can't be answered by re-reading the code, only by seeing real numbers.
    """
    vec = _unpack(embedding)
    best_id, best_dist = None, None
    for row in catalog.person_centroids():
        dist = _distance(vec, _unpack(row["embedding"]))
        if best_dist is None or dist < best_dist:
            best_id, best_dist = row["person_id"], dist
    matched = best_id is not None and best_dist <= MATCH_THRESHOLD
    if on_match:
        on_match(best_id, best_dist, matched)
    if matched:
        return best_id
    return catalog.add_person()
