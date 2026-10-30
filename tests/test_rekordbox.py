"""The rekordbox XML library: tracks, playlists, file locations, updating without duplicates."""

import xml.etree.ElementTree as ET

import numpy as np
import soundfile as sf

from vinyl import rekordbox
from vinyl.discogs import Release, Track
from vinyl.export import SavedTrack, TrackCut, album_folder, export_side

REL = Release(42, "Chic & Friends", "Risqué: Remixes", "1979", ["Funk / Soul"], ["Disco"], "Atlantic", "SD 1",
              [Track("A1", "Good Times", 10, "", 1), Track("A2", "My Feet Keep Dancing", 10, "Guest", 2)], "", "")


def save_album(tmp_path, fmt="both"):
    wav = tmp_path / "side.wav"
    sf.write(wav, np.zeros((44100 * 22, 2), np.float32), 44100)
    cuts = [TrackCut(REL.tracks[0], 0, 9.5), TrackCut(REL.tracks[1], 11, 21)]
    out = tmp_path / "Vinyl Rips"
    saved: list[SavedTrack] = export_side(wav, cuts, REL, out, fmt=fmt, detect_bpm=False)
    # pretend BPM detection found these
    from mutagen.flac import FLAC
    from mutagen.id3 import ID3, TBPM
    for s, bpm in zip(saved, ("118", "124")):
        for f in s.files:
            if f.suffix == ".flac":
                t = FLAC(f)
                t["BPM"] = bpm
                t.save()
            else:
                t = ID3(f)
                t.add(TBPM(encoding=3, text=bpm))
                t.save(v2_version=3)
    return out


def parse(path):
    root = ET.parse(path).getroot()
    tracks = {t.get("TrackID"): t for t in root.find("COLLECTION")}
    folder = next(n for n in root.find("PLAYLISTS/NODE") if n.get("Name") == "Vinyl Recorder")
    return root, tracks, folder


def test_album_becomes_a_playlist(tmp_path):
    out = save_album(tmp_path)
    xml = out / "rekordbox.xml"
    folders = [album_folder(out, REL), album_folder(out, REL, mp3_subfolder=True)]
    assert rekordbox.add_folders(xml, folders, prefer="flac") == 1
    root, tracks, folder = parse(xml)
    assert root.find("COLLECTION").get("Entries") == "2"
    playlist = folder.find("NODE")
    assert playlist.get("Name") == "Chic & Friends – Risqué: Remixes" and playlist.get("Entries") == "2"
    first = tracks[playlist.findall("TRACK")[0].get("Key")]
    assert first.get("Name") == "Good Times" and first.get("AverageBpm") == "118.00"
    assert first.get("Kind") == "FLAC File" and first.get("Album") == "Risqué: Remixes"
    assert first.get("Year") == "1979" and first.get("Label") == "Atlantic" and first.get("Genre") == "Disco"
    assert first.get("TotalTime") == "10" and first.get("Comments") == "Vinyl A1"
    second = tracks[playlist.findall("TRACK")[1].get("Key")]
    assert second.get("Artist") == "Guest"
    # The location is a proper file URL that leads back to the file
    loc = first.get("Location")
    assert loc.startswith("file://localhost/") and " " not in loc
    assert rekordbox.path_from_location(loc).resolve() == (folders[0] / "01 - Good Times.flac").resolve()


def test_mp3_preferred_and_no_duplicates(tmp_path):
    out = save_album(tmp_path)
    xml = out / "rekordbox.xml"
    folders = rekordbox.all_album_folders(out)
    assert len(folders) == 2  # the FLAC and [MP3] folders; raw recordings would be skipped
    rekordbox.add_folders(xml, folders, prefer="mp3")
    rekordbox.add_folders(xml, folders, prefer="mp3")  # again: updates, doesn't duplicate
    root, tracks, folder = parse(xml)
    assert len(tracks) == 2 and len(folder.findall("NODE")) == 1
    assert all(t.get("Kind") == "MP3 File" for t in tracks.values())


def test_deleted_files_are_dropped(tmp_path):
    out = save_album(tmp_path, fmt="flac")
    xml = out / "rekordbox.xml"
    folder_path = album_folder(out, REL)
    rekordbox.add_folders(xml, [folder_path])
    (folder_path / "02 - My Feet Keep Dancing.flac").unlink()
    rekordbox.add_folders(xml, [folder_path])
    _, tracks, folder = parse(xml)
    assert len(tracks) == 1 and folder.find("NODE").get("Entries") == "1"


def test_other_playlists_in_the_file_are_left_alone(tmp_path):
    out = save_album(tmp_path, fmt="flac")
    xml = out / "rekordbox.xml"
    xml.write_text('<?xml version="1.0" encoding="UTF-8"?><DJ_PLAYLISTS Version="1.0.0"><COLLECTION Entries="0"/>'
                   '<PLAYLISTS><NODE Type="0" Name="ROOT" Count="1"><NODE Name="My set" Type="1" KeyType="0" '
                   'Entries="0"/></NODE></PLAYLISTS></DJ_PLAYLISTS>', encoding="utf-8")
    rekordbox.add_folders(xml, [album_folder(out, REL)])
    names = [n.get("Name") for n in ET.parse(xml).getroot().find("PLAYLISTS/NODE")]
    assert names == ["My set", "Vinyl Recorder"]
