"""Add-ons: features for one kind of home, off until they are switched on (Party Hub > Settings > More settings >
Add-ons). Each is kept in config.json as {"addons": {"doorbell": true, ...}}; while one is off, its card stays
hidden on the Party Hub and it does nothing in the background (the doorbell is not listened to, Bulb Studio is not
asked about or started).

    doorbell   a Reolink video doorbell rings a Party Hub cue (doorbell.py; needs reolink_aio, in
               requirements-addons.txt)
    bulbstudio   Bulb Studio's looping shows (bulbstudio/, an app of its own: Govee bulbs over Govee's cloud, a sound
               on a speaker), switched on and off from the Lights tab (bulbstudio.py)
    homeassistant  Home Assistant is told when the studio plays on the lights, so its automations leave them be
               (homeassistant.py: a webhook, and a state for a RESTful sensor)
"""

from __future__ import annotations

import importlib.util
import os

from ..appdirs import code_dir


ADDONS = {
    "doorbell": {"name": "Doorbell", "about": "A Reolink video doorbell rings a cue: the music dips and its show plays."},
    "bulbstudio": {"name": "Bulb Studio", "about": "Looping shows on Govee bulbs, switched on and off on the Party screen and the phone."},
    "homeassistant": {"name": "Home Assistant", "about": "Tells Home Assistant when the show has your lights, so its automations leave their brightness alone."},
}


def installed(addon: str) -> str:
    """'' when the add-on can run on this computer, else what it is missing."""
    if addon == "doorbell":
        return "" if importlib.util.find_spec("reolink_aio") is not None else "needs reolink_aio: pip install -r requirements-addons.txt"
    if addon == "homeassistant":
        return ""
    if addon == "bulbstudio":
        return "" if os.path.isdir(os.path.join(code_dir(), "bulbstudio")) else "Bulb Studio is not in this copy"
    return "no such add-on"


def upgrade(cfg: dict) -> dict:
    """A config saved before Bulb Studio's add-on id changed keeps its switch."""
    on = cfg.get("addons") or {}
    if "bulbshow" in on:
        on["bulbstudio"] = on.pop("bulbshow")
    return cfg


def enabled(cfg: dict, addon: str) -> bool:
    return bool((cfg.get("addons") or {}).get(addon))


def status(cfg: dict) -> list[dict]:
    return [{"id": k, **v, "on": enabled(cfg, k), "missing": installed(k)} for k, v in ADDONS.items()]


def switch(load_config, save_config, addon: str, on: bool) -> dict:
    if addon not in ADDONS:
        raise ValueError(f"no add-on {addon!r}")
    cfg = load_config()
    cfg.setdefault("addons", {})[addon] = bool(on)
    save_config(cfg)
    return cfg
