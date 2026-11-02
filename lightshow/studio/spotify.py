"""A small Spotify Web API client: who is playing what, and what is queued.

Jam mode never asks Spotify for audio, because Spotify does not give it out. It
asks only what any remote control could: the current track and how far in it is,
whether it is paused, and what comes next. It can also pause and resume, which
is how the studio times the hand-over from an opening song on disk.

Sign-in is the Authorization Code flow with PKCE, which needs a client id but no
client secret, so nothing secret ships with the project. You sign in on
Spotify's own page in your browser; the studio receives a code on its loopback
address and keeps only the refresh token, in config.json (which is not in git).
"""

from __future__ import annotations

import base64
import hashlib
import json
import secrets
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field

AUTH_URL = "https://accounts.spotify.com/authorize"
TOKEN_URL = "https://accounts.spotify.com/api/token"
API = "https://api.spotify.com/v1"
# `streaming` (with the two profile scopes it requires) lets the studio run its own Spotify
# player, which appears in Spotify Connect as "Light Show Studio"
# (the two playlist scopes let pre-listen read a party playlist, private ones included)
SCOPES = ("user-read-playback-state user-read-currently-playing user-modify-playback-state "
          "streaming user-read-email user-read-private playlist-read-private playlist-read-collaborative")


@dataclass
class Track:
    id: str
    name: str
    artists: list[str]
    duration: float                       # seconds
    uri: str = ""
    artist_ids: list[str] = field(default_factory=list)
    image: str = ""                       # album art (a small size), for the show's colors
    art: str = ""                         # the same art at its largest (640 px), to look at on the phone

    @property
    def label(self) -> str:
        return f"{', '.join(self.artists)} - {self.name}" if self.artists else self.name

    def to_json(self) -> dict:
        return {"id": self.id, "name": self.name, "artists": self.artists, "duration": self.duration, "label": self.label,
                "image": self.image, "art": self.art}


def largest_image(images: list | None) -> str:
    ims = [i for i in (images or []) if isinstance(i, dict) and i.get("url")]
    return max(ims, key=lambda i: i.get("width") or 0)["url"] if ims else ""


def pick_image(images: list | None) -> str:
    """The smallest album image that is still big enough to read colors from."""
    ims = [i for i in (images or []) if isinstance(i, dict) and i.get("url")]
    if not ims:
        return ""
    big_enough = [i for i in ims if (i.get("width") or 0) >= 64]
    return min(big_enough or ims, key=lambda i: i.get("width") or 10 ** 6)["url"]


@dataclass
class NowPlaying:
    track: Track | None
    progress: float = 0.0                 # seconds into the track when fetched
    playing: bool = False
    fetched_at: float = 0.0               # local clock when the answer arrived
    device: str = ""


def parse_track(item: dict | None) -> Track | None:
    if not item or item.get("type") not in (None, "track") or not item.get("id"):
        return None
    return Track(id=item["id"], name=item.get("name", ""), artists=[a.get("name", "") for a in item.get("artists", [])],
                 duration=float(item.get("duration_ms", 0)) / 1000.0, uri=item.get("uri", ""),
                 artist_ids=[a.get("id", "") for a in item.get("artists", []) if a.get("id")],
                 image=pick_image((item.get("album") or {}).get("images")), art=largest_image((item.get("album") or {}).get("images")))


def http_transport(method: str, url: str, headers: dict, body: bytes | None) -> tuple[int, bytes]:
    req = urllib.request.Request(url, data=body, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=8) as r:
            return r.status, r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()


class SpotifyClient:
    """`store` is a dict-like that persists (client_id, refresh_token); `transport` is swappable for tests."""

    def __init__(self, store: dict, save=lambda: None, transport=http_transport, clock=time.monotonic):
        self.store, self.save, self.transport, self.clock = store, save, transport, clock
        self.access_token = ""
        self.expires_at = 0.0
        self.verifier = ""
        self.state = ""
        self.last_error = ""
        self.artist_cache: dict[str, list[str]] = {}

    # -- sign-in -----------------------------------------------------------------------
    @property
    def client_id(self) -> str:
        return self.store.get("client_id", "")

    @property
    def signed_in(self) -> bool:
        return bool(self.client_id and self.store.get("refresh_token"))

    def auth_url(self, redirect_uri: str) -> str:
        self.verifier = secrets.token_urlsafe(64)[:96]
        self.state = secrets.token_urlsafe(16)
        challenge = base64.urlsafe_b64encode(hashlib.sha256(self.verifier.encode()).digest()).rstrip(b"=").decode()
        q = {"client_id": self.client_id, "response_type": "code", "redirect_uri": redirect_uri, "scope": SCOPES,
             "code_challenge_method": "S256", "code_challenge": challenge, "state": self.state}
        return AUTH_URL + "?" + urllib.parse.urlencode(q)

    def finish_auth(self, code: str, state: str, redirect_uri: str) -> None:
        if not self.verifier or state != self.state:
            raise PermissionError("sign-in did not start here, or the state did not match")
        self._token_request({"grant_type": "authorization_code", "code": code, "redirect_uri": redirect_uri,
                             "client_id": self.client_id, "code_verifier": self.verifier})
        self.verifier = self.state = ""

    @property
    def can_stream(self) -> bool:
        """Whether the sign-in allows the studio's own player (older sign-ins did not ask for it)."""
        return self.signed_in and "streaming" in self.store.get("scope", "").split()

    def sign_out(self) -> None:
        self.store.pop("refresh_token", None)
        self.store.pop("scope", None)
        self.access_token = ""
        self.save()

    def _token_request(self, form: dict) -> None:
        status, body = self.transport("POST", TOKEN_URL, {"Content-Type": "application/x-www-form-urlencoded"},
                                      urllib.parse.urlencode(form).encode())
        data = json.loads(body or b"{}")
        if status != 200 or "access_token" not in data:
            raise PermissionError(f"Spotify refused the sign-in ({status}): {data.get('error_description') or data.get('error') or body[:120]}")
        self.access_token = data["access_token"]
        self.expires_at = self.clock() + float(data.get("expires_in", 3600)) - 60
        if data.get("scope") and data["scope"] != self.store.get("scope"):
            self.store["scope"] = data["scope"]
            self.save()
        if data.get("refresh_token"):
            self.store["refresh_token"] = data["refresh_token"]
            self.save()

    def token(self) -> str:
        """A current access token (for the calls here, and for the studio's own player page)."""
        if not self.access_token or self.clock() >= self.expires_at:
            if not self.store.get("refresh_token"):
                raise PermissionError("not signed in to Spotify")
            self._token_request({"grant_type": "refresh_token", "refresh_token": self.store["refresh_token"], "client_id": self.client_id})
        return self.access_token

    # -- calls -------------------------------------------------------------------------
    def _call(self, method: str, path: str, body: dict | None = None) -> tuple[int, dict]:
        headers = {"Authorization": "Bearer " + self.token()}
        data = None
        if body is not None:
            headers["Content-Type"] = "application/json"
            data = json.dumps(body).encode()
        elif method != "GET":
            headers["Content-Length"] = "0"
        status, raw = self.transport(method, API + path, headers, data)
        if status == 401:                                  # token died early: once more with a fresh one
            self.access_token = ""
            headers["Authorization"] = "Bearer " + self.token()
            status, raw = self.transport(method, API + path, headers, data)
        try:
            parsed = json.loads(raw) if raw else {}
        except ValueError:
            parsed = {}
        if status >= 400:
            self.last_error = f"{status}: {(parsed.get('error') or {}).get('message', '') if isinstance(parsed.get('error'), dict) else parsed.get('error', '')}"
        return status, parsed

    def _done(self, method: str, path: str, body: dict | None = None) -> bool:
        return self._call(method, path, body)[0] in (200, 202, 204)

    def now_playing(self) -> NowPlaying:
        status, data = self._call("GET", "/me/player?additional_types=track")
        now = self.clock()
        if status == 204 or not data:
            return NowPlaying(None, fetched_at=now)
        return NowPlaying(parse_track(data.get("item")), float(data.get("progress_ms") or 0) / 1000.0, bool(data.get("is_playing")),
                          now, (data.get("device") or {}).get("name", ""))

    def queue(self) -> list[Track]:
        status, data = self._call("GET", "/me/player/queue")
        if status != 200:
            return []
        return [t for t in (parse_track(i) for i in data.get("queue", [])) if t]

    def context(self) -> tuple[str, bool] | None:
        """What Spotify is playing from (a playlist's or an album's URI) and whether it shuffles; None when
        it plays loose songs (or something else: an artist, Liked Songs)."""
        err = self.last_error
        status, data = self._call("GET", "/me/player?additional_types=track")
        self.last_error = err                              # (looking further ahead: never an error to show)
        ctx = (data or {}).get("context") or {} if status == 200 else {}
        if ctx.get("type") not in ("playlist", "album") or not ctx.get("uri"):
            return None
        return ctx["uri"], bool(data.get("shuffle_state"))

    def context_tracks(self, uri: str, most: int = 500) -> list[Track]:
        """A playlist's or an album's songs, in order. Spotify will not list some playlists (its own mixes:
        403): that is no error to show, the queue is learned ahead all the same."""
        err = self.last_error
        try:
            return self._context_tracks(uri, most)
        finally:
            self.last_error = err

    def _context_tracks(self, uri: str, most: int) -> list[Track]:
        kind, _, cid = uri.rpartition(":")
        if kind.endswith("playlist"):
            return self.playlist_tracks(cid, most)
        out, offset = [], 0
        while offset < most:
            status, data = self._call("GET", f"/albums/{cid}/tracks?limit=50&offset={offset}")
            if status != 200:
                break
            items = data.get("items") or []
            out += [t for t in (parse_track(i) for i in items) if t]
            if not data.get("next") or not items:
                break
            offset += 50
        return out

    def playlist_tracks(self, playlist_id: str, most: int = 500) -> list[Track]:
        """A playlist's songs, in order (podcast episodes and local files left out)."""
        out, offset = [], 0
        while offset < most:
            status, data = self._call("GET", f"/playlists/{playlist_id}/tracks?limit=100&offset={offset}")
            if status == 401 or status == 403:
                raise PermissionError("sign in to Spotify again so the studio may read your playlists")
            if status == 404:
                raise LookupError("Spotify does not know that playlist")
            if status != 200:
                raise ConnectionError(self.last_error or f"Spotify answered {status}")
            items = data.get("items") or []
            out += [t for t in (parse_track((i or {}).get("track")) for i in items) if t and t.uri.startswith("spotify:track:")]
            if not data.get("next") or not items:
                break
            offset += 100
        return out

    def play_uris(self, device_id: str, uris: list[str]) -> bool:
        """Play these songs, in order, on one device (the studio's own player)."""
        return self._done("PUT", f"/me/player/play?device_id={device_id}", {"uris": list(uris)})

    def artist_genres(self, track: Track) -> list[str]:
        """Genres Spotify lists for the track's artists (per artist, not per song; often empty)."""
        out = []
        for aid in track.artist_ids[:3]:
            if aid not in self.artist_cache:
                status, data = self._call("GET", f"/artists/{aid}")
                self.artist_cache[aid] = list(data.get("genres") or []) if status == 200 else []
            out += [g for g in self.artist_cache[aid] if g not in out]
        return out

    def transfer_playback(self, device_id: str, play: bool = True) -> bool:
        """Make a device (the studio's own player) the one Spotify plays on."""
        return self._done("PUT", "/me/player", {"device_ids": [device_id], "play": bool(play)})

    def pause(self) -> bool:
        return self._done("PUT", "/me/player/pause")

    def resume(self) -> bool:
        return self._done("PUT", "/me/player/play")

    def resume_on(self, device_id: str) -> bool:
        """Play on this device (the studio's own player). A plain resume names no device: once Spotify had let
        a long-paused player go, it answered "no active device" and nothing played. This wakes it."""
        if self._done("PUT", f"/me/player/play?device_id={device_id}"):
            return True
        return self.transfer_playback(device_id, play=True)

    def next_track(self) -> bool:
        return self._done("POST", "/me/player/next")

    def previous_track(self) -> bool:
        """As Spotify's own back button: the song starts over, or (near its start) the one before plays."""
        return self._done("POST", "/me/player/previous")


# ============================================================================ the studio player
class PlayerState:
    """What the studio's own Spotify player last reported, pushed from its page."""

    def __init__(self, clock=time.monotonic):
        self.clock = clock
        self.device_id = ""
        self.ready = False
        self.error = ""
        self.track: Track | None = None
        self.position = 0.0
        self.paused = True
        self.at = 0.0                                    # local clock the position was taken at
        self.heard = 0.0                                 # local clock of the last report of any kind
        self.last_raw: dict = {}                         # the last state exactly as the page sent it (for diagnosis)
        self.window = None                               # the player window's process, if the studio opened it
        self._last_key = None                            # (_stale) the last report's track, position and stamp

    def update(self, report: dict) -> None:
        now = self.clock()
        self.heard = now
        if "device_id" in report:
            self.device_id = report["device_id"] or ""
            self.ready = bool(report.get("ready", bool(self.device_id)))
        if "error" in report:
            self.error = report["error"] or ""
        state = report.get("state")
        if isinstance(state, dict):
            self.last_raw = {k: state.get(k) for k in ("position_ms", "raw_position_ms", "stamp", "paused")}
        if state is not None and self._stale(state):
            return                                       # the same snapshot again: keep counting from the last one
        if state is not None:
            item = state.get("track") if isinstance(state, dict) else None
            if item:
                artists = item.get("artists") or []
                self.track = Track(id=item.get("id") or item.get("uri", "").rsplit(":", 1)[-1], name=item.get("name", ""),
                                   artists=[a.get("name", "") for a in artists], duration=float(item.get("duration_ms", 0)) / 1000.0,
                                   uri=item.get("uri", ""),
                                   artist_ids=[a.get("uri", "").rsplit(":", 1)[-1] for a in artists if a.get("uri")],
                                   image=pick_image((item.get("album") or {}).get("images")), art=largest_image((item.get("album") or {}).get("images")))
            else:
                self.track = None
            self.position = float(state.get("position_ms", 0)) / 1000.0 if isinstance(state, dict) else 0.0
            self.paused = bool(state.get("paused", True)) if isinstance(state, dict) else True
            self.at = now

    def _stale(self, state: dict) -> bool:
        """A report of a playing track whose snapshot is the one already held.

        The SDK repeats its last snapshot until something happens. Taking that
        at face value would set the clock back to the moment of the snapshot on
        every report, and the track would seem to jump back in time.
        """
        if not isinstance(state, dict) or state.get("paused", True) or self.track is None or self.paused:
            return False
        # Measured on the real player: every report carries a fresh timestamp and a current
        # position, so each one is taken as it is. Only a report with no timestamp that
        # repeats the last one exactly is ignored. (An earlier rule also ignored reports a
        # little behind the running estimate; it let the estimate creep 1.4 s ahead.)
        key = (state.get("track") or {}).get("id"), state.get("raw_position_ms", state.get("position_ms")), state.get("stamp")
        stale = key == self._last_key and state.get("stamp") is None
        self._last_key = key
        return stale

    @property
    def fresh(self) -> bool:
        return self.ready and self.at > 0 and self.clock() - self.heard < 5.0

    def to_json(self) -> dict:
        now_pos = self.position + (0.0 if self.paused else self.clock() - self.at)
        return {"ready": self.ready, "device_id": self.device_id, "error": self.error,
                "position": round(now_pos, 2), "last_raw": self.last_raw,
                "track": self.track.label if self.track else None, "fresh": self.fresh,
                "window_open": bool(self.window and self.window.poll() is None)}


class PlayerAwareSpotify:
    """The Spotify client, but with the studio player's own clock for what is playing.

    While the studio's player is the device playing, its reports arrive the moment
    anything changes and its position is exact, so they are used instead of asking
    the Web API once a second (whose answers are always a little stale). Anything
    else, and any time the player has gone quiet, falls through to the Web API.
    """

    def __init__(self, client: SpotifyClient, player: PlayerState):
        self.client, self.player = client, player

    def __getattr__(self, name):                        # queue, pause, resume, artist_genres, signed_in...
        return getattr(self.client, name)

    def now_playing(self) -> NowPlaying:
        p = self.player
        if p.fresh and p.track is not None:
            now = p.clock()
            progress = p.position + (0.0 if p.paused else now - p.at)
            return NowPlaying(p.track, progress, not p.paused, now, "Light Show Studio")
        return self.client.now_playing()
