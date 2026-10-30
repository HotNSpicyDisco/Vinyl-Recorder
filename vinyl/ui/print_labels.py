"""Print labels: one record, or the label queue (a sheet full of different records). Pick a template,
see a preview, print to any printer or save a PDF."""

from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtGui import QPixmap
from PySide6.QtPrintSupport import QPrintDialog, QPrinter, QPrinterInfo
from PySide6.QtWidgets import (
    QAbstractItemView, QButtonGroup, QCheckBox, QComboBox, QDialog, QFileDialog, QFormLayout, QHBoxLayout, QLabel,
    QListWidget, QMessageBox, QPushButton, QRadioButton, QSpinBox, QVBoxLayout,
)

from .. import labels
from ..export import safe_filename
from ..library import LabelQueue
from ..settings import Settings

PREVIEW_W = 440  # pixels


class PrintLabelsDialog(QDialog):
    """record: the label for the record you're on (None to open on the queue only);
    measured: whether its lengths/BPMs come from saved tracks (shown as a note)."""

    def __init__(self, settings: Settings, queue: LabelQueue, record: labels.LabelData | None = None,
                 measured: bool = False, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Print labels")
        self.settings, self.queue, self.record = settings, queue, record
        lay = QHBoxLayout(self)
        left = QVBoxLayout()

        # What to print
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
        self.add_btn.setToolTip("Collect records and print a whole sheet of different ones later")
        self.add_btn.clicked.connect(self._add_to_queue)
        row = QHBoxLayout()
        row.addSpacing(22)
        row.addWidget(self.add_btn)
        row.addStretch()
        left.addLayout(row)
        left.addWidget(self.many)
        self.queue_list = QListWidget()
        self.queue_list.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.queue_list.setMaximumHeight(150)
        left.addWidget(self.queue_list)
        row = QHBoxLayout()
        self.remove_btn = QPushButton("Remove selected")
        self.remove_btn.clicked.connect(self._remove_selected)
        row.addWidget(self.remove_btn)
        self.clear_btn = QPushButton("Clear queue")
        self.clear_btn.clicked.connect(self._clear_queue)
        row.addWidget(self.clear_btn)
        row.addStretch()
        left.addLayout(row)

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
        self.qr = QCheckBox("QR code to the record's Discogs page (scan it with a phone)")
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
        self.pdf_btn.clicked.connect(self._save_pdf)
        row.addWidget(self.pdf_btn)
        self.print_btn = QPushButton("Print…")
        self.print_btn.setDefault(True)
        self.print_btn.clicked.connect(self._print)
        row.addWidget(self.print_btn)
        b = QPushButton("Close")
        b.clicked.connect(self.reject)
        row.addWidget(b)
        left.addLayout(row)
        lay.addLayout(left, 1)

        self.preview = QLabel()
        self.preview.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.preview.setMinimumWidth(PREVIEW_W + 20)
        self.preview.setStyleSheet("background: #8b8f96; padding: 10px;")
        lay.addWidget(self.preview)

        for w in (self.template, self.start, self.copies):
            (w.currentIndexChanged if isinstance(w, QComboBox) else w.valueChanged).connect(self._update)
        for b in (self.rotate, self.one, self.many, self.qr):
            b.toggled.connect(self._update)
        self._fill_queue()
        self._update()

    # --- the queue ---

    def _fill_queue(self):
        self.queue_list.clear()
        for d in self.queue.items:
            self.queue_list.addItem(f"{d.get('artist')} – {d.get('title')}")
        n = len(self.queue.items)
        self.many.setText(f"Label queue: {n} record{'s' if n != 1 else ''}")
        self.many.setEnabled(n > 0)
        if n == 0 and self.many.isChecked() and self.record:
            self.one.setChecked(True)

    def _add_to_queue(self):
        self.queue.add(labels.to_dict(self.record))
        self._fill_queue()
        self._update()

    def _remove_selected(self):
        self.queue.remove([self.queue_list.row(i) for i in self.queue_list.selectedItems()])
        self._fill_queue()
        self._update()

    def _clear_queue(self):
        if QMessageBox.question(self, "Clear queue", "Remove every record from the label queue?") == \
                QMessageBox.StandardButton.Yes:
            self.queue.clear()
            self._fill_queue()
            self._update()

    # --- printing ---

    def _records(self) -> list[labels.LabelData]:
        if self.many.isChecked():
            return [labels.from_dict(d) for d in self.queue.items]
        return [self.record] if self.record else []

    def _tmpl(self) -> labels.Template:
        return labels.TEMPLATES_BY_ID[self.template.currentData()]

    def _update(self, *_):
        t = self._tmpl()
        queue_mode = self.many.isChecked()
        for w in (self.queue_list, self.remove_btn, self.clear_btn):
            w.setEnabled(queue_mode)
        self.add_btn.setEnabled(not queue_mode and self.record is not None)
        self.source.setEnabled(not queue_mode)
        self.start.blockSignals(True)
        self.start.setRange(1, t.per_page)
        self.start.blockSignals(False)
        self.start.setEnabled(t.sheet)
        self.rotate.setVisible(not t.sheet)
        records = self._records()
        for b in (self.print_btn, self.pdf_btn):
            b.setEnabled(bool(records))
        if not records:
            self.info.setText("The label queue is empty. Saved albums are added to it, or use "
                              "\"Add this record to the label queue\".")
            self.preview.clear()
            return
        placed, _ = labels.place(t, records, self.copies.value(), self.start.value(), self.qr.isChecked())
        pages = max(p.page for p in placed) + 1
        what = f"{len(records)} records" if queue_mode else f"{len(self.record.tracks)} tracks"
        text = f"{what}: {len(placed)} label{'s' if len(placed) > 1 else ''}"
        if any(not p.first for p in placed):
            text += " (long tracklists carry on onto the next label)"
        text += f", {pages} {'sheet' if t.sheet else 'label'}{'s' if pages > 1 else ''}."
        if t.sheet and pages > 1:
            text += " The preview shows the first sheet."
        self.info.setText(text)
        size = labels.page_size(t)
        rotated = self.rotate.isChecked() and not t.sheet
        width_in = size.height() if rotated else size.width()
        img = labels.preview(t, records, self.start.value(), dpi=int(PREVIEW_W / width_in), rotate=rotated,
                             qr=self.qr.isChecked())
        self.preview.setPixmap(QPixmap.fromImage(img))

    def _remember(self):
        t = self._tmpl()
        self.settings.label_template = t.id
        self.settings.label_qr = self.qr.isChecked()
        if not t.sheet:
            self.settings.label_rotate = self.rotate.isChecked()
        self.settings.save()

    def _after_printing_queue(self):
        if self.many.isChecked() and QMessageBox.question(
                self, "Label queue", "Done with these? Clear them from the label queue?") == \
                QMessageBox.StandardButton.Yes:
            self.queue.clear()
            self._fill_queue()
            self._update()

    def _print(self):
        t = self._tmpl()
        rotate = self.rotate.isChecked() and not t.sheet
        records = self._records()
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
        self._after_printing_queue()

    def _save_pdf(self):
        t = self._tmpl()
        records = self._records()
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
        self._after_printing_queue()
