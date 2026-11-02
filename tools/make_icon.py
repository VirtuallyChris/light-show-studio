r"""Draw the studio logo and write every size the app needs.

The logo is a waveform, simplified to a few rounded bars in the colors of a show.
Run from the project folder:  .venv\Scripts\python tools\make_icon.py

Outputs the launcher icon (Light Show Studio.ico), the web icons the studio page
serves (where the Chrome app window gets its title bar and taskbar icon from),
the phone's icons, and logo.png, the mark alone for the app's own screens.
"""
import io
import os
import struct
import zlib

import numpy as np
from PIL import Image, ImageFilter

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
STATIC = os.path.join(ROOT, "lightshow", "studio", "static")

STOPS = [(0, (.13, .83, .93)), (.36, (.55, .36, .96)), (.68, (.93, .28, .60)), (1, (.98, .57, .24))]   # cyan, violet, pink, orange
BARS = [.16, .36, .62, .36, .72, .44, .20]    # each bar's height, as parts of the tile
TINY_BARS = [.30, .70, .46, .78, .34]         # five, the same shape, still read at 16 pixels


def waveform(work, bars, span, fill):
    """The bars' coverage (0 to 1) and their colors, running across the logo."""
    yy, xx = np.mgrid[0:work, 0:work]
    x, y = (xx + .5) / work, (yy + .5) / work
    pitch = span / len(bars)
    half = pitch * fill / 2
    cover = np.zeros((work, work))
    for i, height in enumerate(bars):
        cx = .5 - span / 2 + pitch * (i + .5)
        edge = np.hypot(x - cx, np.maximum(np.abs(y - .5) - max(height / 2 - half, 0), 0)) - half
        cover = np.maximum(cover, np.clip(.5 - edge * work, 0, 1))
    t = np.clip((x - (.5 - span / 2)) / span, 0, 1)
    color = np.dstack([np.interp(t, [q[0] for q in STOPS], [q[1][c] for q in STOPS]) for c in range(3)])
    return cover, color


def glow(cover, color, work, amount):
    """The bars' light, spread out around them."""
    lit = Image.fromarray((cover[..., None] * color * 255).astype("uint8"), "RGB")
    return np.asarray(lit.filter(ImageFilter.GaussianBlur(work * .035))) / 255 * amount


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
    cover, color = waveform(work, TINY_BARS if size <= 16 else BARS, .76 if tiny else .68, .68 if tiny else .62)
    y = ((np.arange(work) + .5) / work)[:, None, None]
    sky = (1 - y) * np.array([.075, .05, .16]) + y * np.array([.03, .025, .07])
    back = sky * np.ones((work, work, 3)) + (0 if tiny else glow(cover, color, work, .55))
    rgb = back * (1 - cover[..., None]) + color * cover[..., None]
    return picture(rgb, rounded(work, radius)).resize((size, size), Image.LANCZOS)


def draw_mark(size):
    """The bars alone on nothing, for the app's own dark screens."""
    work = size * 4
    cover, color = waveform(work, BARS, .84, .66)
    img = picture(color, cover)
    img = img.crop(img.getchannel("A").getbbox())     # to the mark, so it sits where it is put
    return img.resize((size, round(size * img.height / img.width)), Image.LANCZOS)


def png(size, pixel):
    """A PNG from a function of (x, y) giving RGBA. The Bulb Studio icon draws with this."""
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
