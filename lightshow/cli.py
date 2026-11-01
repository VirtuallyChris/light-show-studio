"""Command-line entry point: the studio, finding the lights, and measuring the Cast delay.

(The original microphone light show, `lightshow run`, and its helpers, `audio`, `test` and `probe`,
were retired once the studio did all of it: segment counts are set in the studio's light list.)
"""

from __future__ import annotations

import argparse
import json
import os
import sys

from . import govee_lan
from .appdirs import data_dir

CONFIG_PATH = os.path.join(data_dir(), "config.json")
DEFAULT_SEGMENTS = 10


def load_config() -> dict:
    try:
        with open(CONFIG_PATH, encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return {}


def cmd_devices(_args):
    print("Scanning LAN for Govee devices (LAN Control must be enabled in the Govee Home app)...")
    devs = govee_lan.discover(2.5)
    if not devs:
        print("No devices answered.")
        return 1
    cfg = load_config().get("segments", {})
    for d in devs:
        n = cfg.get(d.mac) or cfg.get(d.ip) or cfg.get(d.sku) or DEFAULT_SEGMENTS
        print(f"  {d.sku:7s} ip={d.ip:15s} wifi-mac={d.mac or '?':17s} id={d.device_id}  segments={n}")
    print(f"\nSegment counts come from {CONFIG_PATH} (set them in the studio's light list).")
    return 0


def cmd_calibrate(args):
    from .studio.calibrate import main as cal
    argv = [args.device, "--port", str(args.port), "--runs", str(args.runs)] + (["--mic", args.mic] if args.mic else []) + (["--no-save"] if args.no_save else [])
    return cal(argv)


def cmd_studio(args):
    from .studio.server import run
    run(host=args.host, port=args.port, open_browser=not args.no_browser,
        app_window=not args.tab, stay=args.stay, lights=not args.no_lights, player=not args.no_player)
    return 0


def main(argv=None):
    # Windows consoles often default to a legacy code page: printing never crashes on a character
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass

    p = argparse.ArgumentParser(prog="lightshow", description="Light Show Studio: Govee LAN lights, shows and Spotify jams.")
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("devices", help="scan the LAN for Govee devices").set_defaults(fn=cmd_devices)

    ca = sub.add_parser("calibrate", help="measure the Cast audio delay with the microphone and save it as the default Cast offset")
    ca.add_argument("device", help="Cast speaker or group name, e.g. Everywhere")
    ca.add_argument("--port", type=int, default=8765, help="port of the running studio (it serves the test file)")
    ca.add_argument("--mic", help="input device index or name substring (default: system default)")
    ca.add_argument("--runs", type=int, default=2)
    ca.add_argument("--no-save", action="store_true", help="measure only, do not change config.json")
    ca.set_defaults(fn=cmd_calibrate)

    st = sub.add_parser("studio", help="open the Light Show Studio sequencer in your browser")
    st.add_argument("--host", default="0.0.0.0", help="interface to listen on (default all, so Cast speakers can fetch audio)")
    st.add_argument("--port", type=int, default=8765)
    st.add_argument("--no-browser", action="store_true", help="do not open a browser window")
    st.add_argument("--tab", action="store_true", help="open a normal browser tab instead of a borderless app window")
    st.add_argument("--stay", action="store_true", help="keep the server running after the app window is closed")
    st.add_argument("--no-lights", action="store_true", help="never drive the lights (a test copy beside the real studio)")
    st.add_argument("--no-player", action="store_true", help="do not start the hidden Spotify player")
    st.set_defaults(fn=cmd_studio)

    args = p.parse_args(argv)
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
