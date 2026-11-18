# -*- mode: python ; coding: utf-8 -*-
# PyInstaller build for DeskGuard (one-folder build: fast start, fewer antivirus false positives).
#   pyinstaller --noconfirm --clean deskguard.spec
from pathlib import Path

ROOT = Path(SPECPATH)

EXCLUDES = [
    # stdlib modules DeskGuard never uses
    "tkinter", "unittest", "pydoc", "doctest", "sqlite3", "xmlrpc", "lib2to3", "pytest",
    "ssl", "_ssl",  # no network use at all (socket stays: logging.handlers imports it)
    # Qt modules DeskGuard never uses (only QtCore/QtGui/QtWidgets are needed)
    "PySide6.QtNetwork", "PySide6.QtQml", "PySide6.QtQuick", "PySide6.QtQuickWidgets",
    "PySide6.QtOpenGL", "PySide6.QtOpenGLWidgets", "PySide6.QtSvg", "PySide6.QtSvgWidgets",
    "PySide6.QtDBus", "PySide6.QtPdf", "PySide6.QtPdfWidgets", "PySide6.QtConcurrent",
    "PySide6.QtTest", "PySide6.QtSql", "PySide6.QtXml", "PySide6.QtPrintSupport",
    "PySide6.QtHelp", "PySide6.QtDesigner", "PySide6.QtUiTools", "PySide6.QtMultimedia",
]

a = Analysis(
    [str(ROOT / "main.py")],
    pathex=[str(ROOT)],
    binaries=[],
    # Face models for "Watch my back" (loaded only while the feature is on).
    datas=[(str(ROOT / "resources" / "models" / "*.onnx"), "resources/models"),
           (str(ROOT / "resources" / "models" / "LICENSE*"), "resources/models")],
    hiddenimports=[],
    excludes=EXCLUDES,
    noarchive=False,
)

# OpenCV's FFmpeg plugin (~25 MB) is only for video files/streams; webcams use
# DirectShow / Media Foundation. Qt translations aren't used either.
# Everything below is unused by DeskGuard and safe to drop (~30 MB):
#   opengl32sw.dll        software OpenGL fallback (we paint with the raster engine)
#   Qt6Network / Qt6Svg   pulled in only by plugins we don't ship
#   libssl / libcrypto    OpenSSL (no network; hashlib falls back to built-in hashes)
# Qt plugins: only the Windows platform plugin and the native style are needed
# (icons are drawn with QPainter, no image files are loaded).
DROP_FILES = {
    "opengl32sw.dll", "qt6network.dll", "qt6svg.dll",
    "libssl-3.dll", "libcrypto-3.dll", "_ssl.pyd", "_hashlib.pyd",
}
KEEP_PLUGINS = ("platforms/qwindows.dll", "styles/")


def _keep(dest: str) -> bool:
    path = dest.replace("\\", "/").lower()
    name = path.rsplit("/", 1)[-1]
    if "opencv_videoio_ffmpeg" in name or name in DROP_FILES:
        return False
    if "/plugins/" in path:
        return path.split("/plugins/", 1)[1].startswith(KEEP_PLUGINS)
    return "translations" not in path.split("/")


a.binaries = [b for b in a.binaries if _keep(b[0])]
a.datas = [d for d in a.datas if _keep(d[0])]

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="DeskGuard",
    console=False,          # windowed app: no console window
    icon=str(ROOT / "resources" / "deskguard.ico"),
    version=str(ROOT / "installer" / "version_info.txt"),
    upx=False,              # UPX triggers antivirus false positives
)

coll = COLLECT(exe, a.binaries, a.datas, name="DeskGuard", upx=False)
