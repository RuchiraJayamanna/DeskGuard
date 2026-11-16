"""The single background thread that owns the webcam.

Responsibilities
- Open / reopen / release the one camera instance.
- Pace processing to the chosen frame rate (frames above that rate are grabbed
  but never decoded or processed).
- Run motion detection when enabled.
- Scale frames to the preview's exact pixel size, so the GUI only draws.
- Recover from disconnects with a backoff, never crashing the app.
- Optionally release the camera (light off, ~0 CPU) after the preview has been
  hidden for IDLE_RELEASE_S with nothing to monitor; reopen when shown again.

Hand-off to the GUI is "latest frame only": a single slot holds at most one
frame, and the GUI is signalled only when it has consumed the previous one, so
nothing can pile up in a queue.
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable
from enum import Enum

import cv2
import numpy as np
from PySide6.QtCore import QThread, Signal

from deskguard.camera import CameraError, OpenedCamera, PRIVACY_HINT, open_camera
from deskguard.motion import MotionDetector

log = logging.getLogger(__name__)

MONITOR_FPS = 5           # detection rate while the preview is hidden
FAILS_BEFORE_DISCONNECT = 5
STALL_SECONDS = 3.0       # no good frame for this long => treat as disconnected
RETRY_MIN, RETRY_MAX = 2.0, 10.0
DARK_LEVEL = 8            # mean brightness (0-255) considered "black image"
DARK_CHECK_FRAMES = (10, 30)  # check after exposure settles, then stop checking
IDLE_RELEASE_S = 60.0     # hidden + not monitoring this long => release camera
PAUSED_MSG = "Paused – right-click to resume"
HIDDEN_MSG = "Camera off while hidden"

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

    def __init__(self, open_fn: OpenFn = open_camera, parent=None) -> None:  # type: ignore[no-untyped-def]
        super().__init__(parent)
        self._open_fn = open_fn
        self._wake = threading.Event()
        self._stop = False

        # Desired settings (written by GUI thread, read by worker thread).
        self._index, self._width, self._height, self._fps = 0, 640, 480, 10
        self._display = (240, 150, False)  # width px, height px, crop_to_fill
        self._visible = True
        self._monitor_hidden = False
        self._motion_enabled = False
        self._paused = False
        self._release_hidden = False
        self._reopen = True

        self.detector = MotionDetector()

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

            fps = self._fps if mode == "display" else MONITOR_FPS
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
        if self._motion_enabled and self._monitor_hidden:
            return "monitor"
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

    def _fit(self, frame: np.ndarray) -> np.ndarray:
        tw, th, crop = self._display
        h, w = frame.shape[:2]
        if crop:
            scale = max(tw / w, th / h)
            cw, ch = min(w, round(tw / scale)), min(h, round(th / scale))
            x, y = (w - cw) // 2, (h - ch) // 2
            src, out = frame[y : y + ch, x : x + cw], (tw, th)
        else:
            scale = min(tw / w, th / h)
            src, out = frame, (max(1, round(w * scale)), max(1, round(h * scale)))
        interp = cv2.INTER_AREA if scale < 1 else cv2.INTER_LINEAR
        return cv2.resize(src, out, interpolation=interp)

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
