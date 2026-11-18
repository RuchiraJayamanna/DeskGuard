import json

import numpy as np
import pytest

from deskguard.faces import STRICTNESS, Enroller, Face, FaceEngine, FaceProfile
from deskguard.resources import models_available


def unit(i, j=None, mix=0.0):
    v = np.zeros(128, np.float32)
    v[i] = 1.0
    if j is not None:
        v[j] = mix
    return v / np.linalg.norm(v)


def test_profile_round_trip_and_forget(tmp_path):
    p = FaceProfile(tmp_path / "face.json")
    assert not p.load() and not p.enrolled
    vecs = [unit(0), unit(0, 1, 0.3), unit(0, 2, 0.3)]
    assert p.save(vecs)
    data = json.loads((tmp_path / "face.json").read_text())
    assert set(data) == {"version", "model", "note", "vectors"}  # numbers only, no image
    q = FaceProfile(tmp_path / "face.json")
    assert q.load() and q.enrolled
    assert q.best_score(unit(0)) == pytest.approx(1.0, abs=1e-5)
    q.forget()
    assert not q.enrolled and not (tmp_path / "face.json").exists()


def test_profile_rejects_bad_files(tmp_path):
    path = tmp_path / "face.json"
    path.write_text("{oops", encoding="utf-8")
    assert not FaceProfile(path).load()
    path.write_text(json.dumps({"model": "other", "vectors": [[0.1] * 128] * 3}), encoding="utf-8")
    assert not FaceProfile(path).load()
    path.write_text(json.dumps({"model": "sface_2021dec_int8", "vectors": [[0.1] * 5] * 3}), encoding="utf-8")
    assert not FaceProfile(path).load()


def test_is_me_thresholds(tmp_path):
    p = FaceProfile(tmp_path / "face.json")
    p.vectors = [unit(0)] * 3
    similar = unit(0, 1, 1.5)  # cosine ~0.55
    assert p.is_me(similar, STRICTNESS["strict"])
    assert not p.is_me(unit(1), STRICTNESS["relaxed"])
    assert STRICTNESS["relaxed"] < STRICTNESS["normal"] < STRICTNESS["strict"]


class FakeEngine:
    def __init__(self):
        self.n = 0

    def embed(self, frame, face):
        self.n += 1
        return unit(0, self.n % 100 + 1, 0.5)  # each sample slightly different


FRAME = np.zeros((360, 640, 3), np.uint8)


def f(box=(0.4, 0.3, 0.2, 0.35), score=0.97):
    return Face(box, score, np.zeros(15, np.float32))


def test_enroller_collects_varied_samples():
    e = Enroller(FakeEngine())
    st = None
    t = 0.0
    while st is None or not st.done:
        st = e.update(FRAME, t, [f()])
        t += 0.25
        assert t < 10
    assert len(e.samples) == Enroller.TARGET and st.progress == 1.0


@pytest.mark.parametrize("faces,hint", [
    ([], "Look at the camera"),
    ([f(), f((0.1, 0.1, 0.1, 0.1))], "Only one face"),
    ([f((0.45, 0.4, 0.05, 0.08))], "closer"),
    ([f((0.85, 0.3, 0.14, 0.2))], "centre"),
    ([f(score=0.8)], "Hold still"),
])
def test_enroller_rejects_bad_samples(faces, hint):
    e = Enroller(FakeEngine())
    st = e.update(FRAME, 0.0, faces)
    assert hint in st.hint and not e.samples and not st.done


def test_enroller_times_out():
    e = Enroller(FakeEngine())
    e.update(FRAME, 0.0, [])
    assert e.update(FRAME, Enroller.TIMEOUT_S + 1, []).failed


@pytest.mark.skipif(not models_available(), reason="face models not present")
def test_real_models_load_detect_unload():
    eng = FaceEngine()
    eng.load()
    assert eng.loaded
    assert eng.detect(np.full((360, 640, 3), 128, np.uint8)) == []
    fake = Face((0.4, 0.3, 0.15, 0.25), 0.9,
                np.array([256, 108, 96, 90, 280, 140, 320, 140, 300, 160, 285, 180, 315, 180, 0.9], np.float32))
    vec = eng.embed(np.random.default_rng(0).integers(0, 255, (360, 640, 3), dtype=np.uint8), fake)
    assert vec.shape == (128,) and np.linalg.norm(vec) == pytest.approx(1.0, abs=1e-4)
    eng.unload()
    assert not eng.loaded and eng.detect(np.zeros((10, 10, 3), np.uint8)) == []
