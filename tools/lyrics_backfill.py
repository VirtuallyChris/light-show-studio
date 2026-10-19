r"""Lyrics with word times for every kept Jam copy: shows\_jam\copies\<track id>.flac -> shows\_jam\lyrics\<track id>.json

Run from the project folder, in the model environment (it needs the graphics card, ~5 GB of it):

    .venv-songformer\Scripts\python tools\lyrics_backfill.py              every copy without lyrics yet
    .venv-songformer\Scripts\python tools\lyrics_backfill.py --force      every copy, again
    .venv-songformer\Scripts\python tools\lyrics_backfill.py --only ID    one song (or several: --only A B)

Songs that already have a lyrics file are skipped, so it can be stopped and started again. While a Jam
is streaming it waits (the studio's own models need the card then), and asks again every minute.
The learned analyses (shows\_jam\learned) are only read, for the Spotify name and length of a song.
Nothing is played and no light is touched.
"""
import argparse
import glob
import json
import os
import sys
import time
import traceback
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from lightshow.studio import lyrics  # noqa: E402

STATE_URL = "http://127.0.0.1:8765/api/party/state"


def jamming() -> bool:
    """True while the studio is streaming a Jam (no answer: the studio is not running)."""
    try:
        with urllib.request.urlopen(STATE_URL, timeout=3) as r:
            return bool(json.loads(r.read()).get("streaming"))
    except Exception:
        return False


def song_info(copy: str, learned_dir: str) -> dict:
    """Title, artist, album and the release's length: the copy's tags, then the learned analysis's Spotify track."""
    import soundfile as sf
    tid = os.path.splitext(os.path.basename(copy))[0]
    info = {"title": "", "artist": "", "album": "", "duration": None}
    try:
        tags = sf.SoundFile(copy).copy_metadata()
        info.update(title=tags.get("title", ""), artist=(tags.get("artist", "") or "").split("/")[0].split(",")[0].strip(),
                    album=tags.get("album", ""))
    except Exception:
        pass
    learned = lyrics.read(os.path.join(learned_dir, tid + ".json")) or {}
    track = learned.get("track") or {}
    if track.get("name"):
        info["title"] = track["name"]
    if track.get("artists"):
        info["artist"] = track["artists"][0]
    if track.get("duration"):
        info["duration"] = track["duration"]
    return info


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--shows", default=os.path.join(ROOT, "shows"), help="the studio's shows folder")
    ap.add_argument("--only", nargs="*", help="only these track ids")
    ap.add_argument("--force", action="store_true", help="redo songs that already have lyrics")
    ap.add_argument("--cpu", action="store_true", help="run on the processor (slow), e.g. while jamming")
    args = ap.parse_args()
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")      # song names in any alphabet

    jam = os.path.join(args.shows, "_jam")
    copies = sorted(glob.glob(os.path.join(jam, "copies", "*.flac")))
    if args.only:
        copies = [c for c in copies if os.path.splitext(os.path.basename(c))[0] in set(args.only)]
    out_dir = os.path.join(jam, "lyrics")
    todo = [c for c in copies if args.force or not os.path.exists(os.path.join(out_dir, os.path.basename(c)[:-5] + ".json"))]
    print(f"{len(copies)} copies, {len(todo)} to do", flush=True)
    if not todo:
        return 0

    models = None
    failures = []
    for k, copy in enumerate(todo, 1):
        while not args.cpu and jamming():
            print("  a Jam is streaming: waiting", flush=True)
            time.sleep(60)
        if models is None:
            t = time.monotonic()
            models = lyrics.Models(device="cpu" if args.cpu else None)
            print(f"models loaded in {time.monotonic() - t:.0f} s", flush=True)
        tid = os.path.splitext(os.path.basename(copy))[0]
        info = song_info(copy, os.path.join(jam, "learned"))
        try:
            data = lyrics.lyrics_for(models, copy, info["title"], info["artist"], info["album"], info["duration"])
            data["track_id"] = tid
            lyrics.write(os.path.join(out_dir, tid + ".json"), data)
            print(f"[{k}/{len(todo)}] {info['artist']} - {info['title']}: {data['source']}, {len(data['words'])} words, "
                  f"{data['seconds']} s" + (f", offset {data['offset']}" if data.get("offset") is not None else "")
                  + (f", agreement {data['agreement']}" if "agreement" in data else "") + (f" ({data['why']})" if data.get("why") else ""),
                  flush=True)
        except Exception as e:
            failures.append(tid)
            print(f"[{k}/{len(todo)}] {tid} {info['artist']} - {info['title']}: FAILED {type(e).__name__}: {e}", flush=True)
            traceback.print_exc()
            if models is not None and "out of memory" in str(e).lower():
                models.torch.cuda.empty_cache()
    if models is not None and models.device == "cuda":
        print(f"card memory at most {models.torch.cuda.max_memory_allocated() / 2**30:.1f} GB (PyTorch)", flush=True)
    print(f"done; {len(failures)} failed: {' '.join(failures)}", flush=True)
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
