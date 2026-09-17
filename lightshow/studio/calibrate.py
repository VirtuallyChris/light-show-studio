"""Measure the Cast delay with a microphone.

Casts a short file of sharp chirps at known positions to a speaker or group,
records the microphone meanwhile, and compares when each chirp is *heard*
with when the speakers' reported position said it should play. The median
difference is the Cast offset: how much later the sound reaches the room than
the position the studio follows. Positive = sound arrives later.
"""

from __future__ import annotations

import os
import threading
import time

import numpy as np

SR = 48000
CHIRPS = [1.0, 2.5, 4.0, 5.5, 7.0, 8.5]   # seconds into the file
DUR = 10.5


def chirp_template(sr: int = SR) -> np.ndarray:
    t = np.arange(int(0.06 * sr)) / sr
    f0, f1 = 600.0, 4500.0
    phase = 2 * np.pi * (f0 * t + (f1 - f0) * t * t / (2 * t[-1]))
    env = np.sin(np.pi * t / t[-1]) ** 0.5
    return (np.sin(phase) * env).astype(np.float32)


def write_calibration_file(path: str) -> None:
    import soundfile as sf
    y = np.zeros(int(DUR * SR), dtype=np.float32)
    c = chirp_template()
    for t0 in CHIRPS:
        i = int(t0 * SR)
        y[i:i + len(c)] += c * 0.8
    sf.write(path, y, SR)


class MicRecorder:
    def __init__(self, device=None, sr: int = SR):
        import sounddevice as sd
        self.sd = sd
        self.sr = sr
        self.device = device
        self.chunks: list[np.ndarray] = []
        self.first_at: float | None = None
        self.lock = threading.Lock()

    def _cb(self, indata, frames, time_info, status):
        now = time.monotonic()
        with self.lock:
            if self.first_at is None:
                self.first_at = now - frames / self.sr  # time of the first sample of this buffer
            self.chunks.append(indata.mean(axis=1).copy() if indata.ndim > 1 else indata[:, 0].copy())

    def __enter__(self):
        self.stream = self.sd.InputStream(device=self.device, channels=None, samplerate=self.sr, blocksize=1024, dtype="float32", callback=self._cb)
        self.stream.start()
        return self

    def __exit__(self, *exc):
        self.stream.stop(); self.stream.close()

    def audio(self) -> tuple[np.ndarray, float]:
        with self.lock:
            return (np.concatenate(self.chunks) if self.chunks else np.zeros(0, dtype=np.float32)), self.first_at or 0.0


def measure(cast_manager, media_url: str, wav_path: str, mic_device=None, log=print) -> dict:
    """Run one measurement. Returns {'offset_ms', 'per_chirp_ms', 'n', ...}."""
    from scipy.signal import fftconvolve

    tmpl = chirp_template()
    samples: list[tuple[float, float, str]] = []   # (monotonic, reported position, state)
    with MicRecorder(mic_device) as rec:
        time.sleep(0.5)
        cast_manager.play(media_url, wav_path, "calibration", start=0.0)
        t_end = time.monotonic() + DUR + 2.5
        while time.monotonic() < t_end:
            st = cast_manager.status()
            samples.append((time.monotonic(), st["position"], st["state"]))
            time.sleep(0.1)
        audio, first_at = rec.audio()
    try:
        cast_manager.stop()
    except Exception:  # noqa: BLE001
        pass

    playing = [(t, p) for t, p, s in samples if s == "PLAYING"]
    if len(playing) < 10:
        raise RuntimeError("the speaker never reported PLAYING; is it reachable and is the studio server on the LAN?")
    ts = np.array([t for t, _ in playing]); ps = np.array([p for _, p in playing])
    # the studio's belief: wall time of media position x. Slope is 1 by construction
    # (extrapolation); the intercept is the robust middle of the reported samples.
    intercept = float(np.median(ts - ps))
    pred_wall = lambda pos: pos + intercept

    if len(audio) < SR:
        raise RuntimeError("the microphone recorded nothing")
    audio = audio / (np.abs(audio).max() + 1e-9)
    corr = np.abs(fftconvolve(audio, tmpl[::-1], mode="same"))
    noise = np.median(corr) + 1e-9

    # 1) the whole chirp train at once: sum the matched-filter output at every chirp's
    #    expected sample for a candidate delay; the true delay lines all six up and wins
    #    over the single-chirp aliases 1.5 s away
    expect_idx = [(pred_wall(t0) - first_at) * SR for t0 in CHIRPS]
    cands = np.arange(-1.0, 3.0, 0.001)
    scores = np.zeros(len(cands))
    for k, d in enumerate(cands):
        tot = 0.0
        for e in expect_idx:
            i = int(e + d * SR + len(tmpl) // 2)
            if 0 <= i < len(corr):
                tot += corr[i]
        scores[k] = tot
    D = float(cands[int(np.argmax(scores))])
    log(f"  whole-train match: {D * 1000:+.0f} ms")

    # 2) refine each chirp within +-0.35 s of that
    results = []
    for t0, e in zip(CHIRPS, expect_idx):
        centre = int(e + D * SR + len(tmpl) // 2)
        lo, hi = max(0, centre - int(0.35 * SR)), min(len(corr), centre + int(0.35 * SR))
        if hi - lo < SR // 10:
            continue
        i = lo + int(np.argmax(corr[lo:hi]))
        snr = corr[i] / noise
        heard_at = first_at + (i - len(tmpl) // 2) / SR
        delay_ms = (heard_at - pred_wall(t0)) * 1000
        results.append((t0, delay_ms, snr))
        log(f"  chirp at {t0:4.1f}s: heard {delay_ms:+7.0f} ms after the reported position (peak/noise {snr:5.1f})")
    good = [d for _, d, snr in results if snr > 8]
    if len(good) < 3:
        raise RuntimeError(f"only {len(good)} chirps were heard clearly; move the mic closer to a speaker or raise the volume")
    med = float(np.median(good))
    spread = float(np.percentile(good, 75) - np.percentile(good, 25))
    return {"offset_ms": round(med), "spread_ms": round(spread), "n": len(good), "per_chirp_ms": [round(d) for d in good]}


def wait_for_steady_clock(cast, log=print, timeout=20.0, window=2.5, tolerance=0.07) -> bool:
    """Wait until the speaker's reported position tracks the wall clock steadily.

    For the first few seconds after playback starts the reported position is out
    by several hundred milliseconds while the device settles. Measuring during
    that window gives an answer that is wrong by however early you asked, which
    is why two calibrations of the same speaker can disagree.
    """
    import numpy as np

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        t0 = time.monotonic()
        p0 = cast.status()["position"]
        res = []
        while time.monotonic() - t0 < window:
            time.sleep(0.25)
            el = time.monotonic() - t0
            res.append(cast.status()["position"] - p0 - el)
        spread = float(np.max(res) - np.min(res)) if res else 1.0
        if spread <= tolerance:
            log(f"  speaker clock steady (varies by {spread * 1000:.0f} ms)")
            return True
        log(f"  waiting for the speaker clock to settle (varies by {spread * 1000:.0f} ms)")
    log("  speaker clock never settled; measuring anyway")
    return False


def _one_sync_pass(cast, song_path, seconds, mic_device, margin, log) -> dict:
    """Measure the Cast delay from the song that is actually playing.

    Listens to the room for a few seconds and lines the recording up against the
    song's own audio, matching onset envelopes rather than waveforms: room
    reverb and speaker EQ wreck a waveform comparison but leave the rhythm of
    attacks intact. More honest than a synthetic test tone because it measures
    the real file, decoded by the real speakers, at the real playback position.
    """
    import librosa
    import numpy as np
    from scipy.signal import fftconvolve

    SRC = 22050
    HOP = 256                       # ~11.6 ms per frame
    st = cast.status()
    if st.get("state") != "PLAYING":
        raise RuntimeError("the speakers are not playing; press play first")

    with MicRecorder(mic_device, sr=SRC) as rec:
        t0_pos, t0 = cast.status()["position"], time.monotonic()
        time.sleep(seconds)
        audio, first_at = rec.audio()
    if len(audio) < SRC:
        raise RuntimeError("the microphone recorded nothing")

    pos_at_start = t0_pos + (first_at - t0)          # song position when recording began
    ref_start = max(0.0, pos_at_start - margin)
    used_margin = pos_at_start - ref_start
    ref_len = seconds + used_margin + margin
    ref, _ = librosa.load(song_path, sr=SRC, mono=True, offset=ref_start, duration=ref_len)
    if len(ref) < SRC:
        raise RuntimeError("not enough of the song left at this position to compare against")

    env = lambda y: librosa.onset.onset_strength(y=y, sr=SRC, hop_length=HOP)
    a_env, b_env = env(audio), env(ref)
    a_env = a_env - a_env.mean()
    b_env = b_env - b_env.mean()
    if a_env.std() < 1e-6 or b_env.std() < 1e-6:
        raise RuntimeError("the room was too quiet to measure; turn the speakers up")
    corr = fftconvolve(b_env, a_env[::-1], mode="full")
    corr /= (np.linalg.norm(a_env) * np.linalg.norm(b_env) + 1e-9)

    # Only alignments a speaker could physically produce are worth considering.
    # Left unbounded, a repetitive track will happily match a bar several seconds
    # away and report that with every appearance of confidence.
    zero = len(a_env) - 1
    per_s = SRC / HOP
    lo = max(0, int(zero + (used_margin - margin) * per_s))
    hi = min(len(corr) - 1, int(zero + (used_margin + margin) * per_s))
    if hi <= lo:
        lo, hi = 0, len(corr) - 1
    window = corr[lo:hi + 1]
    k = lo + int(np.argmax(window))
    lag = (k - zero) * HOP / SRC
    offset = used_margin - lag                        # positive: the room hears it later

    # How far the winning alignment stands out. Music repeats, so the real test is
    # whether the runner-up somewhere else in the window comes close: if it does,
    # the song has told us the same story twice and the answer is a coin toss.
    peak = float(corr[k])
    z = (peak - float(window.mean())) / (float(window.std()) + 1e-9)
    guard = int(0.25 * per_s)
    rivals = np.concatenate([window[:max(0, k - lo - guard)], window[k - lo + guard + 1:]])
    rival = float(rivals.max()) if rivals.size else 0.0
    edge = (peak - rival) / (abs(peak) + 1e-9)
    log(f"  peak {peak:.3f}, {z:.1f} above the rest, {edge * 100:.0f}% clear of the nearest rival")
    return {"offset_ms": int(round(offset * 1000)), "peak": round(peak, 3), "z": round(z, 1),
            "edge": round(edge, 3), "seconds": seconds, "position": round(pos_at_start, 2)}


def live_sync_check(cast, song_path, seconds=8.0, mic_device=None, margin=3.0, log=print, repeats=2) -> dict:
    """Measure the delay more than once and only trust it when the passes agree.

    A single correlation can lock onto the wrong bar of a repetitive track. Two
    passes taken at different points in the song will not make the same mistake
    twice, so agreement between them is the real confidence signal.
    """
    import numpy as np

    wait_for_steady_clock(cast, log)
    runs = []
    for i in range(max(1, repeats)):
        if i:
            time.sleep(0.5)
        runs.append(_one_sync_pass(cast, song_path, seconds, mic_device, margin, log))
        log(f"  pass {i + 1}: {runs[-1]['offset_ms']:+d} ms")
    offsets = [r["offset_ms"] for r in runs]
    median = int(round(float(np.median(offsets))))
    spread = int(max(offsets) - min(offsets))
    z = float(np.median([r["z"] for r in runs]))
    edge = float(np.min([r.get("edge", 1.0) for r in runs]))
    agree = spread <= 80
    # Three things have to hold: the passes agree, the winning alignment stands out
    # from the general run of the correlation, and no other alignment came close.
    # A looping track can satisfy the first two and still be wrong, so edge decides.
    confidence = float(np.clip((1 - spread / 200.0) * 0.45 + np.clip(z / 12.0, 0, 1) * 0.25
                               + np.clip(edge / 0.25, 0, 1) * 0.30, 0, 1)) if agree else 0.15
    return {"offset_ms": median, "spread_ms": spread, "passes": offsets, "z": round(z, 1), "edge": round(edge, 3),
            "confidence": round(confidence, 2), "position": runs[-1]["position"]}


def main(argv=None):
    import argparse
    import json
    from ..govee_lan import local_ip
    from .cast import CastManager

    ap = argparse.ArgumentParser(description="Measure the Cast audio delay with the microphone and store it as the default Cast offset.")
    ap.add_argument("device", help="Cast speaker or group name, e.g. Everywhere")
    ap.add_argument("--port", type=int, default=8765, help="port of the running studio server that serves audio")
    ap.add_argument("--mic", help="input device index or name substring (default: system default)")
    ap.add_argument("--runs", type=int, default=2)
    ap.add_argument("--no-save", action="store_true")
    args = ap.parse_args(argv)

    root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    pdir = os.path.join(root, "shows", "_calibration"); os.makedirs(pdir, exist_ok=True)
    wav = os.path.join(pdir, "song.wav")
    write_calibration_file(wav)
    url = f"http://{local_ip()}:{args.port}/api/audio/_calibration"

    mic = None
    if args.mic:
        import sounddevice as sd
        mic = int(args.mic) if args.mic.isdigit() else next((i for i, d in enumerate(sd.query_devices()) if args.mic.lower() in d["name"].lower() and d["max_input_channels"] > 0), None)
    cm = CastManager(local_ip())
    print(f"Connecting to {args.device!r}...")
    cm.connect(args.device)
    offsets = []
    try:
        for r in range(args.runs):
            print(f"Run {r + 1}/{args.runs}: casting chirps and listening...")
            res = measure(cm, url, wav, mic)
            print(f"  -> offset {res['offset_ms']} ms (spread {res['spread_ms']} ms over {res['n']} chirps)")
            offsets.append(res["offset_ms"])
            time.sleep(1.0)
    finally:
        cm.disconnect()
    offset = int(round(float(np.median(offsets))))
    print(f"\nCast offset for {args.device!r}: {offset} ms")
    if not args.no_save:
        cfg_path = os.path.join(root, "config.json")
        try:
            cfg = json.load(open(cfg_path, encoding="utf-8"))
        except (OSError, ValueError):
            cfg = {}
        cfg.setdefault("cast_offsets", {})[args.device] = offset
        cfg["cast_offset_ms"] = offset
        json.dump(cfg, open(cfg_path, "w", encoding="utf-8"), indent=2)
        print(f"Saved as the default Cast offset in {cfg_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
