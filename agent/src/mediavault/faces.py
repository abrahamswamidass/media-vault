"""
Face detection + embedding — the one place that runs the face model.

Optional extra like imaging.py's Pillow and metadata.py's PyExifTool:
insightface (ONNX-based — no C++ toolchain needed in the Docker image, unlike
dlib-based alternatives like `face_recognition`) is imported lazily, so the
core harness stays runnable without it.

"Recognize people" here means face *clustering*, not identification —
this module only detects faces and produces an embedding per face. Matching
an embedding to a person (or starting a new person) is catalog/people.py's
job, not this file's; naming a person is a person's job, not this project's.

Gated behind FACES_LIVE=1 (see PublishAction), off by default like every
other live switch in this project — the first real run needs to download
model weights (a few hundred MB, cached under ~/.insightface after that) and
costs real CPU time per image, not something that should turn on silently.
"""
from __future__ import annotations

import io

#: ArcFace embeddings from insightface's buffalo_l model pack — the
#: general-purpose recommended pack, balances accuracy and speed on CPU
#: (this project never assumes a GPU is available).
_MODEL_NAME = "buffalo_l"


class FacesUnavailable(RuntimeError):
    """insightface isn't installed, so this build can't detect faces."""


_app = None


def _face_app():
    global _app
    try:
        from insightface.app import FaceAnalysis  # noqa: PLC0415 — optional dependency, imported on use
    except ImportError as e:  # pragma: no cover - depends on install profile
        raise FacesUnavailable(
            "insightface is required to detect faces. "
            "pip install insightface onnxruntime (already listed in requirements.txt)."
        ) from e
    if _app is None:
        _app = FaceAnalysis(name=_MODEL_NAME, providers=["CPUExecutionProvider"])
        _app.prepare(ctx_id=-1, det_size=(640, 640))
    return _app


def detect_faces(data: bytes) -> list[dict]:
    """Detect every face in an image. Returns a list of
    {bbox: (x1, y1, x2, y2), embedding: bytes, score: float} — bbox in pixel
    coordinates of the image as given; embedding is a raw float32 buffer
    (catalog/people.py unpacks it), never a numpy array, so nothing outside
    this module needs numpy just to pass an embedding around.

    Returns [] on any failure or absence rather than raising — most photos
    have zero or a few faces, and a decode failure (unsupported format, a
    corrupt file, this being a video frame the caller sent by mistake) is an
    expected outcome here, not an error worth surfacing to the caller. Only
    ever called on bytes already known to decode as an image — the same
    bytes ThumbnailAction just used.
    """
    # _face_app() first: it's what raises FacesUnavailable if insightface
    # itself isn't installed. numpy/Pillow are real transitive dependencies
    # of insightface either way, but importing them before that check would
    # make "insightface isn't installed" fail on a numpy ImportError instead
    # — a confusing error pointing at the wrong missing package.
    app = _face_app()
    from PIL import Image
    import numpy as np

    try:
        with Image.open(io.BytesIO(data)) as im:
            bgr = np.array(im.convert("RGB"))[:, :, ::-1]
    except Exception:
        return []

    try:
        detected = app.get(bgr)
    except Exception:
        return []

    return [
        {
            "bbox": tuple(float(v) for v in f.bbox),
            "embedding": np.asarray(f.embedding, dtype="float32").tobytes(),
            "score": float(f.det_score),
        }
        for f in detected
    ]
