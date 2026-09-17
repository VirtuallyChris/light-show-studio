"""Music structure tracking: is this a buildup, a drop, a heavy section or a
chill one? Works on the raw (not auto-gained) features so that long-term
loudness trends survive.

The tracker keeps exponential averages of loudness, bass and brightness at
several time constants, plus slow-moving peak and trough references that
describe the dynamic range heard so far, and derives:

  intensity   0..1, where the music sits between its quietest and loudest recent parts
  rising      0..1, how strongly energy / brightness / onset density trend up
  breakdown   seconds the bass has been missing (breakdowns precede drops)
  section     "chill" | "groove" | "buildup" | "heavy"  (with hysteresis)
  drop        True for exactly one frame when a drop lands
  kick / hit  short envelopes fired by bass onsets and treble onsets

Until a song has shown some contrast, intensity sits at a neutral 0.5 so a
quiet intro is not mistaken for the loudest thing ever heard. The references
can be saved and restored between runs (see `calibration`).
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from .audio import Features


class Ema:
    """Exponential moving average with a time constant in seconds."""

    def __init__(self, tau: float, init: float | None = None):
        self.tau = tau
        self.v = 0.0 if init is None else init
        self._init = init is not None

    def update(self, x: float, dt: float) -> float:
        if not self._init:
            self.v, self._init = x, True
        else:
            self.v += (x - self.v) * (1 - math.exp(-dt / self.tau))
        return self.v


class Range:
    """Slowly-forgetting peak and trough of a signal.

    The peak jumps up instantly and decays toward the signal with a long
    half-life; the trough drops instantly and creeps back up the same way.
    """

    def __init__(self, half_life: float, floor: float):
        self.k = math.log(2) / half_life
        self.floor = floor
        self.peak = floor
        self.trough = None

    def update(self, x: float, dt: float) -> None:
        decay = math.exp(-self.k * dt)
        self.peak = max(self.floor, self.peak * decay, x)
        if self.trough is None:
            self.trough = max(self.floor, x)
        else:
            self.trough = min(max(self.floor, x), self.trough + (self.peak - self.trough) * (1 - decay))

    def contrast(self) -> float:
        return self.peak / max(self.floor, self.trough or self.floor)

    def position(self, x: float, min_contrast: float = 2.2) -> float:
        """Where x sits between trough (0) and peak (1); 0.5 until contrast is established."""
        if self.trough is None or self.contrast() < min_contrast:
            return 0.5
        return _clamp((x - self.trough) / (self.peak - self.trough))


@dataclass
class Structure:
    section: str = "groove"
    intensity: float = 0.5
    rising: float = 0.0
    buildup: float = 0.0       # progress 0..1 while in a buildup
    breakdown: float = 0.0     # seconds without bass
    drop: bool = False
    drop_age: float = 99.0     # seconds since the last drop
    kick: float = 0.0
    hit: float = 0.0
    onset_rate: float = 0.0    # beats per second
    level_rel: float = 0.5     # loudness position in the song's dynamic range
    bass_rel: float = 0.5
    treble_rel: float = 0.5
    contrast: float = 1.0      # peak/trough loudness ratio heard so far
    time_in_section: float = 0.0


class StructureTracker:
    DROP_COOLDOWN = 8.0
    HEAVY_AFTER_DROP = 8.0

    def __init__(self):
        self.level_fast, self.level_med, self.level_slow = Ema(0.10), Ema(1.2), Ema(6.0)
        self.bass_fast, self.bass_med = Ema(0.06), Ema(1.5)
        self.treb_fast, self.treb_med = Ema(0.06), Ema(1.5)
        self.cent_fast, self.cent_slow = Ema(0.8), Ema(5.0)
        self.onset_fast, self.onset_slow = Ema(1.5, init=0.0), Ema(6.0, init=0.0)
        self.level = Range(40.0, 1e-4)
        self.bass = Range(40.0, 1e-6)
        self.treble = Range(40.0, 1e-7)
        self.bass_hold = 0.0
        self.st = Structure()
        self.last_drop = -99.0
        self.t = 0.0
        self.timers = {"chill": 0.0, "heavy": 0.0, "rising": 0.0, "falling": 0.0, "calm": 0.0}

    # -- persistence -------------------------------------------------------------
    def calibration(self) -> dict:
        return {name: {"peak": r.peak, "trough": r.trough}
                for name, r in (("level", self.level), ("bass", self.bass), ("treble", self.treble))}

    def load_calibration(self, cal: dict) -> None:
        for name, r in (("level", self.level), ("bass", self.bass), ("treble", self.treble)):
            c = cal.get(name) if isinstance(cal, dict) else None
            if not isinstance(c, dict):
                continue
            try:
                peak, trough = float(c["peak"]), float(c["trough"])
            except (KeyError, TypeError, ValueError):
                continue
            if peak > r.floor and 0 < trough <= peak:
                r.peak, r.trough = peak, trough

    # -- main entry ---------------------------------------------------------------
    def update(self, f: Features, dt: float) -> Structure:
        st = self.st
        dt = max(1e-3, min(dt, 0.1))
        self.t += dt

        # --- envelopes ------------------------------------------------------------
        st.kick = 1.0 if f.beat else st.kick * math.exp(-dt / 0.11)
        st.hit = 1.0 if f.hit else st.hit * math.exp(-dt / 0.06)
        impulse = (1.0 / dt) if f.beat else 0.0   # so the EMA reads in beats per second
        self.onset_fast.update(impulse, dt)
        self.onset_slow.update(impulse, dt)
        st.onset_rate = self.onset_fast.v
        st.drop = False
        st.drop_age = self.t - self.last_drop

        if f.silent:
            st.intensity += (0.0 - st.intensity) * (1 - math.exp(-dt / 2.0))
            st.rising = 0.0
            st.breakdown += dt
            self._set_section("chill" if st.time_in_section > 2 else st.section, dt)
            return st

        # --- averages and dynamic range ---------------------------------------------
        lvl = self.level_fast.update(f.raw_rms, dt)
        lmed = self.level_med.update(f.raw_rms, dt)
        lslow = self.level_slow.update(f.raw_rms, dt)
        bass = self.bass_fast.update(f.raw_bass, dt)
        bmed = self.bass_med.update(f.raw_bass, dt)
        treb = self.treb_fast.update(f.raw_treble, dt)
        tmed = self.treb_med.update(f.raw_treble, dt)
        cf = self.cent_fast.update(f.centroid, dt)
        cs = self.cent_slow.update(f.centroid, dt)
        self.level.update(lmed, dt)
        self.bass.update(bmed, dt)
        self.treble.update(tmed, dt)
        ref_l, ref_b = self.level.peak, self.bass.peak

        st.level_rel = self.level.position(lmed)
        st.bass_rel = self.bass.position(bmed)
        st.treble_rel = self.treble.position(tmed)
        st.contrast = self.level.contrast()

        # bass presence with a short hold so the gap between kicks does not count as absence
        prev_breakdown = st.breakdown
        self.bass_hold = max(bass, self.bass_hold * math.exp(-dt / 0.45))
        st.breakdown = prev_breakdown + dt if self.bass_hold < 0.18 * ref_b else 0.0

        # --- intensity & trend ---------------------------------------------------------
        onset_term = min(1.0, st.onset_rate / 3.5)
        st.intensity = _clamp(0.45 * st.level_rel + 0.35 * st.bass_rel + 0.20 * onset_term)

        slope = (lmed - lslow) / ref_l                            # loudness trending up
        bright = (cf - cs) * 3.0                                  # spectrum sweeping upward (risers)
        busier = (self.onset_fast.v - self.onset_slow.v) / 3.0    # snare rolls, faster hits
        st.rising = _clamp(2.5 * slope + bright + busier)

        # --- drop detection ---------------------------------------------------------------
        primed = prev_breakdown > 1.0 or (st.section == "buildup" and st.buildup > 0.25)
        slam = bass > 0.55 * ref_b and lvl > 0.6 * ref_l and (bass > 2.5 * bmed or prev_breakdown > 1.0)
        if primed and slam and st.drop_age > self.DROP_COOLDOWN:
            st.drop = True
            self.last_drop = self.t
            st.drop_age = 0.0
            st.breakdown = 0.0
            self.bass.peak = max(self.bass.peak, bass)
            self.level.peak = max(self.level.peak, lvl)
            self._set_section("heavy", dt, force=True)

        # --- section state machine with hysteresis ----------------------------------------
        T = self.timers
        T["rising"] = T["rising"] + dt if st.rising > 0.45 else 0.0
        T["falling"] = T["falling"] + dt if st.rising < 0.15 else 0.0
        T["heavy"] = T["heavy"] + dt if st.intensity > 0.72 else 0.0
        T["calm"] = T["calm"] + dt if st.intensity < 0.55 else 0.0
        T["chill"] = T["chill"] + dt if st.intensity < 0.35 else 0.0

        # bass gone for a while but the track is still going: the breakdown before a drop
        breakdown_riser = st.breakdown > 1.5 and st.intensity > 0.4 and st.drop_age > 2.0

        sec = st.section
        if st.drop:
            sec = "heavy"
        elif sec == "heavy":
            if breakdown_riser or (T["rising"] > 1.2 and st.drop_age > self.HEAVY_AFTER_DROP):
                sec = "buildup"
            elif st.drop_age > self.HEAVY_AFTER_DROP and T["calm"] > 2.0:
                sec = "groove"
        elif sec == "buildup":
            if (T["falling"] > 1.5 and st.breakdown == 0.0) or st.time_in_section > 30:
                sec = "groove"
            elif T["heavy"] > 3.0 and st.rising < 0.2 and st.breakdown == 0.0:
                sec = "heavy"
        else:  # chill or groove
            if (T["rising"] > 1.2 and st.intensity > 0.2) or (sec == "groove" and breakdown_riser):
                sec = "buildup"
            elif T["heavy"] > 1.5:
                sec = "heavy"
            elif sec == "groove" and T["chill"] > 3.0:
                sec = "chill"
            elif sec == "chill" and st.intensity > 0.5 and T["chill"] == 0.0:
                sec = "groove"
        self._set_section(sec, dt)

        if st.section == "buildup":
            # mostly a clock (so the strobe accelerates steadily), pushed along by how
            # loud and how strongly rising the music is; never moves backwards
            ramp = 0.55 * min(1.0, st.time_in_section / 10.0)
            push = 0.45 * _clamp(0.5 * st.rising + st.level_rel - 0.5)
            st.buildup = _clamp(max(st.buildup, ramp + push))
        else:
            st.buildup = 0.0
        return st

    def _set_section(self, sec: str, dt: float, force: bool = False) -> None:
        st = self.st
        if sec != st.section or force:
            st.section = sec
            st.time_in_section = 0.0
            for k in self.timers:
                self.timers[k] = 0.0
        else:
            st.time_in_section += dt


def _clamp(x: float, lo: float = 0.0, hi: float = 1.0) -> float:
    return lo if x < lo else hi if x > hi else x
