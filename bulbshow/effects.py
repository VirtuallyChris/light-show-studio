"""What the bulb shows at each moment of a show.

A show is a sound and a few layers of clips. Each clip is one of three effects:

  color      a steady color and brightness, with an optional fade in and out
  pulse      breathes between two brightness levels, one breath every `period` seconds
  lightning  a few hard flashes spread across the clip, dark in between

Layers stack bottom to top. Color and pulse cover what is below them; lightning
covers it only while a flash is lit, so lightning over a dim color clip flashes
out of a glow instead of out of the dark. Where no clip plays, the bulb is dark.

  scene      one of the bulb's own effects (a scene from the Govee app), run by the bulb itself

A scene is not drawn here at all: the bulb is told once to start it and plays it on its own,
however long the clip lasts, which costs a single cloud command. A color or flash on a higher
layer interrupts it, and the scene is started again when that ends: one way to keep a bulb
pulsing all evening and send it red now and then for two commands each time.

There are two ways of drawing the same show. A bulb on the LAN takes a new color
twenty times a second, so it gets the smooth version. A bulb driven through
Govee's cloud takes at most two commands a second (a burst of six) and ten
thousand a day for the whole account, so it gets a stepped version: a pulse
jumps between its high and low, a fade moves in half-second steps, and a flash
stays lit long enough to be seen. The editor previews whichever the show's bulb
will get, and counts what the cloud version costs.
"""

from __future__ import annotations

import json
import math
import random
import zlib

FPS = 20                      # frames a second sent to a LAN bulb (Govee drops packets above ~40/s)

CLOUD_RATE = 2.0              # Govee cloud control: per device, 2 commands a second, bursts of 6
CLOUD_BURST = 6
CLOUD_DAILY = 10_000          # per account, every show together
CLOUD_STEP = 0.5              # the shortest step the stepped version takes
CLOUD_FLASH = 0.3             # a flash through the cloud stays lit at least this long

EFFECTS = {
    "color": {"name": "Color", "defaults": {"color": "#7a2cff", "level": 0.8, "fade": 0.0}},
    "pulse": {"name": "Pulse", "defaults": {"color": "#ff6a00", "level": 1.0, "low": 0.1, "period": 2.0}},
    "lightning": {"name": "Lightning", "defaults": {"color": "#dfe8ff", "level": 1.0, "flashes": 3}},
    "scene": {"name": "Scene", "defaults": {"scene": None}},
}


def scene_state(scene: dict | None):
    """What the bulb is asked for during a scene clip: ("scene", instance, value as JSON, name). A clip
    with no scene picked yet is dark."""
    if not scene or scene.get("value") is None:
        return (0, 0, 0)
    return ("scene", scene.get("instance") or "lightScene", json.dumps(scene["value"], sort_keys=True), scene.get("name") or "")


def is_scene(st) -> bool:
    return len(st) == 4 and st[0] == "scene"


def code(st) -> str:
    """A state as the page gets it: #rrggbb, or s:<scene name>."""
    return f"s:{st[3]}" if is_scene(st) else rgb_hex(st)


def hex_rgb(h: str) -> tuple[int, int, int]:
    h = (h or "#000000").lstrip("#")
    if len(h) == 3:
        h = "".join(c * 2 for c in h)
    try:
        return int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)
    except ValueError:
        return 0, 0, 0


def rgb_hex(rgb) -> str:
    return "#%02x%02x%02x" % tuple(rgb)


def param(clip: dict, key: str):
    v = clip.get(key)
    return EFFECTS.get(clip.get("effect"), EFFECTS["color"])["defaults"].get(key) if v is None else v


def _num(clip, key, lo, hi):
    try:
        return max(lo, min(hi, float(param(clip, key))))
    except (TypeError, ValueError):
        return lo


def flashes(clip: dict) -> list[tuple[float, float, float]]:
    """The lightning clip's flashes as (start, length, peak), seconds from the clip's start.

    Placed the same way every time for the same clip (seeded by its id), spread across the clip
    with some jitter, and now and then a quick second strike right after a flash.
    """
    length = max(0.05, float(clip["end"]) - float(clip["start"]))
    n = int(_num(clip, "flashes", 1, 12))
    rng = random.Random(zlib.crc32(str(clip.get("id") or clip["start"]).encode()))
    span = length * 0.9
    out = []
    for i in range(n):
        s = 0.0 if i == 0 else span * i / n + rng.uniform(0, span / n * 0.6)
        d = rng.uniform(0.06, 0.14)
        peak = 1.0 if i == 0 else rng.uniform(0.55, 1.0)
        out.append((s, d, peak))
        if rng.random() < 0.4:
            out.append((s + d + rng.uniform(0.04, 0.09), rng.uniform(0.04, 0.08), peak * 0.7))
    return out


TAIL = 0.06                   # a LAN flash dies away over this long


def clip_light(clip: dict, tl: float, cloud: bool = False) -> tuple[tuple[int, int, int], float, float]:
    """(color, brightness 0..1, cover 0..1) of a clip `tl` seconds after it starts."""
    rgb = hex_rgb(param(clip, "color"))
    level = _num(clip, "level", 0, 1)
    length = float(clip["end"]) - float(clip["start"])
    kind = clip.get("effect")
    if kind == "pulse":
        low = _num(clip, "low", 0, 1)
        period = _num(clip, "period", 2 * CLOUD_STEP if cloud else 0.2, 120)
        if cloud:                                  # the nearest peak or trough: a square wave lined up with the breaths
            k = round(tl / (period / 2))
            return rgb, (low if k % 2 == 0 else level), 1.0
        return rgb, low + (level - low) * (0.5 - 0.5 * math.cos(2 * math.pi * tl / period)), 1.0
    if kind == "lightning":
        lit = 0.0
        for s, d, peak in flashes(clip):
            if cloud:
                if s <= tl < s + max(d, CLOUD_FLASH):
                    lit = 1.0
            elif s <= tl < s + d:
                lit = max(lit, peak)
            elif s + d <= tl < s + d + TAIL:
                lit = max(lit, peak * (1 - (tl - s - d) / TAIL))
        return rgb, level, lit
    fade = _num(clip, "fade", 0, 60)
    if fade > 0:
        if cloud:                                  # half-second steps, each at the level its middle would have
            tl = (math.floor(tl / CLOUD_STEP) + 0.5) * CLOUD_STEP
        level *= max(0.0, min(1.0, tl / fade, (length - tl) / fade))
    return rgb, level, 1.0


def state(show: dict, t: float, cloud: bool = False) -> tuple:
    """What the bulb shows at `t` seconds into the show: a color (brightness folded into it), or a
    scene (see scene_state) where a scene clip is on top."""
    out = (0.0, 0.0, 0.0)
    for layer in show.get("layers") or []:
        for clip in layer:
            s, e = float(clip["start"]), float(clip["end"])
            if s <= t < e:
                if clip.get("effect") == "scene":
                    out = scene_state(param(clip, "scene"))
                    break
                rgb, level, cover = clip_light(clip, t - s, cloud)
                if cover >= 1 or (cover > 0 and is_scene(out)):   # a color can't be mixed into a scene: it replaces it
                    out = tuple(c * level * cover for c in rgb)
                elif cover > 0:
                    out = tuple(c * level * cover + o * (1 - cover) for c, o in zip(rgb, out))
                break                              # clips never overlap within a layer
    if is_scene(out):
        return out
    return tuple(max(0, min(255, int(round(c)))) for c in out)


def render(show: dict, cloud: bool = False, fps: int = FPS) -> list[str]:
    """One loop of the show as colors, `fps` a second (the editor's preview)."""
    n = max(1, int(math.ceil(float(show.get("duration") or 0) * fps)))
    return [code(state(show, i / fps, cloud)) for i in range(n)]


class Bucket:
    """Govee's per-device allowance: tokens refill at `rate` a second up to `burst`."""

    def __init__(self, rate: float = CLOUD_RATE, burst: int = CLOUD_BURST, now: float = 0.0):
        self.rate, self.burst, self.tokens, self.at = rate, burst, float(burst), now

    def take(self, now: float) -> bool:
        self.tokens = min(self.burst, self.tokens + (now - self.at) * self.rate)
        self.at = now
        if self.tokens >= 1:
            self.tokens -= 1
            return True
        return False

    def wait(self, now: float) -> float:
        """Seconds until a token is free."""
        tokens = min(self.burst, self.tokens + (now - self.at) * self.rate)
        return 0.0 if tokens >= 1 else (1 - tokens) / self.rate


def cloud_requests(show: dict, fps: int = FPS) -> int:
    """Commands one loop costs through the cloud, once the show is looping (the second time round,
    so the jump from the end back to the start is counted), as the runner would send them."""
    dur = float(show.get("duration") or 0)
    if dur <= 0:
        return 0
    n = int(math.ceil(dur * fps))
    bucket, sent, pending, count = Bucket(), None, None, 0
    for loop in range(2):
        for i in range(n):
            now = loop * dur + i / fps
            want = state(show, i / fps, cloud=True)
            if want != sent:
                pending = want
            elif pending is not None:
                pending = None
            if pending is not None and bucket.take(now):
                sent, pending = pending, None
                count += loop
    return count


def per_hour(show: dict) -> int:
    dur = float(show.get("duration") or 0)
    return 0 if dur <= 0 else int(round(cloud_requests(show) * 3600 / dur))
