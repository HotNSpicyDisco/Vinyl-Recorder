"""Match the parts of a recording (between split lines) to Discogs tracks.

By default the parts take the tracks in order: the take's sides in the chosen order
(A then B, or B then A for a record played backwards), each side's tracks in order.
You can set the track for any part; the parts after it then carry on from that track,
so fixing the first part of a side usually fixes the whole side.

Overrides are anchored to a time in the recording, not a part number, so they stay with
their part when split lines are moved, added or removed.
"""

from .discogs import Track

Row = tuple[int | None, tuple[float, float] | None, Track | None]  # (part index, (start, end), track)


def sequence(tracks: list[Track], side_order: list[str]) -> list[Track]:
    """The take's tracks in playing order: sides in side_order, each side in its own order."""
    return [t for s in side_order for t in tracks if t.side == s]


def assign(edges: list[float], skips: set[int], seq: list[Track], all_tracks: list[Track],
           overrides: dict[float, int]) -> list[Row]:
    """Pair parts with tracks.

    edges: split lines (music start ... music end); skips: parts that aren't tracks;
    seq: the take's tracks in playing order; all_tracks: every track on the release (a part
    can be set to any of them); overrides: {time inside a part: track number}.
    Tracks in seq that no part got are listed at the end with no part.
    """
    by_number = {t.number: t for t in all_tracks}
    parts = list(zip(edges[:-1], edges[1:]))
    rows: list[Row] = []
    order, pos = seq, -1  # where the next part continues from
    used: set[int] = set()
    for i, (a, b) in enumerate(parts):
        if i in skips:
            rows.append((i, (a, b), None))
            continue
        chosen = next((n for t, n in overrides.items() if a <= t < b), None)
        if chosen in by_number:
            track = by_number[chosen]
            # Continue from it: in the take's order if it's on these sides, else in release order
            order = seq if track in seq else all_tracks
            pos = order.index(track)
        else:
            # The next track not used yet, wrapping round to the start: pick B1 for the first part of a
            # record played B side first and the parts after the flip become A1, A2...
            nxt = next((k for k in [*range(pos + 1, len(order)), *range(0, pos + 1)]
                        if order[k].number not in used), None)
            track = order[nxt] if nxt is not None else None
            pos = nxt if nxt is not None else len(order)
        rows.append((i, (a, b), track))
        if track:
            used.add(track.number)
    rows += [(None, None, t) for t in seq if t.number not in used]
    return rows


def set_override(overrides: dict[float, int], edges: list[float], part: int, track_number: int | None) -> None:
    """Make `part` be the given track (None: back to automatic), replacing any choice already in that part."""
    a, b = edges[part], edges[part + 1]
    for t in [t for t in overrides if a <= t < b]:
        del overrides[t]
    if track_number is not None:
        overrides[(a + b) / 2] = track_number
