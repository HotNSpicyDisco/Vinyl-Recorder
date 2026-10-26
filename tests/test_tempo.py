"""BPM detection against synthetic drum patterns with known tempos."""

import numpy as np
import pytest

from vinyl.tempo import estimate_bpm

SR = 44100


def drum_loop(bpm: float, seconds: float = 40.0, offbeat_hats: bool = True, swing_bass: bool = False,
              jitter: float = 0.0):
    rng = np.random.default_rng(int(bpm * 10))
    n = int(seconds * SR)
    out = 0.01 * rng.standard_normal(n)  # surface noise
    beat = 60.0 / bpm
    t = np.arange(int(0.25 * SR)) / SR
    kick = np.sin(2 * np.pi * 55 * t * (1 + 2 * np.exp(-t * 30))) * np.exp(-t * 12)
    hat = rng.standard_normal(int(0.05 * SR)) * np.exp(-np.arange(int(0.05 * SR)) / SR * 80) * 0.3
    snare = rng.standard_normal(int(0.15 * SR)) * np.exp(-np.arange(int(0.15 * SR)) / SR * 25) * 0.5
    i = 0
    while (i + 1) * beat < seconds - 0.3:
        s = max(0, int((i * beat + jitter * rng.standard_normal()) * SR))
        out[s:s + len(kick)] += kick
        if i % 2 == 1:
            out[s:s + len(snare)] += snare
        if offbeat_hats:
            h = int((i + 0.5) * beat * SR)
            out[h:h + len(hat)] += hat
        if swing_bass and i % 4 == 3:
            b = int((i + 0.75) * beat * SR)
            out[b:b + len(kick)] += 0.4 * kick
        i += 1
    return np.stack([out, out], axis=1).astype(np.float32)


@pytest.mark.parametrize("bpm", [82.0, 96.0, 112.0, 118.5, 123.0, 128.0, 140.0, 150.0])
def test_detects_tempo(bpm):
    got = estimate_bpm(drum_loop(bpm), SR)
    assert got is not None and abs(got - bpm) < 1.0, got


def test_range_decides_octave():
    loop = drum_loop(174.0)
    assert abs(estimate_bpm(loop, SR, low=88.0) - 174.0) < 1.0
    assert abs(estimate_bpm(loop, SR, low=78.0) - 87.0) < 0.5


def test_syncopation_does_not_fool_it():
    got = estimate_bpm(drum_loop(120.0, swing_bass=True), SR)
    assert abs(got - 120.0) < 1.0, got


def test_human_timing():
    got = estimate_bpm(drum_loop(108.0, jitter=0.012), SR)
    assert abs(got - 108.0) < 1.0, got


def test_96k_input():
    from scipy.signal import resample_poly

    x = resample_poly(drum_loop(125.0), 96000, SR, axis=0)
    assert abs(estimate_bpm(x, 96000) - 125.0) < 1.0


def test_too_short_or_silent():
    assert estimate_bpm(np.zeros((SR * 3, 2)), SR) is None
    assert estimate_bpm(np.zeros((SR * 20, 2)), SR) is None
