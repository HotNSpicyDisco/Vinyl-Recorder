"""Collection download parsing and the local record of what has been recorded/saved."""

from pathlib import Path

from vinyl import discogs, library
from vinyl.discogs import Release, Track


class FakeResponse:
    def __init__(self, data):
        self.data = data

    def json(self):
        return self.data


def entry(rid, artist, title, fmt="Vinyl", added="2024-05-01T10:00:00-07:00"):
    return {
        "id": rid, "date_added": added,
        "basic_information": {
            "id": rid, "title": title, "year": 1979, "thumb": f"https://i.discogs.com/{rid}.jpg",
            "artists": [{"name": artist, "join": ""}],
            "labels": [{"name": "Salsoul (2)", "catno": f"SA {rid}"}],
            "formats": [{"name": fmt, "qty": "1", "descriptions": ["LP", "Album"]}],
        },
    }


def test_collection_pages_and_duplicates(monkeypatch):
    pages = {
        1: {"pagination": {"pages": 2}, "releases": [entry(1, "Chic", "C'est Chic"), entry(2, "Loleatta Holloway", "Queen")]},
        2: {"pagination": {"pages": 2}, "releases": [entry(1, "Chic", "C'est Chic"),  # second copy
                                                      entry(3, "Sade", "Diamond Life", fmt="CD")]},
    }
    calls = []

    def fake_get(self, url, **params):
        calls.append((url, params["page"]))
        return FakeResponse(pages[params["page"]])

    monkeypatch.setattr(discogs.Client, "_get", fake_get)
    seen = []
    items = discogs.Client("tok").collection("disco fan", lambda p, n: seen.append((p, n)))
    assert calls == [("/users/disco%20fan/collection/folders/0/releases", 1),
                     ("/users/disco%20fan/collection/folders/0/releases", 2)]
    assert seen == [(1, 2), (2, 2)]
    assert [i.id for i in items] == [1, 2, 3]
    chic = items[0]
    assert chic.label == "Salsoul" and chic.catno == "SA 1" and chic.added == "2024-05-01"
    assert chic.formats == ["Vinyl", "LP", "Album"] and chic.is_vinyl and not items[2].is_vinyl
    assert chic.matches("chic salsoul") and not chic.matches("chic sade")


def release(rid=10, sides=("A", "B")):
    tracks = [Track(f"{s}1", f"Song {s}", 200, "", n + 1) for n, s in enumerate(sides)]
    return Release(rid, "Chic", "Risqué", "1979", [], [], "Atlantic", "SD 16003", tracks, "", "")


def test_riplog_statuses(tmp_path: Path):
    log = library.RipLog(tmp_path / "rip_log.json")
    rel = release()
    assert log.status(rel.id) is None and log.describe(rel.id) == ""

    log.mark_recorded(rel, "A")
    assert log.status(rel.id) == library.PARTIAL
    assert log.describe(rel.id) == "Side A recorded, not saved yet"

    log.mark_saved(rel, "flac", tmp_path, ["A"])
    assert log.status(rel.id) == library.PARTIAL  # side B still to do
    assert "side B still to do" in log.describe(rel.id)

    log.mark_recorded(rel, "B")
    log.mark_saved(rel, "mp3", tmp_path, ["B"])
    assert log.status(rel.id) == library.SAVED
    assert log.describe(rel.id).endswith("(FLAC, MP3)")

    again = library.RipLog(tmp_path / "rip_log.json")  # persisted
    assert again.status(rel.id) == library.SAVED
    again.forget(rel.id)
    assert library.RipLog(tmp_path / "rip_log.json").status(rel.id) is None


def test_tracklist_corrections_are_kept(tmp_path: Path):
    """Discogs says A / B1 / B2 but the record really is A1 / A2 / B1: fix it once, it stays fixed."""
    from dataclasses import replace

    discogs_version = Release(42, "DJ X", "Promo", "1992", [], [], "Lbl", "L1",
                              [Track("A", "First", 400, "", 1), Track("B1", "Second", 300, "", 2),
                               Track("B2", "Third", 280, "", 3)], "", "", cover=b"jpg")
    fixed = replace(discogs_version, tracks=[Track("A1", "First", 400, "", 1), Track("A2", "Second", 300, "", 2),
                                             Track("B1", "Third", 280, "", 3)])
    edits = library.ReleaseEdits(tmp_path / "edits.json")
    assert edits.apply(discogs_version) is discogs_version  # nothing saved yet
    edits.save(fixed)
    again = library.ReleaseEdits(tmp_path / "edits.json").apply(discogs_version)  # e.g. next time it's opened
    assert [t.position for t in again.tracks] == ["A1", "A2", "B1"]
    assert again.sides == ["A", "B"] and [t.title for t in again.tracks_on("A")] == ["First", "Second"]
    assert again.cover == b"jpg" and again.id == 42
    edits.forget(42)
    assert library.ReleaseEdits(tmp_path / "edits.json").apply(discogs_version) is discogs_version


def test_collection_cache_roundtrip(tmp_path: Path):
    cache = library.CollectionCache(tmp_path / "c.json")
    assert cache.items == []
    item = discogs.CollectionItem(5, "Chic", "Risqué", "1979", "Atlantic", "SD 1", ["Vinyl"], "", "2024-01-01")
    cache.save("me", [item])
    back = library.CollectionCache(tmp_path / "c.json")
    assert back.username == "me" and back.items == [item] and back.updated


def test_lp_or_not():
    from vinyl.discogs import CollectionItem

    lp = CollectionItem(1, "A", "Album", "1980", "L", "C", ["Vinyl", "LP", "Album"], "", "")
    twelve = CollectionItem(2, "A", "Single", "1980", "L", "C", ["Vinyl", '12"', "33 ⅓ RPM"], "", "")
    assert lp.is_lp and not twelve.is_lp
