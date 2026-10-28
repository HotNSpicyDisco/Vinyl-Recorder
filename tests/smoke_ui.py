"""Drive the real window offscreen with a fake collection (no network):
pick the next unsaved album, record a side, save it, check it gets marked and the next album is offered.

Run: .venv\\Scripts\\python.exe tests\\smoke_ui.py <out_dir>
"""

import os
import sys
import time
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

out = Path(sys.argv[1])
out.mkdir(parents=True, exist_ok=True)

import vinyl.settings as vs

vs.SETTINGS_PATH = out / "settings.json"  # settings, rip log and collection cache all go here

import numpy as np
import soundfile as sf
from PySide6.QtWidgets import QApplication, QMessageBox

from test_tempo import SR, drum_loop
from vinyl import discogs, library
from vinyl.discogs import CollectionItem, Release, Track
from vinyl.ui.main_window import TAB_ALBUM, TAB_RECORD, TAB_SPLIT, MainWindow

# Fake collection: one already saved, one half done, two to go (plus a CD that is hidden by "Vinyl only")
coll = [
    CollectionItem(101, "Chic", "C'est Chic", "1978", "Atlantic", "SD 19209", ["Vinyl", "LP", "Album"], "", "2024-01-03"),
    CollectionItem(102, "Cerrone", "Supernature", "1977", "Malligator", "773 805", ["Vinyl", "LP"], "", "2024-02-01"),
    CollectionItem(103, "Donna Summer", "Bad Girls", "1979", "Casablanca", "NBLP 2-7150", ["Vinyl", "LP"], "", "2024-03-01"),
    CollectionItem(104, "Sylvester", "Step II", "1978", "Fantasy", "F-9556", ["Vinyl", "LP"], "", "2024-04-01"),
    CollectionItem(105, "Sade", "Diamond Life", "1984", "Epic", "EPC 26044", ["CD", "Album"], "", "2024-05-01"),
]
library.CollectionCache().save("test-user", coll)
log = library.RipLog()
done_rel = Release(101, "Chic", "C'est Chic", "1978", [], [], "Atlantic", "SD 19209",
                   [Track("A1", "Chic Cheer", 200, "", 1), Track("B1", "Le Freak", 300, "", 2)], "", "")
log.mark_recorded(done_rel, "A"); log.mark_recorded(done_rel, "B")
log.mark_saved(done_rel, "flac", out, ["A", "B"])
half = Release(102, "Cerrone", "Supernature", "1977", [], [], "", "",
               [Track("A1", "Supernature", 600, "", 1), Track("B1", "In The Smoke", 300, "", 2)], "", "")
log.mark_recorded(half, "A")

fetched = []


def fake_release(self, rid):
    fetched.append(rid)
    item = next(i for i in coll if i.id == rid)
    return Release(rid, item.artist, item.title, item.year, ["Electronic"], ["Disco"], item.label, item.catno,
                   [Track("A1", "Opener", 21, "", 1), Track("A2", "Middle", 20, "", 2), Track("A3", "Closer", 25, "", 3)],
                   "", "")


discogs.Client.release = fake_release
discogs.Client.image = lambda self, url: None

app = QApplication([])
win = MainWindow()
win.settings.output_dir = str(out / "music")
win.settings.discogs_token = ""  # make sure nothing tries the network
win.resize(1200, 820)
win.show()


def pump(seconds, until=None):
    end = time.time() + seconds
    while time.time() < end:
        app.processEvents()
        if until and until():
            return True
        time.sleep(0.02)
    return False


pump(0.8)
rows = [win.coll_list.item(i).text().split("\n")[0] for i in range(win.coll_list.count())]
print("collection rows:", rows)
print("info:", win.coll_info.text())

# Go down the list (sorted by artist: Cerrone, Chic, Donna Summer, Sylvester).
# From Cerrone, "Next not saved" skips the saved Chic album and lands on Donna Summer.
win.coll_list.setCurrentRow(0)
win._select_next_unsaved()
print("next not saved ->", win.coll_list.currentItem().text().split("\n")[0])
assert pump(3, lambda: win.preview and win.preview.id == 103), "preview never loaded"
print("preview status:", repr(win.preview_status.text()), "| fetched:", fetched)
win.grab().save(str(out / "1_album.png"))

win._use_release()
assert win.tabs.currentIndex() == TAB_RECORD

# A recorded side A (pretend the recorder made it)
gap = np.zeros((int(SR * 2.5), 2), np.float32)
side = np.concatenate([gap, drum_loop(118.0, 20), gap, drum_loop(124.0, 19), gap, drum_loop(100.0, 24), gap])
wav = out / win._recording_path("A").name
sf.write(wav, side, SR, subtype="PCM_24")
rec = win._add_recording(wav, "A")
rec.release_id = 103
win.riplog.mark_recorded(win.release, "A")
win._refresh_marks()
assert pump(10, lambda: rec.env is not None)
print("after recording:", win.riplog.describe(103))

win.tabs.setCurrentIndex(TAB_SPLIT)
pump(0.3)
dialog = {}
win._after_save_dialog = lambda text, offer_next: dialog.update(text=text, offer_next=offer_next) or "next"
win._save_all()
assert pump(30, lambda: win.save_btn.isEnabled() and dialog), "save never finished"
print("after save:", win.riplog.describe(103), "| offered next album:", dialog["offer_next"])
pump(1.0)
print("now on tab:", win.tabs.currentIndex() == TAB_ALBUM and "Album",
      "| selected:", win.coll_list.currentItem().text().split("\n")[0])
print("recordings cleared:", win.recordings == [])
print("info:", win.coll_info.text())
win.grab().save(str(out / "2_album_after.png"))

# ---- Whole LP in one take: side A, flip (with handling thumps), side B ----
from test_core import make_whole_lp

from vinyl.ui.main_window import ALL_SIDES

win.preview = Release(104, "Sylvester", "Step II", "1978", [], [], "Fantasy", "F-9556",
                      [Track("A1", "Dance (Disco Heat)", 31, "", 1), Track("A2", "Grateful", 21, "", 2),
                       Track("A3", "I Took My Strength From You", 26, "", 3),
                       Track("B1", "You Make Me Feel (Mighty Real)", 36, "", 4), Track("B2", "Was It Something", 23, "", 5)],
                      "", "")
win._use_release()
print("record picker default:", win.side_combo.currentText(), "| file:", win._recording_path(ALL_SIDES).name)
lp = out / win._recording_path(ALL_SIDES).name
make_whole_lp(lp, [30, 20, 25], [35, 22])
rec = win._add_recording(lp, ALL_SIDES)
assert pump(10, lambda: rec.env is not None)
win.tabs.setCurrentIndex(TAB_SPLIT)
pump(0.3)
rows = [(win.track_table.item(r, 0).text(), win.track_table.item(r, 1).text()[:30], win.track_table.item(r, 2).text())
        for r in range(win.track_table.rowCount())]
print("whole LP rows:", rows)
print("skips:", rec.skips, "| status:", repr(win.statusBar().currentMessage()))
win.grab().save(str(out / "4_whole_lp.png"))

# Zoom to B1's start (row 4) and check real samples are drawn
win.track_table.selectRow(4)
win._zoom_selected()
pump(0.4)
print("zoomed view: %.2f–%.2f s (span %.1f), detail loaded: %s, scrollbar visible: %s" % (
    win.wave.view_start, win.wave.view_end, win.wave.span, win.wave._detail is not None, win.wave_scroll.isVisible()))
win.grab().save(str(out / "5_zoomed.png"))

# Own splits: add one at the cursor, then remove it again; toggle a part as not-a-track
n = len(rec.edges)
win.wave.cursor = (win.wave.view_start + win.wave.view_end) / 2
win._split_at_cursor()
print("after split at cursor:", len(rec.edges) - n, "new split; pieces vs tracks:", repr(win.statusBar().currentMessage()))
win.wave.remove_split(rec.edges.index(min(rec.edges, key=lambda e: abs(e - win.wave.cursor))))
print("after removing it:", len(rec.edges) == n, "skips:", rec.skips)
win.wave.fit()

# Space bar and double-click playback (player stubbed so nothing comes out of the speakers)
from PySide6.QtCore import Qt as _Qt
from PySide6.QtTest import QTest

played = []
fake = {"playing": False}
win.player.play = lambda path, start, stop_at=None: (played.append(round(start, 3)), fake.update(playing=True))
win.player.stop = lambda: fake.update(playing=False)
type(win.player).is_playing = property(lambda self: fake["playing"])
win.wave.cursor = 12.5
win.activateWindow()
win.wave.setFocus()
QTest.keyClick(win.wave, _Qt.Key.Key_Space)
pump(0.1)
print("space #1 -> played from", played, "| playing:", fake["playing"])
QTest.keyClick(win.wave, _Qt.Key.Key_Space)
pump(0.1)
print("space #2 -> playing:", fake["playing"])
tracks_rows = [r for r in range(win.track_table.rowCount()) if win.track_table.item(r, 0).text() == "B1"]
win.track_table.selectRow(tracks_rows[0])
win._play_selected()
print("double-click B1 -> played from", played[-1], "| B1 starts at", win.track_table.item(tracks_rows[0], 2).text())
win.wave.cursor = played[-1]
win.wave.zoom_to(played[-1], 3.0)
pump(0.4)
win.grab().save(str(out / "6_cursor.png"))
win.wave.fit()

# Save the whole LP: 5 tracks, gaps and flip not saved, both sides logged
dialog.clear()
win.format_combo.setCurrentIndex(win.format_combo.findData("flac"))
win._save_all()
assert pump(30, lambda: win.save_btn.isEnabled() and dialog), "save never finished"
lp_files = sorted(p.name for p in (out / "music" / "Sylvester").rglob("*.flac"))
print("whole LP saved:", lp_files)
print("rip log:", win.riplog.describe(104))
# ---- A white label recorded B side first: side B (2 tracks), flip, side A (3 tracks) ----
win.preview = Release(106, "Unknown Artist", "White Label", "1995", [], [], "", "",
                      [Track("A1", "Alpha", 31, "", 1), Track("A2", "Beta", 21, "", 2), Track("A3", "Gamma", 26, "", 3),
                       Track("B1", "Delta", 36, "", 4), Track("B2", "Epsilon", 23, "", 5)], "", "")
win._use_release()
wl = out / win._recording_path(ALL_SIDES).name
make_whole_lp(wl, [35, 22], [30, 20, 25])  # played B first
rec = win._add_recording(wl, ALL_SIDES)
assert pump(10, lambda: rec.env is not None)
win.tabs.setCurrentIndex(TAB_SPLIT)
pump(0.3)


def table():
    return [(win.track_table.item(r, 0).text(), win.track_table.item(r, 2).text())
            for r in range(win.track_table.rowCount()) if win.track_table.item(r, 0).text() != "—"]


print("B-first take, default A → B:", table())
win.order_combo.setCurrentIndex(1)  # B → A
pump(0.3)
print("after choosing", repr(win.order_combo.currentText()) + ":", table())
win.grab().save(str(out / "7_b_first.png"))

# Pick a track by hand in the table: say the first part is really A3; the next parts follow on from it
win._pick_track(0, 3)
pump(0.2)
print("first part picked as A3:", table(), "| undo enabled:", win.reset_tracks_btn.isEnabled())
win._reset_track_picks()
pump(0.2)
print("after undo:", table())

# ---- Discogs is wrong: it says A / B1 / B2, the record really is A1 A2 | B1 ----
import vinyl.ui.main_window as mw
from vinyl.ui.tracklist_editor import TracklistEditor

QMessageBox.question = staticmethod(lambda *a, **k: QMessageBox.StandardButton.Yes)  # "switch album?" etc.
wrong = Release(107, "DJ Promo", "Test Pressing", "1992", [], [], "Lbl", "P1",
                [Track("A", "First", 30, "", 1), Track("B1", "Second", 20, "", 2), Track("B2", "Third", 35, "", 3)],
                "", "https://www.discogs.com/release/107")
win._release_cache[107] = wrong
win.preview = wrong
win._use_release()
tp = out / win._recording_path(ALL_SIDES).name
make_whole_lp(tp, [30, 20], [35])  # really: A1 A2, flip, B1
rec = win._add_recording(tp, ALL_SIDES)
assert pump(10, lambda: rec.env is not None)
win.tabs.setCurrentIndex(TAB_SPLIT)
pump(0.3)
print("Discogs tracklist (wrong):", table())

# The real editor dialog: retype the positions as they really are
dlg = TracklistEditor(win.release, wrong, win)
for r, pos in enumerate(["A1", "A2", "B1"]):
    dlg.table.item(r, 0).setText(pos)
dlg.show()
pump(0.2)
dlg.grab().save(str(out / "8_editor.png"))
dlg.accept()
fixed = dlg.result_release()


class FakeEditor:  # stands in for the dialog so the window's own handling runs
    def __init__(self, rel, original, parent):
        self.rel = rel

    def exec(self):
        return True

    def result_release(self):
        return fixed if self.rel else mw_new


mw.TracklistEditor = FakeEditor
win._edit_tracklist(win.release)
pump(0.3)
print("after fixing positions:", table(), "| sides:", win.release.sides,
      "| saved correction:", win.edits.has(107))
win.grab().save(str(out / "9_fixed.png"))

# A white label that isn't on Discogs at all
mw_new = Release(-1, "Unknown", "White Label", "", [], [], "", "",
                 [Track("A", "Side A Track", None, "", 1), Track("B", "Side B Track", None, "", 2)], "", "")
win._edit_tracklist(None)
pump(0.3)
print("entered by hand:", win.release.title, "| on tab:", win.tabs.currentIndex() == TAB_RECORD,
      "| record options:", [win.side_combo.itemText(i) for i in range(win.side_combo.count())])
win.recorder.close()
win.close()
