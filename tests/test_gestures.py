"""Hand pose classification, gesture timing and head gestures (no models needed)."""

import numpy as np
import pytest

from deskguard.gestures import (
    GestureDebouncer,
    HandModels,
    HandTracker,
    HeadGestureTracker,
    _palm_anchors,
    classify_hand,
    head_pose,
    palm_from_landmarks,
)
from deskguard.resources import HAND_MODEL, PALM_MODEL, models_available

MCP = {5: (85, 140), 9: (100, 135), 13: (115, 140), 17: (128, 148)}


def hand(fingers=(True, True, True, True), thumb="out"):
    """Upright right hand in image pixels (y grows downwards)."""
    lm = np.zeros((21, 2))
    lm[0] = (100, 200)
    for f, base in enumerate((5, 9, 13, 17)):
        x, y = MCP[base]
        lm[base] = (x, y)
        if fingers[f]:
            lm[base + 1], lm[base + 2], lm[base + 3] = (x, y - 30), (x, y - 45), (x, y - 60)
        else:
            lm[base + 1], lm[base + 2], lm[base + 3] = (x, y - 20), (x, y - 5), (x, y + 10)
    lm[1], lm[2] = (75, 185), (62, 170)
    if thumb == "out":
        lm[3], lm[4] = (52, 155), (35, 135)
    elif thumb == "up":
        lm[3], lm[4] = (72, 115), (70, 90)
    else:  # tucked across the palm
        lm[3], lm[4] = (85, 160), (100, 150)
    return lm


@pytest.mark.parametrize("lm,expected", [
    (hand(), "palm"),
    (hand((False,) * 4, "in"), "fist"),
    (hand((False,) * 4, "up"), "thumbs_up"),
    (hand((True, True, False, False), "in"), "peace"),
    (hand((True, False, False, False), "in"), None),   # pointing: not a gesture
])
def test_classify_hand(lm, expected):
    assert classify_hand(lm) == expected


def test_classifier_works_mirrored_and_scaled():
    lm = hand((False,) * 4, "up")
    mirrored = lm.copy()
    mirrored[:, 0] = 400 - mirrored[:, 0]
    assert classify_hand(mirrored) == "thumbs_up"
    assert classify_hand(lm * 2.5) == "thumbs_up"


def feed(deb, poses, t0=0.0, dt=0.1):
    fired = []
    for i, p in enumerate(poses):
        g, _, _ = deb.update(p, t0 + i * dt)
        if g:
            fired.append((g, round(t0 + i * dt, 2)))
    return fired


def test_debouncer_hold_and_refractory():
    deb = GestureDebouncer()
    fired = feed(deb, ["palm"] * 30)  # 3 s of open palm
    assert fired == [("palm", 1.0)]  # once, after 1 s; not again while still held
    fired = feed(deb, [None] * 5 + ["palm"] * 15, t0=3.0)  # put the hand down, show it again
    assert len(fired) == 1


def test_debouncer_short_pose_and_flicker():
    deb = GestureDebouncer()
    assert feed(deb, ["fist"] * 10 + [None] * 10) == []  # 1 s fist < 1.5 s hold
    deb = GestureDebouncer()
    poses = ["thumbs_up", "thumbs_up", None] * 5  # flickers every third frame
    assert [g for g, _ in feed(deb, poses)] == ["thumbs_up"]


def test_debouncer_reports_progress():
    deb = GestureDebouncer()
    deb.update("fist", 0.0)
    _, pose, progress = deb.update("fist", 0.75)
    assert pose == "fist" and progress == pytest.approx(0.5)


class FakeHandModels:
    def __init__(self, lm):
        self.lm = lm
        self.detects = 0

    def detect_palms(self, image):
        self.detects += 1
        return np.array([np.r_[0, 0, 10, 10, np.zeros(14), 0.9]], np.float32)

    def landmarks(self, image, palm):
        return None if self.lm is None else self.lm.copy()


def test_hand_tracker_only_counts_the_users_hand():
    lm = hand()  # wrist at x=100, y=200 in a 640x360 frame -> u~0.16
    frame = np.zeros((360, 640, 3), np.uint8)
    models = FakeHandModels(lm)
    tr = HandTracker(models)
    me_far = (0.6, 0.2, 0.3, 0.8)   # user sits on the right: this hand is someone else's
    res = [tr.update(frame, i * 0.1, me_far, None) for i in range(20)]
    assert not any(r.gesture for r in res)
    models = FakeHandModels(lm)
    tr = HandTracker(models)
    me_near = (0.05, 0.2, 0.3, 0.8)
    res = [tr.update(frame, i * 0.1, me_near, None) for i in range(20)]
    assert [r.gesture for r in res if r.gesture] == ["palm"]
    assert models.detects <= 3  # tracked from landmarks after the first detection
    tr = HandTracker(models)
    assert not any(tr.update(frame, i * 0.1, None, None).gesture for i in range(20))  # user unknown


def test_palm_from_landmarks_and_anchors():
    row = palm_from_landmarks(hand())
    assert row.shape == (19,) and row[0] <= row[2] and row[1] <= row[3]
    anchors = _palm_anchors()
    assert anchors.shape == (2016, 2) and anchors.min() > 0 and anchors.max() < 1


def yunet_row(yaw=0.0, pitch=0.55, eye_d=40.0):
    re, le = np.array([300.0, 150.0]), np.array([300.0 + eye_d, 150.0])
    mid = (re + le) / 2
    nose = mid + [yaw * eye_d, pitch * eye_d]
    return np.r_[0, 0, 0, 0, re, le, nose, 0, 0, 0, 0, 0.9].astype(np.float32)


def run_head(tr, seq, armed=True, look_away=False, t0=0.0, dt=0.1, body=True):
    out = []
    for i, p in enumerate(seq):
        g = tr.update(t0 + i * dt, p, body, armed, look_away)
        if g:
            out.append(g)
    return out


def test_head_pose_from_yunet_points():
    assert head_pose(yunet_row(0.2, 0.6)) == pytest.approx((0.2, 0.6))


def test_nod_and_shake_only_when_armed():
    still = [(0.0, 0.55)] * 10
    nod = still + [(0.0, 0.62), (0.0, 0.72), (0.0, 0.75), (0.0, 0.66), (0.0, 0.56), (0.0, 0.55)]
    assert run_head(HeadGestureTracker(), nod) == ["nod"]
    assert run_head(HeadGestureTracker(), nod, armed=False) == []
    shake = still + [(0.2, 0.55), (0.25, 0.55), (-0.2, 0.55), (-0.25, 0.55), (0.2, 0.55), (0.0, 0.55)]
    assert run_head(HeadGestureTracker(), shake) == ["shake"]
    # ordinary small head movement does nothing
    wobble = still + [(0.05, 0.58), (-0.05, 0.52), (0.04, 0.57), (0.0, 0.55)] * 3
    assert run_head(HeadGestureTracker(), wobble) == []


def test_look_away_after_delay_but_not_looking_down():
    still = [(0.0, 0.55)] * 10
    away = still + [(0.5, 0.55)] * 25  # turned for 2.5 s
    tr = HeadGestureTracker(look_away_s=2.0)
    assert run_head(tr, away, armed=False, look_away=True) == ["look_away"]
    down = still + [(0.0, 0.95)] * 30  # looking at the keyboard
    assert run_head(HeadGestureTracker(), down, armed=False, look_away=True) == []
    short = still + [(0.5, 0.55)] * 10 + still  # glanced aside for 1 s
    assert run_head(HeadGestureTracker(), short, armed=False, look_away=True) == []
    # turned away until the face is lost, body still in the chair
    lost = still + [(0.25, 0.55), (0.4, 0.55)] + [None] * 25
    assert run_head(HeadGestureTracker(), lost, armed=False, look_away=True) == ["look_away"]
    # fires once per look-away; again only after looking back
    tr = HeadGestureTracker(look_away_s=1.0)
    seq = still + [(0.5, 0.55)] * 30 + still + [(0.5, 0.55)] * 15
    assert run_head(tr, seq, armed=False, look_away=True) == ["look_away", "look_away"]


@pytest.mark.skipif(not models_available(PALM_MODEL, HAND_MODEL), reason="hand models not present")
def test_real_hand_models_run_and_unload():
    m = HandModels()
    m.load()
    frame = np.full((360, 640, 3), 128, np.uint8)
    assert len(m.detect_palms(frame)) == 0
    palm = palm_from_landmarks(hand() + [200, 50])
    out = m.landmarks(frame, palm)
    assert out is None or out.shape == (21, 2)
    m.unload()
    assert not m.loaded
