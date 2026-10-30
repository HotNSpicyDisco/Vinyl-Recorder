"""The Labels tab: make sleeve labels on their own, without recording.

Pick records from your Discogs collection (or the album you're ripping, or one typed in by hand),
arrange them on the sheet (the label queue), and print or save a PDF. Lengths and BPMs come from
your saved tracks where there are any, otherwise from Discogs.
"""

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QAbstractItemView, QCheckBox, QGroupBox, QHBoxLayout, QLabel, QLineEdit, QListWidget, QListWidgetItem,
    QPushButton, QSplitter, QVBoxLayout, QWidget,
)

from .. import labels, library
from .print_labels import LabelOutput, offer_to_clear_queue


class LabelsTab(QWidget):
    """host: the main window, which owns the collection, the label queue, Discogs access and settings."""

    def __init__(self, host):
        super().__init__()
        self.host = host
        self.queue: library.LabelQueue = host.label_queue
        self._adding: list[int] = []
        lay = QHBoxLayout(self)
        split = QSplitter()
        lay.addWidget(split)

        # 1. Pick records
        pick = QGroupBox("1. Pick records")
        v = QVBoxLayout(pick)
        row = QHBoxLayout()
        self.filter = QLineEdit()
        self.filter.setPlaceholderText("Filter your collection…")
        self.filter.setClearButtonEnabled(True)
        self.filter.textChanged.connect(self.fill_collection)
        row.addWidget(self.filter, 1)
        self.vinyl_only = QCheckBox("Vinyl only")
        self.vinyl_only.setChecked(True)
        self.vinyl_only.toggled.connect(self.fill_collection)
        row.addWidget(self.vinyl_only)
        self.hide_lps = QCheckBox("Hide LPs")
        self.hide_lps.setToolTip("Most DJs only label the 12\"s, EPs and singles they play out. With this on, LPs "
                                 "are hidden here and saved LPs aren't added to the label queue.")
        self.hide_lps.setChecked(host.settings.labels_skip_lps)
        self.hide_lps.toggled.connect(self._hide_lps_toggled)
        row.addWidget(self.hide_lps)
        v.addLayout(row)
        self.coll = QListWidget()
        self.coll.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.coll.setToolTip("Ctrl- or Shift-click to pick several. Green: saved; orange: part done; "
                                "blue: already in your Rekordbox library.")
        self.coll.itemDoubleClicked.connect(lambda _: self.add_selected())
        v.addWidget(self.coll, 1)
        self.add_btn = QPushButton("Add selected to the sheet  →")
        self.add_btn.clicked.connect(self.add_selected)
        v.addWidget(self.add_btn)
        row = QHBoxLayout()
        self.add_current_btn = QPushButton("Add the album being ripped")
        self.add_current_btn.clicked.connect(self.add_current)
        row.addWidget(self.add_current_btn)
        b = QPushButton("Enter one by hand…")
        b.setToolTip("For white labels and records that aren't on Discogs")
        b.clicked.connect(self.add_by_hand)
        row.addWidget(b)
        v.addLayout(row)
        self.pick_info = QLabel()
        self.pick_info.setWordWrap(True)
        self.pick_info.setStyleSheet("color: gray;")
        v.addWidget(self.pick_info)
        split.addWidget(pick)

        # 2. On the sheet
        sheet = QGroupBox("2. On the sheet")
        v = QVBoxLayout(sheet)
        self.sheet = QListWidget()
        self.sheet.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.sheet.currentRowChanged.connect(self._jump_to)
        v.addWidget(self.sheet, 1)
        row = QHBoxLayout()
        for text, tip, fn in (("↑", "Move up", lambda: self._move(-1)), ("↓", "Move down", lambda: self._move(1))):
            b = QPushButton(text)
            b.setToolTip(tip)
            b.setFixedWidth(34)
            b.clicked.connect(fn)
            row.addWidget(b)
        b = QPushButton("Remove")
        b.clicked.connect(self._remove)
        row.addWidget(b)
        b = QPushButton("Clear")
        b.clicked.connect(self._clear)
        row.addWidget(b)
        v.addLayout(row)
        note = QLabel("This is the label queue: saved albums are added to it automatically.")
        note.setWordWrap(True)
        note.setStyleSheet("color: gray;")
        v.addWidget(note)
        split.addWidget(sheet)

        # 3. Print
        out = QGroupBox("3. Print")
        v = QVBoxLayout(out)
        self.output = LabelOutput(host.settings, self.records, self, printed=self._printed, queue_fn=lambda: True)
        v.addWidget(self.output)
        split.addWidget(out)
        split.setSizes([330, 260, 760])
        self.refresh()

    # --- the records on the sheet ---

    def records(self) -> list[labels.LabelData]:
        return [labels.from_dict(d) for d in self.queue.items]

    def refresh(self):
        """Re-read the queue and collection (another window may have changed them)."""
        self.hide_lps.blockSignals(True)
        self.hide_lps.setChecked(self.host.settings.labels_skip_lps)  # may have changed in Settings
        self.hide_lps.blockSignals(False)
        self.fill_collection()
        self.fill_sheet()
        self.add_current_btn.setEnabled(self.host.release is not None)
        self.host._load_rekordbox()  # picks up anything Rekordbox has analysed since

    def fill_sheet(self, select: int | None = None):
        self.sheet.blockSignals(True)
        self.sheet.clear()
        for d in self.queue.items:
            self.sheet.addItem(f"{d.get('artist')} – {d.get('title')}")
        if select is not None and 0 <= select < self.sheet.count():
            self.sheet.setCurrentRow(select)
        self.sheet.blockSignals(False)
        self.output.refresh()

    def _jump_to(self, row: int):
        """Show the sheet that the selected record is on."""
        if row < 0:
            return
        out = self.output
        placed, _ = labels.place(out.tmpl(), self.records(), out.copies.value(), out.start.value(),
                                 out.qr.isChecked(), out.key_column())
        title = self.queue.items[row].get("title")
        artist = self.queue.items[row].get("artist")
        page = next((p.page for p in placed if p.first and p.lay.data.title == title and p.lay.data.artist == artist),
                    None)
        if page is not None and page != out.page:
            out.page = page
            out.refresh()

    def _rows(self) -> list[int]:
        return sorted(self.sheet.row(i) for i in self.sheet.selectedItems())

    def _move(self, step: int):
        rows = self._rows()
        if len(rows) != 1:
            return
        self.fill_sheet(select=self.queue.move(rows[0], step))

    def _remove(self):
        self.queue.remove(self._rows())
        self.fill_sheet()
        self.fill_collection()

    def _clear(self):
        if self.queue.items and offer_to_clear_queue(self, self.queue):
            self.fill_sheet()
            self.fill_collection()

    def _printed(self, _queue_mode: bool):
        if offer_to_clear_queue(self, self.queue):
            self.fill_sheet()
            self.fill_collection()

    # --- picking ---

    def fill_collection(self):
        h = self.host
        text = self.filter.text().strip()
        items = [i for i in h.collection.items
                 if (not text or i.matches(text)) and (not self.vinyl_only.isChecked() or i.is_vinyl)
                 and not (self.hide_lps.isChecked() and i.is_lp)]
        items.sort(key=lambda i: (i.artist.lower().removeprefix("the "), i.year, i.title.lower()))
        on_sheet = {d.get("release_id") for d in self.queue.items}
        self.coll.clear()
        for it in items:
            mark = "  ✓ on sheet" if it.id in on_sheet else ""
            item = QListWidgetItem(f"{it.name}{mark}")
            item.setData(Qt.ItemDataRole.UserRole, it.id)
            item.setToolTip(it.subtitle)
            status = h.riplog.status(it.id)
            if status == library.SAVED:
                item.setForeground(QColor("#2da44e"))
            elif status == library.PARTIAL:
                item.setForeground(QColor("#d29922"))
            elif it.id in h._in_rb:
                item.setForeground(h.IN_REKORDBOX)
            self.coll.addItem(item)
        if not h.collection.items:
            self.pick_info.setText("Your Discogs collection isn't loaded: add your token in File → Settings. "
                                   "You can still add the album being ripped, or enter one by hand.")
        elif not self._adding:
            rb = " and your Rekordbox library" if h.rb_cache.library and h.settings.rekordbox_library else ""
            self.pick_info.setText(f"{len(items)} records. Lengths, BPMs and keys come from your saved "
                                   f"tracks{rb}.")

    def _hide_lps_toggled(self, on: bool):
        self.host.settings.labels_skip_lps = on
        self.host.settings.save()
        self.fill_collection()

    def add_selected(self):
        ids = [i.data(Qt.ItemDataRole.UserRole) for i in self.coll.selectedItems()]
        on_sheet = {d.get("release_id") for d in self.queue.items}
        new = [i for i in ids if i not in on_sheet and i not in self._adding]
        if not new:
            return
        self._adding += new
        self.add_btn.setEnabled(False)
        self._add_next()

    def _add_next(self):
        """Fetch and add the selected records one at a time (Discogs allows about one request a second)."""
        if not self._adding:
            self.add_btn.setEnabled(True)
            self.fill_collection()
            return
        rid = self._adding[0]
        self.pick_info.setText(f"Getting tracklists from Discogs… {len(self._adding)} to go")
        h = self.host
        cached = h._release_cache.get(rid)
        client = h._client()

        def fetch():
            return cached or client.release(rid)

        def done(rel):
            h._release_cache[rel.id] = rel
            label, _ = h._label_for(h.edits.apply(rel))  # your tracklist corrections and measured BPMs
            self.queue.add(labels.to_dict(label))
            self._adding.pop(0)
            self.fill_sheet()
            self._add_next()

        def failed(msg):
            self._adding.pop(0)
            self.pick_info.setText(f"Couldn't get one of the records: {msg}")
            self._add_next()

        h.run_bg(fetch, done, failed)

    def add_current(self):
        if self.host.release:
            label, _ = self.host._label_for(self.host.release)
            self.queue.add(labels.to_dict(label))
            self.fill_sheet()
            self.fill_collection()

    def add_by_hand(self):
        from .tracklist_editor import TracklistEditor

        dlg = TracklistEditor(None, None, self)
        if dlg.exec():
            rel = dlg.result_release()
            self.host.edits.save(rel)
            self.queue.add(labels.to_dict(labels.label_data(rel)))
            self.fill_sheet()
