"""Download the two face models into resources/models (build-time only).

DeskGuard itself never downloads anything: the models ship inside the app.
Run this once when building from a fresh checkout if the .onnx files are
missing. Each file is verified against a pinned SHA-256.

    python tools\\fetch_models.py
"""

from __future__ import annotations

import hashlib
import sys
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DEST = ROOT / "resources" / "models"
BASE = "https://github.com/opencv/opencv_zoo/raw/main/models/"
MODELS = {
    "face_detection_yunet_2023mar.onnx": (
        "face_detection_yunet/face_detection_yunet_2023mar.onnx",
        "8f2383e4dd3cfbb4553ea8718107fc0423210dc964f9f4280604804ed2552fa4",
    ),
    "face_recognition_sface_2021dec_int8.onnx": (
        "face_recognition_sface/face_recognition_sface_2021dec_int8.onnx",
        "2b0e941e6f16cc048c20aee0c8e31f569118f65d702914540f7bfdc14048d78a",
    ),
    "human_segmentation_pphumanseg_2023mar.onnx": (
        "human_segmentation_pphumanseg/human_segmentation_pphumanseg_2023mar.onnx",
        "552d8a984054e59b5d773d24b9b12022b22046ceb2bbc4c9aaeaceb36a9ddf24",
    ),
    "palm_detection_mediapipe_2023feb.onnx": (
        "palm_detection_mediapipe/palm_detection_mediapipe_2023feb.onnx",
        "78ff51c38496b7fc8b8ebdb6cc8c1abb02fa6c38427c6848254cdaba57fcce7c",
    ),
    "handpose_estimation_mediapipe_2023feb.onnx": (
        "handpose_estimation_mediapipe/handpose_estimation_mediapipe_2023feb.onnx",
        "db0898ae717b76b075d9bf563af315b29562e11f8df5027a1ef07b02bef6d81c",
    ),
}


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def main() -> int:
    DEST.mkdir(parents=True, exist_ok=True)
    ok = True
    for name, (rel, digest) in MODELS.items():
        target = DEST / name
        if target.is_file() and sha256(target) == digest:
            print(f"ok       {name}")
            continue
        print(f"download {name}")
        tmp = target.with_suffix(".part")
        urllib.request.urlretrieve(BASE + rel, tmp)  # noqa: S310 - fixed https URL
        if sha256(tmp) != digest:
            tmp.unlink(missing_ok=True)
            print(f"ERROR    {name}: checksum mismatch", file=sys.stderr)
            ok = False
            continue
        tmp.replace(target)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
