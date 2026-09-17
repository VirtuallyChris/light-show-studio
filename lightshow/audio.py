"""Microphone capture and real-time music feature extraction.

Produces, for every analysis frame, a small `Features` object with values
normalised to 0..1 that the light mapping code can consume directly:

  level      overall loudness (auto-gain controlled)
  bass/mid/treble   band energies (auto-gain controlled per band)
  centroid   spectral centroid, 0 = all low end, 1 = all top end
  beat       True on detected bass onsets (kick drums, basslines)
  bpm        running tempo estimate from beat intervals (0 if unknown)
"""

from __future__ import annotations

import math
import threading
import time
from collections import deque
from dataclasses import dataclass

import numpy as np
import sounddevice as sd


@dataclass
class Features:
    level: float = 0.0
    bass: float = 0.0
    mid: float = 0.0
    treble: float = 0.0
    centroid: float = 0.0
    beat: bool = False
    bpm: float = 0.0
    silent: bool = True
    raw_rms: float = 0.0
    bins: "np.ndarray | None" = None  # N_BINS log-spaced band energies, 0..1
    fresh: bool = False  # True when computed from a new audio block
    hit: bool = False    # treble onset (snare / hi-hat)
    raw_bass: float = 0.0    # un-normalised band energies for long-term structure tracking
    raw_treble: float = 0.0


N_BINS = 24


class AutoGain:
    """Track a slowly-decaying running peak so 0..1 output fills the range
    regardless of how loud the source is."""

    def __init__(self, decay_per_sec: float = 0.35, floor: float = 1e-4):
        self.decay = decay_per_sec
        self.floor = floor
        self.peak = floor
        self._t = time.monotonic()

    def __call__(self, x: float) -> float:
        now = time.monotonic()
        dt, self._t = now - self._t, now
        self.peak = max(self.floor, self.peak * math.exp(-self.decay * dt), x)
        return min(1.0, x / self.peak)


class VectorAutoGain:
    """AutoGain for an array of bands (one running peak per band)."""

    def __init__(self, n: int, decay_per_sec: float = 0.4, floor: float = 1e-6):
        self.decay = decay_per_sec
        self.floor = floor
        self.peak = np.full(n, floor)
        self._t = time.monotonic()

    def __call__(self, x: np.ndarray) -> np.ndarray:
        now = time.monotonic()
        dt, self._t = now - self._t, now
        self.peak = np.maximum(np.maximum(self.floor, self.peak * math.exp(-self.decay * dt)), x)
        return np.clip(x / self.peak, 0.0, 1.0)


class BeatDetector:
    """Energy-based onset detector on the bass band.

    A beat is declared when the current bass energy exceeds the recent
    average by `sensitivity` standard deviations, with a refractory period
    so one kick cannot trigger twice.
    """

    def __init__(self, history_sec: float = 1.5, fps: float = 43.0, sensitivity: float = 1.4, min_interval: float = 0.18):
        self.history = deque(maxlen=max(8, int(history_sec * fps)))
        self.sensitivity = sensitivity
        self.min_interval = min_interval
        self.last_beat = 0.0
        self.intervals = deque(maxlen=12)

    def __call__(self, energy: float) -> bool:
        now = time.monotonic()
        hist = np.fromiter(self.history, dtype=float) if self.history else np.zeros(1)
        self.history.append(energy)
        if len(hist) < 8:
            return False
        mean, std = hist.mean(), hist.std()
        threshold = mean + self.sensitivity * max(std, 0.02 * mean + 1e-9)
        if energy > threshold and energy > 1e-6 and now - self.last_beat > self.min_interval:
            if self.last_beat:
                self.intervals.append(now - self.last_beat)
            self.last_beat = now
            return True
        return False

    @property
    def bpm(self) -> float:
        if len(self.intervals) < 4:
            return 0.0
        iv = np.median(self.intervals)
        bpm = 60.0 / iv if iv > 0 else 0.0
        while bpm > 180:  # fold double-time detections down
            bpm /= 2
        while 0 < bpm < 60:
            bpm *= 2
        return bpm


class AudioAnalyzer:
    def __init__(self, device=None, samplerate: int | None = None, blocksize: int | None = None,
                 beat_sensitivity: float = 1.4, silence_rms: float = 0.002):
        self.device = device
        if samplerate is None:
            # WASAPI (shared mode) only opens at the device's native rate.
            try:
                samplerate = int(sd.query_devices(device, "input")["default_samplerate"])
            except Exception:
                samplerate = 44100
        if blocksize is None:
            blocksize = 1024 if samplerate <= 48000 else 2048  # FFT window (~23 ms)
        self.samplerate = samplerate
        self.blocksize = blocksize
        self.hop = blocksize // 2  # analyse every half window: ~86 analyses/s at 44.1 kHz
        self.silence_rms = silence_rms
        self._lock = threading.Lock()
        self._ring = np.zeros(blocksize * 8, dtype=np.float32)  # ring buffer of recent samples
        self._write = 0          # total samples written so far
        self._consumed = 0       # total samples analysed so far
        self._stream: sd.InputStream | None = None

        self._window = np.hanning(blocksize).astype(np.float32)
        freqs = np.fft.rfftfreq(blocksize, 1.0 / samplerate)
        self._bass = (freqs >= 30) & (freqs < 200)
        self._mid = (freqs >= 200) & (freqs < 2500)
        self._treble = (freqs >= 2500) & (freqs < 12000)
        self._audible = (freqs >= 30) & (freqs < 12000)
        self._log_f = np.log2(np.clip(freqs, 30, None) / 30.0) / np.log2(12000 / 30.0)
        edges = np.geomspace(40, 12000, N_BINS + 1)
        self._bin_idx = np.clip(np.searchsorted(edges, freqs) - 1, -1, N_BINS)
        # Low bins can be narrower than one FFT bin; fill those from the nearest populated bin.
        counts = np.bincount(self._bin_idx[(self._bin_idx >= 0) & (self._bin_idx < N_BINS)], minlength=N_BINS)
        populated = np.flatnonzero(counts)
        self._bin_fill = np.array([populated[np.abs(populated - i).argmin()] for i in range(N_BINS)])
        self.gain_bins = VectorAutoGain(N_BINS)

        fps = samplerate / blocksize
        self.gain_level = AutoGain()
        self.gain_bass = AutoGain()
        self.gain_mid = AutoGain()
        self.gain_treble = AutoGain()
        self.beats = BeatDetector(fps=fps, sensitivity=beat_sensitivity)
        self.hits = BeatDetector(history_sec=0.8, fps=fps, sensitivity=1.8, min_interval=0.09)

    # -- stream lifecycle -------------------------------------------------
    def _callback(self, indata, frames, time_info, status):
        mono = indata.mean(axis=1) if indata.ndim > 1 else indata[:, 0]
        n = len(mono)
        with self._lock:
            pos = self._write % len(self._ring)
            first = min(n, len(self._ring) - pos)
            self._ring[pos:pos + first] = mono[:first]
            if first < n:
                self._ring[:n - first] = mono[first:]
            self._write += n

    def start(self) -> "AudioAnalyzer":
        self._stream = sd.InputStream(
            device=self.device, channels=None, samplerate=self.samplerate,
            blocksize=self.hop, dtype="float32", callback=self._callback,
        )
        self._stream.start()
        return self

    def stop(self) -> None:
        if self._stream is not None:
            self._stream.stop()
            self._stream.close()
            self._stream = None

    def __enter__(self):
        return self.start()

    def __exit__(self, *exc):
        self.stop()

    # -- analysis -----------------------------------------------------------
    def analyze(self) -> Features:
        with self._lock:
            if self._write - self._consumed < self.hop or self._write < self.blocksize:
                return Features()
            # If we fell behind (bursty driver), skip ahead so the light never lags the music.
            self._consumed = max(self._consumed + self.hop, self._write - self.hop)
            end = self._write % len(self._ring)
            start = end - self.blocksize
            block = self._ring[start:end].copy() if start >= 0 else np.concatenate((self._ring[start:], self._ring[:end]))

        rms = float(np.sqrt(np.mean(block * block)))
        spectrum = np.abs(np.fft.rfft(block * self._window))
        power = spectrum * spectrum

        bass = float(power[self._bass].sum())
        mid = float(power[self._mid].sum())
        treble = float(power[self._treble].sum())
        audible = power[self._audible]
        centroid = float((audible * self._log_f[self._audible]).sum() / (audible.sum() + 1e-12))

        valid = (self._bin_idx >= 0) & (self._bin_idx < N_BINS)
        raw_bins = np.bincount(self._bin_idx[valid], weights=power[valid], minlength=N_BINS)
        raw_bins = raw_bins[self._bin_fill]  # fill bins narrower than one FFT bin
        raw_bins *= np.geomspace(1.0, 6.0, N_BINS)  # tilt: music has far less energy up top

        silent = rms < self.silence_rms
        beat = False if silent else self.beats(bass)
        hit = False if silent else self.hits(treble)
        bins = np.zeros(N_BINS) if silent else self.gain_bins(raw_bins)

        f = Features(
            level=0.0 if silent else self.gain_level(rms),
            bass=0.0 if silent else self.gain_bass(bass),
            mid=0.0 if silent else self.gain_mid(mid),
            treble=0.0 if silent else self.gain_treble(treble),
            centroid=min(1.0, max(0.0, centroid)),
            beat=beat,
            bpm=self.beats.bpm,
            silent=silent,
            raw_rms=rms,
            bins=bins,
            fresh=True,
            hit=hit,
            raw_bass=bass,
            raw_treble=treble,
        )
        return f


def list_input_devices() -> list[tuple[int, str, str]]:
    """Return (index, name, hostapi name) for every device with inputs."""
    apis = sd.query_hostapis()
    out = []
    for i, d in enumerate(sd.query_devices()):
        if d["max_input_channels"] > 0:
            out.append((i, d["name"], apis[d["hostapi"]]["name"]))
    return out


def default_input_device() -> int | None:
    try:
        return sd.default.device[0]
    except Exception:
        return None
