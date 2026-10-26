"""Add (or remove) Vinyl Recorder shortcuts in the Start menu and on the desktop.

The shortcuts start the app with pythonw (no console window), use assets/icon.ico, and carry
the same AppUserModelID the app sets, so pinning to the taskbar keeps the right icon.

Run:    .venv\\Scripts\\python.exe tools\\install_shortcuts.py [--no-desktop]
Remove: .venv\\Scripts\\python.exe tools\\install_shortcuts.py --remove
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from vinyl import shortcuts  # noqa: E402


def main():
    if "--remove" in sys.argv:
        for path in shortcuts.remove():
            print("removed", path)
        return
    target = shortcuts.launch_command()[0]
    if not target.exists():
        sys.exit(f"Can't find {target}. Run launch.bat once first to set up the app.")
    for path in shortcuts.install(desktop="--no-desktop" not in sys.argv):
        print("created", path)


if __name__ == "__main__":
    main()
