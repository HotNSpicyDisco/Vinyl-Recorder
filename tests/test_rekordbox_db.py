"""Rekordbox library matching, keys on labels, and copying keys into saved tracks (no Rekordbox needed)."""

from pathlib import Path

import numpy as np
import soundfile as sf

from vinyl import export, labels
from vinyl.discogs import Release, Track
from vinyl.export import TrackCut, export_side
from vinyl.rekordbox_db import Library, RbTrack, key_text

LIB = Library([
    RbTrack("Lambrusco", "Pontchartrain", "Date Night EP", 112.99, "Dm", 382, Path("D:/Music/P/01 - Lambrusco.mp3")),
    RbTrack("Ayeeoh", "Pontchartrain", "Date Night EP", 105.99, "G", 423, Path("D:/Music/P/02 - Ayeeoh.mp3")),
    RbTrack("Nice 'n Slow", "Pontchartrain", "Date Night EP", 117.8, "Cm", 445, Path("D:/Music/P/03.mp3")),
    RbTrack("Afterlife", "Pontchartrain, Blair French", "Afterlife + Pool", 97.97, "F#m", 281, Path("D:/a.mp3")),
    RbTrack("Lambrusco", "Someone Else", "Other", 90.0, "Am", 300, Path("D:/other.mp3")),
    RbTrack("Gone Home (Original Mix)", "Pontchartrain", "All-Stars", 118.04, "C", 445, Path("D:/g.mp3")),
])


def test_find_same_track():
    assert LIB.find("Pontchartrain", "Lambrusco").key == "Dm"
    assert LIB.find("PONTCHARTRAIN", "nice n slow").bpm == 117.8  # case and punctuation don't matter
    assert LIB.find("Pontchartrain / Blair French", "Afterlife").key == "F#m"  # split credits
    assert LIB.find("Pontchartrain", "Gone Home").key == "C"  # "(Original Mix)" is the same track
    assert LIB.find("Nobody", "Lambrusco", album="Date Night EP").key == "Dm"  # same album
    assert LIB.find("Pontchartrain", "Not On It") is None
    assert LIB.find("Blair", "Afterlife") is None  # only whole names count


def test_key_notations():
    assert key_text("Dm") == "Dm" and key_text("Dm", "camelot") == "7A"
    assert key_text("Dbm", "camelot") == "12A" and key_text("F#", "camelot") == "2B"
    assert key_text("", "camelot") == ""


def test_label_filled_from_rekordbox():
    rel = Release(10088118, "Pontchartrain", "Date Night EP", "2017", [], ["Disco"], "Whiskey Disco", "WD51",
                  [Track("A1", "Lambrusco", None, "", 1), Track("A2", "Ayeeoh", None, "", 2),
                   Track("B1", "Nice 'n Slow", None, "", 3), Track("B2", "Unreleased", None, "", 4)], "", "")
    d = labels.label_data(rel, bpms={2: 104.0})  # your own measured BPM stays
    assert labels.fill_from_rekordbox(d, LIB) == 3
    assert [(t.bpm, t.key, t.length) for t in d.tracks] == [
        ("113", "Dm", "6:22"), ("104", "G", "7:03"), ("118", "Cm", "7:25"), ("", "", "")]
    assert labels.fill_from_rekordbox(d, LIB) == 0  # nothing more to add


def test_key_column_on_labels():
    rel = Release(1, "Pontchartrain", "Date Night EP", "2017", [], [], "", "",
                  [Track("A1", "Lambrusco", 382, "", 1)], "", "")
    d = labels.label_data(rel, keys={1: "Dm"})
    tmpl = labels.TEMPLATES_BY_ID["roll_4x2"]
    _, plain = labels.place(tmpl, d)
    _, keyed = labels.place(tmpl, d, key="camelot")
    assert keyed.track_w < plain.track_w  # room made for the Key box
    assert labels.from_dict(labels.to_dict(d)).tracks[0].key == "Dm"
    assert labels.from_dict({"artist": "a", "title": "b", "tracks": [{"position": "A", "name": "x"}]}).tracks[0].key == ""
    assert not labels.preview(tmpl, d, dpi=50, key="classic").isNull()


def test_keys_copied_into_saved_tracks(tmp_path):
    wav = tmp_path / "side.wav"
    sf.write(wav, (0.2 * np.random.default_rng(0).standard_normal((44100 * 3, 2))).astype(np.float32), 44100)
    rel = Release(5, "Pontchartrain", "Date Night EP", "2017", [], [], "", "",
                  [Track("A1", "Lambrusco", 3, "", 1)], "", "")
    out = tmp_path / "out"
    files = []
    for fmt in ("mp3", "flac", "wav"):
        saved = export_side(wav, [TrackCut(rel.tracks[0], 0, 2.5)], rel, out / fmt, fmt=fmt, detect_bpm=False)
        files.append(saved[0].files[0])
    lib = Library([RbTrack("Lambrusco", "Pontchartrain", "", 113.0, "Dm", 2.5, f) for f in files])
    assert export.copy_rekordbox_keys(out, lib) == 3
    assert [export.read_key(f) for f in files] == ["Dm"] * 3
    assert export.read_saved(out / "mp3", rel)[0] == {1: 113.0}  # BPM filled in where there was none
    assert export.read_saved_keys(out / "flac", rel) == {1: "Dm"}
    assert export.copy_rekordbox_keys(out, lib) == 0  # already there
