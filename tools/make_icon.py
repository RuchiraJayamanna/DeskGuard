"""Regenerate resources/deskguard.ico from the same drawing code the app uses."""

import os
import sys
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from PySide6.QtGui import QGuiApplication  # noqa: E402

from deskguard.icons import draw_icon  # noqa: E402

app = QGuiApplication([])
out = ROOT / "resources" / "deskguard.ico"
out.parent.mkdir(exist_ok=True)
# Qt's ICO writer stores one image per file; 256 px is scaled by Windows as needed.
assert draw_icon(256, None).save(str(out), "ICO"), "ICO export failed"
draw_icon(256, None).save(str(ROOT / "resources" / "deskguard.png"), "PNG")
print("wrote", out)
