"""Learn ahead: a song's show is worked out before it plays, from a copy found on YouTube.

The jam normally has to hear a song to learn it, which is why the room stays a song behind. With
spotDL (https://github.com/spotdl/spotify-downloader) the studio takes a queued song's details from
Spotify (title, artists, length), finds the same recording on YouTube Music (or YouTube), and
downloads it. The full analysis runs on it at once, much faster than the song plays, and the
download is deleted the moment the analysis is done: only the numbers are kept, exactly as a song
learned by listening is kept (shows/_jam/learned). When the song comes up, the jam lines the saved
analysis up with what Spotify actually plays, by ear, so a copy with a slightly different start
still lands on the beat. A copy whose length is not within a few seconds of Spotify's is taken to be
another version (a radio edit, a video with an intro) and not used.

Spotify's own audio is never touched: spotDL reads only the song's details from Spotify. The copy
comes from YouTube, which its terms do not allow; it exists only for as long as the analysis takes.

spotDL lives in its own Python (.venv-spotdl), run as a separate program, so its pinned packages
never meet the studio's. Without it, nothing here runs and the jam listens as before.
"""

from __future__ import annotations

import glob
import os
import sys
import shutil
import subprocess
import tempfile
import threading
import time
from collections import deque

from .model_worker import ModelWorker

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
SPOTDL = os.path.join(ROOT, ".venv-spotdl", "Scripts" if os.name == "nt" else "bin", "spotdl" + (".exe" if os.name == "nt" else ""))
SPOTDL_HOME = os.path.join(os.path.expanduser("~"), ".spotdl")   # where `spotdl --download-deno` puts Deno
LENGTH_SLACK_S = 8.0            # a copy this much longer or shorter than Spotify's is another version (a little
                                # more silence at an end is fine: the jam checks the copy against Spotify by ear)
FETCH_TIMEOUT_S = 240
RETRY_AFTER_S = 30 * 60         # a song with no usable copy (none found, another version) is tried again after this
RETRY_DOWNLOAD_S = 5 * 60       # one whose download failed (YouTube refuses now and then: HTTP 403) much sooner
DOWNLOAD_TRIES = 3              # and before that it is tried again at once, a few seconds apart


def available() -> bool:
    return os.path.isfile(SPOTDL) and shutil.which("ffmpeg") is not None


def _download(track, folder: str) -> str:
    """The song's copy, downloaded into folder as it comes (no re-encoding): its path."""
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0) | getattr(subprocess, "BELOW_NORMAL_PRIORITY_CLASS", 0)
    # Deno (installed with `spotdl --download-deno`) lets YouTube's downloader get the copies that need it
    # ("made for kids" ones among them): its folder goes first on the path spotDL sees.
    env = {**os.environ, "PATH": SPOTDL_HOME + os.pathsep + os.environ.get("PATH", "")}
    r = subprocess.run([SPOTDL, "download", f"https://open.spotify.com/track/{track.id}", "--output",
                        os.path.join(folder, "{track-id}.{output-ext}"), "--format", "m4a", "--bitrate", "disable",
                        "--lyrics", "--skip-album-art",      # (no lyrics searched, no art: not needed, and seconds a song)
                        "--print-errors"], capture_output=True, text=True, timeout=FETCH_TIMEOUT_S, creationflags=flags, env=env)
    got = [p for p in glob.glob(os.path.join(folder, track.id + ".*")) if not p.endswith(".part")]
    if not got:
        tail = (r.stdout + r.stderr).strip().splitlines()[-1:] or ["no copy found"]
        raise LookupError(tail[0][:160])
    return got[0]


def _spotdl_python() -> str:
    return os.path.join(os.path.dirname(SPOTDL), "python" + (".exe" if os.name == "nt" else ""))


def _words(text: str) -> str:
    import re
    return " ".join(re.findall(r"[a-z0-9]+", (text or "").lower()))


NOT_THE_RECORDING = ("live", "cover", "remix", "karaoke", "instrumental", "sped up", "slowed", "nightcore", "8d", "reverb",
                     "acoustic", "demo", "mashup", "concert", "tour", "session", "reaction")


def pick_alternative(track, results: list[dict]) -> str | None:
    """Among YouTube search results ({id, duration, channel, title}), the one most likely to be the very
    recording Spotify plays: within 3 s of its length, its name in the title, from the artist's own channel
    (best of all their "Artist - Topic" channel, which carries the album audio itself), and not a live
    take, a cover or a remix (unless the Spotify title says so). None if nothing is trustworthy."""
    name, artist = _words(track.name), _words(track.artists[0] if track.artists else "")
    best = None
    for r in results:
        title, channel = _words(r.get("title")), _words(r.get("channel"))
        try:
            dur = float(r.get("duration") or 0)
        except (TypeError, ValueError):
            continue
        if abs(dur - float(track.duration)) > 3.0 or not name or name.split("(")[0].strip() not in title:
            continue
        if any(f" {w} " in f" {title} " and f" {w} " not in f" {name} " for w in NOT_THE_RECORDING):
            continue
        score = 3 if channel == f"{artist} topic" else 2 if artist and (artist in channel or channel.replace("vevo", "").strip() in artist.replace(" ", "")) else 0
        if score == 0:
            continue
        key = (score, -abs(dur - float(track.duration)))
        if best is None or key > best[0]:
            best = (key, r["id"])
    return f"https://www.youtube.com/watch?v={best[1]}" if best else None


YTMUSIC_SEARCH = r"""
import json, sys
from ytmusicapi import YTMusic
out = []
for r in YTMusic().search(sys.argv[1], filter="songs", limit=10):
    if r.get("videoId"):
        out.append({"id": r["videoId"], "duration": r.get("duration_seconds"), "title": r.get("title") or "",
                    "artists": [a.get("name") or "" for a in r.get("artists") or []]})
print(json.dumps(out))
"""


def _search_ytmusic(track) -> list[dict]:
    """YouTube Music's songs for it (the album recordings, with their artists and lengths; nothing is
    downloaded). spotDL's own YouTube Music search came back empty for songs plainly there, and then
    fell back to whatever video YouTube had (a music video, a lyric video with an intro)."""
    import json
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    q = f"{track.artists[0] if track.artists else ''} {track.name}".strip()
    r = subprocess.run([_spotdl_python(), "-c", YTMUSIC_SEARCH, q], capture_output=True, text=True, timeout=60, creationflags=flags)
    try:
        return json.loads(r.stdout.strip().splitlines()[-1])
    except (ValueError, IndexError):
        return []


def pick_ytmusic(track, results: list[dict]) -> str | None:
    """The YouTube Music song that is the recording Spotify plays: one of its artists, its name, within 3 s
    of its length, and not another take (live, remix...) unless the Spotify title says so."""
    name = _words(track.name)
    artists = {_words(a) for a in track.artists if a}
    best = None
    for r in results:
        title = _words(r.get("title"))
        try:
            dur = float(r.get("duration") or 0)
        except (TypeError, ValueError):
            continue
        if abs(dur - float(track.duration)) > 3.0 or not ({_words(a) for a in r.get("artists") or []} & artists):
            continue
        base = name.split(" feat ")[0].split(" ft ")[0]
        if base not in title and title not in name:
            continue
        if any(f" {w} " in f" {title} " and f" {w} " not in f" {name} " for w in NOT_THE_RECORDING):
            continue
        key = (title == name, -abs(dur - float(track.duration)))
        if best is None or key > best[0]:
            best = (key, r["id"])
    return f"https://music.youtube.com/watch?v={best[1]}" if best else None


def _search_youtube(track) -> list[dict]:
    """YouTube's results for the song (titles, channels and lengths only: nothing is downloaded)."""
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    env = {**os.environ, "PATH": SPOTDL_HOME + os.pathsep + os.environ.get("PATH", "")}
    q = f"{track.artists[0] if track.artists else ''} {track.name}".strip()
    r = subprocess.run([_spotdl_python(), "-m", "yt_dlp", "--flat-playlist", "--no-warnings", "--print",
                        "%(id)s\t%(duration)s\t%(channel)s\t%(title)s", f"ytsearch12:{q}"],
                       capture_output=True, text=True, timeout=90, creationflags=flags, env=env)
    out = []
    for line in r.stdout.splitlines():
        parts = line.split("\t")
        if len(parts) == 4:
            out.append({"id": parts[0], "duration": parts[1], "channel": parts[2], "title": parts[3]})
    return out


def _download_from(track, folder: str, url: str) -> str:
    """The YouTube video chosen here (not spotDL's pick), its audio as it comes. With yt-dlp itself (spotDL's
    own downloader, in its environment): 2.3 s for a 3-minute song, where spotDL took 17 s (it also looks the
    track up on Spotify, searches lyric sites and fetches and embeds the album art, none of it needed here).
    spotDL ("youtube url|spotify url") only if yt-dlp cannot."""
    for p in glob.glob(os.path.join(folder, track.id + ".*")):
        os.remove(p)
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0) | getattr(subprocess, "BELOW_NORMAL_PRIORITY_CLASS", 0)
    # Deno (installed with `spotdl --download-deno`) lets YouTube's downloader get the copies that need it
    env = {**os.environ, "PATH": SPOTDL_HOME + os.pathsep + os.environ.get("PATH", "")}
    try:
        subprocess.run([_spotdl_python(), "-m", "yt_dlp", "-f", "bestaudio[ext=m4a]/bestaudio", "--no-playlist", "--no-progress", "-q",
                        "-o", os.path.join(folder, track.id + ".%(ext)s"), url],
                       capture_output=True, text=True, timeout=FETCH_TIMEOUT_S, creationflags=flags, env=env)
    except (OSError, subprocess.SubprocessError):
        pass
    got = [p for p in glob.glob(os.path.join(folder, track.id + ".*")) if not p.endswith(".part")]
    if got:
        return got[0]
    for p in glob.glob(os.path.join(folder, track.id + ".*")):
        os.remove(p)
    r = subprocess.run([SPOTDL, "download", f"{url}|https://open.spotify.com/track/{track.id}", "--output",
                        os.path.join(folder, "{track-id}.{output-ext}"), "--format", "m4a", "--bitrate", "disable",
                        "--lyrics", "--skip-album-art", "--print-errors"], capture_output=True, text=True, timeout=FETCH_TIMEOUT_S, creationflags=flags, env=env)
    got = [p for p in glob.glob(os.path.join(folder, track.id + ".*")) if not p.endswith(".part")]
    if not got:
        tail = (r.stdout + r.stderr).strip().splitlines()[-1:] or ["the chosen copy did not download"]
        raise LookupError(tail[0][:160])
    return got[0]


def _to_flac(src: str) -> str:
    """The analysis reads FLAC (not m4a): converted losslessly, in well under a second."""
    out = os.path.splitext(src)[0] + ".flac"
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-i", src, out], check=True, timeout=120, creationflags=flags)
    return out


def _length(path: str) -> float:
    import soundfile as sf
    return float(sf.info(path).duration)


def analyze_here(path: str, cache_dir: str) -> dict:
    """The full analysis of a copy (what the analysis worker runs), with the whole song's fingerprint."""
    import time as _t
    from . import analysis
    t0 = _t.perf_counter()
    analysis.LAPS = None
    a = dict(analysis.analyze_cached(path, cache_dir))
    t1 = _t.perf_counter()
    a["align_bands"] = whole_fingerprint(path)
    laps = analysis.LAPS
    # one line per song in models.log: where the time went (the analysis worker's prints go there)
    print(f"{_t.strftime('%H:%M:%S')} [timing] {os.path.basename(path)}: {_t.perf_counter() - t0:.1f} s in all"
          + (f"; analysis {laps.line()}" if laps else "") + f"; genre and the rest {t1 - t0 - (sum(v for _, v in laps.steps) if laps else 0):.1f}"
          + f"; fingerprint {_t.perf_counter() - t1:.1f}", file=sys.stderr, flush=True)
    return a


class AnalysisWorker(ModelWorker):
    """The analysis in its own process (analysis_worker.py): the studio's Python, below normal priority,
    a few cores at most, so the lights and the music, in the studio's process, always come first."""

    env_extra = {k: "4" for k in ("OMP_NUM_THREADS", "OPENBLAS_NUM_THREADS", "MKL_NUM_THREADS", "NUMBA_NUM_THREADS")}
    hosted = False                                       # a process of its own (the studio's Python, not the models')

    def _start(self) -> bool:
        # the model host first, owned by this (the studio's) process: the analysis process is told its
        # address and uses the same models instead of starting a second set of its own
        from .model_worker import HOST
        HOST.connect()
        return super()._start()

    def python(self) -> str:
        exe = sys.executable
        console = os.path.join(os.path.dirname(exe), "python.exe")        # (pythonw.exe when started hidden)
        return console if os.path.basename(exe).lower() == "pythonw.exe" and os.path.exists(console) else exe


ANALYSER = AnalysisWorker("lightshow.studio.analysis_worker", "analysis", start_timeout=120, request_timeout=900)


def _analyze(path: str, cache_dir: str, urgent: bool = False) -> dict:
    """In the analysis worker; in this process only if the worker cannot run. `urgent`: the song is
    playing now, unlearned: the worker runs it at above-normal priority (else below normal)."""
    if ANALYSER.available():
        a = ANALYSER._ask({"path": os.path.abspath(path), "cache": os.path.abspath(cache_dir), **({"urgent": True} if urgent else {})})
        if a is not None:
            return a
        if ANALYSER.last_error and not ANALYSER.last_error.startswith("the model"):
            raise RuntimeError(ANALYSER.last_error)        # the analysis itself failed: say so
    return analyze_here(path, cache_dir)


def whole_fingerprint(path: str) -> list:
    """What the jam matches by ear, for the whole song rather than its first 45 s: a song learned from a
    copy is checked against what Spotify plays all the way through, not only lined up at its start."""
    import librosa

    from .analysis import HOP, SR
    from .matching import match_features
    y, _ = librosa.load(path, sr=SR, mono=True)
    return [[round(float(v), 2) for v in row] for row in match_features(y, SR, HOP)]


class FetchAhead:
    """One song downloads while the one before it is analysed: two workers, a song at a time each."""

    def __init__(self, library, download=_download, to_flac=_to_flac, length=_length, analyze=_analyze,
                 is_available=available, clock=time.monotonic, log=print, search=_search_youtube, download_from=_download_from,
                 search_music=_search_ytmusic, keep=lambda: False, lyrics=None):
        self.library = library
        # lyrics.learn: each song's lyrics with word times, from the copy right after it is learned (None: none)
        self.lyrics = lyrics
        # config jam_keep_youtube_copies (off by default): keep each copy after it is learned, in
        # shows/_jam/copies, and learn from it again instead of downloading it (a relearn, a new analysis)
        self.keep = keep
        self.download, self.to_flac, self.length, self.analyze = download, to_flac, length, analyze
        self.search, self.download_from, self.search_music = search, download_from, search_music
        self.is_available, self.clock, self.log = is_available, clock, log
        self.retry_pause = 4.0
        self.lock = threading.Condition()
        self.waiting: deque = deque()                 # tracks to download, the next to play first
        self.ready: deque = deque()                   # (track, folder, file) downloaded, to analyse
        self.busy: dict = {"download": None, "analyse": None}
        self.failed: dict[str, tuple[float, str, str]] = {}   # track id -> (when, label, why)
        self.learned_now = 0
        self.now: str | None = None                   # the song playing unlearned (submit): all effort to it
        self.checked: dict[str, bool] = {}
        self.order: list[str] = []                    # the jam's queue as last handed over (ids, next first)            # track id -> learned without anything failing (see _learned_well)
        self.threads: list[threading.Thread] = []

    # -- what to learn ------------------------------------------------------------------------------
    def _measure_copy(self, track, path: str) -> None:
        """How loud the copy is (loudness.py), kept with the song: it plays evened out from its first note.
        Measured in stereo at the copy's own rate, as the relay measures what plays."""
        if not hasattr(self.library, "save_level"):
            return
        try:
            import librosa
            from .loudness import measure
            y, sr = librosa.load(path, sr=None, mono=False)
            m = measure(y.T if y.ndim == 2 else y, int(sr))
            if m:
                self.library.save_level(track.id, "copy", m)
        except Exception as e:  # noqa: BLE001 - it is measured as it plays instead
            self.log(f"[learn ahead] {track.label}: loudness not measured ({type(e).__name__}: {e})")

    def _learned_well(self, track_id: str) -> bool:
        """Learned, and nothing failed doing it. A song whose analysis had something fail (a model that
        was not running) is learned again, once each time the studio runs: read once, not every poll."""
        if not self.library.has_learned(track_id):
            return False
        if track_id not in self.checked:
            doc = self.library.learned(track_id) or {}
            self.checked[track_id] = not ((doc.get("analysis") or {}).get("problems"))
        return self.checked[track_id]

    def _wanted(self, t) -> bool:
        if not getattr(t, "id", None) or self._learned_well(t.id) or self.library.match_project(t):
            return False
        f = self.failed.get(t.id)
        if f and self.clock() - f[0] < (RETRY_DOWNLOAD_S if "download" in f[2].lower() else RETRY_AFTER_S):
            return False
        held = [t2.id for t2 in self.waiting] + [x[0].id for x in self.ready] + [b.id for b in self.busy.values() if b]
        return t.id not in held

    def submit(self, tracks, first: bool = True, now: str | None = None) -> int:
        """Songs to learn ahead. The jam's queue goes first, in the queue's own order (the next to play
        first, a song just added at its end last), ahead of anything else waiting (a playlist's). `now`: the
        song already playing unlearned (the jam just started, or a skip to a song not yet learned): it is
        downloaded past the two-copy limit and analysed at above-normal priority, all effort on it."""
        if not self.is_available():
            return 0
        with self.lock:
            if first:
                self.now = now
            new = [t for t in tracks if self._wanted(t)]
            if first:
                ids = [t.id for t in tracks]
                self.order = ids                          # the queue as it stands: the analyses follow it too
                queued = {t.id: t for t in self.waiting if t.id in ids}
                rest = [t for t in self.waiting if t.id not in ids]
                ordered, seen = [], set()
                for t in tracks:                          # the queue's order, whether waiting already or new
                    w = queued.get(t.id) or next((n for n in new if n.id == t.id), None)
                    if w is not None and t.id not in seen:
                        ordered.append(w)
                        seen.add(t.id)
                self.waiting = deque(ordered + rest)
            else:
                self.waiting.extend(new)
            self.lock.notify_all()
        if new:
            self._start()
        return len(new)

    def mismatched(self, track) -> None:
        """The jam heard that Spotify's version is not this copy: not fetched again tonight."""
        with self.lock:
            self.failed[track.id] = (self.clock() + 24 * 3600, track.label, "Spotify plays another version")

    def forget(self) -> None:
        """Drop what is waiting (a jam stopped): what is under way finishes."""
        with self.lock:
            self.waiting.clear()

    def state_of(self, track_id: str) -> tuple[str, str] | None:
        """Where a song stands: ("analyzing" | "queued" | "failed", why), or None (nothing to do)."""
        with self.lock:
            if any(b and b.id == track_id for b in self.busy.values()):
                return "analyzing", ""
            if any(t.id == track_id for t in self.waiting) or any(x[0].id == track_id for x in self.ready):
                return "queued", ""
            f = self.failed.get(track_id)
            return ("failed", f[2]) if f else None

    def status(self) -> dict:
        with self.lock:
            fails = [{"label": lbl, "why": why} for _, lbl, why in self.failed.values()][-5:]
            busy = self.busy["analyse"] or self.busy["download"]
            return {"available": self.is_available(), "working": busy.label if busy else None,
                    "waiting": len(self.waiting) + len(self.ready), "learned": self.learned_now, "failed": fails}

    def idle(self) -> bool:
        with self.lock:
            return not self.waiting and not self.ready and not any(self.busy.values())

    # -- the two workers ------------------------------------------------------------------------------
    def _start(self) -> None:
        if any(t.is_alive() for t in self.threads):
            return
        self.threads = [threading.Thread(target=self._downloads, daemon=True), threading.Thread(target=self._analyses, daemon=True)]
        for t in self.threads:
            t.start()
        if self.analyze is _analyze and ANALYSER.available():          # the analysis process and its models, loading now
            threading.Thread(target=ANALYSER.warm, daemon=True).start()

    def _fail(self, track, why: str) -> None:
        with self.lock:
            self.failed[track.id] = (self.clock(), track.label, why)
        self.log(f"[learn ahead] {track.label}: {why}")

    def _downloads(self) -> None:
        while True:
            with self.lock:
                # never more than two copies on disk, except the song playing now: it goes at once
                while not self.waiting or (len(self.ready) >= 2 and self.waiting[0].id != self.now):
                    if not self.lock.wait(timeout=600):
                        return
                track = self.waiting.popleft()
                if not self._wanted_now(track):
                    continue
                self.busy["download"] = track
            folder = tempfile.mkdtemp(prefix="lss-ahead-")
            t_dl = self.clock()
            try:
                adopt = getattr(self.library, "adopt_twin", None)
                if adopt is not None and adopt(track):       # learned before under another Spotify id: nothing to do
                    self.log(f"[learn ahead] {track.label}: learned before under another Spotify id, its analysis used")
                    shutil.rmtree(folder, ignore_errors=True)
                    continue
                kept = self.kept_copy(track)
                if kept:                                 # kept from before (jam_keep_youtube_copies): no download
                    with self.lock:
                        self.ready.append((track, folder, kept))
                        self.lock.notify_all()
                    continue
                path, why = None, ""
                spent = {"search": 0.0, "conversion": 0.0}       # where the time went (the log line below)

                def flac(src):
                    t = self.clock()
                    try:
                        return self.to_flac(src)
                    finally:
                        spent["conversion"] += self.clock() - t
                # YouTube Music's own song first (the album recording, matched by artist, name and length),
                # then spotDL's pick, then a search of YouTube itself
                music = pick_ytmusic(track, self.search_music(track))
                spent["search"] = self.clock() - t_dl
                if music:
                    try:
                        path = flac(self._fetch(lambda: self.download_from(track, folder, music)))
                        n = self.length(path)
                        if abs(n - float(track.duration)) > LENGTH_SLACK_S:
                            why, path = f"YouTube Music's copy is {n:.0f} s long, Spotify's {track.duration:.0f} s", None
                    except LookupError as e:
                        why = str(e)
                if path is None:
                    try:
                        path = flac(self._fetch(lambda: self.download(track, folder)))
                        n = self.length(path)
                        if abs(n - float(track.duration)) > LENGTH_SLACK_S:
                            why, path = f"the copy found is {n:.0f} s long, Spotify's {track.duration:.0f} s: another version", None
                    except LookupError as e:
                        why = str(e)
                if path is None:
                    # spotDL's pick is missing or another version (a music video, a lyric video with an intro):
                    # YouTube is searched for the very recording, by its length, its title and its channel
                    t = self.clock()
                    url = pick_alternative(track, self.search(track))
                    spent["search"] += self.clock() - t
                    if url is None:
                        raise ValueError(why or "no copy found")
                    path = flac(self._fetch(lambda: self.download_from(track, folder, url)))
                    n = self.length(path)
                    if abs(n - float(track.duration)) > LENGTH_SLACK_S:
                        raise ValueError(f"no copy of Spotify's length ({track.duration:.0f} s) found")
                    self.log(f"[learn ahead] {track.label}: spotDL's pick was not it; using {url}")
                took = self.clock() - t_dl
                self.log(f"[learn ahead] {track.label}: copy downloaded in {took:.0f} s (search {spent['search']:.1f}, "
                         f"download {took - spent['search'] - spent['conversion']:.1f}, conversion {spent['conversion']:.1f})")
                with self.lock:
                    self.ready.append((track, folder, path))
                    self.lock.notify_all()
            except Exception as e:  # noqa: BLE001 - the jam listens to this one instead
                shutil.rmtree(folder, ignore_errors=True)
                self._fail(track, str(e) or type(e).__name__)
            finally:
                with self.lock:
                    self.busy["download"] = None
                    self.lock.notify_all()

    def copies_dir(self) -> str:
        return os.path.join(self.library.dir, "copies")

    def kept_copy(self, track) -> str | None:
        """The copy kept for this track, when copies are kept and there is one."""
        if not self.keep():
            return None
        p = os.path.join(self.copies_dir(), f"{track.id}.flac")
        return p if os.path.exists(p) else None

    def _keep_copy(self, track, path: str) -> bool:
        """Keep a copy just learned (when copies are kept); True if it was kept."""
        if not self.keep():
            return False
        dest = os.path.join(self.copies_dir(), f"{track.id}.flac")
        if os.path.abspath(path) == os.path.abspath(dest):
            return True
        try:
            os.makedirs(self.copies_dir(), exist_ok=True)
            shutil.move(path, dest)
            return True
        except OSError as e:
            self.log(f"[learn ahead] {track.label}: the copy could not be kept ({e})")
            return False

    def _fetch(self, get):
        """A download, tried again at once (a few seconds apart) when YouTube refuses it (HTTP 403)."""
        for attempt in range(DOWNLOAD_TRIES):
            try:
                return get()
            except LookupError as e:
                if "download" not in str(e).lower() or attempt == DOWNLOAD_TRIES - 1:
                    raise
                time.sleep(self.retry_pause)

    def _wanted_now(self, track) -> bool:
        return not self._learned_well(track.id) and not self.library.match_project(track)

    def _lyrics(self, track, path: str) -> None:
        """The song's lyrics from its copy (lyrics.learn), while the copy is still here. Never in the way:
        skipped while a song playing now, unlearned, waits to be analysed, and a failure only logs."""
        if self.lyrics is None:
            return
        with self.lock:
            if self.now and any(t.id == self.now for t, _, _ in self.ready):
                self.log(f"[learn ahead] {track.label}: no lyrics this time (the song playing now is waiting to be learned)")
                return
        try:
            t0 = self.clock()
            data = self.lyrics(track, path, self.library.dir)
            if data:
                self.log(f"[learn ahead] {track.label}: lyrics, {len(data.get('words') or [])} words ({data.get('source')}) in {self.clock() - t0:.0f} s")
        except Exception as e:  # noqa: BLE001 - the song is learned either way
            self.log(f"[learn ahead] {track.label}: no lyrics ({type(e).__name__}: {e})")

    def _analyses(self) -> None:
        from . import priority
        priority.low_thread()                          # the room's own playback and the live analysis come first
        while True:
            with self.lock:
                while not self.ready:
                    if not self.lock.wait(timeout=600):
                        return
                # the song that plays soonest of those downloaded (the queue may have changed since)
                rank = lambda x: self.order.index(x[0].id) if x[0].id in self.order else len(self.order)
                best = min(self.ready, key=rank)
                self.ready.remove(best)
                track, folder, path = best
                self.busy["analyse"] = track
                self.lock.notify_all()
            try:
                t0 = self.clock()
                urgent = track.id == self.now
                a = self.analyze(path, os.path.join(folder, "cache"), **({"urgent": True} if urgent else {}))
                self.library.save_learned(track, a, source="youtube")
                self._measure_copy(track, path)
                self._lyrics(track, path)
                self.checked[track.id] = True                  # learned again: not again this run, whatever it says
                with self.lock:
                    self.learned_now += 1
                kept = self._keep_copy(track, path)
                self.log(f"[learn ahead] {track.label}: learned in {self.clock() - t0:.0f} s, the copy {'kept' if kept else 'deleted'}")
            except Exception as e:  # noqa: BLE001
                self._fail(track, f"{type(e).__name__}: {e}")
            finally:
                shutil.rmtree(folder, ignore_errors=True)      # the copy goes at once (unless kept): only the numbers stay
                with self.lock:
                    self.busy["analyse"] = None
                    self.lock.notify_all()
