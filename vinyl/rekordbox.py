"""A rekordbox XML library of your rips, so they show up in Rekordbox as playlists.

One playlist per album, inside a "Vinyl Recorder" folder, with title, artist, album, label, genre,
year, length and BPM. Rekordbox works out the key and beat grid itself when it analyses the tracks.

In Rekordbox: Preferences > Advanced > Database > rekordbox xml > Imported Library: choose this
file once. The playlists then appear under "rekordbox xml" in the sidebar; right-click > Import.
"""

import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from urllib.parse import quote, unquote

from mutagen import File as MutagenFile
from mutagen import MutagenError

from . import __version__

FOLDER = "Vinyl Recorder"


@dataclass
class TrackInfo:
    path: Path
    title: str
    artist: str
    album: str
    album_artist: str
    number: int
    year: str
    genre: str
    label: str
    bpm: float | None
    seconds: float
    samplerate: int
    bitrate: int  # kbps
    position: str  # vinyl position, e.g. A1


def location(path: Path) -> str:
    """The file URL form Rekordbox uses: file://localhost/C:/Music/A%20B.flac"""
    p = path.resolve().as_posix()
    if not p.startswith("/"):
        p = "/" + p
    return "file://localhost" + quote(p, safe="/:()!'*,;=+$&@~")


def path_from_location(loc: str) -> Path | None:
    """The file a Rekordbox location points to (None if it isn't a local file URL)."""
    prefix = "file://localhost"
    if not loc.startswith(prefix):
        return None
    p = unquote(loc[len(prefix):])
    if re.match(r"^/[A-Za-z]:/", p):  # Windows: /C:/Music/... -> C:/Music/...
        p = p[1:]
    return Path(p)


def _first(tags, *keys) -> str:
    for k in keys:
        if k in tags:
            v = tags[k]
            v = v.text if hasattr(v, "text") else v
            if isinstance(v, list):
                v = v[0] if v else ""
            if str(v).strip():
                return str(v).strip()
    return ""


def read_track(path: Path) -> TrackInfo | None:
    """Tags and length of one saved FLAC/MP3 (as written by the save step)."""
    try:
        f = MutagenFile(path)
    except (MutagenError, OSError):
        return None
    if f is None or f.tags is None:
        return None
    t = f.tags
    num = _first(t, "TRACKNUMBER", "TRCK").split("/")[0]
    bpm = _first(t, "BPM", "TBPM")
    try:
        bpm_value = float(bpm) if bpm else None
    except ValueError:
        bpm_value = None
    return TrackInfo(
        path=path,
        title=_first(t, "TITLE", "TIT2") or path.stem,
        artist=_first(t, "ARTIST", "TPE1"),
        album=_first(t, "ALBUM", "TALB"),
        album_artist=_first(t, "ALBUMARTIST", "TPE2"),
        number=int(num) if num.isdigit() else 0,
        year=_first(t, "DATE", "TDRC")[:4],
        genre=_first(t, "STYLE", "GENRE", "TXXX:STYLE", "TCON"),
        label=_first(t, "LABEL", "TPUB"),
        bpm=bpm_value,
        seconds=f.info.length,
        samplerate=getattr(f.info, "sample_rate", 0),
        bitrate=int(getattr(f.info, "bitrate", 0) / 1000),
        position=_first(t, "VINYL_POSITION", "TXXX:VINYL_POSITION"),
    )


def album_tracks(folder: Path) -> list[TrackInfo]:
    tracks = [read_track(p) for p in sorted(folder.iterdir()) if p.suffix.lower() in (".flac", ".mp3")]
    return sorted((t for t in tracks if t), key=lambda t: (t.number, t.path.name))


class Library:
    """Reads, updates and writes the XML file. Only playlists in the "Vinyl Recorder" folder are touched."""

    def __init__(self, path: Path):
        self.path = path
        if path.exists():
            self.root = ET.parse(path).getroot()
        else:
            self.root = ET.Element("DJ_PLAYLISTS", Version="1.0.0")
            ET.SubElement(self.root, "PRODUCT", Name="Vinyl Recorder", Version=__version__, Company="")
            ET.SubElement(self.root, "COLLECTION", Entries="0")
            playlists = ET.SubElement(self.root, "PLAYLISTS")
            ET.SubElement(playlists, "NODE", Type="0", Name="ROOT", Count="0")
        self.collection = self.root.find("COLLECTION")
        self.top = self.root.find("PLAYLISTS/NODE")
        self.folder = next((n for n in self.top.findall("NODE") if n.get("Name") == FOLDER and n.get("Type") == "0"),
                           None)
        if self.folder is None:
            self.folder = ET.SubElement(self.top, "NODE", Type="0", Name=FOLDER, Count="0")
        self.by_location = {t.get("Location"): t for t in self.collection.findall("TRACK")}

    def _next_id(self) -> int:
        ids = [int(t.get("TrackID", 0)) for t in self.collection.findall("TRACK") if t.get("TrackID", "").isdigit()]
        return max(ids, default=0) + 1

    def _track(self, info: TrackInfo) -> str:
        """Add or update a track in the collection; returns its TrackID."""
        loc = location(info.path)
        el = self.by_location.get(loc)
        if el is None:
            el = ET.SubElement(self.collection, "TRACK", TrackID=str(self._next_id()), Location=loc,
                               DateAdded=date.today().isoformat(), PlayCount="0", Rating="0")
            self.by_location[loc] = el
        kind = "FLAC File" if info.path.suffix.lower() == ".flac" else "MP3 File"
        attrs = {
            "Name": info.title, "Artist": info.artist, "Album": info.album, "Genre": info.genre,
            "Label": info.label, "Year": info.year, "TrackNumber": str(info.number), "Kind": kind,
            "TotalTime": str(int(round(info.seconds))), "SampleRate": str(info.samplerate),
            "BitRate": str(info.bitrate), "Size": str(info.path.stat().st_size),
            "AverageBpm": f"{info.bpm:.2f}" if info.bpm else "0.00",
            "Comments": f"Vinyl {info.position}" if info.position else "",
        }
        for k, v in attrs.items():
            el.set(k, v)
        return el.get("TrackID")

    def add_album(self, name: str, tracks: list[TrackInfo]) -> None:
        """Make (or replace) the playlist `name` holding these tracks."""
        ids = [self._track(t) for t in tracks]
        for old in [n for n in self.folder.findall("NODE") if n.get("Name") == name]:
            self.folder.remove(old)
        node = ET.SubElement(self.folder, "NODE", Name=name, Type="1", KeyType="0", Entries=str(len(ids)))
        for i in ids:
            ET.SubElement(node, "TRACK", Key=i)
        # Keep the albums sorted by name in the folder
        nodes = sorted(self.folder.findall("NODE"), key=lambda n: n.get("Name", "").lower())
        for n in nodes:
            self.folder.remove(n)
        self.folder.extend(nodes)

    def drop_missing(self) -> int:
        """Forget tracks whose files are gone (and empty playlists). Returns how many were removed."""
        gone = {t.get("TrackID") for t in self.collection.findall("TRACK")
                if (p := path_from_location(t.get("Location", ""))) is not None and not p.exists()}
        for t in [t for t in self.collection.findall("TRACK") if t.get("TrackID") in gone]:
            self.collection.remove(t)
            self.by_location.pop(t.get("Location"), None)
        for node in self.folder.findall("NODE"):
            for ref in [r for r in node.findall("TRACK") if r.get("Key") in gone]:
                node.remove(ref)
            node.set("Entries", str(len(node.findall("TRACK"))))
            if not node.findall("TRACK"):
                self.folder.remove(node)
        return len(gone)

    def save(self) -> None:
        self.collection.set("Entries", str(len(self.collection.findall("TRACK"))))
        self.folder.set("Count", str(len(self.folder.findall("NODE"))))
        self.top.set("Count", str(len(self.top.findall("NODE"))))
        ET.indent(self.root, space="  ")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        ET.ElementTree(self.root).write(tmp, encoding="UTF-8", xml_declaration=True)
        tmp.replace(self.path)


def playlist_name(tracks: list[TrackInfo], folder: Path) -> str:
    t = tracks[0]
    artist = t.album_artist or t.artist
    return f"{artist} – {t.album}" if t.album else folder.name


def add_folders(xml_path: Path, folders: list[Path], prefer: str = "flac") -> int:
    """Add the albums in these folders (FLAC and/or MP3) as playlists. When an album was saved as
    both, `prefer` picks which files Rekordbox gets. Returns how many albums were added."""
    lib = Library(xml_path)
    lib.drop_missing()
    albums: dict[str, list[TrackInfo]] = {}
    for folder in folders:
        tracks = album_tracks(folder) if folder.is_dir() else []
        if not tracks:
            continue
        name = playlist_name(tracks, folder)
        kind = tracks[0].path.suffix.lower().lstrip(".")
        if name in albums and kind != prefer:
            continue  # keep the preferred format's copy
        albums[name] = tracks
    for name, tracks in albums.items():
        lib.add_album(name, tracks)
    lib.save()
    return len(albums)


def all_album_folders(output_dir: Path) -> list[Path]:
    """Every folder under the output folder that holds saved tracks (raw recordings excluded)."""
    folders = set()
    for p in output_dir.rglob("*"):
        if p.suffix.lower() in (".flac", ".mp3") and "_recordings" not in p.parts:
            folders.add(p.parent)
    return sorted(folders)
