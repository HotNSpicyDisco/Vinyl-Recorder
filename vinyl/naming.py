"""Where saved tracks go and what they're called, from a pattern like "{artist}/{album} ({year})/{number} - {title}".

"/" makes folders; the last part is the file name. Fields that are empty for a record drop out
cleanly, so "{album} ({year})" becomes just the album when there's no year.
"""

import re
import string
from pathlib import Path

from .discogs import Release, Track

DEFAULT_PATTERN = "{artist}/{album} ({year})/{number} - {title}"
PRESETS = [
    ("Artist / Album (Year) / 01 - Title", DEFAULT_PATTERN),
    ("Artist / Album (Year) / A1 - Title  (vinyl positions)", "{artist}/{album} ({year})/{position} - {title}"),
    ("Artist - Album (Year) / 01 - Title  (one folder per album)", "{artist} - {album} ({year})/{number} - {title}"),
    ("Label / Cat# - Artist - Album / 01 - Title", "{label}/{catno} - {artist} - {album}/{number} - {title}"),
    ("Genre / Artist / Album / 01 - Title (BPM)", "{style}/{artist}/{album}/{number} - {title} ({bpm})"),
]
FIELDS = {
    "artist": "album artist", "album": "album title", "year": "year", "label": "record label",
    "catno": "catalogue number", "number": "track number (01)", "position": "vinyl position (A1)",
    "title": "track title", "track_artist": "track artist (compilations)", "bpm": "BPM", "genre": "genre",
    "style": "style",
}


class _Blank(dict):
    def __missing__(self, key):
        return ""


def check(pattern: str) -> str | None:
    """A problem with the pattern, or None if it's fine."""
    try:
        names = [f for _, f, _, _ in string.Formatter().parse(pattern) if f is not None]
    except ValueError as e:
        return f"Brackets don't match: {e}"
    unknown = [n for n in names if n not in FIELDS]
    if unknown:
        return f"Unknown field: {{{unknown[0]}}}"
    last = pattern.replace("\\", "/").split("/")[-1]
    if "title" not in last and "number" not in last and "position" not in last:
        return "The file name (after the last /) needs {title}, {number} or {position}, or tracks overwrite each other."
    return None


def _clean(part: str) -> str:
    part = re.sub(r"\(\s*\)|\[\s*\]", "", part)  # "Album ()" when there's no year
    part = re.sub(r"\s+", " ", part)
    part = re.sub(r"^[\s\-–_.,]+|[\s\-–_,]+$", "", part)  # " - Title" when a leading field is empty
    part = re.sub(r"(\s[-–]\s)(\s*[-–]\s)+", r"\1", part)  # "A -  - B"
    part = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", part).strip().rstrip(".")
    return part[:150]


def values(release: Release, track: Track, bpm: float | None = None) -> dict[str, str]:
    width = max(2, len(str(len(release.tracks))))
    return _Blank(
        artist=release.artist, album=release.title, year=release.year or "", label=release.label or "",
        catno=release.catno or "", number=f"{track.number:0{width}d}", position=track.position,
        title=track.title, track_artist=track.artist or release.artist, bpm=str(int(round(bpm))) if bpm else "",
        genre=(release.genres or [""])[0], style=(release.styles or release.genres or [""])[0],
    )


def track_path(output_dir: Path, release: Release, track: Track, ext: str, pattern: str = DEFAULT_PATTERN,
               bpm: float | None = None, mp3_subfolder: bool = False) -> Path:
    """The file for a track. mp3_subfolder: when saving FLAC and MP3, the MP3s go in "<folder> [MP3]"."""
    parts = [_clean(p.format_map(values(release, track, bpm))) for p in pattern.replace("\\", "/").split("/")]
    *folders, stem = parts
    folder = output_dir.joinpath(*[f for f in folders if f])
    if mp3_subfolder:
        folder = folder.with_name(folder.name + " [MP3]") if folder != output_dir else folder / "MP3"
    stem = stem or _clean(f"{values(release, track)['number']} - {track.title}")
    return folder / f"{stem}.{ext}"


def album_folders(output_dir: Path, release: Release, pattern: str = DEFAULT_PATTERN) -> list[Path]:
    """Every folder this release's tracks can be in (both formats), for finding saved files."""
    dirs = []
    for t in release.tracks or [Track("A1", release.title, None, "", 1)]:
        for mp3 in (False, True):
            d = track_path(output_dir, release, t, "flac", pattern, mp3_subfolder=mp3).parent
            if d not in dirs:
                dirs.append(d)
    return dirs


def example(pattern: str) -> str:
    rel = Release(1, "Chic", "Risqué", "1979", ["Funk / Soul"], ["Disco"], "Atlantic", "SD 16003",
                  [Track("A1", "Good Times", 488, "", 1)], "", "")
    p = track_path(Path("."), rel, rel.tracks[0], "flac", pattern, bpm=111)
    return p.as_posix().removeprefix("./")
