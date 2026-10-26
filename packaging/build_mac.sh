#!/bin/bash
# Build "Vinyl Recorder.app" on a Mac and zip it as dist/Vinyl-Recorder-mac.zip
set -euo pipefail
cd "$(dirname "$0")/.."
if [ ! -x .venv/bin/python ]; then
    python3 -m venv .venv
fi
.venv/bin/python -m pip install -q -r requirements.txt pyinstaller
.venv/bin/python -m PyInstaller --noconfirm --clean --distpath dist --workpath build packaging/vinyl_recorder.spec
cp "packaging/Read me first.txt" "dist/Read me first.txt"
# ditto keeps the app's signature and symlinks intact (a plain zip breaks them)
rm -f dist/Vinyl-Recorder-mac.zip
ditto -c -k --keepParent "dist/Vinyl Recorder.app" dist/Vinyl-Recorder-mac.zip
echo "Done: dist/Vinyl-Recorder-mac.zip"
