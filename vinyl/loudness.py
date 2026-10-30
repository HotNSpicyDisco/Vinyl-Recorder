"""Loudness (ITU-R BS.1770 / EBU R128) for ReplayGain 2.0 tags, and peak levels for normalizing.

ReplayGain gain = -18 LUFS minus the measured loudness: how much a player should turn a track
(or the whole album) up or down to play at an even volume. The audio itself isn't changed.
"""

import numpy as np
from scipy.signal import lfilter

REFERENCE_LUFS = -18.0  # ReplayGain 2.0
BLOCK, HOP = 0.4, 0.1  # gating blocks: 400 ms, 75% overlap
ABSOLUTE_GATE = -70.0
RELATIVE_GATE = -10.0


def _k_filters(sr: int):
    """The two K-weighting stages for any sample rate (high shelf, then high pass)."""
    f0, gain_db, q = 1681.974450955533, 3.999843853973347, 0.7071752369554196
    k = np.tan(np.pi * f0 / sr)
    vh = 10 ** (gain_db / 20)
    vb = vh ** 0.4996667741545416
    a0 = 1 + k / q + k * k
    shelf = ([(vh + vb * k / q + k * k) / a0, 2 * (k * k - vh) / a0, (vh - vb * k / q + k * k) / a0],
             [1.0, 2 * (k * k - 1) / a0, (1 - k / q + k * k) / a0])
    f0, q = 38.13547087602444, 0.5003270373238773
    k = np.tan(np.pi * f0 / sr)
    a0 = 1 + k / q + k * k
    highpass = ([1.0, -2.0, 1.0], [1.0, 2 * (k * k - 1) / a0, (1 - k / q + k * k) / a0])
    return shelf, highpass


def block_powers(data: np.ndarray, sr: int) -> np.ndarray:
    """K-weighted mean square of each 400 ms block (channels summed), for gating.
    Keep these per track: the album's loudness is computed from all tracks' blocks together."""
    x = data if data.ndim == 2 else data[:, None]
    for b, a in _k_filters(sr):
        x = lfilter(b, a, x, axis=0)
    n, hop = int(BLOCK * sr), int(HOP * sr)
    if len(x) < n:
        return np.zeros(0)
    csum = np.concatenate([np.zeros((1, x.shape[1])), np.cumsum(x.astype(np.float64) ** 2, axis=0)])
    starts = np.arange(0, len(x) - n + 1, hop)
    return ((csum[starts + n] - csum[starts]) / n).sum(axis=1)


def integrated(blocks: np.ndarray) -> float | None:
    """Integrated loudness in LUFS from block powers (None for silence)."""
    if len(blocks) == 0:
        return None
    lufs = -0.691 + 10 * np.log10(blocks + 1e-20)
    kept = blocks[lufs > ABSOLUTE_GATE]
    if len(kept) == 0:
        return None
    relative = -0.691 + 10 * np.log10(kept.mean()) + RELATIVE_GATE
    kept = kept[-0.691 + 10 * np.log10(kept) > relative]
    return float(-0.691 + 10 * np.log10(kept.mean())) if len(kept) else None


def replaygain(lufs: float | None) -> float | None:
    return None if lufs is None else REFERENCE_LUFS - lufs


def peak(data: np.ndarray) -> float:
    return float(np.abs(data).max()) if len(data) else 0.0
