"""Loudness measurement against the BS.1770 reference values, and gating."""

import numpy as np

from vinyl import loudness

SR = 48000


def sine(db, seconds=10, freq=997, channels=2, sr=SR):
    t = np.arange(int(seconds * sr)) / sr
    x = (10 ** (db / 20) * np.sin(2 * np.pi * freq * t)).astype(np.float32)
    return np.stack([x] * channels, axis=1)


def lufs(x, sr=SR):
    return loudness.integrated(loudness.block_powers(x, sr))


def test_reference_tone():
    # BS.1770: a 0 dBFS 997 Hz sine in one channel measures -3.01 LUFS; in both channels 0.0
    left_only = sine(0)
    left_only[:, 1] = 0
    assert abs(lufs(left_only) - -3.01) < 0.05
    assert abs(lufs(sine(0)) - 0.0) < 0.05
    assert abs(lufs(sine(-20)) - -20.0) < 0.05


def test_other_sample_rates():
    for sr in (44100, 96000):
        assert abs(lufs(sine(-20, sr=sr), sr) - -20.0) < 0.1


def test_gating_ignores_silence():
    # Loud music with long silent gaps measures (almost) the same as the music alone; the few
    # blocks straddling the edges of the silence count partly, as the standard allows
    x = np.concatenate([sine(-14, 5), np.zeros((SR * 10, 2), np.float32), sine(-14, 5)])
    assert abs(lufs(x) - lufs(sine(-14, 10))) < 0.3
    assert lufs(x) > -14.5  # without gating, 10 s of silence in 20 s would pull it down by 3 dB
    assert loudness.integrated(loudness.block_powers(np.zeros((SR * 2, 2)), SR)) is None


def test_replaygain():
    assert loudness.replaygain(-14.0) == -4.0
    assert loudness.replaygain(None) is None
