"""Hands-free recording: wait for the needle, pause at the run-out groove, re-arm, finish after the last side."""

from types import SimpleNamespace

import numpy as np
import soundfile as sf

from vinyl.audio import ARMED, FINISHED, PAUSED, RECORDING, Recorder

SR = 8000
OK = SimpleNamespace(input_overflow=False)


def noise(rng, seconds, db):
    return (10 ** (db / 20) * rng.standard_normal(int(seconds * SR))).astype(np.float32)


def runout(rng, seconds):
    """Groove noise with the once-per-turn click of a run-out groove (every 1.8 s)."""
    x = noise(rng, seconds, -60)
    for t in np.arange(0.3, seconds, 1.8):
        x[int(t * SR)] += 0.5
    return x


def feed(r, mono, block=512):
    """Send audio through the recorder's callback the way PortAudio would; returns states seen."""
    seen = []
    stereo = np.stack([mono, mono], axis=1)
    for i in range(0, len(stereo), block):
        r._callback(stereo[i:i + block], len(stereo[i:i + block]), None, OK)
        if not seen or seen[-1] != r.state:
            seen.append(r.state)
    return seen


def make_recorder(tmp_path, sides):
    r = Recorder()
    r.samplerate = SR
    r.stream = object()  # pretend the input is open
    r.start(tmp_path / "take.wav", hands_free=True, sides=sides)
    return r


def test_two_sides_hands_free(tmp_path):
    rng = np.random.default_rng(1)
    r = make_recorder(tmp_path, sides=2)
    assert r.state == ARMED

    states = feed(r, noise(rng, 1.5, -85))  # needle up: nothing is written
    assert states == [ARMED] and r._rec_frames == 0

    states = feed(r, np.concatenate([noise(rng, 0.4, -15), noise(rng, 10, -20)]))  # needle drop, side A
    assert states == [ARMED, RECORDING]
    states = feed(r, runout(rng, 8))  # side A's run-out groove: pauses after 6 s
    assert PAUSED in states and len(r.side_breaks) == 1

    # Lift, flip (silence with a click), drop the needle on side B
    feed(r, np.concatenate([runout(rng, 1)[:200] * 0 + noise(rng, 0.025, -30)[:200], noise(rng, 3, -85)]))
    assert r.state == PAUSED  # the run-out clicks and the lift didn't restart it
    states = feed(r, np.concatenate([noise(rng, 0.4, -15), noise(rng, 8, -20)]))
    assert RECORDING in states
    states = feed(r, runout(rng, 8))
    assert r.state == FINISHED and r.event == "finished" and len(r.side_breaks) == 2

    path = r.stop()
    seconds = sf.info(path).duration
    # Side A: up to 2 s of pre-roll + 10.4 s + ~6 s of run-out; side B: pre-roll + 8.4 s + ~6 s
    assert 30 < seconds < 37, seconds
    a_end = r.side_breaks[0]
    assert 16 < a_end < 19.5, a_end  # where side A stopped in the file
    # Nothing from the flip was written: the file has no 3 s stretch of needle-up silence
    with sf.SoundFile(path) as f:
        x = f.read(dtype="float32")[:, 0]
    blocks = np.sqrt(np.mean(x[: len(x) // 800 * 800].reshape(-1, 800) ** 2, axis=1))
    silent_run = max((len(s) for s in "".join("s" if b < 1e-3 else "." for b in blocks).split(".")), default=0)
    assert silent_run * 0.1 < 2.5, silent_run * 0.1


def test_no_album_keeps_going_until_stopped(tmp_path):
    rng = np.random.default_rng(2)
    r = make_recorder(tmp_path, sides=None)
    feed(r, np.concatenate([noise(rng, 0.5, -85), noise(rng, 6, -20), runout(rng, 7)]))
    assert r.state == PAUSED and r.event is None  # waits for another side
    r.stop()


def test_quiet_passages_and_track_gaps_dont_end_a_side(tmp_path):
    rng = np.random.default_rng(3)
    r = make_recorder(tmp_path, sides=1)
    music = np.concatenate([noise(rng, 8, -18), noise(rng, 3, -60),  # 3 s gap between tracks
                            noise(rng, 6, -45),  # a quiet intro, well above the groove noise
                            noise(rng, 8, -18)])
    feed(r, np.concatenate([noise(rng, 0.5, -85), music]))
    assert r.state == RECORDING and not r.side_breaks
    r.stop()


def test_normal_mode_records_everything(tmp_path):
    rng = np.random.default_rng(4)
    r = Recorder()
    r.samplerate, r.stream = SR, object()
    r.start(tmp_path / "plain.wav")
    feed(r, noise(rng, 3, -85))
    assert r.state == RECORDING
    path = r.stop()
    assert abs(sf.info(path).duration - 3) < 0.1
