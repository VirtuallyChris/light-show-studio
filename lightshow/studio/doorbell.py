"""A Reolink doorbell: when someone presses it, a phone remote moment runs (the doorbell's own show).

The doorbell is reached on the home network with reolink_aio (the library Home Assistant's Reolink
integration uses): the studio logs in once, subscribes to the doorbell's own event push (Reolink's
"Baichuan" protocol, port 9000), and on every event asks whether the visitor button is pressed. A
press is counted once (on the way down), and not again within RING_GAP_S, nor while a moment is running.
The connection is checked every minute and made again if the doorbell drops off the Wi-Fi.

The settings (address, user, password, which moment) are kept in config.json under "doorbell"; the
password is never sent back to a page.
"""

from __future__ import annotations

import asyncio
import time

RING_GAP_S = 10.0           # a second press this soon after the first is the same visitor
CHECK_EVERY_S = 60.0        # how often the event subscription is checked (and renewed)
RETRY_S = 30.0              # after a failure, how long before trying again


def public(cfg: dict) -> dict:
    """The doorbell's settings as a page may see them: never the password itself."""
    d = dict(cfg.get("doorbell") or {})
    return {"host": d.get("host", ""), "username": d.get("username", "admin"), "has_password": bool(d.get("password")),
            "show_moment": d.get("show_moment", "doorbell"), "on": bool(d.get("on", True))}


class Doorbell:
    def __init__(self, load_config, save_config, on_ring, make_host=None, clock=time.monotonic):
        self.load_config, self.save_config = load_config, save_config
        self.on_ring = on_ring                       # (moment id) -> None: what a press does
        self.make_host = make_host or self._reolink_host
        self.clock = clock
        self.task: asyncio.Task | None = None
        self.host = None
        self.channel = 0
        self.pressed = False
        self.last_ring = 0.0                         # monotonic time of the last press counted
        self.last_ring_at: float | None = None       # wall-clock time of it, for the page
        self.connected = False
        self.error = ""
        self.note = ""                               # what the last press did, when it could not run its moment
        self.name = ""

    @staticmethod
    def _reolink_host(host: str, username: str, password: str):
        from reolink_aio.api import Host
        return Host(host, username, password)

    def settings(self) -> dict:
        return dict(self.load_config().get("doorbell") or {})

    def save(self, body: dict) -> dict:
        """Change the settings (an empty or missing password keeps the one saved) and connect again."""
        cfg = self.load_config()
        d = dict(cfg.get("doorbell") or {})
        for k in ("host", "username", "show_moment"):
            if k in body:
                d[k] = str(body[k] or "").strip()[:120]
        if body.get("password"):
            d["password"] = str(body["password"])[:200]
        if "on" in body:
            d["on"] = bool(body["on"])
        cfg["doorbell"] = d
        self.save_config(cfg)
        return public(cfg)

    def status(self) -> dict:
        return {**public(self.load_config()), "connected": self.connected, "error": self.error, "note": self.note,
                "name": self.name, "last_ring": self.last_ring_at}

    # -- listening -------------------------------------------------------------------------------
    def start(self) -> None:
        """(Re)start listening, if the doorbell is set up and switched on."""
        self.stop()
        d = self.settings()
        if not d.get("host") or not d.get("on", True):
            self.error = ""                          # not set up, or switched off
            return
        if not d.get("password"):
            self.error = "no password yet"
            return
        self.task = asyncio.ensure_future(self._run(d))

    def stop(self) -> None:
        if self.task is not None:
            self.task.cancel()
            self.task = None
        self.connected = False

    async def close(self) -> None:
        self.stop()
        await self._logout()

    async def _logout(self) -> None:
        host, self.host = self.host, None
        if host is not None:
            try:
                await host.baichuan.unsubscribe_events()
            except Exception:  # noqa: BLE001
                pass
            try:
                await host.logout()
            except Exception:  # noqa: BLE001
                pass

    async def _run(self, d: dict) -> None:
        while True:
            try:
                self.host = self.make_host(d["host"], d.get("username") or "admin", d["password"])
                await self.host.get_host_data()
                chans = list(getattr(self.host, "channels", None) or [0])
                self.channel = next((c for c in chans if self.host.is_doorbell(c)), chans[0])
                try:
                    self.name = self.host.camera_name(self.channel)
                except Exception:  # noqa: BLE001
                    self.name = ""
                self.host.baichuan.register_callback("lightshow-doorbell", self._event)
                await self.host.baichuan.subscribe_events()
                self.connected, self.error = True, ""
                while True:
                    await asyncio.sleep(CHECK_EVERY_S)
                    await self.host.baichuan.check_subscribe_events()
            except asyncio.CancelledError:
                await self._logout()
                raise
            except Exception as e:  # noqa: BLE001 - shown on the phone remote; tried again shortly
                self.connected, self.error = False, f"{type(e).__name__}: {e}"[:200]
                await self._logout()
                await asyncio.sleep(RETRY_S)

    def _event(self) -> None:
        """Any push from the doorbell: is the visitor button down? Count it once, on the way down."""
        host = self.host
        if host is None:
            return
        try:
            now_pressed = bool(host.visitor_detected(self.channel))
        except Exception:  # noqa: BLE001
            return
        was, self.pressed = self.pressed, now_pressed
        if now_pressed and not was and self.clock() - self.last_ring >= RING_GAP_S:
            self.last_ring, self.last_ring_at = self.clock(), time.time()
            try:
                self.on_ring(self.settings().get("show_moment") or "doorbell")
                self.note = ""
            except Exception as e:  # noqa: BLE001 - a moment that cannot run (no show yet, one running) is shown, not raised
                self.note = f"rang, but {e}"[:200]
