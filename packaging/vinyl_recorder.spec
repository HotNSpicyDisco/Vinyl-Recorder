# PyInstaller recipe for the standalone app: "Vinyl Recorder.exe" (in a folder) on Windows,
# "Vinyl Recorder.app" on macOS. Build on the system you're building for:
#   Windows: packaging\build.bat     macOS: packaging/build_mac.sh     (or let GitHub Actions do both)
import glob
import os
import re
import sys

import _sounddevice_data
import _soundfile_data
from PyInstaller.utils.hooks import collect_data_files

ROOT = os.path.abspath(os.path.join(SPECPATH, ".."))  # noqa: F821 (SPECPATH is set by PyInstaller)
VERSION = re.search(r'__version__ = "([^"]+)"', open(os.path.join(ROOT, "vinyl", "__init__.py")).read()).group(1)
MAC = sys.platform == "darwin"

# PortAudio (sounddevice; the Windows build includes ASIO) and libsndfile with MP3/FLAC (soundfile)
pa_dir = os.path.join(os.path.dirname(_sounddevice_data.__file__), "portaudio-binaries")
sf_dir = os.path.dirname(_soundfile_data.__file__)
if MAC:
    libs = glob.glob(os.path.join(pa_dir, "*.dylib")), glob.glob(os.path.join(sf_dir, "*.dylib"))
else:
    libs = ([os.path.join(pa_dir, n) for n in ("libportaudio64bit-asio.dll", "libportaudio64bit.dll")],
            [os.path.join(sf_dir, "libsndfile_x64.dll")])
binaries = [(p, "_sounddevice_data/portaudio-binaries") for p in libs[0]] + [(p, "_soundfile_data") for p in libs[1]]

a = Analysis(
    [os.path.join(ROOT, "run.py")],
    pathex=[ROOT],
    binaries=binaries,
    datas=[
        (os.path.join(ROOT, "assets", "icon.ico"), "assets"),
        (os.path.join(ROOT, "assets", "icon.png"), "assets"),
    ] + collect_data_files("certifi"),
    hiddenimports=[] if MAC else ["win32com.propsys", "win32com.shell", "pythoncom", "pywintypes"],
    excludes=[
        # Not used, and large
        "tkinter", "pytest", "PyInstaller", "IPython", "matplotlib", "PIL",
        "PySide6.QtWebEngineCore", "PySide6.QtWebEngineWidgets", "PySide6.QtWebChannel", "PySide6.QtQml",
        "PySide6.QtQuick", "PySide6.QtPdf", "PySide6.Qt3DCore", "PySide6.QtMultimedia", "PySide6.QtCharts",
        "PySide6.QtDataVisualization", "PySide6.QtTest",
    ],
    noarchive=False,
)
pyz = PYZ(a.pure)
exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="Vinyl Recorder",
    icon=os.path.join(ROOT, "assets", "icon.icns" if MAC else "icon.ico"),
    console=False,
    upx=False,
)
coll = COLLECT(exe, a.binaries, a.datas, upx=False, name="Vinyl Recorder")

if MAC:
    app = BUNDLE(
        coll,
        name="Vinyl Recorder.app",
        icon=os.path.join(ROOT, "assets", "icon.icns"),
        bundle_identifier="com.hotnspicydisco.vinylrecorder",
        version=VERSION,
        info_plist={
            "CFBundleDisplayName": "Vinyl Recorder",
            "CFBundleShortVersionString": VERSION,
            # Without this, macOS silently records silence instead of asking for permission
            "NSMicrophoneUsageDescription": "Vinyl Recorder records your turntable or mixer from your audio interface.",
            "NSHighResolutionCapable": True,
            "LSMinimumSystemVersion": "11.0",
        },
    )
