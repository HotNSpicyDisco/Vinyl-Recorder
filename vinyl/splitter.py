"""Find track boundaries in a recorded side by looking for the quiet gaps between songs.

When the Discogs tracklist gives durations, the gaps closest to where each track
should end are chosen; otherwise the quietest, longest gaps win.
"""

from dataclasses import dataclass, field
from pathlib import Path

import numpy as np
import soundfile as sf
from scipy.signal import sosfilt

from .audio import Envelope, rumble_filter


@dataclass
class Gap:
    start: float
    end: float
    depth_db: float  # how far below the music level the gap sits

    @property
    def middle(self) -> float:
        return (self.start + self.end) / 2

    @property
    def length(self) -> float:
        return self.end - self.start


@dataclass
class SplitResult:
    music_start: float
    music_end: float
    boundaries: list[float]  # split points between tracks, in seconds
    gaps: list[Gap]
    chosen: list[Gap | None] = field(default_factory=list)  # the gap behind each boundary, if one was found

    def segments(self) -> list[tuple[float, float]]:
        edges = [self.music_start, *self.boundaries, self.music_end]
        return list(zip(edges[:-1], edges[1:]))

    def edges_and_skips(self) -> tuple[list[float], set[int]]:
        """Edges where each found gap becomes its own part (not a track): a track ends where the
        gap starts and the next one starts where the gap ends."""
        edges, skips = [self.music_start], set()
        chosen = self.chosen or [None] * len(self.boundaries)
        for b, g in zip(self.boundaries, chosen):
            if g is not None and g.length >= MIN_GAP_PART:
                edges.append(g.start)
                skips.add(len(edges) - 1)
                edges.append(g.end)
            else:
                edges.append(b)
        edges.append(self.music_end)
        return edges, skips


MIN_GAP_PART = 0.2  # shorter gaps are just a split line, not a part of their own


QUIET_BELOW_MUSIC_DB = 25.0  # "silence" is always at least this far below the typical music level


def _levels(env: Envelope) -> tuple[float, float, float]:
    """Noise floor, music level and the silence threshold between them, in dBFS.

    The floor comes from the quietest 5% of the recording. If the recording has hardly any
    silence (one long track per side), that "floor" is really quiet music, so the threshold is
    also capped well below the music level; otherwise quiet intros would count as gaps.
    """
    db = env.rms_db
    # The silence that matters is the groove noise between tracks. Stretches with the needle off the
    # record (before it drops, a hands-free take's pre-roll) are far quieter and would hide it.
    in_groove = db[db > NEEDLE_UP_DB]
    floor = float(np.percentile(in_groove if len(in_groove) > 0.2 * len(db) else db, 5))
    music = float(np.percentile(db, 80))
    threshold = min(floor + max(6.0, (music - floor) * 0.3), music - QUIET_BELOW_MUSIC_DB)
    return floor, music, threshold


def find_gaps(env: Envelope, min_gap: float = 0.8) -> tuple[list[Gap], float, float]:
    """Quiet stretches inside the music, plus where the music starts and ends."""
    db = env.rms_db
    if len(db) == 0:
        return [], 0.0, env.duration
    _, music, threshold = _levels(env)
    # A running median over ~100 ms ignores single clicks and crackles.
    k = max(1, int(round(0.1 / env.hop))) | 1
    padded = np.pad(db, k // 2, mode="edge")
    smooth = np.median(np.lib.stride_tricks.sliding_window_view(padded, k), axis=1)
    loud = smooth > threshold
    _drop_short_runs(loud, int(0.3 / env.hop))

    idx = np.flatnonzero(loud)
    if len(idx) == 0:
        return [], 0.0, env.duration
    first, last = idx[0], idx[-1]
    music_start = max(0.0, first * env.hop - 0.25)
    music_end = min(env.duration, (last + 1) * env.hop + 1.0)

    gaps = []
    min_hops = int(min_gap / env.hop)
    i = first
    while i <= last:
        if loud[i]:
            i += 1
            continue
        j = i
        while j <= last and not loud[j]:
            j += 1
        if j - i >= min_hops:
            depth = music - float(np.mean(smooth[i:j]))
            gaps.append(Gap(i * env.hop, j * env.hop, depth))
        i = j
    return gaps, music_start, music_end


def _drop_short_runs(loud: np.ndarray, min_len: int) -> None:
    """Clear loud runs shorter than min_len hops (a pop in a gap is not music)."""
    edges = np.flatnonzero(np.diff(np.concatenate(([0], loud.astype(np.int8), [0]))))
    for a, b in zip(edges[::2], edges[1::2]):
        if b - a < min_len:
            loud[a:b] = False


def _score(gap: Gap) -> float:
    return gap.depth_db + 4.0 * min(gap.length, 4.0)


def split(env: Envelope, durations: list[float | None] | None = None, track_count: int | None = None,
          min_gap: float = 0.8) -> SplitResult:
    """Work out split points.

    durations: expected track lengths from Discogs (None entries allowed).
    track_count: number of tracks on the side, if known without durations.
    """
    gaps, start, end = find_gaps(env, min_gap)
    n = len(durations) if durations else track_count
    if not n or n <= 1:
        found = [] if n == 1 else list(gaps)
        return SplitResult(start, end, [g.middle for g in found], gaps, found)

    known = durations and all(d for d in durations)
    if known:
        bounds, chosen = _match_expected(gaps, start, end, durations, env)
    else:
        chosen = sorted(sorted(gaps, key=_score, reverse=True)[: n - 1], key=lambda g: g.middle)
        bounds = [g.middle for g in chosen]
        # Not enough gaps: spread the missing splits evenly through the longest segments.
        while len(bounds) < n - 1:
            edges = [start, *bounds, end]
            seg = max(range(len(edges) - 1), key=lambda s: edges[s + 1] - edges[s])
            bounds.insert(seg, (edges[seg] + edges[seg + 1]) / 2)
            chosen.insert(seg, None)
    return SplitResult(start, end, bounds, gaps, chosen)


def _match_expected(gaps: list[Gap], start: float, end: float, durations: list[float],
                    env: Envelope) -> tuple[list[float], list[Gap | None]]:
    # Records rarely play at exactly the nominal length, so scale to the recorded music.
    total = sum(durations)
    scale = (end - start) / total if total else 1.0
    expected = list(start + np.cumsum(durations[:-1]) * scale)
    window = max(15.0, 0.08 * (end - start))

    bounds: list[float] = []
    chosen: list[Gap | None] = []
    used: set[int] = set()
    for exp in expected:
        prev = bounds[-1] if bounds else start
        candidates = [
            (i, g) for i, g in enumerate(gaps)
            if i not in used and g.middle > prev + 5 and abs(g.middle - exp) <= window
        ]
        if candidates:
            # Prefer close gaps, but a clearly deeper gap may sit a little further away.
            i, g = min(candidates, key=lambda c: abs(c[1].middle - exp) - 0.5 * _score(c[1]))
            used.add(i)
            bounds.append(g.middle)
            chosen.append(g)
        else:
            bounds.append(_quietest_near(env, max(exp, prev + 5), 4.0))
            chosen.append(None)
    return bounds, chosen


@dataclass
class SidesResult:
    """Split points for a recording of several sides in one take."""

    edges: list[float]  # music start, splits..., music end
    skips: set[int]  # parts between edges that are side changes, not tracks
    side_breaks: list[tuple[float, float]]  # (end of one side's music, start of the next)


MIN_FLIP_SECONDS = 3.0  # nobody flips a record faster than this; gaps between tracks are shorter
FLIP_NOISE_SECONDS = 2.0  # needle drops and handling thumps inside a flip pause are shorter than this


def _slice(env: Envelope, start: float, end: float) -> tuple[Envelope, float]:
    a, b = int(start / env.hop), max(int(start / env.hop) + 1, int(end / env.hop))
    sub = Envelope(env.rms_db[a:b], env.peak[a:b], env.hop, (b - a) * env.hop, env.samplerate)
    return sub, a * env.hop


def find_side_breaks(env: Envelope, sides: int, side_lengths: list[float] | None = None,
                     min_gap: float = 0.8) -> list[tuple[float, float]]:
    """The (sides - 1) pauses where the record was flipped, as (start, end) times.

    A flip is the longest pause in the take. Handling noise during the flip (needle lift, drop,
    a thump) can split the pause into pieces, so pauses separated by short noise are joined first.
    """
    gaps, start, end = find_gaps(env, min_gap)
    merged: list[list[float]] = []
    for g in gaps:
        if merged and g.start - merged[-1][1] < FLIP_NOISE_SECONDS:
            merged[-1][1] = g.end
        else:
            merged.append([g.start, g.end])
    flips = [m for m in merged if m[1] - m[0] >= MIN_FLIP_SECONDS]
    if len(flips) >= sides - 1:
        if side_lengths and len(flips) > sides - 1:
            # More long pauses than flips (quiet intros, long fades): pick the ones nearest where each side should end.
            scale = (end - start) / (sum(side_lengths) or 1)
            expected = start + np.cumsum(side_lengths[:-1]) * scale
            chosen = []
            for exp in expected:
                best = min((f for f in flips if f not in chosen),
                           key=lambda f: abs((f[0] + f[1]) / 2 - exp) - 2 * (f[1] - f[0]))
                chosen.append(best)
            flips = chosen
        else:
            flips = sorted(flips, key=lambda f: f[1] - f[0], reverse=True)[: sides - 1]
        return sorted((a, b) for a, b in flips)
    # No clear pauses (e.g. the tail of side A runs into side B): fall back to the expected lengths.
    if side_lengths:
        scale = (end - start) / (sum(side_lengths) or 1)
        points = start + np.cumsum(side_lengths[:-1]) * scale
        return [(float(p), float(p)) for p in points]
    return []


def split_sides(env: Envelope, sides: list[list[float | None]], min_gap: float = 0.8,
                known_breaks: list[float] | None = None) -> SidesResult:
    """Split a take containing several sides. `sides` holds the Discogs track lengths for each side, in order.

    known_breaks: where each side ended in the file, when the recorder knows (hands-free mode
    pauses between sides, so there's no flip in the file to find).
    """
    if len(sides) <= 1:
        r = split(env, durations=sides[0] if sides else None, track_count=len(sides[0]) if sides else None,
                  min_gap=min_gap)
        return SidesResult(*r.edges_and_skips(), [])
    if known_breaks and len(known_breaks) >= len(sides) - 1:
        breaks = [(b, b) for b in sorted(known_breaks)[: len(sides) - 1]]
    else:
        lengths = [sum(d for d in s if d) or None for s in sides]
        breaks = find_side_breaks(env, len(sides), lengths if all(lengths) else None, min_gap)
    regions, prev = [], 0.0
    for a, b in breaks:
        regions.append((prev, a))
        prev = b
    regions.append((prev, env.duration))

    edges: list[float] = []
    skips: set[int] = set()
    for (a, b), durations in zip(regions, sides):
        sub, offset = _slice(env, a, b)
        r = split(sub, durations=durations if all(durations) else None, track_count=len(durations), min_gap=min_gap)
        local_edges, local_skips = r.edges_and_skips()
        side_edges = [offset + t for t in local_edges]
        if edges:
            if side_edges[0] > edges[-1] + 0.05:
                skips.add(len(edges) - 1)  # the part from the end of the last side to the start of this one
                base = len(edges)
            else:  # no real pause found: the sides touch
                side_edges = side_edges[1:]
                base = len(edges) - 1
        else:
            base = 0
        skips |= {base + s for s in local_skips}
        edges += side_edges
    return SidesResult(edges, skips, breaks)


# ---------- sample-accurate edges ----------

STEP = 0.001  # analysis step: 1 ms
WINDOW = 0.005  # RMS window: 5 ms
SUSTAIN = 0.015  # sound must be unbroken this long to count (a crackle or pop isn't)
CONTINUE = 0.5  # ...and present for half of the next/previous 0.5 s (a needle drop isn't)
PRE_ROLL = 0.005  # start this far before the first beat so the attack isn't shaved off
TAIL = 0.5  # keep this much after a track's fade reaches the noise floor
START_OVER_FLOOR_DB = 10.0  # a track starts where sound rises this far above the gap's noise
END_OVER_FLOOR_DB = 6.0  # a fade counts until it is within this of the gap's noise
START_MIN_DB = -60.0  # never treat anything quieter than this as a track starting
START_BELOW_MUSIC_DB = 35.0  # ...or anything more than this below the record's music level
END_MIN_DB = -66.0
NEEDLE_UP_DB = -68.0  # quieter than any groove noise: the needle is off the record
BURST_LOOK = 4.0  # a "start" followed within this long by...
BURST_QUIET = 1.0  # ...this much quiet is a click / needle drop, not music
GAP_BELOW_MUSIC_DB = 30.0  # a gap's floor must be this far below the music to be a real silence
LEAD_SEARCH = 10.0  # seconds searched before the first track / after the last for the quiet
SEARCH_PAD = 3.0  # how far past a rough edge to look for the real one


def _short_rms(path: Path, a: float, b: float) -> tuple[np.ndarray, float]:
    """5 ms RMS every 1 ms between a and b seconds. Returns (levels, actual start time)."""
    with sf.SoundFile(path) as f:
        sr = f.samplerate
        a_frame = max(0, int(a * sr))
        f.seek(a_frame)
        data = f.read(max(0, min(f.frames, int(b * sr)) - a_frame), dtype="float32", always_2d=True)
    mono = data.mean(axis=1).astype(np.float64)
    # Forward-only filtering: a zero-phase filter would smear each attack ~40 ms backwards in time,
    # and the high-pass barely delays the clicky part of a beat.
    mono = sosfilt(rumble_filter(sr), mono)
    win, step = max(1, int(WINDOW * sr)), max(1, int(STEP * sr))
    if len(mono) < win:
        return np.zeros(0), a_frame / sr
    csum = np.concatenate(([0.0], np.cumsum(mono**2)))
    starts = np.arange(0, len(mono) - win + 1, step)
    rms = np.sqrt((csum[starts + win] - csum[starts]) / win)
    return rms, a_frame / sr


def _db(x: float) -> float:
    return 20 * np.log10(x + 1e-12)


def _qualifying(above: np.ndarray, forward: bool) -> np.ndarray:
    """Where sound starts (forward) or ends (backward) for real: loud for SUSTAIN without a break,
    and loud for at least half of the following (or preceding) CONTINUE."""
    need, cont = int(SUSTAIN / STEP), int(CONTINUE / STEP)
    n = len(above)
    c = np.concatenate(([0], np.cumsum(above.astype(np.int32))))
    i = np.arange(n)
    if forward:
        sus = (c[np.minimum(i + need, n)] - c[i]) == np.minimum(need, n - i)
        frac = (c[np.minimum(i + cont, n)] - c[i]) / cont
        ok = sus & (i + need <= n)
    else:
        sus = (c[i + 1] - c[np.maximum(i + 1 - need, 0)]) == np.minimum(need, i + 1)
        frac = (c[i + 1] - c[np.maximum(i + 1 - cont, 0)]) / cont
        ok = sus & (i + 1 >= need)
    return ok & (frac >= 0.5)


def _quiet_runs(rms: np.ndarray, thr: float) -> np.ndarray:
    """For each step, how long (in steps) the level stays under thr from there on (100 ms smoothing)."""
    k = max(1, int(0.1 / STEP))
    quiet = np.convolve(rms, np.ones(k) / k, mode="same") <= thr
    runs = np.zeros(len(quiet) + 1, dtype=np.int64)
    for j in range(len(quiet) - 1, -1, -1):
        runs[j] = runs[j + 1] + 1 if quiet[j] else 0
    return runs[:-1]


def _isolated(runs: np.ndarray, i: int, forward: bool) -> bool:
    """True if a second or more of quiet follows (or precedes) i closely: i is a click, a needle
    drop or a needle lift, not the start (or end) of a track."""
    look, need = int(BURST_LOOK / STEP), int(BURST_QUIET / STEP)
    if forward:
        seg = runs[i:min(len(runs), i + look)]
        return bool(len(seg) and seg.max() >= need)
    seg = runs[max(0, i - look):max(0, i - need)]
    return bool(len(seg) and seg.max() >= need)


def _gap_floor(rms: np.ndarray, ia: int, ib: int, q: int) -> float:
    """The noise level of a gap: the groove noise. If the needle was lifted during the gap (a flip,
    or before the first track), that much quieter stretch is left out, because tracks start and
    end relative to the groove noise around them, not to the silence of a lifted needle."""
    k = max(1, int(0.2 / STEP))
    level = _db(np.convolve(rms, np.ones(k) / k, mode="same")[ia:ib])
    lifted = level < NEEDLE_UP_DB
    if lifted.sum() * STEP >= 1.0 and (~lifted).sum() * STEP >= 1.0:
        return float(10 ** (np.percentile(level[~lifted], 30) / 20))
    return float(np.percentile(rms[max(0, q - 250):q + 250], 30))


def snap_gap(path: Path, a: float, b: float, music_db: float | None = None, before: float = 0.6,
             after: float = 0.6) -> tuple[float | None, float | None]:
    """Find the real (end of previous track, start of next track) around a rough gap [a, b].

    The gap's noise floor is the groove noise at its quiet point. The next track starts where
    sound rises 10 dB above it (and well within range of the music level) and keeps going; the
    previous one ends where its fade last stood 6 dB above it (plus TAIL). Clicks, needle drops
    and needle lifts are skipped because they're followed (or preceded) by a second of quiet.
    Either result is None if it can't be found.
    """
    # The rough edges can be a few seconds off either way, so search a little beyond them
    # (unless told not to look at all on that side).
    before = max(before, SEARCH_PAD) if before > 0 else 0.0
    after = max(after, SEARCH_PAD) if after > 0 else 0.0
    pad = BURST_LOOK + 0.5  # read enough around the search to judge bursts near its edges
    rms, t0 = _short_rms(path, max(0.0, a - before - pad), b + after + pad)
    if len(rms) < 20:
        return None, None
    lo_i = int(np.clip((a - before - t0) / STEP, 0, len(rms) - 1))
    hi_i = int(np.clip((b + after - t0) / STEP, lo_i + 1, len(rms)))
    ia = int(np.clip((a - t0) / STEP, 0, len(rms) - 1))
    ib = int(np.clip((b - t0) / STEP, ia + 1, len(rms)))
    k = max(1, int(0.2 / STEP))
    q = ia + int(np.argmin(np.convolve(rms, np.ones(k) / k, mode="same")[ia:ib]))
    floor = _gap_floor(rms, ia, ib, q)
    if music_db is not None and _db(floor) > music_db - GAP_BELOW_MUSIC_DB:
        return None, None  # not a real silence (e.g. a split placed by estimate in the music)
    start_db = max(_db(floor) + START_OVER_FLOOR_DB, START_MIN_DB,
                   (music_db - START_BELOW_MUSIC_DB) if music_db is not None else -np.inf)
    start_thr = 10 ** (start_db / 20)
    end_thr = max(floor * 10 ** (END_OVER_FLOOR_DB / 20), 10 ** (END_MIN_DB / 20))

    start = end = None
    runs = _quiet_runs(rms, start_thr)
    for i in np.flatnonzero(_qualifying(rms > start_thr, forward=True)[q:hi_i]) + q:
        if not _isolated(runs, i, forward=True):
            # The first loud window only just overlaps the attack, so the beat is about a window later.
            start = t0 + i * STEP + WINDOW - PRE_ROLL
            break
    runs = _quiet_runs(rms, end_thr)
    for i in (np.flatnonzero(_qualifying(rms > end_thr, forward=False)[lo_i:q]) + lo_i)[::-1]:
        if not _isolated(runs, i, forward=False):
            end = t0 + i * STEP + WINDOW + TAIL
            break
    if start is not None and end is not None:
        end = min(end, start - 0.05)
    return end, start


def refine_edges(path: Path, edges: list[float], skips: set[int], music_db: float | None = None) -> list[float]:
    """Snap track starts to the first beat and track ends to the end of their fade, using the samples.

    Each skipped part (gap, flip) is measured on its own. The first edge is snapped against the
    quiet before it (needle down) and the last against the quiet after it (run-out groove).
    Edges between two touching tracks are left alone.
    """
    out = list(edges)
    last = len(edges) - 1
    if last < 1:
        return out
    duration = sf.info(path).duration
    for k in sorted(skips):
        if not 0 <= k < last:
            continue
        end, start = snap_gap(path, edges[k], edges[k + 1], music_db)
        if end is not None and k > 0 and (k - 1) not in skips:
            out[k] = end
        if start is not None and k + 1 < last and (k + 1) not in skips:
            out[k + 1] = start
    if 0 not in skips and edges[0] > 0.3:
        _, start = snap_gap(path, max(0.0, edges[0] - LEAD_SEARCH), edges[0], music_db, before=0.0)
        if start is not None:
            out[0] = start
    if (last - 1) not in skips and duration - edges[last] > 0.3:
        end, _ = snap_gap(path, edges[last], min(duration, edges[last] + LEAD_SEARCH), music_db, after=0.0)
        if end is not None:
            out[last] = end
    # Keep everything in order; where snapping crossed a neighbour, fall back to the rough edge.
    for i in range(1, last + 1):
        if out[i] <= out[i - 1] + 0.05:
            out[i] = max(edges[i], out[i - 1] + 0.05)
    return out


def _quietest_near(env: Envelope, t: float, radius: float) -> float:
    a = max(0, int((t - radius) / env.hop))
    b = min(len(env.rms_db), int((t + radius) / env.hop) + 1)
    if b <= a:
        return t
    return (a + int(np.argmin(env.rms_db[a:b]))) * env.hop
