"""Names in config.json that changed: a config saved by an older version keeps everything it holds."""

from __future__ import annotations

RENAMED = {"cues": "show_moments", "party_modes": "color_moments", "party_level": "party_energy", "party_ramp": "energy_ramp",
           "jam_colors": "color_settings", "jam_level_target": "party_energy_target"}


def upgrade(cfg: dict) -> dict:
    for old in list(cfg):
        new = RENAMED.get(old) or ("party_" + old[4:] if old.startswith("jam_") else "")
        if new:
            value = cfg.pop(old)
            cfg.setdefault(new, value)
    on = cfg.get("addons") or {}
    if "bulbshow" in on:
        on["bulbstudio"] = on.pop("bulbshow")
    bell = cfg.get("doorbell") or {}
    if "cue" in bell:
        bell["show_moment"] = bell.pop("cue")
    return cfg
