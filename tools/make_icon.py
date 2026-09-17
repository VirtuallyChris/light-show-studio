r"""Draw the studio icon and write every size the app needs.

No image library required: the PNGs are written by hand with zlib. Run from the
project folder:  .venv\Scripts\python tools\make_icon.py

Outputs the launcher icon (Light Show Studio.ico) and the web icons the studio
page serves, which is where the Chrome app window gets its title bar and
taskbar icon from.
"""
import colorsys
import math
import os
import struct
import zlib

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
STATIC = os.path.join(ROOT, "lightshow", "studio", "static")


def png(size, pixel):
    raw = bytearray()
    for y in range(size):
        raw.append(0)
        for x in range(size):
            raw += bytes(pixel(x, y))

    def chunk(tag, data):
        c = tag + data
        return struct.pack(">I", len(data)) + c + struct.pack(">I", zlib.crc32(c) & 0xffffffff)

    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", size, size, 8, 6, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(bytes(raw), 9)) + chunk(b"IEND", b""))


def draw(size):
    """A dark rounded tile with a strip of eight lit LEDs across the middle."""
    r = size * 0.22
    cells = 8 if size >= 32 else 5          # fewer, fatter cells stay readable when tiny

    def inside(x, y):
        px, py = x + 0.5, y + 0.5
        dx = max(abs(px - size / 2) - (size / 2 - r), 0)
        dy = max(abs(py - size / 2) - (size / 2 - r), 0)
        return max(0.0, min(1.0, r - math.hypot(dx, dy) + 0.5))

    def pixel(x, y):
        a = inside(x, y)
        if a <= 0:
            return (0, 0, 0, 0)
        u, v = (x + 0.5) / size, (y + 0.5) / size
        col = (24, 26, 34)
        if 0.38 <= v <= 0.62 and 0.10 <= u <= 0.90:
            cell = (u - 0.10) / 0.80 * cells
            k, f = int(cell), cell - int(cell)
            gap = 0.10 if size >= 32 else 0.14
            if gap < f < 1 - gap:
                h = (k / cells + 0.02) % 1
                rr, gg, bb = colorsys.hsv_to_rgb(h, 0.95, 1)
                edge = min(1, (0.62 - v) / 0.04, (v - 0.38) / 0.04)
                col = tuple(int(c * 255 * (0.4 + 0.6 * edge)) for c in (rr, gg, bb))
        elif 0.24 <= v <= 0.76 and 0.10 <= u <= 0.90:
            d = min(abs(v - 0.38), abs(v - 0.62)) / 0.14
            h = ((u - 0.10) / 0.80 + 0.02) % 1
            rr, gg, bb = colorsys.hsv_to_rgb(h, 0.9, 1)
            g = max(0, 1 - d) ** 2 * 0.35
            col = tuple(int(c0 + (c * 255 - c0) * g) for c0, c in zip(col, (rr, gg, bb)))
        return (*col, int(255 * a))

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
    os.makedirs(STATIC, exist_ok=True)
    open(os.path.join(ROOT, "Light Show Studio.ico"), "wb").write(ico([256, 48, 32, 16]))
    open(os.path.join(STATIC, "icon.ico"), "wb").write(ico([256, 48, 32, 16]))
    for s in (192, 512):
        open(os.path.join(STATIC, f"icon-{s}.png"), "wb").write(draw(s))
    print("icons written")
