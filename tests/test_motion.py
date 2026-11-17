import numpy as np

from deskguard.motion import MotionDetector


def scene(seed=0):
    rng = np.random.default_rng(seed)
    base = np.full((480, 640, 3), 110, np.uint8)
    base[100:200, 100:300] = 160  # some static structure
    return base, rng


def noisy(base, rng, amp=6):
    noise = rng.integers(-amp, amp + 1, base.shape, dtype=np.int16)
    return np.clip(base.astype(np.int16) + noise, 0, 255).astype(np.uint8)


def feed_static(det, base, rng, n, t0=0.0, dt=0.1):
    hits = 0
    for i in range(n):
        hits += det.process(noisy(base, rng), t0 + i * dt)
    return hits


def test_static_scene_with_sensor_noise_never_triggers():
    base, rng = scene()
    for sens in ("low", "medium", "high"):
        det = MotionDetector(sensitivity=sens)
        assert feed_static(det, base, rng, 100) == 0


def test_person_entering_triggers():
    base, rng = scene()
    det = MotionDetector(sensitivity="medium", cooldown=0)
    feed_static(det, base, rng, 20)
    hits = 0
    for i in range(10):
        f = noisy(base, rng)
        x = 300 + i * 20
        f[150:450, x : x + 80] = 30  # dark figure moving across
        hits += det.process(f, 2.0 + i * 0.1)
    assert hits >= 1


def test_lighting_change_does_not_trigger():
    base, rng = scene()
    det = MotionDetector(sensitivity="high", cooldown=0)
    feed_static(det, base, rng, 20)
    brighter = np.clip(base.astype(np.int16) + 70, 0, 255).astype(np.uint8)  # lights switched on
    assert feed_static(det, brighter, rng, 30, t0=3.0) == 0


def test_gradual_lighting_drift_does_not_trigger():
    base, rng = scene()
    det = MotionDetector(sensitivity="high", cooldown=0)
    hits = 0
    for i in range(200):
        level = np.clip(base.astype(np.int16) + i // 4, 0, 255).astype(np.uint8)  # +50 over 20 s
        hits += det.process(noisy(level, rng), i * 0.1)
    assert hits == 0


def test_single_frame_glitch_ignored():
    base, rng = scene()
    det = MotionDetector(sensitivity="medium", cooldown=0)
    feed_static(det, base, rng, 20)
    f = noisy(base, rng)
    f[200:300, 400:500] = 0
    assert det.process(f, 3.0) is False  # one frame only: needs 2 consecutive
    assert feed_static(det, base, rng, 30, t0=3.1) == 0


def test_cooldown_limits_events():
    base, rng = scene()
    det = MotionDetector(sensitivity="high", cooldown=5)
    feed_static(det, base, rng, 20)
    hits = 0
    for i in range(40):  # 4 s of continuous movement
        f = noisy(base, rng)
        x = 20 + (i % 10) * 50
        f[150:450, x : x + 80] = 20
        hits += det.process(f, 2.0 + i * 0.1)
    assert hits == 1


def test_sensitivity_ordering():
    base, rng = scene()
    small = []
    for sens in ("low", "medium", "high"):
        det = MotionDetector(sensitivity=sens, cooldown=0)
        feed_static(det, base, np.random.default_rng(1), 20)
        hits = 0
        for i in range(10):
            f = noisy(base, rng)
            f[300:330, 300 + i * 4 : 340 + i * 4] = 10  # small object (~0.4% of image)
            hits += det.process(f, 2.0 + i * 0.1)
        small.append(hits)
    assert small[0] == 0 and small[2] >= 1


def moving_figure(det, base, rng, x0, y0=150, t0=2.0, n=10, step=10):
    hits = 0
    for i in range(n):
        f = noisy(base, rng)
        x = x0 + i * step
        f[y0 : y0 + 300, x : x + 80] = 30
        hits += det.process(f, t0 + i * 0.1)
    return hits


def test_motion_inside_ignore_zone_is_ignored():
    base, rng = scene()
    det = MotionDetector(sensitivity="high", cooldown=0)
    det.set_ignore_zones([[0.4, 0.2, 0.6, 0.8]])  # right part of the image (where "I" sit)
    feed_static(det, base, rng, 20)
    assert moving_figure(det, base, rng, x0=300) == 0  # figure stays within x 300..470 of 640


def test_motion_outside_ignore_zone_still_triggers():
    base, rng = scene()
    det = MotionDetector(sensitivity="medium", cooldown=0)
    det.set_ignore_zones([[0.6, 0.0, 0.4, 1.0]])
    feed_static(det, base, rng, 20)
    assert moving_figure(det, base, rng, x0=20, step=15) >= 1


def test_zones_covering_everything_never_trigger():
    base, rng = scene()
    det = MotionDetector(sensitivity="high", cooldown=0)
    det.set_ignore_zones([[0.0, 0.0, 1.0, 1.0]])
    feed_static(det, base, rng, 20)
    assert moving_figure(det, base, rng, x0=20, step=30) == 0


def test_clearing_zones_restores_detection():
    base, rng = scene()
    det = MotionDetector(sensitivity="medium", cooldown=0)
    det.set_ignore_zones([[0.0, 0.0, 1.0, 1.0]])
    det.set_ignore_zones([])
    feed_static(det, base, rng, 20)
    assert moving_figure(det, base, rng, x0=300, step=20) >= 1
