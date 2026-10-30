"""Local memory of your collection and of which records you've recorded and saved.

Both live next to the settings in %APPDATA%\\VinylRecorder.
"""

import json
from dataclasses import asdict, replace
from datetime import datetime
from pathlib import Path

from . import settings
from .discogs import CollectionItem, Release, Track

SAVED, PARTIAL = "saved", "partial"


def _dir() -> Path:
    return settings.SETTINGS_PATH.parent


def _read(path: Path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default


def _write(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=1), encoding="utf-8")
    tmp.replace(path)


class RipLog:
    """Which releases have sides recorded, and which have been saved as tracks."""

    def __init__(self, path: Path | None = None):
        self.path = path or _dir() / "rip_log.json"
        self.entries: dict[str, dict] = _read(self.path, {})

    def _entry(self, release: Release) -> dict:
        e = self.entries.setdefault(str(release.id), {"sides_recorded": [], "saved": None, "formats": []})
        e["artist"], e["title"], e["sides"] = release.artist, release.title, release.sides
        return e

    def mark_recorded(self, release: Release, side: str) -> None:
        e = self._entry(release)
        if side not in e["sides_recorded"]:
            e["sides_recorded"].append(side)
        e["last_recorded"] = datetime.now().isoformat(timespec="seconds")
        _write(self.path, self.entries)

    def mark_saved(self, release: Release, fmt: str, folder: Path, sides: list[str]) -> None:
        e = self._entry(release)
        e["sides_saved"] = sorted(set(e.get("sides_saved", [])) | set(sides))
        e["saved"] = datetime.now().isoformat(timespec="seconds")
        e["formats"] = sorted(set(e.get("formats", [])) | {fmt})
        e["folder"] = str(folder)
        _write(self.path, self.entries)

    def forget(self, release_id: int) -> None:
        self.entries.pop(str(release_id), None)
        _write(self.path, self.entries)

    @staticmethod
    def _missing(e: dict) -> list[str]:
        return [s for s in e.get("sides", []) if s not in e.get("sides_saved", [])]

    def status(self, release_id: int) -> str | None:
        """SAVED once every side has been saved; PARTIAL if anything was recorded or saved."""
        e = self.entries.get(str(release_id))
        if not e:
            return None
        if e.get("saved") and not self._missing(e):
            return SAVED
        return PARTIAL if e.get("sides_recorded") or e.get("saved") else None

    def short(self, release_id: int) -> str:
        """A few words for list rows: 'saved', 'A saved · B to do', 'A recorded'."""
        e = self.entries.get(str(release_id))
        if not e:
            return ""
        if e.get("saved"):
            missing = self._missing(e)
            return f"{', '.join(e['sides_saved'])} saved · {', '.join(missing)} to do" if missing else "saved"
        return f"{', '.join(e['sides_recorded'])} recorded" if e.get("sides_recorded") else ""

    def describe(self, release_id: int) -> str:
        e = self.entries.get(str(release_id))
        if not e:
            return ""
        if e.get("saved"):
            missing = self._missing(e)
            when = e["saved"][:10]
            if not missing:
                return f"Saved {when} ({', '.join(f.upper() for f in e.get('formats', []))})"
            return f"Side {', '.join(e['sides_saved'])} saved {when} · side {', '.join(missing)} still to do"
        if e.get("sides_recorded"):
            return f"Side {', '.join(e['sides_recorded'])} recorded, not saved yet"
        return ""


class CollectionCache:
    """Your Discogs collection as last downloaded, so the list shows up instantly."""

    def __init__(self, path: Path | None = None):
        self.path = path or _dir() / "collection.json"
        data = _read(self.path, {})
        self.username: str = data.get("username", "")
        self.updated: str = data.get("updated", "")
        self.items = [CollectionItem(**i) for i in data.get("items", [])]

    def save(self, username: str, items: list[CollectionItem]) -> None:
        self.username, self.items = username, items
        self.updated = datetime.now().isoformat(timespec="minutes")
        _write(self.path, {"username": username, "updated": self.updated, "items": [asdict(i) for i in items]})


class ReleaseEdits:
    """Your corrections to Discogs tracklists (or records you entered yourself), kept per release so a
    wrong or missing Discogs entry only needs fixing once."""

    FIELDS = ("artist", "title", "year", "label", "catno")

    def __init__(self, path: Path | None = None):
        self.path = path or _dir() / "tracklist_edits.json"
        self.entries: dict[str, dict] = _read(self.path, {})

    def has(self, release_id: int) -> bool:
        return str(release_id) in self.entries

    def apply(self, release: Release) -> Release:
        """The release with your saved corrections, if any (cover art and Discogs links are kept)."""
        e = self.entries.get(str(release.id))
        if not e:
            return release
        tracks = [Track(t["position"], t["title"], t.get("duration"), t.get("artist", ""), n)
                  for n, t in enumerate(e["tracks"], 1)]
        return replace(release, tracks=tracks, **{k: e[k] for k in self.FIELDS if k in e})

    def save(self, release: Release) -> None:
        e = {k: getattr(release, k) for k in self.FIELDS}
        e["tracks"] = [{"position": t.position, "title": t.title, "duration": t.duration, "artist": t.artist}
                       for t in release.tracks]
        e["saved"] = datetime.now().isoformat(timespec="seconds")
        self.entries[str(release.id)] = e
        _write(self.path, self.entries)

    def forget(self, release_id: int) -> None:
        self.entries.pop(str(release_id), None)
        _write(self.path, self.entries)


class LabelQueue:
    """Records waiting to have their labels printed, so a whole sheet can be filled at once.
    Each entry is the label's contents (see vinyl.labels.to_dict), so printing needs no Discogs lookup."""

    def __init__(self, path: Path | None = None):
        self.path = path or _dir() / "label_queue.json"
        self.items: list[dict] = _read(self.path, [])

    def _key(self, d: dict) -> tuple:
        return (d.get("release_id") or 0, d.get("artist"), d.get("title"))

    def add(self, label: dict) -> None:
        """Add a record's label; one already queued for the same record is replaced (keeping its place)."""
        for i, d in enumerate(self.items):
            if self._key(d) == self._key(label):
                self.items[i] = label
                break
        else:
            self.items.append(label)
        _write(self.path, self.items)

    def remove(self, indexes: list[int]) -> None:
        self.items = [d for i, d in enumerate(self.items) if i not in set(indexes)]
        _write(self.path, self.items)

    def move(self, index: int, step: int) -> int:
        """Move an entry up (-1) or down (+1); returns where it ended up."""
        to = index + step
        if not (0 <= index < len(self.items) and 0 <= to < len(self.items)):
            return index
        self.items.insert(to, self.items.pop(index))
        _write(self.path, self.items)
        return to

    def save(self) -> None:
        _write(self.path, self.items)

    def has(self, label: dict) -> bool:
        return any(self._key(d) == self._key(label) for d in self.items)

    def clear(self) -> None:
        self.items = []
        _write(self.path, self.items)


def thumb_path(release_id: int) -> Path:
    return _dir() / "thumbs" / f"{release_id}.jpg"
