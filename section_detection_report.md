# Overnight: making the section detector as good as it can be

2026-10-19. Research and fixes; nothing cast, no lights. Everything below is measured against **your own
corrections**: 49 songs you reviewed, scored by the share of each song's time where the label matches yours,
and by drop vs. not-drop (what matters most to the lights).

## The short version

- **No model out there is close to 100% on this.** I re-checked the field tonight. SongFormer (2025) is still
  the state of the art for labeled song sections, and Beat This is still the beat tracker to beat. On your 49
  songs, used on their own:

  | | Section types right | Drop vs. not-drop right |
  |---|---|---|
  | SongFormer alone | 65.1% | 80.2% |
  | All-In-One alone | 65.7% | 80.7% |
  | **The studio's rules** (built on top of SongFormer, the stems and the beats) | **85.9%** | **92.1%** |
  | A model trained on your corrections (best of many tries) | 85.4–86.0% | 92.0–92.5% |

  The studio's rules are already far ahead of any published model on your music, by about 20 points.

- **The model trained on your corrections ties the rules but doesn't beat them, yet.** I gave it everything:
  - the stems' levels;
  - SongFormer's audio embeddings (MuQ + MusicFM) and label heads;
  - All-In-One's labels;
  - the lyrics (how often the words sung in each bar come back);
  - the song map.

  Every combination lands at 85.5–86% / 92–92.5%. With 49 songs it learns what the rules already know, but
  not your harder calls.

- **But it's still climbing.** Every training size below is scored on the same 49 songs, each one left out
  in turn:

  | Trained on | Section types | Drop vs. not-drop |
  |---|---|---|
  | 12 songs | 82.4% | 90.9% |
  | 24 songs | 84.5% | 91.2% |
  | 36 songs | 84.9% | 91.7% |
  | 47 songs | 85.6% | 92.3% |
  | *The rules* | *85.9%* | *92.1%* |

  Each doubling adds about 1.5 points on section types, and the curve hasn't flattened. At about 150 songs
  it should reach roughly 87–88%, which would be the first thing to beat the rules. That's a projection,
  not a promise.

- **Where the remaining 14% comes from:**
  - **About half is boundary placement:** a section starting a bar or two away from where you put it.
  - **About half is whole stretches:** mostly calling a drop where you hear a verse (4.3% of all time, four
    times as often as the reverse), and verse vs. breakdown (3.5%, both calm, so it matters little for the
    lights).
  - **It's concentrated:** 8 songs hold 44% of all the errors (Walking On A Dream, WET DREAM, Starry Eyed,
    WANTURBODY, Frisson, Dance Dance, I'm Good, Close To You).
  - **Every signal agrees and you differ:** the rules and the learned model agree 91% of the time and are
    right there 90%; even taking whichever is right at each bar would only reach 89.6%. The last stretch is
    calls where nothing measurable distinguishes your choice.

## The quarter-beat question

You saw a few songs off by a quarter beat. It came from three separate things, all fixed tonight and pushed:

1. **Light Switch drifted.** Its grid was 91.81 BPM, the song 92.1, so the lights slid 2.2 ms a second and
   went out of time and back every 80 s. The grid is now redrawn at the drums' tempo whenever the drums
   drift steadily against it. Only Light Switch needed it.
2. **Songs counted at half speed with the grid on the off-beat:** sleepover, WANTURBODY, Sad Machine. A grid
   half a real beat off looks like a quarter of a counted beat. The grid now moves onto the snare backbeat
   when the snares sit consistently between its half-beats. All-In-One's own beats independently landed in
   the same spot on both songs it covered.
3. **Every grid was 28 ms late** (fixed yesterday): it was fitted to librosa's onset curve, which peaks after
   each drum hit.

Across all 237 songs, All-In-One's beats agree with ours within 25 ms on 99% of the 177 steady songs where
both use the same tempo (median 10 ms apart). 50 songs are counted at half the tempo All-In-One hears;
that's on purpose, so fast songs don't strobe.

**Bar starts:** your section edges land on our bar starts 91% of the time and on All-In-One's 89%, so
neither is better overall. In two songs (Fix and The Sound) All-In-One consistently puts beat 1 two beats
from ours, and it's probably right about Fix. Too few to justify adding All-In-One to the studio. If a
song's bars feel off, use **Bar −1 / Bar +1** in the Beat grid menu.

## What changed in the studio tonight (all pushed)

- `cb27cd6` **A song you reviewed plays your sections.** Some songs can have no drops: Walking On A Dream and
  Oogie Boogie now play as you set them.
- `c6bbff2` The snare backbeat fix (item 2 above).
- `58f5491` A verse hidden in a long drop: in a drop of 45 s or more, a long stretch where the synths fall
  away is a verse (Frisson, WET DREAM).
- Lyrics redone for all 281 copies, with the timing fix: 229 from the lyrics site, 51 from Whisper.
- All-In-One run on all 237 songs, and SongFormer's embeddings saved for all 237 (`shows/_jam/features`).

## For tomorrow: the 100 songs

Your labels are the only thing that can push this past the rules, and they're also the only fair test: the
rules were tuned on some of these 49 songs, so they get an easy score here. After you've reviewed a batch:

    .venv\Scripts\python tools\section_labeler.py eval

That scores the learned labeler against the rules on all your reviewed songs, leaving each one out in turn.
If it pulls ahead, `train` saves it and I'll wire it into the studio, with the rules as the fallback. New
songs need their SongFormer features first:

    .venv-songformer\Scripts\python tools\songformer_features.py

**Review mode now starts with the songs that teach the most** (`2317757`): the ones where the learned
labeler and the rules disagree most (Cave In, Whiplash, superdeadfriends, What You Know, FUCKING BEST SONG
EVERRR…). It opens on the first song you haven't reviewed. After a batch, refresh the ranking with:

    .venv\Scripts\python tools\section_labeler.py order

Restart the studio first to pick up tonight's changes.

## Tried and dropped tonight

| Idea | Result |
|---|---|
| Boundary refiner (moves the rules' boundaries to the likeliest bar nearby) | 83.5% (worse) |
| Drop checker (is this drop section really a drop?) | never confident: only 12 of 135 drop sections are ones you'd reject |
| Labeling whole sections instead of bars | 83–85% |
| Learned order of sections (verse → buildup → drop) | no gain |
| Tilting toward fewer drops | +0.2 drop/not-drop at most |
| One label per repeated part | 89.5% drop/not-drop (worse) |

Sources checked tonight: [SongFormer](https://www.alphaxiv.org/abs/2510.02797),
[MuSFA](https://www.emergentmind.com/topics/music-structural-function-analysis-musfa),
[AlignBeat](https://arxiv.org/abs/2510.14391),
[Beat This / BeatFM / BeatMamba comparison](https://github.com/bolph71656-ai/HornScribe/issues/387).
