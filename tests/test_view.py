import pytest

from deskguard.view import ViewTransform, clamp_pan, flip_code


def vt(**kw):
    args = dict(frame_w=640, frame_h=360, disp_w=256, disp_h=144)
    args.update(kw)
    return ViewTransform(**args)


def test_full_frame_fits_exactly():
    t = vt()
    assert t.source_rect() == (0, 0, 640, 360)
    assert t.output_size() == (256, 144)
    assert t.offset() == (0, 0)


def test_letterbox_4_3_in_16_9():
    t = vt(frame_w=640, frame_h=480)
    assert t.output_size() == (192, 144)
    assert t.offset() == (32, 0)


def test_crop_fills_display():
    t = vt(frame_w=640, frame_h=480, crop=True)
    assert t.output_size() == (256, 144)
    x, y, w, h = t.source_rect()
    assert (x, w) == (0, 640) and h == 360 and y == 60


def test_zoom_and_pan_select_sub_rect_clamped():
    t = vt(zoom=2.0, pan_x=0.0, pan_y=1.0)  # pan clamped into the frame
    assert t.source_rect() == (0, 180, 320, 180)


@pytest.mark.parametrize("flip_h,flip_v", [(False, False), (True, False), (False, True), (True, True)])
@pytest.mark.parametrize("zoom,crop,fw,fh", [(1.0, False, 640, 360), (2.0, False, 640, 480), (1.7, True, 640, 480)])
def test_widget_raw_round_trip(flip_h, flip_v, zoom, crop, fw, fh):
    t = vt(frame_w=fw, frame_h=fh, zoom=zoom, crop=crop, pan_x=0.4, pan_y=0.6, flip_h=flip_h, flip_v=flip_v)
    for x, y in ((50, 40), (128, 72), (200, 100)):
        u, v = t.widget_to_raw(x, y)
        assert t.raw_to_widget(u, v) == pytest.approx((x, y), abs=1e-6)


def test_mirror_maps_left_of_widget_to_right_of_frame():
    u, _ = vt(flip_h=True).widget_to_raw(10, 72)
    assert u > 0.9
    u, _ = vt().widget_to_raw(10, 72)
    assert u < 0.1


@pytest.mark.parametrize("flip_h", [False, True])
def test_zoom_at_keeps_point_under_cursor(flip_h):
    t = vt(flip_h=flip_h)
    before = t.widget_to_raw(60, 40)
    zoom, px, py = t.zoom_at(60, 40, 2.0)
    after = vt(flip_h=flip_h, zoom=zoom, pan_x=px, pan_y=py).widget_to_raw(60, 40)
    assert zoom == 2.0
    assert after == pytest.approx(before, abs=0.01)


def test_pan_by_moves_opposite_to_drag_and_respects_flip():
    t = vt(zoom=2.0)
    px, _ = t.pan_by(20, 0)  # drag image right => look further left
    assert px < 0.5
    px, _ = vt(zoom=2.0, flip_h=True).pan_by(20, 0)
    assert px > 0.5


def test_helpers():
    assert clamp_pan(2.0, 0.0, 1.0) == (0.25, 0.75)
    assert clamp_pan(1.0, 0.1, 0.9) == (0.5, 0.5)
    assert [flip_code(*f) for f in ((False, False), (True, False), (False, True), (True, True))] == [None, 1, 0, -1]
