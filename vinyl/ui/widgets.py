"""Custom widgets: stereo level meter and a waveform view with draggable split markers."""

import math
import time

from pathlib import Path

import numpy as np
import soundfile as sf
from PySide6.QtCore import QPointF, QRectF, Qt, QTimer, Signal
from PySide6.QtGui import QBrush, QColor, QFont, QPainter, QPainterPath, QPen
from PySide6.QtWidgets import QMenu, QSizePolicy, QWidget

METER_MIN_DB = -60.0


def to_db(x: float) -> float:
    return 20 * math.log10(x) if x > 1e-6 else -120.0


def fmt_time(seconds: float) -> str:
    seconds = max(0.0, seconds)
    m, s = divmod(seconds, 60)
    return f"{int(m)}:{s:04.1f}"


class LevelMeter(QWidget):
    """Horizontal stereo peak meter with peak hold and a clip light (click to reset)."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setMinimumHeight(64)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.level = [METER_MIN_DB, METER_MIN_DB]
        self.hold = [METER_MIN_DB, METER_MIN_DB]
        self.hold_time = [0.0, 0.0]
        self.clipped = [False, False]
        self.max_db = [METER_MIN_DB, METER_MIN_DB]
        self.setToolTip("Peak level. Aim for the loudest parts around -6 dB. Click to reset clip lights.")

    def update_peaks(self, peaks) -> None:
        now = time.monotonic()
        for ch in range(2):
            db = to_db(float(peaks[ch]))
            # Fast attack, ~20 dB/s fall-off so the meter is readable
            self.level[ch] = max(db, self.level[ch] - 0.7)
            if db >= self.hold[ch] or now - self.hold_time[ch] > 1.5:
                self.hold[ch], self.hold_time[ch] = db, now
            if db >= -0.01:
                self.clipped[ch] = True
            self.max_db[ch] = max(self.max_db[ch], db)
        self.update()

    def reset(self) -> None:
        self.clipped = [False, False]
        self.max_db = [METER_MIN_DB, METER_MIN_DB]
        self.update()

    def mousePressEvent(self, e):
        self.reset()

    def _x(self, db: float, w: float) -> float:
        return w * (min(0.0, max(METER_MIN_DB, db)) - METER_MIN_DB) / -METER_MIN_DB

    def paintEvent(self, e):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        clip_w, label_w = 34, 18
        w = self.width() - clip_w - label_w - 8
        bar_h, gap, top = 18, 6, 4
        p.fillRect(self.rect(), self.palette().window())
        small = QFont(self.font())
        small.setPointSizeF(max(7.0, small.pointSizeF() - 1.5))
        p.setFont(small)
        for ch in range(2):
            y = top + ch * (bar_h + gap)
            p.setPen(self.palette().text().color())
            p.drawText(QRectF(0, y, label_w, bar_h), Qt.AlignmentFlag.AlignCenter, "LR"[ch])
            x0 = label_w
            p.fillRect(QRectF(x0, y, w, bar_h), QColor(30, 30, 30))
            lvl = self._x(self.level[ch], w)
            for lo, hi, color in ((-60, -12, "#3fb950"), (-12, -3, "#d29922"), (-3, 0, "#f85149")):
                a, b = self._x(lo, w), self._x(hi, w)
                if lvl > a:
                    p.fillRect(QRectF(x0 + a, y, min(lvl, b) - a, bar_h), QColor(color))
            hx = x0 + self._x(self.hold[ch], w)
            p.fillRect(QRectF(hx - 1, y, 2, bar_h), QColor(230, 230, 230))
            cx = x0 + w + 6
            p.setBrush(QColor("#f85149") if self.clipped[ch] else QColor(60, 60, 60))
            p.setPen(Qt.PenStyle.NoPen)
            p.drawRoundedRect(QRectF(cx, y, clip_w - 6, bar_h), 3, 3)
            p.setPen(QColor(255, 255, 255) if self.clipped[ch] else QColor(140, 140, 140))
            p.drawText(QRectF(cx, y, clip_w - 6, bar_h), Qt.AlignmentFlag.AlignCenter, "CLIP")
        # Scale
        y = top + 2 * (bar_h + gap) - 2
        p.setPen(self.palette().text().color())
        for db in (-60, -48, -36, -24, -18, -12, -6, -3, 0):
            x = label_w + self._x(db, w)
            p.drawLine(QPointF(x, y), QPointF(x, y + 3))
            p.drawText(QRectF(x - 15, y + 3, 30, 12), Qt.AlignmentFlag.AlignCenter, str(db))
        p.end()


class WaveformView(QWidget):
    """Draws a recorded side with split markers.

    Click: set the play cursor. Drag a marker to move it. Double-click: add a split.
    Right-click: menu (add/remove split, mark a part as not a track, play, zoom).
    Scroll wheel zooms around the mouse; drag empty space (or Shift+wheel) to scroll.
    The first and last markers are where the music starts and ends. Parts in `skips`
    (e.g. the pause while flipping the record) are not tracks and are not saved.
    """

    markersChanged = Signal()
    cursorChanged = Signal(float)
    viewChanged = Signal()
    playRequested = Signal(float)

    GRAB_PX = 6
    MIN_SPAN = 0.25  # seconds across the whole widget at maximum zoom
    DETAIL_SPAN = 40.0  # read real samples when showing this many seconds or fewer

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setMinimumHeight(180)
        self.setMouseTracking(True)
        self.peak: np.ndarray | None = None
        self.hop = 0.02
        self.duration = 0.0
        self.edges: list[float] = []
        self.labels: list[str] = []
        self.skips: set[int] = set()  # indexes of parts between edges that are not tracks
        self.cursor = 0.0
        self.playhead: float | None = None
        self.clips: list[float] = []
        self._drag: int | None = None
        self._pan_from: tuple[float, float] | None = None  # (mouse x, view start) while dragging the view
        self._panned = False
        # Visible time range; zooming narrows it.
        self.view_start = 0.0
        self.view_end = 0.0
        # Sample-accurate detail, read from the file when zoomed in close
        self.source: Path | None = None
        self._detail: tuple[float, float, int, np.ndarray] | None = None  # (start, end, width, peaks)
        self._detail_timer = QTimer(self, singleShot=True, interval=60)
        self._detail_timer.timeout.connect(self._load_detail)

    def set_data(self, peak: np.ndarray | None, hop: float, duration: float, source: Path | None = None) -> None:
        self.peak, self.hop, self.duration, self.source = peak, hop, duration, source
        self.cursor = 0.0
        self._detail = None
        self.view_start, self.view_end = 0.0, duration
        self.viewChanged.emit()
        self.update()

    # ---------- zoom / scroll ----------

    @property
    def span(self) -> float:
        return self.view_end - self.view_start

    def set_view(self, start: float, end: float) -> None:
        if self.duration <= 0:
            return
        span = min(self.duration, max(self.MIN_SPAN, end - start))
        start = min(max(0.0, start), self.duration - span)
        if (start, start + span) == (self.view_start, self.view_end):
            return
        self.view_start, self.view_end = start, start + span
        self.viewChanged.emit()
        self._detail_timer.start()
        self.update()

    def zoom(self, factor: float, anchor: float | None = None) -> None:
        """factor > 1 zooms in. The anchor time stays under the same pixel."""
        if anchor is None:
            anchor = (self.view_start + self.view_end) / 2
        frac = (anchor - self.view_start) / self.span if self.span else 0.5
        span = self.span / factor
        self.set_view(anchor - frac * span, anchor - frac * span + span)

    def zoom_to(self, t: float, span: float = 8.0) -> None:
        """Show `span` seconds around t (t sits a third of the way in, so you see what leads into it)."""
        self.set_view(t - span / 3, t - span / 3 + span)

    def fit(self) -> None:
        self.set_view(0.0, self.duration)

    def wheelEvent(self, e):
        dy = e.angleDelta().y() / 120
        dx = e.angleDelta().x() / 120
        if e.modifiers() & Qt.KeyboardModifier.ShiftModifier or dx:
            steps = dx or dy
            self.set_view(self.view_start - steps * 0.15 * self.span, self.view_end - steps * 0.15 * self.span)
        elif dy:
            self.zoom(1.25 ** dy, self._x2t(e.position().x()))
        e.accept()

    def _load_detail(self):
        """Read the visible samples so a zoomed-in view shows the real waveform, not the 20 ms summary."""
        if not self.source or self.span > self.DETAIL_SPAN or self.width() <= 0:
            self._detail = None
            return
        try:
            with sf.SoundFile(self.source) as f:
                sr = f.samplerate
                a, b = int(self.view_start * sr), min(f.frames, int(self.view_end * sr))
                f.seek(a)
                data = f.read(b - a, dtype="float32", always_2d=True)
        except (OSError, RuntimeError):
            self._detail = None
            return
        mono = np.abs(data).max(axis=1)
        w = self.width()
        edges = np.linspace(0, len(mono), w + 1).astype(int)
        peaks = np.array([mono[edges[i]:max(edges[i] + 1, edges[i + 1])].max() if edges[i] < len(mono) else 0.0
                          for i in range(w)], dtype=np.float32)
        self._detail = (self.view_start, self.view_end, w, peaks)
        self.update()

    def resizeEvent(self, e):
        self._detail_timer.start()
        super().resizeEvent(e)

    def set_edges(self, edges: list[float], labels: list[str] | None = None, skips: set[int] | None = None) -> None:
        self.edges = sorted(edges)
        self.labels = labels or []
        self.skips = set(skips or ())
        self.update()

    # ---------- editing splits ----------

    def segment_at(self, t: float) -> int | None:
        for i in range(len(self.edges) - 1):
            if self.edges[i] <= t < self.edges[i + 1]:
                return i
        return None

    def add_split(self, t: float) -> bool:
        k = self.segment_at(t)
        if k is None or min(t - self.edges[k], self.edges[k + 1] - t) < 0.05:
            return False
        self.edges.insert(k + 1, t)
        # Parts after the new split move up one; a split inside a skipped part leaves both halves skipped.
        self.skips = {s if s < k else s + 1 for s in self.skips} | ({k} if k in self.skips else set())
        self.markersChanged.emit()
        self.update()
        return True

    def remove_split(self, i: int) -> None:
        """Remove marker i (not the first or last). The parts either side merge."""
        if not 0 < i < len(self.edges) - 1:
            return
        del self.edges[i]
        merged_skip = (i - 1 in self.skips) or (i in self.skips)
        self.skips = {s if s < i else s - 1 for s in self.skips if s != i and s != i - 1}
        if merged_skip:
            self.skips.add(i - 1)
        self.markersChanged.emit()
        self.update()

    def toggle_skip(self, seg: int) -> None:
        self.skips ^= {seg}
        self.markersChanged.emit()
        self.update()

    def contextMenuEvent(self, e):
        if self.duration <= 0 or len(self.edges) < 2:
            return
        x = e.pos().x()
        t = self._x2t(x)
        marker = self._marker_at(x)
        seg = self.segment_at(t)
        menu = QMenu(self)
        menu.addAction(f"Add split here ({fmt_time(t)})", lambda: self.add_split(t))
        if marker is not None and 0 < marker < len(self.edges) - 1:
            menu.addAction("Remove this split", lambda: self.remove_split(marker))
        if seg is not None:
            if seg in self.skips:
                menu.addAction("Make this part a track again", lambda: self.toggle_skip(seg))
            else:
                menu.addAction("Mark this part as not a track (side change, silence…)", lambda: self.toggle_skip(seg))
        menu.addSeparator()
        menu.addAction("Set music start here", lambda: self._move_edge(0, t))
        menu.addAction("Set music end here", lambda: self._move_edge(len(self.edges) - 1, t))
        menu.addSeparator()
        menu.addAction("Play from here", lambda: self.playRequested.emit(t))
        menu.addAction("Zoom in here", lambda: self.zoom_to(t, max(self.MIN_SPAN, self.span / 4)))
        menu.addAction("Show the whole recording", self.fit)
        menu.exec(e.globalPos())

    def _move_edge(self, i: int, t: float) -> None:
        lo = self.edges[i - 1] + 0.1 if i > 0 else 0.0
        hi = self.edges[i + 1] - 0.1 if i < len(self.edges) - 1 else self.duration
        self.edges[i] = min(hi, max(lo, t))
        self.markersChanged.emit()
        self.update()

    def set_playhead(self, t: float | None) -> None:
        self.playhead = t
        self.update()

    def _t2x(self, t: float) -> float:
        return (t - self.view_start) / self.span * self.width() if self.span else 0.0

    def _x2t(self, x: float) -> float:
        return min(self.duration, max(0.0, self.view_start + x / max(1, self.width()) * self.span))

    def _marker_at(self, x: float) -> int | None:
        best, dist = None, self.GRAB_PX + 1
        for i, t in enumerate(self.edges):
            d = abs(self._t2x(t) - x)
            if d < dist:
                best, dist = i, d
        return best

    def mousePressEvent(self, e):
        x = e.position().x()
        i = self._marker_at(x)
        if e.button() != Qt.MouseButton.LeftButton:
            return  # right-click opens the context menu
        if i is not None:
            self._drag = i
        else:
            # Either a click (set the play cursor) or a drag (scroll the view): decided on release.
            self._pan_from = (x, self.view_start)
            self._panned = False

    def mouseMoveEvent(self, e):
        x = e.position().x()
        if self._drag is not None:
            i = self._drag
            lo = self.edges[i - 1] + 0.1 if i > 0 else 0.0
            hi = self.edges[i + 1] - 0.1 if i < len(self.edges) - 1 else self.duration
            self.edges[i] = min(hi, max(lo, self._x2t(x)))
            self.update()
        elif self._pan_from is not None:
            x0, start0 = self._pan_from
            if abs(x - x0) > 4:
                self._panned = True
                self.setCursor(Qt.CursorShape.ClosedHandCursor)
            if self._panned:
                shift = (x0 - x) / max(1, self.width()) * self.span
                self.set_view(start0 + shift, start0 + shift + self.span)
        else:
            near = self._marker_at(x) is not None
            self.setCursor(Qt.CursorShape.SizeHorCursor if near else Qt.CursorShape.ArrowCursor)

    def mouseReleaseEvent(self, e):
        if self._drag is not None:
            self._drag = None
            self.markersChanged.emit()
        elif self._pan_from is not None:
            if not self._panned:
                self.cursor = self._x2t(e.position().x())
                self.cursorChanged.emit(self.cursor)
                self.update()
            self._pan_from = None
            self.setCursor(Qt.CursorShape.ArrowCursor)

    def mouseDoubleClickEvent(self, e):
        if len(self.edges) < 2 or self.duration <= 0:
            return
        self.add_split(self._x2t(e.position().x()))

    def paintEvent(self, e):
        p = QPainter(self)
        w, h = self.width(), self.height()
        p.fillRect(self.rect(), QColor(24, 26, 30))
        if self.peak is None or len(self.peak) == 0:
            p.setPen(QColor(150, 150, 150))
            p.drawText(self.rect(), Qt.AlignmentFlag.AlignCenter, "Pick a recording to see its waveform")
            p.end()
            return
        ruler_h = 16
        h -= ruler_h
        mid = h / 2
        # Shade the parts outside the music start/end
        if len(self.edges) >= 2:
            p.fillRect(QRectF(0, 0, self._t2x(self.edges[0]), h), QColor(40, 40, 46))
            p.fillRect(QRectF(self._t2x(self.edges[-1]), 0, w, h), QColor(40, 40, 46))
        # Alternate track backgrounds; parts that aren't tracks get a dark hatch
        for i in range(len(self.edges) - 1):
            a, b = self._t2x(self.edges[i]), self._t2x(self.edges[i + 1])
            if i in self.skips:
                p.fillRect(QRectF(a, 0, b - a, h), QColor(40, 40, 46))
                p.fillRect(QRectF(a, 0, b - a, h), QBrush(QColor(90, 90, 100), Qt.BrushStyle.BDiagPattern))
            elif i % 2:
                p.fillRect(QRectF(a, 0, b - a, h), QColor(32, 36, 44))
        # Waveform: max peak per pixel column, from file samples when zoomed in, else the 20 ms summary
        cols = max(1, w)
        d = self._detail
        if d and abs(d[0] - self.view_start) < 1e-9 and abs(d[1] - self.view_end) < 1e-9 and d[2] == cols:
            col_peaks = d[3]
        else:
            n = len(self.peak)
            # Hop indexes of each pixel column's edges within the visible range
            idx = np.clip((np.linspace(self.view_start, self.view_end, cols + 1) / self.hop).astype(int), 0, n)
            col_peaks = np.array([self.peak[idx[x]:max(idx[x] + 1, idx[x + 1])].max() if idx[x] < n else 0.0
                                  for x in range(cols)])
        path = QPainterPath()
        for x in range(cols):
            v = min(1.0, math.sqrt(float(col_peaks[x])))  # sqrt makes quiet passages visible
            path.moveTo(x + 0.5, mid - v * (mid - 4))
            path.lineTo(x + 0.5, mid + v * (mid - 4))
        p.setPen(QPen(QColor("#58a6ff"), 1))
        p.drawPath(path)
        # Clips recorded during this take: red ticks along the top and bottom
        p.setPen(QPen(QColor("#f85149"), 2))
        for t in self.clips:
            x = self._t2x(t)
            p.drawLine(QPointF(x, 0), QPointF(x, 10))
            p.drawLine(QPointF(x, h - 10), QPointF(x, h))
        # Markers
        font = QFont(self.font())
        font.setBold(True)
        p.setFont(font)
        for i, t in enumerate(self.edges):
            x = self._t2x(t)
            edge = i in (0, len(self.edges) - 1)
            p.setPen(QPen(QColor("#d29922") if edge else QColor("#f0f6fc"), 2))
            p.drawLine(QPointF(x, 0), QPointF(x, h))
            if i < len(self.edges) - 1:
                label = self.labels[i] if i < len(self.labels) else "?"
                # Only where it fits: a narrow gap's label would run into the next track's
                if self._t2x(self.edges[i + 1]) - x > p.fontMetrics().horizontalAdvance(label) + 8:
                    p.setPen(QColor("#f0f6fc"))
                    p.drawText(QPointF(x + 4, 16), label)
        # Cursor (hot pink) and playhead (yellow), each with a dark outline so they show on the blue waveform
        cx = self._t2x(self.cursor)
        if -2 <= cx <= w + 2:
            p.setPen(QPen(QColor(0, 0, 0, 200), 5))
            p.drawLine(QPointF(cx, 0), QPointF(cx, h))
            p.setPen(QPen(QColor("#ff3df2"), 2))
            p.drawLine(QPointF(cx, 0), QPointF(cx, h))
            # Handle with the time, at the bottom
            label = ruler_label(self.cursor, 0.05 if self.span < 20 else 0.5)
            box = QRectF(min(max(0, cx - 34), w - 68), h - 22, 68, 18)
            p.fillRect(box, QColor("#ff3df2"))
            p.setPen(QColor("#000000"))
            p.drawText(box, Qt.AlignmentFlag.AlignCenter, label)
        if self.playhead is not None:
            px = self._t2x(self.playhead)
            p.setPen(QPen(QColor(0, 0, 0, 200), 5))
            p.drawLine(QPointF(px, 0), QPointF(px, h))
            p.setPen(QPen(QColor("#ffe14d"), 2))
            p.drawLine(QPointF(px, 0), QPointF(px, h))
        # Time ruler in its own strip below the waveform
        p.fillRect(QRectF(0, h, w, ruler_h), QColor(16, 17, 20))
        p.setPen(QColor(160, 160, 160))
        steps = (0.05, 0.1, 0.25, 0.5, 1, 2, 5, 10, 30, 60, 120, 300, 600)
        step = next((s for s in steps if self.span / s < 12), 1800)
        t = math.floor(self.view_start / step) * step
        while t <= self.view_end:
            x = self._t2x(t)
            if x >= 0:
                p.drawLine(QPointF(x, h), QPointF(x, h + 4))
                p.drawText(QPointF(x + 3, h + ruler_h - 3), ruler_label(t, step))
            t += step
        if self.span < self.duration - 1e-6:
            text = f"zoom {self.duration / self.span:.0f}×"
            box = QRectF(w - 84, 4, 80, 18)
            p.fillRect(box, QColor(0, 0, 0, 150))
            p.setPen(QColor(220, 220, 220))
            p.drawText(box, Qt.AlignmentFlag.AlignCenter, text)
        p.end()


def ruler_label(t: float, step: float) -> str:
    m, s = divmod(t, 60)
    if step >= 1:
        return f"{int(m)}:{int(round(s)):02d}"
    decimals = 2 if step < 0.1 else 1
    return f"{int(m)}:{s:0{3 + decimals}.{decimals}f}"
