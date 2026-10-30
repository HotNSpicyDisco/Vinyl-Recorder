"""Reading your Rekordbox 6/7 library: the BPM, key and length Rekordbox found for each track.

Rekordbox keeps its library in master.db, an encrypted SQLite database. A copy is read, never the
file itself, so nothing can disturb Rekordbox (it can stay open). Used to fill in labels for records
whose tracks you have in Rekordbox, and to write Rekordbox's key into the tracks this app saved.
"""

import os
import re
import shutil
import sys
import tempfile
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path

# The key Rekordbox 6 and 7 encrypt master.db with; the same on every computer (open-source tools
# that read the library use it too).
DB_KEY = "402fd482c38817c35ffa8ffb8c7d93143b749e7d315df7a81732a1ff43608497"

# Classic key names (as Rekordbox stores them) -> Camelot / Rekordbox "alphanumeric"
CAMELOT = {
    "Abm": "1A", "G#m": "1A", "B": "1B", "Cb": "1B",
    "Ebm": "2A", "D#m": "2A", "F#": "2B", "Gb": "2B",
    "Bbm": "3A", "A#m": "3A", "Db": "3B", "C#": "3B",
    "Fm": "4A", "Ab": "4B", "G#": "4B",
    "Cm": "5A", "Eb": "5B", "D#": "5B",
    "Gm": "6A", "Bb": "6B", "A#": "6B",
    "Dm": "7A", "F": "7B",
    "Am": "8A", "C": "8B",
    "Em": "9A", "G": "9B",
    "Bm": "10A", "D": "10B",
    "F#m": "11A", "Gbm": "11A", "A": "11B",
    "C#m": "12A", "Dbm": "12A", "E": "12B",
}
NOTATIONS = {"classic": "Classic (Dm)", "camelot": "Camelot (7A)"}


def key_text(key: str, notation: str = "classic") -> str:
    """A key as the label shows it."""
    key = (key or "").strip()
    if notation == "camelot":
        return CAMELOT.get(key, key)
    return key


def db_path() -> Path | None:
    """Where Rekordbox keeps its library on this computer (None if it isn't there)."""
    if sys.platform == "win32":
        base = Path(os.environ.get("APPDATA", Path.home() / "AppData" / "Roaming"))
    elif sys.platform == "darwin":
        base = Path.home() / "Library"
    else:
        return None
    path = base / "Pioneer" / "rekordbox" / "master.db"
    return path if path.exists() else None


@dataclass
class RbTrack:
    title: str
    artist: str
    album: str
    bpm: float | None
    key: str  # classic notation, e.g. "Dm" ("" if not analysed)
    seconds: float
    path: Path


def _fold(s: str) -> str:
    """Lowercase, no accents or punctuation: 'Nice 'n Slow' -> 'nice n slow'."""
    s = unicodedata.normalize("NFKD", s or "").encode("ascii", "ignore").decode().lower().replace("&", " and ")
    return " ".join(re.sub(r"[^a-z0-9]+", " ", s).split())


def _title(s: str) -> str:
    """A track title for matching ('(Original Mix)' is the same track)."""
    return _fold(re.sub(r"\s*[\(\[]original( mix)?[\)\]]\s*$", "", s or "", flags=re.I))


def _names(artist: str) -> list[str]:
    """'Pontchartrain / Blair French feat. X' -> ['pontchartrain', 'blair french', 'x']"""
    parts = re.split(r"\s*(?:/|,|&|\+| and | feat\.? | ft\.? | featuring | vs\.? | x )\s*", artist or "", flags=re.I)
    return [f for f in (_fold(re.sub(r"\s*\(\d+\)$", "", p)) for p in parts) if f]  # Discogs "Name (2)"


def _path_key(path: str | Path) -> str:
    return os.path.normcase(os.path.normpath(str(path)))


@dataclass
class Library:
    tracks: list[RbTrack] = field(default_factory=list)

    def __post_init__(self):
        self.by_title: dict[str, list[RbTrack]] = {}
        self.by_path: dict[str, RbTrack] = {}
        for t in self.tracks:
            self.by_title.setdefault(_title(t.title), []).append(t)
            self.by_path[_path_key(t.path)] = t

    def at(self, path: Path) -> RbTrack | None:
        """The Rekordbox entry for this exact file."""
        return self.by_path.get(_path_key(path))

    def find(self, artist: str, title: str, album: str = "", seconds: float | None = None) -> RbTrack | None:
        """The track with this title by this artist (or on this album). Prefers the same album,
        then a length close to `seconds`, then one Rekordbox has analysed."""
        names, album_f = _names(artist), _fold(album)
        found = []
        for t in self.by_title.get(_title(title), []):
            same_artist = bool(set(names) & set(_names(t.artist)))
            same_album = bool(album_f) and (album_f == _fold(t.album) or album_f in _fold(t.album))
            if same_artist or same_album:
                found.append((not same_album, abs((seconds or t.seconds) - t.seconds), not (t.bpm and t.key), t))
        return min(found, key=lambda f: f[:3])[3] if found else None


def read(path: Path | None = None) -> Library:
    """Read the library from a copy of master.db. Raises OSError if it can't be read."""
    path = path or db_path()
    if path is None:
        raise OSError("Rekordbox's library wasn't found on this computer")
    try:
        from sqlcipher3 import dbapi2 as sqlcipher
    except ImportError as e:
        raise OSError("Reading the Rekordbox library isn't available in this build") from e
    with tempfile.TemporaryDirectory() as tmp:
        copy = Path(tmp) / "master.db"
        for suffix in ("", "-wal", "-shm"):  # recent changes may still be in the -wal file
            src = path.with_name(path.name + suffix)
            if src.exists():
                shutil.copyfile(src, copy.with_name(copy.name + suffix))
        con = sqlcipher.connect(str(copy))
        try:
            con.execute(f"PRAGMA key='{DB_KEY}'")
            rows = con.execute("""
                SELECT c.Title, a.Name, al.Name, c.BPM, k.ScaleName, c.Length, c.FolderPath
                FROM djmdContent c
                LEFT JOIN djmdArtist a ON a.ID = c.ArtistID
                LEFT JOIN djmdAlbum al ON al.ID = c.AlbumID
                LEFT JOIN djmdKey k ON k.ID = c.KeyID
                WHERE IFNULL(c.rb_local_deleted, 0) = 0""").fetchall()
        except sqlcipher.DatabaseError as e:
            raise OSError(f"Couldn't read the Rekordbox library: {e}") from e
        finally:
            con.close()
    return Library([RbTrack(title or "", artist or "", album or "", bpm / 100 if bpm else None, key or "",
                            float(length or 0), Path(folder or ""))
                    for title, artist, album, bpm, key, length, folder in rows])


class Cache:
    """The library, read again only when Rekordbox has changed it."""

    def __init__(self, path: Path | None = None):
        self.path = path
        self._stamp = None
        self.library: Library | None = None
        self.error = ""

    def get(self) -> Library | None:
        path = self.path or db_path()
        if path is None:
            self.error = "Rekordbox's library wasn't found on this computer"
            return None
        stamp = tuple(p.stat().st_mtime if p.exists() else 0
                      for p in (path, path.with_name(path.name + "-wal")))
        if self.library is None or stamp != self._stamp:
            try:
                self.library, self._stamp, self.error = read(path), stamp, ""
            except OSError as e:
                self.error = str(e)
                return self.library  # the last good copy, if any
        return self.library
