"""The Home Assistant add-on: Home Assistant knows when the studio is playing a show on the lights, and which ones, so its
own automations (brightness by time of day, motion lights, adaptive lighting) leave them alone meanwhile.

The studio tells Home Assistant through a webhook (no token: Home Assistant makes the webhook's id; nothing is read
from Home Assistant). It sends a message whenever what it controls changes, and every HEARTBEAT_S while it plays, so a
studio that crashed stops counting as in control once the heartbeats stop (Home Assistant's side should treat
"controlling" as off after about three missed beats). On an orderly exit it says so at once.

    POST <url>/api/webhook/<webhook_id>
         {"controlling": true, "lights": ["Game Room", ...], "ids": ["D0:C9:...", ...],
          "rooms": ["Game Room", ...], "heartbeat_s": 60, "at": 1793510000}

GET /api/homeassistant/state gives the same, for a RESTful sensor instead of (or as well as) the webhook.
Settings: {"homeassistant": {"url": "http://homeassistant.local:8123", "webhook_id": "light_show_studio"}} in config.json.
"""

from __future__ import annotations

import json
import threading
import time
import urllib.request

from aiohttp import web

HEARTBEAT_S = 60.0
TICK_S = 2.0


class HomeAssistantLink:
    def __init__(self, studio, load_config, save_config, live: bool, post=None, clock=time.time):
        """`live`: a real studio (a test copy never tells Home Assistant anything: it would contradict the real one)."""
        self.studio, self.load_config, self.save_config, self.live = studio, load_config, save_config, live
        self.post = post or self._post
        self.clock = clock
        self.sent: dict | None = None            # the last state Home Assistant was told
        self.sent_at = 0.0
        self.error = ""
        self.stop_event = threading.Event()

    # -- what the studio controls now --------------------------------------------------------
    def settings(self) -> dict:
        return dict(self.load_config().get("homeassistant") or {})

    def enabled(self) -> bool:
        from . import addons
        cfg = self.load_config()
        return addons.enabled(cfg, "homeassistant") and bool((cfg.get("homeassistant") or {}).get("url"))

    def state(self) -> dict:
        out = getattr(self.studio, "output", None)
        devices = list(getattr(out, "devices", []) or []) if out is not None and out.enabled and out.active else []
        cfg = self.load_config()
        names = cfg.get("light_names", {})
        rooms = cfg.get("rooms", [])
        ids, lights, room_names = [], [], []
        for d in devices:
            keys = [k for k in (getattr(d, "mac", ""), getattr(d, "device_id", ""), d.ip) if k]
            ids.append(keys[0] if keys else d.ip)
            lights.append(next((names[k] for k in keys if names.get(k)), "") or getattr(d, "name", "") or getattr(d, "sku", "") or d.ip)
            for r in rooms:
                if r.get("on", True) and any(k in (r.get("lights") or []) for k in keys) and r.get("name") not in room_names:
                    room_names.append(r.get("name"))
        return {"controlling": bool(devices), "lights": lights, "ids": ids, "rooms": room_names, "heartbeat_s": HEARTBEAT_S}

    # -- telling Home Assistant -------------------------------------------------------------
    @staticmethod
    def _post(url: str, body: dict) -> None:
        req = urllib.request.Request(url, data=json.dumps(body).encode(), method="POST", headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=4) as r:
            r.read()

    def _url(self) -> str:
        s = self.settings()
        return f"{str(s.get('url') or '').rstrip('/')}/api/webhook/{s.get('webhook_id') or 'light_show_studio'}"

    def tell(self, st: dict) -> bool:
        try:
            self.post(self._url(), {**st, "at": int(self.clock())})
            self.sent, self.sent_at, self.error = st, self.clock(), ""
            return True
        except Exception as e:  # noqa: BLE001 - Home Assistant away: said in Settings, tried again next tick
            self.error = f"{type(e).__name__}: {e}"
            return False

    def tick(self) -> None:
        """Tell Home Assistant if what the studio controls changed, or a heartbeat is due while it controls lights."""
        if not self.live or not self.enabled():
            return
        st = self.state()
        due = st["controlling"] and self.clock() - self.sent_at >= HEARTBEAT_S
        if st != self.sent or due:
            self.tell(st)

    def release(self) -> None:
        """The studio is closing: the lights are Home Assistant's again, at once."""
        if self.live and self.enabled() and (self.sent or {}).get("controlling"):
            self.tell({"controlling": False, "lights": [], "ids": [], "rooms": [], "heartbeat_s": HEARTBEAT_S})

    def start(self) -> None:
        if not self.live:
            return

        def run():
            while not self.stop_event.wait(TICK_S):
                try:
                    self.tick()
                except Exception as e:  # noqa: BLE001 - the link must not end
                    self.error = f"{type(e).__name__}: {e}"
        threading.Thread(target=run, name="homeassistant", daemon=True).start()

    # -- routes -------------------------------------------------------------------------------
    def routes(self) -> list:
        return [web.get("/api/homeassistant/state", self.h_state), web.get("/api/homeassistant", self.h_settings),
                web.post("/api/homeassistant", self.h_settings_set)]

    async def h_state(self, request):
        return web.json_response({**self.state(), "at": int(self.clock())})

    def status(self) -> dict:
        s = self.settings()
        return {"url": s.get("url") or "", "webhook_id": s.get("webhook_id") or "light_show_studio", "live": self.live,
                "connected": bool(self.sent_at) and not self.error, "error": self.error, "told": self.sent}

    async def h_settings(self, request):
        return web.json_response(self.status())

    async def h_settings_set(self, request):
        """{url?, webhook_id?, test?}: kept in config.json; test sends the state now."""
        body = await request.json()
        cfg = self.load_config()
        s = dict(cfg.get("homeassistant") or {})
        for k in ("url", "webhook_id"):
            if k in body:
                s[k] = str(body[k] or "").strip()[:300]
        cfg["homeassistant"] = s
        self.save_config(cfg)
        if body.get("test") and s.get("url") and self.live:
            import asyncio
            await asyncio.get_running_loop().run_in_executor(None, self.tell, self.state())
        return web.json_response(self.status())
