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
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path
from typing import Any

from deskguard import APP_NAME

log = logging.getLogger(__name__)

# Choices offered in the right-click menu. Values outside these are rejected.
# 16:9 modes use the full sensor width (most webcams cut the sides off for 4:3).
RESOLUTIONS: tuple[tuple[int, int], ...] = ((640, 360), (848, 480), (1280, 720), (1920, 1080), (640, 480))
FPS_CHOICES: tuple[int, ...] = (10, 15)
SENSITIVITY_CHOICES: tuple[str, ...] = ("low", "medium", "high")
COOLDOWN_CHOICES: tuple[int, ...] = (3, 5, 10)
OPACITY_CHOICES: tuple[int, ...] = (100, 85, 70, 50)
DWELL_CHOICES: tuple[int, ...] = (3, 5, 10, 15)
ALERT_COOLDOWN_CHOICES: tuple[int, ...] = (30, 60, 120, 300)
STRICTNESS_CHOICES: tuple[str, ...] = ("relaxed", "normal", "strict")
THEME_CHOICES: tuple[str, ...] = ("system", "dark", "light")

MIN_WINDOW_W, MIN_WINDOW_H = 160, 100
MAX_WINDOW_W, MAX_WINDOW_H = 1920, 1200

MAX_ZONES = 8
MAX_CAMERAS_WITH_PROPS = 8
CONFIG_VERSION = 3

# Hardware camera properties DeskGuard may save and re-apply (name -> cv2 CAP_PROP_* name).
CAMERA_PROPS: dict[str, str] = {
    "brightness": "CAP_PROP_BRIGHTNESS",
    "contrast": "CAP_PROP_CONTRAST",
    "saturation": "CAP_PROP_SATURATION",
    "hue": "CAP_PROP_HUE",
    "sharpness": "CAP_PROP_SHARPNESS",
    "gamma": "CAP_PROP_GAMMA",
    "gain": "CAP_PROP_GAIN",
    "backlight": "CAP_PROP_BACKLIGHT",
    "zoom": "CAP_PROP_ZOOM",
    "pan": "CAP_PROP_PAN",
    "tilt": "CAP_PROP_TILT",
    "focus": "CAP_PROP_FOCUS",
    "exposure": "CAP_PROP_EXPOSURE",
    "white_balance": "CAP_PROP_WHITE_BALANCE_BLUE_U",
}


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
    height: int = 360
    fps: int = 10
    # Window (None position = default top-right corner)
    window_x: int | None = None
    window_y: int | None = None
    window_w: int = 256
    window_h: int = 144
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
    # Motion is ignored inside these rectangles: [x, y, w, h], normalized raw-frame coords.
    ignore_zones: list[list[float]] = field(default_factory=list)
    # View: flips are applied to the preview only (most built-in cameras mirror).
    flip_h: bool = True
    flip_v: bool = False
    zoom: float = 1.0
    pan_x: float = 0.5
    pan_y: float = 0.5
    # Hardware settings changed in the driver dialog, per camera name.
    camera_props: dict[str, dict[str, float]] = field(default_factory=dict)
    # Watch my back: recognise the user, alert when someone lingers behind them.
    presence_enabled: bool = False
    watch_dwell_s: int = 5
    watch_person_without_face: bool = True
    face_strictness: str = "normal"
    alert_cooldown_s: int = 60
    # Appearance of the Settings window and toast.
    theme: str = "system"
    config_version: int = CONFIG_VERSION


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
    if name == "zoom":
        return _num(value) and 1.0 <= value <= 4.0
    if name in ("pan_x", "pan_y"):
        return _num(value) and 0.0 <= value <= 1.0
    if name == "ignore_zones":
        return isinstance(value, list) and len(value) <= MAX_ZONES and all(_valid_zone(z) for z in value)
    if name == "camera_props":
        return (
            isinstance(value, dict)
            and len(value) <= MAX_CAMERAS_WITH_PROPS
            and all(
                isinstance(cam, str) and len(cam) <= 256 and isinstance(props, dict)
                and all(k in CAMERA_PROPS and _num(v) and abs(v) < 1e6 for k, v in props.items())
                for cam, props in value.items()
            )
        )
    if name == "config_version":
        return type(value) is int
    if name == "watch_dwell_s":
        return type(value) is int and value in DWELL_CHOICES
    if name == "alert_cooldown_s":
        return type(value) is int and value in ALERT_COOLDOWN_CHOICES
    if name == "face_strictness":
        return value in STRICTNESS_CHOICES
    if name == "theme":
        return value in THEME_CHOICES
    return False


def _num(value: Any) -> bool:
    return type(value) in (int, float) and value == value  # rejects bool and NaN


def _valid_zone(zone: Any) -> bool:
    if not (isinstance(zone, list) and len(zone) == 4 and all(_num(v) for v in zone)):
        return False
    x, y, w, h = zone
    return 0 <= x <= 1 and 0 <= y <= 1 and 0 < w <= 1 and 0 < h <= 1


def from_dict(data: dict[str, Any]) -> Config:
    """Build a Config from untrusted data, replacing anything invalid with defaults."""
    cfg = Config()
    if "config_version" not in data:
        data = dict(data)
        _migrate_v1(data)
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
    cfg.zoom, cfg.pan_x, cfg.pan_y = float(cfg.zoom), float(cfg.pan_x), float(cfg.pan_y)
    cfg.ignore_zones = [[float(v) for v in z] for z in cfg.ignore_zones]
    cfg.config_version = CONFIG_VERSION
    return cfg


def _migrate_v1(data: dict[str, Any]) -> None:
    """1.1 -> 1.2: move the old 4:3 defaults to the new wide (16:9) defaults.
    Values the user picked themselves are kept."""
    if (data.get("width"), data.get("height")) == (640, 480):
        data["width"], data["height"] = Config.width, Config.height
    if (data.get("window_w"), data.get("window_h")) == (240, 150):
        data["window_w"], data["window_h"] = Config.window_w, Config.window_h


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
