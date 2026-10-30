"""Printing labels: the shared print controls and preview (LabelOutput), and the quick print window
used from the Album and Split & save tabs (PrintLabelsDialog). The Labels tab uses LabelOutput too."""

from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtGui import QPixmap
from PySide6.QtPrintSupport import QPrintDialog, QPrinter, QPrinterInfo
from PySide6.QtWidgets import (
    QAbstractItemView, QButtonGroup, QCheckBox, QComboBox, QDialog, QFileDialog, QFormLayout, QHBoxLayout, QLabel,
    QListWidget, QMessageBox, QPushButton, QRadioButton, QSpinBox, QToolButton, QVBoxLayout, QWidget,
)

from .. import labels
from ..export import safe_filename
from ..library import LabelQueue
from ..settings import Settings

PREVIEW_W = 440  # pixels


class LabelOutput(QWidget):
    """Template, start position, copies, rotate, QR; a page-by-page preview; Print and Save PDF.

    records_fn() gives the records to print. printed(queue_mode) is called after a print or PDF;
    `queue_mode` comes from queue_fn() (whether the label queue was what got printed)."""

    def __init__(self, settings: Settings, records_fn, parent=None, printed=None, queue_fn=lambda: False):
        super().__init__(parent)
        self.settings, self.records_fn, self.printed, self.queue_fn = settings, records_fn, printed, queue_fn
        self.page = 0
        lay = QHBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        left = QVBoxLayout()
        form = QFormLayout()
        self.template = QComboBox()
        for t in labels.TEMPLATES:
            self.template.addItem(t.name, t.id)
        self.template.setCurrentIndex(max(0, self.template.findData(settings.label_template)))
        form.addRow("Labels:", self.template)
        self.start = QSpinBox()
        self.start.setToolTip("Using a sheet that's partly used? Start at the first free label "
                              "(counted left to right, top to bottom).")
        form.addRow("Start at label:", self.start)
        self.copies = QSpinBox()
        self.copies.setRange(1, 50)
        form.addRow("Copies of each:", self.copies)
        self.rotate = QCheckBox("Turn 90° (if your printer prints the label sideways)")
        self.rotate.setChecked(settings.label_rotate)
        form.addRow("", self.rotate)
        self.qr = QCheckBox("QR code to the record's Discogs page")
        self.qr.setChecked(settings.label_qr)
        form.addRow("", self.qr)
        left.addLayout(form)
        self.info = QLabel()
        self.info.setWordWrap(True)
        left.addWidget(self.info)
        tips = QLabel("<b>Thermal printers:</b> load 4\" wide labels and choose the matching label size in the "
                      "print window if it asks. <b>Avery sheets:</b> print at 100% / Actual size, not "
                      "\"Fit to page\".")
        tips.setWordWrap(True)
        tips.setStyleSheet("color: gray;")
        left.addWidget(tips)
        left.addStretch()
        row = QHBoxLayout()
        self.pdf_btn = QPushButton("Save PDF…")
        self.pdf_btn.clicked.connect(self.save_pdf)
        row.addWidget(self.pdf_btn)
        self.print_btn = QPushButton("Print…")
        self.print_btn.setDefault(True)
        self.print_btn.setMinimumHeight(34)
        self.print_btn.clicked.connect(self.print)
        row.addWidget(self.print_btn, 1)
        left.addLayout(row)
        lay.addLayout(left, 1)

        right = QVBoxLayout()
        self.preview = QLabel()
        self.preview.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.preview.setMinimumWidth(PREVIEW_W + 20)
        self.preview.setStyleSheet("background: #8b8f96; padding: 10px;")
        right.addWidget(self.preview, 1)
        nav = QHBoxLayout()
        self.prev_btn = QToolButton(text="◀")
        self.prev_btn.clicked.connect(lambda: self._turn(-1))
        self.next_btn = QToolButton(text="▶")
        self.next_btn.clicked.connect(lambda: self._turn(1))
        self.page_label = QLabel()
        self.page_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        nav.addWidget(self.prev_btn)
        nav.addWidget(self.page_label, 1)
        nav.addWidget(self.next_btn)
        right.addLayout(nav)
        lay.addLayout(right)

        for w in (self.template, self.start, self.copies):
            (w.currentIndexChanged if isinstance(w, QComboBox) else w.valueChanged).connect(self.refresh)
        for b in (self.rotate, self.qr):
            b.toggled.connect(self.refresh)

    def tmpl(self) -> labels.Template:
        return labels.TEMPLATES_BY_ID[self.template.currentData()]

    def _turn(self, step: int):
        self.page += step
        self.refresh()

    def refresh(self, *_):
        t = self.tmpl()
        self.start.blockSignals(True)
        self.start.setRange(1, t.per_page)
        self.start.blockSignals(False)
        self.start.setEnabled(t.sheet)
        self.rotate.setVisible(not t.sheet)
        records = self.records_fn()
        for b in (self.print_btn, self.pdf_btn):
            b.setEnabled(bool(records))
        if not records:
            self.info.setText("Nothing to print yet.")
            self.preview.clear()
            self.page_label.setText("")
            self.prev_btn.setEnabled(False)
            self.next_btn.setEnabled(False)
            return
        qr, copies, start = self.qr.isChecked(), self.copies.value(), self.start.value()
        placed, _ = labels.place(t, records, copies, start, qr)
        pages = max(p.page for p in placed) + 1
        self.page = max(0, min(self.page, pages - 1))
        what = f"{len(records)} records" if len(records) > 1 else f"{len(records[0].tracks)} tracks"
        text = f"{what}: {len(placed)} label{'s' if len(placed) > 1 else ''}"
        if any(not p.first for p in placed):
            text += " (long tracklists carry on onto the next label)"
        text += f", {pages} {'sheet' if t.sheet else 'label'}{'s' if pages > 1 else ''}."
        self.info.setText(text)
        noun = "Sheet" if t.sheet else "Label"
        self.page_label.setText(f"{noun} {self.page + 1} of {pages}")
        self.prev_btn.setEnabled(self.page > 0)
        self.next_btn.setEnabled(self.page < pages - 1)
        size = labels.page_size(t)
        rotated = self.rotate.isChecked() and not t.sheet
        width_in = size.height() if rotated else size.width()
        img = labels.preview(t, records, start, dpi=int(PREVIEW_W / width_in), rotate=rotated, qr=qr,
                             page=self.page, copies=copies)
        self.preview.setPixmap(QPixmap.fromImage(img))

    def _remember(self):
        t = self.tmpl()
        self.settings.label_template = t.id
        self.settings.label_qr = self.qr.isChecked()
        if not t.sheet:
            self.settings.label_rotate = self.rotate.isChecked()
        self.settings.save()

    def print(self):
        t = self.tmpl()
        rotate = self.rotate.isChecked() and not t.sheet
        records = self.records_fn()
        printer = QPrinter(QPrinter.PrinterMode.HighResolution)
        last = self.settings.label_printers.get(t.id)
        if last and last in QPrinterInfo.availablePrinterNames():
            printer.setPrinterName(last)
        printer.setFullPage(True)  # draw from the paper's corner: the labels are placed exactly
        printer.setPageLayout(labels.page_layout(t, rotate))
        printer.setDocName(f"Labels: {records[0].artist} – {records[0].title}" if len(records) == 1
                           else f"Labels: {len(records)} records")
        dlg = QPrintDialog(printer, self)
        dlg.setWindowTitle("Print labels")
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return
        # Picking a printer in the dialog can swap in its default paper: put the label size back
        printer.setPageLayout(labels.page_layout(t, rotate))
        printer.setFullPage(True)
        try:
            labels.print_to(printer, t, records, self.copies.value(), self.start.value(), rotate,
                            self.qr.isChecked())
        except Exception as e:  # driver problems: say so instead of failing silently
            QMessageBox.warning(self, "Print labels", f"Printing failed: {e}")
            return
        self.settings.label_printers[t.id] = printer.printerName()
        self._remember()
        if self.printed:
            self.printed(self.queue_fn())

    def save_pdf(self):
        t = self.tmpl()
        records = self.records_fn()
        name = (f"{records[0].artist} - {records[0].title}" if len(records) == 1 else f"{len(records)} records")
        path, _ = QFileDialog.getSaveFileName(
            self, "Save labels as PDF",
            str(Path(self.settings.output_dir) / safe_filename(f"{name} - labels ({t.id}).pdf")), "PDF (*.pdf)")
        if not path:
            return
        labels.save_pdf(Path(path), t, records, self.copies.value(), self.start.value(),
                        self.rotate.isChecked() and not t.sheet, self.qr.isChecked())
        self._remember()
        QMessageBox.information(self, "Print labels", f"Saved {Path(path).name}")
        if self.printed:
            self.printed(self.queue_fn())


def offer_to_clear_queue(parent, queue: LabelQueue) -> bool:
    """After printing the queue: clear it? Returns True if it was cleared."""
    if queue.items and QMessageBox.question(
            parent, "Label queue", "Done with these? Clear them from the label queue?") == \
            QMessageBox.StandardButton.Yes:
        queue.clear()
        return True
    return False


class PrintLabelsDialog(QDialog):
    """Quick printing from the Album / Split & save tabs: this record, or the label queue.

    record: the label for the record you're on (None to open on the queue only);
    measured: whether its lengths/BPMs come from saved tracks (shown as a note)."""

    def __init__(self, settings: Settings, queue: LabelQueue, record: labels.LabelData | None = None,
                 measured: bool = False, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Print labels")
        self.queue, self.record = queue, record
        lay = QHBoxLayout(self)
        left = QVBoxLayout()

        self.one = QRadioButton(f"This record: {record.artist} – {record.title}" if record else "This record")
        self.many = QRadioButton()
        group = QButtonGroup(self)
        group.addButton(self.one)
        group.addButton(self.many)
        self.one.setEnabled(record is not None)
        (self.one if record else self.many).setChecked(True)
        left.addWidget(self.one)
        self.source = QLabel("Lengths and BPMs measured from your saved tracks." if measured else
                             "Lengths from Discogs. Save the tracks first to print the measured lengths and BPMs.")
        self.source.setWordWrap(True)
        self.source.setStyleSheet("color: gray; margin-left: 22px;")
        left.addWidget(self.source)
        self.add_btn = QPushButton("Add this record to the label queue")
        self.add_btn.setToolTip("Collect records and print a whole sheet of different ones later (Labels tab)")
        self.add_btn.clicked.connect(self._add_to_queue)
        row = QHBoxLayout()
        row.addSpacing(22)
        row.addWidget(self.add_btn)
        row.addStretch()
        left.addLayout(row)
        left.addWidget(self.many)
        self.queue_list = QListWidget()
        self.queue_list.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.queue_list.setMaximumHeight(130)
        left.addWidget(self.queue_list)
        row = QHBoxLayout()
        self.remove_btn = QPushButton("Remove selected")
        self.remove_btn.clicked.connect(self._remove_selected)
        row.addWidget(self.remove_btn)
        row.addStretch()
        left.addLayout(row)
        lay.addLayout(left)

        self.output = LabelOutput(settings, self._records, self, printed=self._printed,
                                  queue_fn=self.many.isChecked)
        out = QVBoxLayout()
        out.addWidget(self.output, 1)
        close = QPushButton("Close")
        close.clicked.connect(self.reject)
        out.addWidget(close, alignment=Qt.AlignmentFlag.AlignRight)
        lay.addLayout(out, 2)
        for b in (self.one, self.many):
            b.toggled.connect(self._update)
        self._fill_queue()
        self._update()

    # aliases kept for scripts and tests that drive the dialog
    @property
    def template(self):
        return self.output.template

    @property
    def start(self):
        return self.output.start

    @property
    def info(self):
        return self.output.info

    def _fill_queue(self):
        self.queue_list.clear()
        for d in self.queue.items:
            self.queue_list.addItem(f"{d.get('artist')} – {d.get('title')}")
        n = len(self.queue.items)
        self.many.setText(f"Label queue: {n} record{'s' if n != 1 else ''}")
        self.many.setEnabled(n > 0)
        if n == 0 and self.many.isChecked() and self.record:
            self.one.setChecked(True)

    def _records(self) -> list[labels.LabelData]:
        if self.many.isChecked():
            return [labels.from_dict(d) for d in self.queue.items]
        return [self.record] if self.record else []

    def _update(self, *_):
        queue_mode = self.many.isChecked()
        self.queue_list.setEnabled(queue_mode)
        self.remove_btn.setEnabled(queue_mode)
        self.add_btn.setEnabled(not queue_mode and self.record is not None)
        self.source.setEnabled(not queue_mode)
        self.output.refresh()

    def _add_to_queue(self):
        self.queue.add(labels.to_dict(self.record))
        self._fill_queue()
        self._update()

    def _remove_selected(self):
        self.queue.remove([self.queue_list.row(i) for i in self.queue_list.selectedItems()])
        self._fill_queue()
        self._update()

    def _printed(self, queue_mode: bool):
        if queue_mode and offer_to_clear_queue(self, self.queue):
            self._fill_queue()
            self._update()
