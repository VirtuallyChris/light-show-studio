"""Light show engine: turns audio Features into per-segment colours.

Every mode returns, for each device, a list of `n` RGB tuples where `n` is
that device's segment (LED zone) count. In real-time mode those go out via
the Govee "razer" protocol (instant, per segment). In classic mode they are
averaged to one colour and sent with the normal colour command, which the
light fades towards over a few hundred milliseconds.
"""

from __future__ import annotations

import colorsys
import math
import random
import time
from dataclasses import dataclass

import numpy as np

from .audio import Features
from .govee_lan import GoveeDevice
from .music import Structure, StructureTracker

RGB = tuple[int, int, int]

PALETTES = {
    "neon": [(255, 0, 128), (0, 255, 200), (140, 0, 255), (255, 220, 0), (0, 120, 255)],
    "fire": [(255, 30, 0), (255, 120, 0), (255, 200, 20), (200, 0, 0), (255, 70, 10)],
    "ocean": [(0, 40, 255), (0, 200, 255), (0, 255, 160), (60, 0, 200), (0, 120, 220)],
    "party": [(255, 0, 0), (0, 255, 0), (0, 0, 255), (255, 255, 0), (255, 0, 255), (0, 255, 255)],
}


def hsv(h: float, s: float, v: float) -> RGB:
    r, g, b = colorsys.hsv_to_rgb(h % 1.0, max(0.0, min(1.0, s)), max(0.0, min(1.0, v)))
    return int(r * 255), int(g * 255), int(b * 255)


def scale(rgb: RGB, k: float) -> RGB:
    k = max(0.0, min(1.0, k))
    return int(rgb[0] * k), int(rgb[1] * k), int(rgb[2] * k)


def mix(a: RGB, b: RGB, t: float) -> RGB:
    t = max(0.0, min(1.0, t))
    return tuple(int(x + (y - x) * t) for x, y in zip(a, b))  # type: ignore[return-value]


def average(colors: list[RGB]) -> RGB:
    if not colors:
        return (0, 0, 0)
    return tuple(int(sum(c[i] for c in colors) / len(colors)) for i in range(3))  # type: ignore[return-value]


def hsv_arr(h, s, v) -> np.ndarray:
    """Vectorised HSV -> RGB. h/s/v are floats or arrays broadcastable to (n,). Returns (n, 3) in 0..1."""
    h = np.asarray(h, dtype=float) % 1.0
    s = np.clip(np.asarray(s, dtype=float), 0, 1)
    v = np.clip(np.asarray(v, dtype=float), 0, 1)
    h, s, v = np.broadcast_arrays(h, s, v)
    i = np.floor(h * 6).astype(int) % 6
    f = h * 6 - np.floor(h * 6)
    p, q, t = v * (1 - s), v * (1 - s * f), v * (1 - s * (1 - f))
    r = np.choose(i, [v, q, p, p, t, v])
    g = np.choose(i, [t, v, v, q, p, p])
    b = np.choose(i, [p, p, t, v, v, q])
    return np.stack([r, g, b], axis=-1)


def resample(values: np.ndarray, n: int) -> np.ndarray:
    """Linearly resample a 1-D array to length n."""
    if n == len(values):
        return values
    x_old = np.linspace(0, 1, len(values))
    x_new = np.linspace(0, 1, n)
    return np.interp(x_new, x_old, values)


@dataclass
class Smoother:
    """Asymmetric exponential smoothing: fast attack, slower release."""
    attack: float = 0.6
    release: float = 0.15
    value: float = 0.0

    def __call__(self, target: float) -> float:
        a = self.attack if target > self.value else self.release
        self.value += (target - self.value) * a
        return self.value


@dataclass
class ShowConfig:
    mode: str = "spectrum"
    palette: str = "neon"
    fps: float = 60.0
    realtime: bool = True            # razer per-segment protocol vs classic colour command
    gradient: bool = False           # ask the light to blend colours between segments
    strobe: bool = True              # allow hard strobing in concert mode (buildups, heavy sections)
    brightness_floor: float = 0.06   # never fully dark while music plays
    beat_flash: float = 0.6          # how hard beats push brightness
    idle_color: RGB = (40, 20, 60)   # shown when no sound is detected
    idle_after: float = 3.0          # seconds of silence before idle colour
    reenable_gap: float = 5.0        # re-send razer enable only after this long without frames (the light
                                     # leaves real-time mode after ~60 s idle; re-enabling while streaming
                                     # blanks it for a frame)


class Show:
    def __init__(self, devices: list[GoveeDevice], config: ShowConfig, segments: list[int]):
        assert len(devices) == len(segments)
        self.devices = devices
        self.segments = [max(1, int(n)) for n in segments]
        self.cfg = config
        self.level = Smoother(0.7, 0.12)
        self.vu = Smoother(0.8, 0.25)
        self.vu_peak = 0.0
        self.band_smoothers = [Smoother(0.7, 0.18) for _ in devices]
        self.hue = random.random()
        self.flash = 0.0
        self.ripple = 2.0  # normalised radius of the last beat's ripple (>1 = gone)
        self.palette = PALETTES.get(config.palette, PALETTES["neon"])
        self.palette_idx = 0
        self._now = time.monotonic  # injectable clock (tests drive it in song time)
        self.last_sound = self._now()
        self.last_send = 0.0
        self.last_enable = 0.0
        self.last_frame = 0.0
        self.frames_sent = 0
        self._t = self._now()
        self._bins_smooth = None
        # concert mode state
        self.tracker = StructureTracker()
        self.st: Structure = self.tracker.st
        self.clock = 0.0
        self.mood_hue = 0.0
        self.rainbow_phase = 0.0
        self.beat_count = 0
        self.drop_count = 0
        self.drop_fx = "comets"          # effect used after the current drop: "comets" or "rainbow"
        self.comet_pos = 0.0             # comet travel, in strip lengths
        self.comet_colors = self.COMET_PAIRS[0]
        self._prev = [np.zeros((n, 3)) for n in self.segments]
        self._sparkle = [np.zeros(n) for n in self.segments]
        self._rng = np.random.default_rng()

    # -- lifecycle ----------------------------------------------------------
    def start(self) -> None:
        # Lights on wifi power-save can miss the first packets after idling, so
        # repeat the wake-up commands a few times before the frames start.
        for _ in range(3):
            for d in self.devices:
                d.turn(True)
                d.brightness(100)
            time.sleep(0.35)
        if self.cfg.realtime:
            for _ in range(3):
                for d in self.devices:
                    d.razer(True)
                time.sleep(0.2)
            self.last_enable = self._now()

    def stop(self, off: bool = False, exit_color: RGB | None = None) -> None:
        if self.cfg.realtime:
            for d in self.devices:
                d.razer(False)
            time.sleep(0.15)
        for d in self.devices:
            if off:
                d.turn(False)
            elif exit_color is not None:
                d.color(*exit_color)

    # -- per frame ----------------------------------------------------------
    def update(self, f: Features) -> list[list[RGB]]:
        """Compute and (rate-limited) send colours. Returns per-device segment colours."""
        now = self._now()
        dt, self._t = now - self._t, now
        if not f.silent:
            self.last_sound = now

        self.flash = max(0.0, self.flash - dt * 4.0)   # decays in ~0.25 s
        self.ripple = min(2.0, self.ripple + dt * 3.0)  # crosses a light in ~0.33 s
        if f.beat:
            self.flash = 1.0
            self.ripple = 0.0
            self.palette_idx = (self.palette_idx + 1) % len(self.palette)

        self.clock += dt
        self.st = self.tracker.update(f, dt)
        if f.beat:
            self.beat_count += 1
        self._update_mood(f, dt)

        if f.bins is not None:
            if self._bins_smooth is None:
                self._bins_smooth = f.bins.copy()
            else:
                up = f.bins > self._bins_smooth
                self._bins_smooth += (f.bins - self._bins_smooth) * np.where(up, 0.7, 0.2)

        if now - self.last_sound > self.cfg.idle_after:
            frames = [[self.cfg.idle_color] * n for n in self.segments]
        else:
            fn = getattr(self, f"_mode_{self.cfg.mode}", self._mode_spectrum)
            frames = fn(f, dt)

        if now - self.last_send >= 1.0 / self.cfg.fps:
            self.last_send = now
            self._send(frames, now)
        return frames

    def _send(self, frames: list[list[RGB]], now: float) -> None:
        if self.cfg.realtime:
            if self.last_frame and now - self.last_frame > self.cfg.reenable_gap:
                for d in self.devices:
                    d.razer(True)
            self.last_frame = now
            for dev, segs in zip(self.devices, frames):
                dev.razer_leds(segs, gradient=self.cfg.gradient)
                self.frames_sent += 1
        else:
            for dev, segs in zip(self.devices, frames):
                if dev.color_if_changed(*average(segs)):
                    self.frames_sent += 1

    # -- helpers ------------------------------------------------------------
    def brightness(self, f: Features) -> float:
        lvl = self.level(f.level)
        b = self.cfg.brightness_floor + (1 - self.cfg.brightness_floor) * lvl
        return min(1.0, b + self.cfg.beat_flash * self.flash * 0.5)

    def _ripple_gain(self, k: int, n: int) -> float:
        """Brightness boost for segment k from a ring expanding out of the centre."""
        if n < 3 or self.ripple >= 1.0:
            return 0.0
        pos = abs((k + 0.5) / n - 0.5) * 2  # 0 at centre, 1 at ends
        return max(0.0, 1.0 - abs(pos - self.ripple) * 4) * (1 - self.ripple)

    # -- modes --------------------------------------------------------------
    def _mode_spectrum(self, f: Features, dt: float) -> list[list[RGB]]:
        """Hue follows the spectral centroid (bass = red .. treble = violet) with a
        gentle gradient along the light; brightness follows loudness; beats
        send a bright ripple out from the centre."""
        target_hue = 0.78 * f.centroid
        self.hue += (target_hue - self.hue) * 0.25
        b = self.brightness(f)
        out = []
        for n in self.segments:
            segs = []
            for k in range(n):
                h = self.hue + ((k + 0.5) / n - 0.5) * 0.12
                sat = 1.0 - 0.6 * max(self.flash * 0.5, self._ripple_gain(k, n))
                segs.append(scale(hsv(h, sat, 1.0), min(1.0, b + 0.5 * self._ripple_gain(k, n))))
            out.append(segs)
        return out

    def _mode_pulse(self, f: Features, dt: float) -> list[list[RGB]]:
        """Palette colour jumps on every beat and a ripple runs along the light;
        brightness pulses with the beat."""
        color = self.palette[self.palette_idx]
        base = self.cfg.brightness_floor + (1 - self.cfg.brightness_floor) * (0.35 * self.level(f.level) + 0.65 * self.flash)
        return [[scale(mix(color, (255, 255, 255), 0.5 * self._ripple_gain(k, n)),
                       min(1.0, base + 0.6 * self._ripple_gain(k, n))) for k in range(n)]
                for n in self.segments]

    def _mode_eq(self, f: Features, dt: float) -> list[list[RGB]]:
        """Spectrum analyser: each segment is one frequency band, bass at one
        end, treble at the other. Colour runs red (bass) to blue (treble)."""
        bins = self._bins_smooth if self._bins_smooth is not None else np.zeros(24)
        out = []
        for n in self.segments:
            e = resample(bins, n)
            segs = []
            for k in range(n):
                h = 0.0 + 0.66 * (k / max(1, n - 1))
                v = self.cfg.brightness_floor + (1 - self.cfg.brightness_floor) * float(e[k]) ** 1.3
                segs.append(hsv(h, 1.0 - 0.3 * float(e[k]), v))
            out.append(segs)
        return out

    def _mode_vu(self, f: Features, dt: float) -> list[list[RGB]]:
        """VU meter: loudness fills the light from one end, green through
        yellow to red, with a falling peak marker. Beats flash the filled part."""
        lvl = self.vu(f.level)
        self.vu_peak = max(lvl, self.vu_peak - dt * 0.8)
        out = []
        for n in self.segments:
            segs = []
            filled = lvl * n
            peak_k = min(n - 1, int(self.vu_peak * n))
            for k in range(n):
                frac = (k + 0.5) / n
                h = 0.33 * (1 - frac)  # green -> red
                if k < filled:
                    v = min(1.0, 0.8 + 0.2 * self.flash)
                elif k == peak_k and self.vu_peak > 0.05:
                    v = 0.7
                else:
                    v = self.cfg.brightness_floor * 0.5
                segs.append(hsv(h, 1.0 - 0.4 * self.flash * (k < filled), v))
            out.append(segs)
        return out

    def _mode_bands(self, f: Features, dt: float) -> list[list[RGB]]:
        """Each light takes a whole frequency band (bass, mid, treble,
        repeating). Good for several lights around a room."""
        bands = [(f.bass, (255, 20, 0)), (f.mid, (0, 255, 60)), (f.treble, (40, 80, 255))]
        out = []
        for i, (sm, n) in enumerate(zip(self.band_smoothers, self.segments)):
            energy, color = bands[i % 3]
            b = self.cfg.brightness_floor + (1 - self.cfg.brightness_floor) * sm(energy)
            out.append([scale(color, b)] * n)
        return out

    def _mode_rainbow(self, f: Features, dt: float) -> list[list[RGB]]:
        """A rainbow runs along the light; the music's energy sets its speed
        and brightness. Multiple lights are offset around the colour wheel."""
        self.hue = (self.hue + dt * (0.05 + 0.6 * self.level(f.level))) % 1.0
        b = self.brightness(f)
        m = max(1, len(self.devices))
        return [[scale(hsv(self.hue + i / m + k / n * 0.5, 1.0, 1.0), b) for k in range(n)]
                for i, n in enumerate(self.segments)]

    def _mode_strobe(self, f: Features, dt: float) -> list[list[RGB]]:
        """Dark between beats, tinted white on each beat. Intense."""
        tint = self.palette[self.palette_idx]
        rgb = scale(mix(tint, (255, 255, 255), self.flash), self.flash) if self.flash > 0.05 else (0, 0, 0)
        return [[rgb] * n for n in self.segments]


    # -- concert mode ---------------------------------------------------------
    RAINBOW_SECS = 6.0  # how long the post-drop effect rages before settling into heavy
    COMET_PAIRS = [((1.0, 0.0, 0.0), (0.0, 0.25, 1.0)),    # red / blue
                   ((1.0, 0.0, 0.6), (0.0, 1.0, 0.8)),     # magenta / aqua
                   ((1.0, 0.45, 0.0), (0.5, 0.0, 1.0))]    # orange / violet

    def _update_mood(self, f: Features, dt: float) -> None:
        """Slowly steer the base hue by how dark or bright the music is:
        bass-heavy and dark -> deep red, mid -> violet/magenta, bright -> cyan."""
        st = self.st
        if f.silent:
            return
        target = float(np.interp(f.centroid, [0.18, 0.42, 0.70], [0.0, 0.83, 0.55]))
        if st.bass_rel > 0.85 and f.centroid < 0.33:
            target = 0.0  # dark and heavy: red
        delta = ((target - self.mood_hue + 0.5) % 1.0) - 0.5  # shortest way round the wheel
        self.mood_hue = (self.mood_hue + delta * (1 - math.exp(-dt / 2.5))) % 1.0
        if st.drop:
            self.drop_count += 1
            self.drop_fx = "comets" if self.drop_count % 2 == 1 else "rainbow"
            self.comet_colors = self.COMET_PAIRS[(self.drop_count // 2) % len(self.COMET_PAIRS)]
            self.comet_pos = 0.0
        speed = 1.0 + 3.0 * st.kick if st.drop_age < self.RAINBOW_SECS else 0.15
        self.rainbow_phase = (self.rainbow_phase + dt * speed) % 1.0
        self.comet_pos += dt * (1.4 + 2.0 * st.kick)  # strip lengths per second

    def _comets(self, x: np.ndarray, st: Structure) -> np.ndarray:
        """Two comets of contrasting colours launched from opposite ends, bouncing
        between the ends with fading trails behind them; kicks stretch the trails."""
        ca, cb = (np.array(c) for c in self.comet_colors)
        p = self.comet_pos % 2.0
        head_a = -1.0 + 2.0 * p if p <= 1.0 else 1.0 - 2.0 * (p - 1.0)   # bounces end to end
        dir_a = 1.0 if p <= 1.0 else -1.0
        head_b, dir_b = -head_a, -dir_a                                   # mirror image
        length = 0.28 + 0.35 * st.kick
        img = np.zeros((len(x), 3))
        for head, d, col in ((head_a, dir_a, ca), (head_b, dir_b, cb)):
            behind = (head - x) * d                       # >0 behind the head, <0 ahead of it
            trail = np.where(behind >= 0, np.exp(-behind / length), 0.0)
            glow = np.exp(-((x - head) / 0.07) ** 2)      # bright head
            img += np.clip(trail + glow, 0, 1)[:, None] * col[None, :]
        return np.clip(img, 0, 1)

    def _mode_concert(self, f: Features, dt: float) -> list[list[RGB]]:
        """The whole show: mood-coloured grooves, aurora fades when it is chill,
        strobing buildups that fill the strip toward the centre, a white blast
        and shockwave on the drop, rainbow chaos after it, beat strobes and
        fast chases while it stays heavy. Sparkles follow the hi-hats."""
        st, cfg = self.st, self.cfg
        white = np.ones(3)
        floor = cfg.brightness_floor
        lvl = self.level(f.level)
        sec = st.section
        out = []
        for i, n in enumerate(self.segments):
            x = np.linspace(-1, 1, n) if n > 1 else np.zeros(1)
            ax = np.abs(x)
            centre = np.exp(-(ax / 0.35) ** 2)
            hue = self.mood_hue

            if sec == "chill":
                ph = self.clock
                h = hue + 0.06 * np.sin(2.0 * x + ph * 0.30) + 0.04 * np.sin(4.7 * x - ph * 0.19)
                v = 0.16 + 0.45 * lvl + 0.12 * np.sin(3.1 * x + ph * 0.45)
                img = hsv_arr(h, 0.85, v)

            elif sec == "groove":
                h = hue + 0.10 * x + 0.02 * np.sin(self.clock * 0.7)
                v = floor + 0.55 * lvl + 0.7 * st.kick * centre
                img = hsv_arr(h, 1.0 - 0.15 * st.kick * centre, v)

            elif sec == "buildup":
                p = st.buildup
                fill = ax > (1.0 - p) * 1.02           # lights up from both ends toward the centre
                h = hue + (0.08 - hue) * 0.6 * p          # heats toward orange as it climbs
                v = np.where(fill, 0.55 + 0.45 * p, floor + 0.25 * p)
                img = hsv_arr(h, 1.0, v)
                period = 0.50 * (1 - p) ** 1.6 + 0.045    # strobe accelerates: ~3 Hz -> ~20 Hz
                phase = (self.clock % period) / period
                if p > 0.3:
                    if cfg.strobe:
                        if phase >= 0.5:                   # blackout half of every cycle
                            img *= 0.0
                    else:
                        img *= 0.65 + 0.35 * math.sin(2 * math.pi * phase)

            else:  # heavy
                blocks = ((x * n / 3.0 + self.clock * (6 + 6 * st.intensity)) // 1) % 2
                h = np.where(blocks > 0, hue, hue + 0.5)
                side = np.where((x < 0) == (self.beat_count % 2 == 0), 1.0, 0.55)
                v = (floor + 0.35 + 0.3 * lvl) * side + 0.6 * st.kick * centre
                if cfg.strobe and st.kick > 0.72:
                    v = np.ones(n)                         # kick: whole strip to full colour...
                img = hsv_arr(h, 1.0, v)
                if cfg.strobe and 0.38 < st.kick < 0.62:
                    img *= 0.0                             # ...then a ~40 ms blackout gap
                if st.drop_age < self.RAINBOW_SECS:
                    if self.drop_fx == "comets":
                        rb = self._comets(x, st)
                    else:
                        rb = hsv_arr(self.rainbow_phase + x * 1.2, 1.0, 1.0)
                        if cfg.strobe and 0.38 < st.kick < 0.62:
                            rb *= 0.0
                    w = min(1.0, (self.RAINBOW_SECS - st.drop_age) / 1.2)  # last 1.2 s cross-fades into heavy
                    img = rb * w + img * (1 - w)

            # -- overlays -----------------------------------------------------------
            if st.drop_age < 0.07:
                img = np.ones_like(img)
            elif st.drop_age < 0.9:
                ring = np.exp(-((ax - st.drop_age * 1.3) / 0.12) ** 2) * (1 - st.drop_age / 0.9)
                img = np.maximum(img, ring[:, None] * hsv_arr(hue + 0.5, 1.0, 1.0))

            sp = self._sparkle[i]
            if sp.shape[0] != n:
                sp = self._sparkle[i] = np.zeros(n)
            sp *= math.exp(-dt / (0.25 if sec == "chill" else 0.07))
            rate = (f.treble * 1.0) if sec == "chill" else (f.treble * 4 + st.hit * 30 + 2 * st.intensity)
            new = self._rng.random(n) < min(0.4, rate * dt)
            sp[new] = 1.0
            tint = hsv_arr(hue + 0.5, 0.55, 1.0)
            img = np.maximum(img, sp[:, None] * tint * (0.6 if sec == "chill" else 1.0))

            # -- temporal smoothing: chill fades slowly, everything else snaps ------
            prev = self._prev[i]
            if prev.shape != img.shape:
                prev = img
            if sec == "chill":
                a_up, a_dn = 0.15, 0.05
            else:
                a_up, a_dn = 0.95, {"groove": 0.35, "buildup": 0.6, "heavy": 0.85}[sec]
            img = prev + (img - prev) * np.where(img > prev, a_up, a_dn)
            self._prev[i] = img
            out.append([(int(r * 255), int(g * 255), int(b * 255)) for r, g, b in np.clip(img, 0, 1)])
        return out


MODES = [m[len("_mode_"):] for m in dir(Show) if m.startswith("_mode_")]
