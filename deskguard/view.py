"""Geometry of the preview: which part of the camera frame is shown, and where.

Pure math (no Qt, no OpenCV) so it is shared by the camera worker (which
renders the preview image) and the preview window (which maps mouse positions
back to the camera frame for ignore zones, zoom-at-cursor and panning).

Coordinate systems
- raw:    normalized 0..1 position in the camera frame as delivered by the
          driver, BEFORE any flip. Ignore zones and the pan centre are stored
          in raw coordinates, so toggling a flip never moves a zone off the
          spot in the room it covers.
- widget: pixel position in the preview (logical or physical pixels – the
          mapping is the same as long as the display size uses the same unit).
"""

from __future__ import annotations

from dataclasses import dataclass

MIN_ZOOM, MAX_ZOOM = 1.0, 4.0


def clamp(value: float, lo: float, hi: float) -> float:
    return lo if value < lo else hi if value > hi else value


def clamp_zoom(zoom: float) -> float:
    return clamp(float(zoom), MIN_ZOOM, MAX_ZOOM)


def clamp_pan(zoom: float, pan_x: float, pan_y: float) -> tuple[float, float]:
    """Keep the zoomed area inside the frame."""
    half = 0.5 / clamp_zoom(zoom)
    return clamp(pan_x, half, 1.0 - half), clamp(pan_y, half, 1.0 - half)


def flip_code(flip_h: bool, flip_v: bool) -> int | None:
    """cv2.flip code for the given flips, or None for no flip."""
    if flip_h and flip_v:
        return -1
    if flip_h:
        return 1
    if flip_v:
        return 0
    return None


@dataclass(frozen=True)
class ViewTransform:
    frame_w: int
    frame_h: int
    disp_w: float
    disp_h: float
    crop: bool = False
    zoom: float = 1.0
    pan_x: float = 0.5
    pan_y: float = 0.5
    flip_h: bool = False
    flip_v: bool = False

    # ------------------------------------------------------------------ #
    def zoom_rect(self) -> tuple[float, float, float, float]:
        """Zoomed area of the raw frame in raw pixels (x, y, w, h), unrounded."""
        z = clamp_zoom(self.zoom)
        px, py = clamp_pan(z, self.pan_x, self.pan_y)
        zw, zh = self.frame_w / z, self.frame_h / z
        return px * self.frame_w - zw / 2, py * self.frame_h - zh / 2, zw, zh

    def source_rect(self) -> tuple[int, int, int, int]:
        """Part of the raw frame that is drawn, in whole raw pixels (x, y, w, h)."""
        x, y, w, h = self.zoom_rect()
        if self.crop and self.disp_w > 0 and self.disp_h > 0:
            # Cut the zoomed area to the display aspect ratio, centred.
            target = self.disp_w / self.disp_h
            if w / h > target:
                nw = h * target
                x, w = x + (w - nw) / 2, nw
            else:
                nh = w / target
                y, h = y + (h - nh) / 2, nh
        ix = int(clamp(round(x), 0, self.frame_w - 1))
        iy = int(clamp(round(y), 0, self.frame_h - 1))
        iw = int(clamp(round(w), 1, self.frame_w - ix))
        ih = int(clamp(round(h), 1, self.frame_h - iy))
        return ix, iy, iw, ih

    def output_size(self) -> tuple[int, int]:
        """Size of the rendered image (letterboxed unless crop)."""
        tw, th = max(1, round(self.disp_w)), max(1, round(self.disp_h))
        if self.crop:
            return tw, th
        _, _, w, h = self.source_rect()
        scale = min(tw / w, th / h)
        return max(1, round(w * scale)), max(1, round(h * scale))

    def offset(self) -> tuple[float, float]:
        """Top-left of the rendered image inside the display."""
        ow, oh = self.output_size()
        return (self.disp_w - ow) / 2, (self.disp_h - oh) / 2

    # ------------------------------------------------------------------ #
    def widget_to_raw(self, x: float, y: float) -> tuple[float, float]:
        """Display position -> normalized raw-frame position (not clamped)."""
        sx, sy, sw, sh = self.source_rect()
        ox, oy = self.offset()
        ow, oh = self.output_size()
        fx, fy = (x - ox) / ow, (y - oy) / oh
        if self.flip_h:
            fx = 1.0 - fx
        if self.flip_v:
            fy = 1.0 - fy
        return (sx + fx * sw) / self.frame_w, (sy + fy * sh) / self.frame_h

    def raw_to_widget(self, u: float, v: float) -> tuple[float, float]:
        """Normalized raw-frame position -> display position."""
        sx, sy, sw, sh = self.source_rect()
        ox, oy = self.offset()
        ow, oh = self.output_size()
        fx, fy = (u * self.frame_w - sx) / sw, (v * self.frame_h - sy) / sh
        if self.flip_h:
            fx = 1.0 - fx
        if self.flip_v:
            fy = 1.0 - fy
        return ox + fx * ow, oy + fy * oh

    def zoom_at(self, x: float, y: float, new_zoom: float) -> tuple[float, float, float]:
        """New (zoom, pan_x, pan_y) that keeps the point under (x, y) fixed."""
        new_zoom = clamp_zoom(new_zoom)
        u, v = self.widget_to_raw(x, y)
        sx, sy, sw, sh = self.source_rect()
        ox, oy = self.offset()
        ow, oh = self.output_size()
        # Position of the cursor inside the drawn image, 0..1 (in raw orientation).
        fx, fy = (u * self.frame_w - sx) / sw, (v * self.frame_h - sy) / sh
        if not (0 <= (x - ox) / ow <= 1 and 0 <= (y - oy) / oh <= 1):
            fx, fy = 0.5, 0.5  # cursor in the letterbox: zoom around the centre
            u, v = (sx + sw / 2) / self.frame_w, (sy + sh / 2) / self.frame_h
        nxt = ViewTransform(self.frame_w, self.frame_h, self.disp_w, self.disp_h, self.crop, new_zoom,
                            0.5, 0.5, self.flip_h, self.flip_v)
        _, _, nw, nh = nxt.source_rect()
        # Crop (if any) is centred in the zoom area, so the shown area is centred on the pan point.
        pan_x = u - (fx - 0.5) * nw / self.frame_w
        pan_y = v - (fy - 0.5) * nh / self.frame_h
        return (new_zoom, *clamp_pan(new_zoom, pan_x, pan_y))

    def pan_by(self, dx: float, dy: float) -> tuple[float, float]:
        """New (pan_x, pan_y) after dragging the image by (dx, dy) display pixels."""
        _, _, sw, sh = self.source_rect()
        ow, oh = self.output_size()
        du, dv = dx / ow * sw / self.frame_w, dy / oh * sh / self.frame_h
        if self.flip_h:
            du = -du
        if self.flip_v:
            dv = -dv
        px, py = clamp_pan(self.zoom, self.pan_x, self.pan_y)
        return clamp_pan(self.zoom, px - du, py - dv)
