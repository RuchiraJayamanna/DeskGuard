"""PresenceTracker tests with a fake face engine and synthetic frames (no models needed)."""

import numpy as np
import pytest

from deskguard.faces import Face, FaceProfile
from deskguard.presence import PresenceTracker, body_from_face

ME = np.zeros(128, np.float32)
ME[0] = 1.0
OTHER = np.zeros(128, np.float32)
OTHER[1] = 1.0
DT = 1 / 3  # tracker runs ~3 times per second


class FakeEngine:
    """embed() returns the vector stored in the face's row."""

    def __init__(self):
        self.embeds = 0

    def detect(self, frame):
        return []

    def embed(self, frame, face):
        self.embeds += 1
        return face.row


def face(box, vec):
    return Face(box, 0.95, vec)


ME_FACE = (0.62, 0.30, 0.14, 0.25)      # user sits on the right
STRANGER_FACE = (0.15, 0.20, 0.07, 0.12)  # someone smaller, further back on the left


@pytest.fixture
def tracker(tmp_path):
    profile = FaceProfile(tmp_path / "face.json")
    profile.vectors = [ME] * 3
    t = PresenceTracker(FakeEngine(), profile)
    t.configure(dwell=5, person_trigger=True, threshold=0.363, cooldown=60, sensitivity="medium")
    return t


def scene():
    f = np.full((360, 640, 3), 120, np.uint8)
    f[:, 300:340] = 90  # some structure
    return f


def run(tracker, seconds, faces_fn, frame_fn=lambda i: scene(), t0=0.0):
    states = []
    n = int(round(seconds / DT))
    for i in range(n):
        now = t0 + i * DT
        states.append(tracker.update(frame_fn(i), now, faces_fn(i)))
    return states


def test_stranger_face_alerts_once_after_dwell(tracker):
    states = run(tracker, 8, lambda i: [face(ME_FACE, ME), face(STRANGER_FACE, OTHER)])
    alerts = [i for i, s in enumerate(states) if s.alert]
    assert len(alerts) == 1
    first = states[alerts[0]]
    assert first.kind == "face" and 5.0 <= first.watch_seconds < 5.0 + 2 * DT + 0.01
    assert states[-1].strangers and states[-1].me_body is not None


def test_my_own_face_never_alerts(tracker):
    states = run(tracker, 20, lambda i: [face(ME_FACE, ME)])
    assert not any(s.alert or s.watching for s in states)
    assert states[-1].me_body == pytest.approx(body_from_face(ME_FACE))


def test_passer_by_does_not_alert(tracker):
    def faces(i):
        return [face(ME_FACE, ME)] + ([face(STRANGER_FACE, OTHER)] if i < 8 else [])  # ~2.7 s
    states = run(tracker, 12, faces)
    assert not any(s.alert for s in states)
    assert not states[-1].watching  # episode ended after the gap


def test_flickering_detection_still_alerts(tracker):
    def faces(i):
        out = [face(ME_FACE, ME)]
        if i % 3 != 2:  # stranger missed every third update
            out.append(face(STRANGER_FACE, OTHER))
        return out
    states = run(tracker, 8, faces)
    assert sum(s.alert for s in states) == 1


def test_cooldown_and_cleared(tracker):
    def faces(i):
        return [face(ME_FACE, ME)] + ([face(STRANGER_FACE, OTHER)] if i < 24 or 45 <= i < 75 else [])
    states = run(tracker, 30, faces)
    assert sum(s.alert for s in states) == 1   # second visit within the 60 s cooldown
    assert any(s.cleared for s in states)
    tracker.cooldown = 0
    states = run(tracker, 10, lambda i: [face(ME_FACE, ME), face(STRANGER_FACE, OTHER)], t0=100)
    assert sum(s.alert for s in states) == 1


def test_body_area_stays_when_face_disappears(tracker):
    run(tracker, 2, lambda i: [face(ME_FACE, ME)])
    states = run(tracker, 5, lambda i: [], t0=2)  # looking down: no face found
    assert states[-1].me_face is None
    assert states[-1].me_body == pytest.approx(body_from_face(ME_FACE))
    tracker.reset()
    assert tracker.me_body is None


def test_recognition_is_not_run_every_update(tracker):
    run(tracker, 10, lambda i: [face(ME_FACE, ME)])
    # 30 updates: 2 checks to confirm, then one every 3 s
    assert tracker.engine.embeds <= 6


def person_frames(x0, x1, start=6):
    """A dark person-sized shape appears at update `start` and then stands still."""
    def frame(i):
        f = scene()
        if i >= start:
            f[60:340, x0:x1] = 30
        return f
    return frame


def test_still_person_without_face_alerts(tracker):
    states = run(tracker, 14, lambda i: [face(ME_FACE, ME)] if i < 3 else [], person_frames(60, 160))
    alerts = [s for s in states if s.alert]
    assert len(alerts) == 1 and alerts[0].kind == "person"
    assert alerts[0].person is not None and alerts[0].person[0] < 0.3


def test_person_inside_my_body_area_is_ignored(tracker):
    body = body_from_face(ME_FACE)
    x0, x1 = int((body[0] + 0.02) * 640), int((body[0] + body[2] - 0.02) * 640)
    states = run(tracker, 14, lambda i: [face(ME_FACE, ME)] if i < 3 else [], person_frames(x0, x1))
    assert not any(s.alert or s.person for s in states)


def test_person_path_waits_until_user_located(tracker):
    states = run(tracker, 14, lambda i: [], person_frames(60, 160))  # user never seen
    assert not any(s.alert for s in states)


def test_lighting_change_does_not_alert(tracker):
    def frame(i):
        f = scene()
        return np.clip(f.astype(np.int16) + (80 if i >= 8 else 0), 0, 255).astype(np.uint8)
    states = run(tracker, 14, lambda i: [face(ME_FACE, ME)] if i < 3 else [], frame)
    assert not any(s.alert or s.person for s in states)


def test_person_trigger_can_be_turned_off(tracker):
    tracker.person_trigger = False
    states = run(tracker, 14, lambda i: [face(ME_FACE, ME)] if i < 3 else [], person_frames(60, 160))
    assert not any(s.alert for s in states)


def test_without_profile_largest_face_is_me(tmp_path):
    t = PresenceTracker(FakeEngine(), FaceProfile(tmp_path / "none.json"))
    t.configure(5, True, 0.363, 60, "medium")
    states = run(t, 7, lambda i: [face(ME_FACE, OTHER), face(STRANGER_FACE, OTHER)])
    assert states[-1].me_face == ME_FACE
    assert sum(s.alert for s in states) == 1 and states[-1].strangers == [STRANGER_FACE]
