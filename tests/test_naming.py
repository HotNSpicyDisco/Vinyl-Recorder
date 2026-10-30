"""File and folder naming patterns."""

from pathlib import Path

import numpy as np
import soundfile as sf

from vinyl import naming
from vinyl.discogs import Release, Track
from vinyl.export import TrackCut, album_folder, export_side, read_saved

REL = Release(1, "Chic", "Risqué", "1979", ["Funk / Soul"], ["Disco"], "Atlantic", "SD 16003",
              [Track("A1", "Good Times", 488, "", 1), Track("B2", "My/Your Feet", 300, "Guest", 12)], "", "")
OUT = Path("out")


def rel_path(track, pattern, **kw):
    return naming.track_path(OUT, REL, track, "flac", pattern, **kw).relative_to(OUT).as_posix()


def test_default_matches_the_original_layout():
    assert rel_path(REL.tracks[0], naming.DEFAULT_PATTERN) == "Chic/Risqué (1979)/01 - Good Times.flac"
    assert naming.track_path(OUT, REL, REL.tracks[0], "mp3", mp3_subfolder=True).relative_to(OUT).as_posix() \
        == "Chic/Risqué (1979) [MP3]/01 - Good Times.mp3"


def test_presets():
    t = REL.tracks[0]
    got = [rel_path(t, p, bpm=111.4) for _, p in naming.PRESETS]
    assert got == [
        "Chic/Risqué (1979)/01 - Good Times.flac",
        "Chic/Risqué (1979)/A1 - Good Times.flac",
        "Chic - Risqué (1979)/01 - Good Times.flac",
        "Atlantic/SD 16003 - Chic - Risqué/01 - Good Times.flac",
        "Disco/Chic/Risqué/01 - Good Times (111).flac",
    ]


def test_empty_fields_drop_out_and_slashes_stay_in_names():
    bare = Release(2, "Unknown", "White Label", "", [], [], "", "", [Track("A", "Untitled/Dub", None, "", 1)], "", "")
    p = naming.track_path(OUT, bare, bare.tracks[0], "flac", "{label}/{catno} - {artist} - {album} ({year})/"
                          "{number} - {title} ({bpm})")
    assert p.relative_to(OUT).as_posix() == "Unknown - White Label/01 - Untitled_Dub.flac"
    assert rel_path(REL.tracks[1], "{artist}/{number} - {track_artist} - {title}") == "Chic/12 - Guest - My_Your Feet.flac"


def test_check():
    assert naming.check(naming.DEFAULT_PATTERN) is None
    assert "Unknown field" in naming.check("{artist}/{tittle}")
    assert "needs" in naming.check("{artist}/{album}")  # every track would get the same file name
    assert "Brackets" in naming.check("{artist/{title}")
    assert naming.example("{label}/{position} {title}") == "Atlantic/A1 Good Times.flac"


def test_save_and_find_with_a_custom_pattern(tmp_path):
    wav = tmp_path / "side.wav"
    sf.write(wav, np.zeros((44100 * 20, 2), np.float32), 44100)
    pattern = "{label}/{artist} - {album}/{position} {title}"
    cuts = [TrackCut(REL.tracks[0], 0, 9), TrackCut(REL.tracks[1], 10, 19)]
    saved = export_side(wav, cuts, REL, tmp_path, fmt="both", detect_bpm=False, pattern=pattern)
    assert saved[0].files[0] == tmp_path / "Atlantic" / "Chic - Risqué" / "A1 Good Times.flac"
    assert saved[0].files[1] == tmp_path / "Atlantic" / "Chic - Risqué [MP3]" / "A1 Good Times.mp3"
    assert album_folder(tmp_path, REL, pattern=pattern) == tmp_path / "Atlantic" / "Chic - Risqué"
    _, lengths = read_saved(tmp_path, REL, pattern)
    assert round(lengths[1]) == 9 and round(lengths[12]) == 9
