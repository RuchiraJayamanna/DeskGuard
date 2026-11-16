"""Run DeskGuard with a synthetic 640x480 @ 30 fps camera and report RAM/CPU.

Useful to separate DeskGuard's own cost from camera-driver cost:
    python tools/bench_synthetic.py --seconds 60 [--motion] [--hidden] [--fps 15]
Real-camera numbers come from `python main.py --diag` (see README).
"""

from __future__ import annotations

import argparse
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import numpy as np  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from deskguard import app as app_mod, win32  # noqa: E402
from deskguard.camera import OpenedCamera  # noqa: E402
from deskguard.camera_worker import CameraWorker  # noqa: E402
from deskguard.config import ConfigStore  # noqa: E402


class SyntheticCapture:
    def __init__(self, fps: int = 30) -> None:
        self.interval, self.next, self.n = 1 / fps, time.monotonic(), 0
        self.base = np.random.default_rng(0).integers(60, 160, (480, 640, 3), dtype=np.uint8)

    def grab(self) -> bool:
        d = self.next - time.monotonic()
        if d > 0:
            time.sleep(d)
        self.next = max(self.next + self.interval, time.monotonic())
        self.n += 1
        return True

    def retrieve(self, image=None):  # type: ignore[no-untyped-def]
        frame = image if image is not None and image.shape == self.base.shape else np.empty_like(self.base)
        np.copyto(frame, self.base)  # simulates the driver's decode into our buffer
        x = (self.n * 7) % 560
        frame[200:400, x : x + 80] = 20  # a moving "person"
        return True, frame

    def release(self) -> None:
        pass


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--seconds", type=int, default=30)
    ap.add_argument("--fps", type=int, default=10)
    ap.add_argument("--motion", action="store_true")
    ap.add_argument("--hidden", action="store_true")
    a = ap.parse_args()

    qapp = QApplication(sys.argv)
    qapp.setQuitOnLastWindowClosed(False)
    app_mod.CameraWorker = lambda: CameraWorker(open_fn=lambda *_: OpenedCamera(SyntheticCapture(), 640, 480, "synthetic"))
    store = ConfigStore(Path(tempfile.mkdtemp()) / "config.json")
    cfg = store.load()
    cfg.fps, cfg.motion_enabled, cfg.monitor_while_hidden = a.fps, a.motion, True
    store.save(cfg)
    guard = app_mod.DeskGuardApp(qapp, store)
    guard.start()
    if a.hidden:
        guard.hide_preview()

    frames = [0]
    guard.worker.frame_ready.connect(lambda: frames.__setitem__(0, frames[0] + 1))
    cpu = win32.CpuSampler()
    t_end = time.monotonic() + a.seconds
    start_mem = None
    while time.monotonic() < t_end:
        qapp.processEvents()
        time.sleep(0.005)
        if start_mem is None and time.monotonic() > t_end - a.seconds + 3:
            start_mem = win32.memory_usage_mb()
            cpu.sample()
    pct = cpu.sample()
    ws, private = win32.memory_usage_mb()
    print(f"seconds={a.seconds} fps={a.fps} motion={a.motion} hidden={a.hidden}")
    print(f"frames shown={frames[0]}  (~{frames[0] / a.seconds:.1f}/s)")
    print(f"memory after 3 s: ws={start_mem[0]:.1f} MB private={start_mem[1]:.1f} MB")
    print(f"memory at end:    ws={ws:.1f} MB private={private:.1f} MB")
    print(f"cpu (whole machine %)={pct:.2f}  cpu (one core %)={pct * (win32.os.cpu_count() or 1):.1f}")
    guard.worker.stop()


if __name__ == "__main__":
    main()
