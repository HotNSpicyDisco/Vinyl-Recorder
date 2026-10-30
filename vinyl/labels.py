"""Record labels for your sleeves: artist, title, label · year · styles, and a track table with
lengths and BPM (plus an empty rating box to write in).

The layout and fitting rules come from the Discogs Label Maker app (its labels.py), redrawn with Qt
so the same code makes the on-screen preview, a PDF, and prints straight to any printer: a laser
printer with Avery sheets, or a thermal printer (Zebra, Rollo, Munbyn...) with a roll of labels.

All layout is in points (1/72 inch), y going down from the top of the label.
"""

import re
import unicodedata
from dataclasses import asdict, dataclass, field
from pathlib import Path

from PySide6.QtCore import QMarginsF, QPointF, QRectF, QSizeF, Qt
from PySide6.QtGui import QColor, QFont, QFontMetricsF, QImage, QPageLayout, QPageSize, QPainter, QPdfWriter, QPen

from .discogs import Release

PTS = 72
SUFFIX_RE = re.compile(r"\s+\(\d+\)\s*$")
# Zero-width and text-direction characters: Discogs data has them, and they print as boxes
INVISIBLE_RE = re.compile(r"[­​-‏‪-‮⁠-⁤⁦-⁩﻿]")
VARIOUS = {"various", "various artists", "unknown", "unknown artist"}


@dataclass(frozen=True)
class Template:
    id: str
    name: str
    label_w: float  # inches
    label_h: float
    sheet: bool  # True: a letter-size sheet of labels; False: a roll, one label per page
    cols: int = 1
    rows: int = 1
    margin_lr: float = 0.0
    margin_tb: float = 0.0
    thermal: bool = False  # black only: no grey text

    @property
    def per_page(self) -> int:
        return self.cols * self.rows


TEMPLATES = [
    Template("avery_5163", 'Avery 5163 / 8163 sheet: 4" × 2", 10 per sheet', 4.0, 2.0, True, 2, 5, 0.156, 0.5),
    Template("avery_5164", 'Avery 5164 / 8164 sheet: 4" × 3⅓", 6 per sheet', 4.0, 3.333, True, 2, 3, 0.156, 0.5),
    Template("roll_4x2", 'Thermal roll (Zebra, Rollo…): 4" × 2"', 4.0, 2.0, False, thermal=True),
    Template("roll_4x3", 'Thermal roll (Zebra, Rollo…): 4" × 3"', 4.0, 3.0, False, thermal=True),
]
TEMPLATES_BY_ID = {t.id: t for t in TEMPLATES}


@dataclass
class LabelTrack:
    position: str
    name: str  # title, with the track's artist in front on compilations
    length: str = ""  # "5:32"
    bpm: str = ""


@dataclass
class LabelData:
    artist: str
    title: str
    label: str = ""
    year: str = ""
    styles: list[str] = field(default_factory=list)
    release_id: int = 0
    tracks: list[LabelTrack] = field(default_factory=list)


def to_dict(d: LabelData) -> dict:
    return asdict(d)


def from_dict(d: dict) -> LabelData:
    return LabelData(**{**d, "tracks": [LabelTrack(**t) for t in d.get("tracks", [])]})


def _norm(s: str) -> str:
    return unicodedata.normalize("NFC", INVISIBLE_RE.sub("", s or "").strip())


def _clean(s: str) -> str:
    return SUFFIX_RE.sub("", _norm(s)).strip()


def _length(seconds: float | None) -> str:
    if not seconds:
        return ""
    m, s = divmod(int(round(seconds)), 60)
    return f"{m}:{s:02d}"


def _bpm(bpm: float | None) -> str:
    """Whole BPMs, as DJs write them (and so three digits fit the box)."""
    return str(int(round(bpm))) if bpm else ""


def label_data(release: Release, bpms: dict[int, float | None] | None = None,
               lengths: dict[int, float] | None = None) -> LabelData:
    """What goes on the label. Measured lengths and BPMs (by track number) win over Discogs lengths."""
    bpms, lengths = bpms or {}, lengths or {}
    artist = _clean(release.artist)
    tracks = []
    for t in release.tracks:
        name = _norm(t.title)
        if t.artist and t.artist.lower() != artist.lower() and t.artist.lower() not in VARIOUS:
            name = f"{_clean(t.artist)} – {name}"
        tracks.append(LabelTrack(t.position, name, _length(lengths.get(t.number) or t.duration),
                                 _bpm(bpms.get(t.number))))
    return LabelData(artist, _clean(release.title), _clean(release.label), release.year,
                     (release.styles or release.genres)[:3], release.id if release.id > 0 else 0, tracks)


# ---------- text helpers (Qt) ----------

_FAMILIES = ["Noto Sans", "Segoe UI", "Arial", "Helvetica Neue", "Helvetica"]
UNIT = 10  # drawing units per point: fonts get integer pixel sizes, so use tenths of a point


def _font(size_pt: float, bold: bool = False) -> QFont:
    f = QFont()
    f.setFamilies(_FAMILIES)
    f.setPixelSize(max(1, int(round(size_pt * UNIT))))
    f.setBold(bold)
    f.setHintingPreference(QFont.HintingPreference.PreferNoHinting)  # same widths on screen and printer
    return f


def _width(text: str, font: QFont) -> float:
    return QFontMetricsF(font).horizontalAdvance(text) / UNIT


def _clamp(text: str, font: QFont, max_w: float) -> str:
    """Cut text with … to fit max_w points."""
    if _width(text, font) <= max_w:
        return text
    lo, hi, best = 0, len(text), "…"
    while lo <= hi:
        mid = (lo + hi) // 2
        c = text[:mid].rstrip() + "…"
        if _width(c, font) <= max_w:
            best, lo = c, mid + 1
        else:
            hi = mid - 1
    return best


def _wrap(text: str, font: QFont, max_w: float, max_lines: int) -> list[str]:
    """Word-wrap into at most max_lines lines (very long words are broken); … if cut."""
    words, lines, cur, i = _norm(text).split(), [], "", 0
    while i < len(words):
        test = f"{cur} {words[i]}".strip()
        if _width(test, font) <= max_w:
            cur, i = test, i + 1
            continue
        if not cur:
            cut = len(words[i])
            while cut > 1 and _width(words[i][:cut], font) > max_w:
                cut -= 1
            cur, words[i] = words[i][:cut], words[i][cut:]
        lines.append(cur)
        cur = ""
        if len(lines) == max_lines:
            break
    if cur and len(lines) < max_lines:
        lines.append(cur)
    if i < len(words) and lines:
        lines[-1] = _clamp(lines[-1] + " " + " ".join(words[i:]), font, max_w)
    return lines or [""]


# ---------- layout (from Discogs Label Maker) ----------

MAX_TRACKS_PER_LABEL = 6  # longer tracklists continue on the next label
# Tried in order until the tracklist fits the fewest labels; text size never changes:
# (title lines, track-name lines, tighter rows)
FIT_STEPS = [(2, 3, False), (1, 3, False), (1, 3, True), (1, 2, True), (1, 1, True)]


QR_SIZE = 36  # points (½"): as tall as the header, ~3.5 printer dots per module on a 203 dpi thermal printer


def discogs_url(release_id: int) -> str:
    return f"https://discogs.com/release/{release_id}"


class _Layout:
    def __init__(self, w: float, h: float, data: LabelData, qr: bool = False):
        k = 1.15 if h > 3.2 * PTS else 1.0  # a little larger on 4" × 3⅓" labels
        self.k, self.w, self.h, self.data = k, w, h, data
        self.pad = 7
        self.inner_w = w - 2 * self.pad
        # A QR code to the Discogs page sits top-right on the first label; the header wraps beside it
        self.qr = QR_SIZE * k if qr and data.release_id else 0.0
        self.head_w = self.inner_w - (self.qr + 6 if self.qr else 0)
        self.f_head, self.f_title = _font(11 * k, True), _font(9.5 * k)
        self.f_meta, self.f_row = _font(6.5 * k), _font(8 * k)
        self.head, self.title_size, self.meta, self.row = 11 * k, 9.5 * k, 6.5 * k, 8 * k
        self.has_len = any(t.length for t in data.tracks)
        self.pos_w = max([_width(t.position, self.f_row) for t in data.tracks] + [8]) + 5
        self.len_w = _width("00:00", self.f_row) + 6 if self.has_len else 0
        self.box_w, self.col_gap = 21 * k, 3
        self.track_w = self.inner_w - self.pos_w - self.len_w - 2 * (self.box_w + self.col_gap)
        self.table_head_h = self.meta + 6
        self.apply(FIT_STEPS[0])

    def apply(self, step):
        self.title_lines_max, self.name_lines_max, tight = step
        self.line_h = self.row * (1.08 if tight else 1.2)
        self.row_pad = (1.2 if tight else 2.6) * self.k

    def name_lines(self, t: LabelTrack) -> list[str]:
        return _wrap(t.name, self.f_row, self.track_w - 4, self.name_lines_max)

    def row_h(self, t: LabelTrack) -> float:
        return len(self.name_lines(t)) * self.line_h + self.row_pad

    def title_lines(self) -> list[str]:
        return _wrap(self.data.title, self.f_title, self.head_w, self.title_lines_max)

    def header_h(self) -> float:
        text = self.head * 1.05 + len(self.title_lines()) * self.title_size * 1.15 + self.meta + 7
        return max(text, self.qr + 1) if self.qr else text

    def chunks(self) -> list[list[LabelTrack]]:
        """Tracks per label: the fewest labels MAX_TRACKS_PER_LABEL allows, tightening rows if needed."""
        tracks = self.data.tracks
        space = self.h - 2 * self.pad - self.table_head_h - 1.5

        def pack():
            out, cur, used, room = [], [], 0.0, space - self.header_h()
            for t in tracks:
                rh = self.row_h(t)
                if cur and (len(cur) == MAX_TRACKS_PER_LABEL or used + rh > room):
                    out.append(cur)
                    cur, used, room = [], 0.0, space
                cur.append(t)
                used += rh
            out.append(cur)
            return out

        fewest = max(1, -(-len(tracks) // MAX_TRACKS_PER_LABEL))
        for step in FIT_STEPS:
            self.apply(step)
            result = pack()
            if len(result) <= fewest:
                break
        return result


def _text(p: QPainter, x: float, baseline: float, text: str, font: QFont, max_w: float, align="left"):
    text = _clamp(_norm(text), font, max_w)
    p.setFont(font)
    w = _width(text, font)
    if align == "right":
        x -= w
    elif align == "center":
        x -= w / 2
    p.drawText(QPointF(x * UNIT, baseline * UNIT), text)


def draw_qr(p: QPainter, x: float, y: float, size: float, text: str):
    """A QR code `size` points square with its top-left at (x, y)."""
    import segno

    matrix = segno.make(text, error="l", boost_error=False).matrix
    module = size / len(matrix)
    p.save()
    p.setPen(Qt.PenStyle.NoPen)
    p.setBrush(QColor(0, 0, 0))
    for r, row in enumerate(matrix):
        for c, dark in enumerate(row):
            if dark:
                # A hair oversized so neighbouring modules print as one solid block
                p.drawRect(QRectF((x + c * module) * UNIT, (y + r * module) * UNIT,
                                  module * UNIT + 1, module * UNIT + 1))
    p.restore()


def draw_label(p: QPainter, x: float, y: float, lay: _Layout, tracks: list[LabelTrack], first: bool,
               thermal: bool = False):
    """One label with its top-left corner at (x, y) points. The first label of a record has the header."""
    d = lay.data
    ink = QColor(0, 0, 0)
    grey = ink if thermal else QColor(97, 97, 97)  # thermal printers can't do grey cleanly
    left, right = x + lay.pad, x + lay.pad + lay.inner_w
    yy = y + lay.pad
    p.setPen(ink)

    if first:
        top = yy
        lead = (d.label or "Various Artists") if (d.artist.lower() in VARIOUS or not d.artist) else d.artist
        yy += lay.head * 0.8
        _text(p, left, yy, lead, lay.f_head, lay.head_w)
        yy += lay.head * 0.25
        for line in lay.title_lines():
            yy += lay.title_size * 1.15
            _text(p, left, yy, line, lay.f_title, lay.head_w)
        yy += lay.meta + 3
        # The Discogs ID goes in the corner, unless the QR code (which links there) takes its place
        id_text = f"ID {d.release_id}" if d.release_id and not lay.qr else ""
        id_w = _width(id_text, lay.f_meta)
        meta = [m for m in (d.label if lead != d.label else "", d.year, ", ".join(d.styles)) if m]
        p.setPen(grey)
        _text(p, left, yy, " · ".join(meta), lay.f_meta, lay.head_w - id_w - (8 if id_text else 0))
        if id_text:
            _text(p, right, yy, id_text, lay.f_meta, id_w + 1, align="right")
        p.setPen(ink)
        yy += 4
        if lay.qr:
            draw_qr(p, right - lay.qr, top, lay.qr, discogs_url(d.release_id))
            yy = max(yy, top + lay.qr + 1)

    # Track table: Pos | Track | Len | BPM | Rtg (the boxes stay drawn to write in by hand)
    x_pos, x_track = left, left + lay.pos_w
    x_len = x_track + lay.track_w
    x_bpm = x_len + lay.len_w
    x_rtg = x_bpm + lay.box_w + lay.col_gap
    table_top = yy
    rows_top = table_top + lay.table_head_h - 1.5
    p.setPen(QPen(ink, 0.5 * UNIT))
    p.drawLine(QPointF(left * UNIT, table_top * UNIT), QPointF(right * UNIT, table_top * UNIT))
    p.setPen(grey)
    hdr = table_top + lay.meta + 1.5
    heads = [(x_pos, "Pos"), (x_track, "Track")] + ([(x_len, "Len")] if lay.has_len else []) + \
            [(x_bpm, "BPM"), (x_rtg, "Rtg")]
    for hx, h in heads:
        _text(p, hx, hdr, h, lay.f_meta, 40)
    p.setPen(QPen(ink, 0.5 * UNIT))
    p.drawLine(QPointF(left * UNIT, rows_top * UNIT), QPointF(right * UNIT, rows_top * UNIT))

    box_pen = QPen(ink, 0.6 * UNIT)
    cell = rows_top + 1.5
    for t in tracks:
        lines = lay.name_lines(t)
        h = len(lines) * lay.line_h + lay.row_pad
        base = cell + lay.row_pad / 2 + lay.row * 0.95
        p.setPen(ink)
        _text(p, x_pos, base, t.position, lay.f_row, lay.pos_w - 3)
        p.setFont(lay.f_row)
        for j, line in enumerate(lines):
            p.drawText(QPointF(x_track * UNIT, (base + j * lay.line_h) * UNIT), line)
        if t.length:
            _text(p, x_len, base, t.length, lay.f_row, lay.len_w - 3)
        p.setPen(box_pen)
        p.setBrush(Qt.BrushStyle.NoBrush)
        for bx in (x_bpm, x_rtg):
            p.drawRect(QRectF(bx * UNIT, cell * UNIT, lay.box_w * UNIT, h * UNIT))
        p.setPen(ink)
        if t.bpm:
            _text(p, x_bpm + lay.box_w / 2, base, t.bpm, lay.f_row, lay.box_w - 2, align="center")
        cell += h


# ---------- pages ----------

@dataclass
class Placed:
    page: int
    x: float  # points from the page's top-left
    y: float
    tracks: list[LabelTrack]
    first: bool
    lay: "_Layout | None" = None  # the record's layout (each record on a sheet has its own)


def page_size(tmpl: Template) -> QSizeF:
    """Page size in inches: a letter sheet, or one label for a roll."""
    return QSizeF(8.5, 11) if tmpl.sheet else QSizeF(tmpl.label_w, tmpl.label_h)


def slots(tmpl: Template) -> list[tuple[float, float]]:
    """Top-left corner (points) of every label position on one page, left to right, top to bottom."""
    if not tmpl.sheet:
        return [(0.0, 0.0)]
    w, h = tmpl.label_w * PTS, tmpl.label_h * PTS
    gap_x = (8.5 * PTS - 2 * tmpl.margin_lr * PTS - tmpl.cols * w) / max(tmpl.cols - 1, 1)
    gap_y = (11 * PTS - 2 * tmpl.margin_tb * PTS - tmpl.rows * h) / max(tmpl.rows - 1, 1)
    return [(tmpl.margin_lr * PTS + c * (w + gap_x), tmpl.margin_tb * PTS + r * (h + gap_y))
            for r in range(tmpl.rows) for c in range(tmpl.cols)]


Records = LabelData | list[LabelData]


def place(tmpl: Template, records: Records, copies: int = 1, start: int = 1,
          qr: bool = False) -> tuple[list[Placed], _Layout]:
    """Where each label goes: the records one after another (each with its continuation labels).
    On sheets, `start` skips labels already used (1 = top-left). qr: add the Discogs QR code.
    Also returns the first record's layout."""
    records = records if isinstance(records, list) else [records]
    labels = []
    for data in records:
        lay = _Layout(tmpl.label_w * PTS, tmpl.label_h * PTS, data, qr)
        chunks = lay.chunks()
        labels += [(c, i == 0, lay) for _ in range(max(1, copies)) for i, c in enumerate(chunks)]
    positions = slots(tmpl)
    slot = max(0, min(start, len(positions)) - 1) if tmpl.sheet else 0
    placed = []
    for c, f, lay in labels:
        page, k = divmod(slot, len(positions))
        placed.append(Placed(page, *positions[k], c, f, lay))
        slot += 1
    return placed, placed[0].lay


def paint_pages(p: QPainter, device_dpi: float, tmpl: Template, data: Records, copies: int = 1,
                start: int = 1, new_page=None, rotate: bool = False, outlines: bool = False,
                max_pages: int | None = None, qr: bool = False) -> int:
    """Draw the pages on painter p (a printer, PDF or image); new_page() is called between pages.
    data: one record or several (a label queue). rotate: turn roll labels 90° for printers whose
    driver feeds labels sideways. outlines: draw every label position's edge (for the on-screen preview).
    Returns the number of pages."""
    placed, _ = place(tmpl, data, copies, start, qr)
    pages = max(pl.page for pl in placed) + 1
    w, h = tmpl.label_w * PTS, tmpl.label_h * PTS
    for page in range(min(pages, max_pages or pages)):
        if page and new_page:
            new_page()
        p.save()
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.scale(device_dpi / PTS / UNIT, device_dpi / PTS / UNIT)
        if rotate and not tmpl.sheet:
            p.translate(h * UNIT, 0)
            p.rotate(90)
        if outlines:
            p.setPen(QPen(QColor(185, 185, 185), 0.75 * UNIT, Qt.PenStyle.DashLine))
            p.setBrush(Qt.BrushStyle.NoBrush)
            for x, y in slots(tmpl):
                p.drawRoundedRect(QRectF(x * UNIT, y * UNIT, w * UNIT, h * UNIT), 6 * UNIT, 6 * UNIT)
        for pl in placed:
            if pl.page == page:
                draw_label(p, pl.x, pl.y, pl.lay, pl.tracks, pl.first, tmpl.thermal)
        p.restore()
    return pages


def page_layout(tmpl: Template, rotate: bool = False) -> QPageLayout:
    size = page_size(tmpl)
    if rotate and not tmpl.sheet:
        size = QSizeF(size.height(), size.width())
    # Describe the page as portrait plus an orientation, so drivers see the real label size
    portrait = QSizeF(min(size.width(), size.height()), max(size.width(), size.height()))
    orientation = (QPageLayout.Orientation.Landscape if size.width() > size.height()
                   else QPageLayout.Orientation.Portrait)
    return QPageLayout(QPageSize(portrait, QPageSize.Unit.Inch, f"{tmpl.label_w:g}x{tmpl.label_h:g} label"
                                 if not tmpl.sheet else "", QPageSize.SizeMatchPolicy.ExactMatch),
                       orientation, QMarginsF(0, 0, 0, 0))


def save_pdf(path: Path, tmpl: Template, data: Records, copies: int = 1, start: int = 1,
             rotate: bool = False, qr: bool = False) -> int:
    writer = QPdfWriter(str(path))
    writer.setResolution(600)
    writer.setPageLayout(page_layout(tmpl, rotate))
    first = data[0] if isinstance(data, list) else data
    writer.setTitle(f"{first.artist} – {first.title} labels" if not isinstance(data, list) or len(data) == 1
                    else f"Labels for {len(data)} records")
    p = QPainter(writer)
    pages = paint_pages(p, 600, tmpl, data, copies, start, writer.newPage, rotate, qr=qr)
    p.end()
    return pages


def preview(tmpl: Template, data: Records, start: int = 1, dpi: int = 96, rotate: bool = False,
            qr: bool = False) -> QImage:
    """The first page as an image, with label outlines, for showing on screen."""
    size = page_size(tmpl)
    if rotate and not tmpl.sheet:
        size = QSizeF(size.height(), size.width())
    img = QImage(int(size.width() * dpi), int(size.height() * dpi), QImage.Format.Format_RGB32)
    img.fill(QColor(255, 255, 255))
    p = QPainter(img)
    paint_pages(p, dpi, tmpl, data, 1, start, rotate=rotate, outlines=True, max_pages=1, qr=qr)
    p.end()
    return img


def print_to(printer, tmpl: Template, data: Records, copies: int = 1, start: int = 1,
             rotate: bool = False, qr: bool = False) -> int:
    """Print on a QPrinter already set up (and chosen in the print dialog)."""
    p = QPainter(printer)
    pages = paint_pages(p, printer.resolution(), tmpl, data, copies, start, printer.newPage, rotate, qr=qr)
    p.end()
    return pages
