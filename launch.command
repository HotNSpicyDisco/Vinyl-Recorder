#!/bin/bash
# macOS: run Vinyl Recorder from the source code (double-click in Finder, or run in Terminal).
# The first run sets everything up. Needs Python 3.11+ (python.org installer or Homebrew).
cd "$(dirname "$0")"
if [ ! -x .venv/bin/python ]; then
    echo "Setting up for first run..."
    python3 -m venv .venv && .venv/bin/python -m pip install -q -r requirements.txt || {
        echo "Setup failed. Install Python 3.11+ from python.org and try again."; read -r; exit 1; }
fi
exec .venv/bin/python run.py
