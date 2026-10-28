"""Matching recording parts to tracks: side order, picking a track, carrying on after it."""

from vinyl.discogs import Track
from vinyl.tracklist import assign, sequence, set_override

TRACKS = [Track("A1", "a1", 300, "", 1), Track("A2", "a2", 300, "", 2),
          Track("B1", "b1", 300, "", 3), Track("B2", "b2", 300, "", 4)]


# Whole LP: track, gap, track, flip, track, gap, track
EDGES = [0, 300, 302, 600, 620, 920, 922, 1220]
SKIPS = {1, 3, 5}


def names(rows, skips=SKIPS):
    """'—' for a gap/flip, '?' for a part with no track, else the track's position.
    Tracks no part got come last (with no part)."""
    return ["—" if p in skips else (t.position if t else "?") for p, _, t in rows]


def test_default_order():
    rows = assign(EDGES, SKIPS, sequence(TRACKS, ["A", "B"]), TRACKS, {})
    assert names(rows) == ["A1", "—", "A2", "—", "B1", "—", "B2"]


def test_record_played_b_side_first():
    rows = assign(EDGES, SKIPS, sequence(TRACKS, ["B", "A"]), TRACKS, {})
    assert names(rows) == ["B1", "—", "B2", "—", "A1", "—", "A2"]


def test_picking_a_track_carries_on_from_it():
    overrides = {}
    set_override(overrides, EDGES, 0, 3)  # first part is really B1
    rows = assign(EDGES, SKIPS, sequence(TRACKS, ["A", "B"]), TRACKS, overrides)
    # B1, B2, then it wraps round to the tracks not used yet: the record was played B side first
    assert names(rows) == ["B1", "—", "B2", "—", "A1", "—", "A2"]
    set_override(overrides, EDGES, 4, 1)  # saying so explicitly gives the same
    rows = assign(EDGES, SKIPS, sequence(TRACKS, ["A", "B"]), TRACKS, overrides)
    assert names(rows) == ["B1", "—", "B2", "—", "A1", "—", "A2"]


def test_override_survives_moving_and_adding_splits():
    overrides = {}
    set_override(overrides, EDGES, 2, 4)  # second track is B2 (anchored at 451 s)
    moved = [0, 310, 312, 600, 620, 920, 922, 1220]  # first split dragged later
    rows = assign(moved, SKIPS, sequence(TRACKS, ["A", "B"]), TRACKS, overrides)
    assert names(rows)[2] == "B2"
    split = [0, 150, 300, 302, 600, 620, 920, 922, 1220]  # an extra split in the first part
    rows = assign(split, {2, 4, 6}, sequence(TRACKS, ["A", "B"]), TRACKS, overrides)
    assert names(rows, {2, 4, 6})[:4] == ["A1", "A2", "—", "B2"]


def test_more_parts_than_tracks():
    rows = assign([0, 100, 200, 300], set(), sequence(TRACKS[:2], ["A"]), TRACKS, {})
    assert names(rows, set()) == ["A1", "A2", "?"]


def test_clearing_an_override():
    overrides = {}
    set_override(overrides, EDGES, 0, 4)
    set_override(overrides, EDGES, 0, None)
    assert overrides == {}
    assert names(assign(EDGES, SKIPS, sequence(TRACKS, ["A", "B"]), TRACKS, overrides))[0] == "A1"
