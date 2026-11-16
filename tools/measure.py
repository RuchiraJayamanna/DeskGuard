"""Measure DeskGuard's real RAM/CPU with the real webcam.

Runs the actual app (same code path as main.py, with a throw-away config so
your settings are untouched) through scripted phases and prints a table:

    python tools/measure.py                 # ~4 min standard run
    python tools/measure.py --long 30       # adds a 30-minute visible soak

CPU is % of the whole machine (Task Manager scale). RAM is private bytes
(Task Manager "Memory") and working set.
"""

from __future__ import annotations

import argparse
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import main as entry  # noqa: E402  (sets env vars before cv2 loads)

if hasattr(entry, "configure_runtime"):
    entry.configure_runtime()

from PySide6.QtCore import QTimer  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from deskguard import win32  # noqa: E402
from deskguard.config import ConfigStore  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--long", type=int, default=0, help="extra visible soak, minutes")
    ap.add_argument("--scale", type=float, default=1.0, help="multiply phase lengths (for quick checks)")
    args = ap.parse_args()

    qapp = QApplication(sys.argv)
    qapp.setQuitOnLastWindowClosed(False)
    if hasattr(entry, "configure_cv2"):
        entry.configure_cv2()

    from deskguard.app import DeskGuardApp

    tmp = Path(tempfile.mkdtemp())
    guard = DeskGuardApp(qapp, ConfigStore(tmp / "config.json"))
    cpu = win32.CpuSampler()
    rows: list[tuple[str, float, float, float, str]] = []

    def record(label: str) -> None:
        ws, private = win32.memory_usage_mb()
        rows.append((label, cpu.sample(), private, ws, guard.state))
        print(f"{label:<38} cpu={rows[-1][1]:5.2f}%  private={private:6.1f} MB  ws={ws:6.1f} MB  state={guard.state}",
              flush=True)

    def set_motion(on: bool) -> None:
        guard.cfg.motion_enabled = on
        guard._apply_motion()

    s = args.scale
    # (seconds, label recorded at END of the phase, action at START of the phase)
    def set_release(on: bool) -> None:
        guard.cfg.release_when_hidden = on
        guard.worker.set_release_when_hidden(on)

    def soak() -> None:
        set_motion(False)
        guard.show_preview()

    phases: list[tuple[float, str, object]] = [
        (15 * s, "startup (camera open, settling)", None),
        (60 * s, "visible 10 fps", None),
        (45 * s, "hidden (camera stays open)", guard.hide_preview),
        (10 * s, "re-shown", guard.show_preview),
        (45 * s, "visible 10 fps + motion detection", lambda: set_motion(True)),
        (65 * s, "hidden, opt-in release (first 65 s)", lambda: (set_motion(False), set_release(True),
                                                                 guard.hide_preview())),
        (30 * s, "hidden, camera released", None),
    ]
    if args.long:
        phases.append((args.long * 60, f"visible {args.long} min soak", soak))

    def run_phase(i: int) -> None:
        if i > 0:
            record(phases[i - 1][1])
        if i == len(phases):
            print(f"\nfinal state: {guard.state}")
            qapp.quit()
            return
        secs, _label, action = phases[i]
        if callable(action):
            action()
        cpu.sample()  # reset the CPU window at phase start
        QTimer.singleShot(int(secs * 1000), lambda: run_phase(i + 1))

    guard.start()
    QTimer.singleShot(0, lambda: run_phase(0))
    return qapp.exec()


if __name__ == "__main__":
    sys.exit(main())
