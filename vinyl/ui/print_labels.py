"""Print labels for a record: pick a template, see a preview, print to any printer or save a PDF."""

from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtGui import QPixmap
from PySide6.QtPrintSupport import QPrintDialog, QPrinter, QPrinterInfo
from PySide6.QtWidgets import (
    QCheckBox, QComboBox, QDialog, QFileDialog, QFormLayout, QHBoxLayout, QLabel, QMessageBox, QPushButton,
    QSpinBox, QVBoxLayout,
)

from .. import labels
from ..discogs import Release
from ..export import safe_filename
from ..settings import Settings

PREVIEW_W = 440  # pixels


class PrintLabelsDialog(QDialog):
    def __init__(self, release: Release, settings: Settings, bpms=None, lengths=None, parent=None):
        super().__init__(parent)
        self.setWindowTitle(f"Print labels · {release.artist} – {release.title}")
        self.settings = settings
        self.data = labels.label_data(release, bpms, lengths)
        measured = bool(bpms or lengths)
        lay = QHBoxLayout(self)

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
        form.addRow("Copies:", self.copies)
        self.rotate = QCheckBox("Turn 90° (if your printer prints the label sideways)")
        self.rotate.setChecked(settings.label_rotate)
        form.addRow("", self.rotate)
        left.addLayout(form)

        self.info = QLabel()
        self.info.setWordWrap(True)
        left.addWidget(self.info)
        source = QLabel("Lengths and BPMs measured from your saved tracks." if measured else
                        "Lengths from Discogs. Save the tracks first to print the measured lengths and BPMs.")
        source.setWordWrap(True)
        source.setStyleSheet("color: gray;")
        left.addWidget(source)
        tips = QLabel("<b>Thermal printers:</b> load 4\" wide labels and choose the matching label size in the "
                      "print window if it asks. <b>Avery sheets:</b> print at 100% / Actual size, not "
                      "\"Fit to page\".")
        tips.setWordWrap(True)
        left.addWidget(tips)
        left.addStretch()
        row = QHBoxLayout()
        b = QPushButton("Save PDF…")
        b.clicked.connect(self._save_pdf)
        row.addWidget(b)
        b = QPushButton("Print…")
        b.setDefault(True)
        b.clicked.connect(self._print)
        row.addWidget(b)
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
        self.rotate.toggled.connect(self._update)
        self._update()

    def _tmpl(self) -> labels.Template:
        return labels.TEMPLATES_BY_ID[self.template.currentData()]

    def _update(self, *_):
        t = self._tmpl()
        self.start.blockSignals(True)
        self.start.setRange(1, t.per_page)
        self.start.blockSignals(False)
        self.start.setEnabled(t.sheet)
        self.rotate.setVisible(not t.sheet)
        placed, _ = labels.place(t, self.data, self.copies.value(), self.start.value())
        per_copy = len(placed) // self.copies.value()
        pages = max(p.page for p in placed) + 1
        text = f"{len(self.data.tracks)} tracks: {per_copy} label{'s' if per_copy > 1 else ''} per copy"
        if per_copy > 1:
            text += " (the tracklist carries on onto the next label)"
        if t.sheet:
            text += f". {pages} sheet{'s' if pages > 1 else ''}."
        self.info.setText(text)
        size = labels.page_size(t)
        rotated = self.rotate.isChecked() and not t.sheet
        width_in = size.height() if rotated else size.width()
        img = labels.preview(t, self.data, self.start.value(), dpi=int(PREVIEW_W / width_in), rotate=rotated)
        self.preview.setPixmap(QPixmap.fromImage(img))

    def _remember(self):
        t = self._tmpl()
        self.settings.label_template = t.id
        if not t.sheet:
            self.settings.label_rotate = self.rotate.isChecked()
        self.settings.save()

    def _print(self):
        t = self._tmpl()
        rotate = self.rotate.isChecked() and not t.sheet
        printer = QPrinter(QPrinter.PrinterMode.HighResolution)
        last = self.settings.label_printers.get(t.id)
        if last and last in QPrinterInfo.availablePrinterNames():
            printer.setPrinterName(last)
        printer.setFullPage(True)  # draw from the paper's corner: the labels are placed exactly
        printer.setPageLayout(labels.page_layout(t, rotate))
        printer.setDocName(f"{self.data.artist} – {self.data.title} labels")
        dlg = QPrintDialog(printer, self)
        dlg.setWindowTitle("Print labels")
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return
        # Picking a printer in the dialog can swap in its default paper: put the label size back
        printer.setPageLayout(labels.page_layout(t, rotate))
        printer.setFullPage(True)
        try:
            labels.print_to(printer, t, self.data, self.copies.value(), self.start.value(), rotate)
        except Exception as e:  # driver problems: say so instead of failing silently
            QMessageBox.warning(self, "Print labels", f"Printing failed: {e}")
            return
        self.settings.label_printers[t.id] = printer.printerName()
        self._remember()
        self.accept()

    def _save_pdf(self):
        t = self._tmpl()
        name = safe_filename(f"{self.data.artist} - {self.data.title} - labels ({t.id}).pdf")
        path, _ = QFileDialog.getSaveFileName(self, "Save labels as PDF", str(Path(self.settings.output_dir) / name),
                                              "PDF (*.pdf)")
        if not path:
            return
        labels.save_pdf(Path(path), t, self.data, self.copies.value(), self.start.value(),
                        self.rotate.isChecked() and not t.sheet)
        self._remember()
        QMessageBox.information(self, "Print labels", f"Saved {Path(path).name}")
