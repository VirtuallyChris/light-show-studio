"""Command-line entry point."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import time

from . import audio, govee_lan
from .show import MODES, PALETTES, Show, ShowConfig, average

CONFIG_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "config.json")
DEFAULT_SEGMENTS = 10


def meter(value: float, width: int) -> str:
    n = int(max(0.0, min(1.0, value)) * width)
    return "#" * n + "." * (width - n)


# -- config (segment counts per light) ---------------------------------------
def load_config() -> dict:
    try:
        with open(CONFIG_PATH, encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return {}


def save_config(cfg: dict) -> None:
    with open(CONFIG_PATH, "w", encoding="utf-8") as fh:
        json.dump(cfg, fh, indent=2)


def segments_for(devs, override: str | None) -> list[int]:
    """Segment count per device: --leds override, then config.json, then a default."""
    cfg = load_config().get("segments", {})
    out = []
    for d in devs:
        n = cfg.get(d.mac) or cfg.get(d.ip) or cfg.get(d.sku) or DEFAULT_SEGMENTS
        out.append(int(n))
    if override:
        if override.isdigit():
            out = [int(override)] * len(devs)
        else:
            for part in override.split(","):
                sel, _, n = part.partition("=")
                for i, d in enumerate(devs):
                    if sel.strip().upper() in (d.ip, d.mac, d.sku.upper()):
                        out[i] = int(n)
    return out


# -- commands -----------------------------------------------------------------
def cmd_devices(_args):
    print("Scanning LAN for Govee devices (LAN Control must be enabled in the Govee Home app)...")
    devs = govee_lan.discover(2.5)
    if not devs:
        print("No devices answered.")
        return 1
    segs = segments_for(devs, None)
    for d, n in zip(devs, segs):
        print(f"  {d.sku:7s} ip={d.ip:15s} wifi-mac={d.mac or '?':17s} id={d.device_id}  segments={n}")
    print(f"\nSegment counts come from {CONFIG_PATH} (run `probe` to measure and save them).")
    return 0


def cmd_audio(_args):
    default = audio.default_input_device()
    for idx, name, api in audio.list_input_devices():
        mark = "*" if idx == default else " "
        print(f"{mark} {idx:3d}  {name}  [{api}]")
    print("\n* = default. Pass --audio-device <index or name substring>.")
    return 0


def cmd_test(args):
    devs = _select_devices(args.devices)
    print("Cycling red / green / blue / white on:", ", ".join(d.label for d in devs))
    for d in devs:
        d.turn(True)
        d.brightness(100)
    for rgb in [(255, 0, 0), (0, 255, 0), (0, 0, 255), (255, 255, 255)]:
        for d in devs:
            d.color(*rgb)
        time.sleep(0.7)
    return 0


def cmd_probe(args):
    """Light segments one at a time so the user can count how many the device has."""
    devs = _select_devices(args.devices)
    if len(devs) != 1:
        sys.exit("error: probe one light at a time, e.g.  lightshow probe 5C:E7:53:E9:C7:FE")
    d = devs[0]
    n = args.max
    print(f"Probing {d.label}: lighting segments 1..{n} one at a time (white on dim blue).")
    print("Count how many distinct zones light up before it stops changing.\n")
    d.turn(True)
    d.brightness(100)
    time.sleep(0.15)
    d.razer(True)
    time.sleep(0.2)
    try:
        for k in range(n):
            sys.stdout.write(f"\r  segment {k + 1:2d} / {n}   ")
            sys.stdout.flush()
            t0 = time.monotonic()
            while time.monotonic() - t0 < args.dwell:
                d.razer_leds([(255, 255, 255) if i == k else (0, 0, 25) for i in range(n)])
                time.sleep(0.05)
        print("\n  sweeping all segments as a rainbow for 3 s...")
        t0 = time.monotonic()
        while time.monotonic() - t0 < 3:
            ph = (time.monotonic() - t0) * 0.5
            from .show import hsv
            d.razer_leds([hsv(i / n + ph, 1, 1) for i in range(n)])
            time.sleep(0.033)
    except KeyboardInterrupt:
        pass
    finally:
        d.razer(False)
    if args.save:
        cfg = load_config()
        cfg.setdefault("segments", {})[d.mac or d.ip] = int(args.save)
        save_config(cfg)
        print(f"Saved segments={args.save} for {d.mac or d.ip} to {CONFIG_PATH}")
    else:
        print("\nWhen you know the count, save it with:  lightshow probe <light> --save <count>")
    return 0


def _prepare_show(args):
    devs = _select_devices(args.devices)
    segs = segments_for(devs, args.leds)
    dev_index = _pick_audio_device(args.audio_device)
    cfg = ShowConfig(mode=args.mode, palette=args.palette, fps=args.fps, realtime=not args.classic,
                     gradient=args.gradient, strobe=not args.no_strobe,
                     brightness_floor=args.floor, beat_flash=args.flash)
    print("Lights : " + ", ".join(f"{d.label} x{n} segments" for d, n in zip(devs, segs)))
    print(f"Audio  : device {dev_index} ({_device_name(dev_index)})")
    print(f"Mode   : {cfg.mode}  palette={cfg.palette}  fps={cfg.fps}  "
          f"{'real-time per-segment (razer)' if cfg.realtime else 'classic colour command'}")
    return devs, segs, dev_index, cfg


def _run_show(args, devs, segs, dev_index, cfg, keep_going=lambda: True, status=lambda: ""):
    """Drive the lights until Ctrl+C or until keep_going() returns False."""
    show = Show(devs, cfg, segs)
    # Loudness calibration learned in earlier runs: lets concert mode judge
    # intensity from the first bar instead of after the first loud section.
    show.tracker.load_calibration(load_config().get("calibration", {}))
    analyzer = audio.AudioAnalyzer(device=dev_index, beat_sensitivity=args.sensitivity)
    last_ui = 0.0
    f, frames = audio.Features(), [[(0, 0, 0)] * n for n in segs]
    show.start()
    try:
        with analyzer:
            while keep_going():
                nf = analyzer.analyze()
                if nf.fresh:  # one analysis block ~ every 21-23 ms
                    f = nf
                    frames = show.update(f)
                now = time.monotonic()
                if now - last_ui > 0.05:
                    last_ui = now
                    _draw(f, frames, show, status())
                time.sleep(0.002)
    finally:
        exit_color = tuple(int(x) for x in args.exit_color.split(",")) if args.exit_color else None
        show.stop(off=args.off_on_exit, exit_color=exit_color)
        if show.st.contrast > 2.2:  # only remember runs that actually heard some music
            cfg_file = load_config()
            cfg_file["calibration"] = show.tracker.calibration()
            try:
                save_config(cfg_file)
            except OSError:
                pass


def cmd_run(args):
    devs, segs, dev_index, cfg = _prepare_show(args)
    print("Press Ctrl+C to stop.\n")
    try:
        _run_show(args, devs, segs, dev_index, cfg)
    except KeyboardInterrupt:
        print("\nStopping.")
    return 0


def _draw(f, frames, show, status: str = ""):
    cols = shutil.get_terminal_size((100, 20)).columns
    w = max(6, min(16, (cols - 80) // 4))
    beat = "*" if f.beat or show.flash > 0.6 else " "
    bpm = f"{f.bpm:3.0f}bpm" if f.bpm else "  --bpm"
    st = show.st
    tag = {"chill": "chill  ", "groove": "groove ", "buildup": f"BUILD {st.buildup:.1f}", "heavy": "HEAVY  "}[st.section]
    if st.drop_age < 1.0:
        tag = "DROP!! "
    status = (f"{tag} i={st.intensity:.2f}" + (f"  {status}" if status else ""))
    # one coloured block per segment of the first light (up to 24), then the average of the others
    first = frames[0][:24] if frames else []
    swatches = "".join(f"\x1b[38;2;{r};{g};{b}m█\x1b[0m" for r, g, b in first)
    if len(frames) > 1:
        swatches += " " + " ".join(f"\x1b[38;2;{r};{g};{b}m■\x1b[0m" for r, g, b in map(average, frames[1:]))
    line = (f"\r{beat} lvl {meter(f.level, w)} bass {meter(f.bass, w)} mid {meter(f.mid, w)} "
            f"hi {meter(f.treble, w)} {bpm} {swatches} tx={show.frames_sent}"
            + ("  (silent)" if f.silent else "          ")
            + (f"  {status[:30]}" if status else ""))
    sys.stdout.write(line)
    sys.stdout.flush()


def _select_devices(selectors):
    devs = govee_lan.discover(2.0)
    try:
        picked = govee_lan.resolve_targets(selectors, devs)
    except LookupError as e:
        sys.exit(f"error: {e}\nDiscovered: " + (", ".join(d.label for d in devs) or "nothing"))
    if not picked:
        sys.exit("error: no Govee devices found. Enable 'LAN Control' in the Govee Home app for each light.")
    return picked


def _pick_audio_device(spec):
    if spec is None:
        # Prefer the WASAPI entry for the default input: it delivers audio in
        # steady small chunks, where MME hands it over in bursts.
        default = audio.default_input_device()
        name = _device_name(default)
        twins = [i for i, n, a in audio.list_input_devices() if n == name and "WASAPI" in a]
        return twins[0] if twins else default
    if spec.isdigit():
        return int(spec)
    spec_l = spec.lower()
    cands = [(i, n, a) for i, n, a in audio.list_input_devices() if spec_l in n.lower()]
    if not cands:
        sys.exit(f"error: no input device matching '{spec}'. Run: lightshow audio")
    # Prefer WASAPI for lower latency when several host APIs expose the same device.
    cands.sort(key=lambda c: 0 if "WASAPI" in c[2] else 1)
    return cands[0][0]


def _device_name(idx):
    try:
        import sounddevice as sd
        return sd.query_devices(idx)["name"]
    except Exception:
        return "?"


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
    # Windows consoles often default to a legacy code page; make sure the
    # coloured swatches and meter characters never crash the show.
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass

    p = argparse.ArgumentParser(prog="lightshow", description="Sync Govee LAN lights to microphone audio.")
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("devices", help="scan the LAN for Govee devices").set_defaults(fn=cmd_devices)
    sub.add_parser("audio", help="list audio input devices").set_defaults(fn=cmd_audio)

    t = sub.add_parser("test", help="cycle colours on the lights to verify control")
    t.add_argument("devices", nargs="*", help="IPs, wifi MACs, Govee ids or SKUs (default: all)")
    t.set_defaults(fn=cmd_test)

    pr = sub.add_parser("probe", help="light segments one by one to count a light's zones")
    pr.add_argument("devices", nargs=1, help="one light: IP, wifi MAC, Govee id or SKU")
    pr.add_argument("--max", type=int, default=30, help="highest segment index to try (default 30)")
    pr.add_argument("--dwell", type=float, default=0.6, help="seconds per segment (default 0.6)")
    pr.add_argument("--save", type=int, help="save this segment count for the light to config.json")
    pr.set_defaults(fn=cmd_probe)

    def show_options(sp):
        sp.add_argument("devices", nargs="*", help="IPs, wifi MACs, Govee ids or SKUs (default: all)")
        sp.add_argument("-a", "--audio-device", help="input device index or name substring (default: system default)")
        sp.add_argument("-m", "--mode", choices=MODES, default="concert")
        sp.add_argument("-p", "--palette", choices=sorted(PALETTES), default="neon")
        sp.add_argument("--leds", help="segment count: a number for all lights, or IP=N,MAC=N,...")
        sp.add_argument("--fps", type=float, default=60.0, help="colour updates per second per light (default 60)")
        sp.add_argument("--gradient", action="store_true", help="let the light blend colours between segments")
        sp.add_argument("--no-strobe", action="store_true", help="concert mode: pulse instead of hard strobing")
        sp.add_argument("--classic", action="store_true",
                        help="use the normal colour command (one colour, slow fade) instead of real-time per-segment mode")
        sp.add_argument("--sensitivity", type=float, default=1.4,
                        help="beat threshold in std-devs; lower = more beats (default 1.4)")
        sp.add_argument("--floor", type=float, default=0.06, help="minimum brightness 0..1 while music plays")
        sp.add_argument("--flash", type=float, default=0.6, help="beat flash strength 0..1")
        sp.add_argument("--off-on-exit", action="store_true", help="turn lights off when stopping")
        sp.add_argument("--exit-color", help="set lights to R,G,B when stopping, e.g. 255,160,60")

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

    r = sub.add_parser("run", help="start the light show")
    show_options(r)
    r.set_defaults(fn=cmd_run)

    args = p.parse_args(argv)
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
