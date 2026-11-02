"""Pre-listen: learn a party playlist's songs ahead of the party.

Spotify's audio only plays in real time (it is protected, and never kept), so a song can only be
learned by hearing it. Pre-listen does the hearing ahead of time: the studio's own hidden Spotify
player plays each song of a playlist (or the queue) that is not learned yet, into the output nobody
hears, through a silent jam (the room hears nothing; no speaker is touched). The jam learns each song
as it ends, exactly as it would at a party, and keeps only what it learned. When every song is
learned, Spotify pauses and the silent jam stops. A playlist of three hours takes three hours, so
it is something to start the night before.

One Spotify account plays one thing at a time: while pre-listen runs, the account is busy. If
Spotify starts playing anywhere else (a phone taking over), or a real jam is started, pre-listen
steps aside at once.

With learn ahead (fetch_ahead: a copy of each song found on YouTube, analysed and deleted) nothing
is played at all: every song is learned from its copy, two at a time (one downloading while the one
before it is analysed), about a minute each, and Spotify stays free.
"""

from __future__ import annotations

import re
import threading
import time

CHECK_S = 3.0               # how often progress is looked at
TAKEOVER_S = 12.0           # Spotify playing somewhere else this long: someone took the account back
MAX_TRACKS = 100            # songs handed to the player at once (Spotify's own limit is near this)


def playlist_id(text: str) -> str | None:
    """A playlist's id from its share link (open.spotify.com/playlist/<id>?si=...), its URI or the id."""
    text = (text or "").strip()
    m = re.search(r"playlist[/:]([A-Za-z0-9]{10,})", text)
    if m:
        return m.group(1)
    return text if re.fullmatch(r"[A-Za-z0-9]{10,}", text) else None


class PreListen:
    def __init__(self, spotify, library, start_jam, stop_jam, device_id, device_name="Light Show Studio",
                 clock=time.monotonic, sleep=time.sleep, fetch=None):
        self.spotify, self.library = spotify, library
        self.start_jam, self.stop_jam = start_jam, stop_jam         # the silent jam that does the learning
        self.device_id = device_id                                  # () -> the studio player's Spotify device id
        self.device_name = device_name
        self.clock, self.sleep = clock, sleep
        self.fetch = fetch                                          # FetchAhead, or None: learn by listening
        self.thread: threading.Thread | None = None
        self.halt = threading.Event()
        self.state: dict = {"running": False}

    # -- what is to be learned ---------------------------------------------------------------------
    def _tracks(self, source: str) -> list:
        if source == "queue":
            return list(self.spotify.queue())
        pid = playlist_id(source)
        if not pid:
            raise ValueError("paste a Spotify playlist link")
        return list(self.spotify.playlist_tracks(pid))

    def _unlearned(self, tracks: list) -> list:
        seen, out = set(), []
        for t in tracks:
            if t.id in seen or self.library.has_learned(t.id) or self.library.match_project(t):
                continue                                    # learned already, or one of your own shows
            seen.add(t.id)
            out.append(t)
        return out[:MAX_TRACKS]

    # -- running -------------------------------------------------------------------------------------
    def start(self, source: str) -> dict:
        if self.busy():
            raise RuntimeError("pre-listen is already running")
        tracks = self._tracks(source)
        todo = self._unlearned(tracks)
        self.state = {"running": bool(todo), "source": "the queue" if source == "queue" else "the playlist",
                      "total": len(todo), "learned": 0, "skipped": len(tracks) - len(todo), "current": None,
                      "remaining_s": round(sum(t.duration for t in todo)),
                      "note": "" if todo else ("every song here is learned already" if tracks else "there are no songs in it")}
        if not todo:
            return self.status()
        self.halt.clear()
        if self.fetch is not None:                          # from copies: nothing plays, Spotify stays free
            self.state["how"] = "copies"
            self.fetch.submit(todo, first=False)
            run = self._run_fetched
        else:
            self.start_jam()                                # silent: the room hears nothing, nothing is kept but what is learned
            run = self._run
        self.thread = threading.Thread(target=run, args=(todo,), daemon=True)
        self.thread.start()
        return self.status()

    def busy(self) -> bool:
        return bool(self.thread and self.thread.is_alive())

    def stop(self, note: str = "stopped") -> dict:
        if self.busy():
            self.halt.set()
            self.thread.join(timeout=10)
        self.state.update(running=False, note=self.state.get("note") or note)
        return self.status()

    def status(self) -> dict:
        return dict(self.state)

    def _finish(self, note: str, pause: bool) -> None:
        if pause:
            try:
                self.spotify.pause()
            except Exception:  # noqa: BLE001
                pass
        try:
            self.stop_jam()
        except Exception:  # noqa: BLE001
            pass
        self.state.update(running=False, current=None, note=note)

    def _run_fetched(self, todo: list) -> None:
        while not self.halt.wait(CHECK_S):
            learned = [t for t in todo if self.library.has_learned(t.id)]
            left = [t for t in todo if t not in learned]
            st = self.fetch.status()
            failed = {f["label"] for f in st["failed"]}
            self.state.update(learned=len(learned), remaining_s=round(60 * len(left)), current=st["working"])
            if not left:
                return self._finish_fetched(f"learned all {len(todo)} songs")
            if self.fetch.idle():                           # the rest could not be found or analysed
                missed = [t.label for t in left if t.label in failed] or [t.label for t in left]
                return self._finish_fetched(f"learned {len(learned)} of {len(todo)}; no copy for: {', '.join(missed[:4])}"
                                            + ("…" if len(missed) > 4 else ""))
        self.fetch.forget()
        self._finish_fetched(f"stopped after {len([t for t in todo if self.library.has_learned(t.id)])} of {len(todo)}")

    def _finish_fetched(self, note: str) -> None:
        self.state.update(running=False, current=None, note=note)

    def _run(self, todo: list) -> None:
        # the studio player has to be up before Spotify can be told to play on it
        dev, t0 = "", self.clock()
        while not dev and self.clock() - t0 < 30 and not self.halt.is_set():
            dev = self.device_id() or ""
            if not dev:
                self.sleep(1.0)
        if not dev:
            return self._finish("the studio's Spotify player did not start", pause=False)
        if not self.spotify.play_uris(dev, [t.uri for t in todo]):
            return self._finish(f"Spotify would not play the songs ({self.spotify.last_error or 'no answer'})", pause=False)
        elsewhere_since = None
        while not self.halt.wait(CHECK_S):
            learned = [t for t in todo if self.library.has_learned(t.id)]
            left = [t for t in todo if t not in learned]
            try:
                np = self.spotify.now_playing()
            except Exception:  # noqa: BLE001 - one bad answer is not a takeover
                np = None
            cur = np.track if np is not None else None
            self.state.update(learned=len(learned), remaining_s=round(sum(t.duration for t in left)),
                              current=cur.label if cur is not None else None)
            if not left:
                return self._finish(f"learned all {len(todo)} songs", pause=True)
            # someone took the account back (a phone playing), or it stopped altogether
            ours = np is not None and np.device == self.device_name
            if np is not None and np.track is not None and np.playing and not ours:
                elsewhere_since = elsewhere_since or self.clock()
                if self.clock() - elsewhere_since >= TAKEOVER_S:
                    return self._finish(f"Spotify is playing on {np.device or 'another device'}: stopped after {len(learned)} of {len(todo)}", pause=False)
            else:
                elsewhere_since = None
        self._finish(f"stopped after {len([t for t in todo if self.library.has_learned(t.id)])} of {len(todo)}", pause=True)
