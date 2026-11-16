"""Wires the preview window, camera worker, tray icon, menu and settings together."""

from __future__ import annotations

import logging
import os
import time
from functools import partial

from PySide6.QtCore import QObject, QPoint, QRect, QTimer, QUrl
from PySide6.QtGui import QAction, QActionGroup, QDesktopServices, QGuiApplication
from PySide6.QtWidgets import QApplication, QMenu, QSystemTrayIcon

from deskguard import APP_NAME, __version__, icons, win32
from deskguard.camera import list_cameras
from deskguard.camera_worker import HIDDEN_MSG, CameraWorker
from deskguard.config import (
    COOLDOWN_CHOICES,
    FPS_CHOICES,
    OPACITY_CHOICES,
    RESOLUTIONS,
    SENSITIVITY_CHOICES,
    Config,
    ConfigStore,
    log_dir,
)
from deskguard.preview_window import PreviewWindow

log = logging.getLogger(__name__)

MOTION_HOLD_S = 3.0        # how long the red MOTION indicator stays
TRAY_ALERT_S = 5.0         # how long the tray icon stays red after hidden motion
AUTO_HIDE_S = 10.0         # auto-shown preview hides again after this
STALL_WARN_S = 4.0         # no frames for this long while live => warn
DIAG_INTERVAL_MS = 60_000
SCREEN_MARGIN = 16

STATE_LABELS = {
    "starting": "Starting",
    "live": "Camera live",
    "paused": "Paused",
    "reconnecting": "Reconnecting",
    "error": "Camera error",
    "stopped": "Stopped",
}


class DeskGuardApp(QObject):
    def __init__(self, qapp: QApplication, store: ConfigStore, diag: bool = False) -> None:
        super().__init__()
        self.qapp = qapp
        self.store = store
        self.cfg: Config = store.load()
        self.diag = diag  # --diag: log RAM/CPU every minute

        self.state, self.message = "starting", ""
        self._camera_names: list[str] = []
        self._last_frame_at = time.monotonic()
        self._stalled = False
        self._auto_shown = False
        self._quitting = False
        self.cpu = win32.CpuSampler()

        self.window = PreviewWindow()
        self.window.setWindowIcon(icons.icon())
        self.worker = CameraWorker()

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

        self.worker.frame_ready.connect(self._on_frame)
        self.worker.state_changed.connect(self._on_state)
        self.worker.motion_detected.connect(self._on_motion)

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
        self._resolve_camera_by_name()
        self._restore_geometry()
        self.window.setWindowOpacity(cfg.opacity / 100)
        if not cfg.always_on_top:
            self.window.set_on_top(False)

        self.worker.configure_camera(cfg.camera_index, cfg.width, cfg.height, cfg.fps)
        self.worker.set_display(*self.window.physical_size(), cfg.crop_to_fill)
        self._apply_motion()
        self.worker.set_release_when_hidden(cfg.release_when_hidden)
        self.worker.start()

        self.window.show()  # does not activate / take focus
        if QSystemTrayIcon.isSystemTrayAvailable():
            self.tray.show()
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
        if not self.worker.stop(3000):
            # A driver call is hanging. Settings are saved; Windows releases the
            # camera when the process exits.
            log.error("Camera thread did not stop in time; forcing exit")
            logging.shutdown()
            os._exit(0)
        log.info("%s stopped", APP_NAME)

    def _resolve_camera_by_name(self) -> None:
        """If cameras were re-plugged in a different order, follow the saved name."""
        if not self.cfg.camera_name:
            return
        names = list_cameras()
        self._camera_names = names
        if self.cfg.camera_name in names:
            self.cfg.camera_index = names.index(self.cfg.camera_name)

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
        g = self.window.geometry()
        cfg = self.cfg
        if (cfg.window_x, cfg.window_y, cfg.window_w, cfg.window_h) != (g.x(), g.y(), g.width(), g.height()):
            cfg.window_x, cfg.window_y, cfg.window_w, cfg.window_h = g.x(), g.y(), g.width(), g.height()
            self.store.save(cfg)

    def _reset_geometry(self) -> None:
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
            self.window.set_frame(frame)

    def _on_state(self, state: str, message: str) -> None:
        self.state, self.message = state, message
        self._last_frame_at = time.monotonic()
        self._stalled = False
        self.window.set_state(state, message)
        self._update_tray()
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

    def _status_label(self) -> str:
        if self.state == "paused" and self.message == HIDDEN_MSG:
            return "Camera off while hidden"
        return STATE_LABELS.get(self.state, self.state)

    def _update_tray(self) -> None:
        if self._tray_alert_timer.isActive():
            return
        self.tray.setIcon(icons.icon(self.state))
        label = self._status_label()
        size = self.worker.actual_size
        detail = f" ({size[0]}×{size[1]} @ {self.cfg.fps} fps)" if self.state == "live" and size else ""
        hidden = "" if self.window.isVisible() else " · preview hidden"
        self.tray.setToolTip(f"{APP_NAME} – {label}{detail}{hidden}")

    # ------------------------------------------------------------------ #
    # Settings changes
    # ------------------------------------------------------------------ #
    def _save(self) -> None:
        self.store.save(self.cfg)

    def _apply_motion(self) -> None:
        c = self.cfg
        self.worker.set_motion(c.motion_enabled, c.motion_sensitivity, c.motion_cooldown, c.monitor_while_hidden)

    def _set_camera(self, index: int, name: str) -> None:
        self.cfg.camera_index, self.cfg.camera_name = index, name
        self._save()
        self.worker.configure_camera(index, self.cfg.width, self.cfg.height, self.cfg.fps)

    def _set_resolution(self, w: int, h: int) -> None:
        self.cfg.width, self.cfg.height = w, h
        self._save()
        self.worker.configure_camera(self.cfg.camera_index, w, h, self.cfg.fps)

    def _set_fps(self, fps: int) -> None:
        self.cfg.fps = fps
        self._save()
        self.worker.set_fps(fps)
        self._update_tray()

    def _set_cfg(self, name: str, value, apply=None) -> None:  # type: ignore[no-untyped-def]
        setattr(self.cfg, name, value)
        self._save()
        if apply:
            apply()

    def _set_on_top(self, on: bool) -> None:
        self._set_cfg("always_on_top", on)
        self.window.set_on_top(on)

    def _set_opacity(self, pct: int) -> None:
        self._set_cfg("opacity", pct)
        self.window.setWindowOpacity(pct / 100)

    def _set_crop(self, crop: bool) -> None:
        self._set_cfg("crop_to_fill", crop)
        self.worker.set_display(*self.window.physical_size(), crop)

    def _set_paused(self, paused: bool) -> None:
        self.worker.set_paused(paused)

    def _log_resources(self) -> None:
        ws, private = win32.memory_usage_mb()
        log.info(
            "resources: working_set=%.1f MB private=%.1f MB cpu=%.2f%% state=%s visible=%s motion=%s",
            ws, private, self.cpu.sample(), self.state, self.window.isVisible(), self.cfg.motion_enabled,
        )

    # ------------------------------------------------------------------ #
    # Menu (shared by right-click on the preview and the tray icon)
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
        for child in m.findChildren(QMenu):
            child.deleteLater()

        size = self.worker.actual_size
        status = self._status_label()
        if self.state == "live" and size:
            status += f" · {size[0]}×{size[1]}"
        m.addAction(icons.icon(self.state), status).setEnabled(False)
        m.addSeparator()

        visible = self.window.isVisible()
        self._action(m, "Hide preview" if visible else "Show preview",
                     self.hide_preview if visible else lambda: (self._cancel_auto_hide(), self.show_preview()))
        paused = self.worker.paused
        self._action(m, "Resume camera" if paused else "Pause camera (turns camera off)",
                     partial(self._set_paused, not paused))
        self._action(m, "Turn camera off when hidden (after 1 min)",
                     lambda on: self._set_cfg("release_when_hidden", on,
                                              lambda: self.worker.set_release_when_hidden(on)),
                     c.release_when_hidden)
        m.addSeparator()

        # Camera
        cam_menu = m.addMenu("Camera")
        cam_menu.aboutToShow.connect(partial(self._fill_camera_menu, cam_menu))
        cam_menu.addAction("Searching…").setEnabled(False)

        res_menu = m.addMenu("Resolution")
        grp = QActionGroup(res_menu)
        for w, h in RESOLUTIONS:
            label = f"{w} × {h}" + ("  (recommended)" if (w, h) == (640, 480) else "")
            self._action(res_menu, label, partial(self._set_resolution, w, h), (c.width, c.height) == (w, h), grp)

        fps_menu = m.addMenu("Frame rate")
        grp = QActionGroup(fps_menu)
        for fps in FPS_CHOICES:
            self._action(fps_menu, f"{fps} fps", partial(self._set_fps, fps), c.fps == fps, grp)

        # Motion
        mo = m.addMenu("Motion detection")
        self._action(mo, "Enabled", lambda on: self._set_cfg("motion_enabled", on, self._apply_motion), c.motion_enabled)
        mo.addSeparator()
        sens = mo.addMenu("Sensitivity")
        grp = QActionGroup(sens)
        for s in SENSITIVITY_CHOICES:
            self._action(sens, s.capitalize(), partial(self._set_cfg, "motion_sensitivity", s, self._apply_motion),
                         c.motion_sensitivity == s, grp)
        cool = mo.addMenu("Cooldown")
        grp = QActionGroup(cool)
        for sec in COOLDOWN_CHOICES:
            self._action(cool, f"{sec} seconds", partial(self._set_cfg, "motion_cooldown", sec, self._apply_motion),
                         c.motion_cooldown == sec, grp)
        mo.addSeparator()
        self._action(mo, "Keep monitoring while hidden",
                     lambda on: self._set_cfg("monitor_while_hidden", on, self._apply_motion), c.monitor_while_hidden)
        self._action(mo, "Show preview on motion (while hidden)",
                     lambda on: self._set_cfg("show_on_motion", on), c.show_on_motion,
                     enabled=c.monitor_while_hidden)

        # Window
        wm = m.addMenu("Window")
        self._action(wm, "Always on top", self._set_on_top, c.always_on_top)
        self._action(wm, "Fill window (crop edges)", self._set_crop, c.crop_to_fill)
        op = wm.addMenu("Opacity")
        grp = QActionGroup(op)
        for pct in OPACITY_CHOICES:
            self._action(op, f"{pct}%", partial(self._set_opacity, pct), c.opacity == pct, grp)
        wm.addSeparator()
        self._action(wm, "Reset size and position", self._reset_geometry)

        if win32.IS_WINDOWS:
            self._action(m, "Start with Windows", win32.set_autostart, win32.autostart_enabled())

        m.addSeparator()
        self._action(m, "Open log folder", lambda: QDesktopServices.openUrl(QUrl.fromLocalFile(str(log_dir()))))
        self._action(m, f"Exit {APP_NAME}", self.quit)

    def _fill_camera_menu(self, menu: QMenu) -> None:
        menu.clear()
        names = list_cameras()  # DirectShow enumeration: does not open any camera
        self._camera_names = names
        if not names:
            menu.addAction("No cameras found").setEnabled(False)
            return
        grp = QActionGroup(menu)
        for i, name in enumerate(names):
            self._action(menu, name, partial(self._set_camera, i, name), i == self.cfg.camera_index, grp)
