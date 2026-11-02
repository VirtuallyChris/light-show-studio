r"""Draw the Bulb Studio icon: a glowing bulb on the studio's dark rounded tile.

Run from the project folder:  .venv\Scripts\python tools\make_bulb_icon.py

Writes bulbshow\Bulb Studio.ico (the pinned taskbar button and the launcher) and the icons the
page serves, which the Chrome app window shows until it takes Bulb Studio's own identity.
"""
import math
import os
import struct
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from make_icon import png  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
APP = os.path.join(ROOT, "bulbshow")
STATIC = os.path.join(APP, "static")


def draw(size):
    r = size * 0.22
    cx, cy, rad = size * 0.5, size * 0.42, size * 0.25          # the glass
    accent, accent2 = (108, 140, 255), (255, 94, 168)

    def tile(px, py):
        dx = max(abs(px - size / 2) - (size / 2 - r), 0)
        dy = max(abs(py - size / 2) - (size / 2 - r), 0)
        return max(0.0, min(1.0, r - math.hypot(dx, dy) + 0.5))

    def pixel(x, y):
        px, py = x + 0.5, y + 0.5
        a = tile(px, py)
        if a <= 0:
            return (0, 0, 0, 0)
        col = [24, 26, 34]
        d = math.hypot(px - cx, py - cy)
        glow = max(0.0, 1 - d / (rad * 1.9)) ** 2 * 0.55          # light thrown around the bulb
        k = max(0.0, min(1.0, (px - (cx - rad)) / (2 * rad)))
        lit = [accent[i] + (accent2[i] - accent[i]) * k for i in range(3)]
        col = [c + (l - c) * glow for c, l in zip(col, lit)]
        glass = max(0.0, min(1.0, rad - d + 0.5))
        neck_w = rad * 0.55                                        # the glass narrowing into the base
        in_neck = cy <= py <= cy + rad * 1.25 and abs(px - cx) <= neck_w * (1 - (py - cy) / (rad * 3.2))
        if in_neck:
            glass = 1.0
        if glass > 0:
            shine = 1 - min(1, math.hypot(px - (cx - rad * 0.35), py - (cy - rad * 0.35)) / (rad * 0.9))
            fill = [min(255, l + 90 * shine) for l in lit]
            col = [c + (f - c) * glass for c, f in zip(col, fill)]
        by0, bw = cy + rad * 1.3, rad * 0.62                       # the screw base: two grey bands
        for i in range(2):
            y0 = by0 + i * rad * 0.34
            if y0 <= py <= y0 + rad * 0.24 and abs(px - cx) <= bw * (1 - 0.18 * i):
                col = [142, 149, 166]
        return (*[int(c) for c in col], int(255 * a))

    return png(size, pixel)


def ico(sizes):
    imgs = [draw(s) for s in sizes]
    out = struct.pack("<HHH", 0, 1, len(sizes))
    off = 6 + 16 * len(sizes)
    for s, im in zip(sizes, imgs):
        out += struct.pack("<BBBBHHII", s % 256, s % 256, 0, 0, 1, 32, len(im), off)
        off += len(im)
    return out + b"".join(imgs)


if __name__ == "__main__":
    data = ico([256, 48, 32, 16])
    open(os.path.join(APP, "Bulb Studio.ico"), "wb").write(data)
    open(os.path.join(STATIC, "icon.ico"), "wb").write(data)
    open(os.path.join(STATIC, "icon-192.png"), "wb").write(draw(192))
    print("icons written")
