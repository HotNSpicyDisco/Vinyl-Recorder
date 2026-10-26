@echo off
rem First-time setup (or repair). After this, open Vinyl Recorder from the Start menu or desktop.
cd /d "%~dp0"
if not exist .venv\Scripts\pythonw.exe (
    echo Setting up for first run...
    python -m venv .venv || goto :error
    .venv\Scripts\python.exe -m pip install -q -r requirements.txt || goto :error
)
if not exist "%APPDATA%\Microsoft\Windows\Start Menu\Programs\Vinyl Recorder.lnk" (
    .venv\Scripts\python.exe tools\install_shortcuts.py
)
start "" .venv\Scripts\pythonw.exe run.py
exit /b 0

:error
echo Setup failed. Make sure Python 3.11+ is installed and on PATH.
pause
