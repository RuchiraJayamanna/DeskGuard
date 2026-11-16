"""DeskGuard entry point.

Usage:
    python main.py            normal start
    python main.py --diag     also log RAM/CPU every minute (for measurements)
    python main.py --reset    restore default settings before starting
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
import threading
from logging.handlers import RotatingFileHandler


def configure_runtime() -> None:
    """Environment tweaks that must happen before numpy/cv2 are imported.

    - Disable MSMF hardware transforms (avoids multi-second camera open times).
    - Keep OpenCV's own console logging quiet.
    - One thread for OpenBLAS/OpenMP: by default they start a pool of one
      thread per CPU core, each with its own committed buffer (hundreds of MB
      private memory on a 16-thread CPU) – DeskGuard does no linear algebra.
    """
    for key, value in (
        ("OPENCV_VIDEOIO_MSMF_ENABLE_HW_TRANSFORMS", "0"),
        ("OPENCV_LOG_LEVEL", "ERROR"),
        ("OPENBLAS_NUM_THREADS", "1"),
        ("OMP_NUM_THREADS", "1"),
    ):
        os.environ.setdefault(key, value)


def configure_cv2() -> None:
    """Our per-frame work is tiny (240x150 resize, 160x120 motion check), so
    OpenCV's worker pool only adds thread wake-ups. OpenCL would allocate GPU
    memory for no benefit."""
    import cv2

    cv2.setNumThreads(1)
    cv2.ocl.setUseOpenCL(False)


configure_runtime()

from PySide6.QtCore import QLockFile  # noqa: E402
from PySide6.QtWidgets import QApplication, QMessageBox  # noqa: E402

from deskguard import APP_NAME, __version__  # noqa: E402
from deskguard.config import ConfigStore, config_dir, log_dir  # noqa: E402


def setup_logging() -> None:
    handlers: list[logging.Handler] = []
    try:
        log_dir().mkdir(parents=True, exist_ok=True)
        handlers.append(
            RotatingFileHandler(log_dir() / "deskguard.log", maxBytes=512_000, backupCount=2, encoding="utf-8")
        )
    except OSError:
        pass
    if sys.stderr is not None:  # windowed .exe has no console
        handlers.append(logging.StreamHandler())
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s", handlers=handlers
    )

    # Log unexpected errors instead of letting them kill the app.
    def excepthook(exc_type, exc, tb):  # type: ignore[no-untyped-def]
        logging.getLogger(APP_NAME).critical("Unhandled error", exc_info=(exc_type, exc, tb))

    sys.excepthook = excepthook
    threading.excepthook = lambda args: excepthook(args.exc_type, args.exc_value, args.exc_traceback)


def main() -> int:
    parser = argparse.ArgumentParser(prog=APP_NAME)
    parser.add_argument("--diag", action="store_true", help="log RAM/CPU usage every minute")
    parser.add_argument("--reset", action="store_true", help="restore default settings")
    args = parser.parse_args()

    setup_logging()
    log = logging.getLogger(APP_NAME)

    app = QApplication(sys.argv)
    app.setApplicationName(APP_NAME)
    app.setApplicationVersion(__version__)
    app.setQuitOnLastWindowClosed(False)  # hiding the preview must not quit

    # One instance only: two copies would fight over the camera.
    config_dir().mkdir(parents=True, exist_ok=True)
    lock = QLockFile(str(config_dir() / "deskguard.lock"))
    lock.setStaleLockTime(0)
    if not lock.tryLock(100):
        QMessageBox.information(
            None, APP_NAME, "DeskGuard is already running.\nLook for its icon in the system tray."
        )
        return 0

    from deskguard import win32

    win32.hold_app_mutex()  # lets the installer detect a running copy

    store = ConfigStore()
    if args.reset:
        try:
            store.path.unlink()
        except FileNotFoundError:
            pass

    # Imported here so the single-instance check happens before OpenCV loads.
    configure_cv2()
    from deskguard.app import DeskGuardApp

    try:
        guard = DeskGuardApp(app, store, diag=args.diag)
        guard.start()
        code = app.exec()
    except Exception:
        log.exception("Fatal error")
        QMessageBox.critical(None, APP_NAME, "DeskGuard hit an unexpected error and must close.\nDetails are in the log.")
        code = 1
    finally:
        lock.unlock()
    return code


if __name__ == "__main__":
    sys.exit(main())
