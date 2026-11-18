"""Person outlines (no Qt).

PP-HumanSeg (OpenCV model zoo, Apache-2.0) marks every pixel that belongs to a
person. DeskGuard uses it a few times per second at 192x192 and works with a
160x120 mask, so the user's own body shape (not a rectangle) can be ignored
and other people can be outlined.

Pre-processing follows the zoo's pphumanseg.py (RGB, (x/255 - 0.5) / 0.5).
"""

from __future__ import annotations

import logging

import cv2
import numpy as np

from deskguard.faces import FaceEngineError
from deskguard.resources import SEG_MODEL, resource_path

log = logging.getLogger(__name__)

MASK_SIZE = (160, 120)   # w, h – same grid as the motion detector
INPUT = 192

Outline = list[tuple[float, float]]  # normalized raw-frame points


class PersonSegmenter:
    def __init__(self) -> None:
        self._net: cv2.dnn.Net | None = None
        self._kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (5, 5))

    @property
    def loaded(self) -> bool:
        return self._net is not None

    def load(self) -> None:
        if self._net is not None:
            return
        path = resource_path(*SEG_MODEL)
        if not path.is_file():
            raise FaceEngineError("The person-outline model is missing. Reinstall DeskGuard.")
        try:
            self._net = cv2.dnn.readNet(str(path))
        except cv2.error as exc:
            raise FaceEngineError("The person-outline model could not be loaded.") from exc
        log.info("Person segmentation model loaded")

    def unload(self) -> None:
        if self._net is not None:
            log.info("Person segmentation model unloaded")
        self._net = None

    def segment(self, frame: np.ndarray) -> np.ndarray | None:
        """0/255 person mask at MASK_SIZE for a BGR frame, or None if not loaded."""
        net = self._net
        if net is None:
            return None
        small = cv2.resize(frame, (INPUT, INPUT), interpolation=cv2.INTER_AREA)
        blob = cv2.dnn.blobFromImage(small, scalefactor=2.0 / 255.0, mean=(127.5, 127.5, 127.5), swapRB=True)
        net.setInput(blob)
        out = net.forward()[0]  # (2, 192, 192): background, person
        person = (out[1] > out[0]).astype(np.uint8) * 255
        mask = cv2.resize(person, MASK_SIZE, interpolation=cv2.INTER_NEAREST)
        cv2.morphologyEx(mask, cv2.MORPH_CLOSE, self._kernel, dst=mask)
        return mask


def outlines(mask: np.ndarray, min_area_px: int = 20) -> list[Outline]:
    """Simplified outlines of every separate part of a 0/255 mask (normalized points)."""
    contours, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    h, w = mask.shape[:2]
    result = []
    for c in sorted(contours, key=cv2.contourArea, reverse=True):
        if cv2.contourArea(c) < min_area_px:
            break
        c = cv2.approxPolyDP(c, 1.2, True)
        result.append([(float(x) / w, float(y) / h) for x, y in c.reshape(-1, 2)])
    return result


def outline(mask: np.ndarray, min_area_px: int = 20) -> Outline:
    """Simplified outline (largest contour) of a 0/255 mask as normalized points."""
    contours, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    if not contours:
        return []
    c = max(contours, key=cv2.contourArea)
    if cv2.contourArea(c) < min_area_px:
        return []
    c = cv2.approxPolyDP(c, 1.2, True)
    h, w = mask.shape[:2]
    return [(float(x) / w, float(y) / h) for x, y in c.reshape(-1, 2)]


def mask_box(mask: np.ndarray) -> tuple[float, float, float, float] | None:
    """Bounding box (normalized x, y, w, h) of the non-zero pixels."""
    pts = cv2.findNonZero(mask)
    if pts is None:
        return None
    x, y, bw, bh = cv2.boundingRect(pts)
    h, w = mask.shape[:2]
    return (x / w, y / h, bw / w, bh / h)
