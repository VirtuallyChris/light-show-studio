"""The house as a map: where every light's segments are on the floor plan, so a show can play across the rooms
as one space (thunder rolling from wall to wall, spirals around the middle, one room against another).

Mapped on the party app's map page (static/party/map.html) over the floor plan drawn as shapes, flat (2D), in
feet: x across the plan from its left wall, y down it from its top wall. The map lives in shows/_space/space.json,
never in the repository:

    {"version": 1, "plan": {"width_ft": 41, "depth_ft": 14.2, "shapes": {rooms, walls, soffits, fixtures}},
     "center": [x, y],
     "lights": {
       "<light key>": {"kind": "strip", "anchors": [{"seg": 0, "x": .., "y": ..}, ...]},
       "<light key>": {"kind": "pole", "x": .., "y": ..},
       "<light key>": {"kind": "tubes", "tubes": [{"x": .., "y": ..}, ...]}}}

A strip's segments run between its anchors (each pinned to the segment found there with the locator), so it
can turn corners, cross several walls or hang in the middle of a room. Segment numbers are the light's own
(as sent to it: a flipped light's map is not flipped). The locator blinks one segment (or one tube) on the
real light while it is being mapped, and test patterns play the map on the lights themselves (a band sweeping
wall to wall, a beam turning around the middle, everything near a spot tapped on the plan), so the map can be seen
to be right; the lights are handed back as they were afterwards.
"""

from __future__ import annotations

import asyncio
import json
import os
import threading
import time

from aiohttp import web

PLAN_TYPES = {"image/png": ".png", "image/jpeg": ".jpg", "image/webp": ".webp"}
KINDS = ("strip", "pole", "tubes")


def _num(v, lo=-1000.0, hi=1000.0) -> float:
    x = float(v)
    if not lo <= x <= hi:
        raise ValueError(f"{x} is out of range")
    return round(x, 3)


def _shapes(sh: dict) -> dict:
    """The floor plan drawn as shapes, in feet: rooms (named outlines), walls (lines through points), drop-ceiling
    edges (soffits) and fixtures (an island, a couch: a rectangle [x, y, width, depth] or an outline), checked."""
    pt = lambda q: [_num(q[0]), _num(q[1])]
    rooms = [{"name": str(r.get("name") or "")[:40], "poly": [pt(q) for q in r["poly"]], **({"label": pt(r["label"])} if r.get("label") else {})}
             for r in sh.get("rooms") or []]
    fixtures = []
    for f in sh.get("fixtures") or []:
        if "rect" in f:
            fixtures.append({"name": str(f.get("name") or "")[:40], "rect": [_num(v) for v in f["rect"][:4]]})
        else:
            fixtures.append({"name": str(f.get("name") or "")[:40], "poly": [pt(q) for q in f["poly"]]})
    walls = [[pt(q) for q in w] for w in sh.get("walls") or []]       # the real walls (an open plan has few between rooms)
    # drop-ceiling edges: drawn solid like walls (open below, but lights are mounted on their face)
    soffits = [{"line": [pt(q) for q in f["line"]]} for f in sh.get("soffits") or []]
    return {"rooms": rooms, "fixtures": fixtures, "walls": walls, "soffits": soffits}


def clean(doc: dict) -> dict:
    """A map as sent by the page, checked and tidied (ValueError if it is not one)."""
    plan = doc.get("plan") or {}
    out = {"version": 1, "plan": {"image": str(plan.get("image") or ""), "width_ft": _num(plan.get("width_ft") or 30, 1, 1000),
                                  **({"depth_ft": _num(plan["depth_ft"], 1, 1000)} if plan.get("depth_ft") else {}),
                                  **({"shapes": _shapes(plan["shapes"])} if plan.get("shapes") else {})},
           "lights": {}}
    if doc.get("center") is not None:
        c = doc["center"]
        out["center"] = [_num(c[0]), _num(c[1])]
    for key, m in (doc.get("lights") or {}).items():
        kind = m.get("kind")
        if kind not in KINDS:
            raise ValueError(f"unknown kind {kind!r}")
        if kind == "strip":
            anchors = sorted(({"seg": int(a["seg"]), "x": _num(a["x"]), "y": _num(a["y"])}
                              for a in m.get("anchors") or []), key=lambda a: a["seg"])
            out["lights"][str(key)] = {"kind": kind, "anchors": anchors}
        elif kind == "pole":
            out["lights"][str(key)] = {"kind": kind, "x": _num(m["x"]), "y": _num(m["y"])}
        else:
            out["lights"][str(key)] = {"kind": kind, "tubes": [
                None if t is None else {"x": _num(t["x"]), "y": _num(t["y"])}
                for t in m.get("tubes") or []]}
    return out


def fill_tubes(tubes: list, count: int = 20) -> list:
    """The tubes strung like a strip: one not placed is on the line between the placed ones either side of it (by
    its number), or beyond the end ones in their line; with fewer than two placed, the others are nowhere yet.
    (The user places a few, 1, 5, 6, 11, 20, along the kitchen and off down the hall.)"""
    t = list(tubes[:count]) + [None] * max(0, count - len(tubes))
    at = [i for i, v in enumerate(t) if v]
    if len(at) < 2:
        return t
    out = []
    for i, v in enumerate(t):
        if v:
            out.append(v)
            continue
        before, after = [j for j in at if j < i], [j for j in at if j > i]
        a, b = (before[-1], after[0]) if before and after else (at[0], at[1]) if after else (at[-2], at[-1])
        k = (i - a) / (b - a)
        out.append({"x": round(t[a]["x"] + (t[b]["x"] - t[a]["x"]) * k, 2), "y": round(t[a]["y"] + (t[b]["y"] - t[a]["y"]) * k, 2), "filled": True})
    return out


def positions(m: dict, segments: int, tube_count: int = 20) -> list | None:
    """Where each of a light's segments is on the plan ([x, y] in feet, by the light's own segment number), from its
    map; None where it cannot be worked out (a strip with fewer than two pins, a tube not placed). A pole is one spot
    (all its segments there); a tube's LEDs are all at its spot."""
    n = max(1, int(segments))
    if m.get("kind") == "pole":
        return None if m.get("x") is None else [[m["x"], m["y"]] for _ in range(n)]
    if m.get("kind") == "tubes":
        per = max(1, round(n / tube_count))
        tubes = fill_tubes(m.get("tubes") or [], tube_count)
        return [None if s // per >= len(tubes) or tubes[s // per] is None else [tubes[s // per]["x"], tubes[s // per]["y"]] for s in range(n)]
    a = m.get("anchors") or []
    if len(a) < 2:
        return None
    out = []
    for s in range(n):
        j = 0                                            # the pair of pins around it (the end pair beyond them)
        while j < len(a) - 2 and s > a[j + 1]["seg"]:
            j += 1
        p, q = a[j], a[j + 1]
        k = (s - p["seg"]) / max(1e-9, q["seg"] - p["seg"])
        out.append([round(p[c] + (q[c] - p[c]) * k, 3) for c in ("x", "y")])
    return out


class Driver:
    """Drives some real lights from the map page, only while it asks: one segment blinking (the locator) or a test
    pattern played across the mapped house (to see on the lights themselves that the map is where they are).
    The lights are handed back as they were when it stops, or after a minute and a half without a word."""

    IDLE_S = 90.0
    FPS = 30.0

    def __init__(self, make_output, show_running):
        self.make_output, self.show_running = make_output, show_running
        self.lock = threading.Lock()
        self.out = None
        self.ips: list = []
        self.render = None                               # (seconds since it began) -> {ip: [r, g, b, ...]}
        self.t0 = self.last = 0.0
        self.thread = None

    def play(self, ips: list, known: list, render) -> None:
        if self.show_running():
            raise RuntimeError("a show is playing: stop it to try the map on the lights")
        if not ips:
            raise ValueError("none of these lights is on the network (or none is mapped)")
        with self.lock:
            if self.out is not None and set(self.ips) != set(ips):
                self._stop_locked()
            if self.out is None:
                self.out = self.make_output()
                self.out.select(list(ips), known)
                self.out.start()
                self.ips = list(ips)
            self.render, self.t0, self.last = render, time.monotonic(), time.monotonic()
        if self.thread is None or not self.thread.is_alive():
            self.thread = threading.Thread(target=self._run, daemon=True)
            self.thread.start()

    def stop(self) -> None:
        with self.lock:
            self._stop_locked()

    def _stop_locked(self) -> None:
        if self.out is not None:
            try:
                self.out.stop()
            finally:
                self.out, self.ips, self.render = None, [], None

    def _run(self) -> None:
        while True:
            with self.lock:
                if self.out is None:
                    return
                if time.monotonic() - self.last > self.IDLE_S:
                    self._stop_locked()
                    return
                frames = self.render(time.monotonic() - self.t0)
                ips = [ip for ip in self.ips if ip in frames]
                self.out.frame([frames[ip] for ip in ips], ips)
            time.sleep(1 / self.FPS)


def blink_frame(t: float, first: int, count: int, n: int) -> list:
    """One segment (or a few) white twice a second, the rest of the light dim so its length shows."""
    on = int(t * 2) % 2 == 0
    flat = []
    for s in range(n):
        flat.extend((255, 255, 255) if on and first <= s < first + count else (12, 12, 12))
    return flat


def pattern_level(kind: str, p: list, t: float, doc: dict, point: list | None) -> float:
    """How lit a segment at p ([x, y] feet) is at t seconds into a test pattern, 0..1."""
    import math
    w = float((doc.get("plan") or {}).get("width_ft") or 41)
    d = float((doc.get("plan") or {}).get("depth_ft") or w / 2.5)
    c = doc.get("center") or [w / 2, d / 2]
    if kind == "near":                                   # everything within 3 feet of the spot tapped
        return 1.0 if point and math.hypot(p[0] - point[0], p[1] - point[1]) <= 3.0 else 0.0
    if kind == "sweep_x":                                # a band crossing the house, left wall to right, 4 s
        x = (t % 4.0) / 4.0 * (w + 6) - 3
        return math.exp(-((p[0] - x) / 1.5) ** 2)
    if kind == "sweep_y":                                # front to back, 3 s
        y = (t % 3.0) / 3.0 * (d + 6) - 3
        return math.exp(-((p[1] - y) / 1.5) ** 2)
    if kind == "spin":                                   # a beam turning around the middle, once every 3 s
        a = math.atan2(p[1] - c[1], p[0] - c[0])
        beam = (t / 3.0) * 2 * math.pi
        diff = abs((a - beam + math.pi) % (2 * math.pi) - math.pi)
        return max(0.0, 1 - diff / math.radians(25))
    if kind == "ripple":                                 # a ring spreading out from the spot tapped (or the middle)
        o = point or c
        r = (t % 2.5) / 2.5 * max(w, d)
        return math.exp(-((math.hypot(p[0] - o[0], p[1] - o[1]) - r) / 1.5) ** 2)
    return 0.0


PATTERNS = ("near", "sweep_x", "sweep_y", "spin", "ripple")


class Space:
    def __init__(self, shows_dir: str, make_output, show_running, known, segments_of, read_only: bool = False):
        self.dir = os.path.join(shows_dir, "_space")
        self.read_only = read_only                       # a test copy of the studio: never writes the map
        self.known = known                               # () -> the lights found on the network
        self.segments_of = segments_of                   # ip -> how many segments that light has
        self.driver = Driver(make_output, show_running)

    def _device(self, key: str):
        return next((d for d in self.known() if key in (d.mac, d.device_id, d.ip)), None)

    def _mapped(self, doc: dict) -> list:
        """[(ip, segments, positions)] for every mapped light found on the network."""
        out = []
        for key, m in (doc.get("lights") or {}).items():
            d = self._device(key)
            if d is None:
                continue
            n = self.segments_of(d.ip)
            pos = positions(m, n)
            if pos:
                out.append((d.ip, n, pos))
        return out

    def blink(self, key: str, first: int, count: int) -> None:
        d = self._device(key)
        if d is None:
            raise ValueError("that light is not on the network")
        n = self.segments_of(d.ip)
        self.driver.play([d.ip], self.known(), lambda t: {d.ip: blink_frame(t, first, count, n)})

    def preview(self, kind: str, point: list | None, doc: dict | None = None) -> None:
        """A test pattern on the lights, from the map as the page has it (saved or not yet), else as saved."""
        if kind not in PATTERNS:
            raise ValueError(f"unknown pattern {kind!r}")
        doc = clean(doc) if doc else self.load()
        lights = self._mapped(doc)

        def render(t):
            frames = {}
            for ip, n, pos in lights:
                flat = []
                for p in pos:
                    if p is None:                        # not placed: off
                        flat.extend((0, 0, 0))
                        continue
                    v = pattern_level(kind, p, t, doc, point)
                    base = 10                            # every mapped segment faintly on, so the whole map shows
                    flat.extend((round(base + (255 - base) * v), round(base + (255 - base) * v), round(base + (255 - base) * v)))
                frames[ip] = flat
            return frames
        self.driver.play([ip for ip, _, _ in lights], self.known(), render)

    @property
    def path(self) -> str:
        return os.path.join(self.dir, "space.json")

    def load(self) -> dict:
        try:
            with open(self.path, encoding="utf-8") as fh:
                return json.load(fh)
        except (OSError, ValueError):
            plan = next((f for f in ("plan.png", "plan.jpg", "plan.webp") if os.path.exists(os.path.join(self.dir, f))), "")
            return {"version": 1, "plan": {"image": plan, "width_ft": 30, "depth_ft": 20}, "lights": {}}

    def save(self, doc: dict) -> dict:
        doc = clean(doc)
        if self.read_only:
            return doc
        os.makedirs(self.dir, exist_ok=True)
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(doc, fh, indent=1)
        os.replace(tmp, self.path)
        return doc

    def save_plan(self, data: bytes, ctype: str) -> str:
        ext = PLAN_TYPES.get(ctype.split(";")[0].strip().lower())
        if not ext:
            raise ValueError("a floor plan is a PNG, JPEG or WebP image")
        if len(data) > 20 * 1024 * 1024:
            raise ValueError("that image is too big (20 MB at most)")
        name = "plan" + ext
        if not self.read_only:
            os.makedirs(self.dir, exist_ok=True)
            with open(os.path.join(self.dir, name), "wb") as fh:
                fh.write(data)
        return name

    # -- routes ------------------------------------------------------------------------------
    def routes(self) -> list:
        return [
            web.get("/party/map", self.h_page),
            web.get("/api/space", self.h_get),
            web.post("/api/space", self.h_set),
            web.get("/api/space/plan", self.h_plan),
            web.post("/api/space/plan", self.h_plan_set),
            web.post("/api/space/locate", self.h_locate),
            web.post("/api/space/preview", self.h_preview),
            web.post("/api/space/stop", self.h_stop),
        ]

    async def h_page(self, request):
        # (never cached, like the Party Hub's page: a phone kept the map from before an update)
        return web.FileResponse(os.path.join(os.path.dirname(__file__), "static", "party", "map.html"), headers={"Cache-Control": "no-store"})

    async def h_get(self, request):
        return web.json_response(await asyncio.get_running_loop().run_in_executor(None, self.load))

    async def h_set(self, request):
        try:
            doc = await asyncio.get_running_loop().run_in_executor(None, self.save, await request.json())
        except (KeyError, TypeError, ValueError, IndexError) as e:
            return web.json_response({"error": f"the map was not saved: {e}"}, status=400)
        return web.json_response({"ok": True, "space": doc, "read_only": self.read_only})

    async def h_plan(self, request):
        name = (self.load().get("plan") or {}).get("image") or ""
        path = os.path.join(self.dir, os.path.basename(name))
        if not name or not os.path.exists(path):
            return web.json_response({"error": "no floor plan yet"}, status=404)
        return web.FileResponse(path, headers={"Cache-Control": "no-cache"})

    async def h_plan_set(self, request):
        try:
            name = self.save_plan(await request.read(), request.headers.get("Content-Type", ""))
        except ValueError as e:
            return web.json_response({"error": str(e)}, status=400)
        doc = self.load()
        doc.setdefault("plan", {})["image"] = name
        if not self.read_only:
            self.save(doc)
        return web.json_response({"ok": True, "image": name})

    async def _drive(self, fn, *args):
        try:
            await asyncio.get_running_loop().run_in_executor(None, fn, *args)
        except RuntimeError as e:
            return web.json_response({"error": str(e)}, status=409)
        except (KeyError, TypeError, ValueError) as e:
            return web.json_response({"error": str(e)}, status=400)
        return web.json_response({"ok": True})

    async def h_locate(self, request):
        body = await request.json()
        return await self._drive(self.blink, str(body.get("key", "")), int(body.get("first", 0)), int(body.get("count", 1)))

    async def h_preview(self, request):
        body = await request.json()
        pt = body.get("point")
        return await self._drive(self.preview, str(body.get("pattern", "")), [float(pt[0]), float(pt[1])] if pt else None, body.get("space"))

    async def h_stop(self, request):
        await asyncio.get_running_loop().run_in_executor(None, self.driver.stop)
        return web.json_response({"ok": True})
