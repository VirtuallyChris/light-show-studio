"""A song's lyrics, word by word, each word at the moment it is sung on the song's own timeline.

Later, color words ("red", "gold"), big words ("fire", "lights", "jump") and the hook line will set off
light moments, so what matters most is *when* each word lands; the words themselves come from a lyrics
site wherever one has them, which is far more exact than any recognizer on sung vocals:

  1. The vocals pulled out of the mix with Demucs (the studio's own stems model, stems_infer.py).
  2. Whisper (faster-whisper) listens to the vocals: what it hears, with rough word times.
  3. The real lyrics from LRCLIB (lrclib.net: free and open, most songs synced line by line).
     The copy is often not the release Spotify plays (a music video's intro, a radio edit), so the
     lyrics' line times are moved onto the copy by matching their words to what Whisper heard.
  4. Each line forced onto the vocals with a CTC aligner (wav2vec2 MMS_FA, torchaudio) inside its
     own stretch of the song: a start and an end for every word.

A song LRCLIB does not have (or whose lyrics are not in the Latin alphabet, which the aligner cannot
read) keeps Whisper's own words. LRCLIB's "instrumental" songs get no words.

The output, one JSON file a song:

    {"words": [{"t": start_s, "end": end_s, "w": "word", "p": confidence 0-1, "bg": 1 (backing vocal,
                in parentheses in the lyrics; left out otherwise), "line": line index}],
     "lines": [{"t", "end", "text"}], "language": "en", "model": "...", "source": "lrclib" | "whisper" | "none",
     ...}

The parts that need no model (LRC parsing, matching, placing lines, writing) run anywhere; the rest
needs the model environment (.venv-songformer: CUDA PyTorch, Demucs, faster-whisper).
"""

from __future__ import annotations

import bisect
import difflib
import json
import os
import re
import statistics
import time
import unicodedata
import urllib.parse
import urllib.request

VERSION = 1
LRCLIB = "https://lrclib.net/api"
USER_AGENT = "LightShowStudio/1 (lyrics timing for home light shows)"
WHISPER = "large-v3-turbo"
ALIGNER = "mms_fa"
SR = 16000                       # what Whisper and the aligner listen at
FRAME_S = 320 / SR               # one aligner frame: 20 ms
HEAR_CONDITION = False           # Whisper reads its last words before the next: more words caught, but loops
WRONG_LYRICS = 0.15              # fewer of the lyrics' words heard than this: lyrics of another song (correct ones: 0.4-0.9)
GARBAGE_LOGP = -4.0             # the "something else is sung here" token at the edges of a line's stretch


# ---------------------------------------------------------------- words


_EDGE = re.compile(r"^[^\w']+|[^\w']+$")
_ONES = "zero one two three four five six seven eight nine ten eleven twelve thirteen fourteen fifteen " \
        "sixteen seventeen eighteen nineteen".split()
_TENS = "_ _ twenty thirty forty fifty sixty seventy eighty ninety".split()


def spoken_number(n: int) -> str:
    """0-99 as it is sung ("twenty two"); bigger numbers digit by digit."""
    if n < 20:
        return _ONES[n]
    if n < 100:
        return _TENS[n // 10] + ("" if n % 10 == 0 else " " + _ONES[n % 10])
    return " ".join(_ONES[int(d)] for d in str(n))


def clean(word: str) -> str:
    """A word as written, without the punctuation around it ("(hold" -> "hold", "right," -> "right")."""
    return _EDGE.sub("", word.replace("’", "'"))


def norm(word: str) -> str:
    """A word as the aligner reads it: lowercase a-z and apostrophes, accents dropped, digits spelled out."""
    w = unicodedata.normalize("NFKD", word.replace("’", "'").lower())
    w = re.sub(r"\d+", lambda m: spoken_number(int(m.group())), w)
    return re.sub(r"[^a-z']", "", w).strip("'")


def latin(text: str) -> bool:
    """True when nearly every letter is in the Latin alphabet (accents allowed): the aligner can read it."""
    letters = [c for c in text if c.isalpha()]
    if not letters:
        return True
    plain = sum(1 for c in letters if unicodedata.normalize("NFKD", c)[0].isascii())
    return plain >= 0.9 * len(letters)


def split_line(text: str) -> list[dict]:
    """A lyric line's words: [{"w": word, "bg": in parentheses}]. Hyphenated runs ("oh-oh-oh") stay one word."""
    out, depth = [], 0
    for raw in text.split():
        opens = raw.count("(") + raw.count("[")
        closes = raw.count(")") + raw.count("]")
        inside = depth > 0 or opens > 0
        depth = max(0, depth + opens - closes)
        w = clean(raw)
        if w:
            out.append({"w": w, "bg": inside})
    return out


# ---------------------------------------------------------------- LRC and LRCLIB


_STAMP = re.compile(r"\[(\d+):(\d+(?:[.:]\d+)?)\]")
_WORD_STAMP = re.compile(r"<(\d+):(\d+(?:\.\d+)?)>")
_TAG = re.compile(r"^\[[a-zA-Z]+:.*\]$")                  # [ar: ...], [length: ...]
_SECTION = re.compile(r"^[\[(][^\])]*[\])]$")            # "[Chorus]" on its own line


def _secs(m: str, s: str) -> float:
    return int(m) * 60 + float(s.replace(":", "."))


def parse_lrc(text: str) -> list[dict]:
    """Synced lyrics (LRC) -> [{"t": seconds, "text": line}], in time order, blank lines left out.

    Handles several stamps on one line ("[00:12.00][01:30.00]chorus line") and word stamps
    ("<00:12.40>word", the "enhanced" LRC), which are dropped from the text but kept as "word_t"."""
    lines = []
    for raw in (text or "").splitlines():
        raw = raw.strip()
        stamps = []
        while True:
            m = _STAMP.match(raw)
            if not m:
                break
            stamps.append(_secs(*m.groups()))
            raw = raw[m.end():].lstrip()
        if not stamps:
            continue
        word_t = [_secs(*m.groups()) for m in _WORD_STAMP.finditer(raw)]
        line = re.sub(r"\s+", " ", _WORD_STAMP.sub(" ", raw)).strip()
        if not line or _SECTION.match(line):
            continue
        for t in stamps:
            entry = {"t": round(t, 3), "text": line}
            if word_t and len(stamps) == 1:
                entry["word_t"] = word_t
            lines.append(entry)
    lines.sort(key=lambda x: x["t"])
    return lines


def plain_lines(text: str) -> list[dict]:
    """Unsynced lyrics -> [{"t": None, "text": line}], blank lines and "[Chorus]" labels left out."""
    out = []
    for raw in (text or "").splitlines():
        line = re.sub(r"\s+", " ", raw).strip()
        if line and not _SECTION.match(line) and not _TAG.match(line):
            out.append({"t": None, "text": line})
    return out


def http_get(url: str, timeout: float = 20.0) -> tuple[int, bytes]:
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()


def _simple(s: str) -> str:
    s = re.sub(r"\s*[\(\[][^)\]]*(feat|ft\.|with|remaster|version|edit|mix)[^)\]]*[\)\]]", "", s, flags=re.I)
    s = re.sub(r"\s+-\s+.*(remaster|version|edit|mix|live).*$", "", s, flags=re.I)
    return re.sub(r"[^a-z0-9]+", " ", unicodedata.normalize("NFKD", s.lower())).strip()


def pick_record(records: list[dict], title: str, artist: str, duration: float | None) -> dict | None:
    """The best LRCLIB record for a song: the right title and artist, the closest length, synced first."""
    want_t, want_a = _simple(title), _simple(artist)
    best, best_score = None, None
    for r in records or []:
        if not isinstance(r, dict) or not (r.get("syncedLyrics") or r.get("plainLyrics") or r.get("instrumental")):
            continue
        t_ok = difflib.SequenceMatcher(None, _simple(r.get("trackName") or ""), want_t).ratio()
        a_ok = difflib.SequenceMatcher(None, _simple(r.get("artistName") or ""), want_a).ratio()
        if want_a and want_a.split()[0] in _simple(r.get("artistName") or ""):
            a_ok = max(a_ok, 0.8)
        if t_ok < 0.7 or a_ok < 0.5:
            continue
        off = abs((r.get("duration") or 0) - duration) if duration else 0.0
        if duration and off > 20:
            continue
        score = (t_ok + a_ok) - off / 10 + (0.5 if r.get("syncedLyrics") else 0)
        if best_score is None or score > best_score:
            best, best_score = r, score
    return best


def lookup(title: str, artist: str, album: str = "", duration: float | None = None, get=http_get) -> dict | None:
    """A song's LRCLIB record ({"syncedLyrics", "plainLyrics", "instrumental", "id", ...}) or None."""
    q = {"track_name": title, "artist_name": artist}
    if album:
        q["album_name"] = album
    if duration:
        q["duration"] = str(round(duration))
    def ask(url):
        status, body = get(url)
        if status != 200:
            return []
        try:
            found = json.loads(body)
        except ValueError:
            return []
        return found if isinstance(found, list) else [found]

    rec = pick_record(ask(LRCLIB + "/get?" + urllib.parse.urlencode(q)), title, artist, duration)
    for query in ({"track_name": title, "artist_name": artist}, {"q": f"{artist} {_simple(title)}"}):
        if rec:
            break
        rec = pick_record(ask(LRCLIB + "/search?" + urllib.parse.urlencode(query)), title, artist, duration)
    return rec


def lyric_lines(record: dict | None) -> tuple[list[dict], bool]:
    """(lines, synced) from an LRCLIB record."""
    if not record:
        return [], False
    synced = parse_lrc(record.get("syncedLyrics") or "")
    if synced:
        return synced, True
    return plain_lines(record.get("plainLyrics") or ""), False


# ---------------------------------------------------------------- lyrics onto the copy


def _lyric_tokens(lines: list[dict]) -> list[tuple[int, int, str]]:
    """(line index, word index in the line, normalized word), every word of the lyrics in order."""
    out = []
    for li, line in enumerate(lines):
        for wi, w in enumerate(split_line(line["text"])):
            n = norm(w["w"])
            if n:
                out.append((li, wi, n))
    return out


def match_heard(lines: list[dict], heard: list[dict]) -> dict[tuple[int, int], float]:
    """Lyric words found among the words Whisper heard, in order: {(line, word): heard time}.

    Only runs of two or more words in a row count, so a lone "the" or "you" cannot pin a line."""
    lyr = _lyric_tokens(lines)
    hw = [norm(h["w"]) for h in heard]
    sm = difflib.SequenceMatcher(None, [x[2] for x in lyr], hw, autojunk=False)
    found = {}
    for a, b, size in sm.get_matching_blocks():
        if size < 2:
            continue
        for k in range(size):
            li, wi, _ = lyr[a + k]
            found[(li, wi)] = heard[b + k]["t"]
    return found


NEAR_S = 8.0                     # how far from where the lyrics put a line it is looked for in what Whisper heard
AGREE_S = 1.5                    # how close to a candidate offset's place a line must be heard to agree with it
PER_WORD_S = 0.3                 # a guess at how long a word takes, to go back from a matched word to the line start


def _found_near(toks: list[tuple[int, str]], line_t: float, heard: list[dict], hw: list[str], at: float,
                radius: float, prefer: float) -> float | None:
    """A line looked for in what Whisper heard within `radius` of `at`: the offset that puts it there
    (the one nearest `prefer` if it was heard more than once), or None. Two words in a row must match."""
    lo = bisect.bisect_left([h["t"] for h in heard], at - radius)
    near = [k for k in range(lo, len(heard)) if heard[k]["t"] <= at + radius]
    if len(toks) < 2 or not near:
        return None
    sm = difflib.SequenceMatcher(None, [n for _, n in toks], [hw[k] for k in near], autojunk=False)
    cands = [heard[near[b]]["t"] - PER_WORD_S * toks[a][0] - line_t for a, b, size in sm.get_matching_blocks() if size >= 2]
    return min(cands, key=lambda d: abs(d - prefer)) if cands else None


def line_offsets(lines: list[dict], found: dict, heard: list[dict] = (), slack: float = 0.0) -> tuple[list[float], float | None]:
    """For synced lyrics: how far each line's time must move to land on the copy.

    First one offset for the whole song (a copy with an intro moves every line the same). The lines
    whose first word Whisper heard, matched over the whole song, give the candidates; that matching
    can pair a chorus with the wrong chorus, often enough to outvote the right pairs (In Camera:
    26.5 s off), so each candidate (and no offset at all) is tried on every line, each looked for
    only close to where the candidate puts it, and the one most lines agree with wins (a tie: the
    smallest). Then each line is looked for again within NEAR_S (plus `slack`: how much the copy's
    length differs from the release's) of where that offset puts it; each line takes the median of
    the offsets near it (an edit with a part cut out moves the lines before and after the cut
    differently)."""
    samples = [t - lines[li]["t"] for (li, wi), t in found.items() if wi == 0]
    if not samples:
        return [0.0] * len(lines), None
    heard = sorted(heard, key=lambda h: h["t"])
    hw = [norm(h["w"]) for h in heard]
    toks = []
    for line in lines:
        tk = [(wi, norm(w["w"])) for wi, w in enumerate(split_line(line["text"]))]
        toks.append([(wi, n) for wi, n in tk if n])

    def agree(off: float) -> list[float]:
        ds = (_found_near(tk, ln["t"], heard, hw, ln["t"] + off, AGREE_S, off) for tk, ln in zip(toks, lines))
        return [d for d in ds if d is not None]

    cands = sorted({round(d, 1) for d in samples} | {0.0}, key=abs)
    votes = max((agree(c) for c in cands), key=len) if heard else []         # max keeps the first (smallest) of a tie
    overall = statistics.median(votes) if votes else statistics.median(samples)
    local = []                                            # (line time, offset) from the second look
    for tk, line in zip(toks, lines):
        d = _found_near(tk, line["t"], heard, hw, line["t"] + overall, NEAR_S + slack, overall)
        if d is not None:
            local.append((line["t"], d))
    out = []
    for line in lines:
        ds = [d for t0, d in local if abs(t0 - line["t"]) <= 40]
        out.append(statistics.median(ds) if len(ds) >= 3 else overall)
    return out, overall


def place_lines(lines: list[dict], synced: bool, found: dict, duration: float, heard: list[dict] = (),
                slack: float = 0.0) -> tuple[list[float], float | None]:
    """Each line's start on the copy's timeline. Synced lyrics: their own time moved by its offset.
    Plain lyrics: the time Whisper heard the line's first matched word, less a little for each word
    before it; lines Whisper did not catch are spread between their neighbors by word count."""
    if synced:
        offs, overall = line_offsets(lines, found, heard, slack)
        return [max(0.0, min(duration, ln["t"] + d)) for ln, d in zip(lines, offs)], overall
    starts: list[float | None] = []
    for li in range(len(lines)):
        hits = sorted((wi, t) for (l2, wi), t in found.items() if l2 == li)
        starts.append(max(0.0, hits[0][1] - 0.35 * hits[0][0]) if hits else None)
    n_words = [max(1, len(split_line(ln["text"]))) for ln in lines]
    known = [i for i, s in enumerate(starts) if s is not None]
    if not known:
        return [], None
    for i in range(len(lines)):
        if starts[i] is not None:
            continue
        prev = max((k for k in known if k < i), default=None)
        nxt = min((k for k in known if k > i), default=None)
        if prev is not None and nxt is not None:
            span = sum(n_words[prev:nxt])
            starts[i] = starts[prev] + (starts[nxt] - starts[prev]) * sum(n_words[prev:i]) / span
        elif prev is not None:
            starts[i] = starts[prev] + 0.4 * sum(n_words[prev:i])
        else:
            starts[i] = max(0.0, starts[nxt] - 0.4 * sum(n_words[i:nxt]))
    out, last = [], 0.0
    for s in starts:                                       # keep them in order
        last = max(last, min(duration, s))
        out.append(last)
    return out, None


def windows(starts: list[float], lines: list[dict], duration: float) -> list[tuple[float, float]]:
    """The stretch of the song each line is aligned in: from a little before it starts to a little after
    the next line starts, but never longer than its words could take (a line before a long instrumental)."""
    out = []
    for i, s in enumerate(starts):
        n = max(1, len(split_line(lines[i]["text"])))
        nxt = starts[i + 1] if i + 1 < len(starts) else duration
        end = min(nxt + 0.4, s + 2.0 + 0.9 * n, s + 30.0, duration)
        out.append((max(0.0, s - 0.6), max(end, min(duration, s + 0.5))))
    return out


LEVEL_FPS = 50
QUIET = 0.04                     # of the song's loud vocals (95th percentile): below it there is no singing


def vocal_level(vocals) -> list[float]:
    """The vocal stem's loudness, LEVEL_FPS frames a second (RMS)."""
    import numpy as np
    hop = SR // LEVEL_FPS
    n = len(vocals) // hop
    return np.sqrt((np.asarray(vocals[: n * hop], dtype=np.float32).reshape(n, hop) ** 2).mean(axis=1)).tolist()


def audible(words: list[dict], level: list[float]) -> list[dict]:
    """Only the words sung where the vocals are actually sounding (the loudest moment of the word's span
    at least QUIET of the song's loud vocals)."""
    if not level:
        return words
    loud = sorted(level)[int(0.95 * (len(level) - 1))] or 1.0
    out = []
    for w in words:
        a, b = int(w["t"] * LEVEL_FPS), max(int(w["t"] * LEVEL_FPS) + 1, int(w["end"] * LEVEL_FPS) + 1)
        if max(level[a:b], default=0.0) >= QUIET * loud:
            out.append(w)
    return out


def hushed(words: list[dict], level: list[float]) -> list[dict]:
    """Aligned words that landed where the vocals are silent are kept (in order, between their
    neighbors) but get confidence 0: the aligner had to put them somewhere."""
    loud = set(map(id, audible(words, level)))
    for w in words:
        if id(w) not in loud:
            w["p"] = 0.0
    return words


# ---------------------------------------------------------------- the result


def heard_only(heard: list[dict]) -> list[dict]:
    """Whisper's own words as the output's words (a song with no lyrics found)."""
    return [{"t": round(h["t"], 3), "end": round(h["end"], 3), "w": h["w"], "p": round(h["p"], 3)} for h in heard if h["w"]]


def tidy(words: list[dict]) -> list[dict]:
    """Words in time order with no word starting before the one before it (overlapping line stretches)."""
    last = 0.0
    for w in words:
        w["t"] = round(max(w["t"], last), 3)
        w["end"] = round(max(w["end"], w["t"]), 3)
        last = w["t"]
    return words


def result(words: list[dict], lines: list[dict], **info) -> dict:
    return {"version": VERSION, "made_at": time.strftime("%Y-%m-%d %H:%M"), **info, "words": words, "lines": lines}


def write(path: str, data: dict) -> None:
    """Written whole or not at all (a half-written file would be read as a song with no lyrics)."""
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, separators=(",", ":"))
    os.replace(tmp, path)


def read(path: str) -> dict | None:
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


# ---------------------------------------------------------------- the models (model environment only)


class Models:
    """Demucs, Whisper and the aligner, loaded once for many songs (about 5 GB of the card together)."""

    def __init__(self, device: str | None = None, whisper: str = WHISPER):
        import torch
        self.torch = torch
        self.device = device or ("cuda" if torch.cuda.is_available() else "cpu")
        from . import stems_infer
        self.stems_infer = stems_infer
        self.demucs, _ = stems_infer.load_model()
        if self.device == "cpu":
            self.demucs.to("cpu")
        from faster_whisper import WhisperModel
        self.whisper_name = whisper
        self.whisper = WhisperModel(whisper, device=self.device, compute_type="float16" if self.device == "cuda" else "int8")
        import torchaudio
        bundle = torchaudio.pipelines.MMS_FA
        self.aligner = bundle.get_model(with_star=False).to(self.device).eval()
        self.labels = bundle.get_dict(star=None)
        self.torchaudio = torchaudio

    def vocals(self, path: str):
        """(the vocal stem at 16 kHz mono, the song's length in seconds), on the copy's own timeline."""
        import librosa
        import numpy as np
        import soundfile as sf
        y, sr = sf.read(path, dtype="float32", always_2d=True)
        stems, _ = self.stems_infer.separate(self.demucs, self.device if self.device != "cuda" else "cuda:0", y.T, sr)
        v = librosa.resample(stems["vocals"], orig_sr=self.stems_infer.OUT_SR, target_sr=SR).astype(np.float32)
        return v, len(y) / sr

    def hear(self, vocals, condition: bool = HEAR_CONDITION) -> tuple[list[dict], str]:
        """What Whisper hears in the vocals: ([{"t", "end", "w", "p"}], language).

        No voice-activity filter: it is trained on speech and throws singing away (Seaweed Song: one
        word with it, 227 without). Instead, words "heard" where the vocal stem is all but silent are
        dropped: what Whisper makes up in the quiet."""
        segments, info = self.whisper.transcribe(vocals, word_timestamps=True, vad_filter=False, beam_size=5,
                                                 condition_on_previous_text=condition)
        words = []
        for seg in segments:
            for w in seg.words or []:
                text = clean(w.word.strip())
                if text:
                    words.append({"t": float(w.start), "end": float(w.end), "w": text, "p": float(w.probability)})
        return audible(words, vocal_level(vocals)), info.language

    def emissions(self, vocals):
        """The aligner's letter log-probabilities over the whole song, 50 frames a second (in 30 s pieces)."""
        torch = self.torch
        piece, pad = 30 * SR, 1 * SR
        out = []
        with torch.inference_mode():
            for s in range(0, len(vocals), piece):
                a, b = max(0, s - pad), min(len(vocals), s + piece + pad)
                x = torch.from_numpy(vocals[a:b]).to(self.device)[None]
                e, _ = self.aligner(x)
                e = e[0].float().cpu()
                lead = round((s - a) / 320)
                keep = round((min(len(vocals), s + piece) - s) / 320)
                out.append(e[lead:lead + keep])
        return torch.cat(out)

    def align_line(self, emission, t0: float, t1: float, words: list[str]) -> list[dict | None]:
        """Each word's {"t", "end", "p"} inside [t0, t1], or None for a word the aligner cannot spell."""
        torch, F = self.torch, self.torchaudio.functional
        f0, f1 = int(t0 / FRAME_S), min(len(emission), int(t1 / FRAME_S) + 1)
        spellable = [(i, norm(w)) for i, w in enumerate(words)]
        spellable = [(i, n) for i, n in spellable if n]
        out: list[dict | None] = [None] * len(words)
        if not spellable or f1 - f0 < 2:
            return out
        garbage = len(self.labels)                       # one extra column: "something else is sung here"
        em = emission[f0:f1]
        em = torch.cat([em, torch.full((len(em), 1), GARBAGE_LOGP)], dim=1)
        ids = [garbage]
        spans = []
        for _, n in spellable:
            a = len(ids)
            ids += [self.labels[c] for c in n]
            spans.append((a, len(ids)))
        ids.append(garbage)
        if len(ids) * 2 > len(em):                        # more letters than the stretch can hold: no alignment
            return out
        labels, scores = F.forced_align(em[None], torch.tensor([ids], dtype=torch.int32), blank=0)
        labels, scores = labels[0].tolist(), scores[0].exp().tolist()
        # each token's frames: walk the path, a new token wherever a non-blank label starts
        token_frames: list[list[int]] = []
        prev = 0
        for f, lab in enumerate(labels):
            if lab != 0 and lab != prev:
                token_frames.append([f])
            elif lab != 0:
                token_frames[-1].append(f)
            prev = lab
        if len(token_frames) != len(ids):
            return out
        for (i, _), (a, b) in zip(spellable, spans):
            frames = [f for tf in token_frames[a:b] for f in tf]
            p = sum(scores[f] for f in frames) / len(frames)
            out[i] = {"t": (f0 + frames[0]) * FRAME_S, "end": (f0 + frames[-1] + 1) * FRAME_S, "p": p}
        return out


def lyrics_for(models: Models, path: str, title: str, artist: str, album: str = "",
               duration: float | None = None, record: dict | None = None, get=http_get) -> dict:
    """One song's lyrics with word times. `duration` is the release's length (Spotify's), for the lookup."""
    started = time.monotonic()
    vocals, length = models.vocals(path)
    heard, language = models.hear(vocals)
    if record is None:
        record = lookup(title, artist, album, duration, get=get) if title else None
    info = {"title": title, "artist": artist, "duration": round(length, 3), "language": language,
            "lrclib_id": record.get("id") if record else None}
    if record and record.get("instrumental"):
        return result([], [], source="none", model="lrclib", instrumental=True, seconds=round(time.monotonic() - started, 1), **info)
    lines, synced = lyric_lines(record)
    found = match_heard(lines, heard) if lines else {}
    n_lyric = len(_lyric_tokens(lines))
    agreement = len(found) / n_lyric if n_lyric else 0.0
    wrong = len(heard) >= 40 and agreement < WRONG_LYRICS
    usable = lines and latin(" ".join(ln["text"] for ln in lines)) and (synced or found) and not wrong
    if usable:
        slack = abs(length - record["duration"]) if record.get("duration") else 0.0
        starts, offset = place_lines(lines, synced, found, length, heard, min(slack, 60.0))
        usable = bool(starts)
    if not usable:
        why = "no lyrics found" if not lines else "lyrics not in the Latin alphabet" if not latin(" ".join(ln["text"] for ln in lines)) \
            else "Whisper heard none of the lyrics"
        return result(heard_only(heard), [], source="whisper", model=f"faster-whisper {models.whisper_name}", why=why,
                      seconds=round(time.monotonic() - started, 1), **info)
    emission = models.emissions(vocals)
    words, out_lines = [], []
    for li, ((t0, t1), line) in enumerate(zip(windows(starts, lines, length), lines)):
        parts = split_line(line["text"])
        timed = models.align_line(emission, t0, t1, [p["w"] for p in parts])
        first = len(words)
        for k, (p, tm) in enumerate(zip(parts, timed)):
            if tm is None:                               # unspellable ("&", a symbol): between its neighbors
                nb = next((x for x in timed[k + 1:] if x), None)
                t = words[-1]["end"] if len(words) > first else (nb["t"] if nb else starts[li])
                tm = {"t": t, "end": nb["t"] if nb else t, "p": 0.0}
            w = {"t": tm["t"], "end": tm["end"], "w": p["w"], "p": round(tm["p"], 3), "line": li}
            if p["bg"]:
                w["bg"] = 1
            words.append(w)
        if len(words) > first:
            out_lines.append({"t": round(words[first]["t"], 3), "end": round(words[-1]["end"], 3), "text": line["text"]})
        else:
            out_lines.append({"t": round(starts[li], 3), "end": round(starts[li], 3), "text": line["text"]})
    tidy(words)
    hushed(words, vocal_level(vocals))
    return result(words, out_lines, source="lrclib", synced=synced, model=f"lrclib + {ALIGNER} (lines placed with faster-whisper {models.whisper_name})",
                  offset=round(offset, 3) if offset is not None else None, agreement=round(agreement, 3),
                  seconds=round(time.monotonic() - started, 1), **info)
