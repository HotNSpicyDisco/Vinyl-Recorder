"""Main window: 1. Album  2. Record  3. Split & save."""

import re
import shutil
import string
import sys
import traceback
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from PySide6.QtCore import QObject, QRunnable, Qt, QThreadPool, QTimer, QUrl, Signal
from PySide6.QtGui import QAction, QColor, QDesktopServices, QIcon, QKeySequence, QPixmap, QShortcut
from PySide6.QtWidgets import (
    QAbstractItemView, QApplication, QCheckBox, QComboBox, QDialog, QDialogButtonBox, QDoubleSpinBox, QFileDialog,
    QFormLayout, QGroupBox, QHBoxLayout, QHeaderView, QLabel, QLineEdit, QListWidget, QListWidgetItem,
    QMainWindow, QMessageBox, QPushButton, QScrollBar, QSpinBox, QSplitter, QTableWidget, QTableWidgetItem, QTabWidget,
    QVBoxLayout, QWidget,
)

import numpy as np

from .. import (HOMEPAGE, __version__, audio, discogs, export, hum, labels, library, naming, rekordbox, shortcuts,
                splitter, tempo, tracklist, updates)
from ..settings import Settings
from .labels_tab import LabelsTab
from .print_labels import PrintLabelsDialog
from .tracklist_editor import TracklistEditor
from .widgets import LevelMeter, WaveformView, fmt_time

SAMPLE_RATES = [44100, 48000, 88200, 96000]
SIDE_LETTERS = list(string.ascii_uppercase[:12])
ALL_SIDES = "ALL"  # a recording of the whole record in one take (flip in the middle)


def side_name(side: str) -> str:
    """'Whole LP', 'Sides C+D' (one disc of a double LP) or 'Side A'."""
    if side == ALL_SIDES:
        return "Whole LP"
    return f"Sides {side}" if "+" in side else f"Side {side}"


def take_sides(side: str, all_sides: list[str]) -> list[str]:
    """The record sides a take covers."""
    if side == ALL_SIDES:
        return list(all_sides)
    return side.split("+")


def fill_side_combo(combo: QComboBox, sides: list[str], whole: bool) -> None:
    """Take options: the whole record, each disc of a multi-disc record (sides A+B, C+D…), single sides."""
    combo.clear()
    if whole:
        label = ("Whole LP (all sides in one take)" if len(sides) <= 2
                 else f"Whole record (all {len(sides)} sides in one take)")
        combo.addItem(label, ALL_SIDES)
    if len(sides) > 2:
        for n, i in enumerate(range(0, len(sides) - 1, 2), 1):
            pair = f"{sides[i]}+{sides[i + 1]}"
            combo.addItem(f"Disc {n}: sides {pair} in one take", pair)
    for s in sides:
        combo.addItem(f"Side {s}", s)
TAB_ALBUM, TAB_RECORD, TAB_SPLIT, TAB_LABELS = range(4)


class _Signals(QObject):
    done = Signal(object)
    failed = Signal(str)


class _Task(QRunnable):
    def __init__(self, fn):
        super().__init__()
        self.fn = fn
        self.signals = _Signals()

    def run(self):
        try:
            result = self.fn()
        except Exception as e:  # reported back to the UI thread
            traceback.print_exc()
            self.signals.failed.emit(str(e) or e.__class__.__name__)
        else:
            self.signals.done.emit(result)


@dataclass
class Recording:
    path: Path
    side: str
    env: audio.Envelope | None = None
    edges: list[float] = field(default_factory=list)  # music start, splits..., music end
    clips: list[float] = field(default_factory=list)  # seconds into the recording
    clip_count: int | None = None  # None for files opened from disk (not counted)
    release_id: int | None = None  # album chosen when this side was recorded
    skips: set[int] = field(default_factory=set)  # parts between edges that aren't tracks (record flips)
    side_order: list[str] | None = None  # order the sides were played in, if not A, B, ... (e.g. B first)
    overrides: dict[float, int] = field(default_factory=dict)  # tracks picked by hand (see vinyl.tracklist)
    side_breaks: list[float] = field(default_factory=list)  # where sides ended, from a hands-free take

    @property
    def label(self) -> str:
        dur = fmt_time(self.env.duration) if self.env else "analysing…"
        clips = f"  ·  ⚠ {self.clip_count} clips" if self.clip_count else ""
        return f"{side_name(self.side)}  ·  {dur}{clips}  ·  {self.path.name}"


class SettingsDialog(QDialog):
    def __init__(self, settings: Settings, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Settings")
        self.setMinimumWidth(520)
        self.settings = settings
        form = QFormLayout(self)

        self.token = QLineEdit(settings.discogs_token)
        self.token.setEchoMode(QLineEdit.EchoMode.Password)
        show = QCheckBox("Show")
        show.toggled.connect(lambda on: self.token.setEchoMode(
            QLineEdit.EchoMode.Normal if on else QLineEdit.EchoMode.Password))
        row = QHBoxLayout()
        row.addWidget(self.token)
        row.addWidget(show)
        form.addRow("Discogs token:", row)
        link = QLabel('<a href="https://www.discogs.com/settings/developers">Get a free personal access token '
                      "on Discogs</a> (Settings → Developers → Generate new token).")
        link.setOpenExternalLinks(True)
        link.setWordWrap(True)
        form.addRow("", link)

        self.output = QLineEdit(settings.output_dir)
        browse = QPushButton("Browse…")
        browse.clicked.connect(self._browse)
        row = QHBoxLayout()
        row.addWidget(self.output)
        row.addWidget(browse)
        form.addRow("Save music to:", row)

        self.name_preset = QComboBox()
        for name, pattern in naming.PRESETS:
            self.name_preset.addItem(name, pattern)
        self.name_preset.addItem("Custom…", None)
        self.pattern = QLineEdit(settings.name_pattern)
        self.pattern.setToolTip("Fields: " + ", ".join(f"{{{k}}} = {v}" for k, v in naming.FIELDS.items())
                                + '. "/" makes a folder.')
        self.name_example = QLabel()
        self.name_example.setStyleSheet("color: gray;")
        i = self.name_preset.findData(settings.name_pattern)
        self.name_preset.setCurrentIndex(i if i >= 0 else self.name_preset.count() - 1)
        self.name_preset.currentIndexChanged.connect(self._preset_chosen)
        self.pattern.textChanged.connect(self._pattern_changed)
        form.addRow("File names:", self.name_preset)
        form.addRow("", self.pattern)
        fields = QLabel("Fields: " + "  ".join(f"{{{k}}}" for k in naming.FIELDS))
        fields.setWordWrap(True)
        fields.setStyleSheet("color: gray;")
        form.addRow("", fields)
        form.addRow("", self.name_example)
        self._pattern_changed()

        self.min_gap = QDoubleSpinBox()
        self.min_gap.setRange(0.2, 5.0)
        self.min_gap.setSingleStep(0.1)
        self.min_gap.setSuffix(" s")
        self.min_gap.setValue(settings.min_gap_seconds)
        form.addRow("Shortest gap between tracks:", self.min_gap)

        self.detect_bpm = QCheckBox("Detect BPM when saving")
        self.detect_bpm.setChecked(settings.detect_bpm)
        form.addRow("", self.detect_bpm)
        self.bpm_range = QComboBox()
        for low in tempo.BPM_RANGES:
            self.bpm_range.addItem(f"{low:g} – {2 * low - 1:g} BPM", low)
        self.bpm_range.setCurrentIndex(max(0, self.bpm_range.findData(settings.bpm_low)))
        self.bpm_range.setToolTip("Tempos are reported inside this range, as in Rekordbox. 78–155 suits "
                                  "disco, funk and house; use 88–175 for faster music like drum & bass.")
        form.addRow("BPM range:", self.bpm_range)

        self.queue_labels = QCheckBox("Add each saved album to the label queue")
        self.queue_labels.setToolTip("Print them all at once with File → Print label queue…")
        self.queue_labels.setChecked(settings.queue_labels)
        form.addRow("Labels:", self.queue_labels)

        self.rekordbox = QCheckBox("Add saved albums to rekordbox.xml (in the save folder)")
        self.rekordbox.setChecked(settings.rekordbox)
        form.addRow("Rekordbox:", self.rekordbox)
        self.rekordbox_prefer = QComboBox()
        self.rekordbox_prefer.addItem("FLAC", "flac")
        self.rekordbox_prefer.addItem("MP3 (for older CDJs that can't play FLAC)", "mp3")
        self.rekordbox_prefer.setCurrentIndex(max(0, self.rekordbox_prefer.findData(settings.rekordbox_prefer)))
        form.addRow("When saving both, use:", self.rekordbox_prefer)
        how = QLabel("In Rekordbox: Preferences → Advanced → Database → rekordbox xml → Imported Library, choose "
                     "the file once. Your albums then appear under <i>rekordbox xml</i> in the sidebar.")
        how.setWordWrap(True)
        how.setStyleSheet("color: gray;")
        form.addRow("", how)

        self.check_updates = QCheckBox("Check GitHub for a new version once a day")
        self.check_updates.setChecked(settings.check_updates)
        form.addRow("Updates:", self.check_updates)

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        form.addRow(buttons)

    def _browse(self):
        d = QFileDialog.getExistingDirectory(self, "Save music to", self.output.text())
        if d:
            self.output.setText(d)

    def _preset_chosen(self):
        pattern = self.name_preset.currentData()
        if pattern:
            self.pattern.setText(pattern)
        else:
            self.pattern.setFocus()

    def _pattern_changed(self):
        pattern = self.pattern.text().strip()
        problem = naming.check(pattern)
        self.name_example.setText(f"<span style='color:#f85149'>{problem}</span>" if problem
                                  else f"e.g. {naming.example(pattern)}")
        i = self.name_preset.findData(pattern)
        self.name_preset.blockSignals(True)
        self.name_preset.setCurrentIndex(i if i >= 0 else self.name_preset.count() - 1)
        self.name_preset.blockSignals(False)

    def accept(self):
        problem = naming.check(self.pattern.text().strip())
        if problem:
            QMessageBox.warning(self, "File names", problem)
            return
        self.settings.name_pattern = self.pattern.text().strip()
        if self.token.text().strip() != self.settings.discogs_token:
            self.settings.discogs_username = ""  # may be a different account now
        self.settings.discogs_token = self.token.text().strip()
        self.settings.output_dir = self.output.text().strip() or self.settings.output_dir
        self.settings.min_gap_seconds = self.min_gap.value()
        self.settings.detect_bpm = self.detect_bpm.isChecked()
        self.settings.bpm_low = self.bpm_range.currentData()
        self.settings.queue_labels = self.queue_labels.isChecked()
        self.settings.rekordbox = self.rekordbox.isChecked()
        self.settings.rekordbox_prefer = self.rekordbox_prefer.currentData()
        self.settings.check_updates = self.check_updates.isChecked()
        self.settings.save()
        super().accept()


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Vinyl Recorder")
        self.resize(1200, 780)
        self.settings = Settings.load()
        self.recorder = audio.Recorder()
        self.player = audio.Player()
        self.pool = QThreadPool.globalInstance()
        self._tasks: set[_Task] = set()
        self.devices: list[audio.InputDevice] = []
        self.recordings: list[Recording] = []
        self.release: discogs.Release | None = None
        self.preview: discogs.Release | None = None
        self.current: Recording | None = None
        self.bpms: dict[int, float | None] = {}  # track number -> BPM found when saving
        self._saving: dict[int, discogs.Release] = {}  # albums being saved in the background, by release id
        self.lengths: dict[int, float] = {}  # track number -> length as saved
        self.riplog = library.RipLog()
        self.collection = library.CollectionCache()
        self.edits = library.ReleaseEdits()
        self.label_queue = library.LabelQueue()
        self._release_cache: dict[int, discogs.Release] = {}
        self._preview_wanted: int | None = None

        self.tabs = QTabWidget()
        self.tabs.addTab(self._build_find_tab(), "1. Album")
        self.tabs.addTab(self._build_record_tab(), "2. Record")
        self.tabs.addTab(self._build_split_tab(), "3. Split && save")
        self.labels_tab = LabelsTab(self)
        self.tabs.addTab(self.labels_tab, "4. Labels")
        self.tabs.currentChanged.connect(lambda i: i == TAB_LABELS and self.labels_tab.refresh())
        self.setCentralWidget(self.tabs)
        self._build_menu()

        self.timer = QTimer(self)
        self.timer.timeout.connect(self._tick)
        self.timer.start(33)

        self._scanning = False
        self._scan_devices(rescan=False)  # connects to the saved input when the scan finishes
        if (shortcuts.SUPPORTED and shortcuts.FROZEN and not self.settings.offered_shortcuts
                and not shortcuts.start_menu_shortcut_exists()):
            QTimer.singleShot(900, self._offer_shortcuts)
        if self.settings.check_updates and updates.due(self.settings.last_update_check):
            QTimer.singleShot(3000, lambda: self._check_updates(manual=False))

    # ---------- helpers ----------

    def run_bg(self, fn, on_done, on_fail=None, pool: QThreadPool | None = None):
        task = _Task(fn)
        self._tasks.add(task)
        task.signals.done.connect(lambda r: (self._tasks.discard(task), on_done(r)))
        task.signals.failed.connect(lambda msg: (self._tasks.discard(task), (on_fail or self._error)(msg)))
        (pool or self.pool).start(task)

    def _error(self, msg: str):
        self.statusBar().showMessage(msg, 10000)
        QMessageBox.warning(self, "Vinyl Recorder", msg)

    def _status(self, msg: str, ms: int = 6000):
        self.statusBar().showMessage(msg, ms)

    def _build_menu(self):
        m = self.menuBar().addMenu("&File")
        act = QAction("Open recording (WAV/FLAC)…", self)
        act.triggered.connect(self._open_recording)
        m.addAction(act)
        act = QAction("Print label queue…", self)
        act.triggered.connect(lambda: self.tabs.setCurrentIndex(TAB_LABELS))
        m.addAction(act)
        act = QAction("Add all saved albums to Rekordbox", self)
        act.triggered.connect(self._rekordbox_all)
        m.addAction(act)
        act = QAction("Open output folder", self)
        act.triggered.connect(lambda: QDesktopServices.openUrl(QUrl.fromLocalFile(self.settings.output_dir)))
        m.addAction(act)
        act = QAction("Settings…", self)
        act.triggered.connect(self._open_settings)
        m.addAction(act)
        if shortcuts.SUPPORTED:
            act = QAction("Add to Start menu and desktop", self)
            act.triggered.connect(lambda: self._add_shortcuts(quiet=False))
            m.addAction(act)
        m.addSeparator()
        act = QAction("Quit", self)
        act.triggered.connect(self.close)
        m.addAction(act)

        m = self.menuBar().addMenu("&Help")
        act = QAction("Check for updates…", self)
        act.triggered.connect(lambda: self._check_updates(manual=True))
        m.addAction(act)
        act = QAction("Vinyl Recorder on GitHub", self)
        act.triggered.connect(lambda: QDesktopServices.openUrl(QUrl(HOMEPAGE)))
        m.addAction(act)
        act = QAction("About Vinyl Recorder", self)
        act.triggered.connect(lambda: QMessageBox.about(
            self, "About Vinyl Recorder",
            f"<b>Vinyl Recorder {__version__}</b><br><br>Record your records, look them up on Discogs, split "
            f"them into tracks and print labels.<br><br><a href='{HOMEPAGE}'>{HOMEPAGE}</a>"))
        m.addAction(act)

    # ---------- updates ----------

    def _check_updates(self, manual: bool):
        """Ask GitHub for the latest release. Automatic checks stay quiet unless there is something new."""
        def done(update):
            self.settings.last_update_check = datetime.now().isoformat(timespec="seconds")
            self.settings.save()
            if not update:
                if manual:
                    QMessageBox.information(self, "Vinyl Recorder", f"You have the latest version ({__version__}).")
                return
            if not manual and update.version == self.settings.skipped_version:
                return
            if self.recorder.is_recording:  # don't interrupt a take with a dialog
                self._status(f"Vinyl Recorder {update.version} is out: Help → Check for updates", 30000)
                return
            self._offer_update(update, manual)

        def failed(msg):
            if manual:
                QMessageBox.warning(self, "Vinyl Recorder", f"Couldn't reach GitHub to check for updates.\n\n{msg}")

        self.run_bg(updates.check, done, failed)

    def _offer_update(self, update: updates.Update, manual: bool):
        notes = update.notes if len(update.notes) <= 1200 else update.notes[:1200].rsplit("\n", 1)[0] + "\n…"
        if shortcuts.FROZEN:
            how = ("Download the zip, close Vinyl Recorder, and unzip it over your old Vinyl Recorder folder "
                   "(on a Mac: drag the new app into Applications and choose Replace). "
                   "Your settings, collection and label queue are kept.")
        else:
            how = "You're running from source: run <code>git pull</code> in the app's folder to update."
        box = QMessageBox(self)
        box.setWindowTitle("Update available")
        box.setIcon(QMessageBox.Icon.Information)
        box.setTextFormat(Qt.TextFormat.RichText)
        box.setText(f"<b>Vinyl Recorder {update.version}</b> is out. You have {__version__}.<br><br>{how}")
        if notes:
            box.setDetailedText(notes)
        download = box.addButton("Download" if shortcuts.FROZEN else "Open release page",
                                 QMessageBox.ButtonRole.AcceptRole)
        skip = None if manual else box.addButton("Skip this version", QMessageBox.ButtonRole.RejectRole)
        box.addButton("Later", QMessageBox.ButtonRole.RejectRole)
        box.exec()
        if box.clickedButton() is download:
            QDesktopServices.openUrl(QUrl(update.download if shortcuts.FROZEN else update.page))
        elif skip is not None and box.clickedButton() is skip:
            self.settings.skipped_version = update.version
            self.settings.save()

    def _offer_shortcuts(self):
        self.settings.offered_shortcuts = True
        self.settings.save()
        ans = QMessageBox.question(
            self, "Vinyl Recorder",
            "Add Vinyl Recorder to the Start menu and the desktop?\n\n"
            "(Keep this folder where it is afterwards: the shortcuts point to it. "
            "You can do this later from the File menu.)")
        if ans == QMessageBox.StandardButton.Yes:
            self._add_shortcuts(quiet=True)

    def _add_shortcuts(self, quiet: bool):
        try:
            made = shortcuts.install()
        except Exception as e:
            self._error(f"Couldn't create the shortcuts: {e}")
            return
        self._status("Added Vinyl Recorder to the Start menu and desktop")
        if not quiet:
            QMessageBox.information(self, "Vinyl Recorder", "Shortcuts added:\n\n" + "\n".join(map(str, made)))

    def _open_settings(self):
        had_user = self.settings.discogs_username
        if SettingsDialog(self.settings, self).exec():
            self.out_label.setText(self.settings.output_dir)
            self._status("Settings saved")
            if self.settings.token() and (not self.collection.items or had_user != self.settings.discogs_username):
                self._refresh_collection()

    # ---------- 2. Record ----------

    def _build_record_tab(self) -> QWidget:
        w = QWidget()
        lay = QVBoxLayout(w)

        box = QGroupBox("Album")
        h = QHBoxLayout(box)
        self.rec_cover = QLabel()
        self.rec_cover.setFixedSize(56, 56)
        self.rec_cover.setStyleSheet("background: #222;")
        h.addWidget(self.rec_cover)
        self.rec_album = QLabel()
        self.rec_album.setWordWrap(True)
        h.addWidget(self.rec_album, 1)
        b = QPushButton("Choose album…")
        b.clicked.connect(lambda: self.tabs.setCurrentIndex(TAB_ALBUM))
        h.addWidget(b)
        lay.addWidget(box)

        box = QGroupBox("Input")
        form = QFormLayout(box)
        self.device_combo = QComboBox()
        self.device_combo.currentIndexChanged.connect(self._device_changed)
        self.refresh_btn = QPushButton("Refresh")
        self.refresh_btn.setToolTip("Look for audio devices again (e.g. after plugging one in)")
        self.refresh_btn.clicked.connect(lambda: self._scan_devices(rescan=True))
        row = QHBoxLayout()
        row.addWidget(self.device_combo, 1)
        row.addWidget(self.refresh_btn)
        form.addRow("Device:", row)

        self.left_spin, self.right_spin = QSpinBox(), QSpinBox()
        for s, v in ((self.left_spin, self.settings.left_input), (self.right_spin, self.settings.right_input)):
            s.setRange(1, 64)
            s.setValue(v)
        self.rate_combo = QComboBox()
        for r in SAMPLE_RATES:
            self.rate_combo.addItem(f"{r / 1000:g} kHz", r)
        self.rate_combo.setCurrentIndex(max(0, SAMPLE_RATES.index(self.settings.sample_rate)
                                            if self.settings.sample_rate in SAMPLE_RATES else 3))
        self.apply_btn = QPushButton("Apply")
        self.apply_btn.clicked.connect(self._connect_input)
        row = QHBoxLayout()
        row.addWidget(QLabel("Left input"))
        row.addWidget(self.left_spin)
        row.addSpacing(12)
        row.addWidget(QLabel("Right input"))
        row.addWidget(self.right_spin)
        row.addSpacing(12)
        row.addWidget(QLabel("Sample rate"))
        row.addWidget(self.rate_combo)
        row.addStretch()
        row.addWidget(self.apply_btn)
        form.addRow("Channels:", row)
        self.input_state = QLabel("Not connected")
        form.addRow("", self.input_state)
        lay.addWidget(box)

        box = QGroupBox("Levels")
        v = QVBoxLayout(box)
        self.meter = LevelMeter()
        v.addWidget(self.meter)
        row = QHBoxLayout()
        self.level_hint = QLabel("Play the loudest part of the record. Set the mixer so peaks reach about -6 dB "
                                 "and the clip counter stays at 0.")
        self.level_hint.setWordWrap(True)
        row.addWidget(self.level_hint, 1)
        self.clip_label = QLabel()
        self.clip_label.setMinimumWidth(230)
        self.clip_label.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        row.addWidget(self.clip_label)
        b = QPushButton("Reset clips")
        b.clicked.connect(self._reset_clips)
        row.addWidget(b)
        v.addLayout(row)
        self.hum_label = QLabel()
        self.hum_label.setWordWrap(True)
        self.hum_label.setStyleSheet("color: #d29922; font-weight: bold;")
        self.hum_label.hide()
        v.addWidget(self.hum_label)
        lay.addWidget(box)
        self._hum_hits, self._hum_clear, self._hum_tick = 0, 0, 0

        box = QGroupBox("Record")
        h = QHBoxLayout()
        h.addWidget(QLabel("Recording:"))
        self.side_combo = QComboBox()
        self.side_combo.setMinimumWidth(230)
        h.addWidget(self.side_combo)
        self.rec_btn = QPushButton("●  Record")
        self.rec_btn.setMinimumHeight(44)
        self.rec_btn.setMinimumWidth(160)
        self.rec_btn.setStyleSheet("QPushButton { font-size: 16px; font-weight: bold; color: #f85149; }")
        self.rec_btn.clicked.connect(self._toggle_record)
        h.addWidget(self.rec_btn)
        self.rec_time = QLabel("0:00.0")
        self.rec_time.setStyleSheet("font-size: 24px; font-family: Consolas, Menlo, monospace;")
        h.addWidget(self.rec_time)
        self.rec_state = QLabel()
        self.rec_state.setStyleSheet("font-size: 14px; font-weight: bold;")
        h.addWidget(self.rec_state, 1)
        rv = QVBoxLayout()
        rv.addLayout(h)
        self.hands_free = QCheckBox("Hands-free: start when the needle drops, pause while you flip, "
                                    "stop after the last side")
        self.hands_free.setChecked(self.settings.hands_free)
        self.hands_free.toggled.connect(self._hands_free_toggled)
        rv.addWidget(self.hands_free)
        box.setLayout(rv)
        lay.addWidget(box)

        box = QGroupBox("Recorded sides")
        v = QVBoxLayout(box)
        self.rec_list = QListWidget()
        self.rec_list.itemDoubleClicked.connect(lambda _: self.tabs.setCurrentIndex(TAB_SPLIT))
        v.addWidget(self.rec_list)
        row = QHBoxLayout()
        b = QPushButton("Open existing file…")
        b.clicked.connect(self._open_recording)
        row.addWidget(b)
        b = QPushButton("Remove from list")
        b.clicked.connect(self._remove_recording)
        row.addWidget(b)
        row.addStretch()
        b = QPushButton("Next: split && save →")
        b.clicked.connect(lambda: self.tabs.setCurrentIndex(TAB_SPLIT))
        row.addWidget(b)
        v.addLayout(row)
        lay.addWidget(box, 1)
        self._update_record_album()
        self._update_clip_label()
        return w

    def _update_record_album(self):
        rel = self.release
        if rel is None:
            self.rec_album.setText("<b>No album chosen.</b> Pick it on the Album tab first so the recordings "
                                   "are named after it.")
            self.rec_cover.clear()
            sides = SIDE_LETTERS
        else:
            self.rec_album.setText(f"<b>{rel.artist} – {rel.title}</b> ({rel.year})<br>"
                                   f"Sides {', '.join(rel.sides)} · {len(rel.tracks)} tracks")
            if rel.cover:
                pm = QPixmap()
                pm.loadFromData(rel.cover)
                self.rec_cover.setPixmap(pm.scaled(56, 56, Qt.AspectRatioMode.KeepAspectRatio,
                                                   Qt.TransformationMode.SmoothTransformation))
            sides = rel.sides
        whole = rel is None or len(sides) > 1
        fill_side_combo(self.side_combo, sides, whole)
        # Whole LP by default; once some sides exist, offer the next one that hasn't been recorded.
        done = {r.side for r in self.recordings}
        if done and ALL_SIDES not in done:
            nxt = next((i for i in range(self.side_combo.count())
                        if self.side_combo.itemData(i) not in done and self.side_combo.itemData(i) != ALL_SIDES), 0)
            self.side_combo.setCurrentIndex(nxt)

    def _reset_clips(self):
        self.recorder.reset_clips()
        self.meter.reset()
        self._update_clip_label()

    def _update_clip_label(self):
        left, right = self.recorder.clip_counts
        total = left + right
        color = "#f85149" if total else "#3fb950"
        self.clip_label.setText(f"<span style='font-size:15px; color:{color}'><b>Clips: {total}</b></span>"
                                f"  (L {left} · R {right})")

    def _scan_devices(self, rescan: bool = True):
        """Find audio devices in the background. Windows can take 10+ seconds to answer when a PC has
        many audio devices, and the rest of the app doesn't need to wait for that."""
        if self._scanning or self.recorder.is_recording:
            return
        self._scanning = True
        if rescan:  # PortAudio only sees devices that existed when it started, so restart it
            self.player.stop()
            self.recorder.close()
        self.devices = []
        self.device_combo.blockSignals(True)
        self.device_combo.clear()
        self.device_combo.addItem("Looking for audio devices…")
        self.device_combo.blockSignals(False)
        for wdg in (self.device_combo, self.refresh_btn, self.apply_btn, self.rec_btn):
            wdg.setEnabled(False)
        self.input_state.setText("Looking for audio devices… (Windows can take a few seconds to answer)")

        def failed(msg):
            self._scanning = False
            for wdg in (self.device_combo, self.refresh_btn, self.apply_btn, self.rec_btn):
                wdg.setEnabled(True)
            self.input_state.setText(f"<span style='color:#f85149'>Couldn't list audio devices: {msg}</span>")

        self.run_bg(audio.rescan_input_devices if rescan else audio.list_input_devices, self._devices_found, failed)

    def _devices_found(self, devices: list[audio.InputDevice]):
        self._scanning = False
        self.devices = devices
        self.device_combo.blockSignals(True)
        self.device_combo.clear()
        for d in self.devices:
            self.device_combo.addItem(d.label)
        pick = next((i for i, d in enumerate(self.devices) if d.name == self.settings.device_name), 0)
        self.device_combo.setCurrentIndex(pick)
        self.device_combo.blockSignals(False)
        for wdg in (self.device_combo, self.refresh_btn, self.apply_btn, self.rec_btn):
            wdg.setEnabled(True)
        self._device_changed()
        self._connect_input()

    def _device_changed(self):
        d = self._device()
        if d:
            if max(self.left_spin.value(), self.right_spin.value()) > d.channels:
                # e.g. a 2-input interface with the 3 & 4 default: use its first pair instead
                self.left_spin.setValue(1)
                self.right_spin.setValue(min(2, d.channels))
            self.left_spin.setMaximum(d.channels)
            self.right_spin.setMaximum(d.channels)

    def _device(self) -> audio.InputDevice | None:
        i = self.device_combo.currentIndex()
        return self.devices[i] if 0 <= i < len(self.devices) else None

    def _connect_input(self):
        if self.recorder.is_recording or self._scanning:
            return
        d = self._device()
        if not d:
            self.input_state.setText("No input devices found")
            return
        left, right = self.left_spin.value(), self.right_spin.value()
        wanted = self.rate_combo.currentData()
        # Not every interface does 96 kHz: fall back to the common rates rather than failing.
        error = None
        for rate in dict.fromkeys([wanted, 48000, 44100]):
            try:
                self.recorder.open(d, left - 1, right - 1, rate)
                break
            except Exception as e:
                error = error or e
        else:
            self.input_state.setText(f"<span style='color:#f85149'>Could not open {d.name}: {error}</span><br>"
                                     "If another app (DJ software, a DAW, another recorder) is using the "
                                     "interface, close it and press Apply.")
            return
        note = ""
        if rate != wanted:
            self.rate_combo.setCurrentIndex(self.rate_combo.findData(rate))
            note = f" ({d.name} can't record at {wanted / 1000:g} kHz)"
        self.settings.device_name, self.settings.left_input, self.settings.right_input = d.name, left, right
        self.settings.sample_rate = rate
        self.settings.save()
        self.meter.reset()
        self.input_state.setText(f"Listening to inputs {left} & {right} on {d.name} at {rate / 1000:g} kHz{note}")

    def _hands_free_toggled(self, on: bool):
        self.settings.hands_free = on
        self.settings.save()

    def _toggle_record(self):
        if self.recorder.is_recording:
            written = self.recorder._rec_frames
            path = self.recorder.stop()
            self.rec_btn.setText("●  Record")
            self.rec_state.setText("")
            self.device_combo.setEnabled(True)
            self.hands_free.setEnabled(True)
            if path and not written:  # hands-free take stopped before the needle dropped
                path.unlink(missing_ok=True)
                self._status("Stopped before anything was recorded.")
                return
            if path:
                rec = self._add_recording(path, self.side_combo.currentData())
                rec.clips = list(self.recorder.clip_times)
                rec.clip_count = sum(self.recorder.clip_counts)
                rec.side_breaks = list(self.recorder.side_breaks)
                self._refresh_rec_labels(rec)
                if self.release:
                    rec.release_id = self.release.id
                    for s in self._sides_of(rec):
                        self.riplog.mark_recorded(self.release, s)
                    self._refresh_marks()
                if rec.clip_count:
                    self._status(f"{side_name(rec.side)} clipped {rec.clip_count} times. The clips are marked in "
                                 "red on the Split tab. Turn the mixer down and re-record if they're audible.", 0)
                if rec.side != ALL_SIDES:  # move on to the next side / disc
                    nxt = self.side_combo.currentIndex() + 1
                    if nxt < self.side_combo.count() and ("+" in rec.side) == ("+" in (
                            self.side_combo.itemData(nxt) or "")):
                        self.side_combo.setCurrentIndex(nxt)
            return
        if not self.recorder.is_open:
            self._connect_input()
            if not self.recorder.is_open:
                return
        if self.release is None:
            ans = QMessageBox.question(
                self, "No album chosen",
                "You haven't picked the album yet, so the file will get a date-based name.\n\nRecord anyway?")
            if ans != QMessageBox.StandardButton.Yes:
                self.tabs.setCurrentIndex(TAB_ALBUM)
                return
        side = self.side_combo.currentData()
        path = self._recording_path(side)
        # A whole LP at 96 kHz / 24-bit is ~1.5 GB: make sure it fits before starting
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            free_gb = shutil.disk_usage(path.parent).free / 1e9
        except OSError:
            free_gb = None
        need_gb = self.recorder.samplerate * 2 * 3 * 60 * 60 / 1e9  # an hour of 24-bit stereo
        if free_gb is not None and free_gb < need_gb:
            ans = QMessageBox.question(
                self, "Low disk space",
                f"Only {free_gb:.1f} GB free where recordings are saved ({path.parent}).\n"
                f"An hour of recording at this quality needs about {need_gb:.1f} GB.\n\nRecord anyway?")
            if ans != QMessageBox.StandardButton.Yes:
                return
        hands_free = self.hands_free.isChecked()
        # Hands-free stops by itself after the take's last side (if the album says how many)
        n_sides = len(take_sides(side, self.release.sides)) if self.release else None
        try:
            self.recorder.start(path, hands_free=hands_free, sides=n_sides)
        except Exception as e:
            self._error(f"Could not start recording: {e}")
            return
        self.meter.reset()
        self._update_clip_label()
        self.rec_btn.setText("■  Stop")
        self.device_combo.setEnabled(False)
        self.hands_free.setEnabled(False)
        self._side_names = take_sides(side, self.release.sides) if self.release else []
        if hands_free:
            self._status(f"Recording {side_name(side).lower()} to {path.name}. Drop the needle when you're ready.", 0)
        else:
            tip = "  Flip the record when side A ends and keep recording." if side == ALL_SIDES else ""
            self._status(f"Recording {side_name(side).lower()} to {path.name}.{tip}", 0)
        self._last_state = None

    def _hands_free_status(self):
        """Show what a hands-free take is doing, and finish it when the last side is done."""
        r = self.recorder
        if not r.hands_free:
            self.rec_state.setText("● Recording")
            return
        n = r.sides_done
        names = self._side_names
        side = lambda i: f"side {names[i]}" if i < len(names) else f"side {i + 1}"  # noqa: E731
        text, color = {
            audio.ARMED: ("Waiting for the needle…", "#d29922"),
            audio.RECORDING: (f"● Recording {side(n)}", "#f85149"),
            audio.PAUSED: (f"S{side(n - 1)[1:]} done. Flip the record: waiting for the needle…", "#d29922"),
            audio.FINISHED: ("All sides recorded", "#3fb950"),
        }[r.state]
        if r.state == audio.RECORDING and r.ending:
            text = f"● Recording {side(n)}: quiet. At the end? Lift the needle to finish the side"
        self.rec_state.setText(f"<span style='color:{color}'>{text}</span>")
        if r.state != self._last_state:
            if r.state == audio.PAUSED:
                QApplication.beep()  # time to flip
            self._last_state = r.state
        if r.event == "finished":
            r.event = None
            self._toggle_record()  # stop and add the take, as if Stop was pressed
            self._status("All sides recorded. The tracks are ready on the Split & save tab.", 0)

    def _sides_of(self, rec: Recording) -> list[str]:
        """The sides a take covers, in the order they were played."""
        sides = take_sides(rec.side, self.release.sides if self.release else [])
        if rec.side_order and sorted(rec.side_order) == sorted(sides):
            return list(rec.side_order)
        return sides

    def _recording_path(self, side: str) -> Path:
        """'Artist - Album (Year) - Side A.wav' or '… - Whole LP.wav', numbered if it exists already."""
        folder = self.settings.recordings_dir
        if self.release is None:
            stem = f"{datetime.now():%Y-%m-%d %H-%M-%S} {side_name(side)}"
        else:
            stem = export.safe_filename(
                f"{self.release.artist} - {export.album_name(self.release)} - {side_name(side)}")
        path, n = folder / f"{stem}.wav", 2
        while path.exists():
            path, n = folder / f"{stem} ({n}).wav", n + 1
        return path

    def _refresh_rec_labels(self, rec: Recording):
        i = self.recordings.index(rec)
        self.rec_list.item(i).setText(rec.label)
        self.split_rec_combo.setItemText(i, rec.label)
        if rec is self.current:
            self.wave.clips = rec.clips
            self.wave.update()

    def _check_hum_live(self):
        """Every few seconds while the input is quiet (needle up, between tracks): is there mains hum?
        Two checks in a row must agree before the warning appears, and two clean ones before it goes."""
        x = self.recorder.recent_audio()
        if len(x) < 2 * self.recorder.samplerate or np.sqrt(np.mean(x ** 2)) > 10 ** (-45 / 20):
            return  # music playing: it would hide hum (or look like it)
        found = hum.detect(x, self.recorder.samplerate)
        if found:
            self._hum_hits, self._hum_clear = self._hum_hits + 1, 0
            if self._hum_hits >= 2:
                self.hum_label.setText("⚠ " + found.advice)
                self.hum_label.show()
        else:
            self._hum_hits, self._hum_clear = 0, self._hum_clear + 1
            if self._hum_clear >= 2:
                self.hum_label.hide()

    def _tick(self):
        if self.recorder.is_open:
            self.meter.update_peaks(self.recorder.read_peaks())
            self._update_clip_label()
            self._hum_tick += 1
            if self._hum_tick % 90 == 0:  # about every 3 seconds
                self._check_hum_live()
        if self.recorder.is_recording:
            self.rec_time.setText(fmt_time(self.recorder.seconds))
            self._hands_free_status()
            if self.recorder.overflows:
                self._status(f"Warning: {self.recorder.overflows} audio dropouts. Close other heavy programs.", 0)
        if self.player.is_playing:
            self.wave.set_playhead(self.player.position)
        elif self.wave.playhead is not None:
            self.wave.set_playhead(None)

    def _open_recording(self):
        files, _ = QFileDialog.getOpenFileNames(self, "Open recordings", str(self.settings.recordings_dir),
                                                "Audio (*.wav *.flac *.aif *.aiff)")
        for f in files:
            stem = Path(f).stem
            m = (re.search(r"sides ([A-Z]{1,2}(?:\+[A-Z]{1,2})+)\b", stem, re.IGNORECASE)
                 or re.search(r"side ([A-Z]{1,2})\b", stem, re.IGNORECASE))
            # Anything not named after a side or disc is treated as a whole-LP take.
            self._add_recording(Path(f), m.group(1).upper() if m else ALL_SIDES)

    def _add_recording(self, path: Path, side: str) -> Recording:
        rec = Recording(path, side)
        self.recordings.append(rec)
        item = QListWidgetItem(rec.label)
        self.rec_list.addItem(item)
        self._refresh_split_choices()
        self._status(f"Analysing {path.name}…")

        def done(env):
            if rec not in self.recordings:
                return
            rec.env = env
            self._refresh_split_choices()
            if self.current is rec:
                self._show_recording(rec)
            self._refresh_rec_labels(rec)
            if not rec.clip_count:
                self._status(f"{path.name}: {fmt_time(env.duration)}")
            # Mains hum shows up in the quiet gaps between tracks
            self.run_bg(lambda: hum.check_recording(path, env, splitter._levels(env)[2]), hum_checked,
                        lambda _: None)

        def hum_checked(found):
            if found and rec in self.recordings:
                self.hum_label.setText(f"⚠ {rec.path.name}: {found.advice}")
                self.hum_label.show()
                self._status(f"{side_name(rec.side)}: {found.advice}", 0)

        self.run_bg(lambda: audio.compute_envelope(path), done)
        return rec

    def _remove_recording(self):
        row = self.rec_list.currentRow()
        if row < 0:
            return
        rec = self.recordings.pop(row)
        self.rec_list.takeItem(row)
        if self.current is rec:
            self.current = None
        self._refresh_split_choices()

    # ---------- 1. Album ----------

    def _build_find_tab(self) -> QWidget:
        w = QWidget()
        lay = QVBoxLayout(w)
        split = QSplitter()
        self.album_source = QTabWidget()
        self.album_source.addTab(self._build_collection_panel(), "My collection")

        search = QWidget()
        sv = QVBoxLayout(search)
        sv.setContentsMargins(4, 6, 4, 4)
        row = QHBoxLayout()
        self.search_edit = QLineEdit()
        self.search_edit.setPlaceholderText("Artist and album, catalogue number, barcode, or a Discogs release URL")
        self.search_edit.returnPressed.connect(self._search)
        row.addWidget(self.search_edit, 1)
        self.vinyl_only = QCheckBox("Vinyl only")
        self.vinyl_only.setChecked(self.settings.vinyl_only_search)
        row.addWidget(self.vinyl_only)
        b = QPushButton("Search")
        b.clicked.connect(self._search)
        row.addWidget(b)
        sv.addLayout(row)
        self.results = QListWidget()
        self.results.setIconSize(QPixmap(64, 64).size())
        self.results.currentItemChanged.connect(self._result_selected)
        sv.addWidget(self.results, 1)
        b = QPushButton("Not on Discogs? Enter it yourself…")
        b.setToolTip("For white labels and anything else Discogs doesn't have: type in the tracklist")
        b.clicked.connect(lambda: self._edit_tracklist(None))
        sv.addWidget(b)
        self.album_source.addTab(search, "Search Discogs")
        split.addWidget(self.album_source)

        right = QWidget()
        rv = QVBoxLayout(right)
        top = QHBoxLayout()
        self.cover_label = QLabel()
        self.cover_label.setFixedSize(220, 220)
        self.cover_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.cover_label.setStyleSheet("background: #222; color: #888;")
        top.addWidget(self.cover_label)
        self.release_info = QLabel("Search for the record you are ripping, then pick the exact pressing.")
        self.release_info.setWordWrap(True)
        self.release_info.setAlignment(Qt.AlignmentFlag.AlignTop)
        self.release_info.setOpenExternalLinks(True)
        self.release_info.setTextFormat(Qt.TextFormat.RichText)
        top.addWidget(self.release_info, 1)
        rv.addLayout(top)
        self.preview_tracks = QTableWidget(0, 3)
        self.preview_tracks.setHorizontalHeaderLabels(["Pos", "Title", "Length"])
        self.preview_tracks.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        self.preview_tracks.verticalHeader().setVisible(False)
        self.preview_tracks.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        rv.addWidget(self.preview_tracks, 1)
        self.preview_status = QLabel()
        rv.addWidget(self.preview_status)
        row = QHBoxLayout()
        self.edit_preview_btn = QPushButton("Edit tracklist…")
        self.edit_preview_btn.setToolTip("Discogs wrong? Fix positions, titles or tracks. Saved for this record.")
        self.edit_preview_btn.setEnabled(False)
        self.edit_preview_btn.setMinimumHeight(36)
        self.edit_preview_btn.clicked.connect(lambda: self._edit_tracklist(self.preview))
        row.addWidget(self.edit_preview_btn)
        self.print_preview_btn = QPushButton("Print label…")
        self.print_preview_btn.setToolTip("Labels for the sleeve: Avery sheets or a thermal printer")
        self.print_preview_btn.setEnabled(False)
        self.print_preview_btn.setMinimumHeight(36)
        self.print_preview_btn.clicked.connect(lambda: self._print_labels(self.preview))
        row.addWidget(self.print_preview_btn)
        self.use_btn = QPushButton("Use this album and start recording →")
        self.use_btn.setEnabled(False)
        self.use_btn.setMinimumHeight(36)
        self.use_btn.clicked.connect(self._use_release)
        row.addWidget(self.use_btn, 1)
        rv.addLayout(row)
        split.addWidget(right)
        split.setSizes([520, 680])
        lay.addWidget(split, 1)
        return w

    # --- My collection ---

    def _build_collection_panel(self) -> QWidget:
        w = QWidget()
        v = QVBoxLayout(w)
        v.setContentsMargins(4, 6, 4, 4)
        row = QHBoxLayout()
        self.coll_filter = QLineEdit()
        self.coll_filter.setPlaceholderText("Filter by artist, title, label, cat#…")
        self.coll_filter.setClearButtonEnabled(True)
        self.coll_filter.textChanged.connect(self._fill_collection)
        row.addWidget(self.coll_filter, 1)
        self.coll_sort = QComboBox()
        for key, name in (("artist", "Artist"), ("title", "Title"), ("label", "Label"), ("year", "Year"),
                          ("added", "Recently added")):
            self.coll_sort.addItem(name, key)
        self.coll_sort.currentIndexChanged.connect(self._fill_collection)
        row.addWidget(self.coll_sort)
        v.addLayout(row)
        row = QHBoxLayout()
        self.coll_hide_done = QCheckBox("Hide saved")
        self.coll_hide_done.toggled.connect(self._fill_collection)
        row.addWidget(self.coll_hide_done)
        self.coll_vinyl = QCheckBox("Vinyl only")
        self.coll_vinyl.setChecked(True)
        self.coll_vinyl.toggled.connect(self._fill_collection)
        row.addWidget(self.coll_vinyl)
        row.addStretch()
        b = QPushButton("Next not saved ↓")
        b.setToolTip("Jump to the next record in the list that hasn't been saved yet")
        b.clicked.connect(self._select_next_unsaved)
        row.addWidget(b)
        self.coll_refresh = QPushButton("Refresh from Discogs")
        self.coll_refresh.clicked.connect(self._refresh_collection)
        row.addWidget(self.coll_refresh)
        v.addLayout(row)
        self.coll_list = QListWidget()
        self.coll_list.setIconSize(QPixmap(56, 56).size())
        self.coll_list.setUniformItemSizes(True)
        self.coll_list.setTextElideMode(Qt.TextElideMode.ElideNone)  # eliding collapses the 2-line rows
        self.coll_list.setStyleSheet("QListWidget::item { padding: 3px 2px; }")
        self.coll_list.currentItemChanged.connect(self._collection_selected)
        self.coll_list.verticalScrollBar().valueChanged.connect(self._queue_thumbs)
        v.addWidget(self.coll_list, 1)
        self.coll_info = QLabel()
        self.coll_info.setWordWrap(True)
        v.addWidget(self.coll_info)

        self.thumb_pool = QThreadPool(self)
        self.thumb_pool.setMaxThreadCount(4)
        self._thumbs: dict[int, QIcon] = {}
        self._thumb_pending: set[int] = set()
        self._thumb_timer = QTimer(self, singleShot=True, interval=120)
        self._thumb_timer.timeout.connect(self._load_visible_thumbs)
        self._preview_timer = QTimer(self, singleShot=True, interval=300)
        self._preview_timer.timeout.connect(self._load_selected_preview)
        self._coll_items: dict[int, QListWidgetItem] = {}
        QTimer.singleShot(0, self._fill_collection)
        if self.collection.items:
            self._status(f"Collection from {self.collection.updated.replace('T', ' ')}")
        elif self.settings.token():
            QTimer.singleShot(300, self._refresh_collection)
        return w

    def _refresh_collection(self):
        if not self.settings.token():
            self.coll_info.setText("<b>Add your Discogs token in File → Settings</b> to load your collection.")
            return
        client = self._client()
        self.coll_refresh.setEnabled(False)
        self.coll_info.setText("Loading your collection from Discogs…")
        progress = _Signals()  # reused for page progress from the worker thread
        progress.done.connect(lambda p: self.coll_info.setText(f"Loading your collection… page {p[0]} of {p[1]}"))

        def work():
            user = self.settings.discogs_username or client.identity()
            return user, client.collection(user, lambda page, pages: progress.done.emit((page, pages)))

        def done(result):
            user, items = result
            self._progress_signals = None
            self.settings.discogs_username = user
            self.settings.save()
            self.collection.save(user, items)
            self.coll_refresh.setEnabled(True)
            self._fill_collection()

        def failed(msg):
            self.coll_refresh.setEnabled(True)
            self.coll_info.setText(f"<span style='color:#f85149'>{msg}</span>")

        self._progress_signals = progress  # keep alive while the worker runs
        self.run_bg(work, done, failed)

    def _status_mark(self, release_id: int) -> tuple[str, QColor | None]:
        status = self.riplog.status(release_id)
        if status == library.SAVED:
            return "✓ ", QColor("#2da44e")
        if status == library.PARTIAL:
            return "◐ ", QColor("#d29922")
        return "", None

    def _fill_collection(self):
        if not hasattr(self, "coll_list"):
            return
        keep = self.coll_list.currentItem().data(Qt.ItemDataRole.UserRole) if self.coll_list.currentItem() else None
        text = self.coll_filter.text().strip()
        sort = self.coll_sort.currentData()
        items = [i for i in self.collection.items
                 if (not text or i.matches(text))
                 and (not self.coll_vinyl.isChecked() or i.is_vinyl)
                 and not (self.coll_hide_done.isChecked() and self.riplog.status(i.id) == library.SAVED)]
        keyfn = {
            "artist": lambda i: (i.artist.lower().removeprefix("the "), i.year, i.title.lower()),
            "title": lambda i: i.title.lower(),
            "label": lambda i: (i.label.lower(), i.catno.lower()),
            "year": lambda i: (i.year or "9999", i.artist.lower()),
            "added": lambda i: i.added,
        }[sort]
        items.sort(key=keyfn, reverse=sort == "added")

        self.coll_list.blockSignals(True)
        self.coll_list.clear()
        self._coll_items = {}
        for it in items:
            item = QListWidgetItem()
            item.setData(Qt.ItemDataRole.UserRole, it.id)
            self._coll_items[it.id] = item
            self._decorate_collection_item(item, it)
            self.coll_list.addItem(item)
        if keep in self._coll_items:
            self.coll_list.setCurrentItem(self._coll_items[keep])
        self.coll_list.blockSignals(False)

        self._update_coll_info(len(items) if text else None)
        self._queue_thumbs()

    def _update_coll_info(self, shown: int | None = None):
        if not self.collection.items:
            if self.settings.token():
                self.coll_info.setText("Press <b>Refresh from Discogs</b> to load your collection.")
            else:
                self.coll_info.setText("<b>Add your Discogs token in File → Settings</b> to load your collection.")
            return
        total = [i for i in self.collection.items if not self.coll_vinyl.isChecked() or i.is_vinyl]
        saved = sum(1 for i in total if self.riplog.status(i.id) == library.SAVED)
        who = f"{self.collection.username}'s collection · " if self.collection.username else ""
        self.coll_info.setText(f"{who}{len(total)} records · <b>{saved} saved</b> · {len(total) - saved} to go"
                               + (f" · {shown} shown" if shown is not None else ""))

    def _decorate_collection_item(self, item: QListWidgetItem, it: discogs.CollectionItem):
        # No symbol prefix here: symbol glyphs come from a taller fallback font and break the fixed row height.
        _, color = self._status_mark(it.id)
        short, note = self.riplog.short(it.id), self.riplog.describe(it.id)
        year_label = " · ".join(p for p in (it.year if it.year != "0" else "", f"{it.label} {it.catno}".strip()) if p)
        item.setText(f"{it.name}\n{(short.upper() + '  ·  ') if short else ''}{year_label}")
        item.setToolTip(f"{it.name}\n{it.subtitle}" + (f"\n{note}" if note else ""))
        item.setForeground(color if color else self.palette().text().color())
        if it.id in self._thumbs:
            item.setIcon(self._thumbs[it.id])

    def _refresh_marks(self):
        by_id = {i.id: i for i in self.collection.items}
        for rid, item in self._coll_items.items():
            if rid in by_id:
                self._decorate_collection_item(item, by_id[rid])
        for i in range(self.results.count()):
            item = self.results.item(i)
            mark, color = self._status_mark(item.data(Qt.ItemDataRole.UserRole))
            item.setText(mark + item.data(Qt.ItemDataRole.UserRole + 1))
            item.setForeground(color if color else self.palette().text().color())
        if self.coll_hide_done.isChecked():
            self._fill_collection()
        else:
            self._update_coll_info()
        if self.preview:
            self.preview_status.setText(self.riplog.describe(self.preview.id))

    def _queue_thumbs(self, *_):
        self._thumb_timer.start()

    def _load_visible_thumbs(self):
        vp = self.coll_list.viewport()
        first = self.coll_list.indexAt(vp.rect().topLeft()).row()
        last = self.coll_list.indexAt(vp.rect().bottomLeft()).row()
        if first < 0:
            return
        last = self.coll_list.count() - 1 if last < 0 else min(last + 10, self.coll_list.count() - 1)
        by_id = {i.id: i for i in self.collection.items}
        for row in range(first, last + 1):
            rid = self.coll_list.item(row).data(Qt.ItemDataRole.UserRole)
            if rid in self._thumbs or rid in self._thumb_pending or not by_id.get(rid, None):
                continue
            self._thumb_pending.add(rid)
            url = by_id[rid].thumb
            self.run_bg(lambda rid=rid, url=url: self._fetch_thumb(rid, url),
                        lambda res: self._thumb_ready(*res), lambda _: None, pool=self.thumb_pool)

    @staticmethod
    def _fetch_thumb(rid: int, url: str):
        path = library.thumb_path(rid)
        if path.exists():
            return rid, path.read_bytes()
        data = discogs.Client().image(url) if url else None
        if data:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)
        return rid, data

    def _thumb_ready(self, rid: int, data: bytes | None):
        self._thumb_pending.discard(rid)
        if not data:
            return
        pm = QPixmap()
        pm.loadFromData(data)
        icon = QIcon(pm)
        self._thumbs[rid] = icon
        if rid in self._coll_items:
            self._coll_items[rid].setIcon(icon)

    def _collection_selected(self, item, _prev=None):
        if item:
            # Wait for the arrow keys to settle so scrolling the list doesn't hit Discogs for every row.
            self._preview_timer.start()

    def _load_selected_preview(self):
        item = self.coll_list.currentItem()
        if item:
            self._load_preview(item.data(Qt.ItemDataRole.UserRole))

    def _select_next_unsaved(self, after_id: int | None = None):
        rows = self.coll_list.count()
        if not rows:
            return
        start = self.coll_list.currentRow()
        if after_id is not None and after_id in self._coll_items:
            start = self.coll_list.row(self._coll_items[after_id])
        for step in range(1, rows + 1):
            row = (start + step) % rows
            item = self.coll_list.item(row)
            rid = item.data(Qt.ItemDataRole.UserRole)
            if self.riplog.status(rid) != library.SAVED and rid not in self._saving:
                self.album_source.setCurrentIndex(0)
                self.coll_list.setCurrentRow(row)
                self.coll_list.scrollToItem(item, QAbstractItemView.ScrollHint.PositionAtCenter)
                return
        self._status("Everything in the list has been saved 🎉")

    def _client(self) -> discogs.Client:
        return discogs.Client(self.settings.token())

    def _search(self):
        q = self.search_edit.text().strip()
        if not q:
            return
        m = re.search(r"release/(\d+)", q) or re.fullmatch(r"\[?r?(\d+)\]?", q)
        if m:  # direct release lookup also works without a token
            self._load_preview(int(m.group(1)))
            return
        if not self.settings.token():
            QMessageBox.information(
                self, "Discogs token needed",
                "Searching Discogs needs a free personal access token.\n\n"
                "Open File → Settings and paste one in (link included there).\n\n"
                "Without a token you can still paste a Discogs release URL into the search box.")
            return
        self.settings.vinyl_only_search = self.vinyl_only.isChecked()
        self.results.clear()
        self._status("Searching Discogs…")
        client = self._client()
        vinyl = self.vinyl_only.isChecked()
        self.run_bg(lambda: client.search(q, vinyl), self._show_results)

    def _show_results(self, results: list[discogs.SearchResult]):
        self.results.clear()
        self._status(f"{len(results)} results")
        client = self._client()
        for r in results:
            text = f"{r.title}\n{r.subtitle}"
            mark, color = self._status_mark(r.id)
            item = QListWidgetItem(mark + text)
            item.setData(Qt.ItemDataRole.UserRole, r.id)
            item.setData(Qt.ItemDataRole.UserRole + 1, text)
            if color:
                item.setForeground(color)
            self.results.addItem(item)
            if r.thumb:
                def got(data, item=item):
                    if data and self.results.row(item) >= 0:
                        pm = QPixmap()
                        pm.loadFromData(data)
                        item.setIcon(QIcon(pm))
                self.run_bg(lambda url=r.thumb: client.image(url), got, lambda _: None)

    def _result_selected(self, item, _prev=None):
        if item:
            self._load_preview(item.data(Qt.ItemDataRole.UserRole))

    def _load_preview(self, release_id: int):
        self._preview_wanted = release_id
        if release_id in self._release_cache:
            self._preview_loaded(self._release_cache[release_id])
            return
        client = self._client()
        self.use_btn.setEnabled(False)
        self._status("Loading release…")

        def fetch():
            rel = client.release(release_id)
            rel.cover = client.image(rel.cover_url)
            return rel

        def done(rel: discogs.Release):
            self._release_cache[rel.id] = rel
            if rel.id == self._preview_wanted:  # ignore answers for rows you've already moved past
                self._preview_loaded(rel)
                self._status("")

        self.run_bg(fetch, done)

    def _preview_loaded(self, rel: discogs.Release):
        self.preview = self.edits.apply(rel)  # your saved corrections, if any
        self._show_preview(self.preview)
        self.preview_status.setText(self.riplog.describe(rel.id))
        self.use_btn.setEnabled(True)
        self.edit_preview_btn.setEnabled(True)
        self.print_preview_btn.setEnabled(True)

    def _edit_tracklist(self, rel: discogs.Release | None):
        """Fix a record's tracklist (or, with rel None, enter one that isn't on Discogs)."""
        original = self._release_cache.get(rel.id) if rel else None  # as Discogs has it
        dlg = TracklistEditor(rel, original, self)
        if not dlg.exec():
            return
        new = dlg.result_release()
        if original is not None and self._same_tracklist(new, original) and (
                new.artist, new.title, new.year, new.label, new.catno) == (
                original.artist, original.title, original.year, original.label, original.catno):
            self.edits.forget(new.id)  # back to exactly the Discogs version
        else:
            self.edits.save(new)
        if rel is None:  # a new record typed in by hand: go straight to recording it
            self.preview = new
            self._show_preview(new)
            self.use_btn.setEnabled(True)
            self._use_release()
            return
        if self.preview and self.preview.id == new.id:
            self.preview = new
            self._show_preview(new)
        if self.release and self.release.id == new.id:
            self._release_changed(new)

    @staticmethod
    def _same_tracklist(a: discogs.Release, b: discogs.Release) -> bool:
        key = lambda r: [(t.position, t.title, t.artist, t.duration) for t in r.tracks]  # noqa: E731
        return key(a) == key(b)

    def _release_changed(self, new: discogs.Release):
        """The album being ripped was edited. If its sides or track lengths changed, the splits are
        found again (track picks are track numbers, so they're forgotten too)."""
        old = self.release
        self.release = new
        structure = lambda r: [(t.position, t.duration) for t in r.tracks]  # noqa: E731
        changed = structure(old) != structure(new)
        self.album_label.setText(f"<b>{new.artist} – {new.title}</b> ({new.year})")
        self._update_record_album()
        for rec in self.recordings:
            if changed:
                rec.overrides.clear()
                rec.edges, rec.skips = [], set()
                if rec.side_order and sorted(rec.side_order) != sorted(take_sides(rec.side, new.sides)):
                    rec.side_order = None
            if rec.side not in (ALL_SIDES, *new.sides) and "+" not in rec.side:
                rec.side = ALL_SIDES  # that side no longer exists
        self._refresh_split_choices()
        if changed:
            self._status("Tracklist changed: tracks found again using the corrected tracklist.", 8000)

    def _show_preview(self, rel: discogs.Release):
        if rel.cover:
            pm = QPixmap()
            pm.loadFromData(rel.cover)
            self.cover_label.setPixmap(pm.scaled(220, 220, Qt.AspectRatioMode.KeepAspectRatio,
                                                 Qt.TransformationMode.SmoothTransformation))
        else:
            self.cover_label.setText("No cover")
        genres = ", ".join(rel.genres + rel.styles)
        self.release_info.setText(
            f"<h2>{rel.artist} – {rel.title}</h2>"
            f"<p>{rel.year} · {rel.label} {rel.catno}<br>{genres}</p>"
            f"<p>Sides: {', '.join(rel.sides)} · {len(rel.tracks)} tracks"
            + (" · <b>your corrected tracklist</b>" if self.edits.has(rel.id) else "") + "</p>"
            + (f"<p><a href='{rel.url}'>Open on Discogs</a></p>" if rel.url else ""))
        self.preview_tracks.setRowCount(len(rel.tracks))
        for i, t in enumerate(rel.tracks):
            title = f"{t.artist} – {t.title}" if t.artist else t.title
            for col, text in enumerate((t.position, title, fmt_time(t.duration)[:-2] if t.duration else "")):
                self.preview_tracks.setItem(i, col, QTableWidgetItem(text))
        self.preview_tracks.resizeColumnToContents(0)

    def _use_release(self):
        if not self.preview:
            return
        new = self.preview
        if self.release and self.release.id != new.id and self.recordings:
            unsaved = [r for r in self.recordings if not self._side_saved(r)]
            if unsaved:
                ans = QMessageBox.question(
                    self, "Switch album",
                    f"{len(unsaved)} recorded side(s) of {self.release.artist} – {self.release.title} haven't been "
                    "saved as tracks yet.\n\nRemove all sides from the list and switch album? "
                    "(The WAV files stay in the _recordings folder.)")
                if ans != QMessageBox.StandardButton.Yes:
                    return
            self._clear_recordings()
        self.release = new
        self.bpms, self.lengths = {}, {}
        for rec in self.recordings:
            rec.edges = []  # re-detect against the new tracklist
        self.album_label.setText(f"<b>{self.release.artist} – {self.release.title}</b> ({self.release.year})")
        self._set_save_buttons()
        self._update_record_album()
        self._refresh_split_choices()
        self.tabs.setCurrentIndex(TAB_SPLIT if self.recordings else TAB_RECORD)

    def _side_saved(self, rec: Recording) -> bool:
        e = self.riplog.entries.get(str(rec.release_id)) if rec.release_id else None
        sides = take_sides(rec.side, e.get("sides", []) if e else [])
        return bool(e and sides and all(s in e.get("sides_saved", []) for s in sides))

    def _clear_recordings(self):
        self.player.stop()
        self.recordings.clear()
        self.rec_list.clear()
        self.current = None
        self._refresh_split_choices()

    # ---------- 3. Split & save ----------

    def _build_split_tab(self) -> QWidget:
        w = QWidget()
        lay = QVBoxLayout(w)
        self.album_label = QLabel("No album chosen yet. Pick one on the Album tab.")
        lay.addWidget(self.album_label)

        row = QHBoxLayout()
        row.addWidget(QLabel("Recording:"))
        self.split_rec_combo = QComboBox()
        self.split_rec_combo.currentIndexChanged.connect(self._split_rec_changed)
        row.addWidget(self.split_rec_combo, 1)
        row.addWidget(QLabel("contains:"))
        self.split_side_combo = QComboBox()
        self.split_side_combo.setMinimumWidth(200)
        self.split_side_combo.currentIndexChanged.connect(self._split_side_changed)
        row.addWidget(self.split_side_combo)
        self.order_label = QLabel("played:")
        row.addWidget(self.order_label)
        self.order_combo = QComboBox()
        self.order_combo.setToolTip("The order you played the sides in. Changing it finds the tracks again.")
        self.order_combo.currentIndexChanged.connect(self._side_order_changed)
        row.addWidget(self.order_combo)
        b = QPushButton("Find tracks again")
        b.clicked.connect(lambda: self._detect(self.current))
        row.addWidget(b)
        self.reset_tracks_btn = QPushButton("Undo track picks")
        self.reset_tracks_btn.setToolTip("Forget the tracks you picked in the table and go back to Discogs order")
        self.reset_tracks_btn.clicked.connect(self._reset_track_picks)
        row.addWidget(self.reset_tracks_btn)
        b = QPushButton("Edit tracklist…")
        b.setToolTip("Discogs wrong? Fix positions, titles or tracks. Saved for this record.")
        b.clicked.connect(lambda: self.release and self._edit_tracklist(self.release))
        row.addWidget(b)
        lay.addLayout(row)

        # Zoom toolbar
        row = QHBoxLayout()
        for text, tip, fn in (
            ("−", "Zoom out", lambda: self.wave.zoom(1 / 1.6)),
            ("+", "Zoom in", lambda: self.wave.zoom(1.6)),
            ("Whole recording", "Show everything", lambda: self.wave.fit()),
        ):
            b = QPushButton(text)
            b.setToolTip(tip)
            if len(text) == 1:
                b.setFixedWidth(34)
            b.clicked.connect(fn)
            row.addWidget(b)
        b = QPushButton("🔍  Zoom to selected track's start")
        b.setToolTip("Show 8 seconds around where the selected track starts (or the first track)")
        b.clicked.connect(self._zoom_selected)
        row.addWidget(b)
        row.addSpacing(16)
        b = QPushButton("✂  Split at cursor")
        b.setToolTip("Add a split where the green dashed cursor is (click the waveform to place it)")
        b.clicked.connect(self._split_at_cursor)
        row.addWidget(b)
        row.addStretch()
        lay.addLayout(row)

        self.wave = WaveformView()
        self.wave.markersChanged.connect(self._markers_changed)
        self.wave.playRequested.connect(self._play)
        lay.addWidget(self.wave, 3)
        self.wave_scroll = QScrollBar(Qt.Orientation.Horizontal)
        self.wave_scroll.valueChanged.connect(self._scrollbar_moved)
        self.wave.viewChanged.connect(self._sync_scrollbar)
        lay.addWidget(self.wave_scroll)
        hint = QLabel("Space: play from the pink cursor / stop · scroll wheel: zoom · drag empty space or "
                      "Shift+wheel: move along · click: set cursor · "
                      "drag a line: move a split · double-click: add a split · right-click: more (remove split, "
                      "mark a part as not a track, set music start…)")
        hint.setWordWrap(True)
        hint.setStyleSheet("color: gray;")
        lay.addWidget(hint)

        # Space plays from the cursor / stops, anywhere on this tab (text boxes still get their spaces).
        space = QShortcut(QKeySequence(Qt.Key.Key_Space), w)
        space.setContext(Qt.ShortcutContext.WidgetWithChildrenShortcut)
        space.activated.connect(self._toggle_play)

        row = QHBoxLayout()
        b = QPushButton("▶  Play from cursor  (Space)")
        b.clicked.connect(lambda: self._play(self.wave.cursor))
        row.addWidget(b)
        b = QPushButton("▶  Play selected track")
        b.clicked.connect(self._play_selected)
        row.addWidget(b)
        b = QPushButton("■  Stop")
        b.clicked.connect(self.player.stop)
        row.addWidget(b)
        row.addStretch()
        lay.addLayout(row)

        self.track_table = QTableWidget(0, 6)
        self.track_table.setHorizontalHeaderLabels(["Pos", "Title", "Starts", "Length", "Discogs length", "BPM"])
        self.track_table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        self.track_table.verticalHeader().setVisible(False)
        self.track_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.track_table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.track_table.doubleClicked.connect(self._play_selected)
        lay.addWidget(self.track_table, 1)

        row = QHBoxLayout()
        row.addWidget(QLabel("Save to:"))
        self.out_label = QLabel(self.settings.output_dir)
        row.addWidget(self.out_label, 1)
        b = QPushButton("Change…")
        b.clicked.connect(self._open_settings)
        row.addWidget(b)
        b = QPushButton("Print labels…")
        b.setToolTip("Labels for the sleeve: Avery sheets or a thermal printer, with measured lengths and BPMs")
        b.clicked.connect(lambda: self._print_labels(self.release))
        row.addWidget(b)
        row.addSpacing(12)
        row.addWidget(QLabel("Format:"))
        self.format_combo = QComboBox()
        for key, name in export.FORMATS.items():
            self.format_combo.addItem(name, key)
        self.format_combo.setCurrentIndex(max(0, self.format_combo.findData(self.settings.export_format)))
        self.format_combo.currentIndexChanged.connect(self._format_changed)
        row.addWidget(self.format_combo)
        self.normalize = QCheckBox("Normalize volume")
        self.normalize.setToolTip("Turn the whole album up or down so its loudest moment is at -1 dB. Only the "
                                  "volume changes (no compression or EQ), so the tracks keep their balance.")
        self.normalize.setChecked(self.settings.normalize)
        self.normalize.toggled.connect(lambda on: setattr(self.settings, "normalize", on))
        row.addWidget(self.normalize)
        self.save_btn = QPushButton()
        self.save_btn.setMinimumHeight(40)
        self.save_btn.setStyleSheet("QPushButton { font-weight: bold; }")
        self.save_btn.clicked.connect(lambda: self._save_all(then_next=False))
        row.addWidget(self.save_btn)
        self.save_next_btn = QPushButton("Save and start next album  →")
        self.save_next_btn.setMinimumHeight(40)
        self.save_next_btn.setStyleSheet("QPushButton { font-weight: bold; }")
        self.save_next_btn.setToolTip("Saves in the background while you pick the next record, check levels and "
                                      "start recording. You'll see in the status bar when it's done.")
        self.save_next_btn.clicked.connect(lambda: self._save_all(then_next=True))
        row.addWidget(self.save_next_btn)
        lay.addLayout(row)
        self._format_changed()
        return w

    def _format_changed(self):
        self.settings.export_format = self.format_combo.currentData()
        name = export.SHORT_NAMES.get(self.settings.export_format, "FLAC")
        self.save_btn.setText(f"Save all tracks as {name}")

    def _refresh_split_choices(self):
        keep = self.current
        self.split_rec_combo.blockSignals(True)
        self.split_rec_combo.clear()
        for rec in self.recordings:
            self.split_rec_combo.addItem(rec.label)
        self.split_rec_combo.blockSignals(False)
        self.split_side_combo.blockSignals(True)
        sides = self.release.sides if self.release else SIDE_LETTERS[:4]
        fill_side_combo(self.split_side_combo, sides, whole=True)
        self.split_side_combo.blockSignals(False)
        if keep in self.recordings:
            self.split_rec_combo.setCurrentIndex(self.recordings.index(keep))
        elif self.recordings:
            self.split_rec_combo.setCurrentIndex(0)
        self._split_rec_changed()

    def _split_rec_changed(self):
        i = self.split_rec_combo.currentIndex()
        rec = self.recordings[i] if 0 <= i < len(self.recordings) else None
        self.current = rec
        self.player.stop()
        if rec is None:
            self.wave.set_data(None, 0.02, 0)
            self.track_table.setRowCount(0)
            return
        j = self.split_side_combo.findData(rec.side)
        self.split_side_combo.blockSignals(True)
        if j >= 0:
            self.split_side_combo.setCurrentIndex(j)
        self.split_side_combo.blockSignals(False)
        self._fill_order_combo(rec)
        self._show_recording(rec)

    def _split_side_changed(self):
        rec = self.current
        if rec is None:
            return
        rec.side = self.split_side_combo.currentData()
        rec.edges, rec.skips, rec.side_order = [], set(), None
        rec.overrides.clear()
        self._refresh_rec_labels(rec)
        self._fill_order_combo(rec)
        self._show_recording(rec)

    def _fill_order_combo(self, rec: Recording):
        """Side order choices for a take with several sides: as on Discogs, or played backwards."""
        sides = take_sides(rec.side, self.release.sides if self.release else [])
        self.order_combo.blockSignals(True)
        self.order_combo.clear()
        if len(sides) > 1:
            for order in (sides, sides[::-1]):
                self.order_combo.addItem(" → ".join(order), order)
            self.order_combo.setCurrentIndex(1 if rec.side_order == sides[::-1] else 0)
        self.order_combo.blockSignals(False)
        for w in (self.order_label, self.order_combo):
            w.setVisible(len(sides) > 1)

    def _side_order_changed(self):
        rec = self.current
        if rec is None or self.order_combo.currentData() is None:
            return
        order = self.order_combo.currentData()
        rec.side_order = None if order == take_sides(rec.side, self.release.sides if self.release else []) else order
        rec.overrides.clear()
        self._detect(rec)  # side lengths decide where the flip is, so find everything again

    def _reset_track_picks(self):
        if self.current:
            self.current.overrides.clear()
            self._update_split_view()

    def _side_tracks(self, rec: Recording) -> list[discogs.Track]:
        """The take's tracks in the order they were played."""
        if not self.release:
            return []
        return tracklist.sequence(self.release.tracks, self._sides_of(rec))

    def _rows(self, rec: Recording) -> list[tracklist.Row]:
        """Pair the parts between splits with tracks: in playing order, except where you picked one.

        Returns (part index, (start, end), track) rows; part or track is None where they don't match up.
        """
        all_tracks = self.release.tracks if self.release else []
        return tracklist.assign(rec.edges, rec.skips, self._side_tracks(rec), all_tracks, rec.overrides)

    def _pick_track(self, part: int, choice):
        """A track was chosen in the table for this part (or 'not a track')."""
        rec = self.current
        if rec is None:
            return
        if choice == "skip":
            tracklist.set_override(rec.overrides, rec.edges, part, None)
            if part not in rec.skips:
                self.wave.toggle_skip(part)  # updates rec.skips and the view
            return
        if part in rec.skips:
            rec.skips.discard(part)
            self.wave.skips.discard(part)
        tracklist.set_override(rec.overrides, rec.edges, part, choice)
        self._update_split_view(update_wave=False)
        self.wave.update()

    def _show_recording(self, rec: Recording):
        if rec.env is None:
            self.wave.set_data(None, 0.02, 0)
            return
        self.wave.set_data(rec.env.peak, rec.env.hop, rec.env.duration, rec.path)
        self.wave.clips = rec.clips
        if not rec.edges:
            self._detect(rec)
        else:
            self._update_split_view()

    def _detect(self, rec: Recording | None):
        if rec is None or rec.env is None:
            return
        gap = self.settings.min_gap_seconds
        sides = self._sides_of(rec)
        if self.release and len(sides) > 1:  # several sides in one take: find the flips too
            per_side = [[t.duration for t in self.release.tracks_on(s)] for s in sides]
            result = splitter.split_sides(rec.env, per_side, min_gap=gap, known_breaks=rec.side_breaks)
            edges, skips = result.edges, set(result.skips)
        else:
            tracks = self._side_tracks(rec)
            durations = [t.duration for t in tracks] if tracks else None
            result = splitter.split(rec.env, durations=durations, track_count=len(tracks) or None, min_gap=gap)
            edges, skips = result.edges_and_skips()
        # Snap each track start to its first beat and each end to the end of its fade
        try:
            edges = splitter.refine_edges(rec.path, edges, skips, music_db=splitter._levels(rec.env)[1])
        except (OSError, RuntimeError):
            pass
        rec.edges, rec.skips = edges, skips
        if rec is self.current:
            self._update_split_view()

    def _markers_changed(self):
        if self.current:
            self.current.edges = list(self.wave.edges)
            self.current.skips = set(self.wave.skips)
            self._update_split_view(update_wave=False)

    def _update_split_view(self, update_wave: bool = True):
        rec = self.current
        rows = self._rows(rec)
        n_parts = len(rec.edges) - 1
        labels = ["?"] * n_parts
        for part, s, t in rows:
            if part is not None:
                labels[part] = self._skip_kind(s)[0] if part in rec.skips else (t.position if t else "?")
        if update_wave:
            self.wave.set_edges(rec.edges, labels, rec.skips)
        else:
            self.wave.labels = labels
            self.wave.update()
        self.track_table.setRowCount(len(rows))
        for r, (part, s, t) in enumerate(rows):
            skipped = part in rec.skips
            bpm = self.bpms.get(t.number) if t else None
            if skipped:
                cells = ["—", self._skip_kind(s)[1], fmt_time(s[0]), fmt_time(s[1] - s[0]), "", ""]
            else:
                cells = [
                    t.position if t else "?",
                    (f"{t.artist} – {t.title}" if t.artist else t.title) if t else "(not saved: no Discogs track)",
                    fmt_time(s[0]) if s else "(missing split)",
                    fmt_time(s[1] - s[0]) if s else "",
                    fmt_time(t.duration)[:-2] if t and t.duration else "",
                    f"{bpm:g}" if bpm else ("–" if t and t.number in self.bpms else ""),
                ]
            for col, text in enumerate(cells):
                item = QTableWidgetItem(text)
                if skipped:
                    item.setForeground(Qt.GlobalColor.gray)
                elif t is None or s is None:
                    item.setForeground(Qt.GlobalColor.red)
                self.track_table.setItem(r, col, item)
            self.track_table.setCellWidget(r, 1, self._track_picker(part, t, skipped, cells[1]))
        self.track_table.resizeColumnToContents(0)
        self.reset_tracks_btn.setEnabled(bool(rec.overrides))
        tracks = self._side_tracks(rec)
        pieces = n_parts - len(rec.skips)
        if tracks and pieces != len(tracks):
            self._status(f"{side_name(rec.side)} has {len(tracks)} tracks on Discogs but {pieces} pieces here. "
                         "Add or remove splits.", 0)
        else:
            self._status("")

    def _track_picker(self, part: int | None, track: discogs.Track | None, skipped: bool, text: str) -> QWidget | None:
        """Dropdown in the Title cell to say which track a part really is (or that it isn't one)."""
        if part is None or not self.release:
            return None  # a Discogs track no part was matched to: nothing to pick
        combo = QComboBox()
        combo.setFrame(False)
        combo.setToolTip("Pick the track this part really is. The parts after it follow on in order.")
        if skipped:
            combo.addItem(f"— {text}", "skip")
        elif track is None:
            combo.addItem("? (pick the track)", None)
        for t in self.release.tracks:
            name = f"{t.artist} – {t.title}" if t.artist else t.title
            combo.addItem(f"{t.position}   {name}", t.number)
        if not skipped:
            combo.addItem("— Not a track (gap, silence…)", "skip")
        combo.setCurrentIndex(max(0, combo.findData(track.number)) if track else 0)

        def picked(i):
            choice = combo.itemData(i)
            if choice is not None:
                # Rebuilding the table replaces this dropdown, so do it after Qt is done with the signal
                QTimer.singleShot(0, lambda: self._pick_track(part, choice))

        combo.currentIndexChanged.connect(picked)
        return combo

    @staticmethod
    def _skip_kind(seg: tuple[float, float]) -> tuple[str, str]:
        """(waveform label, table text) for a part that isn't a track."""
        if seg[1] - seg[0] >= splitter.MIN_FLIP_SECONDS:
            return "flip", "Side change / silence · not saved"
        return "gap", "Gap between tracks · not saved"

    def _selected_part(self) -> int | None:
        rec = self.current
        if not rec or len(rec.edges) < 2:
            return None
        rows = self._rows(rec)
        r = self.track_table.currentRow()
        if 0 <= r < len(rows) and rows[r][0] is not None:
            return rows[r][0]
        return 0

    def _zoom_selected(self):
        part = self._selected_part()
        if part is not None:
            self.wave.zoom_to(self.current.edges[part], 8.0)

    def _split_at_cursor(self):
        if self.current and not self.wave.add_split(self.wave.cursor):
            self._status("Click the waveform inside the music to place the cursor, then split.")

    def _sync_scrollbar(self):
        w = self.wave
        sb = self.wave_scroll
        total_ms, page_ms = int(w.duration * 1000), int(w.span * 1000)
        sb.blockSignals(True)
        sb.setRange(0, max(0, total_ms - page_ms))
        sb.setPageStep(max(1, page_ms))
        sb.setSingleStep(max(1, page_ms // 10))
        sb.setValue(int(w.view_start * 1000))
        sb.blockSignals(False)
        sb.setVisible(page_ms < total_ms)

    def _scrollbar_moved(self, value: int):
        start = value / 1000
        self.wave.set_view(start, start + self.wave.span)

    def _play(self, start: float, length: float | None = None):
        if not self.current:
            return
        if self._scanning:  # the audio thread is busy; playing now would freeze the window until it's done
            self._status("Still looking for audio devices… try again in a few seconds.")
            return
        try:
            self.player.play(self.current.path, start, start + length if length else None)
        except Exception as e:
            self._error(f"Could not play audio: {e}")

    def _play_selected(self):
        part = self._selected_part()
        if part is not None:
            start = self.current.edges[part]
            self.wave.cursor = start
            self.wave.update()
            self._play(start)

    def _toggle_play(self):
        """Space bar: stop if playing, otherwise play from the cursor."""
        if self.player.is_playing:
            self.player.stop()
        else:
            self._play(self.wave.cursor)

    def _save_all(self, then_next: bool = False):
        """Save every side's tracks. then_next: save in the background and go straight on to the next
        album, so the next record can be picked and recorded while this one saves."""
        if not self.release:
            self._error("Pick the album on the Album tab first.")
            return
        jobs, problems = [], []
        for rec in self.recordings:
            if rec.env is None:
                problems.append(f"{rec.path.name} is still being analysed")
                continue
            if not rec.edges:
                self._detect(rec)
            tracks = self._side_tracks(rec)
            if not tracks:
                problems.append(f"{side_name(rec.side)}: no tracks on Discogs for that side")
                continue
            pieces = len(rec.edges) - 1 - len(rec.skips)
            if pieces != len(tracks):
                problems.append(f"{side_name(rec.side)}: {pieces} pieces for {len(tracks)} tracks")
            cuts = [export.TrackCut(t, s[0], s[1]) for part, s, t in self._rows(rec)
                    if t is not None and s is not None and part not in rec.skips]
            jobs.append((rec, cuts))
        # The same track picked for two parts would save one file over the other
        counts: dict[str, int] = {}
        for _, cuts in jobs:
            for c in cuts:
                counts[c.track.position] = counts.get(c.track.position, 0) + 1
        dupes = [p for p, n in counts.items() if n > 1]
        if dupes:
            self._error(f"{', '.join(dupes)} is picked for more than one part. Pick a different track for one of "
                        "them (Title column), or use Undo track picks.")
            return
        if not jobs:
            self._error("Nothing to save.\n\n" + "\n".join(problems))
            return
        if problems:
            ans = QMessageBox.question(self, "Save anyway?", "Some sides don't line up:\n\n" + "\n".join(problems)
                                       + "\n\nSave what matches anyway?")
            if ans != QMessageBox.StandardButton.Yes:
                return
        release, out = self.release, Path(self.settings.output_dir)
        fmt, detect, low = self.settings.export_format, self.settings.detect_bpm, self.settings.bpm_low
        pattern, normalize = self.settings.name_pattern, self.settings.normalize
        self.settings.save()
        if release.id in self._saving:
            self._error(f"{release.artist} – {release.title} is already being saved.")
            return
        self._saving[release.id] = release
        self._set_save_buttons()
        self._status("Saving tracks" + (" and detecting BPM…" if detect else "…"), 0)

        sides = [s for rec, _ in jobs for s in self._sides_of(rec)]

        gain = {"db": 0.0}

        def work():
            if normalize:  # one gain for the whole album, from its loudest moment across all sides
                gain["db"] = export.normalize_gain(max(export.measure_peak(rec.path, cuts) for rec, cuts in jobs))
            saved = []
            for rec, cuts in jobs:
                saved += export.export_side(rec.path, cuts, release, out, fmt=fmt, bpm_low=low, detect_bpm=detect,
                                            pattern=pattern, gain_db=gain["db"])
            export.write_replaygain(saved)
            return saved

        def done(saved: list[export.SavedTrack]):
            self._saving.pop(release.id, None)
            self._set_save_buttons()
            still_here = self.release is not None and self.release.id == release.id
            if still_here:  # not moved on to the next album meanwhile
                if detect:
                    self.bpms.update({s.track.number: s.bpm for s in saved})
                self.lengths.update({s.track.number: s.length for s in saved})
                if self.current:
                    self._update_split_view()
            saved_folders = list(dict.fromkeys(p.parent for s in saved for p in s.files))
            folder = saved_folders[0] if saved_folders else export.album_folder(out, release, pattern=pattern)
            for rec, _ in jobs:
                rec.release_id = release.id
            self.riplog.mark_saved(release, fmt, folder, sides)
            self._refresh_marks()
            if self.settings.queue_labels:
                self.label_queue.add(labels.to_dict(self._label_for(release)[0]))
            if self.settings.rekordbox:
                try:
                    rekordbox.add_folders(self.settings.rekordbox_xml, saved_folders, self.settings.rekordbox_prefer)
                except (OSError, ET.ParseError) as e:
                    self._status(f"Couldn't update the Rekordbox library: {e}", 0)
            self._status(f"Saved {len(saved)} tracks to {folder}", 0)
            if then_next or not still_here or self.recorder.is_recording:
                # Busy with the next record: don't interrupt with a window
                self._status(f"✓ Saved {release.artist} – {release.title}: {len(saved)} tracks to {folder}"
                             + (" · still saving another album…" if self._saving else ""), 0)
                return
            lines = [f"{s.track.position}  {s.track.title}  ·  {fmt_time(s.length)[:-2]}"
                     + (f"  ·  {s.bpm:g} BPM" if s.bpm else ("  ·  BPM not found" if detect else ""))
                     for s in saved]
            todo = [s for s in release.sides if s not in self.riplog.entries[str(release.id)]["sides_saved"]]
            more = f"\n\nStill to record: side {', '.join(todo)}" if todo else ""
            if normalize:
                more = (f"\n\nVolume {'raised' if gain['db'] >= 0 else 'lowered'} by {abs(gain['db']):.1f} dB: "
                        f"the loudest moment is now at {export.NORMALIZE_TARGET_DB:g} dB.") + more
            while True:  # printing labels comes back here, so you can still go on to the next album
                choice = self._after_save_dialog(
                    f"Saved {len(saved)} tracks to:\n{folder.parent}\n\n" + "\n".join(lines) + more,
                    offer_next=not todo and bool(self.collection.items))
                if choice != "print":
                    break
                self._print_labels(release)
            if choice == "open":
                QDesktopServices.openUrl(QUrl.fromLocalFile(str(folder)))
            elif choice == "next":
                self._go_to_next_album(release.id)

        def failed(msg):
            self._saving.pop(release.id, None)
            self._set_save_buttons()
            self._error(f"Saving {release.artist} – {release.title} failed: {msg}\n\nThe recording is still in the "
                        "_recordings folder: open it with File → Open recording to try again.")

        self.run_bg(work, done, failed)
        if then_next:
            self._go_to_next_album(release.id)
            self._status(f"Saving {release.artist} – {release.title} in the background. Pick the next record "
                         "and start recording whenever you're ready.", 0)

    def _set_save_buttons(self):
        """The save buttons are off while the album shown is being saved."""
        busy = self.release is not None and self.release.id in self._saving
        self.save_btn.setEnabled(not busy)
        self.save_next_btn.setEnabled(not busy)

    def _after_save_dialog(self, text: str, offer_next: bool) -> str:
        box = QMessageBox(QMessageBox.Icon.Information, "Saved", text, parent=self)
        nxt = box.addButton("Next album →", QMessageBox.ButtonRole.AcceptRole) if offer_next else None
        prt = box.addButton("Print labels…", QMessageBox.ButtonRole.ActionRole)
        opn = box.addButton("Open folder", QMessageBox.ButtonRole.ActionRole)
        box.addButton(QMessageBox.StandardButton.Close)
        if nxt:
            box.setDefaultButton(nxt)
        box.exec()
        clicked = box.clickedButton()
        if nxt and clicked is nxt:
            return "next"
        return "print" if clicked is prt else "open" if clicked is opn else "close"

    def _rekordbox_all(self):
        out, xml, prefer = Path(self.settings.output_dir), self.settings.rekordbox_xml, self.settings.rekordbox_prefer
        self._status("Adding your saved albums to the Rekordbox library…", 0)

        def done(n):
            self._status(f"{n} albums in {xml}", 0)
            QMessageBox.information(
                self, "Rekordbox",
                f"{n} albums are in the Rekordbox library:\n{xml}\n\nIn Rekordbox: Preferences → Advanced → "
                "Database → rekordbox xml → Imported Library, and choose that file (only needed once). The "
                "albums appear under \"rekordbox xml\" → Playlists → Vinyl Recorder: right-click to import.")

        self.run_bg(lambda: rekordbox.add_folders(xml, rekordbox.all_album_folders(out), prefer), done)

    def _label_for(self, release: discogs.Release) -> tuple[labels.LabelData, bool]:
        """A record's label, with the lengths and BPMs measured from its saved tracks (and whether any were)."""
        bpms, lengths = export.read_saved(Path(self.settings.output_dir), release, self.settings.name_pattern)
        if self.release and release.id == self.release.id:  # this session's results are the newest
            bpms.update({n: b for n, b in self.bpms.items() if b})
            lengths.update(self.lengths)
        return labels.label_data(release, bpms, lengths), bool(bpms or lengths)

    def _print_labels(self, release: discogs.Release | None):
        """Print labels for a record (or, with release None, the label queue)."""
        record, measured = self._label_for(release) if release else (None, False)
        if record is None and not self.label_queue.items:
            self._error("Pick an album first. The label queue is empty too: saved albums are added to it.")
            return
        PrintLabelsDialog(self.settings, self.label_queue, record, measured, self).exec()

    def _go_to_next_album(self, after_id: int):
        """Clear the finished album's sides and select the next record in the collection that isn't saved."""
        self._clear_recordings()
        self.tabs.setCurrentIndex(TAB_ALBUM)
        self.album_source.setCurrentIndex(0)
        self._select_next_unsaved(after_id)

    # ---------- shutdown ----------

    def closeEvent(self, e):
        if self._saving:
            names = ", ".join(f"{r.artist} – {r.title}" for r in self._saving.values())
            ans = QMessageBox.question(self, "Still saving", f"Still saving {names}.\n\nQuit anyway? "
                                       "(Its tracks won't all be saved.)")
            if ans != QMessageBox.StandardButton.Yes:
                e.ignore()
                return
        if self.recorder.is_recording:
            ans = QMessageBox.question(self, "Still recording", "Stop recording and quit?")
            if ans != QMessageBox.StandardButton.Yes:
                e.ignore()
                return
        self.player.stop()
        self.recorder.close()
        self.settings.save()
        super().closeEvent(e)
