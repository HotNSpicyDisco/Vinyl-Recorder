"""Audio input/output: live recording from the interface, preview playback, envelope analysis."""

import os

# Must be set before sounddevice is imported, or PortAudio loads without ASIO support.
os.environ.setdefault("SD_ENABLE_ASIO", "1")

import collections
import functools
import queue
import sys
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

# Hands-free recording
ARMED, RECORDING, PAUSED, FINISHED = "armed", "recording", "paused", "finished"
LEVEL_BLOCK = 0.1  # seconds per level reading
SOUND_DB = -45.0  # a needle drop or music: louder than this (rumble filtered out)...
SOUND_BLOCKS = (3, 5)  # ...in 3 of the last 5 readings (run-out clicks are too short to count)
PREROLL = 2.0  # seconds kept from just before the sound started
RUNOUT_SECONDS = 6.0  # this long without music might be the run-out groove (or a long gap, a quiet intro)...
NEEDLE_UP_DB = -68.0  # ...so the side only ends once the needle is lifted (quieter than any groove noise)...
NEEDLE_UP_SECONDS = 0.5  # ...for this long,
RUNOUT_MAX_SECONDS = 20.0  # or when it's been quiet this long (the run-out left spinning)
QUIET_BELOW_MUSIC_DB = 35.0  # "without music": this far below the loudest second so far...
QUIET_MAX_DB = -40.0  # ...and never louder than this
MONO_SECONDS = 4.0  # recent input kept for the live hum check


class Recorder:
    """Keeps an input stream open for level metering; writes to disk while recording.

    Clips are counted per channel as events (a run of clipped samples counts once).
    While recording, the time of each clip is kept so it can be marked on the waveform.

    Hands-free mode (start(..., hands_free=True)): the take waits for the needle drop and
    begins with the PREROLL seconds before it. When the music stops for RUNOUT_SECONDS it keeps
    recording (it may only be a long gap or a quiet intro) until the needle is lifted, or until
    it has been quiet for RUNOUT_MAX_SECONDS; then it pauses, notes the side break, and re-arms for
    the next side (so the flip isn't recorded). After the last side it finishes on its own.
    `state`, `ending` and `event` tell the UI what's happening.
    """

    def __init__(self):
        self.clip_counts = [0, 0]
        self.clip_times: list[float] = []
        self._prev_clipped = np.zeros(2, dtype=bool)
        self._rec_frames = 0  # frames sent to the file so far
        self._writing = False
        self.hands_free = False
        self.state = RECORDING
        self.event: str | None = None  # "finished" when hands-free mode recorded the last side
        self.sides_target: int | None = None
        self.sides_done = 0
        self.ending = False  # hands-free: quiet long enough to be the run-out; waiting for the needle to lift
        self._quiet_blocks = 0
        self.side_breaks: list[float] = []  # seconds into the file where a side ended
        self._mono = np.zeros(1, dtype=np.float32)  # sized properly when the input opens
        self._mono_pos = 0
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
        self._prepare_detection(samplerate)
        self.stream.start()

    def _prepare_detection(self, samplerate: int) -> None:
        """Level tracking for hands-free mode: rumble-filtered readings every LEVEL_BLOCK, and a
        ring buffer of the last PREROLL seconds so a take can start just before the sound."""
        self._sos = rumble_filter(samplerate)
        self._zi = np.zeros((self._sos.shape[0], 2))  # filter state carried between callbacks
        self._acc, self._acc_n = 0.0, 0
        self._block_n = int(samplerate * LEVEL_BLOCK)
        self._levels: collections.deque = collections.deque(maxlen=int(RUNOUT_SECONDS / LEVEL_BLOCK))
        self._recent: collections.deque = collections.deque(maxlen=SOUND_BLOCKS[1])
        self._music_db = -np.inf  # loudest one-second level since the take started
        self._ring = np.zeros((int(samplerate * PREROLL), 2), dtype=np.float32)
        self._ring_pos, self._ring_full = 0, False
        # The last few seconds in mono, for the hum check while you set levels
        self._mono = np.zeros(int(samplerate * MONO_SECONDS), dtype=np.float32)
        self._mono_pos = 0

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
        self._push_mono(data)
        if not self._recording:
            return
        if self.hands_free:
            self._push_ring(data)
            if self._track_levels(data):  # the sound started this block: the ring already holds it
                preroll = self._ring_contents()
                self._queue.put(preroll)
                self._rec_frames += len(preroll)
                return
        if self._writing:
            self._queue.put(data.copy())
            self._rec_frames += frames

    def _push_mono(self, data: np.ndarray) -> None:
        mono = data.mean(axis=1)
        n, size = len(mono), len(self._mono)
        if n >= size:
            self._mono[:] = mono[-size:]
            self._mono_pos = 0
            return
        end = self._mono_pos + n
        if end <= size:
            self._mono[self._mono_pos:end] = mono
        else:
            first = size - self._mono_pos
            self._mono[self._mono_pos:] = mono[:first]
            self._mono[:n - first] = mono[first:]
        self._mono_pos = end % size

    def recent_audio(self) -> np.ndarray:
        """The last MONO_SECONDS of input (mono), oldest first."""
        return np.concatenate([self._mono[self._mono_pos:], self._mono[:self._mono_pos]])

    # --- hands-free mode (runs on PortAudio's callback thread, so it stays cheap) ---

    def _push_ring(self, data: np.ndarray) -> None:
        n, size = len(data), len(self._ring)
        if n >= size:
            self._ring[:] = data[-size:]
            self._ring_pos, self._ring_full = 0, True
            return
        end = self._ring_pos + n
        if end <= size:
            self._ring[self._ring_pos:end] = data
        else:
            first = size - self._ring_pos
            self._ring[self._ring_pos:] = data[:first]
            self._ring[:n - first] = data[first:]
        self._ring_full = self._ring_full or end >= size
        self._ring_pos = end % size

    def _ring_contents(self) -> np.ndarray:
        if not self._ring_full:
            return self._ring[:self._ring_pos].copy()
        return np.concatenate([self._ring[self._ring_pos:], self._ring[:self._ring_pos]])

    def _track_levels(self, data: np.ndarray) -> bool:
        """Update the state machine. Returns True if writing (re)started on this block."""
        hp, self._zi = sosfilt(self._sos, data.mean(axis=1), zi=self._zi)
        started, pos = False, 0
        while pos < len(hp):  # exact LEVEL_BLOCK readings, whatever size PortAudio's buffers are
            take = min(self._block_n - self._acc_n, len(hp) - pos)
            self._acc += float(np.sum(hp[pos:pos + take] ** 2))
            self._acc_n += take
            pos += take
            if self._acc_n >= self._block_n:
                db = 10 * np.log10(self._acc / self._acc_n + 1e-14)
                self._acc, self._acc_n = 0.0, 0
                started |= self._on_level(db)
        return started

    def _on_level(self, db: float) -> bool:
        self._levels.append(db)
        self._recent.append(db)
        if self.state in (ARMED, PAUSED):
            if sum(d > SOUND_DB for d in self._recent) >= SOUND_BLOCKS[0]:
                self.state, self._writing = RECORDING, True
                self._levels.clear()  # a fresh RUNOUT_SECONDS before this side can end
                return True
            return False
        if self.state == RECORDING:
            last_second = list(self._levels)[-int(1 / LEVEL_BLOCK):]
            power = np.mean(10 ** (np.array(last_second) / 10))
            self._music_db = max(self._music_db, 10 * np.log10(power + 1e-14))
            quiet = min(QUIET_MAX_DB, self._music_db - QUIET_BELOW_MUSIC_DB)
            levels = np.array(self._levels)
            # Median, so the once-per-turn click of a run-out groove doesn't keep the side going
            self.ending = (len(levels) == self._levels.maxlen and np.median(levels) < quiet
                           and np.mean(levels > quiet) < 0.2)
            self._quiet_blocks = self._quiet_blocks + 1 if self.ending else 0
            lifted = levels[-int(NEEDLE_UP_SECONDS / LEVEL_BLOCK):]
            needle_up = np.mean(lifted < NEEDLE_UP_DB) >= 0.8
            long_quiet = self._quiet_blocks * LEVEL_BLOCK >= RUNOUT_MAX_SECONDS - RUNOUT_SECONDS
            if self.ending and (needle_up or long_quiet):
                self._writing = self.ending = False
                self._quiet_blocks = 0
                self.side_breaks.append(self._rec_frames / self.samplerate)
                self.sides_done += 1
                self._recent.clear()
                if self.sides_target and self.sides_done >= self.sides_target:
                    self.state, self.event = FINISHED, "finished"
                else:
                    self.state = PAUSED
        return False

    def _count_clips(self, clipped: np.ndarray) -> None:
        for ch in range(2):
            col = clipped[:, ch]
            starts = col & ~np.concatenate(([self._prev_clipped[ch]], col[:-1]))
            n = int(starts.sum())
            if not n:
                continue
            self.clip_counts[ch] += n
            if self._writing and len(self.clip_times) < MAX_CLIP_TIMES:
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

    def start(self, path: Path, hands_free: bool = False, sides: int | None = None) -> None:
        """Start a take. hands_free: wait for the needle, pause between sides, and finish after
        `sides` sides (None: keep going until stopped)."""
        if not self.is_open:
            raise RuntimeError("Input is not open")
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        self._file = sf.SoundFile(path, "w", samplerate=self.samplerate, channels=2, subtype="PCM_24")
        self.frames_written = 0
        self._rec_frames = 0
        self.reset_clips()
        self.hands_free, self.sides_target, self.sides_done = hands_free, sides, 0
        self.side_breaks, self.event = [], None
        self.ending, self._quiet_blocks = False, 0
        if hands_free:
            self._prepare_detection(self.samplerate)
        self.state = ARMED if hands_free else RECORDING
        self._writing = not hands_free
        self._queue = queue.Queue()
        self._writer = threading.Thread(target=self._write_loop, daemon=True)
        self._writer.start()
        # Hand the GIL over more often, so a save running in the background can't make the audio
        # callback wait long enough to drop samples
        self._switch_interval = sys.getswitchinterval()
        sys.setswitchinterval(0.001)
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
        self._recording = self._writing = False
        self._queue.put(None)
        self._writer.join()
        self._file.close()
        self._file = None
        sys.setswitchinterval(self._switch_interval)
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
