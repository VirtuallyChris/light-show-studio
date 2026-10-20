# Song structure models: is there a better one than SongFormer?

Research only: nothing in the studio was changed. 2026-10-18.

## Bottom line

**No. Nothing open is vastly more accurate than SongFormer.** It is still the published state of the art for
sections with labels (2025-26), and none of the candidates got all of the user's problem songs right on its own.
Keep SongFormer.

Most of tonight's wrong sections did not come from SongFormer. The studio takes its section *labels* from
the loudness detector and uses SongFormer only to *time* the boundaries (`structure_model.py`). On the
flagged songs SongFormer's own labels were much closer to the truth than the labels the studio kept:

| Song | What the studio found (before settle_sections) | What raw SongFormer said |
|---|---|---|
| Madeon - Enjoy | one drop from 28 to 132 s | drops at 47, 85 and 151 (truth: 47, 92, 151) |
| little image - LUNGS BURN | verse from 0 to 129 s | drops at 50 and 109 (truth: about 52.6 and 111.6) |
| The Wldlfe - A Boy On A Phone | no drop near 51 s | drop at 50.9 (truth: 51.0) |

So the improvement worth making is to **let the models vote on labels** instead of swapping the model.
**All-In-One** is the one worth adding as a second opinion. It is a different architecture trained on
different data, it is tiny (11 MB of weights, under 1 GB of GPU memory), and it reuses the Demucs stems the
studio already makes. Run raw, it got 5 of the 7 known drop starts, against SongFormer's 3. It was the only
model to put both The Maine (54.0) and The Wldlfe (51.0) on the exact bar. This is a recommendation to try
it in a branch, not proof that it is better: there are only four songs with truth (see "How far to trust this").

## What was tested

Truth, from the user's corrections (Everything Goes On has no copy on disk, so it was left out; Bombs Away
and Knock Yourself Out XD have no truth and are shown for comparison only):

- **Enjoy**: the full layout from 28.5 to 170 s (9 boundaries, drops at 47, 92 and 151).
- **LUNGS BURN**: drops at 52.6-69.8 and 111.6-129.
- **how to exit a room**: the first drop is at 54.0. **A Boy On A Phone**: the first drop is at 51.0.

Measures (a bar is 2.1 to 3.4 s in these songs):

- **Boundaries**: how many true boundaries have a predicted one within a bar. "Extra" counts predicted
  boundaries in Enjoy that match no true one.
- **Drop starts**: how many true drop starts have a predicted drop starting within a bar.
- **Drop/not-drop time**: the share of the covered span where "is it a drop?" matches the truth.
- **Label time**: the same for all labels (Enjoy only).
- **First drop**: the song's first predicted drop, counted right only if it lands on the right bar (within
  a quarter bar).

Labels were mapped to the studio's types: chorus, inst and solo became drop; pre-chorus and build became
buildup; bridge and break became breakdown. EDMFormer has no verse label, so its buildups longer than 8 bars
were counted as verses. "+ settle_sections" means the model's sections were handed to the studio's current
rules in place of the loudness detector's sections.

## Results

| Candidate | Enjoy boundaries | Enjoy drop starts | Enjoy drop/not-drop time | Enjoy label time | LUNGS boundaries | LUNGS drop starts | LUNGS drop/not-drop time | Maine 1st drop (54.0) | Wldlfe 1st drop (51.0) | Drop starts, all 4 songs |
|---|---|---|---|---|---|---|---|---|---|---|
| Studio, sections as found (loudness labels, SongFormer timing) | 5/9 (+0 extra) | 0/3 | 65% | 65% | 1/4 | 0/2 | 65% | 57.1 (+3.1 s) | 121.1 (+70.1 s) | **0/7** |
| Studio now (found + settle_sections) | 9/9 (+0 extra) | 3/3 | 99% | 99% | 3/4 | 2/2 | 100% | 19.9 (-34.1 s) | 51.0 (hit) | **6/7** |
| SongFormer raw (as the studio runs it) | 4/9 (+3 extra) | 2/3 | 78% | 55% | 0/4 | 0/2 | 85% | 51.0 (-3.0 s) | 50.9 (hit) | **3/7** |
| SongFormer, +-6 s decoder window | 5/9 (+3 extra) | 3/3 | 81% | 54% | 0/4 | 0/2 | 85% | 51.0 (-3.0 s) | 50.9 (hit) | **4/7** |
| SongFormer, dataset prefix 6 | 5/9 (+2 extra) | 3/3 | 81% | 58% | 2/4 | 1/2 | 85% | 53.9 (hit) | 34.0 (-17.0 s) | **5/7** |
| EDMFormer (long buildups = verse) | 5/9 (+2 extra) | 2/3 | 96% | 73% | 0/4 | 0/2 | 35% | 7.5 (-46.5 s) | 51.1 (hit) | **3/7** |
| EDMFormer, +-6 s window | 6/9 (+2 extra) | 2/3 | 88% | 60% | 0/4 | 0/2 | 35% | 7.5 (-46.5 s) | 42.6 (-8.4 s) | **2/7** |
| All-In-One (8-fold ensemble) | 3/9 (+1 extra) | 2/3 | 83% | 68% | 1/4 | 1/2 | 85% | 54.0 (hit) | 51.0 (hit) | **5/7** |
| All-In-One + settle_sections | 5/9 (+1 extra) | 3/3 | 96% | 90% | 1/4 | 1/2 | 85% | 54.0 (hit) | 51.0 (hit) | **6/7** |
| SongFormer raw + settle_sections | 3/9 (+3 extra) | 2/3 | 78% | 68% | 1/4 | 1/2 | 85% | 51.0 (-3.0 s) | 51.0 (hit) | **4/7** |
| MSAF OLDA + 2D-FMC (best MSAF) | 5/9 (+4 extra) | 0/3 | 41% | 0% | 4/4 | 0/2 | 65% | no drop | no drop | n/a (no labels) |
| MSAF SF + 2D-FMC (default) | 3/9 (+3 extra) | 0/3 | 41% | 0% | 1/4 | 0/2 | 65% | no drop | no drop | n/a (no labels) |

The section-by-section output of every candidate is at the end of this report.

### Each candidate

**SongFormer** (ASLP-lab, 2025; what the studio runs). On the authors' benchmarks it is best at boundaries
and labels: Harmonix ACC 0.80 and HR.5F 0.70, against All-In-One's 0.74 and 0.60. Two things limit it here:

- Its decoder keeps a boundary only if it is the strongest one within +-12 s. Two boundaries closer than
  about 12 s can never both come out, so Enjoy's 5 s breakdown and 4 s build are impossible by
  construction. A +-6 s window fixed Enjoy's drop starts (3/3) and nothing else. This is a decoding
  setting, not a new model.
- The studio only uses its timing. Its labels were right where the loudness labels were wrong (the table
  at the top).

The model also carries other "dataset" heads (prefixes 0-8) on the same embeddings, free to run. Prefix 6
(SongForm-Hook) did better on Enjoy and The Maine but lost The Wldlfe's first drop: no clear winner.

**EDMFormer** (Sajeer et al., CUCAI 2026, [arXiv 2603.08759](https://arxiv.org/abs/2603.08759); weights in
[25ohms/EDM-98](https://github.com/25ohms/EDM-98)). This is SongFormer fine-tuned on 98 EDM tracks to
label intro / buildup / drop / breakdown / outro, the studio's own vocabulary. It runs on the same
embeddings SongFormer already computes, so it would cost about 0.1 s and a 104 MB head. It is the best at
"is this a drop" on Madeon (96%), but it is **unusable on anything that is not EDM**:

- LUNGS BURN came out as one drop from 0 to 148 s.
- Knock Yourself Out XD came out as one drop from 6 to 168 s.
- The Maine came out as a drop from 8 to 131 s.

It has no verse label, and 98 training songs are too few. **Rejected.** One technical note: its own
inference package configures stride 3, but it was trained with stride 2 (12.5 fps). Stride 2 is what was
used here.

**All-In-One** (Kim & Nam, 2023; run with the NATTEN-free fork `all-in-one-infer` 3.1.0, because NATTEN
has no Windows build). It is older and below SongFormer on the published benchmarks. On these songs it
nearly matched SongFormer's layout and was more exact at drop starts: 54.0 and 51.0 on the bar, where
SongFormer had 51.0 for The Maine, a bar early. It also gives beats, downbeats and a tempo. Handed to
settle_sections it did 6/7, the same as the studio now. Its misses: in Enjoy it calls 94-170 one long chorus
(missing the breakdown and verse at 132-151), and like SongFormer it starts LUNGS BURN's first drop a bar
before the user's 52.6.

**MSAF** classic algorithms (0.1.80: SF, OLDA, Foote, C-NMF, spectral clustering). These give unlabeled
segments only, so they can't say where the drops are. Boundaries were hit or miss: OLDA got all four of
LUNGS BURN's, but every other combination scattered extra boundaries. **Rejected** except as a historical
baseline.

**Not run.** None of these looked likely to change the answer:

- MOSS-Music (OpenMOSS, May 2026): an 8B audio-language model that lists structure analysis among its
  tasks. Its 16 GB of fp16 weights won't fit next to the studio's models, and its timestamps come out as
  text.
- "Temporal Adaptation" (Zhang et al., WASPAA 2025): no code or weights released, and its published
  results are below SongFormer's.
- edmformer.cpp: a ggml port with the same weights as SongFormer/EDMFormer.
- LinkSeg: below SongFormer on the benchmark above.

## Cost per song (the 16 GB card; the studio was closed during the GPU runs)

| Candidate | Install size | GPU memory (peak allocated) | Time per song | Notes |
|---|---|---|---|---|
| SongFormer (now) | 2.8 GB + MuQ 1.3 GB (already installed) | 6.1-9.6 GB | 1.5-3.2 s | Measured for 158-231 s songs |
| SongFormer, another prefix or decoder window | 0 | 0 | +0.1 s | Same embeddings |
| EDMFormer | 418 MB trainer checkpoint (104 MB head) | ~0 extra | +0.1 s | Same embeddings |
| All-In-One | 5 MB code + 11 MB weights + 73 MB matplotlib (an import-time dependency); no NATTEN | 0.7-1.0 GB | 20-26 s (about 3 s of that on the CPU) | Needs four Demucs stems (htdemucs, 3-4 s, which the studio already runs; it would need vocals kept, stereo at 44.1 kHz). The pure-PyTorch neighborhood attention is the slow part. A single fold instead of the 8-fold ensemble would be about 8x faster, untested for accuracy |
| MSAF | 78 MB + 52 MB (cvxopt, vmo); needs `scipy.inf` and `scipy.signal.gaussian` patched back in | CPU only | 16-26 s per algorithm | |

All of it fits the existing model environment (Python 3.12, CUDA 12.6 PyTorch 2.14). Nothing was installed
into it: each candidate was installed with `pip --target` into its own folder under `.structure-eval/`, which
holds a `.gitignore` of `*`.

## Recommendation

1. **Keep SongFormer.** Don't replace it. EDMFormer and MSAF are out.
2. **Add All-In-One as a second opinion, in a branch first.** The idea is a label vote: where SongFormer
   and All-In-One both call a stretch a chorus and the loudness detector calls it a verse, it is a drop.
   Where both call it a verse inside a long "drop", it is a verse. Those are exactly the long-drop and
   long-verse failures behind choruses_in_long_sections and verses_in_long_drops tonight. It would cost
   about 1 GB of GPU memory and 20 s per song in learn-ahead. Only adopt it if it wins on songs the rules
   were **not** written for.
3. Cheaper first step, no new model: use **SongFormer's own labels** as that vote (they were right on Enjoy,
   LUNGS BURN and A Boy On A Phone), and try its decoder at +-6 s.

## How far to trust this

- Only four songs have truth. They are the very songs settle_sections was tuned on tonight, so "studio now"
  scoring 6/7 is in-sample, and every other row is out-of-sample against it. A fair verdict needs truth for
  15-20 songs the rules were never shaped on. The user could mark them in the editor, and these scripts
  would score every candidate in a minute.
- SongFormer and All-In-One agree with each other closely on every song (Bombs Away, Knock Yourself Out XD
  and LUNGS BURN nearly boundary for boundary). Two models agreeing isn't proof, but it is a strong signal
  where the loudness detector disagrees with both.
- LUNGS BURN: both models start the first drop at 50.1, exactly one bar before the user's "about 52.6",
  and run it to 79-80 rather than 69.8. Worth a listen: it may be a lift into the chorus.

## Side finding for the studio (not changed)

**"Studio now" calls The Maine's first verse (19.9-45 s) a drop.** settle_sections' change list says
"a release after the breakdown": the drop_tails_and_releases rule added for Everything Goes On (cb4486b,
18:09) turned the intro-then-verse into a drop. That came after the 54.0 fix (e16190f, 17:56). The user put
the first drop at 54.0, and SongFormer, All-In-One and the loudness detector all call 20-45 a verse. It
looks like a regression worth checking.

## Reproduce

Everything is in `.structure-eval/` (ignored by git through its own `.gitignore`):

- `scripts/sf_heads.py` runs SongFormer, its prefixes and EDMFormer, and saves the activations.
- `scripts/allinone_run.py` runs All-In-One. `scripts/msaf_run.py` runs MSAF.
- `scripts/evaluate.py` decodes, maps and scores everything into `results.json`, using the studio's `.venv`.
  `scripts/tables.py` makes these tables.
- Data: `heads/` (activations and embeddings), `allinone/`, `msaf/`, and `demix/` (stems, 0.8 GB).
- The EDMFormer checkpoint is loaded with `torch.load(weights_only=True)`, allowing only the omegaconf and
  typing classes it was inspected to contain.
- About 1.7 GB in all; delete the folder to remove everything.

## Sections found, song by song

The start of each section in seconds. **DROP** marks drops; MSAF's segN are unnamed clusters.

#### Madeon - Enjoy (102 bpm, a bar is 2.35 s)

| Candidate | Sections (start in seconds, type) |
|---|---|
| Studio, sections as found (loudness labels, SongFormer timing) | 0 verse, 19 build, 28 **DROP**, 132 break, 137 build, 141 **DROP**, 170 outro |
| Studio now (found + settle_sections) | 0 verse, 19 build, 28 verse, 47 **DROP**, 71 verse, 92 **DROP**, 132 break, 137 build, 141 verse, 151 **DROP**, 170 outro |
| SongFormer raw (as the studio runs it) | 0 intro, 10 verse, 28 build, 47 **DROP**, 66 verse, 85 **DROP**, 113 break, 151 **DROP**, 170 outro |
| SongFormer, +-6 s decoder window | 0 intro, 10 verse, 28 build, 47 **DROP**, 66 verse, 85 build, 94 **DROP**, 113 break, 151 **DROP**, 170 outro, 179 silence |
| SongFormer, dataset prefix 6 | 0 intro, 10 verse, 28 build, 47 **DROP**, 66 verse, 94 **DROP**, 113 break, 151 **DROP**, 170 outro |
| EDMFormer (long buildups = verse) | 0 intro, 29 build, 47 **DROP**, 68 verse, 94 **DROP**, 132 break, 151 **DROP**, 169 outro |
| EDMFormer, +-6 s window | 0 intro, 29 build, 47 **DROP**, 85 build, 94 **DROP**, 132 break, 141 build, 151 **DROP**, 169 outro |
| All-In-One (8-fold ensemble) | 0 intro, 0 verse, 47 **DROP**, 68 break, 94 **DROP**, 170 outro |
| All-In-One + settle_sections | 0 intro, 0 verse, 47 **DROP**, 68 verse, 94 **DROP**, 132 verse, 151 **DROP**, 170 outro |
| SongFormer raw + settle_sections | 0 intro, 10 verse, 47 **DROP**, 66 verse, 85 **DROP**, 113 break, 151 **DROP**, 170 outro |
| MSAF OLDA + 2D-FMC (best MSAF) | 0 seg4, 0 seg2, 1 seg0, 10 seg1, 46 seg3, 48 seg1, 67 seg2, 69 seg1, 85 seg3, 95 seg1, 117 seg0, 133 seg3, 151 seg1, 170 seg0, 181 seg4 |
| MSAF SF + 2D-FMC (default) | 0 seg4, 0 seg0, 12 seg1, 45 seg2, 66 seg1, 92 seg2, 114 seg3, 145 seg2, 168 seg0, 181 seg4 |

#### little image - LUNGS BURN (97 bpm, a bar is 2.46 s)

| Candidate | Sections (start in seconds, type) |
|---|---|
| Studio, sections as found (loudness labels, SongFormer timing) | 0 verse, 129 **DROP** |
| Studio now (found + settle_sections) | 0 verse, 53 **DROP**, 70 verse, 112 **DROP** |
| SongFormer raw (as the studio runs it) | 0 intro, 11 **DROP**, 30 verse, 50 **DROP**, 79 verse, 109 **DROP**, 154 outro |
| SongFormer, +-6 s decoder window | 0 intro, 11 verse, 21 **DROP**, 30 verse, 50 **DROP**, 79 verse, 109 **DROP**, 139 outro |
| SongFormer, dataset prefix 6 | 0 intro, 11 verse, 50 **DROP**, 80 verse, 109 **DROP**, 129 outro |
| EDMFormer (long buildups = verse) | 0 **DROP**, 148 build |
| EDMFormer, +-6 s window | 0 **DROP**, 148 build |
| All-In-One (8-fold ensemble) | 0 intro, 11 verse, 50 **DROP**, 80 verse, 109 **DROP**, 153 outro |
| All-In-One + settle_sections | 0 intro, 11 verse, 50 **DROP**, 80 verse, 109 **DROP**, 153 outro |
| SongFormer raw + settle_sections | 0 intro, 11 verse, 50 **DROP**, 80 verse, 109 **DROP**, 154 outro |
| MSAF OLDA + 2D-FMC (best MSAF) | 0 seg4, 1 seg0, 51 seg1, 70 seg0, 110 seg1, 130 seg3, 154 seg2, 158 seg4 |
| MSAF SF + 2D-FMC (default) | 0 seg4, 1 seg1, 32 seg2, 50 seg1, 68 seg0, 126 seg3, 158 seg4 |

#### The Maine - how to exit a room (77 bpm, a bar is 3.10 s)

| Candidate | Sections (start in seconds, type) |
|---|---|
| Studio, sections as found (loudness labels, SongFormer timing) | 0 intro, 20 verse, 45 break, 57 **DROP**, 79 verse, 107 **DROP**, 131 break, 156 **DROP**, 181 verse, 218 outro |
| Studio now (found + settle_sections) | 0 intro, 20 **DROP**, 45 break, 54 **DROP**, 82 verse, 107 **DROP**, 131 break, 156 **DROP**, 181 verse, 218 outro |
| SongFormer raw (as the studio runs it) | 0 intro, 19 verse, 51 **DROP**, 81 verse, 106 **DROP**, 131 break, 156 **DROP**, 181 outro |
| SongFormer, +-6 s decoder window | 0 intro, 19 verse, 44 build, 51 **DROP**, 81 verse, 106 **DROP**, 131 break, 156 **DROP**, 181 outro |
| SongFormer, dataset prefix 6 | 0 intro, 20 verse, 38 build, 54 **DROP**, 79 verse, 107 **DROP**, 131 break, 168 **DROP**, 181 outro |
| EDMFormer (long buildups = verse) | 0 intro, 8 **DROP**, 131 break, 156 build, 169 **DROP**, 181 outro |
| EDMFormer, +-6 s window | 0 intro, 8 **DROP**, 44 break, 54 **DROP**, 131 break, 156 build, 169 **DROP**, 181 outro |
| All-In-One (8-fold ensemble) | 0 silence, 0 intro, 19 verse, 54 **DROP**, 81 verse, 107 **DROP**, 131 break, 156 **DROP**, 181 outro, 225 silence |
| All-In-One + settle_sections | 0 intro, 20 verse, 54 **DROP**, 131 break, 156 **DROP**, 181 outro |
| SongFormer raw + settle_sections | 0 intro, 20 verse, 51 **DROP**, 82 verse, 107 **DROP**, 131 break, 156 **DROP**, 181 outro |
| MSAF OLDA + 2D-FMC (best MSAF) | 0 seg4, 0 seg1, 8 seg3, 16 seg0, 33 seg3, 41 seg0, 48 seg1, 54 seg2, 79 seg1, 82 seg3, 92 seg0, 107 seg2, 132 seg1, 141 seg3, 157 seg2, 181 seg3, 206 seg1, 225 seg4 |
| MSAF SF + 2D-FMC (default) | 0 seg4, 0 seg3, 7 seg1, 53 seg0, 80 seg1, 106 seg0, 131 seg3, 142 seg2, 154 seg0, 181 seg2, 210 seg3, 225 seg4 |

#### The Wldlfe - A Boy On A Phone (113 bpm, a bar is 2.12 s)

| Candidate | Sections (start in seconds, type) |
|---|---|
| Studio, sections as found (loudness labels, SongFormer timing) | 0 intro, 17 break, 34 verse, 121 **DROP**, 147 verse, 174 **DROP**, 196 verse, 208 outro |
| Studio now (found + settle_sections) | 0 intro, 17 break, 34 verse, 51 **DROP**, 70 verse, 121 **DROP**, 147 verse, 174 **DROP**, 196 verse, 206 outro |
| SongFormer raw (as the studio runs it) | 0 verse, 51 **DROP**, 70 verse, 104 build, 121 **DROP**, 157 break, 174 **DROP**, 191 outro, 211 silence |
| SongFormer, +-6 s decoder window | 0 verse, 25 build, 34 verse, 51 **DROP**, 70 verse, 104 build, 121 **DROP**, 146 break, 174 **DROP**, 191 outro, 211 silence |
| SongFormer, dataset prefix 6 | 0 intro, 1 verse, 34 **DROP**, 70 verse, 104 build, 121 **DROP**, 157 break, 174 **DROP**, 191 outro |
| EDMFormer (long buildups = verse) | 0 intro, 17 build, 34 verse, 51 **DROP**, 208 outro |
| EDMFormer, +-6 s window | 0 intro, 17 build, 43 **DROP**, 147 break, 157 **DROP**, 208 outro |
| All-In-One (8-fold ensemble) | 0 silence, 0 verse, 51 **DROP**, 70 verse, 121 **DROP**, 157 break, 174 **DROP**, 208 silence |
| All-In-One + settle_sections | 0 verse, 51 **DROP**, 70 verse, 121 **DROP**, 144 break, 174 **DROP** |
| SongFormer raw + settle_sections | 0 verse, 51 **DROP**, 70 verse, 104 build, 121 **DROP**, 144 break, 174 **DROP**, 191 outro |
| MSAF OLDA + 2D-FMC (best MSAF) | 0 seg4, 0 seg0, 18 seg3, 51 seg1, 66 seg2, 103 seg1, 145 seg3, 174 seg1, 213 seg4 |
| MSAF SF + 2D-FMC (default) | 0 seg4, 0 seg2, 8 seg3, 34 seg2, 69 seg1, 104 seg0, 188 seg2, 213 seg4 |

#### Your Neighbors - Bombs Away (70 bpm, a bar is 3.43 s)

| Candidate | Sections (start in seconds, type) |
|---|---|
| Studio, sections as found (loudness labels, SongFormer timing) | 0 intro, 16 verse, 29 break, 57 **DROP**, 84 break, 98 verse, 112 **DROP**, 153 verse, 160 build, 184 **DROP**, 211 outro |
| Studio now (found + settle_sections) | 0 intro, 16 verse, 29 break, 57 **DROP**, 84 break, 98 verse, 112 **DROP**, 153 verse, 160 build, 184 **DROP**, 211 outro |
| SongFormer raw (as the studio runs it) | 0 intro, 29 verse, 57 **DROP**, 84 verse, 111 **DROP**, 139 break, 184 **DROP**, 229 silence |
| SongFormer, +-6 s decoder window | 0 intro, 29 verse, 57 **DROP**, 84 verse, 111 **DROP**, 139 break, 184 **DROP**, 220 outro, 229 silence |
| SongFormer, dataset prefix 6 | 0 intro, 29 verse, 57 **DROP**, 84 verse, 112 **DROP**, 139 break, 184 **DROP**, 197 outro |
| EDMFormer (long buildups = verse) | 0 intro, 16 build, 29 **DROP**, 153 break, 184 **DROP**, 211 outro |
| EDMFormer, +-6 s window | 0 intro, 16 build, 29 **DROP**, 153 break, 184 **DROP**, 211 outro |
| All-In-One (8-fold ensemble) | 0 intro, 29 verse, 57 **DROP**, 84 verse, 112 **DROP**, 153 break, 184 **DROP**, 211 outro, 229 silence |
| All-In-One + settle_sections | 0 intro, 29 verse, 57 **DROP**, 84 verse, 112 **DROP**, 153 break, 184 **DROP**, 211 outro |
| SongFormer raw + settle_sections | 0 intro, 29 verse, 57 **DROP**, 84 verse, 112 **DROP**, 139 break, 184 **DROP** |
| MSAF OLDA + 2D-FMC (best MSAF) | 0 seg4, 8 seg0, 16 seg1, 29 seg3, 57 seg1, 85 seg0, 98 seg3, 111 seg1, 136 seg0, 153 seg1, 181 seg2, 182 seg1, 214 seg0, 231 seg4 |
| MSAF SF + 2D-FMC (default) | 0 seg4, 8 seg2, 16 seg0, 30 seg2, 57 seg0, 85 seg1, 112 seg0, 200 seg3, 231 seg4 |

#### Porter Robinson - Knock Yourself Out XD (87 bpm, a bar is 2.76 s)

| Candidate | Sections (start in seconds, type) |
|---|---|
| Studio, sections as found (loudness labels, SongFormer timing) | 0 intro, 17 verse, 44 **DROP**, 77 verse, 102 **DROP**, 121 break, 135 **DROP** |
| Studio now (found + settle_sections) | 0 intro, 19 verse, 44 **DROP**, 77 verse, 102 **DROP**, 121 break, 135 **DROP** |
| SongFormer raw (as the studio runs it) | 0 intro, 16 verse, 43 **DROP**, 77 verse, 101 **DROP**, 157 outro |
| SongFormer, +-6 s decoder window | 0 intro, 16 verse, 43 **DROP**, 77 verse, 101 **DROP**, 157 outro |
| SongFormer, dataset prefix 6 | 0 intro, 16 verse, 41 **DROP**, 77 verse, 102 **DROP**, 157 outro |
| EDMFormer (long buildups = verse) | 0 intro, 6 **DROP** |
| EDMFormer, +-6 s window | 0 intro, 6 build, 16 **DROP**, 124 break, 135 **DROP** |
| All-In-One (8-fold ensemble) | 0 intro, 17 verse, 43 **DROP**, 77 verse, 102 **DROP**, 157 outro |
| All-In-One + settle_sections | 0 intro, 19 verse, 44 **DROP**, 80 verse, 102 **DROP**, 121 verse, 135 **DROP**, 157 outro |
| SongFormer raw + settle_sections | 0 intro, 19 verse, 44 **DROP**, 80 verse, 102 **DROP**, 121 verse, 135 **DROP**, 157 outro |
| MSAF OLDA + 2D-FMC (best MSAF) | 0 seg4, 1 seg1, 6 seg2, 17 seg1, 20 seg0, 42 seg1, 45 seg2, 77 seg3, 81 seg0, 124 seg2, 168 seg3, 168 seg4 |
| MSAF SF + 2D-FMC (default) | 0 seg4, 1 seg3, 16 seg1, 41 seg2, 78 seg1, 101 seg0, 153 seg3, 168 seg4 |
