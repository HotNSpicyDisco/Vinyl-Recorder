"""Start Vinyl Recorder."""

import os
import sys

os.environ.setdefault("SD_ENABLE_ASIO", "1")  # before sounddevice is imported anywhere

from PySide6.QtGui import QIcon
from PySide6.QtWidgets import QApplication

from vinyl.shortcuts import APP_ID, ICON
from vinyl.ui.main_window import MainWindow


def self_test(report_path: str) -> int:
    """Check the parts a packaged build can lose (audio drivers, codecs, scipy) and write a report."""
    import tempfile
    import traceback
    from pathlib import Path

    import numpy as np

    lines, ok = [], True

    def check(name, fn):
        nonlocal ok
        try:
            lines.append(f"OK    {name}: {fn()}")
        except Exception:
            ok = False
            lines.append(f"FAIL  {name}:\n{traceback.format_exc()}")

    from vinyl import audio, export, splitter, tempo
    from vinyl.discogs import Release, Track

    check("audio devices", lambda: sorted({d.hostapi for d in audio.list_input_devices()}))
    check("ASIO inputs", lambda: [d.label for d in audio.list_input_devices() if d.hostapi == "ASIO"] or "none")
    check("icon", lambda: ICON.exists())
    with tempfile.TemporaryDirectory() as tmp:
        sr = 44100
        t = np.arange(sr * 12) / sr
        beat = (np.sin(2 * np.pi * 60 * (t % 0.5)) * np.exp(-(t % 0.5) * 12)).astype(np.float32)
        side = np.concatenate([beat, np.zeros(sr * 2, np.float32), beat])
        wav = Path(tmp) / "side.wav"
        import soundfile as sf

        sf.write(wav, np.stack([side, side], 1), sr, subtype="PCM_24")
        check("BPM", lambda: tempo.estimate_bpm(np.stack([beat, beat], 1), sr))
        env = audio.compute_envelope(wav)
        check("split", lambda: [round(e, 2) for e in splitter.split(env, durations=[12, 12]).edges_and_skips()[0]])
        rel = Release(1, "Test", "Self Test", "2026", [], [], "", "",
                      [Track("A1", "One", 12, "", 1), Track("A2", "Two", 12, "", 2)], "", "")
        cuts = [export.TrackCut(rel.tracks[0], 0, 12), export.TrackCut(rel.tracks[1], 14, 26)]
        check("FLAC + MP3", lambda: [p.name for s in export.export_side(wav, cuts, rel, Path(tmp), fmt="both")
                                     for p in s.files])
    Path(report_path).write_text(("PASS" if ok else "FAIL") + "\n" + "\n".join(lines), encoding="utf-8")
    return 0 if ok else 1


def main():
    if len(sys.argv) == 3 and sys.argv[1] == "--self-test":
        sys.exit(self_test(sys.argv[2]))
    if sys.platform == "win32":
        # Own taskbar identity, so Windows shows our icon instead of Python's and groups with the shortcut.
        import ctypes

        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(APP_ID)
    app = QApplication(sys.argv)
    app.setApplicationName("Vinyl Recorder")
    app.setWindowIcon(QIcon(str(ICON)))
    app.setStyle("Fusion")
    win = MainWindow()
    win.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
