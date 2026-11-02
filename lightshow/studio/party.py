"""The party: somebody else picks the music, the lights still know what is coming.

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
Spotify and a file standing in for the sound card. `PartyRunner` adds the threads
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


def _load_json(path: str, default=None):
    """A JSON file's contents, or default when it is missing or broken."""
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return default


def _save_json(path: str, doc, indent: int | None = None) -> None:
    """Written to a temporary file and moved into place: never half a file."""
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(doc, fh, indent=indent)
    for wait in (0.05, 0.1, 0.2, 0.4, None):
        try:
            os.replace(tmp, path)
            return
        except PermissionError:                     # Windows: something (a virus scan, the indexer) has it open for a moment
            if wait is None:
                raise
            time.sleep(wait)


# ======================================================================= the library
def _norm(text: str) -> list[str]:
    """Words of a title with the noise taken out, for matching a track to a saved show."""
    text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode().lower()
    text = re.sub(r"['’`]", "", text)
    text = re.sub(r"\b(official|video|audio|lyrics?|remaster(ed)?|hd|hq|feat|ft|version|explicit|visualizer)\b", " ", text)
    return [w for w in re.split(r"[^a-z0-9]+", text) if w and not (w.isdigit() and len(w) == 4)]


# Words a release adds to a title without changing the recording ("Found - Live" is the live album's Found:
# the same audio). "remaster", "version", "explicit" and years are gone already (_norm). A remix, an acoustic
# or an extended version is another recording: not here.
SAME_RECORDING_WORDS = {"live", "edit", "radio", "mono", "stereo", "deluxe", "single", "album", "original", "mix", "mixed",
                        "bonus", "track", "clean", "dirty"}


def _same_title(a: str, b: str) -> int:
    """2: the same title; 1: one is the other plus words that leave the recording as it is; 0: neither."""
    if a == b:
        return 2
    wa, wb = a.split(), b.split()
    short, long_ = (wa, wb) if len(wa) <= len(wb) else (wb, wa)
    return 1 if short and long_[:len(short)] == short and set(long_[len(short):]) <= SAME_RECORDING_WORDS else 0


def _analysis_version() -> int:
    from .analysis import ANALYSIS_VERSION
    return ANALYSIS_VERSION



from .analysis import shift_beats  # noqa: E402,F401 - (moved there; the beat fixes use it)

class PartyLibrary:
    """Shows saved on disk, and what has been learned about tracks heard before."""

    def __init__(self, shows_dir: str):
        self.shows_dir = shows_dir
        self.dir = os.path.join(shows_dir, "_party")
        self.learned_dir = os.path.join(self.dir, "learned")
        self.links_path = os.path.join(self.dir, "links.json")
        self._links: dict | None = None
        self.colors_path = os.path.join(self.dir, "colors.json")
        self._colors: dict | None = None
        self.styles_path = os.path.join(self.dir, "styles.json")
        self._styles: dict | None = None
        self.genres_path = os.path.join(self.dir, "genres.json")
        self._genres: dict | None = None
        self.levels_path = os.path.join(self.dir, "levels.json")
        self._levels: dict | None = None
        self.beat_fixes_path = os.path.join(self.dir, "beat_fixes.json")
        self._levels_lock = threading.Lock()
        # the caches below (twins, the learned index, the user's sections) are used by the party, learn ahead, the
        # review and the labeler's threads at once: one lock (a dict changed mid-dump failed a song's start)
        self._cache_lock = threading.RLock()
        self._truth: tuple | None = None                            # (section_truth.json's time, its contents)
        self._twins: dict | None = None                             # twins.json: confirmed pairs
        self._lidx: dict = {}                                       # the learned index (_learned_index)
        self._summary: dict = {}                                    # track id -> (the file's time, its summary)
        self._locked_cache: dict = {}                               # locked file -> (its time, its contents)
        self._lyrics: dict = {}                                     # track id -> (the file's time, its lyrics)
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
            self._links = _load_json(self.links_path, {})
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

    # -- colors picked for a track in the party queue ------------------------------------------
    def colors(self) -> dict:
        """track id -> {"primary": hex, "secondary": hex, "only"?: True}, or {"off": True} for a track that keeps out of
        the party's colors (it plays in its album cover's): kept for the next time the track plays."""
        if self._colors is None:
            self._colors = _load_json(self.colors_path, {})
        return self._colors

    def set_colors(self, track_id: str, primary: str | None, secondary: str | None, off: bool = False, only: bool = False) -> None:
        """Give a track its own two colors (only: those two and nothing else, no accents in other colors
        and no rainbow drops), take it out of the party's colors (off: its cover's instead), or (with
        None) send it back to the party's colors."""
        colors = self.colors()
        if off:
            colors[track_id] = {"off": True}
        elif primary and secondary:
            colors[track_id] = {"primary": primary, "secondary": secondary, **({"only": True} if only else {})}
        else:
            colors.pop(track_id, None)
        os.makedirs(self.dir, exist_ok=True)
        _save_json(self.colors_path, colors, indent=1)

    # -- a song's vibe, chosen by hand ---------------------------------------------------------
    STYLES = ("chill", "groove", "dance", "rave")

    def styles(self) -> dict:
        """track id -> the show style picked for it by hand (a song taken for a rager that is chill),
        kept for the next time it plays; a song not here gets the style its analysis finds."""
        if self._styles is None:
            self._styles = {k: v for k, v in _load_json(self.styles_path, {}).items() if v in self.STYLES}
        return self._styles

    def set_style(self, track_id: str, style: str | None) -> None:
        styles = self.styles()
        if style in self.STYLES:
            styles[track_id] = style
        else:
            styles.pop(track_id, None)                   # back to the style the analysis finds
        os.makedirs(self.dir, exist_ok=True)
        _save_json(self.styles_path, styles, indent=1)

    # A song's genre, chosen by hand: what the effects that belong to a genre listen for (808s and hat rolls
    # for hip hop and trap, wobbles and glitches for bass music, candlelight for folk), and, with the vibe
    # left to the analysis, the vibe too. key -> (label, the genre words the show is given, its level:
    # under 0.7 chill, under 1.6 groove, under 2.4 dance, rave above; as genre.py ranks genres).
    GENRES = {
        "pop": ("Pop", "Pop music", 1.3), "hiphop": ("Hip hop", "Hip hop music", 1.3), "trap": ("Trap", "Trap rap", 2.0),
        "edm": ("EDM", "Electronic dance music", 2.5), "house": ("House", "House music", 2.2), "dubstep": ("Dubstep", "Dubstep bass music", 3.0),
        "dnb": ("Drum & bass", "Drum and bass", 3.0), "rock": ("Rock", "Rock music", 1.1), "indie": ("Indie", "Independent music", 1.0),
        "metal": ("Metal", "Heavy metal", 1.9), "rnb": ("R&B", "Rhythm and blues", 0.9), "country": ("Country", "Country", 0.8),
        "folk": ("Folk", "Folk music acoustic", 0.4), "jazz": ("Jazz", "Jazz", 0.4), "ambient": ("Ambient", "Ambient music", 0.1),
    }

    def genres(self) -> dict:
        if self._genres is None:
            self._genres = {k: v for k, v in _load_json(self.genres_path, {}).items() if v in self.GENRES}
        return self._genres

    def set_genre(self, track_id: str, genre: str | None) -> None:
        genres = self.genres()
        if genre in self.GENRES:
            genres[track_id] = genre
        else:
            genres.pop(track_id, None)                   # back to the genre the analysis hears
        os.makedirs(self.dir, exist_ok=True)
        _save_json(self.genres_path, genres, indent=1)

    def match_project(self, track: Track) -> str | None:
        linked = self.links().get(track.id)
        if linked == "":
            return None                                # '' means "deliberately no custom show"
        if linked is not None and os.path.isfile(os.path.join(self.shows_dir, linked, "project.json")):
            return linked                              # (a show since deleted: as if never linked, the auto show)
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
        if doc.get("version") != _analysis_version():
            return None
        fixes = self.beat_fixes()
        shift = fixes.get(track_id)
        if shift is None:                                       # a twin's fix, when the copies are the same audio
            shift = next((fixes[t] for t, off in self.twins(track_id) if t in fixes and abs(off) < 0.05), None)
        if shift:
            shift_beats(doc["analysis"], float(shift))
        doc["analysis"]["track_id"] = track_id                  # (the learned labeler finds the song's features by it)
        mine = self.reviewed_sections(track_id)
        if mine:
            doc["analysis"]["sections_truth"] = mine            # settle_sections plays these (the user's own)
        return doc

    def summary(self, track_id: str) -> dict | None:
        """{"problems", "mood", "genre"} of a learned song, without parsing all of its megabytes (~95 ms a song: learn
        ahead and the queue's colors read one field of 100+ songs): each value decoded where it stands in the file,
        the whole file read only if one does not look right. Kept by the file's time. None: not learned."""
        path = self._learned_path(track_id)
        try:
            mtime = os.path.getmtime(path)
        except OSError:
            return None
        with self._cache_lock:
            hit = self._summary.get(track_id)
            if hit and hit[0] == mtime:
                return hit[1]
        out = None
        try:
            with open(path, encoding="utf-8") as fh:
                text = fh.read()
            dec, got = json.JSONDecoder(), {}
            for key, find in (("problems", text.rfind), ("genre", text.rfind), ("mood", text.find)):
                i = find(f'"{key}": ')
                if i < 0:
                    raise ValueError(key)
                got[key], _ = dec.raw_decode(text, i + len(key) + 4)
            if isinstance(got["problems"], list) and isinstance(got["mood"], dict) and isinstance(got["genre"], (dict, type(None))):
                out = got
        except (OSError, ValueError):
            out = None
        if out is None:                                    # (not where expected: the whole file, as before)
            doc = self.learned(track_id)
            if not doc:
                return None
            a = doc.get("analysis") or {}
            out = {"problems": list(a.get("problems") or []), "mood": a.get("mood"), "genre": a.get("genre")}
        with self._cache_lock:
            self._summary[track_id] = (mtime, out)
        return out

    def reviewed_sections(self, track_id: str) -> list | None:
        """The sections the user set by ear for a learned song (the studio's review, section_truth.json), or None.
        A song can have no drops at all: the user's word stands (Walking On A Dream, Oogie Boogie's Song)."""
        path = os.path.join(self.dir, "section_truth.json")
        try:
            mtime = os.path.getmtime(path)
        except OSError:
            return None
        cache = self._truth
        if not cache or cache[0] != mtime:
            try:
                with open(path, encoding="utf-8") as fh:
                    cache = (mtime, json.load(fh))
            except (OSError, ValueError):
                return None
            self._truth = cache
        secs = (cache[1].get(track_id) or {}).get("sections")
        if secs:
            return [dict(s) for s in secs]
        for twin, off in self.twins(track_id):                  # reviewed under another id: the same song, moved onto this copy
            secs = (cache[1].get(twin) or {}).get("sections")
            if secs:
                return [{**s, "start": round(max(0.0, float(s["start"]) + off), 3), "end": round(float(s["end"]) + off, 3)} for s in secs]
        return None

    # The same recording under several Spotify ids (a single, then the album it is on, then a compilation): what
    # the user did for one (their sections, a beat fix, the lyrics) is used for all. Twins share an artist, the
    # name (or the name plus words like "live" or "radio edit": then the audio must match 0.95) and the length
    # (within 1.5 s), and their loudness curves match (0.9 or more) at some offset (two
    # YouTube copies may start apart: Found - Live's by 1.6 s). 13 such pairs among 246 learned songs; Pay No
    # Mind's live version (45 s longer) is not one. Confirmed pairs are kept in twins.json.
    def twins(self, track_id: str) -> list:
        """[(twin id, seconds to add to the twin's times for this copy)], confirmed by the audio."""
        idx = self._learned_index()
        me = idx.get(track_id)
        if not me:
            return []
        cands = {t: _same_title(v[1], me[1]) for t, v in idx.items() if v and t != track_id and v[0] & me[0] and abs(v[2] - me[2]) <= 1.5}
        cands = {t: k for t, k in cands.items() if k}
        if not cands:
            return []
        path = os.path.join(self.dir, "twins.json")
        with self._cache_lock:
            if self._twins is None:
                self._twins = _load_json(path, {})
            known = self._twins
            todo = [t for t in cands if "|".join(sorted((track_id, t))) not in known]
        found = {"|".join(sorted((track_id, t))): self._match(*sorted((track_id, t))) for t in todo}   # (slow: outside the lock)
        with self._cache_lock:
            if found:
                known.update(found)
                try:
                    _save_json(path, known, indent=1)
                except OSError:
                    pass
            out = []
            for t in cands:
                m = known.get("|".join(sorted((track_id, t))))
                if m and m[0] >= (0.9 if cands[t] == 2 else 0.95):      # (a title that differs needs the audio closer still)
                    off = float(m[1]) if sorted((track_id, t))[0] == track_id else -float(m[1])
                    out.append((t, round(off, 3)))
        return out

    def _match(self, a_id: str, b_id: str) -> list | None:
        """[how well a's and b's loudness curves match (0..1), seconds to add to b's times for a's], within 20 s."""
        def energy(tid):
            try:
                with open(self._learned_path(tid), encoding="utf-8") as fh:
                    an = json.load(fh)["analysis"]
                return np.asarray(an.get("energy") or [], dtype=float), float(an.get("feature_fps") or 30.0)
            except (OSError, ValueError, KeyError):
                return None, 30.0
        (ea, fps), (eb, _) = energy(a_id), energy(b_id)
        if ea is None or eb is None or len(ea) < 300 or len(eb) < 300:
            return None
        n, best = min(len(ea), len(eb)), (-1.0, 0)
        for lag in range(-int(20 * fps), int(20 * fps) + 1, 2):
            x, y = ea[max(0, lag):n + min(0, lag)], eb[max(0, -lag):n - max(0, lag)]
            m = min(len(x), len(y))
            if m < 30 * fps or np.std(x[:m]) == 0 or np.std(y[:m]) == 0:
                continue
            c = float(np.corrcoef(x[:m], y[:m])[0, 1])
            if c > best[0]:
                best = (c, lag)
        return [round(best[0], 3), round(best[1] / fps, 3)]

    # The beat grid set right by ear (the studio's review: Light Switch's beats sat on its claps, half a beat
    # off its kicks): track id -> seconds the whole grid moves, applied every time the song is read.
    def beat_fixes(self) -> dict:
        return {k: v for k, v in _load_json(self.beat_fixes_path, {}).items() if not k.startswith("_")}

    def set_beat_fix(self, track_id: str, seconds: float) -> None:
        fixes = _load_json(self.beat_fixes_path, {"_about": "Seconds each learned song's beat grid moves (set by ear in the studio's review)."})
        if abs(seconds) < 0.001:
            fixes.pop(track_id, None)
        else:
            fixes[track_id] = round(float(seconds), 4)
        os.makedirs(self.dir, exist_ok=True)
        _save_json(self.beat_fixes_path, fixes, indent=1)

    # -- locked shows: a song whose show the user wants kept exactly as it is ("it's epic") ---------------
    # shows/_party/locked/<id>.json holds the show as it was built and the analysis it was built on, so no
    # change to the show logic, a retrained section labeler, a song learned again or the party's color settings
    # can change it; the party plays it as it is. Removing the file unlocks the song.
    def _locked_path(self, track_id: str) -> str:
        return os.path.join(self.dir, "locked", re.sub(r"[^A-Za-z0-9]", "", track_id) + ".json")

    def locked(self, track_id: str) -> dict | None:
        """{"project", "analysis", ...} for a locked song, else None."""
        if not track_id:
            return None
        path = self._locked_path(track_id)
        try:
            mtime = os.path.getmtime(path)
        except OSError:
            return None                                   # (nearly every song: one quick look)
        cache = self._locked_cache
        if path in cache and cache[path][0] == mtime:
            return cache[path][1]
        try:
            with open(path, encoding="utf-8") as fh:
                doc = json.load(fh)
        except (OSError, ValueError):
            return None
        doc = doc if isinstance(doc, dict) and isinstance(doc.get("project"), dict) and isinstance(doc.get("analysis"), dict) else None
        cache[path] = (mtime, doc)
        return doc

    def lock(self, track_id: str, project: dict, analysis: dict, label: str = "") -> None:
        path = self._locked_path(track_id)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        _save_json(path, {"label": label, "locked_at": time.strftime("%Y-%m-%d %H:%M"), "project": project, "analysis": analysis})

    def lyrics(self, track_id: str) -> dict | None:
        """The song's lyrics with word times (lyrics.py; shows/_party/lyrics/<id>.json), slimmed to what a
        show needs: words {t, end, w, line, bg?, p} and lines {t, end, text}. None when there are none (not
        learned yet, or an instrumental). Read again only when the file changes: learn ahead may write it
        after the song has started."""
        path = os.path.join(self.dir, "lyrics", re.sub(r"[^A-Za-z0-9]", "", track_id) + ".json")
        try:
            mtime = os.path.getmtime(path)
        except OSError:
            for twin, off in self.twins(track_id):              # a twin's words, moved onto this copy
                lyr = self.lyrics(twin) if os.path.exists(os.path.join(self.dir, "lyrics", re.sub(r"[^A-Za-z0-9]", "", twin) + ".json")) else None
                if lyr:
                    mv = lambda x: {**x, **{k: round(float(x[k]) + off, 3) for k in ("t", "end") if k in x}}
                    return {**lyr, "words": [mv(w) for w in lyr["words"]], "lines": [mv(x) for x in lyr["lines"]]}
            return None
        cache = self._lyrics
        if track_id in cache and cache[track_id][0] == mtime:
            return cache[track_id][1]
        try:
            with open(path, encoding="utf-8") as fh:
                doc = json.load(fh)
        except (OSError, ValueError):
            return None
        keep = ("t", "end", "w", "line", "bg", "p")
        words = [{k: w[k] for k in keep if k in w} for w in doc.get("words") or [] if w.get("w")]
        out = {"source": doc.get("source"), "words": words,
               "lines": [{k: x.get(k) for k in ("t", "end", "text")} for x in doc.get("lines") or []]} if words else None
        cache[track_id] = (mtime, out)
        return out

    # Spotify often has one recording under two ids (the single, and the album it is on): learned under one,
    # it played under the other as a song never heard (Knox Hamilton's Set It on Fire, learned at 16:51 and
    # learned again from scratch at 17:50, glowing meanwhile). A twin is found by its artist, name and length.
    def _learned_index(self) -> dict:
        """track id -> (artist words, name, length) of each learned song, read from the head of its file (the
        track comes right after the version: the megabytes after it are not read). Kept, and topped up."""
        with self._cache_lock:
            idx = self._lidx
            try:
                names = os.listdir(self.learned_dir)
            except OSError:
                return dict(idx)
            dec = json.JSONDecoder()
            for fn in names:
                tid = fn[:-5]
                if not fn.endswith(".json") or tid in idx:
                    continue
                try:
                    with open(os.path.join(self.learned_dir, fn), encoding="utf-8") as fh:
                        head = fh.read(8192)
                    t, _ = dec.raw_decode(head, head.index('"track": ') + len('"track": '))
                    idx[tid] = (frozenset(w for a in t.get("artists") or [] for w in _norm(a)), " ".join(_norm(t.get("name") or "")),
                                float(t.get("duration") or 0))
                except (OSError, ValueError):
                    idx[tid] = None
            return dict(idx)                                 # (a copy: callers iterate it while others top it up)

    def twin_of(self, track: Track) -> str | None:
        """The id a song not learned under its own was learned under: one of its artists, the same name and
        the same length (within 1.5 s), learned by the analysis as it is now."""
        if not track.id or self.has_learned(track.id):
            return None
        name, artists = " ".join(_norm(track.name)), frozenset(w for a in track.artists for w in _norm(a))
        if not name or not artists:
            return None
        for tid, v in self._learned_index().items():
            if v and tid != track.id and v[1] == name and v[0] & artists and abs(v[2] - float(track.duration)) <= 1.5                     and self.has_learned(tid):
                return tid
        return None

    def adopt_twin(self, track: Track) -> str | None:
        """Learned as its twin: the twin's analysis saved under this id too, at once (no download, no
        listening). Returns the twin's id, or None."""
        tid = self.twin_of(track)
        doc = self.learned(tid) if tid else None
        if not doc:
            return None
        _save_json(self._learned_path(track.id), {**doc, "track": track.to_json(), "twin_of": tid})       # (the version stays first)
        return tid

    def forget_learned(self, track_id: str) -> None:
        try:
            os.remove(self._learned_path(track_id))
        except OSError:
            pass

    # -- how loud each song is (loudness.py) --------------------------------------------
    def level(self, track_id: str) -> dict:
        """{"copy": measured from its YouTube copy, "relay": measured as it played} (either may be missing)."""
        with self._levels_lock:
            if self._levels is None:
                self._levels = _load_json(self.levels_path, {})
            return dict(self._levels.get(track_id) or {})

    def save_level(self, track_id: str, source: str, level: dict) -> None:
        """source: "copy" (the YouTube copy, when learned) or "relay" (the song as it played, whole)."""
        self.level(track_id)                                        # (loaded)
        with self._levels_lock:
            self._levels.setdefault(track_id, {})[source] = {**level, "at": time.strftime("%Y-%m-%d %H:%M")}
            os.makedirs(self.dir, exist_ok=True)
            _save_json(self.levels_path, self._levels)

    def save_learned(self, track: Track, analysis: dict, source: str = "heard") -> None:
        """source: "heard" (the party listened to it) or "youtube" (learned ahead from a copy, see fetch_ahead)."""
        os.makedirs(self.learned_dir, exist_ok=True)
        prev = self.learned(track.id) or {}
        doc = {"version": _analysis_version(), "track": track.to_json(), "plays": int(prev.get("plays", 0)) + (source == "heard"),
               "learned_at": time.strftime("%Y-%m-%d %H:%M"), "source": source, "analysis": analysis}
        _save_json(self._learned_path(track.id), doc)


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
        # how many peaks reach exactly to t1, counted from the start: rounding each piece on its own
        # (30 feature frames a second into 50 peaks a second) lost up to half a peak a time, and over a
        # song the waveform came out about 8% short, drifting ever earlier than the music
        n_pk = max(0, int(round((t1 - self.base) * WAVE_PPS)) - len(self.peaks))
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
        # time before anything was recorded (a song already playing when the party began) is silence,
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
EARLY_S = 7.0                # a learned song is first lined up this far in (a few seconds before the room hears it)...
EARLY_PEAK, EARLY_EDGE = 0.8, 0.05   # ...if it matches this clearly; otherwise it waits for the usual 14 s
VERIFY_EVERY_S = 20.0        # a song learned from a copy is checked again this often while it plays
WIDE_REACH = 10.0            # a copy that does not line up near Spotify's start is searched this far either way (a
                             # video's intro, a few seconds more silence) before it is given up on, from 30 s in
MISMATCH_PEAK = ALIGN_PEAK   # a song learned from a copy that matches this badly is not the recording Spotify plays
                             # (the same recording scores above 0.9; another, even in the same style, about 0.35)


def align_start(live: np.ndarray, ref: np.ndarray, window_start: float, fps: float, reach: float = 2.0,
                min_ref: float = 8.0) -> tuple[float, float, float]:
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
    if n_t < int(min_ref * fps):
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
        self.cast.play_stream(self.stream_url(self.sid), "audio/wav", "Light Show Party")

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
class PartyTrack:
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
    checked_at: float = 0.0             # when it was last looked for among the songs learned ahead
    source: str = "heard"               # how its saved analysis was learned: "heard", or "youtube" (from a copy)
    tried_at: float = -1e9              # capture time it was last tried for lining up by ear
    verified_at: float = 0.0            # capture time its copy was last checked against what plays
    narrow_failed: bool = False         # its copy did not line up near Spotify's start: searched further
    verify_misses: int = 0              # checks in a row that did not match its copy (two make a mismatch)
    genre_problem: str = ""             # why its genre could not be heard (kept with what is learned of it)
    heard_from: float | None = None     # after a seek: the capture time this stretch of the song begins at (its
                                        # start is still where 0:00 would have been; nothing before is this)
    genre: dict | None = None           # show style and the genres behind it
    spotify_genres: list = field(default_factory=list)

    @property
    def key(self) -> str:
        return f"{self.track.id}-{self.serial}"

    def to_json(self) -> dict:
        return {**self.track.to_json(), "key": self.key, "kind": self.kind, "project": self.project, "aligned": self.aligned,
                "version": self.version, "horizon": round(self.horizon, 2), "style": (self.genre or {}).get("style"),
                "genres": ((self.genre or {}).get("genres") or (self.genre or {}).get("spotify") or [])[:2]}


@dataclass
class PartySettings:
    target_delay: float = 90.0          # how far behind the capture head the room should be (90 s: time for a full analysis)
    max_gap: float = 6.0                # most silence to add between two songs when growing the delay back
    analyse_every: float = 6.0          # new audio needed before the live analysis runs again
    min_audio: float = 12.0             # audio needed before the first pass
    capture_latency: float = 0.06       # Spotify's position versus when the sound card plays it
    events_every: int = 3               # instrument detection is the slow part: every Nth pass
    crossfade: float = 0.0              # Spotify's crossfade setting, in seconds (it cannot be read from the API)
    outro: float = 30.0                 # a change this close to a song's end counts as the song ending
    skip_jumps: bool = False            # a skip jumps the room to the new song now, giving up the delay
    # Learning ahead, a first listen is only a stopgap until its copy is learned: its beat is followed and
    # nothing else (no stems, sections model, instruments or genre), so the processor's time goes to the
    # copy's full analysis, and what is heard is not kept (the copy is what the song is learned from).
    light_live: bool = False
    # Every song at about the same volume (loudness.py): one gain per song, set where it begins.
    even_levels: bool = True
    level_target: float = -9.0          # LUFS


def _ref_of(a: dict) -> tuple | None:
    """What a song's saved analysis is lined up against by ear: (matching features, fps), or None."""
    if not a.get("align_bands"):
        return None
    return np.asarray(a["align_bands"], dtype=np.float32), float(a.get("align_fps", SR_ANALYSIS / HOP))


def _settle_learned(a: dict) -> None:
    """A learned analysis made ready to play: its style as it reads now, its sections settled."""
    from .analysis import settle_sections
    from .genre import restyle
    a["genre"] = restyle(a.get("genre"), float((a.get("mood") or {}).get("drive", 0.6)))
    settle_sections(a)


class PartyEngine:
    def __init__(self, relay: Relay, spotify: SpotifyClient, library: PartyLibrary, output, gate: Gate | None = None,
                 settings: PartySettings | None = None, clock=time.monotonic, analyse_fn=None, cache_dir: str | None = None,
                 genre_fn=None, segment_fn=None):
        self.relay, self.spotify, self.library, self.output = relay, spotify, library, output
        self.gate = gate or Gate()
        self.cfg = settings or PartySettings()
        self.clock = clock
        self.cache_dir = cache_dir
        self.features = FeatureTrack(relay)
        # each mended pause seam is said in the log, and the audio either side of the last few kept to listen to
        self.seam_dir = os.path.join(library.dir, "seams")
        relay.on_seam = self._seam
        self.tracks: list[PartyTrack] = []
        self.queue: list[dict] = []       # what is queued, each with its kind, worked out when fetched
        self.now: NowPlaying | None = None
        self.lock = threading.RLock()
        self.log: list[str] = []
        self.streaming = False
        self.serial = 0
        self.flushes = 0
        self._streamed_at = -1e9                       # when the speakers last got a fresh stream (keep_delay)
        self._planned_level = None                     # the song whose next song's level is set ahead (plan_levels)
        self._paused_room: float | None = None         # where the room was when Spotify paused
        self._paused_at: float | None = None           # and when
        self._speaker_dropped = False                  # the speaker ended its Cast session during that pause
        self._resume_watch = None                      # (when, room) at a resume: has the room moved on since?
        self._resumed_at = -1e9                        # when the speakers last just resumed (keep_delay waits)
        self._duck_fresh = False                       # the music turned down or up while paused: a fresh stream on resume
        self._catch_up_said = False                    # keep_delay has said the room catches up at the end of this song
        self._prep: dict = {}                          # the song about to begin, loaded ahead (_prepare)
        self.error = ""
        self.auto_start = True            # begin streaming by itself once the buffer reaches the target
        self._last_seen = None            # (track id, progress, when, playing) from the previous poll
        self.handover: str | None = None  # None | 'armed' | 'filling': opening with a song from disk
        self.opening = False              # the opening song started with Spotify: the delay is whatever it buffered
        # A phone remote moment holds the room: where it stopped (a capture time), kept while the speakers play
        # something else, so the room picks up exactly there afterwards.
        self.held_at: float | None = None
        self.show_moment_hold = False             # while a moment runs, nothing but the moment lets the room go
        self.show_moment_in_stream = False        # the moment's song is played inside the stream: the speakers are never paused
        # After a moment the music is back in the speakers' stream at once, while Spotify (resumed) is still paused
        # until the next poll sees it: the room plays meanwhile (it said paused, and the lights fading back in
        # were held at 5 frames a second with their clock stopped)
        self.music_back = False
        self.on_queue = None              # (tracks, now) -> None: the song playing and what is queued, as they change (learn ahead);
                                          # now: the id of the song playing unlearned (all effort goes to it), or None
        self.on_context = None            # (tracks) -> None: the rest of the playlist or album playing, behind the queue
        self._context_uri: str | None = None
        self.on_mismatch = None           # (track) -> None: a song learned from a copy turned out not to be what Spotify plays
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
        if self.held_at is not None:
            return self.held_at                          # a moment has the speakers: the room waits where it stopped
        if not self.streaming or not self.output.stream:
            return None
        return self.output.stream.capture_time_at(self.output.position() - self.output.offset)

    def _room_playing(self) -> bool:
        return not self.gate.paused or (self.music_back and self.held_at is None and self.streaming and self.output.stream is not None)

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

    def track_at(self, capture_time: float) -> PartyTrack | None:
        for i in range(len(self.tracks) - 1, -1, -1):
            t = self.tracks[i]
            if capture_time >= max(t.start, t.heard_from if t.heard_from is not None else t.start) - 0.05:
                end = t.end if t.end is not None else t.start + t.track.duration + 1.0
                return t if capture_time < end + 0.05 else None
        return None

    @property
    def current(self) -> PartyTrack | None:
        """The track at the capture head: what Spotify is playing, not what the room hears."""
        return self.tracks[-1] if self.tracks else None

    # ------------------------------------------------------------------------- polling
    def poll(self) -> None:
        """Ask Spotify what is playing and update the timeline. Call about once a second.

        Spotify is asked, and a new song's learned analysis loaded and settled (0.5 s or more), before the lock is
        taken: held through them, the lock kept the page's state, the new song's analysis and the stream waiting."""
        try:
            np_ = self.spotify.now_playing()
            err = ""
        except Exception as e:  # noqa: BLE001 - shown in the UI; the relay keeps running regardless
            np_, err = None, f"{type(e).__name__}: {e}"
        head0 = self.relay.capture_time                    # (the recording's place as Spotify answered, as before)
        cur0 = self.current
        if np_ is not None and np_.track is not None and np_.playing and (cur0 is None or cur0.track.id != np_.track.id):
            try:
                self._prepare(np_.track)
            except Exception as e:  # noqa: BLE001 - loaded the usual way, under the lock
                self.say(f"{np_.track.label}: not prepared ahead ({type(e).__name__}: {e})")
        with self.lock:
            if np_ is None:
                self.error = err
                return
            self.error = ""
            self.now = np_
            head = head0
            if np_.track is None:
                return
            if not np_.playing:
                self._last_seen = (np_.track.id, np_.progress, np_.fetched_at, False)
                if not self.gate.paused:
                    self.gate.paused = True
                    keep = self.output.stream.cursor if (self.streaming and self.output.stream) else None
                    cut = self.relay.trim_tail_silence(keep_after=keep)
                    self.relay.hold()                    # (the seam as it plays again is mended: Relay.hold)
                    self.say(f"paused on the phone: holding the buffer (trimmed {cut:.1f}s of silence)")
                    if self.streaming:
                        self._paused_room, self._speaker_dropped = self.room_capture_time(), False
                        self._paused_at, self._resume_watch = self.clock(), None
                        self.output.pause()
                return
            if self.gate.paused and not self.show_moment_hold:
                self.gate.paused, self.music_back = False, False
                self.say("resumed")
                if self.streaming and not self.show_moment_in_stream:
                    self._resume_output()
                self.show_moment_in_stream = False
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
            if same and jumped and cur.kind in ("learned", "custom"):
                return self._seek(cur, est_start, head)
            if same and not jumped:
                # Every reading is late by however long Spotify took to answer, never early, so the
                # smallest is the truest. Frozen once analysis has begun: its times hang off this.
                if not cur.aligned and cur.passes == 0 and 0 < cur.start - est_start < 0.4:
                    cur.start = est_start
                return
            self._begin_track(np_.track, est_start, cur, restarted=same)

    def _seek(self, cur: PartyTrack, est_start: float, head: float) -> None:
        """A seek within a song whose show is whole (learned, or yours): the show carries on from the new
        place instead of the song starting over as a first listen. A new stretch of the same song begins
        where the seek is heard (the room gets there when it gets there), and it is lined up by ear on
        what is heard after the seek only."""
        at = max(est_start, head - 0.3)                    # where the recording jumps (Spotify is asked once a second)
        cur.end, cur.skipped = at, True
        self.serial += 1
        jt = PartyTrack(cur.track, est_start, cur.kind, cur.project, serial=self.serial)
        jt.heard_from, jt.source, jt.spotify_genres = at, cur.source, cur.spotify_genres
        jt.analysis, jt.genre, jt.version, jt.horizon, jt.ref, jt.saved = cur.analysis, cur.genre, cur.version + 1, cur.horizon, cur.ref, True
        self.tracks.append(jt)
        del self.tracks[:-40]
        self.say(f"seek in {cur.track.name}: its show carries on from {head - est_start:.0f}s")
        if self.cfg.skip_jumps:
            self._flush(jt)

    def _prepare(self, track: Track) -> None:
        """A song about to begin: its learned analysis loaded and settled now, outside the lock (_begin_track takes it).
        Only the latest is kept."""
        prep = self._prep
        if track.id in prep or self.library.match_project(track):
            return
        learned = self.library.learned(track.id)
        if not learned:
            return
        a0 = learned["analysis"]
        _settle_learned(a0)
        prep.clear()
        prep[track.id] = (learned, _ref_of(a0))

    def _begin_track(self, track: Track, est_start: float, prev: PartyTrack | None, restarted: bool) -> None:
        natural = False
        if prev is not None and prev.end is None:
            expected_end = prev.start + prev.track.duration
            # A song that simply ended: the next one begins where this one was due to stop, a little
            # later (a gap), or earlier by the crossfade. A change in a song's last stretch is taken
            # as its end too: the room hears the old song up to where Spotify left it, then the new
            # one, and the song is kept as heard. (This was added when songs seemed to end 20 s
            # early; that turned out to be songs already playing when the party began, measured from
            # the wrong start, since fixed. The rule stays: a skip that late loses little.)
            early = max(self.cfg.crossfade + 1.5, min(self.cfg.outro, 0.5 * prev.track.duration))
            natural = not restarted and -early <= est_start - expected_end <= 3.0
            prev.end = min(est_start, expected_end) if natural else est_start
            prev.skipped = not natural
        ready = self._prep.pop(track.id, None)   # (loaded and settled ahead: poll, _prepare)
        project = None if ready else self.library.match_project(track)
        if not project and not ready:
            try:
                twin = self.library.adopt_twin(track)
                if twin:
                    self.say(f"{track.label}: the same song as one learned before (another Spotify id): its show")
            except Exception:  # noqa: BLE001 - it is learned the usual way instead
                pass
        learned = ready[0] if ready else None if project else self.library.learned(track.id)
        kind = "custom" if project else "learned" if learned else "live"
        self.serial += 1
        # A song already playing when the party began started before the recording did: its start
        # is earlier than anything held. Clamping it to the recording's first moment measured the
        # whole song from the wrong place, and a first listen was saved shifted by however far in
        # the song already was (Ego: 15.7 s, lights that far ahead every time it came back).
        jt = PartyTrack(track, est_start, kind, project, serial=self.serial)
        try:
            jt.spotify_genres = list(getattr(self.spotify, "artist_genres", lambda t: [])(track))
        except Exception:  # noqa: BLE001 - genres are a hint; the party goes on without them
            jt.spotify_genres = []
        if learned:
            jt.source = learned.get("source", "heard")
            jt.analysis = learned["analysis"]
            a0 = jt.analysis
            if not ready:                                   # (else restyled and settled ahead, outside the lock)
                _settle_learned(a0)
            jt.genre = a0.get("genre")
            jt.version = 1
            jt.horizon = track.duration
            jt.ref = ready[1] if ready else _ref_of(a0)
        self.tracks.append(jt)
        del self.tracks[:-40]
        self.say(f"{'next up' if natural else 'now'}: {track.label}  [{kind}{' - ' + project if project else ''}]")
        gain, why = self._level_for(track.id)
        self.relay.set_level(est_start, gain)
        if why:
            self.say(f"level: {track.name}: {why}")
        if natural:
            self._measure_level(prev)
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
                self.say(f"adding {gap:.1f}s between songs to grow the delay back ({self.delay():.0f}s of {self.cfg.target_delay:.0f}s)")

    DRIFT_S = 5.0            # this far past the target delay, the room jumps back

    def keep_delay(self) -> str | None:
        """The delay only ever grew back (silence between songs, a pause, a doorbell, a speaker that stalled), never
        shrank. Past the target by DRIFT_S the room catches up, but only between songs (the user: never in the
        middle of one, as after a doorbell): it leaves out just enough of the end of the song it is in, the
        excess, and plays the next one from its first beat, back at 1 s behind the target. It waits for the next
        song to have begun at the capture head; already past where it should have left, it goes at once. Not
        while paused or held by a moment, nor in the 20 s after the speakers got a fresh stream or resumed."""
        with self.lock:
            if not self.streaming or self.gate.paused or self.show_moment_hold or self.output.stream is None:
                return None
            if self.clock() - self._streamed_at < 20.0 or self.clock() - self._resumed_at < 20.0:
                return None
            d = self.delay()
            if d <= self.cfg.target_delay + self.DRIFT_S:
                self._catch_up_said = False
                return None
            rc = self.room_capture_time()
            if rc is None:
                return None
            excess = d - (self.cfg.target_delay + 1.0)
            here = self.track_at(rc)
            nxt = next((t for t in self.tracks if t.start > rc + 0.5), None)
            if nxt is None or rc < nxt.start - excess - 0.05:
                if not self._catch_up_said:
                    self._catch_up_said = True
                    self.say(f"the room is {d:.0f}s behind: it catches up at the end of this song")
                return None
            self.output.start_stream(self._open_stream(max(self.relay.oldest_time, nxt.start)))
            self._streamed_at, self._catch_up_said = self.clock(), False
            self._quiet_start()
            left = max(0.0, nxt.start - rc)
            self.say(f"caught up between songs: the last {left:.0f}s of {here.track.label if here else 'the song'} left out, "
                     f"{nxt.track.label} from its start ({d:.0f}s behind -> {d - left:.0f}s)")
            return f"delay {d:.0f}s -> {d - left:.0f}s"

    def _open_stream(self, at: float) -> RelayStream:
        """A stream of the party from capture time `at` for the speakers (a virtual one is not paced)."""
        return self.relay.open_stream(at, pace=not isinstance(self.output, VirtualOutput))

    def _quiet_start(self) -> None:
        """The stream just started is the room carrying on (a doorbell's song, the music back after it, a catch-up
        between songs): the speaker's delay has not changed, so no sync check follows it (party_service
        _maybe_auto_sync). One ran 8 s into a doorbell, listening to its song, and again as the music came
        back, crunching the microphone's recording while the lights faded: they stuttered, the studio lagged."""
        self.no_sync_start = getattr(self.output, "started_at", None)

    SPEAKER_LIVE = ("PLAYING", "PAUSED", "BUFFERING")

    RESUME_FRESH_S = 15.0    # paused longer than this, the speaker gets a fresh stream on resume, not "play"
    RESUME_CHECK_S = 5.0     # a plain resume whose room has not moved on by then gets one too

    def _resume_output(self) -> None:
        """Spotify plays again: so does the room. A speaker left paused for a while ends its Cast session
        (it chimes as it does), or keeps it but takes half a minute to play again (a 9 minute pause: the room
        sat silent, fell 36 s behind, and keep_delay jumped half a minute of the song): past RESUME_FRESH_S,
        or with its session gone, it gets a fresh stream from where the room stopped (playing in a second or
        two, nothing skipped), and with it a new sync check (party_service._maybe_auto_sync). A short pause just
        resumes, watched by keep_playing."""
        cast = getattr(self.output, "cast", None)
        if self.clock() - self._streamed_at < 8.0:
            # the speakers were just handed a fresh stream (a moment over, a skip): it is the room's already; no
            # resume, no watch (one more fresh stream on a speaker still starting was a glitch)
            self._speaker_dropped, self._paused_room, self._paused_at, self._resume_watch = False, None, None, None
            return
        paused_for = self.clock() - self._paused_at if self._paused_at is not None else 0.0
        at = self._paused_room if self._paused_room is not None else self.relay.capture_time - self.cfg.target_delay
        duck, self._duck_fresh = getattr(self, "_duck_fresh", False), False
        dropped = cast is not None and (self._speaker_dropped or getattr(cast, "state", "") not in self.SPEAKER_LIVE or paused_for > self.RESUME_FRESH_S)
        if dropped or (cast is not None and duck):
            self.output.start_stream(self.relay.open_stream(max(self.relay.oldest_time, at), pace=True))
            self._streamed_at = self.clock()
            self._resume_watch = None
            if dropped:
                why = "had ended its session" if self._speaker_dropped or getattr(cast, "state", "") not in self.SPEAKER_LIVE else f"was paused {paused_for:.0f}s"
                self.say(f"the speaker {why}: a fresh stream from where the room stopped, then a sync check")
            else:
                self._quiet_start()                          # (its delay is unchanged: no sync check)
                self.say("the music was turned down or up during the pause: a fresh stream from where the room stopped")
        else:
            self.output.resume()
            self._resumed_at = self.clock()                 # (keep_delay waits for it to settle)
            self._resume_watch = (self.clock(), at) if cast is not None else None
        self._speaker_dropped, self._paused_room, self._paused_at = False, None, None

    def keep_playing(self) -> str | None:
        """The speaker's own pause (tapped on the speaker, or the Cast controls on a phone) paused the party
        for everyone: it plays on at once. Pausing the party is Spotify's to do (the phone, the phone remote):
        then, as for a moment or the studio closing, the speakers are left paused. A speaker stopped there (its
        session ended: the speaker's own "next", as there is no next) for 2 s gets a fresh stream, back at
        the target delay. (Its "back" asks for the stream again: restarted where the room was, see
        party_service.stream.)"""
        cast = getattr(self.output, "cast", None)
        with self.lock:
            if (cast is not None and self.streaming and self.gate.paused and not self.show_moment_hold and not self._speaker_dropped
                    and self._paused_room is not None and getattr(cast, "state", "") not in self.SPEAKER_LIVE):
                self._speaker_dropped = True              # paused long enough for the speaker to let go (_resume_output)
                self.say("the speaker ended its Cast session during the pause: it restarts when the music does")
            if cast is None or not self.streaming or self.gate.paused or self.show_moment_hold or self.output.stream is None:
                self._idle_seen = 0
                return None
            # a plain resume (_resume_output) the speaker did not pick up: the room has not moved on since, so a
            # fresh stream from where it stopped (never a jump ahead: nothing of the song is lost)
            watch = self._resume_watch
            if watch is not None and self._streamed_at > watch[0]:
                self._resume_watch = watch = None                # a fresh stream since: it is not the resume's
            if watch is not None and self.clock() - watch[0] >= self.RESUME_CHECK_S:
                self._resume_watch = None
                rc = self.room_capture_time()
                if rc is None or rc - watch[1] < 1.0:
                    self.output.start_stream(self.relay.open_stream(max(self.relay.oldest_time, watch[1]), pace=True))
                    self._streamed_at = self.clock()
                    self.say("the speaker did not play on after the pause: a fresh stream from where the room stopped")
                    return "restarted the speaker after the pause"
            state = getattr(cast, "state", "")
            if state == "PAUSED":
                self._idle_seen = 0
                try:
                    cast.resume()
                except Exception:  # noqa: BLE001 - tried again in a second
                    return None
                self.say("paused on the speaker: playing on (the party is paused on Spotify)")
                return "resumed the speaker"
            if state == "IDLE" and self.clock() - self._streamed_at > 20.0:
                self._idle_seen = getattr(self, "_idle_seen", 0) + 1
                if self._idle_seen >= 2:
                    self._idle_seen = 0
                    to = self.cfg.target_delay + 1.0
                    self.output.start_stream(self.relay.open_stream(max(self.relay.oldest_time, self.relay.capture_time - to), pace=True))
                    self._streamed_at = self.clock()
                    self.say("stopped on the speaker: playing again")
                    return "restarted the speaker"
                return None
            self._idle_seen = 0
            return None

    # ------------------------------------------------------------------ song levels
    def _level_for(self, track_id: str) -> tuple[float, str]:
        """The gain a song plays at (1.0: as Spotify plays it), and why, for the log. Measured as it played
        whole is believed first, then its YouTube copy; a song never measured plays as it is."""
        if not self.cfg.even_levels or not track_id:
            return 1.0, ""
        from .loudness import gain_db
        lv = self.library.level(track_id)
        m, src = (lv["relay"], "as it played") if "relay" in lv else (lv["copy"], "its YouTube copy") if "copy" in lv else (None, "")
        if m is None:
            return 1.0, ""
        db = gain_db(m, self.cfg.level_target)
        return 10 ** (db / 20), f"{db:+.1f} dB ({m['lufs']:.1f} LUFS, from {src})"

    def even_levels(self, on: bool) -> None:
        """Switch the evening out on or off now: the songs held (the room's, Spotify's) take their levels, or
        all go back to as Spotify plays them, gliding there."""
        with self.lock:
            self.cfg.even_levels, self._planned_level = bool(on), None
            with self.relay.cond:
                self.relay.levels = []
            if on:
                for t in self.tracks[-3:]:
                    self.relay.set_level(t.start, self._level_for(t.track.id)[0])

    def plan_levels(self) -> None:
        """A song that ends by itself hands over to the next one queued: that one's level is set where the
        song is due to end, a few seconds before Spotify gets there, so the change falls between the two
        (heard when the change comes, it would land a second into the new song). A skip or a surprise is
        put right when the new song is heard (_begin_track)."""
        if not self.cfg.even_levels:
            return
        with self.lock:
            cur = self.current
            if cur is None or cur.end is not None or not self.queue or self._planned_level == cur.serial:
                return
            due = cur.start + float(cur.track.duration) - self.cfg.crossfade
            if due - self.relay.capture_time > 15.0:
                return
            nxt = self.queue[0]
            self._planned_level = cur.serial
        gain, _ = self._level_for(nxt.get("id") or "")
        self.relay.set_level(due - 0.3, gain)

    def _measure_level(self, jt: PartyTrack) -> None:
        """A song that played whole, from its start, is measured as the speakers got it (in the background),
        kept for next time, and set beside what its YouTube copy said."""
        start, end = jt.start, jt.end
        if (not self.cfg.even_levels or end is None or jt.heard_from is not None or start < self.relay.oldest_time
                or end - start < max(30.0, 0.8 * float(jt.track.duration or 0))):
            return
        frames = self.relay.frames(start, end)

        def run():
            from .loudness import measure
            try:
                m = measure(frames, self.relay.sr)
                if m is None:
                    return
                copy = self.library.level(jt.track.id).get("copy")
                self.library.save_level(jt.track.id, "relay", m)
                said = f" (its YouTube copy: {copy['lufs']:.1f}, {m['lufs'] - copy['lufs']:+.1f} dB)" if copy else ""
                self.say(f"loudness: {jt.track.name}: {m['lufs']:.1f} LUFS as it played{said}")
            except Exception as e:  # noqa: BLE001 - measured again next time it plays
                self.say(f"loudness: {jt.track.name}: not measured ({type(e).__name__})")
        threading.Thread(target=run, daemon=True).start()

    def _quietest_near(self, t: float, reach: float = 1.2) -> float:
        """The quietest moment around a song boundary, so inserted silence lands in the seam."""
        t0, t1 = max(self.relay.oldest_time, t - reach), min(self.relay.capture_time, t + reach)
        x = self.relay.frames(t0, t1)
        win = int(0.05 * self.relay.sr)
        if len(x) < win * 2:
            return t
        level = np.abs(x[: len(x) // win * win].astype(np.int32)).max(axis=1).reshape(-1, win).mean(axis=1)
        return t0 + (int(np.argmin(level)) + 0.5) * win / self.relay.sr

    def _flush(self, jt: PartyTrack) -> None:
        """A skip: drop what was waiting and put the room on the new track at once."""
        if not self.streaming:
            return
        self.output.start_stream(self._open_stream(max(jt.start, self.relay.oldest_time)))
        self._streamed_at = self.clock()
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
            self.output.start_stream(self._open_stream(from_time))
            self._streamed_at = self.clock()
            self.streaming = True
            self.say(f"speakers started {self.relay.capture_time - from_time:.0f}s behind")

    def stop(self) -> None:
        with self.lock:
            self.streaming = False
            self.output.stop()

    # ------------------------------------------------------------------ moments
    def hold(self, why: str = "a moment has the speakers: the room holds where it is") -> float | None:
        """A moment stops the party (or the studio is closing): the room stops at once, where it is, and
        Spotify is paused (the buffer is held, as for a pause on the phone). Returns where the room stopped."""
        with self.lock:
            rc = self.room_capture_time()
            if not self.gate.paused:
                self.gate.paused = True
                if self.streaming:
                    self.output.pause()
            self.held_at, self.show_moment_hold, self.music_back = rc, True, False
            self.say(why)
        self.relay.hold()                                # (what Spotify plays as it pauses: see Relay.hold)
        try:
            self.spotify.pause()
        except Exception:  # noqa: BLE001 - nothing playing, or not Premium: the room is held regardless
            pass
        return rc

    def show_moment_insert(self, pcm: np.ndarray, fade_in_s: float) -> bool:
        """A moment's song, played inside the room's own stream: the speakers get one fresh stream (as for a
        skip, they drop what they held) that cuts from where the room is straight into the song, plays it to
        its end, then the music from where it held, fading in over fade_in_s. Nothing is paused or loaded on
        the speakers in between, so the song starts at once. Spotify is paused meanwhile (the buffer is
        held). False when there is no stream to put it in."""
        with self.lock:
            if not self.streaming or self.output.stream is None:
                return False
            at = self.room_capture_time()
            if at is None:
                # the speakers are starting a fresh stream this moment (the room's place not known yet): where
                # it should be, not the old way (a doorbell rung 2 s after one played its song on the speakers,
                # with a pause, music back late and glitches)
                at = self._paused_room if self._paused_room is not None else self.relay.capture_time - self.cfg.target_delay
                at = max(self.relay.oldest_time, at)
            sr = self.relay.sr
            fresh = self._open_stream(at)
            fresh.insert(pcm, int(fade_in_s * sr))
            self.gate.paused = True                       # Spotify's pause is not the room's: nothing to pause
            self.show_moment_hold, self.show_moment_in_stream, self.music_back = True, True, False
            self.held_at = at                            # where the music picks up after the song
            self.output.start_stream(fresh)
            self._streamed_at = self.clock()             # (keep_delay leaves a fresh stream alone while it settles)
            self._quiet_start()
            self.say("a moment: its song plays in the room's stream, the music holds where it is")
        self.relay.hold()                                # (what Spotify plays as it pauses: see Relay.hold)
        try:
            self.spotify.pause()
        except Exception:  # noqa: BLE001 - nothing playing, or not Premium: the buffer is held regardless
            pass
        return True

    def duck_music(self, to: float, seconds: float) -> str:
        """The music turned down (a party mode) or back up, fading over `seconds` from what the room hears
        now, the speakers' own volume left alone. They hold up to 20 s already sent, so playing they get a fresh
        stream from where the room is with the fade in it, as the music comes back after a doorbell. With the room
        held (a moment, a pause) nothing is heard: the music is simply at its new level when it plays again."""
        with self.lock:
            playing = self.streaming and self.output.stream is not None and self.held_at is None and self._room_playing()
            rc = self.room_capture_time()
            if playing and rc is not None:
                self.relay.set_duck(rc, seconds, to)
                fresh = self._open_stream(rc)
                old = self.output.stream.gain
                if old is not None and old[3] >= 1.0:             # (still fading in after a doorbell: carries on)
                    g = self.output.stream._gain_at(self.output.stream.sent)
                    fresh.fade(seconds, 1.0, start=g)
                self.output.start_stream(fresh)
                self._streamed_at = self.clock()             # (keep_delay leaves it to settle)
                self._resume_watch = None
                self._quiet_start()
                how = "fading"
            else:
                at = rc if rc is not None else self._paused_room if self._paused_room is not None else self.relay.oldest_time
                self.relay.set_duck(at, 0.0, to)
                if self.streaming and getattr(self.output, "cast", None) is not None and rc is not None and self.held_at is None:
                    self._duck_fresh = True                  # (what the paused speaker holds is at the old level)
                how = "at once (nothing playing)"
            self.say(f"the music {'down to ' + str(round(to * 100)) + '%' if to < 1 else 'back up'}, {how}")
            return how

    SEAMS_KEPT = 6

    def _seam(self, info: dict, around) -> None:
        """Spotify played again after a pause (Relay._mend): said in the log; the audio around it kept."""
        gap = f", {info.get('gap_ms', 0):.0f} ms of silence between left out" if info.get("gap_ms") else ""
        if info["cut_s"]:
            self.say(f"Spotify's pause mended: {info['cut_s']:.2f}s it played twice left out (match {info['match']:.2f}){gap}")
        else:
            self.say(f"Spotify's pause: nothing played twice found (best match {info['match']:.2f}), joined{gap}")
        print(f"[party] {self.log[-1][9:]}")                # (in studio.log too: the party's own log goes with the studio)
        if around is None:
            return
        sr, d = self.relay.sr, self.seam_dir

        def keep():
            try:
                import soundfile as sf
                os.makedirs(d, exist_ok=True)
                sf.write(os.path.join(d, time.strftime("%Y%m%d-%H%M%S") + f"-cut{info['cut_s']:.2f}.wav"), around, sr)
                for old in sorted(os.listdir(d))[:-self.SEAMS_KEPT]:
                    os.remove(os.path.join(d, old))
            except Exception:  # noqa: BLE001 - only kept to listen to
                pass
        threading.Thread(target=keep, daemon=True, name="seam-keep").start()

    def show_moment_position(self) -> float | None:
        """How far the room is into a moment's song played in the stream (the speakers' own position)."""
        with self.lock:
            if not (self.show_moment_hold and self.show_moment_in_stream and self.output.stream is not None):
                return None
            return max(0.0, self.output.position() - self.output.offset)

    def show_moment_over(self) -> None:
        """The moment's song is over and the music fades back in, already in the stream: the room is the party's
        again, and Spotify resumes behind it."""
        with self.lock:
            self.held_at, self.show_moment_hold = None, False
            self.music_back = self.streaming and self.output.stream is not None
            self.say("the moment is over: the music fades back in")
        try:
            self.spotify.resume()
        except Exception:  # noqa: BLE001
            pass

    def release(self, restart: bool, fade_in: float = 0.0) -> None:
        """The moment is over: the room carries on from where it stopped. When the moment played on the
        party's own speakers (restart), they get the party's stream back from that point and play it at
        once, fading in over fade_in seconds; then Spotify resumes (otherwise the next poll lets the
        paused room run again)."""
        with self.lock:
            at, self.held_at, self.show_moment_hold = self.held_at, None, False
            if self.show_moment_in_stream:
                restart = True                            # cut short: the song is still in the stream
            if restart and self.streaming and at is not None:
                # it plays from there at once: the buffer already holds what comes next
                fresh = self._open_stream(at)
                if fade_in > 0:
                    fresh.fade(fade_in, 1.0, start=0.0)
                self.output.start_stream(fresh)
                self._streamed_at = self.clock()          # (keep_delay leaves it to settle)
                self._resume_watch = None
                self._quiet_start()
                self.music_back = True
            self.say("the moment is over: the room carries on")
        try:
            self.spotify.resume()
        except Exception:  # noqa: BLE001
            pass

    # ------------------------------------------------------------------ opening song
    def arm_handover(self) -> None:
        """Open the night with a song from disk and slide into the party behind it.

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

    def open_with_song(self) -> None:
        """The opening song plays from its start while Spotify plays, unheard, from the start too: the
        buffer fills for a whole song, and the room joins the party as the opener ends, a song behind."""
        with self.lock:
            self.handover, self.auto_start, self.opening = "filling", False, True

    def handover_tick(self, remaining: float | None, playing: bool) -> None:
        """Call about once a second with how much of the opening song is left."""
        with self.lock:
            if self.show_moment_hold:                                # a moment has the speakers: the party joins once it is over
                return
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
                if self.opening:
                    # The party stays as far behind as the opener let it get (Spotify may have started a little
                    # late): aiming further back would only add silence between the next songs
                    self.opening = False
                    self.cfg.target_delay = max(20.0, min(self.cfg.target_delay, self.buffered()))
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
        if (cur and cur.kind == "learned" and cur.source == "youtube" and cur.aligned and cur.end is None and cur.ref is not None
                and head - cur.verified_at >= VERIFY_EVERY_S):
            did = self._verify(cur, head)
            if did:
                return did
        if cur and cur.kind == "live" and cur.end is None and self.clock() - cur.checked_at >= 5.0:
            cur.checked_at = self.clock()
            if self.library.has_learned(cur.track.id):
                return self._upgrade(cur)
        if cur and cur.kind == "live" and cur.end is None and cur.genre is None and head - cur.start >= 30.0:
            return self._genre(cur, min(head - cur.start, cur.track.duration))
        if cur and cur.kind == "live" and cur.end is None and self.cfg.light_live:
            # While it is learned ahead, an unlearned song just glows slowly (party_show.js buildLight): no beat
            # is followed, so the live beat tracker no longer runs every few seconds, and the PC's effort
            # goes to learning the song.
            return None
        if cur and cur.kind == "live" and cur.end is None:
            have = min(head - cur.start, cur.track.duration)
            every = self.cfg.analyse_every
            if have >= self.cfg.min_audio and have - cur.horizon >= every:
                return self._pass(cur, have, final=False)
        return None

    def _upgrade(self, jt: PartyTrack) -> str | None:
        """The song playing was learned ahead while it played (from its copy): the full analysis from
        here on, lined up by ear like any learned song."""
        learned = self.library.learned(jt.track.id)
        if not learned:
            return None
        a = learned["analysis"]
        _settle_learned(a)
        with self.lock:
            jt.kind, jt.analysis, jt.genre, jt.source = "learned", a, a.get("genre"), learned.get("source", "heard")
            jt.horizon, jt.version, jt.saved = jt.track.duration, jt.version + 1, True
            jt.aligned = False
            jt.ref = _ref_of(a)
        self.say(f"{jt.track.label}: learned ahead while it played, the full show from here")
        return f"upgraded {jt.track.name}"

    def _align(self, jt: PartyTrack, head: float) -> bool:
        heard = max(jt.start, self.relay.oldest_time, jt.heard_from if jt.heard_from is not None else jt.start)
        have = (jt.end or head) - heard                     # what was actually recorded of it (since a seek, if one)
        # With the room only seconds behind (learning ahead), the song has to be lined up before the
        # room hears it: tried from 7 s in, believed then only on a clear match, and from 14 s as before.
        early = EARLY_S <= have < 14.0
        if have < EARLY_S or (early and head - jt.tried_at < 1.0):
            return False
        jt.tried_at = head
        if jt.ref is None:
            jt.ref = self._reference(jt)
            if jt.ref is None:
                jt.aligned = True                           # nothing to align against: trust Spotify's clock
                return False
        env, fps = jt.ref
        env = np.atleast_2d(env)
        # A copy from YouTube may start a few seconds earlier or later than Spotify's (an intro, more
        # silence): when it does not line up close to Spotify's start, it is searched for further either
        # way once enough has been heard for that, and only given up on if it is not found there either.
        wide = jt.source == "youtube" and jt.narrow_failed
        if wide and have < 2 * WIDE_REACH + 10.0:
            return False
        reach = WIDE_REACH if wide else 2.0
        w0 = max(self.relay.oldest_time, jt.start - reach, heard if jt.heard_from is not None else -1e18)
        # A song already playing when the party began has no opening in the recording: match the
        # part of the saved song that the recording's first moment could be, not its first seconds.
        q0 = max(0.0, w0 - (jt.start - reach))            # song time at w0 for the earliest start searched
        if int(q0 * fps) + int(8 * fps) > env.shape[1]:     # nothing saved that far into it (a seek past what is kept)
            jt.aligned = True
            self.say(f"{jt.track.name}: nothing saved that far in to line up against; using Spotify's clock")
            return True
        y = self.relay.audio(w0, min(w0 + 24.0 + 2 * reach, jt.end or head), SR_ANALYSIS, mono=True)
        start, peak, edge = align_start(live_bands(y), env[:, int(q0 * fps):], w0 - q0, fps, reach=reach, min_ref=4.0 if early else 8.0)
        jt.align_peak, jt.align_edge = peak, edge
        if early and not (peak >= EARLY_PEAK and edge >= EARLY_EDGE and abs(start - jt.start) <= reach + 0.5):
            return False                                    # not sure yet: another second of it
        if peak >= ALIGN_PEAK and edge >= ALIGN_EDGE and abs(start - jt.start) <= reach + 0.5:
            moved = start - jt.start
            with self.lock:
                jt.start, jt.aligned, jt.version = start, True, jt.version + 1
                jt.verified_at = head
            self.say(f"lined up {jt.track.name} by ear {have:.0f}s in ({moved * 1000:+.0f} ms, match {peak:.2f})")
        elif jt.source == "youtube" and not wide and jt.end is None:
            jt.narrow_failed = True                          # not near Spotify's start: look further before giving up
            self.say(f"{jt.track.name}: its copy does not start where Spotify's does (match {peak:.2f}); looking further")
            return False
        elif jt.source == "youtube" and peak < MISMATCH_PEAK and jt.end is None:
            self._mismatch(jt, peak)
        else:
            jt.aligned = True
            why = "it matched equally well a beat away" if peak >= ALIGN_PEAK else "it did not sound like the saved version"
            self.say(f"could not line up {jt.track.name} by ear ({why}); using Spotify's clock")
        return True

    def _verify(self, jt: PartyTrack, head: float) -> str | None:
        """A song learned from a copy, checked again while it plays: the last ten seconds heard against
        the same stretch of the copy. Still on the beat: nothing to do (or a small slip put right).
        Not the same music any more (another edit from here): its show is built as it plays."""
        jt.verified_at = head
        env, fps = jt.ref
        env = np.atleast_2d(env)
        span, reach = 10.0, 0.5
        q = head - span - jt.start                           # song time the window opens at
        if q < 0 or q + span > env.shape[1] / fps or (jt.heard_from is not None and head - span - reach < jt.heard_from):
            return None
        y = self.relay.audio(head - span - reach, head, SR_ANALYSIS, mono=True)
        found, peak, edge = align_start(live_bands(y), env[:, int(q * fps):int((q + span) * fps)], head - span - reach, fps,
                                        reach=reach, min_ref=6.0)
        if peak < MISMATCH_PEAK:
            # one check that does not match (a quiet stretch, a breakdown with little to match on) is not yet
            # another version: two in a row are
            jt.verify_misses += 1
            if jt.verify_misses < 2:
                return None
            self._mismatch(jt, peak)
            return f"{jt.track.name}: no longer matches its copy"
        jt.verify_misses = 0
        slip = (found - q) - jt.start
        if peak >= ALIGN_PEAK and edge >= ALIGN_EDGE and 0.05 < abs(slip) <= reach:
            with self.lock:
                jt.start, jt.version = jt.start + slip, jt.version + 1
            self.say(f"{jt.track.name}: put back on the beat ({slip * 1000:+.0f} ms)")
            return f"re-aligned {jt.track.name}"
        return None

    def _mismatch(self, jt: PartyTrack, peak: float) -> None:
        """What Spotify plays is not the copy this song was learned from (another version, or the wrong
        song found): its show is built as it plays instead, from Spotify's own audio, like a first
        listen, and that copy is not used again.

        Learning ahead, a song playing unlearned only glows (nothing is built as it plays), and the copy
        set aside left it glowing for the rest of the night (Panic Room): there the learned show is kept,
        on Spotify's clock rather than lined up by ear, and the copy is not set aside."""
        if self.cfg.light_live:
            with self.lock:
                jt.aligned, jt.ref, jt.verify_misses = True, None, 0
            self.say(f"{jt.track.name}: Spotify's version does not quite match its copy here (match {peak:.2f}); "
                     "keeping its show, on Spotify's clock")
            return
        self.library.forget_learned(jt.track.id)
        with self.lock:
            jt.kind, jt.source, jt.analysis, jt.genre, jt.ref = "live", "heard", None, None, None
            jt.horizon, jt.passes, jt.bpm_lock, jt.saved, jt.aligned = 0.0, 0, None, False, True
            jt.version += 1
        self.say(f"{jt.track.name}: Spotify's version does not match the copy it was learned from (match {peak:.2f}); "
                 "building its show as it plays")
        if self.on_mismatch is not None:
            try:
                self.on_mismatch(jt.track)
            except Exception:  # noqa: BLE001
                pass

    def _reference(self, jt: PartyTrack):
        if jt.kind == "custom" and jt.project:
            song = self.library.project_song(jt.project)
            if song and self.cache_dir is not False:
                try:
                    from .analysis import analyze_cached
                    return _ref_of(analyze_cached(song, os.path.join(os.path.dirname(song), "cache")))
                except Exception as e:  # noqa: BLE001
                    self.say(f"could not read the analysis of {jt.project}: {e}")
        return None

    def _genre(self, jt: PartyTrack, have: float) -> str:
        """Decide how hard this track's lights should go, from half a minute of it and its artist's genres."""
        from .genre import style_from

        labels = None
        if self.genre_fn is not None:
            try:
                labels = self.genre_fn(self.relay.audio(max(jt.start, self.relay.oldest_time), jt.start + have, 16000, mono=True), 16000)
            except Exception as e:  # noqa: BLE001
                self.say(f"genre model unavailable: {e}")
        if labels is None:                                 # noted with what is learned of it: the song shows "error"
            from .genre import WORKER
            jt.genre_problem = f"the genre could not be heard ({WORKER.failed or WORKER.last_error or ('no genre model' if self.genre_fn is None else 'no answer')})"
            if jt.analysis is not None:
                jt.analysis["problems"] = [*(jt.analysis.get("problems") or []), jt.genre_problem]
        drive = float(((jt.analysis or {}).get("mood") or {}).get("drive", 0.6))
        g = style_from(labels, drive, jt.spotify_genres)
        with self.lock:
            jt.genre, jt.version = g, jt.version + 1
            if jt.analysis is not None:
                jt.analysis["genre"] = g
        return f"{jt.track.name}: {g['style']} ({', '.join(g['genres'] or g['spotify'] or ['no genre found'])})"

    def _pass(self, jt: PartyTrack, have: float, final: bool) -> str:
        y = self.relay.audio(jt.start, jt.start + have, SR_ANALYSIS, mono=True, pad=True)   # song time 0 is sample 0
        events = final or (jt.passes % self.cfg.events_every == self.cfg.events_every - 1)
        stereo = self.relay.audio(jt.start, jt.start + have, SR_ANALYSIS, mono=False, pad=True) if events else None
        a = self.analyse_fn(y, SR_ANALYSIS, stereo=stereo, use_model=False, events=events, bpm_hint=jt.bpm_lock,
                            segment_fn=self.segment_fn)
        if not events and jt.analysis:                      # keep the last instrument pass until the next one
            for k in ("events", "fills", "hook", "curves", "drum_calibration", "rolls", "cuts", "entries", "hatrolls", "wobbles", "key_change"):
                a[k] = jt.analysis.get(k, a.get(k))
        if jt.genre:
            a["genre"] = jt.genre                          # what was decided about the genre carries across passes
        if jt.genre_problem:
            a["problems"] = [*(a.get("problems") or []), jt.genre_problem]
        with self.lock:
            jt.passes += 1
            if jt.passes >= 2 and jt.bpm_lock is None:
                jt.bpm_lock = float(a["bpm"])
            jt.analysis, jt.horizon, jt.version = a, have, jt.version + 1
        return f"analysed {have:.0f}s of {jt.track.name}: {a['bpm']:.1f} bpm, {len(a['sections'])} sections, {len(a['drops'])} drops"

    def _finish(self, jt: PartyTrack) -> str:
        have = jt.end - jt.start
        jt.saved = True
        if self.cfg.light_live:
            return f"{jt.track.name}: heard only for its beat; it is learned from its copy instead"
        if have < max(30.0, 0.9 * jt.track.duration) or jt.start < self.relay.oldest_time:
            return f"{jt.track.name}: not enough of it was heard to keep"
        msg = self._pass(jt, have, final=True)
        self.library.save_learned(jt.track, jt.analysis)
        self.say(f"learned {jt.track.label}: next time it gets a full show from the first beat")
        return msg + " (saved)"

    # --------------------------------------------------------------------- for the UI
    def refresh_queue(self) -> None:
        try:
            upcoming = list(self.spotify.queue())        # all Spotify tells (about 20): learned ahead
            q = [self._kind_of(t) for t in upcoming[:12]]    # (the first 12 shown)
        except Exception:  # noqa: BLE001
            return
        with self.lock:
            self.queue = q
            cur = self.current
        if self.on_queue is not None:
            playing = [cur.track] if cur is not None and cur.end is None and cur.kind == "live" else []
            try:
                self.on_queue(playing + upcoming, playing[0].id if playing else None)   # the song heard now first
            except Exception as e:  # noqa: BLE001
                self.say(f"learn ahead: {type(e).__name__}: {e}")
        if self.on_context is not None:
            self._look_further(cur)

    def _look_further(self, cur) -> None:
        """Past the queue Spotify shows: the rest of the playlist or album it plays from, learned behind the
        queue (from the song after this one, around to the start; any order when it shuffles). Read once
        each time what it plays from changes."""
        try:
            ctx = getattr(self.spotify, "context", lambda: None)()
        except Exception:  # noqa: BLE001
            return
        if not ctx or ctx[0] == self._context_uri:
            return
        self._context_uri = ctx[0]
        uri, shuffled = ctx
        here = cur.track.id if cur is not None else None

        def read():
            try:
                tracks = list(self.spotify.context_tracks(uri))
                at = next((i for i, t in enumerate(tracks) if t.id == here), -1)
                if at >= 0 and not shuffled:
                    tracks = tracks[at + 1:] + tracks[:at]
                self.on_context(tracks)
            except (PermissionError, LookupError):        # Spotify will not list it (its own mixes): no matter
                pass
            except Exception as e:  # noqa: BLE001 - the queue is still learned ahead
                self.say(f"learn ahead: the playlist could not be read ({type(e).__name__}: {e})")
        threading.Thread(target=read, daemon=True, name="learn-further").start()

    def _queue_after(self, ahead) -> list:
        """The queue as last read, without the song Spotify plays now at its head."""
        q = list(self.queue)
        now_id = ahead.track.id if ahead is not None else None
        while q and now_id and q[0].get("id") == now_id:
            q.pop(0)
        return q

    def _kind_of(self, track: Track) -> dict:
        project = self.library.match_project(track)
        return {**track.to_json(), "kind": "custom" if project else "learned" if self.library.has_learned(track.id) else "live", "project": project}

    def state(self) -> dict:
        with self.lock:
            rc = self.room_capture_time()
            rt = self.track_at(rc) if rc is not None else None
            room = None
            if rt is not None:
                room = {**rt.to_json(), "position": round(rc - rt.start, 3), "playing": self._room_playing(), "at": self.clock()}
            ahead = self.current
            return {
                "streaming": self.streaming, "delay": round(self.delay(), 2), "target_delay": self.cfg.target_delay,
                "buffered": round(self.buffered(), 1), "paused": self.gate.paused, "speaker": self.output.name,
                "room": room, "ahead": ahead.to_json() if ahead else None,
                "spotify": {"signed_in": self.spotify.signed_in, "device": self.now.device if self.now else "", "error": self.error},
                # (the queue is read every 6 s: just after Spotify moves on, it still opens with the song now
                # playing, which Up next showed twice)
                "queue": self._queue_after(ahead), "flushes": self.flushes, "log": self.log[-12:],
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
                    "horizon": round(jt.horizon, 2), "light": jt.kind == "live" and self.cfg.light_live}
            if jt.kind == "custom":
                return base
            if jt.kind == "learned":
                kept = self.library.locked(jt.track.id)
                if kept:                                     # locked by the user: the show and its analysis as they were
                    return {**base, "analysis": kept["analysis"], "locked": kept["project"]}
                lyr = self.library.lyrics(jt.track.id) if jt.track.id else None
                return {**base, "analysis": {**jt.analysis, "lyrics": lyr} if lyr else jt.analysis}
            a, dur = jt.analysis, jt.track.duration
            head = min(self.relay.capture_time - jt.start, dur)
            feat = self.features.slice(jt.start, jt.start + head)
        n_f, n_p = int(dur * FEATURE_FPS), int(dur * WAVE_PPS)
        pad = lambda xs, n, fill: (xs + [fill] * max(0, n - len(xs)))[:n]
        out = {"duration": dur, "feature_fps": FEATURE_FPS, "wave_pps": WAVE_PPS, "structure_source": "live", "heard": round(head, 2),
               "energy": pad(feat["energy"], n_f, 0.0), "bands": pad(feat["bands"], n_f, [0.0] * N_BANDS), "peaks": pad(feat["peaks"], n_p, 0.0)}
        if a is None:                                        # nothing analysed yet: a plain grid and one open section
            out.update({"bpm": 120.0, "downbeat_phase": 0,
                        "beats": [round(i * 0.5, 4) for i in range(int(dur * 2))], "provisional": True,
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
                        "hook": a.get("hook"), "curves": a.get("curves", {}), "provisional": jt.horizon < dur - 1.0,
                        # what the drum track says: whether the song has drums at all, and its rolls
                        "drum_calibration": a.get("drum_calibration"), "rolls": a.get("rolls", []),
                        "cuts": a.get("cuts", []), "entries": a.get("entries", []), "hatrolls": a.get("hatrolls", []), "wobbles": a.get("wobbles", []), "key_change": a.get("key_change")})
        return {**base, "analysis": out, "analysed_to": round(head, 2)}


# ============================================================================== runner
class PartyRunner:
    """Threads around the engine for real use: capture is already running, this polls and analyses."""

    def __init__(self, engine: PartyEngine, file_status=None):
        self.engine = engine
        self.file_status = file_status            # () -> (seconds left of a song from disk, playing?) for the hand-over
        self.stop_flag = threading.Event()

    def start(self) -> None:
        self.stop_flag.clear()
        for fn in (self._poll, self._analyse):
            threading.Thread(target=fn, daemon=True).start()

    def stop(self) -> None:
        self.stop_flag.set()
        self.engine.stop()

    def _poll(self) -> None:
        n = 0
        while not self.stop_flag.wait(1.0):
            e = self.engine
            try:
                e.poll()
                e.keep_delay()
                e.keep_playing()
                e.plan_levels()
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
            # (the code only: no model is loaded here, where it would compete on the GPU with the learn-ahead
            # process loading its own; a model loads when a first listen first needs it)
            self.engine.analyse_fn(y, SR_ANALYSIS, stereo=np.stack([y, y]), use_model=False, events=True,
                                   segment_fn=None)
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
