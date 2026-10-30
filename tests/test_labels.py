"""Labels: what goes on them, how they're laid out on sheets and rolls, and the PDF page sizes."""

import os
import re

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import numpy as np
import soundfile as sf
from PySide6.QtWidgets import QApplication

from vinyl import labels
from vinyl.discogs import Release, Track
from vinyl.export import TrackCut, export_side, read_saved

app = QApplication.instance() or QApplication([])

EIGHT = Release(7, "Various", "Compilation", "1990", ["Electronic"], ["House"], "A&M Records (2)", "SP 1",
                [Track(f"{s}{n}", f"Song {s}{n}", 300 + n, f"Artist {s}{n}", i)
                 for i, (s, n) in enumerate([(s, n) for s in "AB" for n in range(1, 5)], 1)], "", "")


def test_label_data_prefers_measured_values():
    d = labels.label_data(EIGHT, bpms={1: 120.9, 2: None}, lengths={1: 326.6})
    assert d.label == "A&M Records"  # Discogs "(2)" suffix gone
    first = d.tracks[0]
    assert first.name == "Artist A1 – Song A1" and first.length == "5:27" and first.bpm == "121"
    assert d.tracks[1].length == "5:02" and d.tracks[1].bpm == ""  # Discogs length, no BPM measured


def test_long_tracklist_continues_on_next_label():
    d = labels.label_data(EIGHT)
    for tmpl in labels.TEMPLATES:
        placed, _ = labels.place(tmpl, d)
        assert [len(p.tracks) for p in placed] == [6, 2], tmpl.id
        assert [p.first for p in placed] == [True, False]


def test_sheet_start_position_and_pages():
    d = labels.label_data(EIGHT)
    sheet = labels.TEMPLATES_BY_ID["avery_5163"]  # 2 × 5
    placed, _ = labels.place(sheet, d, copies=1, start=4)
    slots = labels.slots(sheet)
    assert (placed[0].x, placed[0].y) == slots[3] and (placed[1].x, placed[1].y) == slots[4]
    placed, _ = labels.place(sheet, d, copies=5, start=3)  # 10 labels from position 3: spills onto page 2
    assert [p.page for p in placed].count(1) == 2
    # Avery 5163: 4" labels, 0.156" side margins, 0.5" top margin
    assert slots[0] == (0.156 * 72, 0.5 * 72) and round(slots[1][0] - slots[0][0], 1) == round(4 * 72 + (612 - 2 * 0.156 * 72 - 8 * 72), 1)


def test_pdf_page_sizes(tmp_path):
    d = labels.label_data(EIGHT)
    expect = {"avery_5163": (612, 792, 1), "avery_5164": (612, 792, 1), "roll_4x2": (288, 144, 2),
              "roll_4x3": (288, 216, 2)}
    for tid, (w, h, pages) in expect.items():
        path = tmp_path / f"{tid}.pdf"
        assert labels.save_pdf(path, labels.TEMPLATES_BY_ID[tid], d) == pages
        box = re.findall(rb"/MediaBox \[0 0 ([\d.]+) ([\d.]+)\]", path.read_bytes())
        assert (round(float(box[0][0])), round(float(box[0][1]))) == (w, h), tid
    rotated = tmp_path / "rot.pdf"
    labels.save_pdf(rotated, labels.TEMPLATES_BY_ID["roll_4x2"], d, rotate=True)
    box = re.findall(rb"/MediaBox \[0 0 ([\d.]+) ([\d.]+)\]", rotated.read_bytes())
    assert (round(float(box[0][0])), round(float(box[0][1]))) == (144, 288)


def test_preview_draws_something():
    img = labels.preview(labels.TEMPLATES_BY_ID["roll_4x2"], labels.label_data(EIGHT), dpi=50)
    assert (img.width(), img.height()) == (200, 100)
    dark = sum(1 for x in range(0, 200, 2) for y in range(0, 100, 2) if img.pixelColor(x, y).lightness() < 100)
    assert dark > 50  # text and lines were drawn


def test_read_saved_lengths_and_bpms(tmp_path):
    rel = Release(9, "DJ", "Album", "2000", [], [], "", "", [Track("A1", "One", 10, "", 1),
                                                             Track("A2", "Two", 10, "", 2)], "", "")
    wav = tmp_path / "side.wav"
    sf.write(wav, np.zeros((44100 * 25, 2), np.float32), 44100)
    cuts = [TrackCut(rel.tracks[0], 0, 9.5), TrackCut(rel.tracks[1], 12, 24)]
    export_side(wav, cuts, rel, tmp_path / "out", fmt="both", detect_bpm=False)
    bpms, lengths = read_saved(tmp_path / "out", rel)
    assert round(lengths[1], 1) == 9.5 and round(lengths[2], 1) == 12.0 and bpms == {}
