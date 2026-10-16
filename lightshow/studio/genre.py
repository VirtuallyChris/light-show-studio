"""What kind of music this is, and so how hard the lights should go.

A light show for a rock song should not look like one for dubstep: rock lifts
and moves, dubstep flashes on the beat. The studio works this out once per
song and turns it into one of four show styles:

    chill    slow fades and drifting light; no strobes, no flashing
    groove   movement on the beat, but drops lift without flashing
    dance    drops flash on the beat; buildups strobe up to 1/16 notes
    rave     everything, hardest: 1/32 strobes, blasts, drum-fill rushes

The genre comes from a model that listens to the song (MIT's Audio Spectrogram
Transformer, trained on AudioSet, run by genre_infer.py in the model
environment) and, in Jam mode, from the genres Spotify gives the artist. Each
genre has a place on a 0 to 3 scale; the song's own drive and the model's mood
labels (exciting, angry, tender, sad) can move it a little, but never far: a
loud rock song is still a rock song.
"""

from __future__ import annotations

import os


import numpy as np

from .model_worker import ModelWorker

STYLES = ("chill", "groove", "dance", "rave")

# Where each AudioSet genre sits, from 0 (ambient) to 3 (dubstep)
TIER = {
    "Dubstep": 3.0, "Drum and bass": 3.0, "Techno": 2.8,
    "Electronic dance music": 2.5, "Trance music": 2.4,
    "House music": 2.2, "Dance music": 2.1, "Electronica": 2.0, "Electronic music": 2.0,
    # heavy guitar music flashes, but even at its loudest it tops out at dance: rave is for bass music
    "Heavy metal": 1.9, "Punk rock": 1.8,
    "Hip hop music": 1.3, "Pop music": 1.3, "Disco": 1.4, "Funk": 1.3, "Ska": 1.3, "Afrobeat": 1.3, "Beatboxing": 1.3,
    "Music of Latin America": 1.3, "Salsa music": 1.3, "Music of Bollywood": 1.3, "Video game music": 1.3, "Wedding music": 1.2,
    "Rock music": 1.1, "Grunge": 1.1, "Rock and roll": 1.1, "Psychedelic rock": 1.0, "Progressive rock": 1.0, "Independent music": 1.0,
    "Reggae": 0.9, "Rhythm and blues": 0.9, "Country": 0.8, "Music of Africa": 1.0,
    "Soul music": 0.5, "Blues": 0.5, "Folk music": 0.4, "Jazz": 0.4, "Swing music": 0.6, "Bluegrass": 0.6, "Gospel music": 0.5,
    "Christian music": 0.5, "Vocal music": 0.4, "A capella": 0.3, "Flamenco": 0.6, "Middle Eastern music": 0.6, "Music of Asia": 0.6,
    "Carnatic music": 0.5, "Traditional music": 0.5, "Music for children": 0.5, "Christmas music": 0.5,
    "Ambient music": 0.1, "New-age music": 0.1, "Classical music": 0.2, "Opera": 0.2, "Lullaby": 0.0,
}
MOOD = {"Exciting music": 0.6, "Angry music": 0.6, "Happy music": 0.15, "Tender music": -0.6, "Sad music": -0.6, "Scary music": 0.1}

# Spotify's artist genres are free text ("melodic dubstep", "indie rock"); first match wins
SPOTIFY_KEYWORDS = [
    (("dubstep", "riddim", "brostep", "drum and bass", "dnb", "neurofunk", "hardstyle", "hardcore", "techno", "jungle", "bass music"), 3.0),
    (("edm", "big room", "electro", "trance", "future bass", "bass house", "house", "dance", "electronic", "metal", "punk"), 2.2),
    (("hip hop", "rap", "trap", "pop", "disco", "funk", "latin", "reggaeton", "k-pop", "afro"), 1.3),
    (("rock", "indie", "alternative", "grunge"), 1.1),
    (("r&b", "reggae", "country", "soul"), 0.8),
    (("jazz", "blues", "folk", "acoustic", "singer-songwriter", "gospel", "lo-fi", "lofi", "chill"), 0.4),
    (("ambient", "classical", "new age", "sleep", "piano", "orchestral", "soundtrack"), 0.1),
]
BOUNDS = (0.7, 1.6, 2.4)          # chill | groove | dance | rave
MAX_NUDGE = 0.45                  # energy and mood can move a song this far along the scale, no further


def spotify_level(genres: list[str]) -> float | None:
    levels = []
    for g in genres or []:
        g = g.lower()
        level = next((lv for words, lv in SPOTIFY_KEYWORDS if any(w in g for w in words)), None)
        if level is not None:
            levels.append(level)
    return float(np.mean(levels)) if levels else None


def style_from(labels: dict | None, drive: float = 0.6, spotify_genres: list[str] | None = None) -> dict:
    """Turn genre scores (and whatever Spotify says) into a show style, with the reasons."""
    labels = labels or {}
    genres = sorted(((n, p) for n, p in labels.items() if n in TIER), key=lambda kv: -kv[1])
    top = [(n, p) for n, p in genres[:5] if p >= 0.05] or genres[:1]
    weight = sum(p for _, p in top)
    model_level = sum(p * TIER[n] for n, p in top) / weight if weight >= 0.05 else None
    sp_level = spotify_level(spotify_genres)
    levels = [lv for lv in (model_level, sp_level) if lv is not None]
    if levels:
        base = float(np.mean(levels))
        mood = sum(labels.get(n, 0.0) * w for n, w in MOOD.items())
        nudge = float(np.clip((drive - 0.6) * 1.5 + mood, -MAX_NUDGE, MAX_NUDGE))
        source = "+".join(s for s, lv in (("listening", model_level), ("spotify", sp_level)) if lv is not None)
    else:                                                # nothing to go on: the old rule, by drive alone
        base, nudge, source = (2.1 if drive > 0.72 else 1.2), 0.0, "energy"
    level = float(np.clip(base + nudge, 0.0, 3.0))
    style = STYLES[int(np.searchsorted(BOUNDS, level, side="right"))]
    return {"style": style, "level": round(level, 2), "source": source,
            "genres": [n for n, _ in top[:3] if labels.get(n, 0) >= 0.05],
            "spotify": list(spotify_genres or [])[:4],
            "scores": {n: round(p, 3) for n, p in genres[:8]}}


# ------------------------------------------------------------------------ the worker
class GenreWorker(ModelWorker):
    """The genre model as a long-lived process; see model_worker.py."""

    def classify_file(self, path: str) -> dict | None:
        return self.ask_file(path)

    def classify_audio(self, y: np.ndarray, sr: int) -> dict | None:
        """Audio from memory, piped straight to the model: nothing is written to disk."""
        return self.ask_audio(y, sr)


WORKER = GenreWorker("lightshow.studio.genre_infer", "labels", start_timeout=600, request_timeout=120)


def add_genre(analysis: dict, path: str) -> bool:
    """Give an analysis its genre and style if it has none yet. Returns True if it changed."""
    if analysis.get("genre") and analysis["genre"].get("source") != "energy":
        return False
    drive = float((analysis.get("mood") or {}).get("drive", 0.6))
    labels = WORKER.classify_file(path)                  # (a model that failed is tried again every 2 minutes)
    problems = [p for p in analysis.get("problems") or [] if not p.startswith("the genre")]
    if labels is None:                                   # said where it can be read, even from a studio with no console
        from .model_worker import note
        why = WORKER.failed or WORKER.last_error or ("the model environment is not installed" if not WORKER.available() else "no answer")
        note(f"[genre] no genre for {os.path.basename(os.path.dirname(path))}: {why}")
        problems.append(f"the genre could not be heard ({why})")
    analysis["problems"] = problems
    if labels is None and analysis.get("genre"):
        return True                                      # still the energy guess, but what failed is noted
    analysis["genre"] = style_from(labels, drive)
    return True
