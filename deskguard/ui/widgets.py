"""Small Fluent-style widgets painted with QPainter (no image or font assets)."""

from __future__ import annotations

from PySide6.QtCore import Property, QEasingCurve, QPointF, QRectF, QSize, Qt, QVariantAnimation, Signal
from PySide6.QtGui import QColor, QPainter, QPaintEvent, QPen
from PySide6.QtWidgets import QAbstractButton, QFrame, QHBoxLayout, QLabel, QVBoxLayout, QWidget

from deskguard.icons import draw_glyph
from deskguard.ui import theme


def label(text: str, role: str | None = None, wrap: bool = False) -> QLabel:
    lbl = QLabel(text)
    if role:
        lbl.setProperty("role", role)
    lbl.setWordWrap(wrap)
    return lbl


class ToggleSwitch(QAbstractButton):
    """Windows 11 toggle switch with a short knob slide."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setCheckable(True)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFixedSize(44, 22)
        self._pos = 0.0
        self._anim = QVariantAnimation(self, duration=120, easingCurve=QEasingCurve.Type.OutCubic)
        self._anim.valueChanged.connect(self._set_pos)
        self.toggled.connect(self._animate)

    def sizeHint(self) -> QSize:  # noqa: N802
        return QSize(44, 22)

    def _set_pos(self, v: float) -> None:
        self._pos = float(v)
        self.update()

    def _animate(self, on: bool) -> None:
        self._anim.stop()
        self._anim.setStartValue(self._pos)
        self._anim.setEndValue(1.0 if on else 0.0)
        self._anim.start()

    def setChecked(self, on: bool) -> None:  # noqa: N802 - snap without animation when set programmatically
        super().setChecked(on)
        self._anim.stop()
        self._pos = 1.0 if on else 0.0
        self.update()

    def paintEvent(self, event: QPaintEvent) -> None:  # noqa: N802
        pal = theme.current()
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        r = QRectF(self.rect()).adjusted(1, 1, -1, -1)
        on = self.isChecked()
        enabled = self.isEnabled()
        if on:
            fill = QColor(pal.accent) if enabled else pal.q("text3")
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(fill)
        else:
            p.setPen(QPen(pal.q("text2") if enabled else pal.q("text3"), 1))
            p.setBrush(Qt.BrushStyle.NoBrush)
        p.drawRoundedRect(r, r.height() / 2, r.height() / 2)
        knob = 12 if self.underMouse() else 10
        x = r.left() + r.height() / 2 + self._pos * (r.width() - r.height())
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QColor(pal.on_accent) if on else (pal.q("text2") if enabled else pal.q("text3")))
        p.drawEllipse(QPointF(x, r.center().y()), knob / 2, knob / 2)
        p.end()

    def enterEvent(self, event) -> None:  # type: ignore[no-untyped-def]  # noqa: N802
        self.update()

    def leaveEvent(self, event) -> None:  # type: ignore[no-untyped-def]  # noqa: N802
        self.update()


class GlyphLabel(QWidget):
    """A glyph from icons.draw_glyph in the current theme colour."""

    def __init__(self, name: str, size: int = 20, color: str | None = None, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.name, self.color = name, color
        self.setFixedSize(size, size)

    def set_glyph(self, name: str, color: str | None = None) -> None:
        self.name, self.color = name, color
        self.update()

    def paintEvent(self, event: QPaintEvent) -> None:  # noqa: N802
        p = QPainter(self)
        color = QColor(self.color) if self.color else theme.current().q("text")
        draw_glyph(p, self.name, QRectF(self.rect()), color, 1.5 if self.width() <= 24 else 2.2)
        p.end()


class SettingCard(QFrame):
    """Icon + title + description on the left, a control on the right."""

    def __init__(self, glyph: str | None, title: str, description: str = "",
                 control: QWidget | None = None, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("Card")
        self.setMinimumHeight(64)
        row = QHBoxLayout(self)
        row.setContentsMargins(16, 10, 16, 10)
        row.setSpacing(14)
        if glyph:
            row.addWidget(GlyphLabel(glyph, 20), 0, Qt.AlignmentFlag.AlignVCenter)
        text = QVBoxLayout()
        text.setSpacing(1)
        self.title = label(title, "cardTitle")
        text.addWidget(self.title)
        self.description = label(description, "caption", wrap=True)
        self.description.setVisible(bool(description))
        text.addWidget(self.description)
        row.addLayout(text, 1)
        self.controls = QHBoxLayout()
        self.controls.setSpacing(8)
        row.addLayout(self.controls)
        if control is not None:
            self.add_control(control)

    def add_control(self, widget: QWidget) -> QWidget:
        self.controls.addWidget(widget, 0, Qt.AlignmentFlag.AlignVCenter)
        return widget

    def set_description(self, text: str) -> None:
        self.description.setText(text)
        self.description.setVisible(bool(text))


class NavButton(QAbstractButton):
    """Sidebar navigation item with the Windows 11 accent pill when selected."""

    def __init__(self, glyph: str, text: str, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.glyph = glyph
        self.setText(text)
        self.setCheckable(True)
        self.setAutoExclusive(True)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFixedHeight(38)

    def paintEvent(self, event: QPaintEvent) -> None:  # noqa: N802
        pal = theme.current()
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        r = QRectF(self.rect()).adjusted(4, 2, -4, -2)
        if self.isChecked() or self.underMouse():
            bg = pal.q("text")
            bg.setAlphaF(0.08 if self.isChecked() else 0.05)
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(bg)
            p.drawRoundedRect(r, 5, 5)
        if self.isChecked():
            p.setBrush(QColor(pal.accent))
            p.drawRoundedRect(QRectF(r.left(), r.center().y() - 8, 3, 16), 1.5, 1.5)
        draw_glyph(p, self.glyph, QRectF(r.left() + 14, r.center().y() - 9, 18, 18), pal.q("text"))
        p.setPen(pal.q("text"))
        f = self.font()
        f.setPixelSize(14)
        p.setFont(f)
        p.drawText(r.adjusted(44, 0, 0, 0), int(Qt.AlignmentFlag.AlignVCenter), self.text())
        p.end()

    def enterEvent(self, event) -> None:  # type: ignore[no-untyped-def]  # noqa: N802
        self.update()

    def leaveEvent(self, event) -> None:  # type: ignore[no-untyped-def]  # noqa: N802
        self.update()


class ProgressRing(QWidget):
    """Circular progress (0..1) drawn around its content."""

    def __init__(self, size: int = 56, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setFixedSize(size, size)
        self._value = 0.0

    def get_value(self) -> float:
        return self._value

    def set_value(self, v: float) -> None:
        self._value = max(0.0, min(1.0, float(v)))
        self.update()

    value = Property(float, get_value, set_value)

    def paintEvent(self, event: QPaintEvent) -> None:  # noqa: N802
        pal = theme.current()
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        r = QRectF(self.rect()).adjusted(4, 4, -4, -4)
        track = pal.q("text3")
        track.setAlphaF(0.35)
        p.setPen(QPen(track, 4))
        p.drawEllipse(r)
        pen = QPen(QColor(pal.success if self._value >= 1 else pal.accent), 4)
        pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        p.setPen(pen)
        p.drawArc(r, 90 * 16, -int(360 * 16 * self._value))
        if self._value >= 1:
            draw_glyph(p, "check", r.adjusted(r.width() * 0.25, r.height() * 0.25,
                                             -r.width() * 0.25, -r.height() * 0.25), QColor(pal.success), 2.5)
        else:
            p.setPen(pal.q("text"))
            f = self.font()
            f.setPixelSize(13)
            f.setBold(True)
            p.setFont(f)
            p.drawText(r, int(Qt.AlignmentFlag.AlignCenter), f"{round(self._value * 100)}%")
        p.end()


class InfoBar(QFrame):
    """Coloured status strip: info / success / warning / error."""

    clicked = Signal()

    def __init__(self, severity: str, text: str, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._severity = severity
        row = QHBoxLayout(self)
        row.setContentsMargins(14, 10, 14, 10)
        row.setSpacing(12)
        self._glyph = GlyphLabel("shield", 18)
        row.addWidget(self._glyph)
        self.text = label(text, wrap=True)
        row.addWidget(self.text, 1)
        self.set(severity, text)

    def set(self, severity: str, text: str) -> None:
        pal = theme.current()
        self._severity = severity
        self.text.setText(text)
        color = {"success": pal.success, "warning": pal.warning, "error": pal.danger}.get(severity, pal.accent)
        # Informational bars stay neutral (only the icon uses the accent), so a
        # red Windows accent colour doesn't make them look like errors.
        tint = QColor(color) if severity != "info" else pal.q("card")
        tint.setAlphaF((0.14 if pal.dark else 0.10) if severity != "info" else 1.0)
        border = QColor(color) if severity != "info" else pal.q("stroke")
        border.setAlphaF(0.35 if severity != "info" else 1.0)
        self.setStyleSheet(
            f"InfoBar {{ background: rgba({tint.red()},{tint.green()},{tint.blue()},{tint.alphaF():.2f});"
            f" border: 1px solid rgba({border.red()},{border.green()},{border.blue()},{border.alphaF():.2f});"
            f" border-radius: 8px; }}"
        )
        glyph = {"success": "shield", "warning": "alert", "error": "alert"}.get(severity, "shield")
        self._glyph.set_glyph(glyph, color)
