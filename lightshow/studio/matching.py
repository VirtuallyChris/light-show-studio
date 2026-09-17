"""Finding one piece of audio inside another, by ear.

Two jobs in the studio come down to this: measuring how late the room hears the
speakers (a microphone recording, found inside the audio that was sent), and
placing a known song on the jam timeline (the opening of the song, found inside
what was captured).

What makes it work on real dance music, each point learned by getting it wrong:

  * Attacks, not waveforms. Room reverb and speaker EQ wreck a waveform match but
    leave the rhythm of attacks intact.
  * Compressed magnitudes, log(1 + g*S), not decibels. In decibels a flicker in a
    nearly silent band counts as much as a kick drum, so ordinary room noise
    drowns the features: the true alignment scored 0.25 where it should be 0.97.
  * What was played as well as when. Attacks alone make every beat of a loop look
    the same, and the match is then a coin toss between beats (measured: exactly
    one in three right, with three beats in reach). A fine-grained picture of the
    low end, where the bass line moves, tells one beat from the next.
  * Normalised correlation. A plain correlation scores loud stretches higher
    whether they match or not, which near a drop drags the answer a beat toward
    the loud side, consistently and confidently.
  * Knowing when not to answer. Some music really is the same a beat later. Then
    the runner-up scores within a whisker of the winner, and that is reported
    (`edge`) so the caller can listen again rather than guess. Over 142 trials
    at five tempos with heavy noise: 97% right outright, and none wrong among
    those that cleared the edge test.
"""

from __future__ import annotations

from functools import lru_cache

import numpy as np

CLEAR_EDGE = 0.03            # the winner must beat any rival elsewhere by this fraction to be believed
GAMMA = 5.0


@lru_cache(maxsize=8)
def _filters(sr: int):
    import librosa

    return (librosa.filters.mel(sr=sr, n_fft=1024, n_mels=40, fmin=40, fmax=min(8000, sr // 2)),
            librosa.filters.mel(sr=sr, n_fft=4096, n_mels=24, fmin=35, fmax=1500))


def match_features(y, sr: int, hop: int) -> np.ndarray:
    """What a stretch of audio looks like for matching: shape (32, frames).

    Eight rows of attacks (compressed spectral flux in bands up to 8 kHz, spread
    over about 60 ms so the result does not depend on which sample the audio
    happened to start on) and 24 rows of low-end content at fine pitch
    resolution. The two views are scaled to count equally.
    """
    import librosa

    y = np.asarray(y, dtype=np.float32)
    mel, low = _filters(int(sr))
    S = mel @ (np.abs(librosa.stft(y, n_fft=1024, hop_length=hop)) ** 2)
    C = np.log1p(GAMMA * S / (np.percentile(S, 99) + 1e-12))
    flux = np.maximum(0.0, np.diff(C, axis=1, prepend=C[:, :1]))
    flux = np.stack([flux[i:i + 5].sum(axis=0) for i in range(0, 40, 5)])
    width = max(3, int(round(0.06 * sr / hop)) | 1)
    kernel = np.hanning(width + 2)[1:-1]
    kernel /= kernel.sum()
    flux = np.stack([np.convolve(row, kernel, mode="same") for row in flux])
    L = low @ (np.abs(librosa.stft(y, n_fft=4096, hop_length=hop)) ** 2)
    content = np.log1p(GAMMA * L / (np.percentile(L, 99) + 1e-12))
    n = min(flux.shape[1], content.shape[1])
    flux, content = flux[:, :n], content[:, :n]
    flux = flux - flux.mean(axis=1, keepdims=True)
    content = content - content.mean(axis=1, keepdims=True)
    return np.vstack([flux / (np.linalg.norm(flux) + 1e-12) * np.sqrt(n), content / (np.linalg.norm(content) + 1e-12) * np.sqrt(n)])


def normalised_match(search: np.ndarray, template: np.ndarray) -> np.ndarray:
    """Normalised cross-correlation of `template` at every place it fits wholly inside `search`.

    Both are (bands, frames) or (frames,). Element k is the match with the
    template starting at frame k of the search: 1.0 is identical, 0 unrelated.
    """
    from scipy.signal import fftconvolve

    S, T = np.atleast_2d(np.asarray(search, dtype=np.float64)), np.atleast_2d(np.asarray(template, dtype=np.float64))
    n = T.shape[1]
    if S.shape[1] < n or n < 8:
        return np.zeros(0)
    S = S - S.mean(axis=1, keepdims=True)
    T = T - T.mean(axis=1, keepdims=True)
    num = sum(fftconvolve(S[b], T[b][::-1], mode="valid") for b in range(len(T)))
    energy = np.concatenate([[0.0], np.cumsum((S ** 2).sum(axis=0))])
    local = energy[n:] - energy[:-n]                                   # energy of the search under each placement
    return num / (np.sqrt(np.maximum(local, 1e-12)) * np.sqrt((T ** 2).sum()) + 1e-12)


def best_match(search: np.ndarray, template: np.ndarray, guard_frames: int) -> dict:
    """Where the template fits best, and how clearly.

    `z` is how far the winner stands above the general run of scores; `edge` is
    how far it stands above the best rival more than `guard_frames` away.
    """
    m = normalised_match(search, template)
    if len(m) == 0:
        return {"index": 0, "peak": 0.0, "z": 0.0, "edge": 0.0, "scores": m}
    k = int(np.argmax(m))
    rivals = np.concatenate([m[:max(0, k - guard_frames)], m[k + guard_frames + 1:]])
    rival = float(rivals.max()) if rivals.size else 0.0
    return {"index": k, "peak": float(m[k]), "z": float((m[k] - m.mean()) / (m.std() + 1e-9)),
            "edge": float((m[k] - rival) / (abs(m[k]) + 1e-9)), "scores": m}
