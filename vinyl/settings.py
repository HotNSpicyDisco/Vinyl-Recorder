"""User settings, stored as JSON in the per-user app data folder:

Windows: %APPDATA%\\VinylRecorder   macOS: ~/Library/Application Support/VinylRecorder
Linux:   ~/.config/VinylRecorder
"""

import json
import os
import sys
from dataclasses import asdict, dataclass, field
from pathlib import Path


def _settings_dir() -> Path:
    if sys.platform == "win32":
        base = Path(os.environ.get("APPDATA") or Path.home())
    elif sys.platform == "darwin":
        base = Path.home() / "Library" / "Application Support"
    else:
        base = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config")
    return base / "VinylRecorder"


SETTINGS_PATH = _settings_dir() / "settings.json"


@dataclass
class Settings:
    discogs_token: str = ""
    discogs_username: str = ""  # filled in from the token on first collection load
    output_dir: str = str(Path.home() / "Music" / "Vinyl Rips")
    device_name: str = ""  # remembered after the first successful connection
    # 1-based input numbers as printed on the interface
    left_input: int = 1
    right_input: int = 2
    sample_rate: int = 96000  # falls back to 48/44.1 kHz if the interface can't do it
    vinyl_only_search: bool = True
    min_gap_seconds: float = 0.8
    export_format: str = "flac"  # "flac", "mp3" or "both"
    name_pattern: str = "{artist}/{album} ({year})/{number} - {title}"  # see vinyl.naming
    detect_bpm: bool = True
    bpm_low: float = 78.0  # BPM results fall in [bpm_low, 2 * bpm_low)
    offered_shortcuts: bool = False  # the packaged Windows app asks once about Start menu/desktop shortcuts
    hands_free: bool = True  # wait for the needle, pause between sides, stop after the last side
    rekordbox: bool = True  # add saved albums to a rekordbox XML library
    rekordbox_prefer: str = "flac"  # when both FLAC and MP3 are saved: which Rekordbox gets
    queue_labels: bool = True  # add each saved album to the label queue
    label_template: str = "avery_5163"
    label_printers: dict = field(default_factory=dict)  # template id -> printer last used for it
    label_rotate: bool = False  # thermal rolls: turn labels 90° for drivers that feed sideways
    extra: dict = field(default_factory=dict)

    @property
    def recordings_dir(self) -> Path:
        return Path(self.output_dir) / "_recordings"

    @property
    def rekordbox_xml(self) -> Path:
        return Path(self.output_dir) / "rekordbox.xml"

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
