"""Mains hum detection: finds 50/60 Hz ground hum, ignores clean noise and music."""

import numpy as np
import soundfile as sf

from vinyl import hum
from vinyl.audio import compute_envelope

SR = 48000
rng = np.random.default_rng(0)


def noise(seconds, db):
    return 10 ** (db / 20) * rng.standard_normal(int(seconds * SR))


def buzz(seconds, f0, db, harmonics=(1, 0.5, 0.3)):
    t = np.arange(int(seconds * SR)) / SR
    return sum(10 ** (db / 20) * np.sqrt(2) * a * np.sin(2 * np.pi * f0 * (k + 1) * t) for k, a in enumerate(harmonics))


def test_finds_60_and_50_hz():
    h = hum.detect(noise(4, -70) + buzz(4, 60, -55), SR)
    assert h and h.freq == 60 and abs(h.level_db - -55) < 1.5, h
    h = hum.detect(noise(4, -70) + buzz(4, 50, -60, harmonics=(0.2, 1, 0.4)), SR)  # mostly the 100 Hz harmonic
    assert h and h.freq == 50, h
    assert "ground wire" in h.advice


def test_clean_noise_and_music_are_not_hum():
    assert hum.detect(noise(4, -60), SR) is None  # surface noise
    t = np.arange(4 * SR) / SR
    music = noise(4, -25) + 0.1 * np.sin(2 * np.pi * 110 * t)  # a bass note near 2 × 55... not mains
    assert hum.detect(music, SR) is None
    assert hum.detect(noise(4, -90) + buzz(4, 60, -85), SR) is None  # too quiet to matter
    assert hum.detect(noise(1, -60), SR) is None  # too short to judge


def test_check_recording_uses_the_gaps(tmp_path):
    side = np.concatenate([noise(10, -18), noise(3, -62), noise(10, -18), noise(3, -62)])
    hummy = side + buzz(len(side) / SR, 60, -58)
    for x, expect in ((side, None), (hummy, 60)):
        p = tmp_path / "take.wav"
        sf.write(p, np.stack([x, x], axis=1).astype(np.float32), SR, subtype="PCM_24")
        found = hum.check_recording(p, compute_envelope(p), quiet_db=-45)
        assert (found.freq if found else None) == expect
