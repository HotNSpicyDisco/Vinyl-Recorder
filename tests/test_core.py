"""Splitting and export, checked against a synthetic side with known track boundaries."""

import numpy as np
import soundfile as sf
from mutagen.flac import FLAC

from vinyl.audio import compute_envelope
from vinyl.discogs import Release, Track, join_artists, parse_duration
from vinyl.export import TrackCut, export_side
from vinyl.splitter import split, split_sides

SR = 8000


def make_side(path, songs, gap=2.5, lead_in=3.0, lead_out=4.0, quiet_passage=None):
    """Write a fake side: surface noise everywhere, loud noise bursts for songs.

    Returns the true boundaries (middle of each gap)."""
    rng = np.random.default_rng(1)
    parts, t, bounds = [], 0.0, []
    parts.append(np.zeros(int(lead_in * SR)))
    t += lead_in
    for i, length in enumerate(songs):
        song = 0.3 * rng.standard_normal(int(length * SR)) * (0.6 + 0.4 * np.sin(np.arange(int(length * SR)) / SR))
        if quiet_passage and i == quiet_passage[0]:
            a = int(quiet_passage[1] * SR)
            song[a:a + int(1.5 * SR)] *= 0.005
        parts.append(song)
        t += length
        if i < len(songs) - 1:
            parts.append(np.zeros(int(gap * SR)))
            bounds.append(t + gap / 2)
            t += gap
    parts.append(np.zeros(int(lead_out * SR)))
    mono = np.concatenate(parts)
    mono += 0.002 * rng.standard_normal(len(mono))  # surface noise
    clicks = rng.integers(0, len(mono), 40)
    mono[clicks] += 0.5  # crackle
    sf.write(path, np.stack([mono, mono], axis=1), SR, subtype="PCM_24")
    return bounds


def test_split_uses_discogs_durations(tmp_path):
    wav = tmp_path / "side.wav"
    truth = make_side(wav, [40, 25, 50], quiet_passage=(1, 10))
    env = compute_envelope(wav)
    # Nominal lengths a few % off, as on a real record
    result = split(env, durations=[41, 26, 52])
    assert len(result.boundaries) == 2
    for got, want in zip(result.boundaries, truth):
        assert abs(got - want) < 1.5
    assert result.music_start < 3.1 and result.music_end > env.duration - 4.5


def test_split_without_durations(tmp_path):
    wav = tmp_path / "side.wav"
    truth = make_side(wav, [30, 30, 30, 30])
    result = split(compute_envelope(wav), track_count=4)
    assert [round(b) for b in result.boundaries] == [round(b) for b in truth]


def make_whole_lp(path, side_a, side_b, flip=20.0, gap=2.5, thumps=True):
    """Side A, a flip pause with handling noise, then side B, in one file.

    Returns (true track boundaries per side, (flip start, flip end))."""
    rng = np.random.default_rng(7)

    def song(length):
        n = int(length * SR)
        return 0.3 * rng.standard_normal(n) * (0.6 + 0.4 * np.sin(np.arange(n) / SR))

    parts, t, bounds = [np.zeros(int(2 * SR))], 2.0, []
    for side_i, side in enumerate((side_a, side_b)):
        for i, length in enumerate(side):
            parts.append(song(length))
            t += length
            if i < len(side) - 1:
                parts.append(np.zeros(int(gap * SR)))
                bounds.append(t + gap / 2)
                t += gap
        if side_i == 0:
            pause = np.zeros(int(flip * SR))
            if thumps:  # needle lift (after a few s of run-out), a bump while flipping, needle drop (lead-in)
                for at in (3.0, flip / 2, flip - 4.0):
                    a = int(at * SR)
                    pause[a:a + int(0.6 * SR)] = 0.25 * rng.standard_normal(int(0.6 * SR))
            parts.append(pause)
            flip_span = (t, t + flip)
            t += flip
    parts.append(np.zeros(int(3 * SR)))
    mono = np.concatenate(parts) + 0.002 * rng.standard_normal(int(sum(len(p) for p in parts)))
    sf.write(path, np.stack([mono, mono], axis=1), SR, subtype="PCM_24")
    return bounds, flip_span


def test_whole_lp_split(tmp_path):
    wav = tmp_path / "lp.wav"
    truth, (flip_a, flip_b) = make_whole_lp(wav, [30, 20, 25], [35, 22])
    env = compute_envelope(wav)
    r = split_sides(env, [[31, 21, 26], [36, 23]])
    # Parts: A1 gap A2 gap A3 | flip | B1 gap B2  ->  5 tracks, 4 skipped parts
    segs = list(zip(r.edges[:-1], r.edges[1:]))
    tracks = [s for i, s in enumerate(segs) if i not in r.skips]
    skipped = [s for i, s in enumerate(segs) if i in r.skips]
    assert len(tracks) == 5 and len(skipped) == 4
    a, b = max(skipped, key=lambda s: s[1] - s[0])  # the flip is the longest
    assert abs(a - flip_a) < 1.5 and abs(b - flip_b) < 1.5, (a, b, flip_a, flip_b)
    # Each later track starts where its gap ends (true boundary is the gap middle, gaps are 2.5 s)
    for (start, _), want in zip([tracks[1], tracks[2], tracks[4]], truth):
        assert abs(start - (want + 1.25)) < 0.2, (start, want + 1.25)


def test_gaps_become_their_own_parts(tmp_path):
    wav = tmp_path / "side.wav"
    make_side(wav, [40, 25, 50])  # lead-in 3 s, gaps 2.5 s
    edges, skips = split(compute_envelope(wav), durations=[41, 26, 52]).edges_and_skips()
    assert skips == {1, 3} and len(edges) == 6
    # A1 ends where the first gap starts (3 + 40 = 43), A2 starts where it ends (45.5)
    assert abs(edges[1] - 43.0) < 0.2 and abs(edges[2] - 45.5) < 0.2, edges


def _db_noise(rng, seconds, db, sr):
    return 10 ** (db / 20) * rng.standard_normal(int(seconds * sr))


def _music(rng, seconds, sr, db=-20.0):
    """Busy 'music': noise with a kick every half second, so its level never drops to silence."""
    n = int(seconds * sr)
    x = _db_noise(rng, seconds, db, sr)
    k = np.arange(int(0.2 * sr)) / sr
    kick = np.sin(2 * np.pi * 60 * k) * np.exp(-k * 10) * 0.5
    for s in range(0, n - len(kick), int(0.5 * sr)):
        x[s:s + len(kick)] += kick
    return x


def test_snaps_to_first_beat_through_crackle(tmp_path):
    from vinyl.splitter import refine_edges

    sr = 48000
    rng = np.random.default_rng(3)
    x = _db_noise(rng, 14, -60, sr)  # groove noise everywhere
    x[int(2 * sr):int(8.3 * sr)] += _music(rng, 6.3, sr)  # track 1: 2.000 – 8.300 s
    for t in (8.9, 9.4, 9.7, 9.95):  # crackle in the gap
        x[int(t * sr)] += 0.6
    x[int(10 * sr):] += _music(rng, 4, sr)  # track 2 starts on a kick at exactly 10.000 s
    wav = tmp_path / "gap.wav"
    sf.write(wav, np.stack([x, x], axis=1), sr, subtype="PCM_24")

    edges = refine_edges(wav, [1.8, 8.45, 9.85, 13.9], {1}, music_db=-20)
    assert 1.99 <= edges[0] <= 2.0, edges  # first beat, a few ms early at most
    assert 8.3 <= edges[1] <= 8.3 + 0.6, edges  # end of track 1 plus the short tail
    assert 9.99 <= edges[2] <= 10.0, edges  # next track on its first beat, crackle ignored


def test_long_fade_is_kept(tmp_path):
    """A track that fades out over 10 s into groove noise ends where the fade meets the noise,
    not where it first gets quiet (the Jam Harder 'Tom's Diner' case)."""
    from vinyl.splitter import refine_edges

    sr = 48000
    rng = np.random.default_rng(5)
    x = _db_noise(rng, 30, -63, sr)
    fade = _music(rng, 12, sr) * np.logspace(0, -45 / 20, int(12 * sr))  # -20 dB down to -65 over 12 s
    x[int(2 * sr):int(14 * sr)] += fade
    x[int(17 * sr):] += _music(rng, 13, sr)
    wav = tmp_path / "fade.wav"
    sf.write(wav, np.stack([x, x], axis=1), sr, subtype="PCM_24")
    # Rough edges as the 20 ms analysis would give them: the gap "starts" while the fade is still going
    edges = refine_edges(wav, [2.0, 9.0, 16.9, 29.0], {1}, music_db=-20)
    # The fade passes groove noise + 6 dB (-57) about 10.4 s in, i.e. at ~12.4 s
    assert 11.8 <= edges[1] <= 13.5, edges
    assert 16.99 <= edges[2] <= 17.0, edges


def test_needle_down_and_flip_noise_ignored(tmp_path):
    """Lead-in rumble and clicks before the first track, and run-out clicks, needle lift, silence,
    needle drop and lead-in noise during a flip, are all left out (the Jam Harder / Reflex cases)."""
    from vinyl.splitter import refine_edges

    sr = 48000
    rng = np.random.default_rng(9)
    x = _db_noise(rng, 60, -85, sr)  # needle up: electronics noise only
    t = np.arange(len(x)) / sr

    def groove(a, b):  # needle in the groove: noise plus heavy rumble at 12 Hz
        i, j = int(a * sr), int(b * sr)
        x[i:j] += _db_noise(rng, b - a, -58, sr) + 0.02 * np.sin(2 * np.pi * 12 * t[i:j])

    def burst(at, length, db):  # needle drop / lift / once-per-turn click
        i = int(at * sr)
        x[i:i + int(length * sr)] += _db_noise(rng, length, db, sr)

    groove(1.0, 20.0)
    burst(1.0, 0.4, -25)  # needle drop
    burst(2.5, 0.05, -40)  # lead-in click
    x[int(6 * sr):int(16 * sr)] += _music(rng, 10, sr)  # side A: 6.000 – 16.000 s
    for c in (17.0, 18.8):
        burst(c, 0.05, -45)  # run-out groove clicks, one per revolution
    burst(20.0, 0.3, -30)  # needle lift
    groove(26.0, 60.0)
    burst(26.0, 1.2, -22)  # needle drop on side B (long and loud)
    x[int(30 * sr):int(40 * sr)] += _music(rng, 10, sr)  # side B: 30.000 – 40.000 s
    wav = tmp_path / "flip.wav"
    sf.write(wav, np.stack([x, x], axis=1), sr, subtype="PCM_24")

    edges = refine_edges(wav, [5.5, 16.2, 29.0, 41.0], {1}, music_db=-20)
    assert 5.99 <= edges[0] <= 6.0, edges  # not the needle drop or lead-in click
    assert 16.0 <= edges[1] <= 16.6, edges  # not the run-out clicks or the needle lift
    assert 29.99 <= edges[2] <= 30.0, edges  # not the side B needle drop or lead-in rumble
    assert 40.0 <= edges[3] <= 40.6, edges


def test_whole_lp_without_clear_flip_falls_back_to_lengths(tmp_path):
    wav = tmp_path / "lp.wav"
    make_whole_lp(wav, [30, 20], [30, 20], flip=2.0, thumps=False)  # flip as short as a track gap
    r = split_sides(compute_envelope(wav), [[30, 20], [30, 20]])
    assert len(r.edges) - 1 - len(r.skips) == 4  # still four tracks


def test_export_writes_tagged_flacs(tmp_path):
    wav = tmp_path / "side.wav"
    make_side(wav, [10, 10])
    release = Release(
        id=1, artist="Test Artist", title="Test: Album?", year="1979", genres=["Funk / Soul"],
        styles=["Disco"], label="Label", catno="LAB 1",
        tracks=[Track("A1", "One", 10, "", 1), Track("A2", "Two/Three", 10, "Guest", 2)],
        cover_url="", url="", cover=b"\xff\xd8\xff fake jpeg",
    )
    cuts = [TrackCut(release.tracks[0], 3, 13), TrackCut(release.tracks[1], 15.5, 25.5)]
    saved = export_side(wav, cuts, release, tmp_path / "out")
    files = [s.files[0] for s in saved]
    assert [f.name for f in files] == ["01 - One.flac", "02 - Two_Three.flac"]
    assert files[0].parent.name == "Test_ Album_ (1979)"
    tags = FLAC(files[1])
    assert tags["ARTIST"] == ["Guest"] and tags["ALBUMARTIST"] == ["Test Artist"]
    assert tags["TRACKNUMBER"] == ["2"] and tags["VINYL_POSITION"] == ["A2"]
    assert len(tags.pictures) == 1
    assert abs(sf.info(files[0]).duration - 10) < 0.01


def test_export_mp3_and_flac_with_bpm(tmp_path):
    from mutagen.id3 import ID3
    from scipy.signal import resample_poly

    from test_tempo import SR as DSR, drum_loop

    # A 96 kHz side with two tracks at different tempos, as the recorder would make it
    side = np.concatenate([drum_loop(124.0, 20), np.zeros((DSR * 2, 2), np.float32), drum_loop(100.0, 20)])
    wav = tmp_path / "side.wav"
    sf.write(wav, resample_poly(side, 96000, DSR, axis=0), 96000, subtype="PCM_24")
    release = Release(
        id=7, artist="DJ Test", title="Tempo", year="1983", genres=["Electronic"], styles=["Disco"],
        label="L", catno="C1", tracks=[Track("A1", "Fast", 20, "", 1), Track("A2", "Slow", 20, "", 2)],
        cover_url="", url="", cover=b"\xff\xd8\xff fake jpeg",
    )
    cuts = [TrackCut(release.tracks[0], 0, 20), TrackCut(release.tracks[1], 22, 42)]
    saved = export_side(wav, cuts, release, tmp_path / "out", fmt="both")

    assert [round(s.bpm) for s in saved] == [124, 100]
    flac, mp3 = saved[0].files
    assert flac.suffix == ".flac" and mp3.suffix == ".mp3"
    assert mp3.parent.name == "Tempo (1983) [MP3]"
    assert FLAC(flac)["BPM"] == ["124"] and FLAC(flac)["LENGTH"] == ["20000"]

    info = sf.info(mp3)
    assert info.samplerate == 48000 and abs(info.duration - 20) < 0.1
    id3 = ID3(mp3)
    assert id3.version[:2] == (2, 3)
    assert id3["TBPM"].text == ["124"] and id3["TLEN"].text == ["20000"]
    assert id3["TRCK"].text == ["1/2"] and id3["TPE2"].text == ["DJ Test"]
    assert id3["TXXX:CATALOGNUMBER"].text == ["C1"] and id3.getall("APIC")

    only_mp3 = export_side(wav, cuts[:1], release, tmp_path / "mp3only", fmt="mp3", detect_bpm=False)
    assert only_mp3[0].files[0].parent.name == "Tempo (1983)" and only_mp3[0].bpm is None


def test_clip_counter():
    from types import SimpleNamespace

    from vinyl.audio import Recorder

    r = Recorder()
    r.samplerate = 1000
    ok = SimpleNamespace(input_overflow=False)
    a = np.zeros((100, 2), np.float32)
    a[10:20, 0] = 1.0  # one clip event on L (10 samples in a row)
    a[50, 1] = -1.0  # one on R
    a[99, 0] = 1.0  # starts at the end of this block...
    b = np.zeros((100, 2), np.float32)
    b[0:3, 0] = 1.0  # ...and continues here: still one event
    b[40, 0] = 0.5  # loud but not clipped
    r._callback(a, 100, None, ok)
    r._recording = True  # clip times are only kept while recording
    r._callback(b, 100, None, ok)
    c = np.zeros((100, 2), np.float32)
    c[30, :] = 1.0
    r._callback(c, 100, None, ok)
    assert r.clip_counts == [3, 2]
    assert r.clip_times == [0.13, 0.13]  # 100 recorded frames + 30, at 1 kHz
    r.reset_clips()
    assert r.clip_counts == [0, 0] and r.clip_times == []


def test_discogs_helpers():
    assert parse_duration("3:45") == 225
    assert parse_duration("1:02:03") == 3723
    assert parse_duration("") is None
    artists = [{"name": "Chic (2)", "join": "Feat."}, {"name": "Nile Rodgers", "join": ","}, {"name": "X"}]
    assert join_artists(artists) == "Chic Feat. Nile Rodgers, X"
