"""'Watch my back': find the user, ignore them, and notice anyone lingering behind.

No Qt here. Runs in the camera thread a few times per second.

Two independent ways to notice a watcher
1. Face path: faces are tracked between updates and compared with the user's
   enrolled face profile. Any face that is not the user is a STRANGER.
2. Person path: a slow background model (160x120 grey) finds a person-sized
   shape that is different from the empty scene, even when it stands still and
   the face isn't visible. The user's body area and manual ignore zones are
   masked out.

The user's body area is derived from where their face is, and it stays at the
last known place when the face disappears (looking down, turning away).

An alert fires once per episode when someone has been present for `dwell`
seconds (short gaps tolerated), and not more often than `cooldown`.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import cv2
import numpy as np

from deskguard.faces import Box, Face, FaceEngine, FaceProfile

PERSON_MIN_AREA: dict[str, float] = {"low": 0.05, "medium": 0.03, "high": 0.015}
PERSON_MIN_HEIGHT = 0.20      # of frame height
FG_THRESHOLD = 25             # grey levels
LEARN_BG = 0.05               # background adapts quickly where nothing is present...
LEARN_FG = 0.005              # ...and very slowly under a foreground shape (~1 min at 3 Hz)
LIGHTING_FRACTION = 0.5       # this much changes at once => lighting, rebuild background
WARMUP_UPDATES = 6
GAP_S = 1.5                   # flicker tolerated while counting
CLEAR_S = 3.0                 # nobody for this long => episode over
RECHECK_S = 3.0               # re-verify confirmed face tracks this often
TRACK_MISSES = 4              # drop a face track after this many updates unseen
MAX_EMBEDS_PER_UPDATE = 3


def body_from_face(face: Box) -> Box:
    """The user's body area: around the face and down to the bottom of the frame."""
    fx, fy, fw, fh = face
    x0, x1 = max(0.0, fx - 1.6 * fw), min(1.0, fx + 2.6 * fw)
    y0 = max(0.0, fy - 0.4 * fh)
    return (x0, y0, x1 - x0, 1.0 - y0)


def iou(a: Box, b: Box) -> float:
    ax1, ay1, bx1, by1 = a[0] + a[2], a[1] + a[3], b[0] + b[2], b[1] + b[3]
    iw = max(0.0, min(ax1, bx1) - max(a[0], b[0]))
    ih = max(0.0, min(ay1, by1) - max(a[1], b[1]))
    inter = iw * ih
    union = a[2] * a[3] + b[2] * b[3] - inter
    return inter / union if union > 0 else 0.0


@dataclass
class Track:
    id: int
    face: Face
    missed: int = 0
    label: str = "unknown"            # unknown | me | stranger
    votes: list[bool] = field(default_factory=list)  # True = is me
    checked_at: float = -1e9


@dataclass
class PresenceState:
    me_face: Box | None = None
    me_body: Box | None = None
    strangers: list[Box] = field(default_factory=list)
    person: Box | None = None
    watching: bool = False
    kind: str = ""                    # "face" | "person" | ""
    watch_seconds: float = 0.0
    alert: bool = False               # True on the update that crosses the dwell time
    cleared: bool = False             # True on the update that ends an alerted episode
    error: str = ""

    def key(self) -> tuple:
        """Coarse fingerprint used to avoid re-sending unchanged states to the GUI."""
        def r(b: Box | None) -> tuple | None:
            return None if b is None else tuple(round(v, 2) for v in b)
        return (r(self.me_face), r(self.me_body), tuple(r(b) for b in self.strangers), r(self.person),
                self.watching, self.kind, int(self.watch_seconds), self.alert, self.cleared, self.error)


class PresenceTracker:
    def __init__(self, engine: FaceEngine, profile: FaceProfile, size: tuple[int, int] = (160, 120)) -> None:
        self.engine = engine
        self.profile = profile
        self.size = size
        self.dwell = 5.0
        self.person_trigger = True
        self.threshold = 0.363
        self.cooldown = 60.0
        self.sensitivity = "medium"
        self._zones: list[list[float]] = []
        w, h = size
        self._small = np.empty((h, w, 3), np.uint8)
        self._gray = np.empty((h, w), np.uint8)
        self._bg_u8 = np.empty((h, w), np.uint8)
        self._fg = np.empty((h, w), np.uint8)
        self._kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (3, 3))
        self._last_alert = -1e9
        self.reset()

    # ------------------------------------------------------------------ #
    def configure(self, dwell: float, person_trigger: bool, threshold: float, cooldown: float,
                  sensitivity: str) -> None:
        self.dwell, self.person_trigger = float(dwell), bool(person_trigger)
        self.threshold, self.cooldown, self.sensitivity = float(threshold), float(cooldown), sensitivity

    def set_ignore_zones(self, zones: list[list[float]]) -> None:
        self._zones = [list(z) for z in zones]

    def reset(self) -> None:
        """Forget everything about the scene (e.g. after the camera reopens)."""
        self._tracks: list[Track] = []
        self._next_id = 1
        self.me_face: Box | None = None
        self.me_body: Box | None = None
        self._bg: np.ndarray | None = None
        self._updates = 0
        self._cand_start: float | None = None
        self._last_present = -1e9
        self._kind = ""
        self._alerted = False

    def ignore_regions(self) -> list[list[float]]:
        """Manual zones plus the user's body area (for the motion detector)."""
        regions = [list(z) for z in self._zones]
        if self.me_body is not None:
            regions.append(list(self.me_body))
        return regions

    # ------------------------------------------------------------------ #
    def update(self, frame: np.ndarray, now: float, faces: list[Face] | None = None) -> PresenceState:
        if faces is None:
            faces = self.engine.detect(frame)
        self._update_tracks(frame, now, faces)
        person = self._person_blob(frame)
        self._updates += 1

        strangers = [t.face.box for t in self._tracks if t.label == "stranger" and t.missed == 0]
        kind = "face" if strangers else "person" if person is not None else ""

        state = PresenceState(me_face=self.me_face, me_body=self.me_body, strangers=strangers, person=person)
        if kind:
            if self._cand_start is None:
                self._cand_start = now
            self._last_present = now
            if kind == "face" or not self._kind:
                self._kind = kind
            if (not self._alerted and now - self._cand_start >= self.dwell
                    and now - self._last_alert >= self.cooldown):
                self._alerted = True
                self._last_alert = now
                state.alert = True
        elif self._cand_start is not None:
            idle = now - self._last_present
            if self._alerted and idle > CLEAR_S:
                state.cleared = True
                self._end_episode()
            elif not self._alerted and idle > GAP_S:
                self._end_episode()

        if self._cand_start is not None:
            state.watching = True
            state.kind = self._kind
            state.watch_seconds = now - self._cand_start
        return state

    def _end_episode(self) -> None:
        self._cand_start = None
        self._kind = ""
        self._alerted = False

    # ------------------------------------------------------------------ #
    def _update_tracks(self, frame: np.ndarray, now: float, faces: list[Face]) -> None:
        unmatched = list(faces)
        for track in self._tracks:
            best, best_iou = None, 0.3
            for f in unmatched:
                o = iou(track.face.box, f.box)
                if o > best_iou:
                    best, best_iou = f, o
            if best is not None:
                track.face, track.missed = best, 0
                unmatched.remove(best)
            else:
                track.missed += 1
        self._tracks = [t for t in self._tracks if t.missed <= TRACK_MISSES]
        for f in unmatched:
            self._tracks.append(Track(self._next_id, f))
            self._next_id += 1

        visible = [t for t in self._tracks if t.missed == 0]
        if self.profile.enrolled:
            due = [t for t in visible if t.label == "unknown" or now - t.checked_at >= RECHECK_S]
            due.sort(key=lambda t: t.label != "unknown")  # unconfirmed tracks first
            for track in due[:MAX_EMBEDS_PER_UPDATE]:
                is_me = self.profile.is_me(self.engine.embed(frame, track.face), self.threshold)
                track.checked_at = now
                track.votes = (track.votes + [is_me])[-3:]
                if len(track.votes) >= 2 and track.votes[-1] == track.votes[-2]:
                    track.label = "me" if is_me else "stranger"
        else:
            # No profile yet: assume the largest (closest) face is the user.
            for i, t in enumerate(sorted(visible, key=lambda t: t.face.box[2] * t.face.box[3], reverse=True)):
                t.label = "me" if i == 0 else "stranger"

        me = [t for t in visible if t.label == "me"]
        if me:
            mine = max(me, key=lambda t: t.face.box[2] * t.face.box[3])
            self.me_face = mine.face.box
            self.me_body = body_from_face(mine.face.box)  # sticky until seen again
        elif not any(t.label == "me" for t in self._tracks):
            self.me_face = None  # keep me_body: the user is probably just looking away

    # ------------------------------------------------------------------ #
    def _person_blob(self, frame: np.ndarray) -> Box | None:
        """Largest person-sized shape that differs from the empty scene."""
        w, h = self.size
        cv2.resize(frame, self.size, dst=self._small, interpolation=cv2.INTER_AREA)
        cv2.cvtColor(self._small, cv2.COLOR_BGR2GRAY, dst=self._gray)
        cv2.GaussianBlur(self._gray, (5, 5), 0, dst=self._gray)
        if self._bg is None:
            self._bg = self._gray.astype(np.float32)
            return None

        cv2.convertScaleAbs(self._bg, dst=self._bg_u8)
        cv2.absdiff(self._gray, self._bg_u8, dst=self._fg)
        cv2.threshold(self._fg, FG_THRESHOLD, 255, cv2.THRESH_BINARY, dst=self._fg)
        # The user's body and manual zones never count (and don't trip the
        # lighting check when the user moves close to the camera).
        active = w * h
        regions = self.ignore_regions()
        if regions:
            mask = np.full((h, w), 255, np.uint8)
            for zx, zy, zw, zh in regions:
                mask[max(0, int(zy * h)):max(0, int(np.ceil((zy + zh) * h))),
                     max(0, int(zx * w)):max(0, int(np.ceil((zx + zw) * w)))] = 0
            cv2.bitwise_and(self._fg, mask, dst=self._fg)
            active = cv2.countNonZero(mask)
        if active == 0:
            return None
        if cv2.countNonZero(self._fg) > LIGHTING_FRACTION * active:
            self._bg[:] = self._gray  # lights switched / exposure jump
            return None
        not_fg = cv2.bitwise_not(self._fg)
        cv2.accumulateWeighted(self._gray, self._bg, LEARN_BG, mask=not_fg)
        cv2.accumulateWeighted(self._gray, self._bg, LEARN_FG, mask=self._fg)

        # Only count a person once the user has been located (their own body
        # would otherwise look like a person) and the background has settled.
        if not self.person_trigger or self.me_body is None or self._updates < WARMUP_UPDATES:
            return None
        cv2.morphologyEx(self._fg, cv2.MORPH_OPEN, self._kernel, dst=self._fg)
        n, _, stats, _ = cv2.connectedComponentsWithStats(self._fg, connectivity=8)
        if n <= 1:
            return None
        i = 1 + int(np.argmax(stats[1:, cv2.CC_STAT_AREA]))
        x, y, bw, bh, area = (int(v) for v in stats[i])
        if area < PERSON_MIN_AREA.get(self.sensitivity, 0.03) * w * h or bh < PERSON_MIN_HEIGHT * h:
            return None
        return (x / w, y / h, bw / w, bh / h)
