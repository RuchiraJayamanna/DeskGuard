"""Locating bundled read-only files (models), from source or a PyInstaller build."""

from __future__ import annotations

import sys
from pathlib import Path


def resource_path(*parts: str) -> Path:
    """Path of a file under the project's `resources` folder."""
    base = getattr(sys, "_MEIPASS", None)  # PyInstaller one-folder: <app>/_internal
    root = Path(base) if base else Path(__file__).resolve().parent.parent
    return root.joinpath("resources", *parts)


YUNET_MODEL = ("models", "face_detection_yunet_2023mar.onnx")
SFACE_MODEL = ("models", "face_recognition_sface_2021dec_int8.onnx")
SEG_MODEL = ("models", "human_segmentation_pphumanseg_2023mar.onnx")
PALM_MODEL = ("models", "palm_detection_mediapipe_2023feb.onnx")
HAND_MODEL = ("models", "handpose_estimation_mediapipe_2023feb.onnx")


def models_available(*models: tuple[str, str]) -> bool:
    """True if all given models (default: the two face models) are bundled."""
    return all(resource_path(*m).is_file() for m in (models or (YUNET_MODEL, SFACE_MODEL)))
