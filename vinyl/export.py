"""Cut tracks out of a recorded side and save them as tagged FLAC and/or MP3 files."""

import re
from dataclasses import dataclass, field
from math import gcd
from pathlib import Path

import numpy as np
import soundfile as sf
from mutagen.flac import FLAC, Picture
from mutagen.id3 import APIC, ID3, TALB, TBPM, TCON, TDRC, TIT2, TLEN, TPE1, TPE2, TPUB, TRCK, TXXX
from mutagen import MutagenError
from mutagen.mp3 import MP3
from scipy.signal import resample_poly

from .discogs import Release, Track
from .tempo import DEFAULT_LOW_BPM, estimate_bpm

FADE_SECONDS = 0.01  # tiny fade at each cut so it doesn't click
FORMATS = {"flac": "FLAC (lossless)", "mp3": "MP3 320 kbps", "both": "FLAC + MP3"}
MP3_MAX_RATE = 48000


def safe_filename(name: str) -> str:
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", name).strip().rstrip(".")
    return name[:150] or "Untitled"


def album_name(release: Release) -> str:
    return f"{release.title} ({release.year})" if release.year else release.title


def album_folder(output_dir: Path, release: Release, mp3_subfolder: bool = False) -> Path:
    name = album_name(release) + (" [MP3]" if mp3_subfolder else "")
    return output_dir / safe_filename(release.artist) / safe_filename(name)


@dataclass
class TrackCut:
    track: Track
    start: float
    end: float


@dataclass
class SavedTrack:
    track: Track
    length: float  # seconds
    bpm: float | None
    files: list[Path] = field(default_factory=list)


def export_side(recording: Path, cuts: list[TrackCut], release: Release, output_dir: Path,
                fmt: str = "flac", bpm_low: float = DEFAULT_LOW_BPM, detect_bpm: bool = True) -> list[SavedTrack]:
    want_flac, want_mp3 = fmt in ("flac", "both"), fmt in ("mp3", "both")
    flac_dir = album_folder(output_dir, release)
    mp3_dir = album_folder(output_dir, release, mp3_subfolder=fmt == "both")
    width = max(2, len(str(len(release.tracks))))
    saved = []
    with sf.SoundFile(recording) as src:
        sr = src.samplerate
        subtype = "PCM_24" if src.subtype in ("PCM_24", "PCM_32", "FLOAT", "DOUBLE") else "PCM_16"
        for cut in cuts:
            t = cut.track
            a = max(0, int(cut.start * sr))
            b = min(src.frames, int(cut.end * sr))
            src.seek(a)
            data = src.read(b - a, dtype="float32", always_2d=True)
            _fade(data, int(FADE_SECONDS * sr))
            result = SavedTrack(t, len(data) / sr, estimate_bpm(data, sr, bpm_low) if detect_bpm else None)
            stem = f"{t.number:0{width}d} - {safe_filename(t.title)}"
            if want_flac:
                flac_dir.mkdir(parents=True, exist_ok=True)
                path = flac_dir / f"{stem}.flac"
                sf.write(path, data, sr, subtype=subtype, format="FLAC")
                tag_flac(path, t, release, result)
                result.files.append(path)
            if want_mp3:
                mp3_dir.mkdir(parents=True, exist_ok=True)
                path = mp3_dir / f"{stem}.mp3"
                write_mp3(path, data, sr)
                tag_mp3(path, t, release, result)
                result.files.append(path)
            saved.append(result)
    if release.cover:
        for d in {flac_dir if want_flac else None, mp3_dir if want_mp3 else None} - {None}:
            (d / "cover.jpg").write_bytes(release.cover)
    return saved


def _fade(data: np.ndarray, n: int) -> None:
    n = min(n, len(data) // 2)
    if n <= 0:
        return
    ramp = np.linspace(0.0, 1.0, n, dtype=np.float32)[:, None]
    data[:n] *= ramp
    data[-n:] *= ramp[::-1]


def mp3_rate(sr: int) -> int:
    """MP3 tops out at 48 kHz: 88.2/176.4 kHz -> 44.1 kHz, 96/192 kHz -> 48 kHz."""
    if sr <= MP3_MAX_RATE:
        return sr
    return 44100 if sr % 44100 == 0 else MP3_MAX_RATE


def write_mp3(path: Path, data: np.ndarray, sr: int) -> None:
    rate = mp3_rate(sr)
    if rate != sr:
        g = gcd(rate, sr)
        data = resample_poly(data, rate // g, sr // g, axis=0).astype(np.float32)
    np.clip(data, -1.0, 1.0, out=data)
    # compression_level 0 = highest bitrate, i.e. 320 kbps constant
    sf.write(path, data, rate, format="MP3", subtype="MPEG_LAYER_III", bitrate_mode="CONSTANT",
             compression_level=0.0)


def _bpm_text(bpm: float | None) -> str | None:
    return str(int(round(bpm))) if bpm else None


def tag_flac(path: Path, track: Track, release: Release, info: SavedTrack) -> None:
    f = FLAC(path)
    f["TITLE"] = track.title
    f["ARTIST"] = track.artist or release.artist
    f["ALBUMARTIST"] = release.artist
    f["ALBUM"] = release.title
    f["TRACKNUMBER"] = str(track.number)
    f["TRACKTOTAL"] = str(len(release.tracks))
    if release.year:
        f["DATE"] = release.year
    if release.genres:
        f["GENRE"] = release.genres
    if release.styles:
        f["STYLE"] = release.styles
    if release.label:
        f["LABEL"] = release.label
    if release.catno:
        f["CATALOGNUMBER"] = release.catno
    if bpm := _bpm_text(info.bpm):
        f["BPM"] = bpm
    f["LENGTH"] = str(int(round(info.length * 1000)))  # milliseconds, same as ID3 TLEN
    f["VINYL_POSITION"] = track.position
    if release.id > 0:  # records typed in by hand have no Discogs entry
        f["DISCOGS_RELEASE_ID"] = str(release.id)
    if release.cover:
        pic = Picture()
        pic.type = 3  # front cover
        pic.mime = _mime(release.cover)
        pic.data = release.cover
        f.clear_pictures()
        f.add_picture(pic)
    f.save()


def tag_mp3(path: Path, track: Track, release: Release, info: SavedTrack) -> None:
    f = MP3(path, ID3=ID3)
    if f.tags is None:
        f.add_tags()
    tags = f.tags
    tags.add(TIT2(encoding=3, text=track.title))
    tags.add(TPE1(encoding=3, text=track.artist or release.artist))
    tags.add(TPE2(encoding=3, text=release.artist))
    tags.add(TALB(encoding=3, text=release.title))
    tags.add(TRCK(encoding=3, text=f"{track.number}/{len(release.tracks)}"))
    if release.year:
        tags.add(TDRC(encoding=3, text=release.year))
    if release.genres:
        tags.add(TCON(encoding=3, text=release.genres))
    if release.label:
        tags.add(TPUB(encoding=3, text=release.label))
    if bpm := _bpm_text(info.bpm):
        tags.add(TBPM(encoding=3, text=bpm))
    tags.add(TLEN(encoding=3, text=str(int(round(info.length * 1000)))))
    for desc, value in (("CATALOGNUMBER", release.catno), ("STYLE", "; ".join(release.styles)),
                        ("VINYL_POSITION", track.position),
                        ("DISCOGS_RELEASE_ID", str(release.id) if release.id > 0 else "")):
        if value:
            tags.add(TXXX(encoding=3, desc=desc, text=value))
    if release.cover:
        tags.add(APIC(encoding=3, mime=_mime(release.cover), type=3, desc="Cover", data=release.cover))
    # ID3v2.3 is what Windows Explorer, Rekordbox and Serato read most reliably
    f.save(v2_version=3)


def read_saved(output_dir: Path, release: Release) -> tuple[dict[int, float], dict[int, float]]:
    """BPMs and lengths (by track number) from tracks already saved for this release, for labels."""
    bpms: dict[int, float] = {}
    lengths: dict[int, float] = {}
    for folder in (album_folder(output_dir, release), album_folder(output_dir, release, mp3_subfolder=True)):
        if not folder.is_dir():
            continue
        for path in sorted(folder.iterdir()):
            try:
                if path.suffix.lower() == ".flac":
                    f = FLAC(path)
                    number, bpm = f.get("TRACKNUMBER", [""])[0], f.get("BPM", [""])[0]
                elif path.suffix.lower() == ".mp3":
                    f = MP3(path, ID3=ID3)
                    tags = f.tags or {}
                    number = str(tags["TRCK"].text[0]) if "TRCK" in tags else ""
                    bpm = str(tags["TBPM"].text[0]) if "TBPM" in tags else ""
                else:
                    continue
                n = int(number.split("/")[0])
            except (ValueError, KeyError, OSError, IndexError, MutagenError):
                continue
            lengths.setdefault(n, f.info.length)
            try:
                bpms.setdefault(n, float(bpm))
            except ValueError:
                pass
    return bpms, lengths


def _mime(data: bytes) -> str:
    return "image/png" if data[:4] == b"\x89PNG" else "image/jpeg"
