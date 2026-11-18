"""Face detection, recognition and the user's face profile (no Qt).

Models (bundled, offline – see resources/models/LICENSES.md):
- YuNet   (face detection, ~0.2 MB)
- SFace   (face recognition, int8, ~10 MB) -> 128-number feature vector

Privacy
- No face image is ever stored. The profile file holds only feature vectors
  (lists of numbers), and can be deleted at any time (FaceProfile.forget).
- Models are loaded only while a face feature is in use (FaceEngine.load /
  unload), so they cost no RAM otherwise.
"""

from __future__ import annotations

import json
import logging
import math
import os
from dataclasses import dataclass
from pathlib import Path

import cv2
import numpy as np

from deskguard.resources import SFACE_MODEL, YUNET_MODEL, resource_path

log = logging.getLogger(__name__)

Box = tuple[float, float, float, float]  # x, y, w, h normalized to the raw frame

DETECT_WIDTH = 416          # faces are detected on a copy this wide
DETECT_SCORE = 0.75
STRICTNESS: dict[str, float] = {"relaxed": 0.30, "normal": 0.363, "strict": 0.42}
PROFILE_VERSION = 1
MODEL_ID = "sface_2021dec_int8"


class FaceEngineError(Exception):
    """Raised with a user-facing message when the models can't be used."""


@dataclass
class Face:
    box: Box
    score: float
    row: np.ndarray  # YuNet result row (15 values) in frame pixels, for alignCrop

    def pixel_size(self, frame_w: int) -> float:
        return self.box[2] * frame_w


class FaceEngine:
    """Thin wrapper around OpenCV's YuNet detector and SFace recognizer."""

    def __init__(self, detect_width: int = DETECT_WIDTH) -> None:
        self.detect_width = detect_width
        self._det: cv2.FaceDetectorYN | None = None
        self._rec: cv2.FaceRecognizerSF | None = None
        self._input_size: tuple[int, int] | None = None
        self._small: np.ndarray | None = None

    @property
    def loaded(self) -> bool:
        return self._det is not None

    def load(self) -> None:
        if self.loaded:
            return
        det_path, rec_path = resource_path(*YUNET_MODEL), resource_path(*SFACE_MODEL)
        if not det_path.is_file() or not rec_path.is_file():
            raise FaceEngineError("Face models are missing. Reinstall DeskGuard.")
        try:
            self._det = cv2.FaceDetectorYN.create(str(det_path), "", (320, 180), DETECT_SCORE, 0.3, 20)
            self._rec = cv2.FaceRecognizerSF.create(str(rec_path), "")
        except cv2.error as exc:
            self.unload()
            raise FaceEngineError("Face models could not be loaded.") from exc
        self._input_size = None
        log.info("Face models loaded")

    def unload(self) -> None:
        if self._det is not None or self._rec is not None:
            log.info("Face models unloaded")
        self._det = self._rec = None
        self._input_size = None
        self._small = None

    def detect(self, frame: np.ndarray) -> list[Face]:
        """Faces in `frame` (BGR), largest first."""
        if self._det is None:
            return []
        h, w = frame.shape[:2]
        scale = min(1.0, self.detect_width / w)
        sw, sh = max(1, round(w * scale)), max(1, round(h * scale))
        if scale < 1.0:
            if self._small is None or self._small.shape[:2] != (sh, sw):
                self._small = np.empty((sh, sw, 3), np.uint8)
            cv2.resize(frame, (sw, sh), dst=self._small, interpolation=cv2.INTER_AREA)
            img = self._small
        else:
            img = frame
        if self._input_size != (sw, sh):
            self._det.setInputSize((sw, sh))
            self._input_size = (sw, sh)
        _, rows = self._det.detect(img)
        if rows is None:
            return []
        faces = []
        for r in rows:
            row = r.copy()
            row[:14] /= scale  # back to full-frame pixels
            x, y, bw, bh = (float(v) for v in row[:4])
            box = (max(0.0, x / w), max(0.0, y / h), min(bw / w, 1.0), min(bh / h, 1.0))
            faces.append(Face(box, float(row[14]), row))
        faces.sort(key=lambda f: f.box[2] * f.box[3], reverse=True)
        return faces

    def embed(self, frame: np.ndarray, face: Face) -> np.ndarray:
        """L2-normalized 128-number feature vector of `face`."""
        if self._rec is None:
            raise FaceEngineError("Face models are not loaded.")
        crop = self._rec.alignCrop(frame, face.row)
        feat = self._rec.feature(crop).reshape(-1).astype(np.float32)
        norm = float(np.linalg.norm(feat))
        return feat / norm if norm > 0 else feat

    @staticmethod
    def similarity(a: np.ndarray, b: np.ndarray) -> float:
        """Cosine similarity of two normalized vectors (-1..1)."""
        return float(np.dot(a, b))


# --------------------------------------------------------------------------- #
class FaceProfile:
    """The user's enrolled face: a few feature vectors, persisted as JSON."""

    MIN_VECTORS = 3

    def __init__(self, path: Path) -> None:
        self.path = path
        self.vectors: list[np.ndarray] = []

    @property
    def enrolled(self) -> bool:
        return len(self.vectors) >= self.MIN_VECTORS

    def load(self) -> bool:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            self.vectors = []
            return False
        except (OSError, ValueError):
            log.warning("Face profile unreadable; ignoring it")
            self.vectors = []
            return False
        vectors = []
        if isinstance(data, dict) and data.get("model") == MODEL_ID and isinstance(data.get("vectors"), list):
            for v in data["vectors"][:16]:
                if isinstance(v, list) and len(v) == 128 and all(type(x) in (int, float) and math.isfinite(x) for x in v):
                    arr = np.asarray(v, np.float32)
                    norm = float(np.linalg.norm(arr))
                    if norm > 0:
                        vectors.append(arr / norm)
        self.vectors = vectors
        return self.enrolled

    def save(self, vectors: list[np.ndarray]) -> bool:
        data = {
            "version": PROFILE_VERSION,
            "model": MODEL_ID,
            "note": "DeskGuard face profile: numbers only, no image. Delete this file to forget your face.",
            "vectors": [[round(float(x), 6) for x in v] for v in vectors],
        }
        tmp = self.path.with_suffix(".tmp")
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp.write_text(json.dumps(data), encoding="utf-8")
            os.replace(tmp, self.path)
        except OSError:
            log.exception("Could not save the face profile")
            return False
        self.vectors = [v.astype(np.float32) for v in vectors]
        return True

    def forget(self) -> None:
        self.vectors = []
        try:
            self.path.unlink()
        except FileNotFoundError:
            pass
        except OSError:
            log.exception("Could not delete the face profile")

    def best_score(self, vector: np.ndarray) -> float:
        vectors = self.vectors  # local ref: may be replaced by the GUI thread
        if not vectors:
            return -1.0
        return max(float(np.dot(vector, v)) for v in vectors)

    def is_me(self, vector: np.ndarray, threshold: float) -> bool:
        return self.best_score(vector) >= threshold


# --------------------------------------------------------------------------- #
@dataclass
class EnrollStatus:
    progress: float          # 0..1
    hint: str
    box: Box | None          # the face being enrolled (raw normalized)
    done: bool = False
    failed: bool = False


class Enroller:
    """Collects a handful of varied, good-quality samples of one face."""

    TARGET = 6
    MIN_GAP_S = 0.4
    MIN_FACE_PX = 60
    MIN_SCORE = 0.9
    TOO_SIMILAR = 0.97       # skip near-duplicate samples (head didn't move)
    TIMEOUT_S = 30.0

    def __init__(self, engine: FaceEngine) -> None:
        self.engine = engine
        self.samples: list[np.ndarray] = []
        self._last = -1e9
        self._start: float | None = None

    def update(self, frame: np.ndarray, now: float, faces: list[Face]) -> EnrollStatus:
        if self._start is None:
            self._start = now
        progress = len(self.samples) / self.TARGET
        if now - self._start > self.TIMEOUT_S:
            return EnrollStatus(progress, "Couldn't see your face clearly. Try better light and look at the camera.",
                                None, failed=True)
        if not faces:
            return EnrollStatus(progress, "Look at the camera", None)
        if len(faces) > 1:
            return EnrollStatus(progress, "Only one face please – make sure nobody is behind you", faces[0].box)
        face = faces[0]
        w = frame.shape[1]
        cx = face.box[0] + face.box[2] / 2
        if face.pixel_size(w) < self.MIN_FACE_PX:
            return EnrollStatus(progress, "Move a little closer", face.box)
        if not 0.15 <= cx <= 0.85:
            return EnrollStatus(progress, "Move to the centre of the picture", face.box)
        if face.score < self.MIN_SCORE:
            return EnrollStatus(progress, "Hold still – more light helps", face.box)
        hint = "Look at the camera" if len(self.samples) < 3 else "Now turn your head slightly left and right"
        if now - self._last >= self.MIN_GAP_S:
            vec = self.engine.embed(frame, face)
            if all(float(np.dot(vec, s)) < self.TOO_SIMILAR for s in self.samples) or len(self.samples) < 2:
                self.samples.append(vec)
                self._last = now
        progress = len(self.samples) / self.TARGET
        if len(self.samples) >= self.TARGET:
            return EnrollStatus(1.0, "Done – DeskGuard knows your face", face.box, done=True)
        return EnrollStatus(progress, hint, face.box)
