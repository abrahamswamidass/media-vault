"""
Face detection tests. The real `insightface` package (and its ONNX model
weights, downloaded on first real use) isn't available in CI or this dev
sandbox, so these inject a fake `insightface` module — only the field-mapping
logic in faces.py is under test here, not real face detection accuracy
(that's exercised manually against the Docker image, which does have it).
"""
from __future__ import annotations

import io
import struct
import sys
import types

import pytest

PIL = pytest.importorskip("PIL", reason="faces.py needs Pillow to decode the image")
from PIL import Image  # noqa: E402

from mediavault import faces  # noqa: E402


def _jpeg_bytes(size=(100, 100)) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", size, color=(10, 20, 30)).save(buf, format="JPEG")
    return buf.getvalue()


class _FakeFace:
    def __init__(self, bbox, embedding, score):
        self.bbox = bbox
        self.embedding = embedding
        self.det_score = score


@pytest.fixture
def fake_insightface(monkeypatch):
    """Injects a fake `insightface.app.FaceAnalysis` and resets faces.py's
    cached model singleton, so each test starts clean."""
    state = {"faces": [], "prepared": None}

    class FakeFaceAnalysis:
        def __init__(self, name=None, providers=None):
            state["name"] = name
            state["providers"] = providers

        def prepare(self, ctx_id, det_size):
            state["prepared"] = (ctx_id, det_size)

        def get(self, img):
            state["last_img_shape"] = img.shape
            return state["faces"]

    fake_app_module = types.SimpleNamespace(FaceAnalysis=FakeFaceAnalysis)
    fake_module = types.SimpleNamespace(app=fake_app_module)
    monkeypatch.setitem(sys.modules, "insightface", fake_module)
    monkeypatch.setitem(sys.modules, "insightface.app", fake_app_module)
    monkeypatch.setattr(faces, "_app", None)
    return state


def test_detect_faces_maps_bbox_score_and_embedding(fake_insightface):
    import numpy as np
    embedding = np.array([0.1, 0.2, 0.3], dtype="float32")
    fake_insightface["faces"] = [_FakeFace((10.0, 20.0, 30.0, 40.0), embedding, 0.87)]

    result = faces.detect_faces(_jpeg_bytes())

    assert len(result) == 1
    assert result[0]["bbox"] == (10.0, 20.0, 30.0, 40.0)
    assert result[0]["score"] == pytest.approx(0.87)
    assert struct.unpack("3f", result[0]["embedding"]) == pytest.approx((0.1, 0.2, 0.3))


def test_detect_faces_returns_empty_list_when_no_faces_found(fake_insightface):
    fake_insightface["faces"] = []

    assert faces.detect_faces(_jpeg_bytes()) == []


def test_detect_faces_handles_undecodable_bytes(fake_insightface):
    assert faces.detect_faces(b"not an image") == []


def test_detect_faces_uses_cpu_provider_and_buffalo_l(fake_insightface):
    faces.detect_faces(_jpeg_bytes())

    assert fake_insightface["name"] == "buffalo_l"
    assert fake_insightface["providers"] == ["CPUExecutionProvider"]
    assert fake_insightface["prepared"][0] == -1  # ctx_id=-1: no GPU assumed


def test_missing_insightface_raises_facesunavailable(monkeypatch):
    monkeypatch.setitem(sys.modules, "insightface", None)
    monkeypatch.setattr(faces, "_app", None)

    with pytest.raises(faces.FacesUnavailable):
        faces.detect_faces(_jpeg_bytes())
