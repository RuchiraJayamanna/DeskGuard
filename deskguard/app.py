"""Wires the preview window, camera worker, tray icon, menu, Settings window and toast together."""

from __future__ import annotations

import logging
import os
import time
from functools import partial

from PySide6.QtCore import QObject, QPoint, QRect, QTimer, QUrl, Signal
from PySide6.QtGui import QAction, QActionGroup, QDesktopServices, QGuiApplication
from PySide6.QtWidgets import QApplication, QMenu, QSystemTrayIcon

from deskguard import APP_NAME, __version__, icons, win32
from deskguard.camera import list_cameras
from deskguard.camera_worker import HIDDEN_MSG, CameraWorker
from deskguard.config import HAND_GESTURES, MAX_CAMERAS_WITH_PROPS, Config, ConfigStore, config_dir, log_dir
from deskguard.faces import STRICTNESS, FaceProfile
from deskguard.presence import PresenceState
from deskguard.preview_window import PreviewWindow
from deskguard.ui import theme

log = logging.getLogger(__name__)

MOTION_HOLD_S = 3.0        # how long the red MOTION indicator stays
TRAY_ALERT_S = 5.0         # how long the tray icon stays red after hidden motion
AUTO_HIDE_S = 10.0         # auto-shown preview hides again after this
STALL_WARN_S = 4.0         # no frames for this long while live => warn
DIAG_INTERVAL_MS = 60_000
SCREEN_MARGIN = 16
SNOOZE_MIN = 10

GESTURE_LABELS = {
    "palm": "Open palm", "thumbs_up": "Thumbs up", "peace": "Peace sign", "fist": "Fist",
    "nod": "Nod", "shake": "Head shake", "look_away": "Look away",
}
ACTION_LABELS = {
    "toggle_preview": "Hide / show preview", "dismiss": "Dismiss the alert",
    "snooze": f"Snooze alerts for {SNOOZE_MIN} min", "toggle_watch": "Pause / resume Watch my back",
    "none": "Do nothing",
}
PRIVACY_LABELS = {"show_desktop": "Show the desktop", "lock": "Lock the PC", "hide_preview": "Hide the preview"}

STATE_LABELS = {
    "starting": "Starting",
    "live": "Camera live",
    "paused": "Paused",
    "reconnecting": "Reconnecting",
    "error": "Camera error",
    "stopped": "Stopped",
}


class DeskGuardApp(QObject):
    changed = Signal()          # a setting was changed and saved
    status_changed = Signal()   # camera state / Watch my back status changed

    def __init__(self, qapp: QApplication, store: ConfigStore, diag: bool = False,
                 profile: FaceProfile | None = None) -> None:
        super().__init__()
        self.qapp = qapp
        self.store = store
        self.cfg: Config = store.load()
        self.profile = profile or FaceProfile(config_dir() / "face.json")
        self.profile.load()
        self.presence = PresenceState()
        self._alert_active = False
        self.toast = None       # ToastWindow, created on first alert
        self.hud = None         # HudPill, created on first gesture
        self._snoozed_until = 0.0
        self.settings = None    # SettingsWindow, created when opened
        self.diag = diag  # --diag: log RAM/CPU every minute

        self.state, self.message = "starting", ""
        self._camera_names: list[str] = []
        self._last_frame_at = time.monotonic()
        self._stalled = False
        self._auto_shown = False
        self._quitting = False
        self._raw_size: tuple[int, int] | None = None
        self._prefs_dirty = False
        self.cpu = win32.CpuSampler()

        self.window = PreviewWindow()
        self.window.setWindowIcon(icons.icon())
        self.worker = CameraWorker()
        self.worker.set_profile(self.profile)

        self.menu = QMenu()
        self.menu.aboutToShow.connect(self._build_menu)

        self.tray = QSystemTrayIcon(icons.icon("starting"))
        self.tray.setContextMenu(self.menu)
        self.tray.activated.connect(self._on_tray_activated)

        self._save_timer = self._timer(800, self._save_geometry)
        self._tray_alert_timer = self._timer(int(TRAY_ALERT_S * 1000), self._update_tray)
        self._auto_hide_timer = self._timer(int(AUTO_HIDE_S * 1000), self._auto_hide)
        self._watchdog = self._timer(1000, self._check_stall, single=False)
        self._diag_timer = self._timer(DIAG_INTERVAL_MS, self._log_resources, single=False)

        w = self.window
        w.exit_requested.connect(self.quit)
        w.hide_requested.connect(self.hide_preview)
        w.menu_requested.connect(self._popup_menu)
        w.geometry_changed.connect(self._save_timer.start)
        w.display_size_changed.connect(self._on_display_size)
        w.user_interacted.connect(self._cancel_auto_hide)
        w.view_changed.connect(self._on_view_changed)
        w.zones_changed.connect(self._on_zones_changed)
        w.zone_edit_finished.connect(self._sync_zones)
        w.settings_requested.connect(self.open_settings)

        self.worker.frame_ready.connect(self._on_frame)
        self.worker.state_changed.connect(self._on_state)
        self.worker.motion_detected.connect(self._on_motion)
        self.worker.camera_props_changed.connect(self._on_camera_props)
        self.worker.presence_changed.connect(self._on_presence)
        self.worker.watcher_alert.connect(self._on_watcher_alert)
        self.worker.watcher_cleared.connect(self._on_watcher_cleared)
        self.worker.gesture.connect(self._on_gesture)
        self.worker.gesture_progress.connect(self.window.set_gesture_progress)

        qapp.aboutToQuit.connect(self._shutdown)

    def _timer(self, ms: int, slot, single: bool = True) -> QTimer:  # type: ignore[no-untyped-def]
        t = QTimer(self)
        t.setInterval(ms)
        t.setSingleShot(single)
        t.timeout.connect(slot)
        return t

    # ------------------------------------------------------------------ #
    # Startup / shutdown
    # ------------------------------------------------------------------ #
    def start(self) -> None:
        cfg = self.cfg
        theme.set_current(theme.palette_for(cfg.theme))
        self._resolve_camera_by_name()
        self._restore_geometry()
        self.window.setWindowOpacity(cfg.opacity / 100)
        if not cfg.always_on_top:
            self.window.set_on_top(False)

        self.worker.configure_camera(cfg.camera_index, cfg.width, cfg.height, cfg.fps)
        self.worker.set_camera_props(cfg.camera_props.get(self._camera_name(), {}))
        self.worker.set_display(*self.window.physical_size(), cfg.crop_to_fill)
        self._apply_view()
        self._apply_motion()
        self._apply_presence()
        self._apply_gestures()
        self.worker.set_release_when_hidden(cfg.release_when_hidden)
        background = cfg.start_in_background and QSystemTrayIcon.isSystemTrayAvailable()
        self.worker.set_visible(not background)
        self.worker.start()

        if QSystemTrayIcon.isSystemTrayAvailable():
            self.tray.show()
        if not background:
            self.window.show()  # does not activate / take focus
        else:
            log.info("Started in background (preview hidden)")
        self._update_tray()
        self._watchdog.start()
        if self.diag:
            self._diag_timer.start()
            QTimer.singleShot(10_000, self._log_resources)
        log.info("%s %s started", APP_NAME, __version__)

    def quit(self) -> None:
        self.qapp.quit()

    def _shutdown(self) -> None:
        if self._quitting:
            return
        self._quitting = True
        self._save_geometry()
        self.tray.hide()
        if self.toast is not None:
            self.toast.hide()
        if self.hud is not None:
            self.hud.hide()
        if self.settings is not None:
            self.settings.close()
        if not self.worker.stop(3000):
            # A driver call is hanging. Settings are saved; Windows releases the
            # camera when the process exits.
            log.error("Camera thread did not stop in time; forcing exit")
            logging.shutdown()
            os._exit(0)
        log.info("%s stopped", APP_NAME)

    def _resolve_camera_by_name(self) -> None:
        """If cameras were re-plugged in a different order, follow the saved name."""
        names = list_cameras()
        self._camera_names = names
        if self.cfg.camera_name and self.cfg.camera_name in names:
            self.cfg.camera_index = names.index(self.cfg.camera_name)

    def _camera_name(self) -> str:
        """Name of the configured camera (key for its saved hardware settings)."""
        if self.cfg.camera_name:
            return self.cfg.camera_name
        names, i = self._camera_names, self.cfg.camera_index
        return names[i] if 0 <= i < len(names) else f"Camera {i}"

    # ------------------------------------------------------------------ #
    # Window geometry
    # ------------------------------------------------------------------ #
    def _restore_geometry(self) -> None:
        cfg = self.cfg
        w, h = cfg.window_w, cfg.window_h
        rect = None
        if cfg.window_x is not None and cfg.window_y is not None:
            saved = QRect(cfg.window_x, cfg.window_y, w, h)
            for screen in QGuiApplication.screens():
                avail = screen.availableGeometry()
                inter = avail.intersected(saved)
                if inter.width() >= 60 and inter.height() >= 40:
                    # Visible enough – nudge fully onto that screen.
                    x = min(max(saved.x(), avail.left()), avail.right() - w + 1)
                    y = min(max(saved.y(), avail.top()), avail.bottom() - h + 1)
                    rect = QRect(x, y, w, h)
                    break
        if rect is None:
            rect = self._default_rect(w, h)
        self.window.setGeometry(rect)

    @staticmethod
    def _default_rect(w: int, h: int) -> QRect:
        screen = QGuiApplication.primaryScreen()
        avail = screen.availableGeometry() if screen else QRect(0, 0, 1280, 720)
        return QRect(avail.right() - w - SCREEN_MARGIN + 1, avail.top() + SCREEN_MARGIN, w, h)

    def _save_geometry(self) -> None:
        """Debounced save of window geometry (and of live zoom/pan changes)."""
        g = self.window.geometry()
        cfg = self.cfg
        dirty, self._prefs_dirty = self._prefs_dirty, False
        if (cfg.window_x, cfg.window_y, cfg.window_w, cfg.window_h) != (g.x(), g.y(), g.width(), g.height()):
            cfg.window_x, cfg.window_y, cfg.window_w, cfg.window_h = g.x(), g.y(), g.width(), g.height()
            dirty = True
        if dirty:
            self.store.save(cfg)

    def reset_geometry(self) -> None:
        self.window.setGeometry(self._default_rect(Config.window_w, Config.window_h))
        self._save_geometry()

    # ------------------------------------------------------------------ #
    # Worker signals
    # ------------------------------------------------------------------ #
    def _on_frame(self) -> None:
        frame = self.worker.take_frame()
        if frame is not None and self.state == "live":
            self._last_frame_at = time.monotonic()
            if self._stalled:
                self._stalled = False
                self.window.set_state(self.state, self.message)
            if self._raw_size != self.worker.actual_size:
                self._raw_size = self.worker.actual_size
                self._sync_window_view()
            self.window.set_frame(frame)

    def _on_state(self, state: str, message: str) -> None:
        self.state, self.message = state, message
        self._last_frame_at = time.monotonic()
        self._stalled = False
        self.window.set_state(state, message)
        self._update_tray()
        self.status_changed.emit()
        if state in ("error", "reconnecting") and message:
            log.info("Camera state: %s – %s", state, message)
        if state == "live" and message:
            # Informational notes over live video fade out after a few seconds.
            QTimer.singleShot(6000, partial(self._clear_live_note, message))

    def _clear_live_note(self, message: str) -> None:
        if self.state == "live" and self.message == message:
            self.message = ""
            self.window.set_state("live", "")

    def _check_stall(self) -> None:
        if (
            not self._stalled
            and self.state == "live"
            and self.window.isVisible()
            and time.monotonic() - self._last_frame_at > STALL_WARN_S
        ):
            self._stalled = True
            self.window.set_state("reconnecting", "Camera not responding…")

    def _on_display_size(self, w: int, h: int) -> None:
        self.worker.set_display(w, h, self.cfg.crop_to_fill)

    def _on_motion(self) -> None:
        if self.window.isVisible():
            self.window.show_motion(MOTION_HOLD_S)
            return
        self._tray_alert_timer.start()
        self.tray.setIcon(icons.icon("motion"))
        self.tray.setToolTip(f"{APP_NAME} – motion detected")
        if self.cfg.show_on_motion:
            self._auto_shown = True
            self.show_preview()
            self.window.show_motion(MOTION_HOLD_S)
            self._auto_hide_timer.start()

    # ------------------------------------------------------------------ #
    # Show / hide
    # ------------------------------------------------------------------ #
    def show_preview(self) -> None:
        if not self.window.isVisible():
            self._last_frame_at = time.monotonic()
            if self.message == HIDDEN_MSG:  # camera was released; worker reopens it now
                self.window.set_state("starting", "Starting camera…")
            self.window.show()  # WA_ShowWithoutActivating: no focus change
            if self.cfg.always_on_top:
                win32.set_topmost(int(self.window.winId()), True)
        self.worker.set_visible(True)
        self._update_tray()

    def hide_preview(self) -> None:
        if not self.tray.isVisible():
            # Without a tray icon there would be no way to bring the preview back.
            log.warning("System tray unavailable; not hiding the preview")
            return
        self._auto_shown = False
        self._auto_hide_timer.stop()
        self._save_geometry()
        self.window.hide()
        self.window.set_frame(None)
        self.worker.set_visible(False)
        self._update_tray()

    def _auto_hide(self) -> None:
        if self._auto_shown:
            self.hide_preview()

    def _cancel_auto_hide(self) -> None:
        self._auto_shown = False
        self._auto_hide_timer.stop()

    def _on_tray_activated(self, reason: QSystemTrayIcon.ActivationReason) -> None:
        if reason == QSystemTrayIcon.ActivationReason.Trigger:
            if self.window.isVisible():
                self.hide_preview()
            else:
                self._cancel_auto_hide()
                self.show_preview()

    def status_label(self) -> str:
        if self.state == "paused" and self.message == HIDDEN_MSG:
            return "Camera off while hidden"
        return STATE_LABELS.get(self.state, self.state)

    def _update_tray(self) -> None:
        if self._alert_active:
            self.tray.setIcon(icons.icon("motion"))
            self.tray.setToolTip(f"{APP_NAME} – someone is watching you")
            return
        if self._tray_alert_timer.isActive():
            return
        self.tray.setIcon(icons.icon(self.state))
        label = self.status_label()
        size = self.worker.actual_size
        detail = f" ({size[0]}×{size[1]} @ {self.cfg.fps} fps)" if self.state == "live" and size else ""
        hidden = "" if self.window.isVisible() else " · preview hidden"
        self.tray.setToolTip(f"{APP_NAME} – {label}{detail}{hidden}")

    # ------------------------------------------------------------------ #
    # Settings changes
    # ------------------------------------------------------------------ #
    def _save(self) -> None:
        self.store.save(self.cfg)
        self.changed.emit()

    def _apply_motion(self) -> None:
        c = self.cfg
        self.worker.detector.set_ignore_zones(c.ignore_zones)
        self.worker.set_motion(c.motion_enabled, c.motion_sensitivity, c.motion_cooldown, c.monitor_while_hidden)
        self._sync_zones()

    def set_camera(self, index: int, name: str) -> None:
        self.cfg.camera_index, self.cfg.camera_name = index, name
        self._save()
        self.worker.set_camera_props(self.cfg.camera_props.get(name, {}))
        self.worker.configure_camera(index, self.cfg.width, self.cfg.height, self.cfg.fps)

    def set_resolution(self, w: int, h: int) -> None:
        self.cfg.width, self.cfg.height = w, h
        self._save()
        self.worker.configure_camera(self.cfg.camera_index, w, h, self.cfg.fps)

    def set_fps(self, fps: int) -> None:
        self.cfg.fps = fps
        self._save()
        self.worker.set_fps(fps)
        self._update_tray()

    def set_cfg(self, name: str, value, apply=None) -> None:  # type: ignore[no-untyped-def]
        setattr(self.cfg, name, value)
        self._save()
        if apply:
            apply()

    def set_on_top(self, on: bool) -> None:
        self.set_cfg("always_on_top", on)
        self.window.set_on_top(on)

    def set_opacity(self, pct: int) -> None:
        self.set_cfg("opacity", pct)
        self.window.setWindowOpacity(pct / 100)

    def set_crop(self, crop: bool) -> None:
        self.set_cfg("crop_to_fill", crop)
        self.worker.set_display(*self.window.physical_size(), crop)
        self._sync_window_view()

    # View (flip / digital zoom) ------------------------------------------
    def _apply_view(self) -> None:
        c = self.cfg
        self.worker.set_view(c.flip_h, c.flip_v, c.zoom, c.pan_x, c.pan_y)
        self._sync_window_view()

    def _sync_window_view(self) -> None:
        c = self.cfg
        self.window.set_view_params(self._raw_size, c.crop_to_fill, c.zoom, c.pan_x, c.pan_y, c.flip_h, c.flip_v)

    def set_view(self, **changes) -> None:  # type: ignore[no-untyped-def]
        for name, value in changes.items():
            setattr(self.cfg, name, value)
        self._save()
        self._apply_view()

    def set_zoom(self, zoom: float) -> None:
        self.set_view(zoom=zoom, pan_x=0.5, pan_y=0.5)

    def _on_view_changed(self, zoom: float, pan_x: float, pan_y: float) -> None:
        """Live Ctrl+wheel / Ctrl+drag: apply now, save debounced."""
        c = self.cfg
        c.zoom, c.pan_x, c.pan_y = round(zoom, 3), round(pan_x, 4), round(pan_y, 4)
        self.worker.set_view(c.flip_h, c.flip_v, c.zoom, c.pan_x, c.pan_y)
        self._sync_window_view()
        self._prefs_dirty = True
        self._save_timer.start()

    # Ignore zones --------------------------------------------------------
    def _sync_zones(self) -> None:
        c = self.cfg
        self.window.set_zones(c.ignore_zones, c.motion_enabled)

    def _on_zones_changed(self, zones: list[list[float]]) -> None:
        self.cfg.ignore_zones = [list(z) for z in zones]
        self._save()
        self.worker.detector.set_ignore_zones(self.cfg.ignore_zones)
        self.worker.set_presence_zones(self.cfg.ignore_zones)
        self._sync_zones()

    def clear_zones(self) -> None:
        self._on_zones_changed([])

    def edit_zones(self) -> None:
        if not self.window.isVisible():
            self._cancel_auto_hide()
            self.show_preview()
        self._sync_zones()
        self.window.set_zone_edit(True)

    # Hardware camera settings ---------------------------------------------
    def _on_camera_props(self, props: dict[str, float]) -> None:
        saved = self.cfg.camera_props
        name = self._camera_name()
        saved.pop(name, None)
        saved[name] = dict(props)
        while len(saved) > MAX_CAMERAS_WITH_PROPS:
            saved.pop(next(iter(saved)))  # drop the oldest camera
        self._save()

    def forget_camera_props(self) -> None:
        self.cfg.camera_props.pop(self._camera_name(), None)
        self._save()
        self.worker.set_camera_props({})

    def set_paused(self, paused: bool) -> None:
        self.worker.set_paused(paused)
        QTimer.singleShot(0, self.changed.emit)

    def set_motion_option(self, name: str, value) -> None:  # type: ignore[no-untyped-def]
        self.set_cfg(name, value, self._apply_motion)
        if name == "motion_sensitivity":
            self._apply_presence()

    def set_release_when_hidden(self, on: bool) -> None:
        self.set_cfg("release_when_hidden", on, lambda: self.worker.set_release_when_hidden(on))

    def open_camera_settings(self) -> None:
        self.worker.request_settings_dialog()

    def camera_names(self) -> list[str]:
        self._camera_names = list_cameras()  # DirectShow enumeration: does not open any camera
        return self._camera_names

    def set_theme(self, name: str) -> None:
        self.set_cfg("theme", name)
        theme.set_current(theme.palette_for(name))
        if self.settings is not None:
            self.settings.apply_theme()
        if self.toast is not None:
            self.toast.update()

    def set_autostart(self, on: bool) -> None:
        win32.set_autostart(on)
        self.changed.emit()

    def open_log_folder(self) -> None:
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(log_dir())))

    def open_settings_folder(self) -> None:
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(config_dir())))

    # ------------------------------------------------------------------ #
    # Watch my back
    # ------------------------------------------------------------------ #
    def _apply_presence(self) -> None:
        c = self.cfg
        self.worker.set_presence_zones(c.ignore_zones)
        self.worker.set_body_outline(c.body_outline)
        self.worker.set_presence(c.presence_enabled, c.watch_dwell_s, c.watch_person_without_face,
                                 STRICTNESS.get(c.face_strictness, STRICTNESS["normal"]),
                                 c.alert_cooldown_s, c.motion_sensitivity)
        if not c.presence_enabled:
            self._on_presence(PresenceState())
        self.window.set_presence(self.presence, c.presence_enabled)

    def set_presence_enabled(self, on: bool) -> None:
        self.set_cfg("presence_enabled", on, self._apply_presence)
        self.status_changed.emit()
        if on and not self.profile.enrolled:
            self.open_settings("face")  # first use: learn the user's face

    def set_presence_option(self, name: str, value) -> None:  # type: ignore[no-untyped-def]
        self.set_cfg(name, value, self._apply_presence)

    def forget_face(self) -> None:
        self.profile.forget()
        self.worker.set_profile(self.profile)
        log.info("Face profile deleted")
        self.changed.emit()
        self.status_changed.emit()

    def watch_status(self) -> tuple[str, str]:
        """(severity, text) describing Watch my back, for the Settings window."""
        if not self.cfg.presence_enabled:
            return "off", "Off – turn on to be told when someone stands behind you."
        if self.presence.error:
            return "error", self.presence.error
        if self._alert_active or (self.presence.watching and self.presence.watch_seconds >= 1):
            return "alert", "Someone is behind you"
        if self.state != "live" and self.window.isVisible():
            return "warning", "Waiting for the camera…"
        if self.snoozed:
            mins = max(1, round((self._snoozed_until - time.monotonic()) / 60))
            return "warning", f"On – alerts snoozed for {mins} more min."
        if not self.profile.enrolled:
            return "warning", "On – set up your face so DeskGuard knows which person is you."
        if self.presence.me_face is not None:
            return "ok", "On – you are recognised, watching behind you."
        return "ok", "On – watching behind you."

    def _on_presence(self, state: PresenceState) -> None:
        was = (self.presence.watching, bool(self.presence.me_face), self.presence.error)
        self.presence = state
        self.window.set_presence(state, self.cfg.presence_enabled)
        if self.toast is not None and self.toast.isVisible() and self.toast.kind != "test":
            if state.watching:
                self.toast.update_seconds(state.watch_seconds)
            else:
                self.toast.person_left()
        if was != (state.watching, bool(state.me_face), state.error):
            self.status_changed.emit()

    def _on_watcher_alert(self, kind: str, seconds: float) -> None:
        log.info("Watch my back: someone behind the user (%s, %.0f s)", kind, seconds)
        self._alert_active = True
        self._update_tray()
        self.window.flash_alert(8.0)
        self._toast().show_alert(kind, seconds, self._preview_screen())
        self.worker.set_gesture_armed(True)  # nod / shake now answer the alert
        self.status_changed.emit()

    def _on_watcher_cleared(self) -> None:
        self._alert_active = False
        self._update_tray()
        if self.toast is not None:
            self.toast.person_left()
        self.status_changed.emit()

    def test_alert(self) -> None:
        self._toast().show_alert("test", 0, self._preview_screen())

    def _toast(self):  # type: ignore[no-untyped-def]
        if self.toast is None:
            from deskguard.ui.toast import ToastWindow

            self.toast = ToastWindow()
            self.toast.setWindowIcon(icons.icon())
            self.toast.show_preview_requested.connect(self._show_from_toast)
            self.toast.dismissed.connect(lambda: self.worker.set_gesture_armed(False))
        return self.toast

    # ------------------------------------------------------------------ #
    # Gestures and their actions
    # ------------------------------------------------------------------ #
    def _apply_gestures(self) -> None:
        c = self.cfg
        self.worker.set_gestures(c.hand_gestures, c.face_gestures, c.look_away_enabled, c.look_away_s)

    def set_gesture_option(self, name: str, value) -> None:  # type: ignore[no-untyped-def]
        self.set_cfg(name, value, self._apply_gestures)

    def set_gesture_action(self, gesture: str, action: str) -> None:
        self.cfg.gesture_actions = {**self.cfg.gesture_actions, gesture: action}
        self._save()

    def _on_gesture(self, name: str) -> None:
        if name in HAND_GESTURES:
            action = self.cfg.gesture_actions.get(name, "none")
        else:
            action = {"nod": "dismiss", "shake": "snooze", "look_away": "privacy"}.get(name, "none")
        log.info("Gesture %s -> %s", name, action)
        self.run_action(action, name)

    def run_action(self, action: str, gesture: str = "") -> None:
        glyph = "face" if gesture in ("nod", "shake", "look_away") else "hand"
        if action == "toggle_preview":
            if self.window.isVisible():
                self.hide_preview()
                self.show_hud(glyph, "Preview hidden – still watching")
            else:
                self._cancel_auto_hide()
                self.show_preview()
                self.show_hud(glyph, "Preview shown")
        elif action == "dismiss":
            had_alert = self.toast is not None and self.toast.isVisible() and self.toast.kind != "test"
            self.dismiss_alert()
            self.show_hud("check" if had_alert else glyph, "Alert dismissed" if had_alert else "No alert to dismiss")
        elif action == "snooze":
            self.snooze_alerts(SNOOZE_MIN)
            self.show_hud("snooze", f"Alerts snoozed for {SNOOZE_MIN} min")
        elif action == "toggle_watch":
            on = not self.cfg.presence_enabled
            self.set_cfg("presence_enabled", on, self._apply_presence)  # no Settings window from a gesture
            self.status_changed.emit()
            self.show_hud("shield", "Watch my back on" if on else "Watch my back paused")
        elif action == "privacy":
            self.privacy_action()

    def privacy_action(self) -> None:
        what = self.cfg.privacy_action
        if what == "lock":
            win32.lock_workstation()
        elif what == "hide_preview":
            self.hide_preview()
            self.show_hud("lock", "Preview hidden for privacy")
        else:
            win32.show_desktop()
            self.show_hud("lock", "Desktop shown for privacy")

    def dismiss_alert(self) -> None:
        if self.toast is not None:
            self.toast.dismiss()
        self.worker.set_gesture_armed(False)
        if self._alert_active:
            self._alert_active = False
            self._update_tray()
            self.status_changed.emit()

    def snooze_alerts(self, minutes: float) -> None:
        self.worker.snooze(minutes * 60)
        self._snoozed_until = time.monotonic() + minutes * 60
        self.dismiss_alert()
        QTimer.singleShot(int(minutes * 60 * 1000) + 100, self.status_changed.emit)
        self.status_changed.emit()

    def resume_alerts(self) -> None:
        self.worker.snooze(0)
        self._snoozed_until = 0.0
        self.status_changed.emit()

    @property
    def snoozed(self) -> bool:
        return time.monotonic() < self._snoozed_until

    def show_hud(self, glyph: str, text: str) -> None:
        if self.hud is None:
            from deskguard.ui.toast import HudPill

            self.hud = HudPill()
        self.hud.show_message(glyph, text, self._preview_screen())

    def _show_from_toast(self) -> None:
        self._cancel_auto_hide()
        self.show_preview()
        self.window.flash_alert(5.0)

    def _preview_screen(self) -> QRect:
        screen = QGuiApplication.screenAt(self.window.geometry().center()) or QGuiApplication.primaryScreen()
        return screen.availableGeometry() if screen else QRect(0, 0, 1280, 720)

    # ------------------------------------------------------------------ #
    # Settings window (created on demand, destroyed when closed)
    # ------------------------------------------------------------------ #
    def open_settings(self, page: str | None = None) -> None:
        if self.settings is None:
            from deskguard.ui.settings_window import SettingsWindow

            self.settings = SettingsWindow(self)
            self.settings.destroyed.connect(self._settings_closed)
        if page:
            self.settings.show_page(page)
        self.settings.show()
        self.settings.raise_()
        self.settings.activateWindow()

    def _settings_closed(self) -> None:
        self.settings = None

    def _log_resources(self) -> None:
        ws, private = win32.memory_usage_mb()
        log.info(
            "resources: working_set=%.1f MB private=%.1f MB cpu=%.2f%% state=%s visible=%s motion=%s watch=%s",
            ws, private, self.cpu.sample(), self.state, self.window.isVisible(), self.cfg.motion_enabled,
            self.cfg.presence_enabled,
        )

    # ------------------------------------------------------------------ #
    # Quick menu (right-click on the preview or the tray icon). Everything
    # else lives in the Settings window.
    # ------------------------------------------------------------------ #
    def _popup_menu(self, pos: QPoint) -> None:
        self.menu.popup(pos)

    def _action(self, menu: QMenu, text: str, slot, checked: bool | None = None,  # type: ignore[no-untyped-def]
                group: QActionGroup | None = None, enabled: bool = True) -> QAction:
        act = QAction(text, menu)
        if checked is not None:
            act.setCheckable(True)
            act.setChecked(checked)
        if checked is not None and group is None:
            act.toggled.connect(slot)  # slot receives the new on/off state
        else:
            act.triggered.connect(lambda _checked=False, s=slot: s())
        if group is not None:
            group.addAction(act)
        act.setEnabled(enabled)
        menu.addAction(act)
        return act

    def _build_menu(self) -> None:
        m, c = self.menu, self.cfg
        m.clear()

        size = self.worker.actual_size
        status = self.status_label()
        if self.state == "live" and size:
            status += f" · {size[0]}×{size[1]}"
        m.addAction(icons.icon(self.state), status).setEnabled(False)
        m.addSeparator()

        visible = self.window.isVisible()
        self._action(m, "Run in background (hide preview)" if visible else "Show preview",
                     self.hide_preview if visible else lambda: (self._cancel_auto_hide(), self.show_preview()))
        paused = self.worker.paused
        self._action(m, "Resume camera" if paused else "Pause camera (turns camera off)",
                     partial(self.set_paused, not paused))
        m.addSeparator()

        self._action(m, "Watch my back", self.set_presence_enabled, c.presence_enabled)
        if c.presence_enabled:
            if self.snoozed:
                self._action(m, "Resume alerts now", self.resume_alerts)
            else:
                self._action(m, f"Snooze alerts for {SNOOZE_MIN} min", partial(self.snooze_alerts, SNOOZE_MIN))
        self._action(m, "Hand gestures", lambda on: self.set_gesture_option("hand_gestures", on), c.hand_gestures)
        self._action(m, "Motion detection", lambda on: self.set_motion_option("motion_enabled", on), c.motion_enabled)
        self._action(m, "Edit ignore zones…", self.edit_zones, enabled=self._raw_size is not None)
        self._action(m, "Mirror image", lambda on: self.set_view(flip_h=on), c.flip_h)
        m.addSeparator()

        self._action(m, "Settings…", self.open_settings)
        self._action(m, f"Exit {APP_NAME}", self.quit)
