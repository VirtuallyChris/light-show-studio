"""What kind of music this is, and so how hard the lights should go.

A light show for a rock song should not look like one for dubstep: rock lifts
and moves, dubstep flashes on the beat. The studio works this out once per
song and turns it into one of four show styles:

    chill    slow fades and drifting light; no strobes, no flashing
    groove   movement on the beat, but drops lift without flashing
    dance    drops flash on the beat; buildups strobe up to 1/16 notes
    rave     everything, hardest: 1/32 strobes, blasts, drum-fill rushes

The genre comes from a model that listens to the song (MAEST, trained on the
styles Discogs gives music, run by genre_infer.py in the model environment; MIT's
AudioSet model where MAEST cannot run, and in analyses saved before it) and, in
Jam mode, from the genres Spotify gives the artist. Each genre has a place on a
0 to 3 scale; the song's own drive (and AST's mood labels: exciting, angry,
tender, sad) can move it a little, but never far: a loud rock song is still a
rock song.
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
# Where each Discogs style (MAEST's labels, "Genre---Style") sits on the same scale; a style not named here
# takes its genre's place (DISCOGS_GENRE). Written from what the styles are, and checked on 60 of the user's
# songs with their own drive (Oct 2026): 52 got the right show style, against 49 from AudioSet's labels; on
# the 44 Jam songs not used to write it, 40 against 38.
DISCOGS_TIER = {
    "Dubstep": 3.0, "Drum n Bass": 3.0, "Jungle": 3.0, "Hardstyle": 3.0, "Hardcore": 3.0, "Gabber": 3.0, "Breakcore": 3.0,
    "Halftime": 3.0, "Speedcore": 3.0, "Happy Hardcore": 3.0, "Jumpstyle": 3.0, "Bass Music": 2.7, "Glitch Hop": 2.6,
    "Techno": 2.8, "Hard Techno": 2.9, "Hard Trance": 2.7, "Schranz": 2.9, "Minimal Techno": 2.3, "Deep Techno": 2.3,
    "Electro House": 2.5, "Big Beat": 2.3, "Breakbeat": 2.4, "Breaks": 2.4, "Bassline": 2.6, "Hard House": 2.5,
    "Trance": 2.4, "Progressive Trance": 2.4, "Psy-Trance": 2.6, "Goa Trance": 2.5, "Tech Trance": 2.5, "Hands Up": 2.5,
    "Eurodance": 2.2, "Progressive House": 2.3, "Tech House": 2.3, "House": 2.2, "Acid House": 2.2, "Euro House": 2.2,
    "Speed Garage": 2.2, "UK Garage": 2.0, "Electro": 2.0, "Deep House": 1.9, "Tropical House": 1.8, "Nu-Disco": 1.6,
    "Grime": 2.0, "Glitch": 2.0, "IDM": 1.5, "Dance-pop": 1.5, "Synth-pop": 1.3, "Synthwave": 1.4, "Electroclash": 1.6,
    "Chillwave": 0.9, "Downtempo": 0.6, "Trip Hop": 0.7, "Ambient": 0.1, "Dark Ambient": 0.1, "Experimental": 1.0,
    "Indie Pop": 1.0, "Pop Rock": 1.1, "Alternative Rock": 1.1, "Indie Rock": 1.1, "Pop Punk": 1.4, "Punk": 1.8,
    "Hard Rock": 1.5, "Heavy Metal": 1.9, "Metalcore": 1.9, "Post-Hardcore": 1.6, "Emo": 1.3, "Math Rock": 1.2,
    "K-pop": 1.4, "Europop": 1.4, "J-pop": 1.3, "Dream Pop": 0.8, "Shoegaze": 0.9, "Ballad": 0.4, "Vocal": 0.6,
    "Contemporary R&B": 0.9, "Neo Soul": 0.6, "Soul": 0.5, "Disco": 1.4, "Funk": 1.3, "Hip Hop": 1.3, "Trap": 1.6,
    "Pop Rap": 1.3, "Folk": 0.4, "Acoustic": 0.4, "Folk Rock": 0.6, "Country": 0.8, "Soft Rock": 0.7, "Lo-Fi": 0.5,
}
DISCOGS_GENRE = {"Electronic": 2.0, "Rock": 1.1, "Pop": 1.2, "Hip Hop": 1.3, "Funk / Soul": 0.9, "Jazz": 0.4,
                 "Classical": 0.2, "Folk, World, & Country": 0.5, "Blues": 0.5, "Latin": 1.2, "Reggae": 0.9,
                 "Non-Music": 0.0, "Stage & Screen": 0.5, "Children's": 0.5, "Brass & Military": 0.5}
MAEST_SURE = 1.0                  # MAEST's scores are higher than AST's (one song has several styles at 0.3-0.7)


def tier(name: str) -> float | None:
    """A label's place on the 0-3 scale: an AudioSet genre (AST) or a Discogs style (MAEST); None if neither."""
    if "---" in name:
        g, st = name.split("---", 1)
        return DISCOGS_TIER.get(st, DISCOGS_GENRE.get(g, MIDDLE))
    return TIER.get(name)


def shown(name: str) -> str:
    """A label as the studio shows it: a Discogs style without its genre ("Pop Rock", "Drum and bass")."""
    st = name.split("---", 1)[-1]
    return "Drum and bass" if st == "Drum n Bass" else st


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
MAX_NUDGE = 0.3                   # energy and mood can move a song this far along the scale, no further
LOUD_IS_NORMAL = 0.85             # nearly every modern master measures this driving: only beyond it is a song pushed up
SURE_WEIGHT = 0.4                 # genre scores adding up to this much are believed in full; less pulls towards groove
MIDDLE = 1.2                      # groove: where a song nothing much is known about sits


def spotify_level(genres: list[str]) -> float | None:
    levels = []
    for g in genres or []:
        g = g.lower()
        level = next((lv for words, lv in SPOTIFY_KEYWORDS if any(w in g for w in words)), None)
        if level is not None:
            levels.append(level)
    return float(np.mean(levels)) if levels else None


def _weighted_median(pairs: list[tuple[float, float]]) -> float:
    pairs = sorted(pairs)
    half, acc = sum(w for _, w in pairs) / 2, 0.0
    for v, w in pairs:
        acc += w
        if acc >= half:
            return v
    return pairs[-1][0]


def style_from(labels: dict | None, drive: float = 0.6, spotify_genres: list[str] | None = None) -> dict:
    """Turn genre scores (and whatever Spotify says) into a show style, with the reasons.

    Calibrated on 34 songs heard at a party (Oct 2026), where three songs in four came out dance or rave,
    indie rock bands included. Two reasons, both fixed here: (1) the genres were averaged, so a faint
    "dubstep 0.08" among pop, R&B and electronic guesses dragged a pop song up a style; now the weighted
    middle guess counts, and guesses that add up to little (the model unsure) are pulled towards groove;
    (2) drive was measured from 0.6, but nearly every modern master is 0.85 to 1.0, so nearly every song
    got the whole push up; now only drive beyond that counts, and the push is smaller.
    """
    labels = labels or {}
    genres = sorted(((n, p) for n, p in labels.items() if tier(n) is not None), key=lambda kv: -kv[1])
    top = [(n, p) for n, p in genres[:5] if p >= 0.05] or genres[:1]
    weight = sum(p for _, p in top)
    maest = any("---" in n for n, _ in genres)
    model_level = None
    if weight >= 0.03:
        sure = min(1.0, weight / (MAEST_SURE if maest else SURE_WEIGHT))
        model_level = sure * _weighted_median([(tier(n), p) for n, p in top]) + (1 - sure) * MIDDLE
    sp_level = spotify_level(spotify_genres)
    levels = [lv for lv in (model_level, sp_level) if lv is not None]
    mood = sum(labels.get(n, 0.0) * w for n, w in MOOD.items())
    nudge = float(np.clip((drive - LOUD_IS_NORMAL) * 1.2 + 0.5 * mood, -MAX_NUDGE, MAX_NUDGE))
    if levels:
        base = float(np.mean(levels))
        source = "+".join(s for s, lv in (("listening", model_level), ("spotify", sp_level)) if lv is not None)
    else:                                                # nothing to go on: groove, a little either way by drive
        base, source = MIDDLE, "energy"
    level = float(np.clip(base + nudge, 0.0, 3.0))
    style = STYLES[int(np.searchsorted(BOUNDS, level, side="right"))]
    return {"style": style, "level": round(level, 2), "source": source, "model": "maest" if maest else ("ast" if genres else None),
            "genres": [shown(n) for n, _ in top[:3] if labels.get(n, 0) >= 0.05],
            "spotify": list(spotify_genres or [])[:4],
            "scores": {n: round(p, 3) for n, p in genres[:8]}}


def restyle(genre: dict | None, drive: float) -> dict | None:
    """A genre saved by an earlier rule, decided again from what was heard (kept with it): songs learned
    before the rule changed get the style they would get now, without being analysed again."""
    if not genre or not genre.get("scores") or "listening" not in str(genre.get("source")):
        return genre
    g = style_from(genre["scores"], drive, genre.get("spotify"))
    g["genres"] = genre.get("genres") or g["genres"]
    return g


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
    g = analysis.get("genre")
    if g and g.get("source") != "energy" and "model" in g:
        return False                                     # (a genre from before MAEST, with no "model", is heard again)
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
