"""Set up Qt for the tests before any of them starts it: no windows, and real fonts.

On Windows the offscreen platform doesn't find the system fonts by itself and falls back to one
with very different widths, which changes how much fits on a label."""

import os
import sys

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
if sys.platform == "win32":
    os.environ.setdefault("QT_QPA_FONTDIR", os.path.join(os.environ.get("WINDIR", r"C:\Windows"), "Fonts"))
