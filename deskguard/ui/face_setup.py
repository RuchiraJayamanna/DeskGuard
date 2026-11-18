"""Guided face setup ("Look at the camera…") shown inside the Settings window.

The live picture comes from the camera worker's enrolment slot (320x180, in
memory only). Only the resulting feature vectors are saved – never an image.
"""

from __future__ import annotations

import numpy as np
from PySide6.QtCore import QRectF, Qt, Signal
from PySide6.QtGui import QColor, QImage, QPainter, QPainterPath, QPaintEvent, QPen
from PySide6.QtWidgets import QHBoxLayout, QPushButton, QSizePolicy, QVBoxLayout, QWidget

from deskguard.icons import draw_glyph
from deskguard.ui import theme
from deskguard.ui.widgets import ProgressRing, label

STEPS = ("Look at the camera", "Turn your head slightly", "Done")


class VideoView(QWidget):
    """Rounded live picture with the detected face outlined."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setMinimumSize(320, 180)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        self._frame: np.ndarray | None = None
        self._box: tuple[float, float, float, float] | None = None
        self._good = False

    def heightForWidth(self, w: int) -> int:  # noqa: N802
        return round(w * 9 / 16)

    def hasHeightForWidth(self) -> bool:  # noqa: N802
        return True

    def set_frame(self, frame: np.ndarray | None) -> None:
        self._frame = frame
        self.update()

    def set_box(self, box: tuple[float, float, float, float] | None, good: bool) -> None:
        self._box, self._good = box, good
        self.update()

    def paintEvent(self, event: QPaintEvent) -> None:  # noqa: N802
        pal = theme.current()
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
        w = self.width()
        h = min(self.height(), round(w * 9 / 16))
        w = round(h * 16 / 9)
        r = QRectF((self.width() - w) / 2, (self.height() - h) / 2, w, h)
        path = QPainterPath()
        path.addRoundedRect(r, 12, 12)
        p.setClipPath(path)
        p.fillRect(r, QColor("#111418"))
        frame = self._frame
        if frame is not None:
            fh, fw = frame.shape[:2]
            img = QImage(frame.data, fw, fh, frame.strides[0], QImage.Format.Format_BGR888)
            p.drawImage(r, img)
        else:
            draw_glyph(p, "camera", QRectF(r.center().x() - 20, r.center().y() - 20, 40, 40), QColor("#9ca3af"), 2)
        if self._box is not None and frame is not None:
            bx, by, bw, bh = self._box
            box = QRectF(r.left() + bx * r.width(), r.top() + by * r.height(), bw * r.width(), bh * r.height())
            color = QColor(pal.success) if self._good else QColor(pal.accent)
            p.setPen(QPen(color, 2.5))
            p.setBrush(Qt.BrushStyle.NoBrush)
            p.drawRoundedRect(box.adjusted(-6, -6, 6, 6), 10, 10)
        p.setClipping(False)
        p.setPen(QPen(pal.q("stroke"), 1))
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.drawRoundedRect(r.adjusted(0.5, 0.5, -0.5, -0.5), 12, 12)
        p.end()


class FaceSetupPage(QWidget):
    finished = Signal(bool)  # True = face saved

    def __init__(self, app, parent: QWidget | None = None) -> None:  # type: ignore[no-untyped-def]
        super().__init__(parent)
        self.app = app
        self.setObjectName("Page")
        self._running = False
        self._done = False

        root = QVBoxLayout(self)
        root.setContentsMargins(36, 28, 36, 28)
        root.setSpacing(6)
        root.addWidget(label("Set up face recognition", "pageTitle"))
        root.addWidget(label("Sit as you normally do and look at the camera for a few seconds. DeskGuard saves "
                             "only numbers that describe your face – never a photo.", "caption", wrap=True))
        root.addSpacing(14)

        body = QHBoxLayout()
        body.setSpacing(24)
        self.video = VideoView()
        body.addWidget(self.video, 3)

        side = QVBoxLayout()
        side.setSpacing(10)
        self.ring = ProgressRing(76)
        side.addWidget(self.ring, 0, Qt.AlignmentFlag.AlignLeft)
        self.hint = label("Starting the camera…", "hint", wrap=True)
        side.addWidget(self.hint)
        self.step_labels = []
        for i, text in enumerate(STEPS):
            lbl = label(f"{i + 1}   {text}", "caption")
            self.step_labels.append(lbl)
            side.addWidget(lbl)
        side.addStretch(1)
        body.addLayout(side, 2)
        root.addLayout(body, 1)

        root.addSpacing(12)
        buttons = QHBoxLayout()
        buttons.addStretch(1)
        self.cancel_btn = QPushButton("Cancel")
        self.cancel_btn.clicked.connect(self.cancel)
        buttons.addWidget(self.cancel_btn)
        self.action_btn = QPushButton("Finish")
        self.action_btn.setProperty("accent", True)
        self.action_btn.setEnabled(False)
        self.action_btn.clicked.connect(self._on_action)
        buttons.addWidget(self.action_btn)
        root.addLayout(buttons)

        worker = app.worker
        worker.enroll_frame_ready.connect(self._on_frame)
        worker.enroll_progress.connect(self._on_progress)

    # ------------------------------------------------------------------ #
    def start(self) -> None:
        self._running, self._done = True, False
        self.ring.set_value(0)
        self.hint.setText("Starting the camera…")
        self.action_btn.setText("Finish")
        self.action_btn.setEnabled(False)
        self.cancel_btn.setEnabled(True)
        self._set_step(0)
        self.app.worker.start_enroll()

    def cancel(self) -> None:
        if self._running:
            self.app.worker.cancel_enroll()
        self._running = False
        self.video.set_frame(None)
        self.finished.emit(False)

    def stop_silently(self) -> None:
        """Window closing: stop enrolling without navigating."""
        if self._running:
            self.app.worker.cancel_enroll()
        self._running = False

    def _on_action(self) -> None:
        if self._done:
            self.video.set_frame(None)
            self.finished.emit(True)
        else:
            self.start()  # "Try again"

    def _set_step(self, idx: int) -> None:
        pal = theme.current()
        for i, lbl in enumerate(self.step_labels):
            color = pal.success if i < idx else pal.text if i == idx else pal.text2
            weight = 600 if i == idx else 400
            lbl.setStyleSheet(f"color: {color}; font-weight: {weight}; font-size: 13px;")

    def _on_frame(self) -> None:
        frame = self.app.worker.take_enroll_frame()
        if self._running and frame is not None:
            self.video.set_frame(frame)

    def _on_progress(self, info: dict) -> None:
        if not self._running:
            return
        box = info.get("box")
        if box is not None and (info.get("flip_h") or info.get("flip_v")):
            x, y, w, h = box
            if info.get("flip_h"):
                x = 1 - x - w
            if info.get("flip_v"):
                y = 1 - y - h
            box = (x, y, w, h)
        progress = float(info.get("progress", 0))
        self.ring.set_value(progress)
        self.hint.setText(info.get("hint", ""))
        good = box is not None and "closer" not in info.get("hint", "") and "Only one" not in info.get("hint", "")
        self.video.set_box(box, good)
        self._set_step(2 if info.get("done") else 1 if progress >= 0.5 else 0)
        if info.get("done"):
            self._running, self._done = False, True
            self.action_btn.setText("Finish")
            self.action_btn.setEnabled(True)
            self.cancel_btn.setEnabled(False)
        elif info.get("failed"):
            self._running = False
            self.action_btn.setText("Try again")
            self.action_btn.setEnabled(True)
