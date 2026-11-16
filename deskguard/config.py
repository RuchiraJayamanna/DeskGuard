"""User preferences: a small dataclass persisted as JSON.

Design notes
- Every field has a safe default, so a missing, partial or corrupt file never
  stops the app from starting.
- Each value is validated on load; invalid values silently fall back to the
  default (and are logged).
- Saves are atomic (write temp file, then os.replace) so a crash or power cut
  during a save can't leave a half-written config behind.
"""

from __future__ import annotations

import json
import logging
import os
import sys
from dataclasses import asdict, dataclass, fields
from pathlib import Path
from typing import Any

from deskguard import APP_NAME

log = logging.getLogger(__name__)

# Choices offered in the right-click menu. Values outside these are rejected.
RESOLUTIONS: tuple[tuple[int, int], ...] = ((640, 480), (1280, 720), (1920, 1080))
FPS_CHOICES: tuple[int, ...] = (10, 15)
SENSITIVITY_CHOICES: tuple[str, ...] = ("low", "medium", "high")
COOLDOWN_CHOICES: tuple[int, ...] = (3, 5, 10)
OPACITY_CHOICES: tuple[int, ...] = (100, 85, 70, 50)

MIN_WINDOW_W, MIN_WINDOW_H = 160, 100
MAX_WINDOW_W, MAX_WINDOW_H = 1920, 1200


def config_dir() -> Path:
    """%APPDATA%\\DeskGuard on Windows, ~/.config/DeskGuard elsewhere."""
    if sys.platform == "win32" and os.environ.get("APPDATA"):
        return Path(os.environ["APPDATA"]) / APP_NAME
    return Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / APP_NAME


def log_dir() -> Path:
    """%LOCALAPPDATA%\\DeskGuard on Windows, ~/.local/state/DeskGuard elsewhere."""
    if sys.platform == "win32" and os.environ.get("LOCALAPPDATA"):
        return Path(os.environ["LOCALAPPDATA"]) / APP_NAME
    return Path(os.environ.get("XDG_STATE_HOME", Path.home() / ".local" / "state")) / APP_NAME


@dataclass
class Config:
    # Camera
    camera_index: int = 0
    camera_name: str = ""
    width: int = 640
    height: int = 480
    fps: int = 10
    # Window (None position = default top-right corner)
    window_x: int | None = None
    window_y: int | None = None
    window_w: int = 240
    window_h: int = 150
    always_on_top: bool = True
    opacity: int = 100
    crop_to_fill: bool = False
    # Camera off after 1 min hidden (light off, ~0 CPU). Off by default: each
    # DirectShow reopen leaks ~6 MB inside Windows/OpenCV.
    release_when_hidden: bool = False
    # Motion detection (off by default)
    motion_enabled: bool = False
    motion_sensitivity: str = "medium"
    motion_cooldown: int = 5
    monitor_while_hidden: bool = False
    show_on_motion: bool = False


def _valid(name: str, value: Any, default: Any) -> bool:
    """Return True if `value` is acceptable for field `name`."""
    if name in ("window_x", "window_y"):
        return value is None or (type(value) is int and -100_000 < value < 100_000)
    if isinstance(default, bool):
        return type(value) is bool
    if name == "camera_index":
        return type(value) is int and 0 <= value <= 31
    if name == "camera_name":
        return isinstance(value, str) and len(value) <= 256
    if name in ("width", "height"):
        return type(value) is int and value > 0
    if name == "fps":
        return value in FPS_CHOICES and type(value) is int
    if name == "window_w":
        return type(value) is int and MIN_WINDOW_W <= value <= MAX_WINDOW_W
    if name == "window_h":
        return type(value) is int and MIN_WINDOW_H <= value <= MAX_WINDOW_H
    if name == "opacity":
        return type(value) is int and value in OPACITY_CHOICES
    if name == "motion_sensitivity":
        return value in SENSITIVITY_CHOICES
    if name == "motion_cooldown":
        return type(value) is int and value in COOLDOWN_CHOICES
    return False


def from_dict(data: dict[str, Any]) -> Config:
    """Build a Config from untrusted data, replacing anything invalid with defaults."""
    cfg = Config()
    for f in fields(Config):
        if f.name not in data:
            continue
        value, default = data[f.name], getattr(cfg, f.name)
        if _valid(f.name, value, default):
            setattr(cfg, f.name, value)
        else:
            log.warning("Ignoring invalid config value %s=%r", f.name, value)
    if (cfg.width, cfg.height) not in RESOLUTIONS:
        log.warning("Ignoring unsupported resolution %sx%s", cfg.width, cfg.height)
        cfg.width, cfg.height = Config.width, Config.height
    return cfg


class ConfigStore:
    """Loads and saves Config to a JSON file."""

    def __init__(self, path: Path | None = None) -> None:
        self.path = path or (config_dir() / "config.json")

    def load(self) -> Config:
        try:
            raw = self.path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return Config()
        except OSError:
            log.exception("Could not read %s; using defaults", self.path)
            return Config()
        try:
            data = json.loads(raw)
            if not isinstance(data, dict):
                raise ValueError("top level is not an object")
        except ValueError:
            backup = self.path.with_suffix(".bad.json")
            log.error("Config file is corrupt; moved to %s and using defaults", backup)
            try:
                os.replace(self.path, backup)
            except OSError:
                pass
            return Config()
        return from_dict(data)

    def save(self, cfg: Config) -> bool:
        tmp = self.path.with_suffix(".tmp")
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp.write_text(json.dumps(asdict(cfg), indent=2), encoding="utf-8")
            os.replace(tmp, self.path)
            return True
        except OSError:
            log.exception("Could not save settings to %s", self.path)
            return False
