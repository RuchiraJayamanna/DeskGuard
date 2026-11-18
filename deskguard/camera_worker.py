"""The single background thread that owns the webcam.

Responsibilities
- Open / reopen / release the one camera instance.
- Pace processing to the chosen frame rate (frames above that rate are grabbed
  but never decoded or processed).
- Run motion detection when enabled.
- Scale frames to the preview's exact pixel size (with digital zoom/pan and
  flips applied), so the GUI only draws.
- Open the driver's settings dialog on request and remember what the user
  changed there, re-applying it whenever the camera is opened.
- "Watch my back" (optional): run the PresenceTracker a few times per second,
  and face enrolment on request. Face models are loaded only while needed.
- Gestures (optional): the user's hand gestures and head gestures (nod,
  shake, look away). Their models are loaded only while enabled.
- Recover from disconnects with a backoff, never crashing the app.
- Optionally release the camera (light off, ~0 CPU) after the preview has been
  hidden for IDLE_RELEASE_S with nothing to monitor; reopen when shown again.

Hand-off to the GUI is "latest frame only": a single slot holds at most one
frame, and the GUI is signalled only when it has consumed the previous one, so
nothing can pile up in a queue.
"""

from __future__ import annotations

import logging
import os
import threading
import time
from collections.abc import Callable
from enum import Enum
from pathlib import Path

import cv2
import numpy as np
from PySide6.QtCore import QThread, Signal

from deskguard.camera import (
    PRIVACY_HINT,
    CameraError,
    OpenedCamera,
    apply_props,
    is_manual,
    open_camera,
    open_settings_dialog,
    read_props,
)
from deskguard.faces import Enroller, FaceEngine, FaceEngineError, FaceProfile
from deskguard.gestures import HandModels, HandTracker, HeadGestureTracker, head_pose
from deskguard.motion import MotionDetector
from deskguard.presence import PresenceState, PresenceTracker, iou
from deskguard.segmentation import PersonSegmenter
from deskguard.view import ViewTransform, flip_code

log = logging.getLogger(__name__)

MONITOR_FPS = 5           # detection rate while the preview is hidden
GESTURE_MONITOR_FPS = 10  # ...when gestures are on (they need a faster rate)
HAND_HZ = 6.0             # hand landmark rate (palm detection itself is slower)
HEAD_HZ = 10.0            # head gesture rate (YuNet on a small crop around the face)
FAILS_BEFORE_DISCONNECT = 5
STALL_SECONDS = 3.0       # no good frame for this long => treat as disconnected
RETRY_MIN, RETRY_MAX = 2.0, 10.0
DARK_LEVEL = 8            # mean brightness (0-255) considered "black image"
DARK_CHECK_FRAMES = (10, 30)  # check after exposure settles, then stop checking
IDLE_RELEASE_S = 60.0     # hidden + not monitoring this long => release camera
PAUSED_MSG = "Paused – right-click to resume"
HIDDEN_MSG = "Camera off while hidden"
PROPS_WATCH_S = 120.0     # watch for driver-dialog changes this long after opening it
PROPS_POLL_S = 2.0
NO_SETTINGS_MSG = "This camera has no settings dialog"
PRESENCE_HZ = 3.0         # face / person analysis rate (independent of preview fps)
ENROLL_VIEW = (320, 180)  # live picture for the face-setup screen

OpenFn = Callable[[int, int, int, int], OpenedCamera]


class CamState(str, Enum):
    STARTING = "starting"
    LIVE = "live"
    PAUSED = "paused"
    RECONNECTING = "reconnecting"
    ERROR = "error"
    STOPPED = "stopped"


class CameraWorker(QThread):
    frame_ready = Signal()
    state_changed = Signal(str, str)   # CamState value, user-facing message
    motion_detected = Signal()
    camera_props_changed = Signal(object)  # dict[str, float]: settings to save for this camera
    presence_changed = Signal(object)      # PresenceState (only when it changed)
    watcher_alert = Signal(str, float)     # kind ("face" | "person"), seconds present
    watcher_cleared = Signal()
    enroll_progress = Signal(object)       # dict: progress, hint, box, done, failed, flip_h, flip_v
    enroll_frame_ready = Signal()
    gesture = Signal(str)                  # palm, thumbs_up, peace, fist, nod, shake, look_away
    gesture_progress = Signal(str, float)  # pose being held ("" = none), 0..1

    def __init__(self, open_fn: OpenFn = open_camera, parent=None,  # type: ignore[no-untyped-def]
                 engine: FaceEngine | None = None, profile: FaceProfile | None = None,
                 segmenter: PersonSegmenter | None = None, hand_models: HandModels | None = None) -> None:
        super().__init__(parent)
        self._open_fn = open_fn
        self._wake = threading.Event()
        self._stop = False

        # Desired settings (written by GUI thread, read by worker thread).
        self._index, self._width, self._height, self._fps = 0, 640, 360, 10
        self._display = (256, 144, False)  # width px, height px, crop_to_fill
        self._view = (False, False, 1.0, 0.5, 0.5)  # flip_h, flip_v, zoom, pan_x, pan_y
        self._props: dict[str, float] = {}  # saved hardware settings for this camera
        self._settings_request = False
        self._visible = True
        self._monitor_hidden = False
        self._motion_enabled = False
        self._paused = False
        self._release_hidden = False
        self._reopen = True

        self.detector = MotionDetector()

        # Watch my back (settings written by the GUI thread).
        self.engine = engine or FaceEngine()
        self.profile = profile or FaceProfile(Path(os.devnull))
        self.segmenter = segmenter or PersonSegmenter()
        self.tracker = PresenceTracker(self.engine, self.profile, segmenter=self.segmenter)
        self.hand_models = hand_models or HandModels()
        self.hand_tracker = HandTracker(self.hand_models)
        self.head_tracker = HeadGestureTracker()
        self._gestures: tuple[bool, bool, bool, float] = (False, False, False, 2.0)  # hand, face, look away, s
        self._armed = False            # an alert is showing: nod / shake answer it
        self._snooze_until = 0.0       # monotonic time until which alerts are not sent
        self._seg_failed = False
        self._hand_failed = False
        self._outline_on = True       # person segmentation for body outlines
        self._presence_on = False
        self._presence_cfg: tuple = (5.0, True, 0.363, 60.0, "medium")
        self._presence_zones: list[list[float]] = []
        self._enroll_request: str | None = None  # "start" | "cancel"
        self._presence_failed = False

        # Second latest-frame slot, used only while enrolling a face.
        self._enroll_latest: np.ndarray | None = None
        self._enroll_pending = False

        # Latest-frame slot.
        self._slot_lock = threading.Lock()
        self._latest: np.ndarray | None = None
        self._pending = False
        self._pending_since = 0.0

        # Worker-thread-only state.
        self._cam: OpenedCamera | None = None
        self._buf: np.ndarray | None = None
        self._state: CamState | None = None
        self._message = ""
        self.actual_size: tuple[int, int] | None = None
        self._props_snapshot: dict[str, float] = {}
        self._props_touched: set[str] = set()
        self._props_watch_until = 0.0
        self._props_next_poll = 0.0
        self._enroller: Enroller | None = None
        self._presence_due = 0.0
        self._presence_key: tuple | None = None
        self._hand_due = 0.0
        self._head_due = 0.0
        self._head_box = None          # last known box of the user's face, for the head crop
        self._progress_sent: tuple[str, float] = ("", 0.0)

    # ------------------------------------------------------------------ #
    # GUI-thread API (all cheap, thread-safe attribute writes)
    # ------------------------------------------------------------------ #
    def configure_camera(self, index: int, width: int, height: int, fps: int) -> None:
        changed = (index, width, height) != (self._index, self._width, self._height)
        self._index, self._width, self._height, self._fps = index, width, height, fps
        if changed:
            self._reopen = True
        self._wake.set()

    def set_fps(self, fps: int) -> None:
        self._fps = fps

    def set_display(self, width: int, height: int, crop: bool) -> None:
        self._display = (max(1, width), max(1, height), crop)

    def set_view(self, flip_h: bool, flip_v: bool, zoom: float, pan_x: float, pan_y: float) -> None:
        self._view = (flip_h, flip_v, float(zoom), float(pan_x), float(pan_y))

    def set_camera_props(self, props: dict[str, float]) -> None:
        """Saved hardware settings for the configured camera; applied on (re)open."""
        self._props = dict(props)

    def request_settings_dialog(self) -> None:
        self._settings_request = True
        self._wake.set()

    def set_visible(self, visible: bool) -> None:
        self._visible = visible
        self._wake.set()

    def set_motion(self, enabled: bool, sensitivity: str, cooldown: float, monitor_hidden: bool) -> None:
        if enabled and not self._motion_enabled:
            self.detector.reset()
        self.detector.set_sensitivity(sensitivity)
        self.detector.set_cooldown(cooldown)
        self._motion_enabled = enabled
        self._monitor_hidden = monitor_hidden
        self._wake.set()

    def set_presence(self, enabled: bool, dwell: float, person_trigger: bool, threshold: float,
                     cooldown: float, sensitivity: str) -> None:
        self._presence_cfg = (dwell, person_trigger, threshold, cooldown, sensitivity)
        self._presence_on = enabled
        self._presence_failed = False
        self._wake.set()

    def set_gestures(self, hand: bool, face: bool, look_away: bool, look_away_s: float) -> None:
        self._gestures = (hand, face, look_away, float(look_away_s))
        self._hand_failed = False
        self._presence_failed = False
        self._wake.set()

    def set_body_outline(self, on: bool) -> None:
        self._outline_on = on
        self._wake.set()

    def set_gesture_armed(self, armed: bool) -> None:
        self._armed = armed

    def snooze(self, seconds: float) -> None:
        """Don't send watcher alerts for `seconds` (0 = stop snoozing)."""
        self._snooze_until = time.monotonic() + max(0.0, seconds)

    @property
    def snoozed(self) -> bool:
        return time.monotonic() < self._snooze_until

    def _gestures_on(self) -> bool:
        hand, face, away, _ = self._gestures
        return hand or face or away

    def set_presence_zones(self, zones: list[list[float]]) -> None:
        self._presence_zones = [list(z) for z in zones]

    def set_profile(self, profile: FaceProfile) -> None:
        """Use another profile object (e.g. after it was reloaded or forgotten)."""
        self.profile = profile
        self.tracker.profile = profile

    def start_enroll(self) -> None:
        self._enroll_request = "start"
        self._presence_failed = False
        self._wake.set()

    def cancel_enroll(self) -> None:
        self._enroll_request = "cancel"
        self._wake.set()

    @property
    def enrolling(self) -> bool:
        return self._enroller is not None or self._enroll_request == "start"

    def take_enroll_frame(self) -> np.ndarray | None:
        with self._slot_lock:
            frame, self._enroll_latest, self._enroll_pending = self._enroll_latest, None, False
        return frame

    def set_release_when_hidden(self, enabled: bool) -> None:
        self._release_hidden = enabled
        self._wake.set()

    def set_paused(self, paused: bool) -> None:
        self._paused = paused
        self._wake.set()

    @property
    def paused(self) -> bool:
        """True only when the user paused the camera (not the idle release)."""
        return self._paused

    def take_frame(self) -> np.ndarray | None:
        """Called by the GUI on frame_ready. Returns the newest frame."""
        with self._slot_lock:
            frame, self._latest, self._pending = self._latest, None, False
        return frame

    def stop(self, timeout_ms: int = 3000) -> bool:
        """Ask the thread to finish and release the camera. Returns False if it
        did not stop in time (e.g. a driver call is hanging)."""
        self._stop = True
        self._wake.set()
        return self.wait(timeout_ms)

    # ------------------------------------------------------------------ #
    # Worker thread
    # ------------------------------------------------------------------ #
    def run(self) -> None:
        try:
            self._loop()
        except Exception:
            log.exception("Camera thread crashed")
            self._set_state(CamState.ERROR, "Unexpected camera error. Restart DeskGuard (details in the log).")
        finally:
            self._release()
            self.engine.unload()
            self.segmenter.unload()
            self.hand_models.unload()
            if self._state != CamState.ERROR:
                self._set_state(CamState.STOPPED, "")

    def _loop(self) -> None:
        retry_at = 0.0
        retry_delay = RETRY_MIN
        next_due = 0.0
        fails = 0
        last_ok = time.monotonic()
        idle = False
        frames_seen = 0
        dark_frames = 0
        idle_since: float | None = None
        idle_released = False

        while not self._stop:
            # --- paused: camera fully released (light off) ---------------
            if self._paused:
                if self._cam is not None or self._message != PAUSED_MSG:
                    self._release()
                    self._set_state(CamState.PAUSED, PAUSED_MSG)
                self._reopen = True
                self._sleep(0.5)
                continue

            # --- hidden with nothing to monitor: release after a while ----
            mode = self._mode()
            if mode == "idle":
                now = time.monotonic()
                if idle_since is None:
                    idle_since = now
                if self._release_hidden and not idle_released and now - idle_since >= IDLE_RELEASE_S:
                    log.info("Preview hidden for %.0f s – releasing camera", IDLE_RELEASE_S)
                    self._release()
                    idle_released = True
                if idle_released:
                    self._set_state(CamState.PAUSED, HIDDEN_MSG)  # no-op if unchanged
                    self._sleep(0.5)
                    continue
            else:
                idle_since = None
                if idle_released:
                    idle_released = False
                    self._reopen = True

            # --- (re)open -------------------------------------------------
            if self._reopen or self._cam is None:
                now = time.monotonic()
                if self._reopen:
                    self._release()
                    self._reopen = False
                    retry_at, retry_delay = 0.0, RETRY_MIN
                if now < retry_at:
                    self._sleep(min(0.5, retry_at - now))
                    continue
                if not self._open():
                    retry_at = time.monotonic() + retry_delay
                    retry_delay = min(retry_delay * 1.5, RETRY_MAX)
                    continue
                retry_delay = RETRY_MIN
                next_due = 0.0
                fails, frames_seen, dark_frames = 0, 0, 0
                last_ok = time.monotonic()
                idle = False

            # --- hidden and not monitoring: keep camera open, read nothing
            if mode == "idle":
                idle = True
                self._sleep(0.5)
                continue
            if idle:
                self._flush()
                idle = False
                last_ok = time.monotonic()

            if self._settings_request or self._props_watch_until:
                self._service_settings()

            fps = self._fps if mode == "display" else GESTURE_MONITOR_FPS if self._gestures_on() else MONITOR_FPS
            interval = 1.0 / max(1, fps)
            tolerance = 0.3 * interval

            # --- grab (cheap: no decode) ----------------------------------
            cam = self._cam
            assert cam is not None
            t0 = time.monotonic()
            try:
                ok = cam.capture.grab()
            except cv2.error:
                ok = False
            t1 = time.monotonic()

            if not ok:
                fails += 1
                if fails >= FAILS_BEFORE_DISCONNECT or t1 - last_ok > STALL_SECONDS:
                    self._lost("Camera disconnected – reconnecting…")
                    retry_at = time.monotonic() + RETRY_MIN
                    continue
                self._sleep(0.05)
                continue

            if t1 + tolerance < next_due:
                if t1 - t0 < 0.002:  # backend returned instantly: don't spin
                    self._sleep(min(next_due - t1 - tolerance, 0.01))
                continue
            next_due = max(next_due + interval, t1 + interval - tolerance)

            # --- decode + process -----------------------------------------
            try:
                ok, frame = cam.capture.retrieve(self._buf)
            except cv2.error:
                ok, frame = False, None
            if not ok or frame is None or frame.ndim != 3:
                fails += 1
                continue
            fails = 0
            last_ok = t1
            self._buf = frame  # reused as the decode buffer next time

            if self._motion_enabled and self.detector.process(frame, t1):
                self.motion_detected.emit()

            if (self._presence_on or self._gestures_on() or self._enroll_request
                    or self._enroller is not None):
                self._service_presence(frame, t1)
            elif self.engine.loaded or self.segmenter.loaded or self.hand_models.loaded:
                self._stop_presence()

            if mode == "display":
                self._publish(self._fit(frame))

            # Black-image check shortly after opening (covered lens / camera
            # held by another app often yields black frames).
            if frames_seen < DARK_CHECK_FRAMES[1]:
                frames_seen += 1
                if frames_seen > DARK_CHECK_FRAMES[0]:
                    if float(frame[::16, ::16].mean()) < DARK_LEVEL:
                        dark_frames += 1
                    if frames_seen == DARK_CHECK_FRAMES[1]:
                        span = DARK_CHECK_FRAMES[1] - DARK_CHECK_FRAMES[0]
                        if dark_frames == span:
                            self._set_state(
                                CamState.LIVE,
                                "Camera image is black – the lens may be covered or the camera is busy.",
                                force=True,
                            )

    # ------------------------------------------------------------------ #
    def _mode(self) -> str:
        if self._visible:
            return "display"
        if ((self._motion_enabled and self._monitor_hidden) or self._presence_on or self.enrolling
                or self._gestures_on()):
            return "monitor"  # Watch my back keeps watching while the preview is hidden
        return "idle"

    def _open(self) -> bool:
        if self._state not in (CamState.ERROR, CamState.RECONNECTING):
            self._set_state(CamState.STARTING, "Starting camera…")
        try:
            cam = self._open_fn(self._index, self._width, self._height, self._fps)
        except CameraError as exc:
            self._set_state(CamState.ERROR, str(exc))
            return False
        except Exception:
            log.exception("Camera open failed")
            self._set_state(CamState.ERROR, f"Camera initialisation failed. {PRIVACY_HINT}")
            return False
        if self._stop or self._paused or self._reopen:
            cam.capture.release()  # settings changed while opening
            return False
        self._cam = cam
        self._buf = None
        self.actual_size = (cam.width, cam.height)
        self.detector.reset()
        self.tracker.reset()
        self.hand_tracker.reset()
        self.head_tracker.reset()
        self._head_box = None
        self._presence_key = None
        self._props_watch_until = 0.0
        if self._props:
            apply_props(cam.capture, self._props)
        msg = ""
        if (cam.width, cam.height) != (self._width, self._height):
            msg = f"{self._width}×{self._height} not supported – using {cam.width}×{cam.height}"
            log.info(msg)
        self._set_state(CamState.LIVE, msg)
        return True

    def _lost(self, message: str) -> None:
        log.warning("Camera lost")
        self._release()
        self._set_state(CamState.RECONNECTING, message)

    def _release(self) -> None:
        cam, self._cam = self._cam, None
        self._buf = None
        if cam is not None:
            try:
                cam.capture.release()
            except Exception:
                log.exception("Error releasing camera")

    def _flush(self) -> None:
        """Drop frames buffered by the driver while we weren't reading."""
        cam = self._cam
        if cam is None:
            return
        end = time.monotonic() + 0.3
        for _ in range(5):
            if time.monotonic() > end:
                break
            try:
                if not cam.capture.grab():
                    break
            except cv2.error:
                break

    # ------------------------------------------------------------------ #
    # Watch my back / face enrolment (worker thread)
    # ------------------------------------------------------------------ #
    def _service_presence(self, frame: np.ndarray, now: float) -> None:
        request, self._enroll_request = self._enroll_request, None
        if request == "cancel":
            self._enroller = None
        if self._presence_failed:
            return
        if not self.engine.loaded:
            try:
                self.engine.load()
            except FaceEngineError as exc:
                self._presence_failed = True
                self._enroller = None
                log.error("Face engine unavailable: %s", exc)
                self.presence_changed.emit(PresenceState(error=str(exc)))
                self.enroll_progress.emit({"progress": 0.0, "hint": str(exc), "box": None,
                                           "done": False, "failed": True, "flip_h": False, "flip_v": False})
                return
        if request == "start":
            self._enroller = Enroller(self.engine)
        self._manage_models()

        if self._enroller is not None:
            self._publish_enroll_frame(frame)
        if now >= self._presence_due:
            self._presence_due = now + 1.0 / PRESENCE_HZ
            if self._enroller is not None:
                self._enroll_step(frame, now)
                return
            self._presence_step(frame, now)
        if self._enroller is None:
            self._gesture_step(frame, now)

    def _manage_models(self) -> None:
        """Load / unload the outline and hand models to match what is switched on."""
        want_seg = self._presence_on and self._outline_on
        if want_seg and not self.segmenter.loaded and not self._seg_failed:
            try:
                self.segmenter.load()
            except FaceEngineError as exc:
                self._seg_failed = True  # fall back to the background-difference check
                log.error("Person outlines unavailable: %s", exc)
        elif not want_seg and self.segmenter.loaded:
            self.segmenter.unload()
            self.tracker.me_mask = None
        hand_on = self._gestures[0]
        if hand_on and not self.hand_models.loaded and not self._hand_failed:
            try:
                self.hand_models.load()
            except FaceEngineError as exc:
                self._hand_failed = True
                log.error("Hand gestures unavailable: %s", exc)
                self.presence_changed.emit(PresenceState(error=str(exc)))
        elif not hand_on and self.hand_models.loaded:
            self.hand_models.unload()
            self.hand_tracker.reset()
            self._send_progress("", 0.0)

    def _presence_step(self, frame: np.ndarray, now: float) -> None:
        dwell, person, threshold, cooldown, sensitivity = self._presence_cfg
        self.tracker.configure(dwell, person, threshold, cooldown, sensitivity)
        self.tracker.set_ignore_zones(self._presence_zones)
        state = self.tracker.update(frame, now)
        if state.me_mask is not None:
            self.detector.set_dynamic_mask(state.me_mask)  # the user's exact outline
        else:
            self.detector.set_dynamic_zones([list(state.me_body)] if state.me_body else [])
        if self._presence_on:
            if state.alert and now >= self._snooze_until:
                self.watcher_alert.emit(state.kind, state.watch_seconds)
            if state.cleared:
                self.watcher_cleared.emit()
        key = state.key()
        if key != self._presence_key:
            self._presence_key = key
            self.presence_changed.emit(state)

    def _gesture_step(self, frame: np.ndarray, now: float) -> None:
        hand_on, face_on, away_on, away_s = self._gestures
        tr = self.tracker
        if hand_on and self.hand_models.loaded and now >= self._hand_due:
            self._hand_due = now + 1.0 / HAND_HZ
            res = self.hand_tracker.update(frame, now, tr.me_body, tr.me_mask)
            if res.gesture:
                log.info("Gesture: %s", res.gesture)
                self.gesture.emit(res.gesture)
            self._send_progress(res.pose or "", res.progress)
        if (face_on or away_on) and now >= self._head_due:
            self._head_due = now + 1.0 / HEAD_HZ
            self.head_tracker.look_away_s = away_s
            pose = self._head_pose(frame)
            body = tr.me_mask is not None or tr.me_body is not None
            g = self.head_tracker.update(now, pose, body, self._armed and face_on, away_on)
            if g:
                log.info("Gesture: %s", g)
                self.gesture.emit(g)

    def _head_pose(self, frame: np.ndarray) -> tuple[float, float] | None:
        """Yaw/pitch of the user's face, from YuNet on a crop around it."""
        box = self.tracker.me_face or self._head_box
        if box is None:
            return None
        h, w = frame.shape[:2]
        fx, fy, fw, fh = box
        cx, cy = (fx + fw / 2) * w, (fy + fh / 2) * h
        half = 1.25 * max(fw * w, fh * h)
        x0, y0 = int(max(0, cx - half)), int(max(0, cy - half))
        x1, y1 = int(min(w, cx + half)), int(min(h, cy + half))
        crop = frame[y0:y1, x0:x1]
        if crop.size == 0:
            return None
        best, best_iou = None, 0.15
        for f in self.engine.detect(crop):
            bx, by, bw, bh = f.box
            full = ((x0 + bx * crop.shape[1]) / w, (y0 + by * crop.shape[0]) / h,
                    bw * crop.shape[1] / w, bh * crop.shape[0] / h)
            o = iou(full, box)
            if o > best_iou:
                best, best_iou = (f, full), o
        if best is None:
            return None
        face, full = best
        self._head_box = full
        return head_pose(face.row)  # translation doesn't change the ratios

    def _send_progress(self, pose: str, progress: float) -> None:
        sent = (pose, round(progress, 1))
        if sent != self._progress_sent:
            self._progress_sent = sent
            self.gesture_progress.emit(pose, progress)

    def _enroll_step(self, frame: np.ndarray, now: float) -> None:
        enroller = self._enroller
        assert enroller is not None
        status = enroller.update(frame, now, self.engine.detect(frame))
        done = status.done
        hint = status.hint
        if done:
            if self.profile.save(enroller.samples):
                log.info("Face profile saved (%d samples)", len(enroller.samples))
                self.tracker.reset()
            else:
                done, hint = False, "Couldn't save your face profile (see the log)."
                status.failed = True
        if done or status.failed:
            self._enroller = None
        flip_h, flip_v = self._view[0], self._view[1]
        self.enroll_progress.emit({"progress": status.progress, "hint": hint, "box": status.box,
                                   "done": done, "failed": status.failed, "flip_h": flip_h, "flip_v": flip_v})

    def _publish_enroll_frame(self, frame: np.ndarray) -> None:
        img = cv2.resize(frame, ENROLL_VIEW, interpolation=cv2.INTER_AREA)
        code = flip_code(self._view[0], self._view[1])
        if code is not None:
            img = cv2.flip(img, code)
        with self._slot_lock:
            self._enroll_latest = img
            notify = not self._enroll_pending
            self._enroll_pending = True
        if notify:
            self.enroll_frame_ready.emit()

    def _stop_presence(self) -> None:
        self.engine.unload()
        self.segmenter.unload()
        self.hand_models.unload()
        self.tracker.reset()
        self.hand_tracker.reset()
        self.head_tracker.reset()
        self._head_box = None
        self._send_progress("", 0.0)
        self.detector.set_dynamic_zones([])
        self._presence_key = None
        self.presence_changed.emit(PresenceState())

    def _service_settings(self) -> None:
        """Open the driver dialog on request, then poll for the user's changes."""
        cam = self._cam
        if cam is None:
            return
        now = time.monotonic()
        if self._settings_request:
            self._settings_request = False
            if cam.backend != "DirectShow" or not open_settings_dialog(cam.capture):
                self._set_state(CamState.LIVE, NO_SETTINGS_MSG, force=True)
                return
            log.info("Opened camera settings dialog")
            self._props_snapshot = read_props(cam.capture)
            self._props_touched = set()
            self._props_watch_until = now + PROPS_WATCH_S
            self._props_next_poll = now + PROPS_POLL_S
            return
        if now >= self._props_watch_until:
            self._props_watch_until = 0.0
            return
        if now < self._props_next_poll:
            return
        self._props_next_poll = now + PROPS_POLL_S
        current = read_props(cam.capture)
        for name, value in current.items():
            if value != self._props_snapshot.get(name) and is_manual(cam.capture, name):
                self._props_touched.add(name)
        if not self._props_touched:
            return
        merged = dict(self._props)
        merged.update({k: current[k] for k in self._props_touched if k in current})
        if merged != self._props:
            self._props = merged
            log.info("Camera settings changed: %s", sorted(self._props_touched))
            self.camera_props_changed.emit(dict(merged))

    def _fit(self, frame: np.ndarray) -> np.ndarray:
        tw, th, crop = self._display
        flip_h, flip_v, zoom, pan_x, pan_y = self._view
        h, w = frame.shape[:2]
        vt = ViewTransform(w, h, tw, th, crop, zoom, pan_x, pan_y, flip_h, flip_v)
        x, y, cw, ch = vt.source_rect()
        out = vt.output_size()
        interp = cv2.INTER_AREA if out[0] < cw else cv2.INTER_LINEAR
        img = cv2.resize(frame[y : y + ch, x : x + cw], out, interpolation=interp)
        code = flip_code(flip_h, flip_v)
        return img if code is None else cv2.flip(img, code)

    def _publish(self, img: np.ndarray) -> None:
        now = time.monotonic()
        with self._slot_lock:
            self._latest = img
            # Signal only if the GUI consumed the previous frame. Re-signal after
            # 1 s anyway, so a lost signal can never stall the preview forever.
            notify = not self._pending or now - self._pending_since > 1.0
            if notify:
                self._pending, self._pending_since = True, now
        if notify:
            self.frame_ready.emit()

    def _set_state(self, state: CamState, message: str, force: bool = False) -> None:
        if force or (state, message) != (self._state, self._message):
            self._state, self._message = state, message
            self.state_changed.emit(state.value, message)

    def _sleep(self, seconds: float) -> None:
        self._wake.wait(max(0.0, seconds))
        self._wake.clear()
