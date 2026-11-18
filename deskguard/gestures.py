"""Hand and head gestures (no Qt). Only the user's own hand / face count.

Hand pipeline (OpenCV model zoo, Apache-2.0):
  MediaPipe palm detector (192x192) -> hand landmark model (224x224, 21 points).
  Pre/post-processing is adapted from the zoo's mp_palmdet.py / mp_handpose.py:
    Copyright (C) 2021, Shenzhen Institute of Artificial Intelligence and
    Robotics for Society. Licensed under the Apache License, Version 2.0
    (resources/models/LICENSE_palm_detection_mediapipe.txt,
     resources/models/LICENSE_handpose_estimation_mediapipe.txt).
  Once a hand is found it is followed from its previous landmarks, so the
  (heavier) palm detector only runs while no hand is being tracked.

Head gestures use the 5 YuNet points (eyes, nose, mouth corners) of the
user's recognised face: nod, shake, and "look away".
"""

from __future__ import annotations

import logging
import math
from collections import deque
from dataclasses import dataclass, field

import cv2
import numpy as np

from deskguard.faces import Box, FaceEngineError
from deskguard.resources import HAND_MODEL, PALM_MODEL, resource_path

log = logging.getLogger(__name__)

HOLD_S: dict[str, float] = {"palm": 1.0, "thumbs_up": 0.8, "peace": 0.8, "fist": 1.5}
REFRACTORY_S = 2.0
FLICKER_S = 0.35            # a pose may drop out this long without restarting the hold
MIN_AGREEMENT = 0.7         # share of samples during the hold that must show the pose
PALM_SCORE = 0.6
HAND_CONF = 0.75
PALM_LANDMARK_IDS = (0, 5, 9, 13, 17, 1, 2)


# --------------------------------------------------------------------------- #
# Models
# --------------------------------------------------------------------------- #
def _palm_anchors() -> np.ndarray:
    """SSD anchors of the 192x192 MediaPipe palm detector (2016 x 2, normalized)."""
    out = []
    for stride, per_cell in ((8, 2), (16, 6)):
        n = 192 // stride
        for y in range(n):
            for x in range(n):
                out.extend([((x + 0.5) / n, (y + 0.5) / n)] * per_cell)
    return np.array(out, np.float32)


class HandModels:
    """Palm detector + hand landmark model, loaded together."""

    def __init__(self) -> None:
        self._palm: cv2.dnn.Net | None = None
        self._hand: cv2.dnn.Net | None = None
        self._anchors = _palm_anchors()

    @property
    def loaded(self) -> bool:
        return self._palm is not None

    def load(self) -> None:
        if self.loaded:
            return
        palm, hand = resource_path(*PALM_MODEL), resource_path(*HAND_MODEL)
        if not palm.is_file() or not hand.is_file():
            raise FaceEngineError("Hand gesture models are missing. Reinstall DeskGuard.")
        try:
            self._palm = cv2.dnn.readNet(str(palm))
            self._hand = cv2.dnn.readNet(str(hand))
        except cv2.error as exc:
            self.unload()
            raise FaceEngineError("Hand gesture models could not be loaded.") from exc
        log.info("Hand gesture models loaded")

    def unload(self) -> None:
        if self._palm is not None:
            log.info("Hand gesture models unloaded")
        self._palm = self._hand = None

    # -- palm detection (mp_palmdet.py) ---------------------------------- #
    def detect_palms(self, image: np.ndarray) -> np.ndarray:
        """Rows of [x1, y1, x2, y2, 7 x (x, y), score] in `image` pixels."""
        net = self._palm
        if net is None:
            return np.empty((0, 19), np.float32)
        h, w = image.shape[:2]
        ratio = min(192 / h, 192 / w)
        rh, rw = int(h * ratio), int(w * ratio)
        img = cv2.resize(image, (rw, rh))
        pad_w, pad_h = 192 - rw, 192 - rh
        left, top = pad_w // 2, pad_h // 2
        img = cv2.copyMakeBorder(img, top, pad_h - top, left, pad_w - left, cv2.BORDER_CONSTANT, value=(0, 0, 0))
        blob = cv2.cvtColor(img, cv2.COLOR_BGR2RGB).astype(np.float32)[np.newaxis] / 255.0
        pad_bias = (np.array([left, top], np.float32) / ratio).astype(np.int32)
        net.setInput(blob)
        out = net.forward(net.getUnconnectedOutLayersNames())
        score = 1 / (1 + np.exp(-out[1][0, :, 0].astype(np.float64)))
        deltas = out[0][0]
        scale = max(w, h)
        cxy = deltas[:, :2] / 192
        wh = deltas[:, 2:4] / 192
        xy1 = (cxy - wh / 2 + self._anchors) * scale - pad_bias
        xy2 = (cxy + wh / 2 + self._anchors) * scale - pad_bias
        boxes = np.concatenate([xy1, xy2], axis=1)
        keep = cv2.dnn.NMSBoxes([[float(b[0]), float(b[1]), float(b[2] - b[0]), float(b[3] - b[1])] for b in boxes],
                                score.tolist(), PALM_SCORE, 0.3) if (score >= PALM_SCORE).any() else []
        rows = []
        for i in np.asarray(keep).reshape(-1):
            lm = deltas[i, 4:].reshape(7, 2) / 192 + self._anchors[i]
            lm = lm * scale - pad_bias
            rows.append(np.r_[boxes[i], lm.reshape(-1), score[i]].astype(np.float32))
        return np.array(rows, np.float32).reshape(-1, 19)

    # -- hand landmarks (mp_handpose.py) --------------------------------- #
    @staticmethod
    def _crop_from_palm(image: np.ndarray, box: np.ndarray, for_rotation: bool):  # type: ignore[no-untyped-def]
        wh = box[1] - box[0]
        shift = np.array([0, 0] if for_rotation else [0, -0.4]) * wh
        box = box + shift
        center = box.sum(axis=0) / 2
        wh = box[1] - box[0]
        half = wh * (4 if for_rotation else 3) / 2
        box = np.array([center - half, center + half]).astype(np.int32)
        box[:, 0] = np.clip(box[:, 0], 0, image.shape[1])
        box[:, 1] = np.clip(box[:, 1], 0, image.shape[0])
        crop = image[box[0][1]:box[1][1], box[0][0]:box[1][0]]
        side = int(np.linalg.norm(crop.shape[:2])) if for_rotation else max(crop.shape[:2])
        pad_h, pad_w = side - crop.shape[0], side - crop.shape[1]
        left, top = pad_w // 2, pad_h // 2
        crop = cv2.copyMakeBorder(crop, top, pad_h - top, left, pad_w - left, cv2.BORDER_CONSTANT, value=(0, 0, 0))
        return crop, box, box[0] - [left, top]

    def landmarks(self, image: np.ndarray, palm: np.ndarray) -> np.ndarray | None:
        """21 x 2 hand points in `image` pixels, or None if no confident hand."""
        net = self._hand
        if net is None:
            return None
        crop, box, bias = self._crop_from_palm(image, palm[0:4].reshape(2, 2), True)
        if crop.size == 0 or min(crop.shape[:2]) < 8:
            return None
        crop = cv2.cvtColor(crop, cv2.COLOR_BGR2RGB)
        pad_bias = np.array(bias, np.int32)
        palm_lm = palm[4:18].reshape(7, 2) - pad_bias
        p1, p2 = palm_lm[0], palm_lm[2]
        radians = np.pi / 2 - np.arctan2(-(p2[1] - p1[1]), p2[0] - p1[0])
        radians -= 2 * np.pi * np.floor((radians + np.pi) / (2 * np.pi))
        angle = float(np.rad2deg(radians))
        center = (box - pad_bias).sum(axis=0) / 2
        rot = cv2.getRotationMatrix2D((float(center[0]), float(center[1])), angle, 1.0)
        rotated = cv2.warpAffine(crop, rot, (crop.shape[1], crop.shape[0]))
        hom = np.c_[palm_lm, np.ones(7)]
        rlm = np.array([hom @ rot[0], hom @ rot[1]])
        rbox = np.array([rlm.min(axis=1), rlm.max(axis=1)])
        hand_crop, rbox, _ = self._crop_from_palm(rotated, rbox, False)
        if hand_crop.size == 0:
            return None
        blob = cv2.resize(hand_crop, (224, 224), interpolation=cv2.INTER_AREA).astype(np.float32)[np.newaxis] / 255.0
        net.setInput(blob)
        lms, conf, _handedness, _world = net.forward(net.getUnconnectedOutLayersNames())
        if float(conf[0][0]) < HAND_CONF:
            return None
        lms = lms[0].reshape(-1, 3)[:, :2].astype(np.float64)
        scale = max((rbox[1] - rbox[0]) / 224)
        lms = (lms - 112) * scale
        lms = lms @ cv2.getRotationMatrix2D((0, 0), angle, 1.0)[:, :2]
        rc = np.array([[rot[0][0], rot[1][0]], [rot[0][1], rot[1][1]]])
        rt = np.array([rot[0][2], rot[1][2]])
        inv = np.c_[rc, [-rc[0] @ rt, -rc[1] @ rt]]
        c = np.append(rbox.sum(axis=0) / 2, 1)
        return lms + np.array([c @ inv[0], c @ inv[1]]) + pad_bias


def palm_from_landmarks(lm: np.ndarray) -> np.ndarray:
    """A palm-detector-style row built from the previous hand (for tracking)."""
    pts = lm[list(PALM_LANDMARK_IDS)]
    return np.r_[pts.min(axis=0), pts.max(axis=0), pts.reshape(-1), 1.0].astype(np.float32)


# --------------------------------------------------------------------------- #
# Hand pose classification (21 points: wrist, thumb 1-4, index 5-8, ...)
# --------------------------------------------------------------------------- #
def classify_hand(lm: np.ndarray) -> str | None:
    """'palm' | 'fist' | 'thumbs_up' | 'peace' | None for a 21x2 landmark set."""
    lm = np.asarray(lm, np.float64)[:, :2]
    wrist = lm[0]
    palm_size = float(np.linalg.norm(lm[9] - wrist))
    if palm_size < 1e-6:
        return None

    def extended(tip: int, pip: int) -> bool:
        return np.linalg.norm(lm[tip] - wrist) > np.linalg.norm(lm[pip] - wrist) * 1.15

    fingers = [extended(8, 6), extended(12, 10), extended(16, 14), extended(20, 18)]
    thumb_out = (np.linalg.norm(lm[4] - lm[17]) > np.linalg.norm(lm[3] - lm[17]) * 1.1
                 and np.linalg.norm(lm[4] - lm[5]) > 0.55 * palm_size)
    if all(fingers) and thumb_out:
        return "palm"
    if fingers[0] and fingers[1] and not fingers[2] and not fingers[3]:
        return "peace"
    if not any(fingers):
        thumb_up = lm[4][1] < lm[5][1] - 0.35 * palm_size and lm[4][1] < wrist[1] - 0.6 * palm_size
        if thumb_out and thumb_up:
            return "thumbs_up"
        if np.linalg.norm(lm[4] - lm[9]) < 1.0 * palm_size or not thumb_out:
            return "fist"
    return None


class GestureDebouncer:
    """Fires a pose once it has been held long enough, then waits for a change."""

    def __init__(self, hold: dict[str, float] | None = None) -> None:
        self.hold = dict(hold or HOLD_S)
        self.reset()

    def reset(self) -> None:
        self._pose: str | None = None
        self._start = 0.0
        self._last_seen = -1e9
        self._samples: deque[tuple[float, str | None]] = deque()
        self._blocked: str | None = None   # fired pose; must change before it can fire again
        self._refractory_until = -1e9

    def update(self, pose: str | None, now: float) -> tuple[str | None, str | None, float]:
        """Returns (fired gesture or None, pose being held or None, hold progress 0..1)."""
        self._samples.append((now, pose))
        while self._samples and now - self._samples[0][0] > 3.0:
            self._samples.popleft()
        if pose is not None and pose == self._pose:
            self._last_seen = now
        elif pose is not None:
            if self._pose is None or now - self._last_seen > FLICKER_S:
                self._pose, self._start, self._last_seen = pose, now, now
        elif self._pose is not None and now - self._last_seen > FLICKER_S:
            self._pose = None
        if self._blocked is not None and self._pose != self._blocked:
            self._blocked = None
        current = self._pose
        if current is None or current not in self.hold or current == self._blocked:
            return None, None, 0.0
        hold = self.hold[current]
        progress = min(1.0, (now - self._start) / hold)
        if progress >= 1.0 and now >= self._refractory_until:
            window = [p for t, p in self._samples if t >= self._start]
            if window and sum(p == current for p in window) / len(window) >= MIN_AGREEMENT:
                self._blocked = current
                self._refractory_until = now + REFRACTORY_S
                self._pose = None
                return current, None, 0.0
        return None, current, progress


@dataclass
class HandResult:
    gesture: str | None = None        # fired this update
    pose: str | None = None           # being held
    progress: float = 0.0
    landmarks: np.ndarray | None = None


class HandTracker:
    """Find the user's hand, follow it, classify it and debounce gestures."""

    DETECT_INTERVAL = 0.4    # palm detector at most 2.5x/s while no hand is tracked

    def __init__(self, models: HandModels) -> None:
        self.models = models
        self.debouncer = GestureDebouncer()
        self._track: np.ndarray | None = None
        self._next_detect = 0.0

    def reset(self) -> None:
        self._track = None
        self.debouncer.reset()

    def update(self, frame: np.ndarray, now: float, me_body: Box | None, me_mask: np.ndarray | None) -> HandResult:
        if me_body is None:  # the user hasn't been located: nobody's gestures count
            self._track = None
            fired, pose, prog = self.debouncer.update(None, now)
            return HandResult(fired, pose, prog)
        h, w = frame.shape[:2]
        lm = None
        if self._track is not None:
            lm = self.models.landmarks(frame, self._track)
            if lm is None:
                self._track = None
        if lm is None and now >= self._next_detect:
            self._next_detect = now + self.DETECT_INTERVAL
            x0, y0, x1, y1 = _hand_region(me_body, w, h)
            crop = frame[y0:y1, x0:x1]
            if crop.size:
                palms = self.models.detect_palms(crop)
                if len(palms):
                    palm = palms[int(np.argmax(palms[:, 18]))].copy()
                    palm[0:18:2] += x0
                    palm[1:18:2] += y0
                    lm = self.models.landmarks(frame, palm)
        pose = None
        if lm is not None and _belongs_to_user(lm, me_body, me_mask, w, h):
            self._track = palm_from_landmarks(lm)
            pose = classify_hand(lm)
        elif lm is not None:
            self._track, lm = None, None
        fired, held, progress = self.debouncer.update(pose, now)
        return HandResult(fired, held, progress, lm)


def _hand_region(body: Box, w: int, h: int) -> tuple[int, int, int, int]:
    """Where to look for the user's hands: their body area, widened and raised."""
    bx, by, bw, bh = body
    x0, x1 = max(0.0, bx - 0.35 * bw), min(1.0, bx + 1.35 * bw)
    y0 = max(0.0, by - 0.35 * bh)
    return int(x0 * w), int(y0 * h), int(math.ceil(x1 * w)), h


def _belongs_to_user(lm: np.ndarray, body: Box, mask: np.ndarray | None, w: int, h: int) -> bool:
    wrist = lm[0]
    u, v = wrist[0] / w, wrist[1] / h
    if mask is not None:
        mh, mw = mask.shape[:2]
        near = cv2.dilate(mask, np.ones((9, 9), np.uint8))
        x, y = int(np.clip(u * mw, 0, mw - 1)), int(np.clip(v * mh, 0, mh - 1))
        if near[y, x]:
            return True
    bx, by, bw, bh = body
    return bx - 0.25 * bw <= u <= bx + 1.25 * bw and by - 0.35 * bh <= v <= 1.0


# --------------------------------------------------------------------------- #
# Head gestures
# --------------------------------------------------------------------------- #
def head_pose(row: np.ndarray) -> tuple[float, float] | None:
    """(yaw, pitch) proxies from YuNet's 5 points: nose offset from the eye
    midpoint, in eye distances. Yaw > 0: nose towards image right."""
    re, le, nose = np.array(row[4:6]), np.array(row[6:8]), np.array(row[8:10])
    d = float(np.linalg.norm(le - re))
    if d < 1e-3:
        return None
    mid = (re + le) / 2
    return float((nose[0] - mid[0]) / d), float((nose[1] - mid[1]) / d)


@dataclass
class HeadGestureTracker:
    """Nod / shake (only while armed) and look-away, from yaw/pitch samples."""

    nod_threshold: float = 0.13
    shake_threshold: float = 0.14
    away_threshold: float = 0.38
    look_away_s: float = 2.0
    _samples: deque = field(default_factory=lambda: deque(maxlen=60))
    _base: tuple[float, float] | None = None
    _away_since: float | None = None
    _away_fired: bool = False
    _last_seen: float = -1e9
    _last_yaw: float = 0.0
    _cooldown_until: float = -1e9

    def reset(self) -> None:
        self._samples.clear()
        self._base = None
        self._away_since = None
        self._away_fired = False

    def update(self, now: float, pose: tuple[float, float] | None, body_present: bool,
               armed: bool, look_away: bool) -> str | None:
        """Feed one sample (pose None = user's face not found). Returns 'nod',
        'shake', 'look_away' or None."""
        if pose is not None:
            yaw, pitch = pose
            if self._base is None:
                self._base = (yaw, pitch)
            dy, dp = yaw - self._base[0], pitch - self._base[1]
            # slowly adapt the neutral pose while the head is roughly still
            if abs(dy) < 0.12 and abs(dp) < 0.1:
                self._base = (self._base[0] * 0.95 + yaw * 0.05, self._base[1] * 0.95 + pitch * 0.05)
            self._samples.append((now, dy, dp))
            self._last_seen, self._last_yaw = now, dy
        while self._samples and now - self._samples[0][0] > 1.6:
            self._samples.popleft()

        if look_away:
            away = (pose is not None and abs(self._last_yaw) >= self.away_threshold) or (
                pose is None and body_present and now - self._last_seen < self.look_away_s + 1.0
                and abs(self._last_yaw) >= self.away_threshold * 0.55)
            if away:
                if self._away_since is None:
                    self._away_since = now
                if not self._away_fired and now - self._away_since >= self.look_away_s:
                    self._away_fired = True
                    return "look_away"
            elif pose is not None and abs(self._last_yaw) < self.away_threshold * 0.4:
                self._away_since = None
                self._away_fired = False  # looked back: may fire again next time

        if not armed or pose is None or now < self._cooldown_until:
            return None
        gesture = self._nod() or self._shake()
        if gesture:
            self._samples.clear()
            self._cooldown_until = now + 1.5
        return gesture

    def _nod(self) -> str | None:
        pts = [(t, dy, dp) for t, dy, dp in self._samples]
        if len(pts) < 4:
            return None
        yaws = [p[1] for p in pts]
        if max(yaws) - min(yaws) > 0.18:
            return None
        t_end = pts[-1][0]
        recent = [p for p in pts if t_end - p[0] <= 1.2]
        peak = max(range(len(recent)), key=lambda i: recent[i][2])
        if recent[peak][2] < self.nod_threshold:
            return None
        before = [p[2] for p in recent[:peak]]
        after = [p[2] for p in recent[peak + 1:]]
        if before and after and min(before) < self.nod_threshold * 0.4 and after[-1] < self.nod_threshold * 0.4:
            return "nod"
        return None

    def _shake(self) -> str | None:
        signs: list[int] = []
        for _, dy, dp in self._samples:
            if abs(dp) > 0.2:
                return None
            if abs(dy) >= self.shake_threshold:
                s = 1 if dy > 0 else -1
                if not signs or signs[-1] != s:
                    signs.append(s)
        return "shake" if len(signs) >= 3 else None
