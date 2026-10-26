# PyInstaller recipe for the standalone Windows app. Build with packaging\build.bat.
import os

from PyInstaller.utils.hooks import collect_data_files

ROOT = os.path.abspath(os.path.join(SPECPATH, ".."))  # noqa: F821 (SPECPATH is set by PyInstaller)
SITE = os.path.join(ROOT, ".venv", "Lib", "site-packages")

# PortAudio with ASIO support (sounddevice) and libsndfile with MP3/FLAC (soundfile): 64-bit Windows only
binaries = [
    (os.path.join(SITE, "_sounddevice_data", "portaudio-binaries", name), "_sounddevice_data/portaudio-binaries")
    for name in ("libportaudio64bit-asio.dll", "libportaudio64bit.dll")
] + [(os.path.join(SITE, "_soundfile_data", "libsndfile_x64.dll"), "_soundfile_data")]

a = Analysis(
    [os.path.join(ROOT, "run.py")],
    pathex=[ROOT],
    binaries=binaries,
    datas=[
        (os.path.join(ROOT, "assets", "icon.ico"), "assets"),
        (os.path.join(ROOT, "assets", "icon.png"), "assets"),
    ] + collect_data_files("certifi"),
    hiddenimports=["win32com.propsys", "win32com.shell", "pythoncom", "pywintypes"],
    excludes=[
        # Not used, and large
        "tkinter", "pytest", "PyInstaller", "IPython", "matplotlib",
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
    icon=os.path.join(ROOT, "assets", "icon.ico"),
    console=False,
    upx=False,
)
coll = COLLECT(exe, a.binaries, a.datas, upx=False, name="Vinyl Recorder")
