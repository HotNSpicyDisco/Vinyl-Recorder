"""Audio input/output: live recording from the interface, preview playback, envelope analysis."""

import os

# Must be set before sounddevice is imported, or PortAudio loads without ASIO support.
os.environ.setdefault("SD_ENABLE_ASIO", "1")

import functools
import queue
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import soundfile as sf
from scipy.signal import butter, sosfilt


class _LazySounddevice:
    """sounddevice, imported on first use.

    Importing it starts PortAudio, which scans every audio device through every Windows audio
    system; on a PC with many devices that takes 10+ seconds. Deferring it lets the window open
    straight away while the scan runs in the background (see list_input_devices).
    """

    def __getattr__(self, name):
        import sounddevice

        return getattr(sounddevice, name)


sd = _LazySounddevice()

# All PortAudio calls (start-up, device scan, opening/closing streams) happen on this one thread.
# ASIO drivers are COM objects tied to the thread PortAudio started on: opening a stream from
# another thread crashes. Audio callbacks still run on PortAudio's own threads.
_audio_thread = ThreadPoolExecutor(max_workers=1, thread_name_prefix="portaudio")


def on_audio_thread(fn):
    """Run fn on the PortAudio thread and wait for its result (directly if already there)."""

    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        if threading.current_thread().name.startswith("portaudio"):
            return fn(*args, **kwargs)
        return _audio_thread.submit(fn, *args, **kwargs).result()

    return wrapper


@dataclass
class InputDevice:
    index: int
    name: str
    hostapi: str
    channels: int

    @property
    def label(self) -> str:
        return f"{self.name}  [{self.hostapi}, {self.channels} in]"


@on_audio_thread
def list_input_devices() -> list[InputDevice]:
    """All recording devices, ASIO first. The first call starts PortAudio (slow; run it in the background)."""
    apis = sd.query_hostapis()
    devices = []
    for i, d in enumerate(sd.query_devices()):
        if d["max_input_channels"] > 0:
            devices.append(InputDevice(i, d["name"], apis[d["hostapi"]]["name"], d["max_input_channels"]))
    # ASIO first: it is the only way to reach inputs 3+ on most interfaces.
    devices.sort(key=lambda d: (d.hostapi != "ASIO", d.hostapi, d.name))
    return devices


@on_audio_thread
def rescan_input_devices() -> list[InputDevice]:
    """Restart PortAudio so it sees devices plugged in since it started, then list them.
    Close any open streams first."""
    sd._terminate()
    sd._initialize()
    return list_input_devices()


CLIP_LEVEL = 0.999  # a sample this close to full scale counts as clipped
MAX_CLIP_TIMES = 5000


class Recorder:
    """Keeps an input stream open for level metering; writes to disk while recording.

    Clips are counted per channel as events (a run of clipped samples counts once).
    While recording, the time of each clip is kept so it can be marked on the waveform.
    """

    def __init__(self):
        self.clip_counts = [0, 0]
        self.clip_times: list[float] = []
        self._prev_clipped = np.zeros(2, dtype=bool)
        self._rec_frames = 0
        self.stream = None  # sounddevice.InputStream while the input is open
        self.samplerate = 0
        self._select: list[int] | None = None
        self._lock = threading.Lock()
        self._peak = np.zeros(2, dtype=np.float32)
        self._queue: queue.Queue = queue.Queue()
        self._recording = False
        self._writer: threading.Thread | None = None
        self._file: sf.SoundFile | None = None
        self.frames_written = 0
        self.overflows = 0
        self.path: Path | None = None

    @property
    def is_open(self) -> bool:
        return self.stream is not None

    @property
    def is_recording(self) -> bool:
        return self._recording

    @on_audio_thread
    def open(self, device: InputDevice, left: int, right: int, samplerate: int) -> None:
        """Open the input stream. left/right are 0-based channel indexes on the device."""
        self.close()
        if max(left, right) >= device.channels:
            raise ValueError(f"{device.name} only has {device.channels} inputs")
        kwargs = {}
        if device.hostapi == "ASIO":
            kwargs["extra_settings"] = sd.AsioSettings(channel_selectors=[left, right])
            channels, self._select = 2, None
        else:
            channels, self._select = max(left, right) + 1, [left, right]
        self.stream = sd.InputStream(
            device=device.index,
            channels=channels,
            samplerate=samplerate,
            dtype="float32",
            blocksize=0,
            latency="high",
            callback=self._callback,
            **kwargs,
        )
        self.samplerate = samplerate
        self.overflows = 0
        self.stream.start()

    @on_audio_thread
    def close(self) -> None:
        if self._recording:
            self.stop()
        if self.stream is not None:
            self.stream.stop()
            self.stream.close()
            self.stream = None

    def _callback(self, indata, frames, time_info, status):
        if status.input_overflow:
            self.overflows += 1
        data = indata if self._select is None else indata[:, self._select]
        mag = np.abs(data)
        peak = mag.max(axis=0)
        with self._lock:
            np.maximum(self._peak, peak, out=self._peak)
        if peak.max() >= CLIP_LEVEL:
            self._count_clips(mag >= CLIP_LEVEL)
        else:
            self._prev_clipped[:] = False
        if self._recording:
            self._queue.put(data.copy())
            self._rec_frames += frames

    def _count_clips(self, clipped: np.ndarray) -> None:
        for ch in range(2):
            col = clipped[:, ch]
            starts = col & ~np.concatenate(([self._prev_clipped[ch]], col[:-1]))
            n = int(starts.sum())
            if not n:
                continue
            self.clip_counts[ch] += n
            if self._recording and len(self.clip_times) < MAX_CLIP_TIMES:
                frames = self._rec_frames + np.flatnonzero(starts)
                self.clip_times.extend((frames / self.samplerate).tolist())
        self._prev_clipped[:] = clipped[-1]

    def reset_clips(self) -> None:
        self.clip_counts = [0, 0]
        self.clip_times = []

    def read_peaks(self) -> np.ndarray:
        """Peak level (linear, 0..1) per channel since the last call."""
        with self._lock:
            peaks = self._peak.copy()
            self._peak[:] = 0
        return peaks

    def start(self, path: Path) -> None:
        if not self.is_open:
            raise RuntimeError("Input is not open")
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        self._file = sf.SoundFile(path, "w", samplerate=self.samplerate, channels=2, subtype="PCM_24")
        self.frames_written = 0
        self._rec_frames = 0
        self.reset_clips()
        self._queue = queue.Queue()
        self._writer = threading.Thread(target=self._write_loop, daemon=True)
        self._writer.start()
        self._recording = True

    def _write_loop(self):
        while True:
            block = self._queue.get()
            if block is None:
                break
            self._file.write(block)
            self.frames_written += len(block)

    def stop(self) -> Path | None:
        if not self._recording:
            return None
        self._recording = False
        self._queue.put(None)
        self._writer.join()
        self._file.close()
        self._file = None
        return self.path

    @property
    def seconds(self) -> float:
        return self.frames_written / self.samplerate if self.samplerate else 0.0


class Player:
    """Plays part of a file on the default output so you can check split points."""

    def __init__(self):
        self.stream = None  # sounddevice.OutputStream while playing
        self._file: sf.SoundFile | None = None
        self.position = 0.0
        self.stop_at: float | None = None

    @property
    def is_playing(self) -> bool:
        return self.stream is not None and self.stream.active

    @on_audio_thread
    def play(self, path: Path, start: float, stop_at: float | None = None) -> None:
        self.stop()
        self._file = sf.SoundFile(path)
        self._file.seek(int(start * self._file.samplerate))
        self.position = start
        self.stop_at = stop_at
        self.stream = sd.OutputStream(
            samplerate=self._file.samplerate,
            channels=self._file.channels,
            dtype="float32",
            callback=self._callback,
            finished_callback=self._finished,
        )
        self.stream.start()

    def _callback(self, outdata, frames, time_info, status):
        f = self._file
        if self.stop_at is not None:
            frames = max(0, min(frames, int(self.stop_at * f.samplerate) - f.tell()))
        data = f.read(frames, dtype="float32", always_2d=True)
        n = len(data)
        outdata[:n] = data
        outdata[n:] = 0
        self.position = f.tell() / f.samplerate
        if n < len(outdata):
            raise sd.CallbackStop

    def _finished(self):
        if self._file is not None:
            self._file.close()
            self._file = None

    @on_audio_thread
    def stop(self) -> None:
        if self.stream is not None:
            self.stream.abort()
            self.stream.close()
            self.stream = None
        self._finished()


HOP_SECONDS = 0.02


@dataclass
class Envelope:
    """Per-hop loudness of a recording, used for gap detection and drawing."""

    rms_db: np.ndarray  # mono RMS in dBFS per hop
    peak: np.ndarray  # mono peak (linear) per hop
    hop: float
    duration: float
    samplerate: int


RUMBLE_CUTOFF_HZ = 40.0  # turntable/lead-in rumble lives below ~30 Hz; kick drums are above ~45 Hz


def rumble_filter(sr: int) -> np.ndarray:
    """High-pass filter (as second-order sections) that removes subsonic rumble but keeps the kick."""
    return butter(4, RUMBLE_CUTOFF_HZ, btype="highpass", fs=sr, output="sos")


def compute_envelope(path: Path, hop: float = HOP_SECONDS) -> Envelope:
    """Loudness per 20 ms hop, measured without rumble so a needle in the lead-in groove doesn't look like music."""
    with sf.SoundFile(path) as f:
        sr = f.samplerate
        hop_frames = max(1, int(sr * hop))
        sos = rumble_filter(sr)
        zi = np.zeros((sos.shape[0], 2))
        rms, peak = [], []
        for block in f.blocks(blocksize=hop_frames * 500, dtype="float32", always_2d=True):
            mono, zi = sosfilt(sos, block.mean(axis=1), zi=zi)
            n = len(mono) // hop_frames
            if n == 0:
                continue
            chunks = mono[: n * hop_frames].reshape(n, hop_frames)
            rms.append(np.sqrt(np.mean(chunks**2, axis=1)))
            peak.append(np.abs(chunks).max(axis=1))
        duration = f.frames / sr
    rms_arr = np.concatenate(rms) if rms else np.zeros(0)
    peak_arr = np.concatenate(peak) if peak else np.zeros(0)
    return Envelope(20 * np.log10(rms_arr + 1e-10), peak_arr, hop_frames / sr, duration, sr)
