"""Cut tracks out of a recorded side and save them as tagged FLAC, MP3, WAV or AIFF files."""

import re
from dataclasses import dataclass, field
from math import gcd
from pathlib import Path

import numpy as np
import soundfile as sf
from mutagen.flac import FLAC, Picture
from mutagen.id3 import APIC, ID3, TALB, TBPM, TCON, TDRC, TIT2, TKEY, TLEN, TPE1, TPE2, TPUB, TRCK, TXXX
from mutagen import File as MutagenFile
from mutagen import MutagenError
from mutagen.aiff import AIFF
from mutagen.mp3 import MP3
from mutagen.wave import WAVE
from scipy.signal import resample_poly

from . import loudness, naming
from .discogs import Release, Track
from .tempo import DEFAULT_LOW_BPM, estimate_bpm

FADE_SECONDS = 0.01  # tiny fade at each cut so it doesn't click
# What the Save button offers. CDJs: every Pioneer CDJ plays MP3 and 16-bit WAV/AIFF;
# FLAC needs a CDJ-2000NXS2, CDJ-3000 or a recent XDJ.
FORMATS = {
    "flac": "FLAC: lossless, full quality (CDJ-2000NXS2, CDJ-3000, XDJ)",
    "mp3": "MP3 320 kbps (plays on every CDJ)",
    "wav": "WAV 16-bit / 44.1 kHz: CD quality (every CDJ)",
    "aiff": "AIFF 16-bit / 44.1 kHz: CD quality (every CDJ)",
    "both": "FLAC + MP3",
}
SHORT_NAMES = {"flac": "FLAC", "mp3": "MP3", "wav": "WAV", "aiff": "AIFF", "both": "FLAC + MP3"}
EXTENSIONS = {"flac": "flac", "mp3": "mp3", "wav": "wav", "aiff": "aiff"}
AUDIO_SUFFIXES = (".flac", ".mp3", ".wav", ".aiff", ".aif")
MP3_MAX_RATE = 48000
CD_RATE = 44100


def safe_filename(name: str) -> str:
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", name).strip().rstrip(".")
    return name[:150] or "Untitled"


def album_name(release: Release) -> str:
    return f"{release.title} ({release.year})" if release.year else release.title


def album_folder(output_dir: Path, release: Release, mp3_subfolder: bool = False,
                 pattern: str = naming.DEFAULT_PATTERN) -> Path:
    """The folder the release's (first) track is saved in."""
    first = release.tracks[0] if release.tracks else Track("A1", release.title, None, "", 1)
    return naming.track_path(output_dir, release, first, "flac", pattern, mp3_subfolder=mp3_subfolder).parent


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
    peak: float = 0.0  # loudest sample as saved (linear)
    blocks: np.ndarray | None = field(default=None, repr=False)  # loudness blocks, for album ReplayGain


NORMALIZE_TARGET_DB = -1.0  # the album's loudest moment, after normalizing
MAX_NORMALIZE_DB = 20.0


def measure_peak(recording: Path, cuts: list[TrackCut]) -> float:
    """The loudest sample in the parts of a recording that will be saved."""
    top = 0.0
    with sf.SoundFile(recording) as src:
        for cut in cuts:
            src.seek(max(0, int(cut.start * src.samplerate)))
            left = max(0, int(cut.end * src.samplerate)) - src.tell()
            while left > 0:
                block = src.read(min(left, src.samplerate * 10), dtype="float32", always_2d=True)
                if not len(block):
                    break
                top = max(top, float(np.abs(block).max()))
                left -= len(block)
    return top


def normalize_gain(peak: float) -> float:
    """dB to bring an album whose loudest sample is `peak` to NORMALIZE_TARGET_DB (a pure volume change)."""
    if peak <= 0:
        return 0.0
    return float(np.clip(NORMALIZE_TARGET_DB - 20 * np.log10(peak), -MAX_NORMALIZE_DB, MAX_NORMALIZE_DB))


def write_replaygain(saved: list[SavedTrack]) -> float | None:
    """ReplayGain 2.0 track and album tags on every saved file. Returns the album gain (dB)."""
    blocks = [s.blocks for s in saved if s.blocks is not None and len(s.blocks)]
    album_gain = loudness.replaygain(loudness.integrated(np.concatenate(blocks))) if blocks else None
    album_peak = max((s.peak for s in saved), default=0.0)
    for s in saved:
        track_gain = loudness.replaygain(loudness.integrated(s.blocks)) if s.blocks is not None else None
        tags = {"REPLAYGAIN_TRACK_GAIN": track_gain, "REPLAYGAIN_ALBUM_GAIN": album_gain}
        values = {k: f"{v:+.2f} dB" for k, v in tags.items() if v is not None}
        values["REPLAYGAIN_TRACK_PEAK"] = f"{s.peak:.6f}"
        values["REPLAYGAIN_ALBUM_PEAK"] = f"{album_peak:.6f}"
        for path in s.files:
            if path.suffix == ".flac":
                f = FLAC(path)
                for k, v in values.items():
                    f[k] = v
                f.save()
            else:  # ID3: MP3, and the ID3 chunk of WAV/AIFF
                f = MutagenFile(path)
                for k, v in values.items():
                    f.tags.add(TXXX(encoding=3, desc=k, text=v))
                f.save(v2_version=3)
    return album_gain


def export_side(recording: Path, cuts: list[TrackCut], release: Release, output_dir: Path,
                fmt: str = "flac", bpm_low: float = DEFAULT_LOW_BPM, detect_bpm: bool = True,
                pattern: str = naming.DEFAULT_PATTERN, gain_db: float = 0.0) -> list[SavedTrack]:
    """Cut, save and tag the tracks. `pattern` decides folders and file names (see vinyl.naming);
    gain_db changes the volume of everything (normalizing). Call write_replaygain() on the result
    of all sides afterwards for the loudness tags."""
    want_flac, want_mp3 = fmt in ("flac", "both"), fmt in ("mp3", "both")
    cd_format = fmt if fmt in ("wav", "aiff") else None
    saved, folders = [], set()
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
            if gain_db:
                data *= np.float32(10 ** (gain_db / 20))
                np.clip(data, -1.0, 1.0, out=data)
            result = SavedTrack(t, len(data) / sr, estimate_bpm(data, sr, bpm_low) if detect_bpm else None,
                                peak=loudness.peak(data), blocks=loudness.block_powers(data, sr))
            if want_flac:
                path = naming.track_path(output_dir, release, t, "flac", pattern, result.bpm)
                path.parent.mkdir(parents=True, exist_ok=True)
                sf.write(path, data, sr, subtype=subtype, format="FLAC")
                tag_flac(path, t, release, result)
                result.files.append(path)
            if want_mp3:
                path = naming.track_path(output_dir, release, t, "mp3", pattern, result.bpm,
                                         mp3_subfolder=fmt == "both")
                path.parent.mkdir(parents=True, exist_ok=True)
                write_mp3(path, data, sr)
                tag_mp3(path, t, release, result)
                result.files.append(path)
            if cd_format:
                path = naming.track_path(output_dir, release, t, EXTENSIONS[cd_format], pattern, result.bpm)
                path.parent.mkdir(parents=True, exist_ok=True)
                write_cd_quality(path, data, sr, cd_format)
                tag_id3_file(path, t, release, result)
                result.files.append(path)
            folders |= {p.parent for p in result.files}
            saved.append(result)
    if release.cover:
        for d in folders:
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


def write_cd_quality(path: Path, data: np.ndarray, sr: int, fmt: str) -> None:
    """16-bit / 44.1 kHz WAV or AIFF, which every CDJ plays. Dithered, so quiet fades stay smooth."""
    if sr != CD_RATE:
        g = gcd(CD_RATE, sr)
        data = resample_poly(data, CD_RATE // g, sr // g, axis=0).astype(np.float32)
    lsb = 1 / 32768
    rng = np.random.default_rng(0)
    data = data + ((rng.random(data.shape) - rng.random(data.shape)) * lsb).astype(np.float32)  # TPDF dither
    np.clip(data, -1.0, 1.0 - lsb, out=data)
    sf.write(path, data, CD_RATE, subtype="PCM_16", format="WAV" if fmt == "wav" else "AIFF")


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
    _id3_tags(f.tags, track, release, info)
    # ID3v2.3 is what Windows Explorer, Rekordbox and Serato read most reliably
    f.save(v2_version=3)


def tag_id3_file(path: Path, track: Track, release: Release, info: SavedTrack) -> None:
    """The same tags as an MP3, in the ID3 chunk of a WAV or AIFF file (Rekordbox and Serato read it)."""
    f = WAVE(path) if path.suffix.lower() == ".wav" else AIFF(path)
    if f.tags is None:
        f.add_tags()
    _id3_tags(f.tags, track, release, info)
    f.save(v2_version=3)


def _id3_tags(tags: ID3, track: Track, release: Release, info: SavedTrack) -> None:
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


def _saved_tags(output_dir: Path, release: Release, pattern: str):
    """(track number, length, BPM text, key) for each track already saved for this release."""
    folders = naming.album_folders(output_dir, release, pattern)
    if pattern != naming.DEFAULT_PATTERN:  # albums saved before the pattern was changed
        folders += naming.album_folders(output_dir, release)
    for folder in folders:
        if not folder.is_dir():
            continue
        for path in sorted(folder.iterdir()):
            try:
                if path.suffix.lower() == ".flac":
                    f = FLAC(path)
                    number, bpm = f.get("TRACKNUMBER", [""])[0], f.get("BPM", [""])[0]
                    key = f.get("INITIALKEY", [""])[0]
                elif path.suffix.lower() in AUDIO_SUFFIXES:
                    f = MutagenFile(path)
                    tags = (f.tags if f is not None else None) or {}
                    number = str(tags["TRCK"].text[0]) if "TRCK" in tags else ""
                    bpm = str(tags["TBPM"].text[0]) if "TBPM" in tags else ""
                    key = str(tags["TKEY"].text[0]) if "TKEY" in tags else ""
                else:
                    continue
                n = int(number.split("/")[0])
            except (ValueError, KeyError, OSError, IndexError, MutagenError):
                continue
            yield n, f.info.length, bpm, key


def read_saved(output_dir: Path, release: Release,
               pattern: str = naming.DEFAULT_PATTERN) -> tuple[dict[int, float], dict[int, float]]:
    """BPMs and lengths (by track number) from tracks already saved for this release, for labels."""
    bpms: dict[int, float] = {}
    lengths: dict[int, float] = {}
    for n, length, bpm, _ in _saved_tags(output_dir, release, pattern):
        lengths.setdefault(n, length)
        try:
            bpms.setdefault(n, float(bpm))
        except ValueError:
            pass
    return bpms, lengths


def read_saved_keys(output_dir: Path, release: Release, pattern: str = naming.DEFAULT_PATTERN) -> dict[int, str]:
    """Keys (by track number) in the tracks saved for this release, e.g. copied in from Rekordbox."""
    keys: dict[int, str] = {}
    for n, _, _, key in _saved_tags(output_dir, release, pattern):
        if key:
            keys.setdefault(n, key)
    return keys


def read_key(path: Path) -> str:
    """The key tag of a saved track ("" if none)."""
    try:
        if path.suffix.lower() == ".flac":
            return FLAC(path).get("INITIALKEY", [""])[0]
        f = MutagenFile(path)
        tags = (f.tags if f is not None else None) or {}
        return str(tags["TKEY"].text[0]) if "TKEY" in tags else ""
    except (MutagenError, OSError, KeyError, IndexError):
        return ""


def write_key(path: Path, key: str, bpm: float | None = None) -> None:
    """Store a key (classic notation, e.g. "Dm") in a saved track, and its BPM if it has none."""
    if path.suffix.lower() == ".flac":
        f = FLAC(path)
        f["INITIALKEY"] = key
        if bpm and not f.get("BPM"):
            f["BPM"] = _bpm_text(bpm)
        f.save()
        return
    f = MutagenFile(path)
    if f.tags is None:
        f.add_tags()
    f.tags.add(TKEY(encoding=3, text=key))
    if bpm and "TBPM" not in f.tags:
        f.tags.add(TBPM(encoding=3, text=_bpm_text(bpm)))
    f.save(v2_version=3)


def copy_rekordbox_keys(output_dir: Path, library) -> int:
    """Write the key Rekordbox found into every saved track it knows (a rekordbox_db.Library).
    Returns how many files were changed."""
    changed = 0
    for path in output_dir.rglob("*"):
        if path.suffix.lower() not in AUDIO_SUFFIXES or "_recordings" in path.parts:
            continue
        rb = library.at(path)
        if rb is None or not rb.key or read_key(path) == rb.key:
            continue
        try:
            write_key(path, rb.key, rb.bpm)
            changed += 1
        except (MutagenError, OSError):
            pass  # in use or read-only: try again next time
    return changed


def _mime(data: bytes) -> str:
    return "image/png" if data[:4] == b"\x89PNG" else "image/jpeg"
