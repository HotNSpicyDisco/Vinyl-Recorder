"""Edit a record's tracklist by hand: fix a wrong Discogs entry, or type in a record that isn't on Discogs."""

import time
from dataclasses import replace

from PySide6.QtWidgets import (
    QAbstractItemView, QDialog, QDialogButtonBox, QFormLayout, QHBoxLayout, QHeaderView, QLabel, QLineEdit,
    QMessageBox, QPushButton, QTableWidget, QTableWidgetItem, QVBoxLayout,
)

from ..discogs import Release, Track, parse_duration

COLS = ["Position", "Title", "Artist (if not the album's)", "Length (m:ss)"]


def _length_text(seconds: float | None) -> str:
    if not seconds:
        return ""
    m, s = divmod(int(round(seconds)), 60)
    return f"{m}:{s:02d}"


class TracklistEditor(QDialog):
    """Returns the edited release from `result_release()` after exec() is accepted.

    release: the record as it is now (None to enter a new one); original: the Discogs version,
    offered as "Back to the Discogs version" once you've changed it.
    """

    def __init__(self, release: Release | None, original: Release | None = None, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Edit tracklist" if release else "Enter a record by hand")
        self.resize(760, 560)
        self.release = release
        self.original = original
        lay = QVBoxLayout(self)

        form = QFormLayout()
        r = release
        self.artist = QLineEdit(r.artist if r else "")
        self.title = QLineEdit(r.title if r else "")
        self.year = QLineEdit(r.year if r else "")
        self.label = QLineEdit(r.label if r else "")
        self.catno = QLineEdit(r.catno if r else "")
        self.artist.setPlaceholderText("e.g. Unknown Artist")
        self.title.setPlaceholderText("e.g. White Label 004")
        form.addRow("Artist:", self.artist)
        form.addRow("Album / title:", self.title)
        row = QHBoxLayout()
        for text, w in (("Year", self.year), ("Label", self.label), ("Cat #", self.catno)):
            row.addWidget(QLabel(text))
            row.addWidget(w)
        form.addRow("", row)
        lay.addLayout(form)

        hint = QLabel("Positions decide the sides: <b>A1, A2</b> are side A and <b>B1</b> is side B (A and AA "
                      "work too). Lengths are optional, but they help find the gaps between tracks.")
        hint.setWordWrap(True)
        lay.addWidget(hint)

        self.table = QTableWidget(0, len(COLS))
        self.table.setHorizontalHeaderLabels(COLS)
        self.table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        for col, width in ((0, 80), (2, 190), (3, 110)):
            self.table.setColumnWidth(col, width)
        self.table.verticalHeader().setVisible(False)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        lay.addWidget(self.table, 1)
        self._fill(r.tracks if r else [])

        row = QHBoxLayout()
        for text, fn in (("Add track", self._add), ("Remove", self._remove), ("Move up", lambda: self._move(-1)),
                         ("Move down", lambda: self._move(1))):
            b = QPushButton(text)
            b.clicked.connect(fn)
            row.addWidget(b)
        row.addStretch()
        if original is not None:
            b = QPushButton("Back to the Discogs version")
            b.clicked.connect(self._revert)
            row.addWidget(b)
        lay.addLayout(row)

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        lay.addWidget(buttons)
        self._result: Release | None = None

    def _fill(self, tracks: list[Track]):
        self.table.setRowCount(0)
        for t in tracks:
            self._add_row(t.position, t.title, t.artist, _length_text(t.duration))
        if not tracks:
            for pos in ("A1", "A2", "B1", "B2"):
                self._add_row(pos, "", "", "")

    def _add_row(self, *cells, at: int | None = None):
        r = self.table.rowCount() if at is None else at
        self.table.insertRow(r)
        for c, text in enumerate(cells):
            self.table.setItem(r, c, QTableWidgetItem(text))

    def _cells(self, r: int) -> list[str]:
        return [(self.table.item(r, c).text().strip() if self.table.item(r, c) else "") for c in range(len(COLS))]

    def _add(self):
        r = self.table.currentRow()
        self._add_row("", "", "", "", at=r + 1 if r >= 0 else None)
        new = r + 1 if r >= 0 else self.table.rowCount() - 1
        self.table.setCurrentCell(new, 0)
        self.table.editItem(self.table.item(new, 0))

    def _remove(self):
        rows = sorted({i.row() for i in self.table.selectedIndexes()}, reverse=True)
        for r in rows:
            self.table.removeRow(r)

    def _move(self, step: int):
        r = self.table.currentRow()
        to = r + step
        if r < 0 or not 0 <= to < self.table.rowCount():
            return
        cells = self._cells(r)
        self.table.removeRow(r)
        self._add_row(*cells, at=to)
        self.table.setCurrentCell(to, 0)

    def _revert(self):
        o = self.original
        for w, v in ((self.artist, o.artist), (self.title, o.title), (self.year, o.year), (self.label, o.label),
                     (self.catno, o.catno)):
            w.setText(v)
        self._fill(o.tracks)

    def accept(self):
        tracks = []
        for r in range(self.table.rowCount()):
            pos, title, artist, length = self._cells(r)
            if not (pos or title):
                continue  # an empty row
            if length and parse_duration(length) is None:
                QMessageBox.warning(self, "Length", f"Row {r + 1}: write lengths like 5:32.")
                return
            tracks.append(Track(pos or str(len(tracks) + 1), title or f"Track {len(tracks) + 1}",
                                parse_duration(length), artist, len(tracks) + 1))
        if not tracks:
            QMessageBox.warning(self, "Tracklist", "Add at least one track.")
            return
        details = dict(artist=self.artist.text().strip() or "Unknown Artist",
                       title=self.title.text().strip() or "Untitled", year=self.year.text().strip(),
                       label=self.label.text().strip(), catno=self.catno.text().strip())
        if self.release:
            self._result = replace(self.release, tracks=tracks, **details)
        else:  # not on Discogs: a negative id can never clash with a Discogs release
            self._result = Release(-int(time.time()), genres=[], styles=[], tracks=tracks, cover_url="", url="",
                                   **details)
        super().accept()

    def result_release(self) -> Release | None:
        return self._result
