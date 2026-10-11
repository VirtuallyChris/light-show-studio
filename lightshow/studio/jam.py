"""Jam mode: somebody else picks the music, the lights still know what is coming.

Spotify plays on this PC. The relay captures it and the house speakers play it
back some tens of seconds late. In that gap the studio does its work:

  * a track that has a show built for it gets that show, lined up by ear;
  * a track heard before gets a full show built from what was learned then;
  * a track never heard before is analysed as it arrives, which still puts the
    lights most of a minute ahead of the room, enough to see a drop coming and
    build into it.

When a track has played all the way through, its analysis is kept: beat grid,
sections, drops, the times of its riff and fills, and the onset envelope that
lets it be recognised by ear next time. Numbers only. The audio itself only
ever exists in the relay's rolling memory buffer and is never written anywhere.

The engine is driven by ticks (`poll`, `analyse`) rather than by its own
threads, so the whole of it can be run in a test with a fake clock, a fake
Spotify and a file standing in for the sound card. `JamRunner` adds the threads
for real use.
"""

from __future__ import annotations

import json
import os
import re
import threading
import time
import unicodedata
from dataclasses import dataclass, field

import numpy as np

from . import audiotrim
from .loopback import Gate
from .relay import Relay, RelayStream
from .spotify import NowPlaying, SpotifyClient, Track

SR_ANALYSIS = 22050
HOP = 512
FEATURE_FPS = 30.0
WAVE_PPS = 50
N_BANDS = 12


# ======================================================================= the library
def _norm(text: str) -> list[str]:
    """Words of a title with the noise taken out, for matching a track to a saved show."""
    text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode().lower()
    text = re.sub(r"['’`]", "", text)
    text = re.sub(r"\b(official|video|audio|lyrics?|remaster(ed)?|hd|hq|feat|ft|version|explicit|visualizer)\b", " ", text)
    return [w for w in re.split(r"[^a-z0-9]+", text) if w and not (w.isdigit() and len(w) == 4)]


def _analysis_version() -> int:
    from .analysis import ANALYSIS_VERSION
    return ANALYSIS_VERSION


class JamLibrary:
    """Shows saved on disk, and what has been learned about tracks heard before."""

    def __init__(self, shows_dir: str):
        self.shows_dir = shows_dir
        self.dir = os.path.join(shows_dir, "_jam")
        self.learned_dir = os.path.join(self.dir, "learned")
        self.links_path = os.path.join(self.dir, "links.json")
        self._links: dict | None = None
        self.colors_path = os.path.join(self.dir, "colors.json")
        self._colors: dict | None = None
        self._projects: tuple[float, list[dict]] | None = None     # (when listed, the list)
        self._durations: dict[tuple, float] = {}                    # (path, size, mtime) -> seconds

    # -- saved shows -------------------------------------------------------------------
    PROJECTS_TTL = 10.0

    def projects(self) -> list[dict]:
        """The saved shows, listed at most every few seconds.

        Measuring a song's length reads the whole file, and this is asked for
        every queued track. Done afresh each time it held the server up for most
        of a second, and the lights, which are fed through the same server,
        stood still while it did.
        """
        now = time.monotonic()
        if self._projects is not None and now - self._projects[0] < self.PROJECTS_TTL:
            return self._projects[1]
        out = []
        if os.path.isdir(self.shows_dir):
            for name in sorted(os.listdir(self.shows_dir)):
                pdir = os.path.join(self.shows_dir, name)
                if name.startswith("_") or not os.path.isfile(os.path.join(pdir, "project.json")):
                    continue
                song = next((f for f in os.listdir(pdir) if f.startswith("song.")), None)
                if song:
                    path = os.path.join(pdir, song)
                    out.append({"name": name, "song": path, "words": set(_norm(name)), "duration": self._duration(path)})
        self._projects = (now, out)
        return out

    def _duration(self, path: str) -> float:
        try:
            st = os.stat(path)
        except OSError:
            return 0.0
        key = (path, st.st_size, st.st_mtime)
        if key not in self._durations:
            self._durations[key] = audiotrim.total_duration(path)
        return self._durations[key]

    def links(self) -> dict:
        if self._links is None:
            try:
                with open(self.links_path, encoding="utf-8") as fh:
                    self._links = json.load(fh)
            except (OSError, ValueError):
                self._links = {}
        return self._links

    def link(self, track_id: str, project: str | None) -> None:
        """Pin a Spotify track to a saved show (or to none, with project='')."""
        links = self.links()
        if project is None:
            links.pop(track_id, None)
        else:
            links[track_id] = project
        os.makedirs(self.dir, exist_ok=True)
        with open(self.links_path, "w", encoding="utf-8") as fh:
            json.dump(links, fh, indent=1)

    # -- colors picked for a track in the jam queue ------------------------------------------
    def colors(self) -> dict:
        """track id -> {"primary": hex, "secondary": hex}: kept for the next time the track plays."""
        if self._colors is None:
            try:
                with open(self.colors_path, encoding="utf-8") as fh:
                    self._colors = json.load(fh)
            except (OSError, ValueError):
                self._colors = {}
        return self._colors

    def set_colors(self, track_id: str, primary: str | None, secondary: str | None) -> None:
        """Give a track its own two colors, or (with None) send it back to the jam's colors."""
        colors = self.colors()
        if primary and secondary:
            colors[track_id] = {"primary": primary, "secondary": secondary}
        else:
            colors.pop(track_id, None)
        os.makedirs(self.dir, exist_ok=True)
        tmp = self.colors_path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(colors, fh, indent=1)
        os.replace(tmp, self.colors_path)

    def match_project(self, track: Track) -> str | None:
        linked = self.links().get(track.id)
        if linked is not None:
            return linked or None                      # '' means "deliberately no custom show"
        title = set(_norm(track.name))
        if not title:
            return None
        artist_words = set(w for a in track.artists for w in _norm(a))
        best = None
        for p in self.projects():
            if not title <= p["words"]:
                continue
            by_artist = bool(artist_words & p["words"])
            by_length = p["duration"] > 0 and abs(p["duration"] - track.duration) <= 4.0
            if by_artist or by_length:
                score = (by_artist, by_length, -len(p["words"]))
                if best is None or score > best[0]:
                    best = (score, p["name"])
        return best[1] if best else None

    def project_song(self, project: str) -> str | None:
        return next((p["song"] for p in self.projects() if p["name"] == project), None)

    # -- learned tracks ----------------------------------------------------------------
    def _learned_path(self, track_id: str) -> str:
        return os.path.join(self.learned_dir, re.sub(r"[^A-Za-z0-9]", "", track_id) + ".json")

    # A learned song is only as good as the analysis that learned it: one saved before the
    # analysis last changed (drums, the tempo cap, buildups only where they climb) is heard afresh.
    def has_learned(self, track_id: str) -> bool:
        try:
            with open(self._learned_path(track_id), encoding="utf-8") as fh:
                head = fh.read(64)                      # the version is written first, so this is cheap
        except OSError:
            return False
        return head.startswith(f'{{"version": {_analysis_version()},')

    def learned(self, track_id: str) -> dict | None:
        try:
            with open(self._learned_path(track_id), encoding="utf-8") as fh:
                doc = json.load(fh)
        except (OSError, ValueError):
            return None
        return doc if doc.get("version") == _analysis_version() else None

    def save_learned(self, track: Track, analysis: dict) -> None:
        os.makedirs(self.learned_dir, exist_ok=True)
        prev = self.learned(track.id) or {}
        doc = {"version": _analysis_version(), "track": track.to_json(), "plays": int(prev.get("plays", 0)) + 1, "learned_at": time.strftime("%Y-%m-%d %H:%M"),
               "analysis": analysis}
        tmp = self._learned_path(track.id) + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(doc, fh)
        os.replace(tmp, self._learned_path(track.id))


# ================================================================== streaming features
class FeatureTrack:
    """Energy, band levels and waveform peaks for everything captured, as it arrives.

    The full analysis is slow and runs a few seconds behind the capture head.
    These are cheap and keep right up with it, so there is always something to
    drive the lights with, even in the seconds after a skip when the delay is
    short and the analysis has not caught up.
    """

    KEEP_S = 900.0                                        # a long party must not grow this without end

    def __init__(self, relay: Relay):
        self.relay = relay
        self.done = 0.0                                   # capture time analysed up to
        self.base = 0.0                                   # capture time of the first value still held
        self.rms = np.zeros(0, dtype=np.float32)          # FEATURE_FPS
        self.bands = np.zeros((0, N_BANDS), dtype=np.float32)
        self.peaks = np.zeros(0, dtype=np.float32)        # WAVE_PPS
        self._mel = None

    def _truncate(self, t: float) -> None:
        """Forget everything after capture time t (the relay trimmed silence off its end)."""
        self.rms = self.rms[: max(0, int(round((t - self.base) * FEATURE_FPS)))]
        self.bands = self.bands[: len(self.rms)]
        self.peaks = self.peaks[: max(0, int(round((t - self.base) * WAVE_PPS)))]
        self.done = self.base + len(self.rms) / FEATURE_FPS

    def update(self) -> None:
        import librosa

        head = self.relay.capture_time
        if head < self.done - 1e-6:
            self._truncate(head)
        step = 1.0 / FEATURE_FPS
        n_new = int((head - self.done) / step)
        if n_new < 8:
            return
        t0, t1 = self.done, self.done + n_new * step
        y = self.relay.audio(max(0.0, t0 - 0.1), t1, SR_ANALYSIS, mono=True)
        lead = int(round((t0 - max(0.0, t0 - 0.1)) * SR_ANALYSIS))
        if self._mel is None:
            self._mel = librosa.filters.mel(sr=SR_ANALYSIS, n_fft=2048, n_mels=N_BANDS, fmin=40, fmax=11000)
        S = np.abs(librosa.stft(y, n_fft=2048, hop_length=HOP)) ** 2
        mel = self._mel @ S
        ft = librosa.frames_to_time(np.arange(S.shape[1]), sr=SR_ANALYSIS, hop_length=HOP) - lead / SR_ANALYSIS
        out_t = np.arange(n_new) * step
        self.rms = np.concatenate([self.rms, np.interp(out_t, ft, np.sqrt(S.mean(axis=0) + 1e-12)).astype(np.float32)])
        db = 10.0 * np.log10(mel + 1e-10)
        self.bands = np.concatenate([self.bands, np.stack([np.interp(out_t, ft, db[k]) for k in range(N_BANDS)], axis=1).astype(np.float32)])
        body = y[lead:]
        n_pk = int(round(n_new * step * WAVE_PPS))
        edges = np.linspace(0, len(body), n_pk + 1).astype(int)
        self.peaks = np.concatenate([self.peaks, np.array([np.abs(body[i:j]).max() if j > i else 0.0 for i, j in zip(edges[:-1], edges[1:])], dtype=np.float32)])
        self.done = t1
        if self.done - self.base > self.KEEP_S * 1.25:    # drop the oldest quarter in one go
            nf = int((self.done - self.KEEP_S - self.base) * FEATURE_FPS)
            self.rms, self.bands, self.peaks = self.rms[nf:], self.bands[nf:], self.peaks[int(nf / FEATURE_FPS * WAVE_PPS):]
            self.base += nf / FEATURE_FPS

    def slice(self, t0: float, t1: float) -> dict:
        """Features for a stretch of capture time, normalised over that stretch the way the offline analysis does."""
        a0, p0 = int(np.floor((t0 - self.base) * FEATURE_FPS)), int(np.floor((t0 - self.base) * WAVE_PPS))
        a, b = max(0, a0), min(int((t1 - self.base) * FEATURE_FPS), len(self.rms))
        pa, pb = max(0, p0), min(int((t1 - self.base) * WAVE_PPS), len(self.peaks))
        if b <= a:
            return {"energy": [], "bands": [], "peaks": []}
        rms = self.rms[a:b]
        energy = rms / (np.percentile(rms, 99.5) + 1e-9)
        bands = self.bands[a:b]
        lo, hi = np.percentile(bands, 5, axis=0), np.percentile(bands, 99.5, axis=0)
        bands = np.clip((bands - lo) / (hi - lo + 1e-9), 0, 1)
        peaks = self.peaks[pa:pb]
        peaks = peaks / (peaks.max() + 1e-9) if len(peaks) else peaks
        # time before anything was recorded (a song already playing when the jam began) is silence,
        # so index 0 is still the song's first moment
        if a0 < 0:
            energy, bands = np.concatenate([np.zeros(-a0), energy]), np.concatenate([np.zeros((-a0, bands.shape[1])), bands])
        if p0 < 0:
            peaks = np.concatenate([np.zeros(-p0), peaks])
        return {"energy": np.round(energy, 4).tolist(), "bands": np.round(bands, 3).tolist(), "peaks": np.round(peaks, 3).tolist()}


# ============================================================================ alignment
def live_bands(y: np.ndarray) -> np.ndarray:
    from .matching import match_features

    return match_features(y, SR_ANALYSIS, HOP)


ALIGN_EDGE = 0.01            # a placement must beat every rival elsewhere by this much to be believed
ALIGN_PEAK = 0.5


def align_start(live: np.ndarray, ref: np.ndarray, window_start: float, fps: float, reach: float = 2.0) -> tuple[float, float, float]:
    """Where a known track's 0:00 sits in capture time, found by ear.

    `live` is the matching features of captured audio beginning at `window_start`
    (a little before the track is believed to start); `ref` is the same for the
    opening of the track itself. Returns (capture time of 0:00, peak, edge): peak
    is how well it matched (1.0 is identical) and edge how far the best placement
    stood clear of any rival more than a quarter of a second away.

    An earlier version weighed candidates by how near they were to where
    Spotify's clock put the start. With these features that made things worse
    (56 of 72 right, against 70 of 72 for the plain best match): the clock is
    stale by an unknown amount, and leaning on it pulls the answer off the truth.
    The two misses that remain are exact ties, which the edge exposes.
    """
    from .matching import best_match

    live, ref = np.atleast_2d(live), np.atleast_2d(ref)
    n_t = min(ref.shape[1], live.shape[1] - int(2 * reach * fps) - 1)
    if n_t < int(8 * fps):
        return window_start, 0.0, 0.0
    m = best_match(live, ref[:, :n_t], guard_frames=int(0.25 * fps))
    return window_start + m["index"] / fps, m["peak"], m["edge"]


# ============================================================================== output
class VirtualOutput:
    """A perfect imaginary speaker, for tests: plays the stream in step with the clock."""

    name = "virtual"

    def __init__(self, clock):
        self.clock = clock
        self.stream: RelayStream | None = None
        self.started_at = 0.0
        self.paused_at: float | None = None
        self.held = 0.0
        self.offset = 0.0
        self.starts = 0

    def start_stream(self, stream: RelayStream) -> None:
        if self.stream:
            self.stream.close()
        self.stream, self.started_at, self.paused_at, self.held = stream, self.clock(), None, 0.0
        self.starts += 1

    def position(self) -> float:
        end = self.paused_at if self.paused_at is not None else self.clock()
        return max(0.0, end - self.started_at - self.held)

    def pump(self) -> None:
        """Pull from the stream what a real speaker would have pulled by now."""
        if not self.stream:
            return
        want = int((self.position() + 1.0) * self.stream.relay.sr) - self.stream.sent
        while want > 0:
            got = self.stream.read(min(want, 8192), timeout=0)
            if not got:
                break
            want -= len(got) // (self.stream.relay.channels * 2)

    def pause(self) -> None:
        if self.paused_at is None:
            self.paused_at = self.clock()

    def resume(self) -> None:
        if self.paused_at is not None:
            self.held += self.clock() - self.paused_at
            self.paused_at = None

    def stop(self) -> None:
        if self.stream:
            self.stream.close()
        self.stream = None


class CastOutput:
    """The real speakers: a Cast device playing the relay's stream."""

    def __init__(self, cast, stream_url, offset_s=lambda: 0.0):
        self.cast, self.stream_url, self._offset = cast, stream_url, offset_s
        self.stream: RelayStream | None = None
        self.sid = 0

    @property
    def name(self) -> str:
        return self.cast.name or "no speaker"

    @property
    def offset(self) -> float:
        return float(self._offset())

    def start_stream(self, stream: RelayStream) -> None:
        if self.stream:
            self.stream.close()
        self.stream, self.sid = stream, self.sid + 1
        self.started_at = time.monotonic()
        self.cast.play_stream(self.stream_url(self.sid), "audio/wav", "Light Show Jam")

    def position(self) -> float:
        return float(self.cast.status().get("position", 0.0))

    def pump(self) -> None:
        pass                                              # the speaker pulls for itself

    def pause(self) -> None:
        self.cast.pause()

    def resume(self) -> None:
        self.cast.resume()

    def stop(self) -> None:
        if self.stream:
            self.stream.close()
        self.stream = None
        try:
            self.cast.stop()
        except Exception:  # noqa: BLE001
            pass


# ============================================================================== engine
@dataclass
class JamTrack:
    track: Track
    start: float                        # capture time of the track's 0:00
    kind: str                           # custom | learned | live
    project: str | None = None
    analysis: dict | None = None        # learned: what was cached; live: the latest pass
    version: int = 0
    horizon: float = 0.0                # seconds of the track the analysis covers
    passes: int = 0
    bpm_lock: float | None = None
    aligned: bool = False
    align_peak: float = 0.0
    align_edge: float = 0.0
    ref: tuple | None = None            # (onset envelope, fps) to align against
    end: float | None = None            # capture time it stopped at, once known
    skipped: bool = False
    saved: bool = False
    serial: int = 0
    genre: dict | None = None           # show style and the genres behind it
    spotify_genres: list = field(default_factory=list)

    @property
    def key(self) -> str:
        return f"{self.track.id}-{self.serial}"

    def to_json(self) -> dict:
        return {**self.track.to_json(), "key": self.key, "kind": self.kind, "project": self.project, "aligned": self.aligned,
                "version": self.version, "horizon": round(self.horizon, 2), "style": (self.genre or {}).get("style")}


@dataclass
class JamSettings:
    target_delay: float = 90.0          # how far behind the capture head the room should be (90 s: time for a full analysis)
    min_delay: float = 3.0              # where the stream restarts after a skip
    max_gap: float = 6.0                # most silence to add between two songs when growing the delay back
    analyse_every: float = 6.0          # new audio needed before the live analysis runs again
    min_audio: float = 12.0             # audio needed before the first pass
    capture_latency: float = 0.06       # Spotify's position versus when the sound card plays it
    events_every: int = 3               # instrument detection is the slow part: every Nth pass
    crossfade: float = 0.0              # Spotify's crossfade setting, in seconds (it cannot be read from the API)
    outro: float = 30.0                 # a change this close to a song's end counts as the song ending
    skip_jumps: bool = False            # a skip jumps the room to the new song now, giving up the delay


class JamEngine:
    def __init__(self, relay: Relay, spotify: SpotifyClient, library: JamLibrary, output, gate: Gate | None = None,
                 settings: JamSettings | None = None, clock=time.monotonic, analyse_fn=None, cache_dir: str | None = None,
                 genre_fn=None, segment_fn=None):
        self.relay, self.spotify, self.library, self.output = relay, spotify, library, output
        self.gate = gate or Gate()
        self.cfg = settings or JamSettings()
        self.clock = clock
        self.cache_dir = cache_dir
        self.features = FeatureTrack(relay)
        self.tracks: list[JamTrack] = []
        self.queue: list[dict] = []       # what is queued, each with its kind, worked out when fetched
        self.now: NowPlaying | None = None
        self.lock = threading.RLock()
        self.log: list[str] = []
        self.streaming = False
        self.serial = 0
        self.flushes = 0
        self.gaps_added = 0.0
        self.error = ""
        self.auto_start = True            # begin streaming by itself once the buffer reaches the target
        self._last_seen = None            # (track id, progress, when, playing) from the previous poll
        self.handover: str | None = None  # None | 'armed' | 'filling': opening with a song from disk
        if analyse_fn is None:
            from .analysis import analyze_audio
            analyse_fn = analyze_audio
        self.analyse_fn = analyse_fn
        # genre_fn(audio, sr) -> genre scores, or None; audio goes to the model straight from memory
        self.genre_fn = genre_fn
        # segment_fn(audio, sr) -> SongFormer's segments for what has been heard, to time the sections
        self.segment_fn = segment_fn

    def say(self, msg: str) -> None:
        self.log.append(f"{time.strftime('%H:%M:%S')} {msg}")
        del self.log[:-60]

    # ---------------------------------------------------------------- where the room is
    def room_capture_time(self) -> float | None:
        """The capture time the room is hearing right now, or None when it hears nothing of ours."""
        if not self.streaming or not self.output.stream:
            return None
        return self.output.stream.capture_time_at(self.output.position() - self.output.offset)

    def delay(self) -> float:
        rc = self.room_capture_time()
        if rc is None:
            s = self.output.stream
            return self.relay.capture_time - (s.cursor / self.relay.sr) if (self.streaming and s) else self.buffered()
        return self.relay.capture_time - rc

    def buffered(self) -> float:
        """Audio waiting to be heard, before streaming has begun."""
        first = self.tracks[0].start if self.tracks else self.relay.capture_time
        return max(0.0, self.relay.capture_time - max(first, self.relay.oldest_time))

    def track_at(self, capture_time: float) -> JamTrack | None:
        for i in range(len(self.tracks) - 1, -1, -1):
            t = self.tracks[i]
            if capture_time >= t.start - 0.05:
                end = t.end if t.end is not None else t.start + t.track.duration + 1.0
                return t if capture_time < end + 0.05 else None
        return None

    @property
    def current(self) -> JamTrack | None:
        """The track at the capture head: what Spotify is playing, not what the room hears."""
        return self.tracks[-1] if self.tracks else None

    # ------------------------------------------------------------------------- polling
    def poll(self) -> None:
        """Ask Spotify what is playing and update the timeline. Call about once a second."""
        with self.lock:
            try:
                np_ = self.spotify.now_playing()
                self.error = ""
            except Exception as e:  # noqa: BLE001 - shown in the UI; the relay keeps running regardless
                self.error = f"{type(e).__name__}: {e}"
                return
            self.now = np_
            head = self.relay.capture_time
            if np_.track is None:
                return
            if not np_.playing:
                self._last_seen = (np_.track.id, np_.progress, np_.fetched_at, False)
                if not self.gate.paused:
                    self.gate.paused = True
                    keep = self.output.stream.cursor if (self.streaming and self.output.stream) else None
                    cut = self.relay.trim_tail_silence(keep_after=keep)
                    self.say(f"paused on the phone: holding the buffer (trimmed {cut:.1f}s of silence)")
                    if self.streaming:
                        self.output.pause()
                return
            if self.gate.paused:
                self.gate.paused = False
                self.say("resumed")
                if self.streaming:
                    self.output.resume()
                head = self.relay.capture_time

            est_start = head - np_.progress - self.cfg.capture_latency
            cur = self.current
            same = cur is not None and cur.track.id == np_.track.id and cur.end is None
            # Whether the same track was restarted or sought is judged on Spotify's own timeline:
            # did its position jump, against its own clock? Comparing with the capture timeline
            # instead mistook every small disagreement between the two clocks for a restart,
            # and each one threw the buffer away and started the song over.
            jumped = False
            if same and self._last_seen is not None and self._last_seen[0] == np_.track.id:
                _, last_prog, last_at, was_playing = self._last_seen
                expected = last_prog + ((np_.fetched_at - last_at) if was_playing else 0.0)
                jumped = abs(np_.progress - expected) > 3.0
            self._last_seen = (np_.track.id, np_.progress, np_.fetched_at, True)
            if same and not jumped:
                # Every reading is late by however long Spotify took to answer, never early, so the
                # smallest is the truest. Frozen once analysis has begun: its times hang off this.
                if not cur.aligned and cur.passes == 0 and 0 < cur.start - est_start < 0.4:
                    cur.start = est_start
                return
            self._begin_track(np_.track, est_start, cur, restarted=same)

    def _begin_track(self, track: Track, est_start: float, prev: JamTrack | None, restarted: bool) -> None:
        natural = False
        if prev is not None and prev.end is None:
            expected_end = prev.start + prev.track.duration
            # A song that simply ended: the next one begins where this one was due to stop, a little
            # later (a gap), or earlier by the crossfade. A change in a song's last stretch is taken
            # as its end too: the room hears the old song up to where Spotify left it, then the new
            # one, and the song is kept as heard. (This was added when songs seemed to end 20 s
            # early; that turned out to be songs already playing when the jam began, measured from
            # the wrong start, since fixed. The rule stays: a skip that late loses little.)
            early = max(self.cfg.crossfade + 1.5, min(self.cfg.outro, 0.5 * prev.track.duration))
            natural = not restarted and -early <= est_start - expected_end <= 3.0
            prev.end = min(est_start, expected_end) if natural else est_start
            prev.skipped = not natural
        project = self.library.match_project(track)
        learned = None if project else self.library.learned(track.id)
        kind = "custom" if project else "learned" if learned else "live"
        self.serial += 1
        # A song already playing when the jam began started before the recording did: its start
        # is earlier than anything held. Clamping it to the recording's first moment measured the
        # whole song from the wrong place, and a first listen was saved shifted by however far in
        # the song already was (Ego: 15.7 s, lights that far ahead every time it came back).
        jt = JamTrack(track, est_start, kind, project, serial=self.serial)
        try:
            jt.spotify_genres = list(getattr(self.spotify, "artist_genres", lambda t: [])(track))
        except Exception:  # noqa: BLE001 - genres are a hint; the jam goes on without them
            jt.spotify_genres = []
        if learned:
            jt.analysis = learned["analysis"]
            jt.genre = learned["analysis"].get("genre")
            jt.version = 1
            jt.horizon = track.duration
            a = learned["analysis"]
            if a.get("align_bands"):
                jt.ref = (np.asarray(a["align_bands"], dtype=np.float32), float(a.get("align_fps", SR_ANALYSIS / HOP)))
        self.tracks.append(jt)
        del self.tracks[:-40]
        self.say(f"{'next up' if natural else 'now'}: {track.label}  [{kind}{' - ' + project if project else ''}]")
        if prev is None:
            return
        if not natural and self.cfg.skip_jumps:
            self._flush(jt)
        elif not natural:
            # The room is a delayed copy of Spotify, skips included: it hears the skip when it gets
            # there, and the lights keep their full lead on the new song.
            if self.streaming:
                self.say(f"skip: the room gets there in {self.delay():.0f}s, the lights keep their lead")
        elif self.streaming:
            short = self.cfg.target_delay - self.delay()
            if short > 1.0:
                gap = min(self.cfg.max_gap, short)
                self.output.stream.add_gap(self._quietest_near(jt.start), gap)
                self.gaps_added += gap
                self.say(f"adding {gap:.1f}s between songs to grow the delay back ({self.delay():.0f}s of {self.cfg.target_delay:.0f}s)")

    def _quietest_near(self, t: float, reach: float = 1.2) -> float:
        """The quietest moment around a song boundary, so inserted silence lands in the seam."""
        t0, t1 = max(self.relay.oldest_time, t - reach), min(self.relay.capture_time, t + reach)
        x = self.relay.frames(t0, t1)
        win = int(0.05 * self.relay.sr)
        if len(x) < win * 2:
            return t
        level = np.abs(x[: len(x) // win * win].astype(np.int32)).max(axis=1).reshape(-1, win).mean(axis=1)
        return t0 + (int(np.argmin(level)) + 0.5) * win / self.relay.sr

    def _flush(self, jt: JamTrack) -> None:
        """A skip: drop what was waiting and put the room on the new track at once."""
        if not self.streaming:
            return
        start = max(jt.start, self.relay.oldest_time)
        self.output.start_stream(self.relay.open_stream(start, pace=not isinstance(self.output, VirtualOutput)))
        self.flushes += 1
        self.say(f"skip: buffer flushed, the room joins {jt.track.name} now")

    # ------------------------------------------------------------------ start and stop
    def start_streaming(self, from_time: float | None = None) -> None:
        with self.lock:
            if from_time is None:
                first = next((t for t in self.tracks if t.end is None or t.end > self.relay.oldest_time), None)
                from_time = max(first.start if first else self.relay.capture_time, self.relay.oldest_time)
                # never start further back than the target: a long wait is cut down to it
                from_time = max(from_time, self.relay.capture_time - self.cfg.target_delay * 2)
            self.output.start_stream(self.relay.open_stream(from_time, pace=not isinstance(self.output, VirtualOutput)))
            self.streaming = True
            self.say(f"speakers started {self.relay.capture_time - from_time:.0f}s behind")

    def stop(self) -> None:
        with self.lock:
            self.streaming = False
            self.output.stop()

    # ------------------------------------------------------------------ opening song
    def arm_handover(self) -> None:
        """Open the night with a song from disk and slide into the jam behind it.

        A buffer can only fill while the room is listening to something else. So
        the studio plays one of your own shows first, holds Spotify paused, lets
        it go when the opener has exactly the target delay left to run, and
        switches the speakers over to the relay as the opener ends. The room
        hears one song follow another; the lights are already most of a minute
        ahead.
        """
        with self.lock:
            self.handover, self.auto_start = "armed", False
            try:
                self.spotify.pause()
            except Exception:  # noqa: BLE001 - not Premium, or nothing active yet: she can press play herself
                pass
            self.say("armed: Spotify is held until the opening song is nearly over")

    def handover_tick(self, remaining: float | None, playing: bool) -> None:
        """Call about once a second with how much of the opening song is left."""
        with self.lock:
            if self.handover == "armed" and remaining is not None and remaining <= self.cfg.target_delay:
                ok = False
                try:
                    ok = self.spotify.resume()
                except Exception:  # noqa: BLE001
                    pass
                self.handover = "filling"
                self.say("Spotify released: filling the buffer behind the opening song" if ok
                         else "press play in Spotify now: the opening song is nearly over")
            elif self.handover == "filling" and (not playing or (remaining is not None and remaining <= 0.4)):
                self.handover, self.auto_start = None, True
                if self.buffered() > 1.0:
                    self.start_streaming()
                else:
                    self.say("the opening song ended with nothing buffered; starting as soon as there is music")

    # ------------------------------------------------------------------------ analysis
    def analyse(self) -> str | None:
        """Do one piece of analysis work, if any is due. Returns what it did."""
        self.features.update()
        with self.lock:
            tracks = list(self.tracks)
            head = self.relay.capture_time
        for jt in tracks:                                  # line up known tracks by ear
            if jt.kind != "live" and not jt.aligned:
                if jt.end is not None and jt.end - jt.start < 14.0:
                    jt.aligned = True                       # gone before there was enough to listen to
                elif self._align(jt, head):
                    return f"aligned {jt.track.name}"
        for jt in tracks:                                  # finish a first listen that played through
            if jt.kind == "live" and jt.end is not None and not jt.saved and not jt.skipped:
                return self._finish(jt)
        cur = tracks[-1] if tracks else None
        if cur and cur.kind == "live" and cur.end is None and cur.genre is None and head - cur.start >= 30.0:
            return self._genre(cur, min(head - cur.start, cur.track.duration))
        if cur and cur.kind == "live" and cur.end is None:
            have = min(head - cur.start, cur.track.duration)
            if have >= self.cfg.min_audio and have - cur.horizon >= self.cfg.analyse_every:
                return self._pass(cur, have, final=False)
        return None

    def _align(self, jt: JamTrack, head: float) -> bool:
        have = (jt.end or head) - max(jt.start, self.relay.oldest_time)     # what was actually recorded of it
        if have < 14.0:
            return False
        if jt.ref is None:
            jt.ref = self._reference(jt)
            if jt.ref is None:
                jt.aligned = True                           # nothing to align against: trust Spotify's clock
                return False
        env, fps = jt.ref
        env = np.atleast_2d(env)
        reach = 2.0
        w0 = max(self.relay.oldest_time, jt.start - reach)
        # A song already playing when the jam began has no opening in the recording: match the
        # part of the saved song that the recording's first moment could be, not its first seconds.
        q0 = max(0.0, w0 - (jt.start - reach))            # song time at w0 for the earliest start searched
        y = self.relay.audio(w0, min(w0 + 24.0 + reach, jt.end or head), SR_ANALYSIS, mono=True)
        start, peak, edge = align_start(live_bands(y), env[:, int(q0 * fps):], w0 - q0, fps, reach=reach)
        jt.align_peak, jt.align_edge = peak, edge
        if peak >= ALIGN_PEAK and edge >= ALIGN_EDGE and abs(start - jt.start) <= reach + 0.5:
            moved = start - jt.start
            with self.lock:
                jt.start, jt.aligned, jt.version = start, True, jt.version + 1
            self.say(f"lined up {jt.track.name} by ear ({moved * 1000:+.0f} ms, match {peak:.2f})")
        else:
            jt.aligned = True
            why = "it matched equally well a beat away" if peak >= ALIGN_PEAK else "it did not sound like the saved version"
            self.say(f"could not line up {jt.track.name} by ear ({why}); using Spotify's clock")
        return True

    def _reference(self, jt: JamTrack):
        if jt.kind == "custom" and jt.project:
            song = self.library.project_song(jt.project)
            if song and self.cache_dir is not False:
                try:
                    from .analysis import analyze_cached
                    a = analyze_cached(song, os.path.join(os.path.dirname(song), "cache"))
                    if a.get("align_bands"):
                        return np.asarray(a["align_bands"], dtype=np.float32), float(a.get("align_fps", SR_ANALYSIS / HOP))
                except Exception as e:  # noqa: BLE001
                    self.say(f"could not read the analysis of {jt.project}: {e}")
        return None

    def _genre(self, jt: JamTrack, have: float) -> str:
        """Decide how hard this track's lights should go, from half a minute of it and its artist's genres."""
        from .genre import style_from

        labels = None
        if self.genre_fn is not None:
            try:
                labels = self.genre_fn(self.relay.audio(max(jt.start, self.relay.oldest_time), jt.start + have, 16000, mono=True), 16000)
            except Exception as e:  # noqa: BLE001
                self.say(f"genre model unavailable: {e}")
        drive = float(((jt.analysis or {}).get("mood") or {}).get("drive", 0.6))
        g = style_from(labels, drive, jt.spotify_genres)
        with self.lock:
            jt.genre, jt.version = g, jt.version + 1
            if jt.analysis is not None:
                jt.analysis["genre"] = g
        return f"{jt.track.name}: {g['style']} ({', '.join(g['genres'] or g['spotify'] or ['no genre found'])})"

    def _pass(self, jt: JamTrack, have: float, final: bool) -> str:
        y = self.relay.audio(jt.start, jt.start + have, SR_ANALYSIS, mono=True, pad=True)   # song time 0 is sample 0
        events = final or (jt.passes % self.cfg.events_every == self.cfg.events_every - 1)
        stereo = self.relay.audio(jt.start, jt.start + have, SR_ANALYSIS, mono=False, pad=True) if events else None
        a = self.analyse_fn(y, SR_ANALYSIS, stereo=stereo, use_model=False, events=events, bpm_hint=jt.bpm_lock,
                            segment_fn=self.segment_fn)
        if not events and jt.analysis:                      # keep the last instrument pass until the next one
            for k in ("events", "fills", "hook", "curves"):
                a[k] = jt.analysis.get(k, a[k])
        if jt.genre:
            a["genre"] = jt.genre                          # what was decided about the genre carries across passes
        with self.lock:
            jt.passes += 1
            if jt.passes >= 2 and jt.bpm_lock is None:
                jt.bpm_lock = float(a["bpm"])
            jt.analysis, jt.horizon, jt.version = a, have, jt.version + 1
        return f"analysed {have:.0f}s of {jt.track.name}: {a['bpm']:.1f} bpm, {len(a['sections'])} sections, {len(a['drops'])} drops"

    def _finish(self, jt: JamTrack) -> str:
        have = jt.end - jt.start
        jt.saved = True
        if have < max(30.0, 0.9 * jt.track.duration) or jt.start < self.relay.oldest_time:
            return f"{jt.track.name}: not enough of it was heard to keep"
        msg = self._pass(jt, have, final=True)
        self.library.save_learned(jt.track, jt.analysis)
        self.say(f"learned {jt.track.label}: next time it gets a full show from the first beat")
        return msg + " (saved)"

    # --------------------------------------------------------------------- for the UI
    def refresh_queue(self) -> None:
        try:
            q = [self._kind_of(t) for t in self.spotify.queue()[:12]]
        except Exception:  # noqa: BLE001
            return
        with self.lock:
            self.queue = q

    def _kind_of(self, track: Track) -> dict:
        project = self.library.match_project(track)
        return {**track.to_json(), "kind": "custom" if project else "learned" if self.library.has_learned(track.id) else "live", "project": project}

    def state(self) -> dict:
        with self.lock:
            rc = self.room_capture_time()
            rt = self.track_at(rc) if rc is not None else None
            room = None
            if rt is not None:
                room = {**rt.to_json(), "position": round(rc - rt.start, 3), "playing": not self.gate.paused, "at": self.clock()}
            ahead = self.current
            return {
                "streaming": self.streaming, "delay": round(self.delay(), 2), "target_delay": self.cfg.target_delay,
                "buffered": round(self.buffered(), 1), "paused": self.gate.paused, "speaker": self.output.name,
                "room": room, "ahead": ahead.to_json() if ahead else None,
                "spotify": {"signed_in": self.spotify.signed_in, "device": self.now.device if self.now else "", "error": self.error},
                "queue": list(self.queue), "flushes": self.flushes, "log": self.log[-12:],
                "handover": self.handover,
            }

    def analysis_json(self, key: str) -> dict | None:
        """What the studio needs to build and run a show for one track on the timeline.

        For a first listen the beat grid and sections come from the latest pass,
        stretched over the whole track: beats carry on at the found tempo and the
        part not analysed yet is one provisional section. Levels and waveform come
        from the streaming features, which reach all the way to the capture head.
        """
        with self.lock:
            jt = next((t for t in self.tracks if t.key == key), None)
            if jt is None:
                return None
            base = {"key": jt.key, "kind": jt.kind, "project": jt.project, "version": jt.version, "track": jt.track.to_json(),
                    "horizon": round(jt.horizon, 2)}
            if jt.kind == "custom":
                return base
            if jt.kind == "learned":
                return {**base, "analysis": jt.analysis}
            a, dur = jt.analysis, jt.track.duration
            head = min(self.relay.capture_time - jt.start, dur)
            feat = self.features.slice(jt.start, jt.start + head)
        n_f, n_p = int(dur * FEATURE_FPS), int(dur * WAVE_PPS)
        pad = lambda xs, n, fill: (xs + [fill] * max(0, n - len(xs)))[:n]
        out = {"duration": dur, "feature_fps": FEATURE_FPS, "wave_pps": WAVE_PPS, "structure_source": "live",
               "energy": pad(feat["energy"], n_f, 0.0), "bands": pad(feat["bands"], n_f, [0.0] * N_BANDS), "peaks": pad(feat["peaks"], n_p, 0.0)}
        if a is None:                                        # nothing analysed yet: a plain grid and one open section
            out.update({"bpm": 120.0, "downbeat_phase": 0, "beats": [round(i * 0.5, 4) for i in range(int(dur * 2))], "provisional": True,
                        "sections": [{"start": 0.0, "end": dur, "type": "verse", "energy": 0.6, "provisional": True}], "drops": [],
                        "mood": {}, "events": {}, "fills": [], "hook": None, "curves": {}})
        else:
            period = 60.0 / a["bpm"]
            beats = list(a["beats"])
            t = (beats[-1] + period) if beats else 0.0
            while t < dur:
                beats.append(round(t, 4))
                t += period
            sections = [dict(s) for s in a["sections"]]
            drops = list(a["drops"])
            if jt.horizon < min(dur - 1.0, 75.0):
                # Part way through, "loud" only means louder than what has been heard so far, and
                # early on that is just the first verse, with the intro before it passing for a
                # buildup. Until there is enough of the song to judge by, nothing in the first
                # half minute is a buildup or a drop. Real ones that early are rare, and being
                # late to one is far better than strobing through verse one of every new song.
                for i, sec in enumerate(sections):
                    if sec["start"] < 30.0 and sec["type"] in ("drop", "buildup"):
                        if sec["type"] == "drop":
                            drops = [d for d in drops if not (sec["start"] - 0.5 <= d < sec["end"])]
                        sec["type"] = "intro" if i == 0 else "verse"
            if sections and jt.horizon < dur - 1.0:
                # the last section of a partial analysis is only "the end" because the audio ran out
                last = sections[-1]
                last["end"], last["provisional"] = dur, True
                if last["type"] == "outro":
                    last["type"] = "verse"
            out["genre"] = jt.genre
            out.update({"bpm": a["bpm"], "downbeat_phase": a["downbeat_phase"], "beats": beats, "sections": sections,
                        "drops": drops, "mood": a.get("mood", {}), "events": a.get("events", {}), "fills": a.get("fills", []),
                        "hook": a.get("hook"), "curves": a.get("curves", {}), "provisional": jt.horizon < dur - 1.0})
        return {**base, "analysis": out, "analysed_to": round(head, 2)}


# ============================================================================== runner
class JamRunner:
    """Threads around the engine for real use: capture is already running, this polls and analyses."""

    def __init__(self, engine: JamEngine, file_status=None):
        self.engine = engine
        self.file_status = file_status            # () -> (seconds left of a song from disk, playing?) for the hand-over
        self.stop_flag = threading.Event()
        self.threads: list[threading.Thread] = []

    def start(self) -> None:
        self.stop_flag.clear()
        for fn in (self._poll, self._analyse):
            t = threading.Thread(target=fn, daemon=True)
            t.start()
            self.threads.append(t)

    def stop(self) -> None:
        self.stop_flag.set()
        self.engine.stop()

    def _poll(self) -> None:
        n = 0
        while not self.stop_flag.wait(1.0):
            e = self.engine
            try:
                e.poll()
                n += 1
                if n % 6 == 1:
                    e.refresh_queue()
                if e.handover and self.file_status:
                    e.handover_tick(*self.file_status())
                if not e.streaming and e.auto_start and e.buffered() >= e.cfg.target_delay:
                    e.start_streaming()
                e.output.pump()
            except Exception as ex:  # noqa: BLE001 - keep following the music whatever happens
                e.say(f"hiccup: {type(ex).__name__}: {ex}")

    def _warm_up(self) -> None:
        """Run the analysis once on a few seconds of noise.

        The first call into the audio libraries compiles and caches a good deal of
        code and can take ten seconds or more. Spent here, it is over before the
        first song needs it; otherwise the first track of the night gets its show late.
        """
        try:
            rng = np.random.default_rng(0)
            y = (0.1 * rng.standard_normal(SR_ANALYSIS * 8)).astype(np.float32)
            self.engine.analyse_fn(y, SR_ANALYSIS, stereo=np.stack([y, y]), use_model=False, events=True,
                                   segment_fn=self.engine.segment_fn)
            live_bands(y)
        except Exception:  # noqa: BLE001 - a warm-up that fails costs nothing
            pass

    def _analyse(self) -> None:
        from . import priority
        priority.low_thread()                     # analysis only has to get done; sound and lights come first
        self._warm_up()
        while not self.stop_flag.wait(0.5):
            try:
                self.engine.output.pump()
                did = self.engine.analyse()
                if did:
                    self.engine.say(did)
            except Exception as e:  # noqa: BLE001 - one bad pass must not end the party
                self.engine.say(f"analysis hiccup: {type(e).__name__}: {e}")
                time.sleep(2.0)
