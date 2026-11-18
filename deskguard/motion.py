"""Lightweight motion detection (frame differencing against a running average).

No Qt here, so it is easy to unit-test. All work happens on a tiny blurred
grayscale copy of the frame (160x120 by default), which costs well under a
millisecond per frame on a modern CPU.

False-positive handling
- Running-average background slowly absorbs gradual lighting changes.
- Gaussian blur + per-pixel threshold ignores sensor noise.
- Motion must persist for N consecutive frames before it counts.
- A "lighting event" (a large share of the image changing at once, e.g. lights
  switched or camera auto-exposure) resets the background instead of alerting.
- Warm-up frames after a reset are ignored while exposure settles.
- A cooldown prevents repeated alerts for the same event.
- Ignore zones (e.g. where the user sits) are masked out; fractions are
  measured against the remaining (active) area only.
"""

from __future__ import annotations

import threading

import cv2
import numpy as np

# sensitivity -> (minimum changed-pixel fraction, consecutive frames required)
SENSITIVITY: dict[str, tuple[float, int]] = {
    "low": (0.020, 3),
    "medium": (0.008, 2),
    "high": (0.003, 2),
}


class MotionDetector:
    def __init__(
        self,
        sensitivity: str = "medium",
        cooldown: float = 5.0,
        size: tuple[int, int] = (160, 120),
        pixel_threshold: int = 25,
        lighting_fraction: float = 0.55,
        learn_rate: float = 0.05,
        warmup_frames: int = 5,
    ) -> None:
        self.size = size
        self.pixel_threshold = pixel_threshold
        self.lighting_fraction = lighting_fraction
        self.learn_rate = learn_rate
        self.warmup_frames = warmup_frames
        self.cooldown = cooldown
        self.min_fraction, self.required_frames = SENSITIVITY["medium"]
        self.set_sensitivity(sensitivity)

        w, h = size
        # Reused buffers: no per-frame allocations after the first frame.
        self._small = np.empty((h, w, 3), np.uint8)
        self._gray = np.empty((h, w), np.uint8)
        self._diff = np.empty((h, w), np.uint8)
        self._bg_u8 = np.empty((h, w), np.uint8)
        # (mask, active pixel count). mask is 0 inside ignore zones, 255 elsewhere;
        # None = no zones. Replaced as one tuple so the GUI thread can swap it safely.
        self._zones: tuple[np.ndarray | None, int] = (None, w * h)
        self._static_zones: list[list[float]] = []
        self._dynamic_zones: list[list[float]] = []
        self._zones_lock = threading.Lock()  # GUI sets static zones, worker sets dynamic ones
        self._background: np.ndarray | None = None
        self._consecutive = 0
        self._seen = 0
        self._last_event = -1e9
        self.last_fraction = 0.0

    def set_sensitivity(self, sensitivity: str) -> None:
        self.min_fraction, self.required_frames = SENSITIVITY.get(sensitivity, SENSITIVITY["medium"])

    def set_cooldown(self, seconds: float) -> None:
        self.cooldown = max(0.0, float(seconds))

    def set_ignore_zones(self, zones: list[list[float]]) -> None:
        """Ignore motion inside these [x, y, w, h] rectangles (normalized 0..1)."""
        self._static_zones = [list(z) for z in zones]
        self._rebuild_mask()

    def set_dynamic_zones(self, zones: list[list[float]]) -> None:
        """Extra ignore rectangles that change at runtime (the user's body area)."""
        zones = [list(z) for z in zones]
        if zones != self._dynamic_zones:
            self._dynamic_zones = zones
            self._rebuild_mask()

    def _rebuild_mask(self) -> None:
        with self._zones_lock:
            self._build_mask(self._static_zones + self._dynamic_zones)

    def _build_mask(self, zones: list[list[float]]) -> None:
        w, h = self.size
        if not zones:
            self._zones = (None, w * h)
            return
        mask = np.full((h, w), 255, np.uint8)
        for zx, zy, zw, zh in zones:
            x0, y0 = int(np.floor(zx * w)), int(np.floor(zy * h))
            x1, y1 = int(np.ceil((zx + zw) * w)), int(np.ceil((zy + zh) * h))
            mask[max(0, y0):max(0, y1), max(0, x0):max(0, x1)] = 0
        self._zones = (mask, cv2.countNonZero(mask))

    def reset(self) -> None:
        """Forget the background, e.g. after the camera is reopened."""
        self._background = None
        self._consecutive = 0
        self._seen = 0

    def process(self, frame_bgr: np.ndarray, now: float) -> bool:
        """Feed one frame. Returns True only when a new motion event fires."""
        cv2.resize(frame_bgr, self.size, dst=self._small, interpolation=cv2.INTER_AREA)
        cv2.cvtColor(self._small, cv2.COLOR_BGR2GRAY, dst=self._gray)
        cv2.GaussianBlur(self._gray, (5, 5), 0, dst=self._gray)

        if self._background is None:
            self._background = self._gray.astype(np.float32)
            self._seen = 1
            return False

        cv2.convertScaleAbs(self._background, dst=self._bg_u8)
        cv2.absdiff(self._gray, self._bg_u8, dst=self._diff)
        cv2.threshold(self._diff, self.pixel_threshold, 255, cv2.THRESH_BINARY, dst=self._diff)
        mask, active = self._zones
        if mask is not None:
            cv2.bitwise_and(self._diff, mask, dst=self._diff)
        fraction = cv2.countNonZero(self._diff) / active if active else 0.0
        self.last_fraction = fraction

        self._seen += 1
        if fraction >= self.lighting_fraction or self._seen <= self.warmup_frames:
            # Whole-scene change (lighting / exposure) or still settling: adopt
            # the current image as the new background, don't alert.
            self._background[:] = self._gray
            self._consecutive = 0
            return False

        cv2.accumulateWeighted(self._gray, self._background, self.learn_rate)

        if active and fraction >= self.min_fraction:
            self._consecutive += 1
        else:
            self._consecutive = 0

        if self._consecutive >= self.required_frames and now - self._last_event >= self.cooldown:
            self._last_event = now
            self._consecutive = 0
            return True
        return False
