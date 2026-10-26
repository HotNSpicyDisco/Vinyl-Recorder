"""Draw the app icon (a record with a hot label and a REC dot) and write assets/icon.png + icon.ico.

Run: .venv\\Scripts\\python.exe tools\\make_icon.py
"""

import math
import struct
import sys
from pathlib import Path

from PySide6.QtCore import QBuffer, QByteArray, QIODevice, QPointF, QRectF, Qt
from PySide6.QtGui import (QBrush, QColor, QConicalGradient, QGuiApplication, QImage, QLinearGradient, QPainter,
                           QPen, QRadialGradient)

ASSETS = Path(__file__).resolve().parents[1] / "assets"
ICO_SIZES = [16, 20, 24, 32, 40, 48, 64, 128, 256]


def draw(size: int) -> QImage:
    img = QImage(size, size, QImage.Format.Format_ARGB32_Premultiplied)
    img.fill(Qt.GlobalColor.transparent)
    p = QPainter(img)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    p.scale(size / 256, size / 256)
    c = QPointF(128, 128)

    # Disc
    disc = QRadialGradient(c, 122)
    disc.setColorAt(0.0, QColor("#2a2a30"))
    disc.setColorAt(0.85, QColor("#141418"))
    disc.setColorAt(1.0, QColor("#08080a"))
    p.setPen(QPen(QColor(255, 255, 255, 70), 3))  # faint rim so it reads on dark taskbars
    p.setBrush(disc)
    p.drawEllipse(c, 122, 122)

    # Grooves (skipped when tiny: they'd just turn to grey mush)
    if size >= 40:
        for r in range(56, 118, 5 if size >= 96 else 9):
            p.setPen(QPen(QColor(255, 255, 255, 18 if r % 2 else 10), 1.2))
            p.setBrush(Qt.BrushStyle.NoBrush)
            p.drawEllipse(c, r, r)

    # Light catching the grooves
    shine = QConicalGradient(c, 35)
    for stop, alpha in ((0.0, 0), (0.07, 70), (0.14, 0), (0.5, 0), (0.57, 55), (0.64, 0), (1.0, 0)):
        shine.setColorAt(stop, QColor(255, 255, 255, alpha))
    p.setPen(Qt.PenStyle.NoPen)
    p.setBrush(QBrush(shine))
    p.drawEllipse(c, 118, 118)

    # Label
    label = QLinearGradient(QPointF(90, 90), QPointF(166, 166))
    label.setColorAt(0.0, QColor("#ff2e88"))
    label.setColorAt(1.0, QColor("#ff8a00"))
    p.setBrush(label)
    p.drawEllipse(c, 48, 48)
    p.setPen(QPen(QColor(255, 255, 255, 90), 2))
    p.setBrush(Qt.BrushStyle.NoBrush)
    p.drawEllipse(c, 38, 38)
    # Spindle hole
    p.setPen(Qt.PenStyle.NoPen)
    p.setBrush(QColor("#08080a"))
    p.drawEllipse(c, 8 if size >= 32 else 11, 8 if size >= 32 else 11)

    # REC dot, bottom right
    rc = QPointF(206, 206)
    p.setBrush(QColor("#ffffff"))
    p.drawEllipse(rc, 40, 40)
    rec = QRadialGradient(QPointF(198, 198), 40)
    rec.setColorAt(0.0, QColor("#ff6b6b"))
    rec.setColorAt(1.0, QColor("#d7263d"))
    p.setBrush(rec)
    p.drawEllipse(rc, 32, 32)
    p.end()
    return img


def png_bytes(img: QImage) -> bytes:
    data = QByteArray()
    buf = QBuffer(data)
    buf.open(QIODevice.OpenModeFlag.WriteOnly)
    img.save(buf, "PNG")
    return bytes(data)


def write_ico(path: Path, sizes: list[int]) -> None:
    """ICO file with one PNG-compressed image per size (supported since Windows Vista)."""
    images = [png_bytes(draw(s)) for s in sizes]
    header = struct.pack("<HHH", 0, 1, len(images))
    offset = len(header) + 16 * len(images)
    entries, blobs = b"", b""
    for s, blob in zip(sizes, images):
        dim = 0 if s >= 256 else s  # 0 means 256
        entries += struct.pack("<BBBBHHII", dim, dim, 0, 0, 1, 32, len(blob), offset)
        offset += len(blob)
        blobs += blob
    path.write_bytes(header + entries + blobs)


def write_icns(path: Path) -> None:
    """macOS icon. Needs Pillow (pip install pillow), which picks the sizes from a 1024 px image."""
    import io

    from PIL import Image

    big = Image.open(io.BytesIO(png_bytes(draw(1024))))
    big.save(path, format="ICNS")


def main():
    app = QGuiApplication(sys.argv)  # noqa: F841 (needed for QPainter on images)
    ASSETS.mkdir(exist_ok=True)
    draw(256).save(str(ASSETS / "icon.png"))
    write_ico(ASSETS / "icon.ico", ICO_SIZES)
    write_icns(ASSETS / "icon.icns")
    print("wrote", ASSETS / "icon.png", "icon.ico and icon.icns")


if __name__ == "__main__":
    main()
