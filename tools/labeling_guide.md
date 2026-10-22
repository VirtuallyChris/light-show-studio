# How the user labels song sections

Distilled on 2026-10-21 from the user's own 129 reviews: 117 studied as pictures (tools/song_picture.py: spectrogram,
waveform, stem lanes, kick/snare hits, the user's sections against the labeler's), 12 held out to test against. For
Claude labeling songs in the user's style (tools/claude_review.py). Six types: intro, verse, buildup, drop, breakdown,
outro.

## Start from the labeler

A first blind test (11 held-out songs) of labeling from scratch with this guide scored 77.5% against the labeler's
83.5%: the rules below were over-applied, above all verse called breakdown (6% of the time), verse called drop or
intro. So: **take the labeler's smoothed answer as the draft and change it only where the evidence is strong and
clear in both the table and the picture**, or where its top probability is below ~0.6 and the guide decides it.
When in doubt, keep the labeler's answer; between verse and anything else, verse. A breakdown needs drums and bass
both gone (or a plain fall of the whole song) for 2+ bars; a drop needs the "other" layer and bass lifting for 4+ bars.

## What to look at

Per bar (the table, and the picture's lanes): **loud%** (the bar against the song's loud level, its 90th percentile),
**bass**, **drums**, **vocals**, **other** (synths, guitars), **kick** and **snare** hits, and the waveform's width.
The labeler's guess is a hint; it is usually right on drops in EDM and wrong in the ways listed under each type.

## drop: the chorus, the hook, the peak

- The marker is the **"other" layer brightening together with the bass**, loud% at 90-100% held for 4+ bars, a snare
  backbeat (often doubled against the verse), a wider waveform. In rock and pop-punk the chorus also has denser kick.
- **Loud alone is not a drop.** Yellow loud% with a dark "other" lane, or kick with no snare, stays verse. Verses can
  be louder than the drops (hip-hop: 3005, WET DREAM): the hook is still the drop.
- **Every chorus is a drop** in pop, rock, pop-punk, hip-hop and party songs; in EDM every full-beat section is.
- An **instrumental hook** (vocals dark, band at full energy), including one right after the opening, is a drop; when
  the singing comes back it is verse again, even if still loud.
- **A song can have no drop** when no section steps up from the rest (the drum pattern never changes): long verses
  only (Headlights, Oogie Boogie, Howard Jones, Cave In). Some 80s/theatrical songs keep the drop for the final
  choruses only.

## verse: the default

- Vocals over a beat below the drop tier; pre-chorus lifts stay verse (unless they are a buildup, below). Solos are
  usually verse. Bass coming back without drums after a breakdown makes it verse.
- Back-to-back verses split where the bass changes.

## breakdown: the song clearly thins out

- **Drums and bass both gone**; or bass and "other" going dark with loud% falling, **even if the kick keeps going**
  (four-on-the-floor kick is fine when the snare and bass go).
- **A drumless sung stretch right after the intro is a breakdown** (very consistent: Waterfalls, Miracle, Charlie
  Puth, Something Better). A drumless song opening on singing may start on breakdown at bar 0.
- **Bridges are breakdowns** when the bass goes dark, even with drums still playing.
- **Thinning just before a drop is breakdown, not buildup** (a drumless riser or pre-chorus).
- **Quiet endings**: the quieting is breakdown, then a short outro. Ballad halves inside a track: one long breakdown.
- 1-2 bar stops are usually breakdown. A breakdown often goes straight into a drop: do not invent a buildup.
- Snare spikes and risers inside a breakdown stay breakdown.

## buildup: the riser into a drop

- Starts at the **first bar of the riser** and ends exactly at the drop's downbeat; its length is the riser's length,
  not a fixed 4: rock/pop 1-4 bars, EDM 8-16 bars. The user's lengths: 4 most often, then 2, also 8, up to 16.
- Risers: a **snare roll** (8-24 snares a bar against 2-4), a **kick roll or doubling**, layers stacking with the
  waveform swelling, or a **1-2 bar dropout/near-silence right before the drop**. In EDM and dance-pop a swelling
  pre-chorus (4-8 bars, vocals in) is a buildup.
- Snare rolls that do not lead into a drop stay in their section. Rarely a buildup leads into a verse or breakdown.
- **An opening that swells straight into the first drop is a buildup**, not an intro.

## intro

- The opening before the vocals or the main section, sparse or instrumental, leading into a verse; it may have a beat.
- Not when the full band starts at once (verse), when the opening is the instrumental hook at full energy (drop),
  when it swells into the drop (buildup), or when it is a drumless sung opening (breakdown).

## outro

- **The final tail**: a 1-2 bar outro over the last decay or silence, **nearly always, even after a loud ending**.
  Left out only when the music runs to the very last bar.
- A fade: breakdown for the quieting, outro for the last 1-5 bars.

## Boundaries

- Mostly on **4-bar phrase lines from bar 0** (8 in EDM). A section starts on the first bar where the bass, drums and
  loud% change together; breakdown on the first bar with drums/bass gone; a drop on the downbeat after the roll.
- A fill or roll bar belongs to the section before it. Short sections (1-3 bars) break the grid when the audio does.
- Boundaries follow the waveform and lanes, not the lyric lines. Repeated sections get the same labels each time.

## Results

Blind, 11 of the user's songs nobody had studied (types / drop-not-drop): from scratch with this guide 77.5% / 92.4%;
starting from the labeler and fixing only clear cases 82.7% / 93.3%; the labeler alone 83.5% / 92.8%. A tie at best:
the labeler already holds these patterns. Not worth labeling hundreds of songs with agents for.
