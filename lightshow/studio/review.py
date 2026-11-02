"""Review mode: step through the learned Party songs that have no saved show, set their sections right in the
studio, and keep what the user says as the truth the section labeler learns from (labeler.py).

A song can be reviewed when it was learned (shows/_party/learned/<id>.json) and its copy kept
(shows/_party/copies/<id>.flac): the studio plays the copy, the learned analysis is the one a Party uses (its
sections settled by the current rules), and nothing is saved as a show. What the user gives back goes to
shows/_party/section_truth.json under the track id:

    {"name": "Artist - Title", "drops": [drop starts], "sections": [{"type", "start", "end"}, ...],
     "reviewed": "corrected" | "right as found", "added": "YYYY-MM-DD"}

("drops" as the scorer has always read them; "sections" the whole song, every boundary and type.)
"""

from __future__ import annotations

import asyncio
import datetime
import json
import os
import re
import shutil
import threading

from aiohttp import web

from . import webjson
from .spotify import Track

SECTION_TYPES = ("intro", "verse", "buildup", "drop", "breakdown", "outro")


def _safe(track_id: str) -> str:
    return re.sub(r"[^A-Za-z0-9]", "", track_id)


def _read_json(path: str) -> dict:
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return {}


def _entries(path: str) -> dict:
    """A JSON file of songs, without its notes (keys starting with _)."""
    return {k: v for k, v in _read_json(path).items() if not k.startswith("_")}


def _write_json(path: str, doc: dict) -> None:
    """Written whole, through a temporary file."""
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(doc, fh, indent=1)
    os.replace(tmp, path)


def _track(track_id: str, t: dict) -> Track:
    """The track of a learned file."""
    return Track(id=track_id, name=t.get("name") or "", artists=list(t.get("artists") or []), duration=float(t.get("duration") or 0))


def _recycle(path: str, aside_dir: str) -> None:
    """Into the Recycle Bin (Windows); elsewhere, or if that fails, moved into aside_dir. Never deleted outright."""
    if os.name == "nt":
        try:
            import ctypes
            from ctypes import wintypes

            class SHFILEOPSTRUCTW(ctypes.Structure):
                _fields_ = [("hwnd", wintypes.HWND), ("wFunc", wintypes.UINT), ("pFrom", wintypes.LPCWSTR), ("pTo", wintypes.LPCWSTR),
                            ("fFlags", ctypes.c_uint16), ("fAnyOperationsAborted", wintypes.BOOL), ("hNameMappings", ctypes.c_void_p),
                            ("lpszProgressTitle", wintypes.LPCWSTR)]
            # FO_DELETE with FOF_ALLOWUNDO (the Recycle Bin), no questions, no progress, no error boxes
            op = SHFILEOPSTRUCTW(None, 3, os.path.abspath(path) + "\0", None, 0x0040 | 0x0010 | 0x0004 | 0x0400, False, None, None)
            if ctypes.windll.shell32.SHFileOperationW(ctypes.byref(op)) == 0 and not os.path.exists(path):
                return
        except Exception:  # noqa: BLE001 - set aside instead
            pass
    os.makedirs(aside_dir, exist_ok=True)
    shutil.move(path, os.path.join(aside_dir, os.path.basename(path) + datetime.datetime.now().strftime(" %Y%m%d-%H%M%S")))


class Review:
    def __init__(self, library, read_only: bool = False):
        self.library = library
        self.read_only = read_only                        # a test copy of the studio: never writes the truth
        self.lock = threading.Lock()
        self._heads: dict[str, tuple[float, dict]] = {}

    @property
    def truth_path(self) -> str:
        return os.path.join(self.library.dir, "section_truth.json")

    def copy_path(self, track_id: str) -> str:
        return os.path.join(self.library.dir, "copies", _safe(track_id) + ".flac")

    def truth(self) -> dict:
        return _read_json(self.truth_path)

    # Claude's suggested sections (tools/claude_review.py propose): never the truth by themselves. A song with one opens
    # in review mode on it (M flips to the labeler's own, to compare); Done keeps what is showing as the user's review,
    # and either way the suggestion is gone once the user has reviewed the song.
    @property
    def proposals_path(self) -> str:
        return os.path.join(self.library.dir, "section_proposals.json")

    def proposals(self) -> dict:
        return _entries(self.proposals_path)

    def propose(self, track_id: str, sections: list, by: str = "claude") -> None:
        if self.read_only:
            return
        with self.lock:
            props = self.proposals()
            props[track_id] = {"sections": [{"type": x["type"], "start": round(float(x["start"]), 3), "end": round(float(x["end"]), 3)} for x in sections],
                               "by": by, "added": datetime.date.today().isoformat()}
            _write_json(self.proposals_path, props)

    def _head(self, track_id: str) -> dict | None:
        """The track of a learned file (it comes right after the version: the rest is not read)."""
        path = os.path.join(self.library.learned_dir, _safe(track_id) + ".json")
        try:
            mtime = os.path.getmtime(path)
        except OSError:
            return None
        if track_id in self._heads and self._heads[track_id][0] == mtime:
            return self._heads[track_id][1]
        try:
            with open(path, encoding="utf-8") as fh:
                head = fh.read(8192)
            t, _ = json.JSONDecoder().raw_decode(head, head.index('"track": ') + len('"track": '))
        except (OSError, ValueError):
            return None
        self._heads[track_id] = (mtime, t)
        return t

    def _with_copies(self):
        """(id, track) of every learned song whose copy was kept."""
        try:
            names = os.listdir(self.library.learned_dir)
        except OSError:
            return
        for fn in names:
            if not fn.endswith(".json"):
                continue
            tid = fn[:-5]
            t = self._head(tid)
            if t and os.path.exists(self.copy_path(tid)):
                yield tid, _track(tid, t)

    def _twins(self, track_id: str) -> list[str]:
        return [t for t, _ in self.library.twins(track_id)] if hasattr(self.library, "twins") else []

    @staticmethod
    def _once(songs: list[dict]) -> list[dict]:
        """The same song under several ids (twins: an album and a single) listed once, the first of them."""
        keep, gone = [], set()
        for s in songs:
            if s["id"] not in gone:
                keep.append(s)
                gone.update(s["twins"])
        return keep

    def songs(self) -> list[dict]:
        """The learned songs with a kept copy and no saved show: the most doubted first (review_order.json), else by
        artist and title."""
        truth, props, out = self.truth(), self.proposals(), []
        for tid, track in self._with_copies():
            if self.library.match_project(track):
                continue
            seen = truth.get(tid) or {}
            out.append({"id": tid, "name": track.name, "artists": track.artists, "label": track.label,
                        "reviewed": seen.get("reviewed") or ("drops" if seen else ""), "proposal": tid in props and not seen,
                        "twins": self._twins(tid)})
        out = self._once(sorted(out, key=lambda s: (not s["reviewed"], s["id"])))     # (the reviewed one if any)
        # the songs the learned labeler and the rules disagree on most come first (tools/section_labeler.py order):
        # each review there teaches the most; without a ranking, by artist and title
        doubt = _entries(os.path.join(self.library.dir, "review_order.json"))
        # (songs already reviewed last, whatever an older ranking said of them)
        # (and before them all, the songs Claude suggested sections for: they wait for the user's yes or no)
        return sorted(out, key=lambda s: (bool(s["reviewed"]), not s["proposal"], -float(doubt.get(s["id"], 0.0)), s["label"].lower()))

    def every_song(self) -> list[dict]:
        """Every learned song with a kept copy, for the studio's song box: {id, label, show} (show: the saved show
        it plays, if it has one), each song once (twins: the one with a show, else the reviewed one, else any)."""
        truth, out = self.truth(), []
        for tid, track in self._with_copies():
            out.append({"id": tid, "label": track.label, "show": self.library.match_project(track), "reviewed": bool(truth.get(tid)),
                        "twins": self._twins(tid)})
        keep = self._once(sorted(out, key=lambda s: (not s["show"], not s["reviewed"], s["id"])))
        return sorted(({k: s[k] for k in ("id", "label", "show")} for s in keep), key=lambda s: s["label"].lower())

    def make_custom(self, track_id: str, project: dict) -> str:
        """A party song edited by hand becomes a show of its own: shows/<Artist - Title>/ with the song's kept copy
        and the show, and the track pinned to it (links.json), so the party plays it from then on."""
        t = self._head(track_id)
        if not t or not os.path.exists(self.copy_path(track_id)):
            raise ValueError("That song is not learned, or its copy is gone.")
        label = _track(track_id, t).label
        name = re.sub(r"[^\w\- .()]+", "_", label).strip(" .")[:80] or "untitled"          # (server.safe_name)
        if self.read_only:
            return name
        pdir = os.path.join(self.library.shows_dir, name)
        os.makedirs(pdir, exist_ok=True)
        if not any(f.startswith("song.") for f in os.listdir(pdir)):
            shutil.copy2(self.copy_path(track_id), os.path.join(pdir, "song.flac"))
        tmp = os.path.join(pdir, "project.json.tmp")
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump({**project, "name": name}, fh)
        os.replace(tmp, os.path.join(pdir, "project.json"))
        self.library.link(track_id, name)
        return name

    def drop_custom(self, track_id: str, name: str) -> bool:
        """Undone back to the party's own show: the show make_custom made for the song is set aside (to the Recycle
        Bin) and the track no longer pinned to it. Only a show pinned to this song, by this name."""
        if self.read_only or not name or self.library.links().get(track_id) != name:
            return False
        self.library.link(track_id, None)
        pdir = os.path.join(self.library.shows_dir, name)
        if os.path.isdir(pdir):
            _recycle(pdir, os.path.join(self.library.dir, "set_aside"))
        return True

    def song(self, track_id: str) -> dict | None:
        """What the studio's loader takes ({project, audio_url, analysis, saved}), for one learned song."""
        doc = self.library.learned(track_id)
        if not doc or not os.path.exists(self.copy_path(track_id)):
            return None
        a = doc["analysis"]
        from .analysis import settle_sections
        from .genre import restyle
        a["genre"] = restyle(a.get("genre"), float((a.get("mood") or {}).get("drive", 0.6)))
        settle_sections(a)
        lyr = self.library.lyrics(track_id)
        if lyr:
            a["lyrics"] = lyr
        t = doc.get("track") or {}
        return {"project": _track(track_id, t).label, "audio_url": f"/api/review/audio/{_safe(track_id)}", "analysis": a, "saved": None,
                "review": {"id": track_id, "truth": self.truth().get(track_id),
                           "proposal": (self.proposals().get(track_id) or {}).get("sections")}}

    def keep(self, track_id: str, sections: list, ok: bool, beat_shift: float = 0.0, by: str | None = None) -> dict:
        """The user's word on a song's sections, into the truth (written whole, through a temporary file);
        beat_shift: seconds the user moved its beat grid, added to the song's beat fix (party.py). by="claude": a review
        Claude made by itself (tools/claude_review.py), marked so: the labeler learns less from it, it is never scored
        against, and the user's own review of the song replaces it."""
        t = self._head(track_id)
        if not t:
            raise ValueError("no such learned song")
        secs = []
        for s in sections:
            kind = str(s.get("type"))
            if kind not in SECTION_TYPES:
                raise ValueError(f"unknown section type {kind!r}")
            secs.append({"type": kind, "start": round(float(s["start"]), 3), "end": round(float(s["end"]), 3)})
        secs.sort(key=lambda s: s["start"])
        entry = {"name": _track(track_id, t).label, "drops": [s["start"] for s in secs if s["type"] == "drop"], "sections": secs,
                 "reviewed": "right as found" if ok else "corrected", "added": datetime.date.today().isoformat()}
        if by:
            entry["by"], entry["reviewed"] = by, by
        if beat_shift and abs(beat_shift) < 2.0:
            entry["beat_shift"] = round(float(beat_shift), 4)
        old = self.truth().get(track_id) or {}
        if ok and not beat_shift and old.get("sections") == secs and old.get("by") == by:
            return old                                    # reviewed before and left as it was: kept as it was ("corrected" stays)
        if self.read_only:
            return entry
        if beat_shift and abs(beat_shift) < 2.0:
            self.library.set_beat_fix(track_id, float(self.library.beat_fixes().get(track_id) or 0.0) + float(beat_shift))
        with self.lock:
            truth = self.truth()
            truth.setdefault("_about", "Sections the user set by ear (review mode in the studio): the section labeler "
                                       "learns from them; sections are the whole song.")
            truth[track_id] = entry
            _write_json(self.truth_path, truth)
            props = self.proposals()
            if not by and track_id in props:                  # the user's word on it: the suggestion is decided
                del props[track_id]
                _write_json(self.proposals_path, props)
        try:                                                  # the learned labeler learns from it (every few songs)
            from . import labeler
            labeler.retrain_soon(self.library.dir)
        except Exception as e:  # noqa: BLE001 - the review is kept either way
            print(f"[labeler] no retrain: {type(e).__name__}: {e}")
        return entry

    # -- routes ------------------------------------------------------------------------------
    def routes(self) -> list:
        return [
            web.get("/api/review/songs", self.h_songs),
            web.get("/api/review/song/{id}", self.h_song),
            web.get("/api/review/audio/{id}", self.h_audio),
            web.post("/api/review/truth", self.h_truth),
            web.get("/api/review/every", self.h_every),
            web.post("/api/review/custom", self.h_custom),
            web.post("/api/review/uncustom", self.h_uncustom),
        ]

    async def h_uncustom(self, request):
        body = await request.json()
        done = await asyncio.get_running_loop().run_in_executor(None, self.drop_custom, str(body.get("track_id", "")), str(body.get("name", "")))
        return web.json_response({"ok": True, "dropped": done})

    async def h_every(self, request):
        return web.json_response({"songs": await asyncio.get_running_loop().run_in_executor(None, self.every_song)})

    async def h_custom(self, request):
        body = await request.json()
        try:
            name = await asyncio.get_running_loop().run_in_executor(None, self.make_custom, str(body["track_id"]), dict(body["project"]))
        except (KeyError, TypeError, ValueError) as e:
            return web.json_response({"error": str(e)}, status=400)
        return web.json_response({"ok": True, "name": name, "read_only": self.read_only})

    async def h_songs(self, request):
        songs = await asyncio.get_running_loop().run_in_executor(None, self.songs)
        return web.json_response({"songs": songs})

    async def h_song(self, request):
        doc = await asyncio.get_running_loop().run_in_executor(None, self.song, request.match_info["id"])
        return await webjson.response(doc) if doc else web.json_response({"error": "That song is not learned, or its copy is gone."}, status=404)

    async def h_audio(self, request):
        path = self.copy_path(request.match_info["id"])
        if not os.path.exists(path):
            raise web.HTTPNotFound()
        return web.FileResponse(path, headers={"Content-Type": "audio/flac"})

    async def h_truth(self, request):
        body = await request.json()
        try:
            entry = await asyncio.get_running_loop().run_in_executor(
                None, self.keep, str(body.get("id") or ""), list(body.get("sections") or []), bool(body.get("ok")),
                float(body.get("beat_shift") or 0.0))
        except (ValueError, KeyError, TypeError) as e:
            return web.json_response({"error": str(e)}, status=400)
        return web.json_response({"ok": True, "entry": entry})
