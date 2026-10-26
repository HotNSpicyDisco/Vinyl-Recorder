"""User settings, stored as JSON in %APPDATA%\\VinylRecorder\\settings.json."""

import json
import os
from dataclasses import asdict, dataclass, field
from pathlib import Path


def _settings_dir() -> Path:
    base = os.environ.get("APPDATA") or str(Path.home())
    return Path(base) / "VinylRecorder"


SETTINGS_PATH = _settings_dir() / "settings.json"


@dataclass
class Settings:
    discogs_token: str = ""
    discogs_username: str = ""  # filled in from the token on first collection load
    output_dir: str = str(Path.home() / "Music" / "Vinyl Rips")
    device_name: str = "Focusrite USB ASIO"
    # 1-based input numbers as printed on the interface
    left_input: int = 3
    right_input: int = 4
    sample_rate: int = 96000
    vinyl_only_search: bool = True
    min_gap_seconds: float = 0.8
    export_format: str = "flac"  # "flac", "mp3" or "both"
    detect_bpm: bool = True
    bpm_low: float = 78.0  # BPM results fall in [bpm_low, 2 * bpm_low)
    offered_shortcuts: bool = False  # the packaged app asks once about Start menu/desktop shortcuts
    extra: dict = field(default_factory=dict)

    @property
    def recordings_dir(self) -> Path:
        return Path(self.output_dir) / "_recordings"

    def token(self) -> str:
        return self.discogs_token.strip() or os.environ.get("DISCOGS_TOKEN", "").strip()

    def save(self) -> None:
        SETTINGS_PATH.parent.mkdir(parents=True, exist_ok=True)
        SETTINGS_PATH.write_text(json.dumps(asdict(self), indent=2), encoding="utf-8")

    @classmethod
    def load(cls) -> "Settings":
        try:
            data = json.loads(SETTINGS_PATH.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return cls()
        known = {k: v for k, v in data.items() if k in cls.__dataclass_fields__}
        return cls(**known)
