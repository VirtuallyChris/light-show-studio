r"""Draw the studio logo and write every size the app needs.

The logo is two stage lamps whose beams cross in the air, the colors adding up
where they meet, over a glow on the floor. Run from the project folder:
.venv\Scripts\python tools\make_icon.py

Outputs the launcher icon (Light Show Studio.ico), the web icons the studio page
serves (where the Chrome app window gets its title bar and taskbar icon from),
the phone's icons, and logo.png, the mark alone for the app's own screens.
"""
import io
import os
import struct
import zlib

import numpy as np
from PIL import Image

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
STATIC = os.path.join(ROOT, "lightshow", "studio", "static")

MAGENTA, CYAN = (1, .10, .60), (.05, .72, 1)
#        lamp          aimed at      half width in degrees, color
BEAMS = [((.17, .17), (.66, .89), 10, MAGENTA),
         ((.83, .17), (.34, .89), 10, CYAN)]
TINY_BEAMS = [((.15, .15), (.70, .92), 13, MAGENTA),
              ((.85, .15), (.30, .92), 13, CYAN)]    # fatter, to still read at 16 pixels
FLOOR = (.5, .3, 1)                                  # where the beams land


def emission(work, beams, lamp, floor=False):
    """The light alone, as an RGB array that adds up: overlaps go past 1.0 and clip toward white."""
    yy, xx = np.mgrid[0:work, 0:work]
    x, y = (xx + .5) / work, (yy + .5) / work
    px = 1 / work
    light = np.zeros((work, work, 3))
    for (ax, ay), (tx, ty), half, color in beams:
        reach = np.hypot(tx - ax, ty - ay)
        ux, uy = (tx - ax) / reach, (ty - ay) / reach
        along = (x - ax) * ux + (y - ay) * uy
        across = np.abs((y - ay) * ux - (x - ax) * uy)
        width = np.maximum(along, 0) * np.tan(np.radians(half)) + lamp * .55
        side = np.clip((width - across) / px + .5, 0, 1) * (along > -lamp)
        t = np.clip(along / reach, 0, 1)
        fade = (1 - .72 * t ** 1.15) * np.clip((reach - along) / (reach * .10), 0, 1)   # thins out like a beam through haze
        light += (side * fade)[..., None] * np.array(color)
    if floor:
        light += (np.exp(-((y - .86) / .035) ** 2) * np.exp(-((x - .5) / .30) ** 2))[..., None] * np.array(FLOOR) * .9
    for (ax, ay), _, _, color in beams:
        d = np.hypot(x - ax, y - ay)
        light += np.exp(-(d / (lamp * 1.15)) ** 2)[..., None] * 1.6                     # the lamp, white hot
        light += np.exp(-(d / (lamp * 3)) ** 2)[..., None] * np.array(color) * .5       # and its halo
    return light


def rounded(work, radius):
    """A rounded square's coverage; radius 0 fills the whole picture."""
    if not radius:
        return np.ones((work, work))
    yy, xx = np.mgrid[0:work, 0:work]
    c, r = work / 2, radius * work
    dx = np.maximum(np.abs(xx + .5 - c) - (c - r), 0)
    dy = np.maximum(np.abs(yy + .5 - c) - (c - r), 0)
    return np.clip(r - np.hypot(dx, dy) + .5, 0, 1)


def picture(rgb, alpha):
    return Image.fromarray((np.dstack([np.clip(rgb, 0, 1), alpha]) * 255 + .5).astype("uint8"), "RGBA")


def draw(size, radius=.225):
    """The logo on its dark tile, size pixels square."""
    tiny = size <= 32
    work = size * (8 if tiny else 4)
    y = ((np.arange(work) + .5) / work)[:, None, None]
    sky = (1 - y) * np.array([.085, .045, .20]) + y * np.array([.022, .016, .055])     # a dark room, violet up by the rig
    light = emission(work, TINY_BEAMS if tiny else BEAMS, .05 if tiny else .03, floor=not tiny)
    return picture(sky + light, rounded(work, radius)).resize((size, size), Image.LANCZOS)


def draw_mark(size):
    """The beams alone on nothing, for the app's own dark screens."""
    work = size * 4
    shown = np.clip(emission(work, BEAMS, .03), 0, 1)
    alpha = shown.max(axis=2)
    rgb = np.divide(shown, alpha[..., None], out=np.zeros_like(shown), where=alpha[..., None] > 0)
    img = picture(rgb, alpha)
    img = img.crop(img.getchannel("A").point(lambda a: 255 if a > 10 else 0).getbbox())   # to the mark, so it sits where it is put
    return img.resize((size, round(size * img.height / img.width)), Image.LANCZOS)


def png(size, pixel):
    """A PNG from a function of (x, y) giving RGBA. The Bulb Show icon draws with this."""
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


def encode(img):
    out = io.BytesIO()
    img.save(out, "PNG", optimize=True)
    return out.getvalue()


def ico(sizes):
    imgs = [encode(draw(s)) for s in sizes]
    out = struct.pack("<HHH", 0, 1, len(sizes))
    off = 6 + 16 * len(sizes)
    for s, im in zip(sizes, imgs):
        out += struct.pack("<BBBBHHII", s % 256, s % 256, 0, 0, 1, 32, len(im), off)
        off += len(im)
    return out + b"".join(imgs)


if __name__ == "__main__":
    pack = ico([256, 48, 32, 16])
    open(os.path.join(ROOT, "Light Show Studio.ico"), "wb").write(pack)
    open(os.path.join(STATIC, "icon.ico"), "wb").write(pack)
    for s in (192, 512):
        open(os.path.join(STATIC, f"icon-{s}.png"), "wb").write(encode(draw(s)))
        open(os.path.join(STATIC, "party", f"icon-{s}.png"), "wb").write(encode(draw(s, radius=0)))   # a phone rounds its own
    open(os.path.join(STATIC, "party", "apple-touch-icon.png"), "wb").write(encode(draw(180, radius=0).convert("RGB")))
    open(os.path.join(STATIC, "logo.png"), "wb").write(encode(draw_mark(192)))
    print("icons written")
