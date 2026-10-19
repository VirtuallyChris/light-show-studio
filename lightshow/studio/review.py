"""Review mode: step through the learned Jam songs that have no saved show, set their sections right in the
studio, and keep what the user says as the truth the section rules are scored against (score_sections.py).

A song can be reviewed when it was learned (shows/_jam/learned/<id>.json) and its copy kept
(shows/_jam/copies/<id>.flac): the studio plays the copy, the learned analysis is the one a Jam uses (its
sections settled by the current rules), and nothing is saved as a show. What the user gives back goes to
shows/_jam/section_truth.json under the track id:

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
import threading

from aiohttp import web

from .spotify import Track

SECTION_TYPES = ("intro", "verse", "buildup", "drop", "breakdown", "outro")


def _safe(track_id: str) -> str:
    return re.sub(r"[^A-Za-z0-9]", "", track_id)


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
        try:
            with open(self.truth_path, encoding="utf-8") as fh:
                return json.load(fh)
        except (OSError, ValueError):
            return {}

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

    def songs(self) -> list[dict]:
        """The learned songs with a kept copy and no saved show: the most doubted first (review_order.json), else by
        artist and title."""
        try:
            names = os.listdir(self.library.learned_dir)
        except OSError:
            return []
        truth, out = self.truth(), []
        for fn in names:
            if not fn.endswith(".json"):
                continue
            tid = fn[:-5]
            t = self._head(tid)
            if not t or not os.path.exists(self.copy_path(tid)):
                continue
            track = Track(id=tid, name=t.get("name") or "", artists=list(t.get("artists") or []), duration=float(t.get("duration") or 0))
            if self.library.match_project(track):
                continue
            seen = truth.get(tid) or {}
            out.append({"id": tid, "name": track.name, "artists": track.artists, "label": track.label,
                        "reviewed": seen.get("reviewed") or ("drops" if seen else "")})
        # the songs the learned labeler and the rules disagree on most come first (tools/section_labeler.py order):
        # each review there teaches the most; without a ranking, by artist and title
        try:
            with open(os.path.join(self.library.dir, "review_order.json"), encoding="utf-8") as fh:
                doubt = {k: v for k, v in json.load(fh).items() if not k.startswith("_")}
        except (OSError, ValueError):
            doubt = {}
        return sorted(out, key=lambda s: (-float(doubt.get(s["id"], 0.0)), s["label"].lower()))

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
        label = Track(id=track_id, name=t.get("name") or "", artists=list(t.get("artists") or []), duration=float(t.get("duration") or 0)).label
        return {"project": label, "audio_url": f"/api/review/audio/{_safe(track_id)}", "analysis": a, "saved": None,
                "review": {"id": track_id, "truth": self.truth().get(track_id)}}

    def keep(self, track_id: str, sections: list, ok: bool, beat_shift: float = 0.0) -> dict:
        """The user's word on a song's sections, into the truth (written whole, through a temporary file);
        beat_shift: seconds the user moved its beat grid, added to the song's beat fix (jam.py)."""
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
        label = Track(id=track_id, name=t.get("name") or "", artists=list(t.get("artists") or []), duration=0.0).label
        entry = {"name": label, "drops": [s["start"] for s in secs if s["type"] == "drop"], "sections": secs,
                 "reviewed": "right as found" if ok else "corrected", "added": datetime.date.today().isoformat()}
        if beat_shift and abs(beat_shift) < 2.0:
            entry["beat_shift"] = round(float(beat_shift), 4)
        old = self.truth().get(track_id) or {}
        if ok and not beat_shift and old.get("sections") == secs:
            return old                                    # reviewed before and left as it was: kept as it was ("corrected" stays)
        if self.read_only:
            return entry
        if beat_shift and abs(beat_shift) < 2.0:
            self.library.set_beat_fix(track_id, float(self.library.beat_fixes().get(track_id) or 0.0) + float(beat_shift))
        with self.lock:
            truth = self.truth()
            truth.setdefault("_about", "Sections the user set by ear (review mode in the studio): drops are what "
                                       "score_sections.py scores; sections are the whole song.")
            truth[track_id] = entry
            tmp = self.truth_path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(truth, fh, indent=1)
            os.replace(tmp, self.truth_path)
        return entry

    # -- routes ------------------------------------------------------------------------------
    def routes(self) -> list:
        return [
            web.get("/api/review/songs", self.h_songs),
            web.get("/api/review/song/{id}", self.h_song),
            web.get("/api/review/audio/{id}", self.h_audio),
            web.post("/api/review/truth", self.h_truth),
        ]

    async def h_songs(self, request):
        songs = await asyncio.get_running_loop().run_in_executor(None, self.songs)
        return web.json_response({"songs": songs})

    async def h_song(self, request):
        doc = await asyncio.get_running_loop().run_in_executor(None, self.song, request.match_info["id"])
        return web.json_response(doc) if doc else web.json_response({"error": "That song is not learned, or its copy is gone."}, status=404)

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
