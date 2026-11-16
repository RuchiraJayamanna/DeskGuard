"""Opening a webcam with OpenCV and listing available cameras.

Backend choice on Windows
- DirectShow (CAP_DSHOW) first: opens quickly and lets us request MJPG, which
  USB 2.0 cameras need for 720p/1080p at usable frame rates (640x480 uses the
  camera's default uncompressed format – no per-frame JPEG decode).
- Media Foundation (CAP_MSMF) as fallback for cameras DirectShow can't open
  (only if the installed OpenCV build ships its MSMF plugin).
  OPENCV_VIDEOIO_MSMF_ENABLE_HW_TRANSFORMS=0 is set in main.py before cv2 is
  imported; it avoids MSMF's very slow open on many webcams.
"""

from __future__ import annotations

import logging
import sys
from dataclasses import dataclass
from typing import Protocol

import cv2
import numpy as np

from deskguard import win32

log = logging.getLogger(__name__)

PRIVACY_HINT = (
    "It may be in use by another app, or camera access may be off in "
    "Windows Settings › Privacy & security › Camera."
)


class CameraError(Exception):
    """Raised with a message that can be shown to the user as-is."""


class Capture(Protocol):
    """The subset of cv2.VideoCapture DeskGuard uses (lets tests use a fake)."""

    def grab(self) -> bool: ...
    def retrieve(self, image: np.ndarray | None = None) -> tuple[bool, np.ndarray | None]: ...
    def release(self) -> None: ...


@dataclass
class OpenedCamera:
    capture: Capture
    width: int
    height: int
    backend: str


def list_cameras() -> list[str]:
    """Camera names in index order. Never opens a camera on Windows."""
    names = win32.list_video_devices()
    if names is not None:
        return names
    # Non-Windows (development only): quick probe of the first few indices.
    found = []
    for i in range(4):
        cap = cv2.VideoCapture(i)
        if cap.isOpened():
            found.append(f"Camera {i}")
        cap.release()
    return found


def _backends() -> list[tuple[int, str]]:
    if sys.platform == "win32":
        return [(cv2.CAP_DSHOW, "DirectShow"), (cv2.CAP_MSMF, "Media Foundation")]
    return [(cv2.CAP_ANY, "default")]


def _first_frame(cap: cv2.VideoCapture) -> np.ndarray | None:
    """Some cameras return a few empty frames right after opening."""
    for _ in range(10):
        ok, frame = cap.read()
        if ok and frame is not None and frame.size:
            return frame
    return None


def _configure(cap: cv2.VideoCapture, api: int, width: int, height: int, fps: int) -> np.ndarray | None:
    """Apply the requested format and return the first good frame.

    DirectShow rebuilds its capture graph on every property change, and each
    rebuild leaks ~6 MB inside DirectShow/OpenCV that is never returned
    (measured: open + width/height + fps = ~19 MB per reopen). So with
    DirectShow we change nothing when the camera's default already matches
    (most webcams default to 640x480), never set FPS (DirectShow's grab()
    returns immediately; CameraWorker paces processing itself), and otherwise
    rebuild only once.
    """
    if api != getattr(cv2, "CAP_DSHOW", -1):
        cap.set(cv2.CAP_PROP_FRAME_WIDTH, width)
        cap.set(cv2.CAP_PROP_FRAME_HEIGHT, height)
        cap.set(cv2.CAP_PROP_FPS, fps)
        cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)  # honoured by some backends only
        return _first_frame(cap)

    frame = _first_frame(cap)
    if frame is not None and frame.shape[1] == width and frame.shape[0] == height:
        return frame
    if width > 640:
        # USB 2.0 needs MJPG for HD at usable frame rates. At 640x480 raw
        # YUY2 fits easily and skips a JPEG decode on every frame.
        cap.set(cv2.CAP_PROP_FOURCC, cv2.VideoWriter_fourcc(*"MJPG"))
    cap.set(cv2.CAP_PROP_FRAME_WIDTH, width)
    cap.set(cv2.CAP_PROP_FRAME_HEIGHT, height)
    return _first_frame(cap)


def open_camera(index: int, width: int, height: int, fps: int) -> OpenedCamera:
    """Open camera `index` and confirm it actually delivers frames.

    Raises CameraError with a user-friendly message on failure.
    """
    names = win32.list_video_devices()
    if names is not None:
        if not names:
            raise CameraError("No webcam found. Connect a camera – DeskGuard will keep checking.")
        if index >= len(names):
            raise CameraError(f"Camera {index + 1} is not connected. Choose another camera from the right-click menu.")
    label = names[index] if names else f"Camera {index}"

    for api, backend in _backends():
        cap = cv2.VideoCapture(index, api)
        if not cap.isOpened():
            cap.release()
            continue
        try:
            frame = _configure(cap, api, width, height, fps)
            if frame is None:
                log.warning("%s opened via %s but returned no frames", label, backend)
                cap.release()
                continue
            h, w = frame.shape[:2]
            log.info("Opened %s via %s at %dx%d", label, backend, w, h)
            return OpenedCamera(cap, w, h, backend)
        except cv2.error:
            log.exception("OpenCV error while opening %s via %s", label, backend)
            cap.release()

    raise CameraError(f"Can't open {label}. {PRIVACY_HINT}")
