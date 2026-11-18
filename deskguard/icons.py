"""App and tray icons, drawn with QPainter (no image files needed at runtime)."""

from __future__ import annotations

from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import QColor, QIcon, QPainter, QPixmap

STATUS_COLORS = {
    "live": "#22c55e",
    "starting": "#f59e0b",
    "reconnecting": "#f59e0b",
    "paused": "#9ca3af",
    "error": "#ef4444",
    "stopped": "#9ca3af",
    "motion": "#ef4444",
}

_cache: dict[str, QIcon] = {}


def draw_icon(size: int, status: str | None) -> QPixmap:
    s = float(size)
    pm = QPixmap(size, size)
    pm.fill(Qt.GlobalColor.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    p.setPen(Qt.PenStyle.NoPen)
    p.setBrush(QColor("#1f2937"))
    p.drawRoundedRect(QRectF(s * 0.04, s * 0.04, s * 0.92, s * 0.92), s * 0.22, s * 0.22)
    centre = QPointF(s * 0.5, s * 0.53)
    p.setBrush(QColor("#e5e7eb"))
    p.drawEllipse(centre, s * 0.29, s * 0.29)
    p.setBrush(QColor("#0f172a"))
    p.drawEllipse(centre, s * 0.19, s * 0.19)
    p.setBrush(QColor("#60a5fa"))
    p.drawEllipse(QPointF(s * 0.45, s * 0.47), s * 0.06, s * 0.06)
    if status:
        p.setBrush(QColor("#1f2937"))
        p.drawEllipse(QPointF(s * 0.80, s * 0.20), s * 0.17, s * 0.17)
        p.setBrush(QColor(STATUS_COLORS.get(status, "#9ca3af")))
        p.drawEllipse(QPointF(s * 0.80, s * 0.20), s * 0.12, s * 0.12)
    p.end()
    return pm


def icon(status: str | None = None) -> QIcon:
    key = status or ""
    if key not in _cache:
        ic = QIcon()
        for size in (16, 20, 24, 32, 48, 64, 256):
            ic.addPixmap(draw_icon(size, status))
        _cache[key] = ic
    return _cache[key]


# --------------------------------------------------------------------------- #
# Line glyphs (Fluent-style, 1.5 px strokes) for the Settings window and toast
# --------------------------------------------------------------------------- #
GLYPHS = ("shield", "motion", "camera", "view", "window", "lock", "face", "eye", "check", "close", "alert")


def _shield_path(r: QRectF):  # type: ignore[no-untyped-def]
    from PySide6.QtGui import QPainterPath

    path = QPainterPath()
    cx, top, w, h = r.center().x(), r.top(), r.width(), r.height()
    path.moveTo(cx, top)
    path.cubicTo(cx + w * 0.25, top + h * 0.12, cx + w * 0.42, top + h * 0.12, cx + w * 0.42, top + h * 0.12)
    path.lineTo(cx + w * 0.42, top + h * 0.48)
    path.cubicTo(cx + w * 0.42, top + h * 0.75, cx + w * 0.2, top + h * 0.9, cx, top + h)
    path.cubicTo(cx - w * 0.2, top + h * 0.9, cx - w * 0.42, top + h * 0.75, cx - w * 0.42, top + h * 0.48)
    path.lineTo(cx - w * 0.42, top + h * 0.12)
    path.cubicTo(cx - w * 0.42, top + h * 0.12, cx - w * 0.25, top + h * 0.12, cx, top)
    return path


def draw_glyph(p: QPainter, name: str, rect: QRectF, color: QColor, width: float = 1.5) -> None:
    """Draw a simple outline icon inside `rect`."""
    from PySide6.QtGui import QPen

    p.save()
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    pen = QPen(color, width)
    pen.setCapStyle(Qt.PenCapStyle.RoundCap)
    pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
    p.setPen(pen)
    p.setBrush(Qt.BrushStyle.NoBrush)
    r = rect.adjusted(width, width, -width, -width)
    x, y, w, h = r.x(), r.y(), r.width(), r.height()
    c = r.center()
    if name in ("shield", "lock") or name == "alert":
        p.drawPath(_shield_path(r))
        if name == "lock":
            p.drawRoundedRect(QRectF(c.x() - w * 0.16, c.y() - h * 0.02, w * 0.32, h * 0.24), 1.5, 1.5)
            p.drawArc(QRectF(c.x() - w * 0.1, c.y() - h * 0.18, w * 0.2, h * 0.26), 0, 180 * 16)
        elif name == "alert":
            p.drawLine(QPointF(c.x(), y + h * 0.3), QPointF(c.x(), y + h * 0.56))
            p.drawPoint(QPointF(c.x(), y + h * 0.7))
        else:
            p.drawPolyline([QPointF(c.x() - w * 0.16, c.y()), QPointF(c.x() - w * 0.03, c.y() + h * 0.13),
                            QPointF(c.x() + w * 0.18, c.y() - h * 0.12)])
    elif name == "motion":
        for i, k in enumerate((0.18, 0.32, 0.46)):
            p.drawArc(QRectF(c.x() - w * k, c.y() - h * k, 2 * w * k, 2 * h * k), -45 * 16, 90 * 16)
            p.drawArc(QRectF(c.x() - w * k, c.y() - h * k, 2 * w * k, 2 * h * k), 135 * 16, 90 * 16)
        p.drawEllipse(c, w * 0.05, h * 0.05)
    elif name == "camera":
        p.drawRoundedRect(QRectF(x, y + h * 0.25, w * 0.68, h * 0.5), 3, 3)
        p.drawPolygon([QPointF(x + w * 0.72, y + h * 0.42), QPointF(x + w, y + h * 0.28),
                       QPointF(x + w, y + h * 0.72), QPointF(x + w * 0.72, y + h * 0.58)])
    elif name in ("view", "eye"):
        p.drawEllipse(QRectF(x, y + h * 0.22, w, h * 0.56))
        p.drawEllipse(c, w * 0.15, h * 0.15)
    elif name == "window":
        p.drawRoundedRect(QRectF(x, y + h * 0.12, w, h * 0.76), 3, 3)
        p.drawLine(QPointF(x, y + h * 0.32), QPointF(x + w, y + h * 0.32))
        p.drawRoundedRect(QRectF(x + w * 0.5, y + h * 0.48, w * 0.36, h * 0.28), 1.5, 1.5)
    elif name == "face":
        p.drawEllipse(QRectF(x + w * 0.12, y, w * 0.76, h * 0.8))
        p.drawPoint(QPointF(c.x() - w * 0.14, y + h * 0.34))
        p.drawPoint(QPointF(c.x() + w * 0.14, y + h * 0.34))
        p.drawArc(QRectF(c.x() - w * 0.16, y + h * 0.38, w * 0.32, h * 0.22), -20 * 16, -140 * 16)
        p.drawLine(QPointF(x + w * 0.05, y + h), QPointF(x + w * 0.95, y + h))
    elif name == "check":
        p.drawPolyline([QPointF(x + w * 0.15, c.y()), QPointF(x + w * 0.42, y + h * 0.78),
                        QPointF(x + w * 0.88, y + h * 0.22)])
    elif name == "close":
        p.drawLine(QPointF(x + w * 0.2, y + h * 0.2), QPointF(x + w * 0.8, y + h * 0.8))
        p.drawLine(QPointF(x + w * 0.8, y + h * 0.2), QPointF(x + w * 0.2, y + h * 0.8))
    p.restore()
