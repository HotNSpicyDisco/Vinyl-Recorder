"""Tempo (BPM) estimation from audio, using onset strength and autocorrelation.

Good enough for steady-beat music (disco, house, funk, pop). Free-tempo or beatless
tracks get None or a best guess.
"""

from math import gcd

import numpy as np
from scipy.ndimage import gaussian_filter1d
from scipy.signal import resample_poly, stft

ANALYSIS_RATE = 11025
HOP = 128  # ~86 onset frames per second
SEARCH_MIN_BPM, SEARCH_MAX_BPM = 40.0, 200.0
# Result range is [low, 2*low), as in Rekordbox. 78 suits disco/house/funk.
BPM_RANGES = [58.0, 68.0, 78.0, 88.0, 98.0]
DEFAULT_LOW_BPM = 78.0
ANALYSE_SECONDS = 90.0
SPAN_SECONDS = 8.0  # how far the coarse search looks for repeats
REFINE_SPAN_SECONDS = 40.0  # longer span for a precise final value


def onset_strength(mono: np.ndarray, sr: int) -> tuple[np.ndarray, float]:
    """Spectral flux: how much new energy appears in each frame. Returns (envelope, frames per second)."""
    g = gcd(int(sr), ANALYSIS_RATE)
    x = resample_poly(mono, ANALYSIS_RATE // g, int(sr) // g) if sr != ANALYSIS_RATE else mono
    _, _, z = stft(x, fs=ANALYSIS_RATE, nperseg=1024, noverlap=1024 - HOP, boundary=None, padded=False)
    mag = np.log1p(100 * np.abs(z))
    flux = np.maximum(0.0, np.diff(mag, axis=1)).sum(axis=0)
    flux -= np.convolve(flux, np.ones(16) / 16, mode="same")  # remove slow loudness changes
    return np.maximum(flux, 0.0), ANALYSIS_RATE / HOP


def _comb(acf: np.ndarray, period: float, span: float, start: float = 1.0) -> float:
    """Mean autocorrelation at start*period, (start+1)*period, ... up to span frames."""
    points = np.arange(start * period, min(span, len(acf) - 1), period)
    return float(np.interp(points, np.arange(len(acf)), acf).mean()) if len(points) else 0.0


def estimate_bpm(data: np.ndarray, sr: int, low: float = DEFAULT_LOW_BPM) -> float | None:
    """BPM of audio (mono or (frames, channels)) within [low, 2*low), or None if there is no clear beat."""
    mono = data.mean(axis=1) if data.ndim == 2 else data
    if len(mono) < sr * 8:
        return None
    # Use up to 90 s from the middle of the track; intros and outros are often beatless.
    n = int(ANALYSE_SECONDS * sr)
    if len(mono) > n:
        a = (len(mono) - n) // 2
        mono = mono[a:a + n]
    env, fps = onset_strength(mono.astype(np.float64), sr)
    if not np.any(env):
        return None
    # Widen each onset a little so slightly uneven (human, vinyl) timing still lines up.
    env = gaussian_filter1d(env, 1.5)
    env -= env.mean()
    acf = np.correlate(env, env, mode="full")[len(env) - 1:]
    if acf[0] <= 0:
        return None
    acf /= acf[0]

    span = SPAN_SECONDS * fps
    grid = np.arange(SEARCH_MIN_BPM, SEARCH_MAX_BPM + 0.01, 0.25)
    # 1. Strongest steady repetition. This is often the half-bar or bar rather than the beat;
    #    which octave is "right" is a matter of convention, so step 2 decides it.
    scores = np.array([_comb(acf, 60 * fps / b, span) for b in grid])
    bpm = float(grid[int(np.argmax(scores))])
    if scores.max() < 0.1:
        return None

    # 2. Fold into the chosen range, like DJ software does (e.g. 78-155: 64 -> 128, 87 -> 174 needs 88-175).
    while bpm < low:
        bpm *= 2
    while bpm >= 2 * low:
        bpm /= 2

    # 3. Refine over a longer span, where small tempo errors add up and become visible.
    fine = np.arange(bpm * 0.98, bpm * 1.02, 0.02)
    long_span = REFINE_SPAN_SECONDS * fps
    bpm = float(fine[int(np.argmax([_comb(acf, 60 * fps / b, long_span) for b in fine]))])
    return round(bpm, 1)
