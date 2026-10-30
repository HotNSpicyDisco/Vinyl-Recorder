"""Minimal Discogs API client: search releases, fetch tracklists and cover art.

Searching needs a personal access token (discogs.com/settings/developers).
"""

import re
import time
from dataclasses import dataclass, field
from urllib.parse import quote

import requests

from . import HOMEPAGE, __version__

API = "https://api.discogs.com"
USER_AGENT = f"VinylRecorder/{__version__} +{HOMEPAGE}"  # Discogs asks apps to identify themselves


class DiscogsError(Exception):
    pass


@dataclass
class SearchResult:
    id: int
    title: str  # "Artist - Album"
    year: str
    country: str
    formats: list[str]
    label: str
    catno: str
    thumb: str

    @property
    def subtitle(self) -> str:
        parts = [self.year, self.country, self.label, self.catno, ", ".join(self.formats)]
        return " · ".join(p for p in parts if p)


@dataclass
class CollectionItem:
    """One record in the user's Discogs collection."""

    id: int
    artist: str
    title: str
    year: str
    label: str
    catno: str
    formats: list[str]
    thumb: str
    added: str  # ISO date it was added to the collection

    @property
    def name(self) -> str:
        return f"{self.artist} – {self.title}"

    @property
    def subtitle(self) -> str:
        parts = [self.year if self.year != "0" else "", f"{self.label} {self.catno}".strip(), ", ".join(self.formats)]
        return " · ".join(p for p in parts if p)

    @property
    def is_vinyl(self) -> bool:
        return "Vinyl" in self.formats

    @property
    def is_lp(self) -> bool:
        """An album-length LP (Discogs says "LP"), as opposed to a 12", EP or single."""
        return "LP" in self.formats

    def matches(self, text: str) -> bool:
        hay = f"{self.artist} {self.title} {self.label} {self.catno} {self.year}".lower()
        return all(word in hay for word in text.lower().split())


@dataclass
class Track:
    position: str  # "A1", "B2", "1", ...
    title: str
    duration: float | None  # seconds
    artist: str  # empty unless the track artist differs from the album artist
    number: int  # 1-based order on the release

    @property
    def side(self) -> str:
        m = re.match(r"([A-Za-z]+)", self.position)
        return m.group(1).upper() if m else "1"


@dataclass
class Release:
    id: int
    artist: str
    title: str
    year: str
    genres: list[str]
    styles: list[str]
    label: str
    catno: str
    tracks: list[Track]
    cover_url: str
    url: str
    cover: bytes | None = field(default=None, repr=False)

    @property
    def sides(self) -> list[str]:
        seen = []
        for t in self.tracks:
            if t.side not in seen:
                seen.append(t.side)
        return seen

    def tracks_on(self, side: str) -> list[Track]:
        return [t for t in self.tracks if t.side == side]


def clean_name(name: str) -> str:
    """Drop Discogs disambiguation suffixes: 'Prince (2)' -> 'Prince'."""
    return re.sub(r"\s+\(\d+\)$", "", name.strip()).rstrip("*").strip()


def join_artists(artists: list[dict]) -> str:
    out = ""
    for i, a in enumerate(artists):
        out += clean_name(a.get("anv") or a["name"])
        if i < len(artists) - 1:
            join = (a.get("join") or "").strip()
            out += ", " if join == "," else f" {join} " if join else " & "
    return out


def parse_duration(text: str) -> float | None:
    if not text:
        return None
    try:
        parts = [int(p) for p in text.strip().split(":")]
    except ValueError:
        return None
    secs = 0
    for p in parts:
        secs = secs * 60 + p
    return float(secs) or None


class Client:
    def __init__(self, token: str = ""):
        self.session = requests.Session()
        self.session.headers["User-Agent"] = USER_AGENT
        if token:
            self.session.headers["Authorization"] = f"Discogs token={token}"
        self.has_token = bool(token)

    def _get(self, url: str, **params):
        for attempt in range(4):
            try:
                r = self.session.get(url if url.startswith("http") else API + url, params=params, timeout=20)
            except requests.RequestException as e:
                raise DiscogsError(f"Could not reach Discogs: {e}") from e
            if r.status_code != 429 or attempt == 3:
                break
            time.sleep(5 * (attempt + 1))  # rate limited (60 requests/minute): back off and retry
        if r.status_code == 401:
            raise DiscogsError("Discogs rejected the token. Check it in Settings.")
        if r.status_code == 403:
            raise DiscogsError("Discogs refused access. Is the collection private to another account?")
        if r.status_code == 429:
            raise DiscogsError("Discogs rate limit hit. Wait a minute and try again.")
        if not r.ok:
            raise DiscogsError(f"Discogs error {r.status_code}: {r.text[:200]}")
        return r

    def search(self, query: str, vinyl_only: bool = True) -> list[SearchResult]:
        if not self.has_token:
            raise DiscogsError("Searching Discogs needs a personal access token. Add one in Settings.")
        params = {"q": query, "type": "release", "per_page": 50}
        if vinyl_only:
            params["format"] = "Vinyl"
        data = self._get("/database/search", **params).json()
        results = []
        for r in data.get("results", []):
            results.append(
                SearchResult(
                    id=r["id"],
                    title=r.get("title", ""),
                    year=str(r.get("year", "") or ""),
                    country=r.get("country", "") or "",
                    formats=list(dict.fromkeys(r.get("format", []) or [])),
                    label=(r.get("label") or [""])[0],
                    catno=r.get("catno", "") or "",
                    thumb=r.get("thumb", "") or "",
                )
            )
        return results

    def identity(self) -> str:
        """Username of the token's owner."""
        if not self.has_token:
            raise DiscogsError("Loading your collection needs your Discogs token. Add it in Settings.")
        return self._get("/oauth/identity").json()["username"]

    def collection(self, username: str, progress=None) -> list[CollectionItem]:
        """Every release in the user's collection ("All" folder), one entry per release."""
        items: dict[int, CollectionItem] = {}
        page, pages = 1, 1
        while page <= pages:
            data = self._get(f"/users/{quote(username)}/collection/folders/0/releases",
                             page=page, per_page=100, sort="artist", sort_order="asc").json()
            pages = data.get("pagination", {}).get("pages", 1)
            for r in data.get("releases", []):
                b = r.get("basic_information", {})
                if not b.get("id") or b["id"] in items:
                    continue
                labels = b.get("labels") or [{}]
                formats = []
                for f in b.get("formats", []) or []:
                    formats += [f.get("name", "")] + list(f.get("descriptions", []) or [])
                items[b["id"]] = CollectionItem(
                    id=b["id"],
                    artist=join_artists(b.get("artists", [])) or "Unknown Artist",
                    title=b.get("title", ""),
                    year=str(b.get("year", "") or ""),
                    label=clean_name(labels[0].get("name", "")),
                    catno=labels[0].get("catno", "") or "",
                    formats=list(dict.fromkeys(f for f in formats if f)),
                    thumb=b.get("thumb", "") or "",
                    added=(r.get("date_added") or "")[:10],
                )
            if progress:
                progress(page, pages)
            page += 1
        return list(items.values())

    def release(self, release_id: int) -> Release:
        d = self._get(f"/releases/{release_id}").json()
        artist = join_artists(d.get("artists", [])) or "Unknown Artist"
        tracks = []
        for t in d.get("tracklist", []):
            if t.get("type_", "track") != "track":
                continue
            track_artist = join_artists(t.get("artists", [])) if t.get("artists") else ""
            tracks.append(
                Track(
                    position=t.get("position", "") or str(len(tracks) + 1),
                    title=t.get("title", "") or f"Track {len(tracks) + 1}",
                    duration=parse_duration(t.get("duration", "")),
                    artist="" if track_artist == artist else track_artist,
                    number=len(tracks) + 1,
                )
            )
        images = d.get("images", []) or []
        primary = next((i for i in images if i.get("type") == "primary"), images[0] if images else None)
        labels = d.get("labels", []) or [{}]
        return Release(
            id=d["id"],
            artist=artist,
            title=d.get("title", ""),
            year=str(d.get("year", "") or ""),
            genres=d.get("genres", []) or [],
            styles=d.get("styles", []) or [],
            label=clean_name(labels[0].get("name", "")),
            catno=labels[0].get("catno", "") or "",
            tracks=tracks,
            cover_url=(primary or {}).get("uri", ""),
            url=d.get("uri", ""),
        )

    def image(self, url: str) -> bytes | None:
        if not url:
            return None
        try:
            return self._get(url).content
        except DiscogsError:
            return None
