"""DeskGuard Settings: a Windows 11-style window with a sidebar and pages.

Created only when opened and destroyed when closed (WA_DeleteOnClose), so it
costs nothing while DeskGuard runs in the background. Every control calls the
same DeskGuardApp methods as the quick menu; `sync()` refreshes all controls
from the saved settings whenever something changes elsewhere.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QCloseEvent, QColor, QPainter, QPaintEvent
from PySide6.QtWidgets import (
    QButtonGroup,
    QComboBox,
    QFrame,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QScrollArea,
    QSlider,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

from deskguard import APP_NAME, __version__, icons, win32
from deskguard.config import (
    ALERT_COOLDOWN_CHOICES,
    COOLDOWN_CHOICES,
    DWELL_CHOICES,
    FPS_CHOICES,
    OPACITY_CHOICES,
    RESOLUTIONS,
    SENSITIVITY_CHOICES,
    config_dir,
    log_dir,
)
from deskguard.ui import theme
from deskguard.ui.face_setup import FaceSetupPage
from deskguard.ui.widgets import GlyphLabel, InfoBar, NavButton, SettingCard, ToggleSwitch, label

PAGES = (
    ("watch", "shield", "Watch my back"),
    ("motion", "motion", "Motion"),
    ("camera", "camera", "Camera"),
    ("view", "view", "View"),
    ("window", "window", "Window & app"),
    ("privacy", "lock", "Privacy"),
)


def _duration(seconds: int) -> str:
    if seconds < 60:
        return f"{seconds} seconds"
    minutes = seconds // 60
    return f"{minutes} minute{'s' if minutes != 1 else ''}"


class StatusDot(QWidget):
    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setFixedSize(10, 10)
        self.color = QColor("#9ca3af")

    def paintEvent(self, event: QPaintEvent) -> None:  # noqa: N802
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(self.color)
        p.drawEllipse(self.rect().adjusted(1, 1, -1, -1))
        p.end()


class SettingsWindow(QWidget):
    def __init__(self, app) -> None:  # type: ignore[no-untyped-def]
        super().__init__(None)
        self.app = app
        self.setObjectName("SettingsRoot")
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground)
        self.setWindowTitle(f"{APP_NAME} Settings")
        self.setWindowIcon(icons.icon())
        self.resize(900, 620)
        self.setMinimumSize(780, 520)
        self._syncing = False
        self.c: dict[str, Any] = {}  # controls by setting name

        root = QHBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)
        root.addWidget(self._build_sidebar())
        self.stack = QStackedWidget()
        root.addWidget(self.stack, 1)

        self.pages: dict[str, QWidget] = {
            "watch": self._page_watch(),
            "motion": self._page_motion(),
            "camera": self._page_camera(),
            "view": self._page_view(),
            "window": self._page_window(),
            "privacy": self._page_privacy(),
        }
        for page in self.pages.values():
            self.stack.addWidget(page)
        self.face_page = FaceSetupPage(app)
        self.face_page.finished.connect(self._face_setup_finished)
        self.stack.addWidget(self.face_page)

        app.changed.connect(self.sync)
        app.status_changed.connect(self._sync_status)
        self.apply_theme()
        self.sync()
        self.show_page("watch")

    # ------------------------------------------------------------------ #
    # Layout helpers
    # ------------------------------------------------------------------ #
    def _build_sidebar(self) -> QFrame:
        side = QFrame()
        side.setObjectName("Sidebar")
        side.setFixedWidth(250)
        col = QVBoxLayout(side)
        col.setContentsMargins(12, 18, 12, 14)
        col.setSpacing(2)

        head = QHBoxLayout()
        head.setContentsMargins(10, 0, 0, 14)
        logo = QLabel()
        logo.setPixmap(icons.icon().pixmap(32, 32))
        head.addWidget(logo)
        names = QVBoxLayout()
        names.setSpacing(0)
        names.addWidget(label(APP_NAME, "appName"))
        names.addWidget(label(f"Version {__version__}", "caption"))
        head.addLayout(names, 1)
        col.addLayout(head)

        self.nav: dict[str, NavButton] = {}
        group = QButtonGroup(side)
        for key, glyph, text in PAGES:
            btn = NavButton(glyph, text)
            btn.clicked.connect(lambda _c=False, k=key: self.show_page(k))
            group.addButton(btn)
            self.nav[key] = btn
            col.addWidget(btn)
        col.addStretch(1)

        status = QHBoxLayout()
        status.setContentsMargins(12, 0, 0, 0)
        status.setSpacing(8)
        self.status_dot = StatusDot()
        status.addWidget(self.status_dot)
        self.status_text = label("", "caption")
        status.addWidget(self.status_text, 1)
        col.addLayout(status)
        return side

    def _page(self, title: str, subtitle: str = "") -> tuple[QScrollArea, QVBoxLayout]:
        area = QScrollArea()
        area.setWidgetResizable(True)
        area.setFrameShape(QFrame.Shape.NoFrame)
        area.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        inner = QWidget()
        inner.setObjectName("Page")
        col = QVBoxLayout(inner)
        col.setContentsMargins(36, 28, 36, 28)
        col.setSpacing(6)
        col.addWidget(label(title, "pageTitle"))
        if subtitle:
            col.addWidget(label(subtitle, "caption", wrap=True))
        col.addSpacing(12)
        area.setWidget(inner)
        return area, col

    @staticmethod
    def _finish(col: QVBoxLayout) -> None:
        col.addStretch(1)

    def _toggle(self, key: str, on_change: Callable[[bool], None]) -> ToggleSwitch:
        sw = ToggleSwitch()
        sw.toggled.connect(lambda on: None if self._syncing else on_change(on))
        self.c[key] = sw
        return sw

    def _combo(self, key: str, items: list[tuple[str, Any]], on_change: Callable[[Any], None]) -> QComboBox:
        box = QComboBox()
        for text, value in items:
            box.addItem(text, value)
        box.currentIndexChanged.connect(
            lambda i: None if self._syncing or i < 0 else on_change(box.itemData(i)))
        self.c[key] = box
        return box

    @staticmethod
    def _button(text: str, slot: Callable[[], None], accent: bool = False, danger: bool = False) -> QPushButton:
        btn = QPushButton(text)
        if accent:
            btn.setProperty("accent", True)
        if danger:
            btn.setProperty("danger", True)
        btn.clicked.connect(lambda _c=False: slot())
        return btn

    @staticmethod
    def _set_combo(box: QComboBox, value: Any) -> None:
        idx = box.findData(value)
        if idx >= 0:
            box.setCurrentIndex(idx)

    # ------------------------------------------------------------------ #
    # Pages
    # ------------------------------------------------------------------ #
    def _page_watch(self) -> QWidget:
        app = self.app
        area, col = self._page("Watch my back",
                               "DeskGuard learns your face, ignores you, and tells you when someone stays behind you.")
        hero = QFrame()
        hero.setObjectName("Hero")
        row = QHBoxLayout(hero)
        row.setContentsMargins(20, 18, 20, 18)
        row.setSpacing(18)
        self.hero_glyph = GlyphLabel("shield", 48)
        row.addWidget(self.hero_glyph)
        text = QVBoxLayout()
        text.setSpacing(2)
        text.addWidget(label("Watch my back", "heroTitle"))
        self.hero_status = label("", "caption", wrap=True)
        text.addWidget(self.hero_status)
        row.addLayout(text, 1)
        row.addWidget(self._toggle("presence_enabled", app.set_presence_enabled))
        col.addWidget(hero)
        col.addSpacing(6)

        self.face_info = InfoBar("info", "Set up face recognition so DeskGuard knows which person is you. "
                                         "Until then, the person closest to the camera is treated as you.")
        col.addWidget(self.face_info)

        self.face_card = SettingCard("face", "Your face", "")
        self.face_setup_btn = self.face_card.add_control(self._button("Set up face", self.start_face_setup, accent=True))
        self.face_forget_btn = self.face_card.add_control(self._button("Forget", self._forget_face, danger=True))
        col.addWidget(self.face_card)

        col.addWidget(label("Alerts", "section"))
        col.addWidget(SettingCard(
            "eye", "Alert when someone stays behind you for",
            "People walking past are ignored. The alert is a silent notification that never takes your keyboard.",
            self._combo("watch_dwell_s", [(f"{s} seconds", s) for s in DWELL_CHOICES],
                        lambda v: app.set_presence_option("watch_dwell_s", v))))
        col.addWidget(SettingCard(
            "motion", "Also detect people whose face isn't visible",
            "Notices a person-shaped change behind you even when they look away. Catches more, "
            "with an occasional false alarm (e.g. a moved chair).",
            self._toggle("watch_person_without_face", lambda on: app.set_presence_option("watch_person_without_face", on))))
        col.addWidget(SettingCard(
            "face", "Face matching", "Strict avoids mistaking others for you; relaxed copes better with poor light.",
            self._combo("face_strictness", [("Relaxed", "relaxed"), ("Normal", "normal"), ("Strict", "strict")],
                        lambda v: app.set_presence_option("face_strictness", v))))
        col.addWidget(SettingCard(
            "alert", "Don't repeat alerts within", "",
            self._combo("alert_cooldown_s", [(_duration(s), s) for s in ALERT_COOLDOWN_CHOICES],
                        lambda v: app.set_presence_option("alert_cooldown_s", v))))
        col.addWidget(SettingCard("shield", "Preview the alert", "Shows the notification exactly as you'll see it.",
                                  self._button("Test alert", app.test_alert)))
        self._finish(col)
        return area

    def _page_motion(self) -> QWidget:
        app = self.app
        area, col = self._page("Motion", "A red MOTION border on the preview when something moves.")
        col.addWidget(SettingCard("motion", "Motion detection", "Off by default. Runs only on this PC.",
                                  self._toggle("motion_enabled", lambda on: app.set_motion_option("motion_enabled", on))))
        col.addWidget(SettingCard(
            None, "Sensitivity", "Higher notices smaller movements.",
            self._combo("motion_sensitivity", [(s.capitalize(), s) for s in SENSITIVITY_CHOICES],
                        lambda v: app.set_motion_option("motion_sensitivity", v))))
        col.addWidget(SettingCard(
            None, "Wait between alerts", "",
            self._combo("motion_cooldown", [(f"{s} seconds", s) for s in COOLDOWN_CHOICES],
                        lambda v: app.set_motion_option("motion_cooldown", v))))
        self.zones_card = SettingCard("window", "Ignore zones",
                                      "Draw rectangles on the preview where movement should be ignored.")
        self.zones_card.add_control(self._button("Edit on preview", app.edit_zones))
        self.zones_clear_btn = self.zones_card.add_control(self._button("Clear", app.clear_zones))
        col.addWidget(self.zones_card)
        col.addWidget(label("While the preview is hidden", "section"))
        col.addWidget(SettingCard(None, "Keep monitoring", "The tray icon turns red when something moves.",
                                  self._toggle("monitor_while_hidden",
                                               lambda on: app.set_motion_option("monitor_while_hidden", on))))
        self.show_on_motion_card = SettingCard(
            None, "Show the preview on motion", "It hides again after 10 seconds unless you touch it.",
            self._toggle("show_on_motion", lambda on: app.set_cfg("show_on_motion", on)))
        col.addWidget(self.show_on_motion_card)
        self._finish(col)
        return area

    def _page_camera(self) -> QWidget:
        app = self.app
        area, col = self._page("Camera")
        self.camera_box = QComboBox()
        self.camera_box.setMinimumWidth(260)
        self.camera_box.currentIndexChanged.connect(self._camera_chosen)
        col.addWidget(SettingCard("camera", "Camera", "Listing cameras never turns one on.", self.camera_box))
        res_items = []
        for w, h in RESOLUTIONS:
            note = " (wide, recommended)" if (w, h) == (640, 360) else " (4:3, narrower)" if (w, h) == (640, 480) else ""
            res_items.append((f"{w} × {h}{note}", (w, h)))
        col.addWidget(SettingCard(None, "Resolution", "The preview is small, so higher resolutions mostly cost CPU.",
                                  self._combo("resolution", res_items, lambda v: app.set_resolution(*v))))
        col.addWidget(SettingCard(None, "Frame rate", "",
                                  self._combo("fps", [(f"{f} fps", f) for f in FPS_CHOICES], app.set_fps)))
        col.addWidget(label("Hardware", "section"))
        col.addWidget(SettingCard(None, "Camera settings", "Zoom, focus, exposure and brightness from the camera "
                                  "driver. Changes you make there are remembered.",
                                  self._button("Open…", app.open_camera_settings)))
        self.forget_props_card = SettingCard(None, "Saved camera settings", "",
                                             self._button("Forget", app.forget_camera_props))
        col.addWidget(self.forget_props_card)
        col.addWidget(label("Power", "section"))
        col.addWidget(SettingCard(None, "Pause camera", "Turns the camera off completely (its light goes out).",
                                  self._toggle("paused", app.set_paused)))
        col.addWidget(SettingCard(None, "Turn the camera off when hidden",
                                  "After 1 minute hidden with nothing to watch. Not used while Watch my back is on.",
                                  self._toggle("release_when_hidden", app.set_release_when_hidden)))
        self._finish(col)
        return area

    def _page_view(self) -> QWidget:
        app = self.app
        area, col = self._page("View", "Tip: Ctrl + mouse wheel on the preview zooms, Ctrl + drag pans.")
        col.addWidget(SettingCard("view", "Mirror image", "Like a rear-view mirror: people on your left appear on the left.",
                                  self._toggle("flip_h", lambda on: app.set_view(flip_h=on))))
        col.addWidget(SettingCard(None, "Flip upside-down", "For a camera mounted upside-down.",
                                  self._toggle("flip_v", lambda on: app.set_view(flip_v=on))))
        col.addWidget(SettingCard(None, "Rotate 180°", "",
                                  self._button("Rotate", lambda: app.set_view(flip_h=not app.cfg.flip_h,
                                                                              flip_v=not app.cfg.flip_v))))
        zoom_row = QWidget()
        zr = QHBoxLayout(zoom_row)
        zr.setContentsMargins(0, 0, 0, 0)
        self.zoom_slider = QSlider(Qt.Orientation.Horizontal)
        self.zoom_slider.setRange(100, 400)
        self.zoom_slider.setFixedWidth(180)
        self.zoom_value = label("1.0×", "caption")
        self.zoom_value.setFixedWidth(36)
        self.zoom_slider.valueChanged.connect(self._zoom_changed)
        zr.addWidget(self.zoom_slider)
        zr.addWidget(self.zoom_value)
        zr.addWidget(self._button("Reset", lambda: app.set_zoom(1.0)))
        col.addWidget(SettingCard(None, "Digital zoom", "1× shows the widest view.", zoom_row))
        col.addWidget(SettingCard(None, "Fill the window", "Crops the edges instead of showing bars.",
                                  self._toggle("crop_to_fill", app.set_crop)))
        self._finish(col)
        return area

    def _page_window(self) -> QWidget:
        app = self.app
        area, col = self._page("Window & app")
        col.addWidget(SettingCard("window", "Always on top", "Keep the preview above other windows.",
                                  self._toggle("always_on_top", app.set_on_top)))
        col.addWidget(SettingCard(None, "Preview opacity", "",
                                  self._combo("opacity", [(f"{o}%", o) for o in OPACITY_CHOICES], app.set_opacity)))
        col.addWidget(SettingCard(None, "Theme", "For this window and notifications.",
                                  self._combo("theme", [("Use Windows setting", "system"), ("Dark", "dark"),
                                                        ("Light", "light")], app.set_theme)))
        if win32.IS_WINDOWS:
            col.addWidget(SettingCard(None, "Start with Windows", "",
                                      self._toggle("autostart", app.set_autostart)))
        col.addWidget(SettingCard(None, "Preview position", "Move the preview back to the top-right corner.",
                                  self._button("Reset", app.reset_geometry)))
        col.addWidget(SettingCard(None, "Log file", str(log_dir()), self._button("Open folder", app.open_log_folder)))
        self._finish(col)
        return area

    def _page_privacy(self) -> QWidget:
        app = self.app
        area, col = self._page("Privacy")
        col.addWidget(InfoBar("success", "Everything stays on this PC. DeskGuard never records video, saves photos, "
                                         "records audio or connects to the internet."))
        col.addSpacing(4)
        col.addWidget(SettingCard("window", "Settings", str(config_dir() / "config.json"),
                                  self._button("Open folder", app.open_settings_folder)))
        self.privacy_face = SettingCard("face", "Face profile", "")
        self.privacy_face_btn = self.privacy_face.add_control(
            self._button("Delete my face data", self._forget_face, danger=True))
        col.addWidget(self.privacy_face)
        col.addWidget(SettingCard("camera", "Camera use",
                                  "The camera light is on while DeskGuard runs. Pause the camera from the tray "
                                  "menu at any time."))
        self._finish(col)
        return area

    # ------------------------------------------------------------------ #
    # Behaviour
    # ------------------------------------------------------------------ #
    def show_page(self, key: str) -> None:
        if key == "face":
            self.start_face_setup()
            return
        if self.stack.currentWidget() is self.face_page:
            self.face_page.stop_silently()
        page = self.pages.get(key, self.pages["watch"])
        self.stack.setCurrentWidget(page)
        if key in self.nav:
            self.nav[key].setChecked(True)
        if key == "camera":
            self._fill_cameras()

    def start_face_setup(self) -> None:
        self.nav["watch"].setChecked(True)
        self.stack.setCurrentWidget(self.face_page)
        self.face_page.start()

    def _face_setup_finished(self, saved: bool) -> None:
        if saved and not self.app.cfg.presence_enabled:
            self.app.set_presence_enabled(True)
        self.app.changed.emit()
        self.show_page("watch")

    def _forget_face(self) -> None:
        self.app.forget_face()

    def _fill_cameras(self) -> None:
        names = self.app.camera_names()
        self._syncing = True
        self.camera_box.clear()
        if not names:
            self.camera_box.addItem("No cameras found", None)
        for i, name in enumerate(names):
            self.camera_box.addItem(name, (i, name))
        if 0 <= self.app.cfg.camera_index < len(names):
            self.camera_box.setCurrentIndex(self.app.cfg.camera_index)
        self._syncing = False

    def _camera_chosen(self, idx: int) -> None:
        data = self.camera_box.itemData(idx)
        if self._syncing or data is None:
            return
        if data[0] != self.app.cfg.camera_index or data[1] != self.app.cfg.camera_name:
            self.app.set_camera(*data)

    def _zoom_changed(self, value: int) -> None:
        self.zoom_value.setText(f"{value / 100:.1f}×")
        if not self._syncing:
            self._zoom_timer_value = value / 100
            if not hasattr(self, "_zoom_timer"):
                self._zoom_timer = QTimer(self, singleShot=True, interval=150,
                                          timeout=lambda: self.app.set_zoom(self._zoom_timer_value))
            self._zoom_timer.start()

    def apply_theme(self) -> None:
        pal = theme.current()
        font = self.font()
        font.setFamily(theme.font_family(theme.TEXT_FAMILIES))
        font.setPixelSize(14)
        self.setFont(font)
        self.setStyleSheet(theme.stylesheet(pal))
        win32.set_dark_title_bar(int(self.winId()), pal.dark)
        for bar in self.findChildren(InfoBar):
            bar.set(bar._severity, bar.text.text())
        self._sync_status()
        self.update()

    def sync(self) -> None:
        """Refresh every control from the current settings."""
        app, c = self.app, self.app.cfg
        self._syncing = True
        try:
            for key in ("presence_enabled", "watch_person_without_face", "motion_enabled", "monitor_while_hidden",
                        "show_on_motion", "release_when_hidden", "flip_h", "flip_v", "crop_to_fill", "always_on_top"):
                self.c[key].setChecked(bool(getattr(c, key)))
            for key in ("watch_dwell_s", "face_strictness", "alert_cooldown_s", "motion_sensitivity",
                        "motion_cooldown", "fps", "opacity", "theme"):
                self._set_combo(self.c[key], getattr(c, key))
            self._set_combo(self.c["resolution"], (c.width, c.height))
            self.c["paused"].setChecked(app.worker.paused)
            if "autostart" in self.c:
                self.c["autostart"].setChecked(win32.autostart_enabled())
            self.zoom_slider.setValue(round(c.zoom * 100))
            self.zoom_value.setText(f"{c.zoom:.1f}×")
            self.show_on_motion_card.setEnabled(c.monitor_while_hidden)

            n = len(c.ignore_zones)
            self.zones_card.set_description(
                (f"{n} zone{'s' if n != 1 else ''}. " if n else "No zones. ")
                + "Draw rectangles on the preview where movement should be ignored."
                + (" Watch my back also ignores you automatically." if c.presence_enabled else ""))
            self.zones_clear_btn.setEnabled(bool(n))

            saved_props = c.camera_props.get(app._camera_name(), {})
            self.forget_props_card.set_description(
                f"{len(saved_props)} value(s) re-applied when the camera starts." if saved_props
                else "Nothing saved yet.")
            self.forget_props_card.setEnabled(bool(saved_props))

            enrolled = app.profile.enrolled
            self.face_info.setVisible(not enrolled)
            self.face_card.set_description(
                "Set up ✓ – you are recognised and ignored." if enrolled
                else "Not set up yet. Takes about 5 seconds.")
            self.face_setup_btn.setText("Set up again" if enrolled else "Set up face")
            self.face_setup_btn.setProperty("accent", not enrolled)
            self.face_setup_btn.style().unpolish(self.face_setup_btn)
            self.face_setup_btn.style().polish(self.face_setup_btn)
            self.face_forget_btn.setEnabled(enrolled)
            self.privacy_face.set_description(
                f"Saved as numbers only (no photo) in {app.profile.path}" if enrolled else "Not set up – nothing stored.")
            self.privacy_face_btn.setEnabled(enrolled)
        finally:
            self._syncing = False
        self._sync_status()

    def _sync_status(self) -> None:
        pal = theme.current()
        severity, text = self.app.watch_status()
        color = {"ok": pal.success, "alert": pal.danger, "error": pal.danger, "warning": pal.warning}.get(
            severity, pal.text3)
        self.hero_status.setText(text)
        self.hero_glyph.set_glyph("alert" if severity == "alert" else "shield", color)
        state_colors = {"live": pal.success, "error": pal.danger, "paused": pal.text3}
        self.status_dot.color = QColor(state_colors.get(self.app.state, pal.warning))
        self.status_dot.update()
        self.status_text.setText(self.app.status_label())

    def closeEvent(self, event: QCloseEvent) -> None:  # noqa: N802
        self.face_page.stop_silently()
        try:
            self.app.changed.disconnect(self.sync)
            self.app.status_changed.disconnect(self._sync_status)
        except (RuntimeError, TypeError):
            pass
        super().closeEvent(event)
