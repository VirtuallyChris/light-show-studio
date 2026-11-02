import argparse
import sys

from .server import run


def main(argv=None) -> int:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass
    p = argparse.ArgumentParser(prog="bulbshow", description="Bulb Studio: looping light shows, one sound, one speaker, one bulb each.")
    p.add_argument("--host", default="0.0.0.0", help="interface to listen on (default all, so speakers can fetch the sound)")
    p.add_argument("--port", type=int, default=8770)
    p.add_argument("--no-browser", action="store_true", help="do not open a window")
    p.add_argument("--stay", action="store_true", help="keep running after the window is closed")
    p.add_argument("--no-lights", action="store_true", help="virtual bulbs only (a test copy)")
    p.add_argument("--no-cast", action="store_true", help="virtual speakers only (a test copy: no sound anywhere)")
    a = p.parse_args(argv)
    run(host=a.host, port=a.port, open_browser=not a.no_browser, stay=a.stay, lights=not a.no_lights, cast=not a.no_cast)
    return 0


if __name__ == "__main__":
    sys.exit(main())
