"""Start menu / desktop shortcuts (Windows), the taskbar identity and where the app's own files live.

Works both from source (pythonw run.py) and as the packaged app. On macOS there are no
shortcuts to make: the packaged app goes in Applications like any other.
"""

import os
import sys
from pathlib import Path

APP_ID = "HotNSpicyDisco.VinylRecorder"  # taskbar identity; shortcuts carry the same one
NAME = "Vinyl Recorder.lnk"
FROZEN = getattr(sys, "frozen", False)  # True inside the packaged app
SUPPORTED = sys.platform == "win32"


def resource_dir() -> Path:
    """Folder holding assets/ (inside the package when frozen, the project folder otherwise)."""
    return Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parents[1]))


ICON = resource_dir() / "assets" / "icon.ico"


def launch_command() -> tuple[Path, str, Path, Path]:
    """(target, arguments, working folder, icon) for a shortcut that starts this app."""
    if FROZEN:
        exe = Path(sys.executable)
        return exe, "", exe.parent, exe  # the exe carries the icon itself
    root = Path(__file__).resolve().parents[1]
    return root / ".venv" / "Scripts" / "pythonw.exe", f'"{root / "run.py"}"', root, ICON


def locations(desktop: bool = True) -> list[Path]:
    import win32com.client

    start_menu = Path(os.environ["APPDATA"]) / "Microsoft" / "Windows" / "Start Menu" / "Programs"
    places = [start_menu / NAME]
    if desktop:
        shell = win32com.client.Dispatch("WScript.Shell")
        places.append(Path(shell.SpecialFolders("Desktop")) / NAME)  # follows OneDrive desktop redirection
    return places


def make_shortcut(path: Path) -> None:
    import pythoncom
    import win32com.client
    from win32com.propsys import propsys, pscon
    from win32com.shell import shellcon

    target, args, workdir, icon = launch_command()
    path.parent.mkdir(parents=True, exist_ok=True)
    sc = win32com.client.Dispatch("WScript.Shell").CreateShortcut(str(path))
    sc.TargetPath = str(target)
    sc.Arguments = args
    sc.WorkingDirectory = str(workdir)
    sc.IconLocation = f"{icon},0"
    sc.Description = "Record vinyl, look it up on Discogs, split and tag the tracks"
    sc.Save()
    # Same AppUserModelID as the running app, so a pinned shortcut and the open window share one taskbar button
    store = propsys.SHGetPropertyStoreFromParsingName(str(path), None, shellcon.GPS_READWRITE,
                                                      propsys.IID_IPropertyStore)
    store.SetValue(pscon.PKEY_AppUserModel_ID, propsys.PROPVARIANTType(APP_ID, pythoncom.VT_LPWSTR))
    store.Commit()


def install(desktop: bool = True) -> list[Path]:
    made = []
    for path in locations(desktop):
        make_shortcut(path)
        made.append(path)
    return made


def remove() -> list[Path]:
    gone = []
    for path in locations(desktop=True):
        if path.exists():
            path.unlink()
            gone.append(path)
    return gone


def start_menu_shortcut_exists() -> bool:
    if not SUPPORTED:
        return False
    return (Path(os.environ["APPDATA"]) / "Microsoft" / "Windows" / "Start Menu" / "Programs" / NAME).exists()
