"""Body outlines: the user's own shape is ignored, other people are outlined."""

import numpy as np
import pytest

from deskguard.faces import Face, FaceProfile
from deskguard.motion import MotionDetector
from deskguard.presence import PresenceTracker
from deskguard.resources import SEG_MODEL, models_available
from deskguard.segmentation import PersonSegmenter, mask_box, outline
from tests.test_presence import DT, ME, ME_FACE, OTHER, STRANGER_FACE, FakeEngine, face

W, H = 160, 120


class FakeSegmenter:
    """Returns the mask produced by `self.fn(update_index)`."""

    loaded = True

    def __init__(self, fn):
        self.fn, self.i = fn, 0

    def segment(self, frame):
        m = self.fn(self.i)
        self.i += 1
        return m


def me_blob(mask):
    # user: head at ME_FACE (x 0.62..0.76, y 0.30..0.55) and shoulders down to the bottom
    mask[int(0.28 * H):int(0.58 * H), int(0.60 * W):int(0.78 * W)] = 255
    mask[int(0.55 * H):, int(0.48 * W):int(0.92 * W)] = 255
    return mask


def other_blob(mask, x0=0.10, x1=0.24):
    mask[int(0.15 * H):int(0.95 * H), int(x0 * W):int(x1 * W)] = 255
    return mask


def make_tracker(tmp_path, fn):
    profile = FaceProfile(tmp_path / "face.json")
    profile.vectors = [ME] * 3
    t = PresenceTracker(FakeEngine(), profile, segmenter=FakeSegmenter(fn))
    t.configure(dwell=5, person_trigger=True, threshold=0.363, cooldown=60, sensitivity="medium")
    return t


def run(t, seconds, faces_fn):
    return [t.update(np.zeros((360, 640, 3), np.uint8), i * DT, faces_fn(i)) for i in range(int(seconds / DT))]


def test_user_outline_follows_body_not_rectangle(tmp_path):
    t = make_tracker(tmp_path, lambda i: me_blob(np.zeros((H, W), np.uint8)))
    states = run(t, 2, lambda i: [face(ME_FACE, ME)])
    st = states[-1]
    assert st.me_mask is not None and len(st.me_outline) >= 1 and len(st.me_outline[0]) >= 4
    # the body is wider at the shoulders than at the head: a shape, not a box
    xs_head = np.nonzero(st.me_mask[int(0.4 * H)])[0]
    xs_body = np.nonzero(st.me_mask[int(0.9 * H)])[0]
    assert xs_body.size > xs_head.size * 1.5
    assert not any(s.alert or s.person for s in states)


def test_user_outline_kept_when_face_disappears(tmp_path):
    t = make_tracker(tmp_path, lambda i: me_blob(np.zeros((H, W), np.uint8)))
    run(t, 2, lambda i: [face(ME_FACE, ME)])
    states = run(t, 3, lambda i: [])  # looking down
    assert states[-1].me_mask is not None and states[-1].me_face is None
    assert not any(s.person for s in states)


def test_other_person_outline_alerts_even_without_face(tmp_path):
    t = make_tracker(tmp_path, lambda i: other_blob(me_blob(np.zeros((H, W), np.uint8))) if i >= 3
                     else me_blob(np.zeros((H, W), np.uint8)))
    states = run(t, 9, lambda i: [face(ME_FACE, ME)])
    alerts = [s for s in states if s.alert]
    assert len(alerts) == 1 and alerts[0].kind == "person"
    assert alerts[0].person[0] < 0.3 and len(alerts[0].people_outlines) == 1


def test_person_standing_right_behind_is_split_from_user(tmp_path):
    def mask(i):
        m = me_blob(np.zeros((H, W), np.uint8))
        if i >= 3:  # someone standing behind: head and torso above the user's head, touching them
            m[0:int(0.30 * H), int(0.58 * W):int(0.80 * W)] = 255
        return m
    t = make_tracker(tmp_path, mask)
    t.sensitivity = "high"
    states = run(t, 9, lambda i: [face(ME_FACE, ME)])
    assert any(s.alert and s.kind == "person" for s in states)


def test_stranger_face_still_wins(tmp_path):
    t = make_tracker(tmp_path, lambda i: other_blob(me_blob(np.zeros((H, W), np.uint8))))
    states = run(t, 7, lambda i: [face(ME_FACE, ME), face(STRANGER_FACE, OTHER)])
    assert [s.kind for s in states if s.alert] == ["face"]


def test_people_ignored_inside_zones(tmp_path):
    t = make_tracker(tmp_path, lambda i: other_blob(me_blob(np.zeros((H, W), np.uint8))))
    t.set_ignore_zones([[0.0, 0.0, 0.3, 1.0]])
    states = run(t, 9, lambda i: [face(ME_FACE, ME)])
    assert not any(s.alert for s in states)


def test_outline_and_box_helpers():
    m = np.zeros((H, W), np.uint8)
    assert outline(m) == [] and mask_box(m) is None
    m[30:60, 40:80] = 255
    pts = outline(m)
    assert len(pts) >= 4 and all(0 <= x <= 1 and 0 <= y <= 1 for x, y in pts)
    assert mask_box(m) == pytest.approx((40 / W, 30 / H, 40 / W, 30 / H))


def test_motion_ignores_exact_body_shape():
    from tests.test_motion import feed_static, scene
    base, rng = scene()
    det = MotionDetector(sensitivity="high", cooldown=0)
    body = np.zeros((H, W), np.uint8)
    body[40:120, 60:110] = 255  # user's outline
    det.set_dynamic_mask(body)
    feed_static(det, base, rng, 20)
    hits = 0
    for i in range(10):  # movement inside the outline only
        f = base.copy()
        f[200:440, 260:400] = 20 + (i % 2) * 200
        hits += det.process(f, 3.0 + i * 0.1)
    assert hits == 0
    hits = 0
    for i in range(10):  # the same movement just outside it
        f = base.copy()
        f[200:440, 20 + i * 15:100 + i * 15] = 20
        hits += det.process(f, 5.0 + i * 0.1)
    assert hits >= 1


@pytest.mark.skipif(not models_available(SEG_MODEL), reason="segmentation model not present")
def test_real_segmenter_load_segment_unload():
    s = PersonSegmenter()
    s.load()
    m = s.segment(np.full((360, 640, 3), 128, np.uint8))
    assert m.shape == (H, W) and m.dtype == np.uint8
    s.unload()
    assert not s.loaded and s.segment(np.zeros((10, 10, 3), np.uint8)) is None


def test_torso_split_from_head_still_belongs_to_user(tmp_path):
    def mask(i):
        m = np.zeros((H, W), np.uint8)
        m[int(0.28 * H):int(0.56 * H), int(0.60 * W):int(0.78 * W)] = 255   # head
        m[int(0.64 * H):, int(0.48 * W):int(0.92 * W)] = 255                 # torso, not touching the head
        return m
    t = make_tracker(tmp_path, mask)
    states = run(t, 3, lambda i: [face(ME_FACE, ME)])
    assert not any(s.person or s.people_outlines for s in states)
    assert states[-1].me_mask[int(0.9 * H), int(0.7 * W)] > 0
    assert len(states[-1].me_outline) == 2  # head and torso drawn as two parts of "You"
    later = run(t, 3, lambda i: [])  # face gone: both parts still recognised as the user
    assert not any(s.person for s in later)
