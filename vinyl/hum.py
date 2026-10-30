"""Mains hum (50/60 Hz buzz) detection: usually a turntable ground wire that isn't connected.

Hum is a sharp, steady peak at the mains frequency and its harmonics (100/120, 150/180 Hz...)
standing well above the noise around it. It's only checked in quiet audio (needle up, gaps
between tracks), where music can't hide it or be mistaken for it.
"""

from dataclasses import dataclass

import numpy as np
from scipy.signal import welch

from .audio import Envelope

MIN_RATIO_DB = 12.0  # how far the hum peak must stand above the noise around it
MIN_LEVEL_DB = -70.0  # quieter hum sits under a record's own surface noise: not worth a warning
HARMONICS = 4


@dataclass
class Hum:
    freq: int  # 50 or 60
    level_db: float  # dBFS of the strongest hum component
    ratio_db: float  # how far it stands above the surrounding noise

    @property
    def advice(self) -> str:
        return (f"{self.freq} Hz hum detected ({self.level_db:.0f} dB). Check the turntable's ground wire is "
                f"connected to the GND post on your mixer or preamp.")


def detect(x: np.ndarray, sr: int) -> Hum | None:
    """Hum in (quiet) mono or stereo audio of at least 2 seconds, or None."""
    if x.ndim == 2:
        x = x.mean(axis=1)
    if len(x) < 2 * sr:
        return None
    nper = min(len(x), 2 * sr)  # 0.5 Hz resolution
    f, p = welch(x.astype(np.float64), sr, nperseg=nper, noverlap=nper // 2, scaling="spectrum")
    best: Hum | None = None
    for f0 in (50, 60):
        for h in range(1, HARMONICS + 1):
            fc = f0 * h
            peak_band = (f >= fc - 1.0) & (f <= fc + 1.0)
            around = (f >= fc - 10) & (f <= fc + 10) & ~((f >= fc - 2.5) & (f <= fc + 2.5))
            if not peak_band.any() or not around.any():
                continue
            peak = p[peak_band].max()  # with "spectrum" scaling, a steady tone's peak bin is its RMS²
            ratio = 10 * np.log10(peak / (np.median(p[around]) + 1e-30) + 1e-30)
            level = 10 * np.log10(peak + 1e-30)
            if ratio >= MIN_RATIO_DB and level >= MIN_LEVEL_DB and (best is None or level > best.level_db):
                best = Hum(f0, float(level), float(ratio))
    return best


def check_recording(path, env: Envelope, quiet_db: float) -> Hum | None:
    """Look for hum in a recording's quiet stretches (gaps, lead-in), using up to the 5 longest.
    Hum is steady, so it must turn up in at least two of them (a bass note in one gap doesn't count)."""
    import soundfile as sf

    quiet = env.rms_db < quiet_db
    runs, start = [], None
    for i, q in enumerate(np.append(quiet, False)):
        if q and start is None:
            start = i
        elif not q and start is not None:
            if (i - start) * env.hop >= 2.2:
                runs.append((start * env.hop + 0.1, i * env.hop - 0.1))
            start = None
    runs = sorted(runs, key=lambda r: r[1] - r[0], reverse=True)[:5]
    hits: list[Hum] = []
    with sf.SoundFile(path) as f:
        for a, b in runs:
            f.seek(int(a * f.samplerate))
            x = f.read(int(min(b - a, 8.0) * f.samplerate), dtype="float32", always_2d=True)
            if found := detect(x, f.samplerate):
                hits.append(found)
    for freq in (50, 60):
        same = [h for h in hits if h.freq == freq]
        if len(same) >= min(2, len(runs)):
            return max(same, key=lambda h: h.level_db)
    return None
