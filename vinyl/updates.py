"""Checking GitHub for a newer release.

Only asks GitHub which release is the latest (no personal data is sent), at most once a day.
"""

import platform
import sys
from dataclasses import dataclass
from datetime import datetime, timedelta

import requests

from . import HOMEPAGE, __version__

LATEST_API = "https://api.github.com/repos/HotNSpicyDisco/Vinyl-Recorder/releases/latest"
CHECK_EVERY = timedelta(days=1)


@dataclass
class Update:
    version: str  # "1.2.1"
    page: str  # the release page
    download: str  # the zip for this computer (the release page if there isn't one)
    notes: str = ""


def parse_version(tag: str) -> tuple[int, ...]:
    """'v1.2.0' -> (1, 2, 0). Anything that isn't a number counts as 0."""
    parts = []
    for p in tag.strip().lstrip("vV").split("."):
        digits = "".join(c for c in p if c.isdigit())
        parts.append(int(digits) if digits else 0)
    while len(parts) > 1 and parts[-1] == 0:
        parts.pop()
    return tuple(parts)


def is_newer(tag: str, current: str = __version__) -> bool:
    return parse_version(tag) > parse_version(current)


def asset_name(system: str = sys.platform, machine: str = platform.machine()) -> str:
    """The release zip for this kind of computer."""
    if system == "darwin":
        return "Vinyl-Recorder-mac-apple-silicon.zip" if machine.lower() in ("arm64", "aarch64") \
            else "Vinyl-Recorder-mac-intel.zip"
    return "Vinyl-Recorder-windows.zip"


def due(last_check: str, now: datetime | None = None) -> bool:
    """Whether a day has passed since the last check (an ISO time, or "" for never)."""
    try:
        return (now or datetime.now()) - datetime.fromisoformat(last_check) >= CHECK_EVERY
    except ValueError:
        return True


def from_release(data: dict, current: str = __version__) -> Update | None:
    """The update described by GitHub's release JSON, or None if it isn't newer than this version."""
    tag = data.get("tag_name") or ""
    if not tag or data.get("draft") or data.get("prerelease") or not is_newer(tag, current):
        return None
    page = data.get("html_url") or f"{HOMEPAGE}/releases/latest"
    want = asset_name()
    download = next((a.get("browser_download_url") for a in data.get("assets", []) if a.get("name") == want), page)
    return Update(tag.lstrip("vV"), page, download, (data.get("body") or "").strip())


def check(timeout: float = 10) -> Update | None:
    """Ask GitHub for the latest release. Raises requests.RequestException if GitHub can't be reached."""
    r = requests.get(LATEST_API, timeout=timeout, headers={
        "Accept": "application/vnd.github+json", "User-Agent": f"VinylRecorder/{__version__}"})
    r.raise_for_status()
    return from_release(r.json())
