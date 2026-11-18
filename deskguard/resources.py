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


def models_available() -> bool:
    return resource_path(*YUNET_MODEL).is_file() and resource_path(*SFACE_MODEL).is_file()
