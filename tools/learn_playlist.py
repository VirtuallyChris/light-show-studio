r"""Learn a whole Spotify playlist ahead, without the studio running: every song not learned yet is found on YouTube,
analysed, its lyrics and SongFormer's features taken, and its copy kept (shows/_jam/copies), exactly as learn ahead
does for a jam (fetch_ahead.py). Nothing plays, no light or speaker is touched. A jam started in the studio meanwhile
wins the graphics card: what is waiting is dropped (run this again later to go on; learned songs are skipped).

    .venv\Scripts\python tools\learn_playlist.py <playlist link>

Then Claude can review the new songs (tools/claude_review.py) and the labeler trains on them.
"""
import json
import os
import subprocess
import sys
import time
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
from lightshow.studio import labeler, lyrics  # noqa: E402
from lightshow.studio.fetch_ahead import FetchAhead, _spotdl_python  # noqa: E402
from lightshow.studio.jam import JamLibrary  # noqa: E402
from lightshow.studio.prelisten import playlist_id  # noqa: E402
from lightshow.studio.spotify import SpotifyClient, Track, largest_image, pick_image  # noqa: E402

CONFIG = os.path.join(ROOT, "config.json")


def _config() -> dict:
    with open(CONFIG, encoding="utf-8") as fh:
        return json.load(fh)


def _save_sign_in(store: dict) -> None:
    """Spotify hands out a new sign-in with each refresh: written back whole, so the studio keeps working."""
    cfg = _config()
    cfg["spotify"] = dict(store)
    tmp = CONFIG + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(cfg, fh, indent=2)
    os.replace(tmp, CONFIG)


# Without the playlist permission in the studio's Spotify sign-in, spotDL's own Spotify access lists a public playlist
_LIST = r'''
import json, sys
from spotdl.utils.config import DEFAULT_CONFIG
from spotdl.utils.spotify import SpotifyClient
SpotifyClient.init(client_id=DEFAULT_CONFIG["client_id"], client_secret=DEFAULT_CONFIG["client_secret"], user_auth=False)
sp, out = SpotifyClient(), []
res = sp.playlist_items(sys.argv[1], additional_types=("track",), limit=100)
while res:
    out += [it["track"] for it in res["items"] if (it.get("track") or {}).get("id") and it["track"].get("type") == "track"]
    res = sp.next(res) if res.get("next") else None
print(json.dumps(out))
'''


def _playlist_by_spotdl(pid: str) -> list:
    r = subprocess.run([_spotdl_python(), "-I", "-c", _LIST, pid], capture_output=True, text=True, encoding="utf-8", timeout=300)
    if r.returncode:
        raise SystemExit("spotDL could not read the playlist: " + (r.stderr.strip().splitlines() or ["?"])[-1])
    return [Track(id=t["id"], name=t["name"], artists=[a["name"] for a in t["artists"]], duration=t["duration_ms"] / 1000.0,
                  uri=t.get("uri", ""), artist_ids=[a["id"] for a in t["artists"] if a.get("id")],
                  image=pick_image((t.get("album") or {}).get("images")), art=largest_image((t.get("album") or {}).get("images")))
            for t in json.loads(r.stdout)]


def _jamming() -> bool:
    try:
        with urllib.request.urlopen("http://localhost:8765/api/jam/state", timeout=3) as r:
            return bool(json.load(r).get("running"))
    except Exception:  # noqa: BLE001  (the studio is not running)
        return False


def main(link: str) -> None:
    # (a title Windows' console code page cannot spell, logged by a worker, would kill that worker and stall the run)
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    pid = playlist_id(link)
    if not pid:
        raise SystemExit("paste a Spotify playlist link")
    store = dict(_config().get("spotify", {}))
    try:
        tracks = SpotifyClient(store, save=lambda: _save_sign_in(store)).playlist_tracks(pid, most=2000)
    except PermissionError:
        tracks = _playlist_by_spotdl(pid)
    lib = JamLibrary(os.path.join(ROOT, "shows"))
    fetch = FetchAhead(lib, keep=lambda: True, lyrics=lyrics.learn, features=labeler.learn_features)
    if not fetch.is_available():
        raise SystemExit("learn ahead is not set up (.venv-spotdl)")
    n = fetch.submit(tracks, first=False)
    print(f"{len(tracks)} songs in the playlist; {n} to learn, the rest learned already or one of your shows", flush=True)
    t0, last = time.time(), -1
    while not fetch.idle():
        time.sleep(5)
        if _jamming():
            fetch.forget()
            print("a jam started: what is waiting is dropped (run this again later to go on)", flush=True)
            while not fetch.idle():
                time.sleep(5)
            break
        st = fetch.status()
        if st["learned"] != last:                   # a line per song learned
            print(f"{st['learned']}/{n} learned, {st['waiting']} waiting, {len(fetch.failed)} failed"
                  f" ({(time.time() - t0) / 60:.0f} min)", flush=True)
            last = st["learned"]
    print(f"done: {fetch.status()['learned']} learned, {len(fetch.failed)} without a copy:", flush=True)
    for _, label, why in fetch.failed.values():
        print(f"  {label}: {why}", flush=True)


if __name__ == "__main__":
    main(" ".join(sys.argv[1:]))
