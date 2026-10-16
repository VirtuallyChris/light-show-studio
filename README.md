# LAN Light Show

Listens to a microphone (or any audio input) and drives Govee smart lights
over the local network in real time. No cloud, no Govee account, no API key.

Two ways to use it: a live mic-reactive show (`lightshow run`), or the
**Studio** (`lightshow studio`), a browser sequencer for building a show on a
saved song beat by beat. See *Studio* below.

Two ways of talking to the lights:

* **Real-time per-segment mode (default).** Uses the same LAN protocol
  Govee's desktop app uses for Razer Chroma: every segment (LED zone) of the
  light is set individually, instantly, 60 times a second. No fade.
* **Classic mode** (`--classic`). Uses the documented `colorwc` command. One
  color for the whole light and the firmware fades towards it over a few
  hundred milliseconds, so it looks soft and laggy. Useful for lights that do
  not support real-time mode.

## Setup

1. In the **Govee Home** app open each light -> settings (gear) -> turn on
   **LAN Control**. Lights without it will not answer the scan.
2. Install Python 3.11+ and the dependencies:

   ```bash
   python -m venv .venv
   .venv\Scripts\python -m pip install -r requirements.txt
   ```

3. Optional: copy `config.example.json` to `config.json` and put your own
   light's MAC and LED count in it. Without it the studio starts every light at
   10 zones and you set the real count in the Lights panel, which saves it for
   you. `config.json` holds your network's addresses, so it stays out of git,
   as do the songs you drop in; the show designs saved next to them
   (`shows/<song>/project.json`) are kept.

**Naming lights.** Click a light's name in the Lights panel (it shows the model,
like H608D, until named) and type. The name is used in the preview and in each
clip's light picker. Names you gave the lights in the Govee app cannot be read:
Govee keeps them in its cloud, and on the network the lights announce no name of
any kind (no hostname, NetBIOS or mDNS). Names are saved in `config.json` against
the light's MAC and its own device id, so a new IP address does not lose them.

`lightshow.cmd` in this folder runs the tool with the venv, so the commands
below can be typed as `lightshow ...` from this directory (`.\lightshow ...` in PowerShell).
**Double-click `Light Show Studio.vbs`** to open the Studio like an app: one
window appears and nothing else. The server runs invisibly behind it, with no
console window or taskbar entry, and shuts itself down a few seconds after you
close the studio window. Its messages go to `studio.log` in this folder. A
second double-click while it is running just opens another window on the same
studio. `studio.cmd` is the same thing from a terminal, with the output visible.

For a desktop icon, make a shortcut to the `.vbs` (right-click it, *Send to*,
*Desktop*) and give it `Light Show Studio.ico`. The same icon is the page's
favicon and web-app manifest icon, which is what the app window shows in its
title bar and on the taskbar; `tools/make_icon.py` redraws every size.

The Studio opens in a **borderless app window** (Chrome, Edge or Brave, whichever
is installed) with no tabs or address bar, using its own browser profile so it
keeps its size and stays out of your normal browsing. Closing that window quits
the studio, the way a desktop app would. Use `--tab` for an ordinary browser tab,
`--stay` to leave the server running after the window is closed, and
`--no-browser` to open nothing.

## Usage

```bash
lightshow devices                       # scan the LAN, show IP / wifi MAC / SKU / segment count
lightshow audio                         # list audio inputs (* = default)
lightshow test 5C:E7:53:E9:C7:FE        # cycle red/green/blue/white on one light
lightshow probe 5C:E7:53:E9:C7:FE       # light each segment in turn so you can count them
lightshow probe 5C:E7:53:E9:C7:FE --save 12   # remember that this light has 12 segments
lightshow run                           # all lights, default mic, concert mode
lightshow run 5C:E7:53:E9:C7:FE -m eq   # one light, spectrum-analyser mode
lightshow run --no-strobe               # concert mode without hard strobes
lightshow run -a "Volt" -m pulse -p fire
```

Lights can be selected by IP, wifi MAC (as shown by your router), Govee
device id, or SKU (`H608D` picks every H608D). With no selector every
discovered light is used.

### Modes (`-m`)

| mode       | what it does |
|------------|--------------|
| `concert` (default) | Reads the *structure* of the music and changes the whole show with it. Chill: slow aurora fades. Groove: mood-colored gradient with kick pulses from the center. Buildup: the strip fills from both ends toward the center while a strobe accelerates from 2 Hz to 20 Hz. Drop: white blast, shockwave, then six seconds of fast rainbow chaos. Heavy: beat strobes, fast color-block chases, alternating halves. Hi-hats sparkle throughout. Base color follows the mood: dark bass-heavy = deep red, mid = violet, bright = cyan. |
| `spectrum` | Hue follows the tonal center of the music (bass = red, treble = violet), brightness follows loudness, beats ripple out from the center of the light. |
| `eq`       | Spectrum analyser: each segment is a frequency band, bass at one end, treble at the other. |
| `vu`       | Loudness fills the light from one end (green -> yellow -> red) with a falling peak marker. |
| `pulse`    | Palette color jumps on every beat, brightness pumps with the beat, beats ripple along the light. |
| `rainbow`  | A rainbow runs along the light; energy sets its speed and brightness. |
| `bands`    | Each *light* takes a band (bass / mid / treble). Good with several lights in a room. |
| `strobe`   | Dark between beats, white flash on beats. |

Palettes (`-p`): `neon`, `fire`, `ocean`, `party`.

### Useful knobs

| flag | meaning |
|------|---------|
| `--leds N` or `--leds MAC=N,IP=N` | LED count per light for this run (otherwise `config.json`, default 10). The H608D has 50 individually addressable LEDs; the protocol allows up to 84 per light |
| `--fps 60` | color updates per second per light. 60 is confirmed smooth on the H608D; 90 also gets through |
| `--sensitivity 1.4` | beat threshold in standard deviations. Lower = more beats |
| `--floor 0.06` | minimum brightness while music plays |
| `--flash 0.6` | how hard beats push brightness |
| `--gradient` | ask the light to blend colors between segments |
| `--no-strobe` | concert mode: smooth pulses instead of hard strobes (photosensitivity) |
| `--classic` | use the normal color command instead of real-time mode |
| `--off-on-exit` / `--exit-color 255,160,60` | what to do with the lights on Ctrl+C |


## Studio: build a show on a saved song

`lightshow studio` opens a full sequencer in your browser. Drag a song in,
it finds the tempo, beat grid, sections and drops, and you arrange
tempo-synced effects on layered lanes over the waveform.

```bash
lightshow studio
```

Then drag an audio file onto the page (MP3, WAV, FLAC, OGG, M4A) or click
**Open song**. First analysis of a song takes 10-20 seconds; it is cached
after that. Saved shows live in the `shows/` folder and reload from the
**Saved shows** menu.

**Workflow**

1. **Pick lights** on the left. The studio finds them by itself: when it
   opens, every half minute while nothing is playing, and when its window
   comes back to the front. The strip you started with is ticked by
   default; tick others to include them. Set each light's segment count here.
   If a light is mounted the other way round (a left-to-right comet runs right
   to left on it), press its **⇄** button: everything sent to that light is
   mirrored, the preview stays as designed, and the setting is remembered.
   **Around the room:** drag the lights by their grip (⋮⋮) into the order they
   stand around the room, once. The ticked lights, in that order, are the room:
   the first half one side, the rest the other. With two or more ticked, every
   auto-built show (Jam mode included) uses the room with no extra work: drops
   split it in two colors that swap on the beat (**Room split**), buildups run a
   light around it lamp by lamp (**Room chase**), and some verses roll a swell
   around it (**Room wave**). They are also in the library under *Around the
   room*. With one light they play across that light's own strip.
2. **Auto-build show** generates a complete show from the detected structure
   as a starting point: aurora in intros, gradients and beat pulses in verses,
   hits all around the room through buildups, a white blast and rainbow on drops,
   and fades on the outro, with the palette changing per section.
   **How hard it goes depends on the kind of music.** Each song gets a show
   style:

   | Style | Typical music | What the lights do |
   |---|---|---|
   | Chill | ambient, jazz, folk, soul, classical | Slow fades and drifting light. No strobes, no flashes |
   | Groove | rock, pop, hip hop, indie, R&B | Moves on the beat. Drops lift without flashing; buildups strobe to 1/8 notes |
   | Dance | house, trance, EDM, metal | Drops flash every other beat, softer, in the song's own colors; buildups strobe to 1/16 notes |
   | Rave | dubstep, drum and bass, techno | Everything: 1/32 strobes, blasts, rushes on drum fills, rainbow drops |

   The accents on top (one-shots on the biggest lead moments, the lead traced, ghost-note
   sparks) follow the style too: none in a chill song, two one-shots a drop in groove,
   three in dance, all of them in rave.

   The genre comes from a model that listens to the song: MIT's Audio
   Spectrogram Transformer, trained on Google's AudioSet (open source, about
   350 MB, downloaded once, runs in the model environment described below). In
   Jam mode the genres Spotify lists for the artist count too. The model's
   middle guess counts, not an average (a faint "dubstep" among pop guesses does
   not drag a pop song up a style), and when its guesses are unsure the song leans
   to Groove. The song's own energy moves it only a little, measured from how
   loud a modern master normally is, so a loud rock song is still rock. (At a
   party in October 2026 three songs in four came out Dance or Rave, indie bands
   included; with this rule the same 34 songs came out 24 Groove, 7 Dance, 2 Rave,
   1 Chill. Songs learned before the change are styled again from what was heard.) The Song
   panel (nothing selected) shows what the model heard and lets you choose the
   style yourself; press **Auto-build show** again to rebuild in it.

   **Shows keep up with the studio.** How shows are built is versioned, and every
   saved show records the version it was built with and the analysis it was built
   on. A show built by older logic is rebuilt when it is opened, keeping its
   settings (style, main color): in the studio it is then saved, the old one kept
   as `project.before-rebuild.json`; in a jam or as a cue it is rebuilt as it
   plays. A show changed by hand since it was built is never rebuilt by itself:
   opening it says it is out of date, and **Auto-build show** rebuilds it. (A test
   copy of the studio, `--no-lights`, never saves or deletes a show.) The effects a
   show is made of are drawn for each song from its vibe and genre, never chosen
   by hand.
3. **Tweak**: drag effects from the library onto any layer, drag clip edges
   to resize, drag the body to move, Alt-drag to copy. Layers are numbered
   from the bottom; higher layers sit on top, so a comet on layer 2 rides over
   a gradient on layer 1. Add as many layers as you like with the **+ add
   layer** strip, right-click a layer name to insert, remove or reorder, and
   click its eye to hide or show it.
   Clips never overlap inside a layer: dropping one where another already is
   places it on the next free layer up, resizing stops at the neighbour, and a
   drag into an occupied spot slides to the nearest gap. Each clip has its own
   parameters, opacity, blend mode and fades in the inspector.
   **Which lights play is not part of a show.** Tick them in the Lights panel
   and every song, the preview and Jam mode use that one selection; opening a
   song never changes it. It is saved in `config.json` by each light's MAC.
   **Selecting several at once** works as in a DAW: drag across empty space
   to box-select, <kbd>Shift</kbd>+click to add or remove one,
   <kbd>Ctrl</kbd>+<kbd>A</kbd> for everything. Drag any selected clip and
   the whole block moves together, keeping its shape across layers; if the
   block would land on something outside it, it stays put.
   <kbd>Ctrl</kbd>+<kbd>C</kbd>/<kbd>X</kbd>/<kbd>V</kbd> copy, cut and paste
   the block at the playhead, <kbd>Ctrl</kbd>+<kbd>D</kbd> duplicates it right
   after itself, <kbd>Delete</kbd> removes it, and right-clicking gives the
   same menu, with *Paste here* landing the block where you clicked. The
   inspector shows the group with one opacity slider and one color choice
   for all of them.
4. **Colors**: no effect owns a color; every clip does. Effects that used to
   be named for one (White flash, Rainbow, Rainbow chaos) are now Flash, a
   Gradient sweep with the Rainbow preset, and Chaos, and they simply start
   out white or rainbow until you change them. Pick **Single color** in the
   inspector when one color says it best, which is often. Every clip carries
   its own colors, shown as a color bar
   along the bottom of the clip on the timeline. Select a clip and pick from
   the visual palette grid in the inspector, choose *Custom* for your own
   color swatches. Clips without their own choice use the song's
   **Default palette** (in the Song panel of the inspector). Dropping an *effect* onto an
   existing clip swaps the effect and keeps the clip's timing, layer, colors
   and opacity. Click the eye on a layer name to hide that layer while you
   work on the others.
5. **Play** with the **Lights** switch on (at the top of the Lights list) to
   drive the real lights in sync with a live preview of every strip. The **Offset** (under the ⚙ button, top right)
   sends the lights a few milliseconds ahead of the audio to line them up by
   eye; it is saved once for every song.
   The switch is on when the studio opens (unless you last switched it
   off), but the lights keep showing whatever they were until something plays:
   then the studio remembers each light's power, brightness and color and takes
   it over at full brightness. Closing the studio (or switching Lights off) gives
   every light back as it was, and so does unticking one light in the Lights
   list, just that light; ticking one mid-show takes it over the same way. A
   scene from the Govee app comes back by itself; the network control can only
   restore power, brightness and a single color.

**Casting to Google speakers.** Pick a Cast speaker or speaker group in the
*Cast* dropdown (groups are marked) and press **Cast**. The studio serves the
song to the speakers over the LAN and follows *their* playback position, so
the lights stay in sync with what you hear around the house. Play, pause,
seek and stop work as usual. **PC audio** keeps playing the song on this
computer too, delayed by the same **Cast offset** as the lights: turn the
offset until the room and the house sound together, and the lights are in
sync as well. The slider next to it is the speakers' volume. **Stop cast**
returns playback to the PC.

**The offset measures itself.** A few seconds after you start casting, the
studio listens to the room through the microphone, lines up what it hears
against the song's own audio, and fills in the offset. It mutes **PC audio**
for those few seconds so it hears only the speakers, then puts it back. The
answer is saved per speaker in `config.json` and reused the next time you cast
there, with a quick re-check on each new session. Press **Sync check** to
measure again at any time, and type your own number to override it for a
project. On this setup Office Home 2 measures about 300 ms.

Behind that: a Cast speaker asked to start at, say, 95 seconds has to guess
which byte of the file that is. In a variable bitrate MP3 the guess is wrong
by up to a second and a half, and it stays wrong for the rest of playback, so
every start position drifted by a different amount. The studio now hands the
speaker a stream that already begins at the requested second, so the speaker
never seeks. What remains is that speaker's own fixed output delay, which one
measurement pins down for the whole session. Measured on one speaker before
the change, starting at four different points gave offsets spanning 1143 ms;
after it, 47 ms, which is the measurement's own resolution.

Seeking and pausing keep that figure. A seek loads a fresh stream at the exact
new second, and if the speaker drops a long-paused session, the next play
reloads it where you left off. The old chirp-based `lightshow calibrate
<speaker>` command still exists, but the studio no longer needs it.

**Tempo and bar lines**: the analysis halves the tempo by itself when the grid
counts double: the in-between beats have neither a kick nor a snare. A kick on
1 and 3 with a snare on 2 and 4 is an ordinary backbeat and is left alone. If it
still gets it wrong, open the **Beat grid ▾** menu above the timeline and press
**Halve ÷2**; **Double ×2** doubles it.
Clips keep their place in the song, so nothing moves, but a "1 beat" effect
now means the new, longer beat. If the bar lines sit in the wrong place, press
**Bar−1** or **Bar+1** to shift the downbeat a beat at a time, or right-click
the waveform on a downbeat and choose *Set downbeat to nearest beat*. Sections
snap to bar lines, so getting this right first saves nudging every boundary.

**Beats**: turn on **Beat edit** to fix the grid. Click the waveform to add a
beat, tap <kbd>Space</kbd> along with the music while it plays to lay beats
down, drag a beat to move it, <kbd>Delete</kbd> to remove one. Or set the
**BPM** and press **Regrid from BPM** in the Beat grid menu
for a fresh even grid, and nudge the whole grid with the 10 ms buttons there.
**Bar +1** moves the downbeat.

**Section timing by SongFormer (optional, recommended).** Two detectors
work together. The loudness detector (below) decides *what* each part is:
drop, buildup, breakdown. The SongFormer model (ASLP Lab, 2025) decides
exactly *when* each buildup and drop starts: each of those boundaries moves to
SongFormer's nearest one if it is within a few seconds, then onto the bar line.
Quiet transitions keep the loudness detector's timing, which is exact there.
SongFormer alone thinks in pop terms (verse, chorus) and labels dance music
poorly; the loudness detector alone tends to call a drop a bar or two early
when the pre-chorus is already loud. Together, on two songs corrected by hand,
they matched 89-99% of the labels and put 14 of 15 buildup, drop and breakdown
starts within half a second of the hand-placed ones. It takes a few seconds per
song on an RTX-class GPU, and Jam mode uses it too, on audio in memory. The Song
panel says "timed by SongFormer" when it ran, and says why when it could not. A song
analysed while SongFormer could not run is tried again the next time it is
opened, and **Re-detect** asks again too. To set it up (about 5 GB, once):

```bash
winget install --id Python.Python.3.12 --scope user
%LOCALAPPDATA%\Programs\Python\Python312\python.exe -m venv .venv-songformer
.venv-songformer\Scripts\python.exe -m pip install torch torchaudio --index-url https://download.pytorch.org/whl/cu126
.venv-songformer\Scripts\python.exe -m pip install "transformers==4.51.1" huggingface_hub librosa soundfile scipy numpy ema-pytorch loguru muq omegaconf tqdm x-transformers einops nnAudio
.venv-songformer\Scripts\python.exe -m pip install demucs
```

The weights download automatically on first use. The environment lives in the
project folder (`.venv-songformer`, not in git), the folder the studio runs from, and
nowhere else; on the local disk on purpose: a network share is far too slow for
PyTorch. (Do not put it under AppData\Local from a terminal inside a packaged app such
as the Claude desktop app's: Windows quietly keeps what such an app writes there in the
app's own storage, and the studio started from its shortcut never sees it. That is how
the studio once went days with no genre, stems or sections.)
A model that will not start is tried again every 2 minutes, and why goes to
`studio.log` and `models.log`.
Without it, the built-in detector below is used.

**How sections are found.** The analyser works per bar over the whole file,
with cues checked against hand-labelled tracks. A *drop* is loud **and**
full-spectrum: a verse can be just as loud on a modern master, but its mids
are sparse, while a drop's mids and highs light up together. A *breakdown*
loses the sub bass and the level. A *buildup* is the 4-16 bars running into a
drop, bounded by the nearest texture change and extended back while the
brightness and level keep climbing. Everything else with a groove is a
*verse*; quiet first and last stretches are intro and outro. On a
hand-labelled Porter Robinson track it agrees with the human labels 95% of
the time and lands every drop within a second. Press **Re-detect** to refresh
sections, drops and the beat grid from analysis without touching your clips,
then fix anything it got wrong by dragging section edges.

**Sections and drops**: drag a section's left edge to move the boundary,
double-click a section to change its type, right-click to split or merge. Drop
markers (red) can be dragged, added and deleted; auto-build puts a blast on
each one. A marker sitting on a section edge moves with it when you drag the
edge, so a drop and its blast stay together.

**Any window size**: the whole studio scales with the window, from a laptop to
a 1440p monitor, text, buttons and timeline alike. The top bar holds the file,
building, transport, Cast and lights controls; the timeline's own controls
(BPM, the Beat grid menu, snap, zoom, undo, Beat edit) sit in a strip right
above it. Below about 1150 px wide the library of lights and effects
becomes a drawer behind the ☰ button, and on a very narrow window the
inspector moves to the bottom. The **?** button lists the keyboard shortcuts.

**Getting around**: the strip across the top is an overview of the whole song
(section colors, a summary waveform and the drop markers). The bright box on
it is what you are looking at: drag it to scroll, drag its edges to zoom, or
click anywhere to jump there. In the timeline the wheel zooms around the
pointer, <kbd>Shift</kbd>+wheel pans, and <kbd>Ctrl</kbd>+wheel scrolls the
layer stack. <kbd>Space</kbd> plays and pauses, clicking the waveform seeks,
dragging the ruler scrubs (the playhead snaps to the **Snap** setting, like
clips; hold <kbd>Alt</kbd> to place it freely), and <kbd>+</kbd>/<kbd>-</kbd>/<kbd>Fit</kbd> zoom
from the keyboard. Zooming out can go past the end of the song (up to four
songs' width); the time after the end is shaded, and **Fit** brings the whole
song back to the full width.
<kbd>Ctrl</kbd>+<kbd>Z</kbd>/<kbd>Y</kbd> undo/redo. There is no Save button: everything
saves by itself a moment after each change, and anything still waiting is saved
when the window closes. <kbd>Ctrl</kbd>+<kbd>S</kbd> saves at once and says so.

There are around 40 effects grouped as slow (aurora, gradient, breathe, wave,
twinkle, embers), groove (pulse, chase, comets, dual comets, meteors, pulsar,
orbit, color battle, spectrum, scanner, ripples, sparkles), fast
(strobe, blackout strobe, hyper chase, lightning, chaos, glitch,
halves), the room's buildup (Build-up hits) and one-shot accents (drop
blast, fades, flash, slam, sweep out). Effect timing is always in beats, so a "1 beat" pulse stays locked to
the song whatever its tempo.

**Signature flair: what makes each song sound like itself.** The analysis also
listens for the parts a track is built on, and auto-build gives each one its
own layer, only where the song actually has it:

| Effect | Fires on |
|---|---|
| **Hook strike** | every return of the song's main riff, found by comparing every bar against every other and keeping the motif with the most twins |
| **Lead trace** | notes of the wide lead instrument: a saw synth, horn line or riff. Color and position follow the note played |
| **Lead glow** | lit only while that lead instrument is actually sounding |
| **Ghost sparks** | drum hits landing *between* the beats: fills, ghost notes, shakers |
| **Fill rush** | drum fills and breaks only, speeding up as the fill builds |
| **Bass note** | color follows the bass note, swelling on each new one |
| **Bass hits** | the whole strip flashes on every hit of the bass line, on the beat grid, from the separated bass track |

The lead is taken from the separated "other" track (synths, guitars, keys,
with no drums and no vocals) when the model environment is installed, which is
where a lead synth or a guitar line actually is. Without it, the lead comes from
the stereo side channel (left minus right), which cancels anything panned dead
center, so lead vocals and kick largely drop out and the wide instruments
remain. Either way it follows the synth rather than the singer.

**Energy ramps with the song.** Intros, breakdowns and outros get slow fades
and drifting comets and never flash. Buildups may strobe, because that is what
a buildup is for. A drop only flashes on the beat when the track is genuinely
hard-driving, judged from its own measured drive; a gentler song gets motion
instead: scrolling gradients, crossing comets, orbits.

**Flair punctuates, it does not blanket.** A drop keeps one base effect for its
whole length, and the song's own moments are fired on top as one-shots: a
strike each time the riff returns, a rush through each drum fill, and a
sweep, slam or ripple on the biggest lead hits, spaced at least two
bars apart so a long drop keeps surprising. Quiet sections get none of this.

**Color** comes from the song, not from a fixed table. The key, brightness and
drive pick a small family of palettes that suit the track, and each section
gets its own rotation of one of them, so the show stays coherent without every
drop looking like the last one.

The design rule is *dark space*: a strip that is mostly dark with a few
bright things moving over it feels far more alive than a wall of color, so
the groove effects default to black between their bright parts, the sparse
effects (meteors, pulsar, orbit, dual comets) draw on transparent black, and
auto-build pairs a dim base with one sparse overlay. Auto-build keeps one base
effect and one overlay for the whole of every drop; only the blast on its
first beats is extra. A buildup is **Build-up hits**: every snare (or every
hit, or a beat-by-beat build) lights random stretches all around the room,
never the same ones twice running, and more of them at once as it goes (one at
the start, up to four at the end), brighter and snappier, so the energy builds
around the room rather than along one strip. (The old strip-filling Riser is
gone: a show that had one gets Build-up hits over the same stretch, on the
same drums, when it is opened.) **Strobe riser** is a blackout strobe for the
layer above that steps up into the drop, if you want one.

## The lights' frame rate follows the Wi-Fi

Real-time frames to the lights are one-way UDP: a light never says whether a
frame arrived. But the Wi-Fi they share shows its state in a ping, slow or lost
exactly when frames arrive late and the lights stutter. So while a show plays,
the studio pings the lights it drives (a small ping every quarter second, in
turn, through Windows' own ICMP API), and **each light has a frame rate of its
own**, set from its own replies, between 15 and 60 frames a second: a step down
at once when a ping is lost or the replies turn slow (90th percentile over
90 ms; two steps when a third are lost or they pass 200 ms), and a step back up
after 15 s of clean, quick replies (none lost, under 45 ms), never sooner than
30 s after a step down. Steps: 15, 20, 24, 30, 40, 50, 60; each starts at 24.
Frames go to each light at its own rate, so a light at the edge of the Wi-Fi
never holds back one next to the router. Changes go to studio.log, light by
light. Set `"light_fps_ceiling"` in config.json for a lower top rate (60 if not
set), or `"light_fps_auto": false` to keep a fixed `light_max_fps` for all.

Stress test, casting to Everywhere (the whole house) with all five lights:
3 minutes of a saved show, then 3 minutes of the jam's live stream. The two
7-segment poles answered pings slowly (41-60 ms on average, up to 160 ms) and
stayed at 15 frames a second; the three strips (7-11 ms) ran at 40-54. No
errors, and frames never more than 0.18 s apart. One thing seen: while taking
30 frames a second or more, the strips skip about a quarter of status questions
(at 15 every one was answered); whether they also drop frames then cannot be
seen from here.

## Pre-listen: a party playlist learned ahead of time

A jam learns each song the first time it plays (about a minute and a half
ahead of the room, which stays the default for every jam), and a learned song
gets its full show from the first beat the next time. Spotify's audio only
plays in real time, so songs cannot be learned any faster than they play; but
they can be learned before the party. In the Party Hub's **Settings**,
**Pre-listen**, paste a Spotify playlist link and press **Learn this playlist**
(or **Learn the queue**): the studio's own hidden Spotify player plays every song
of it that is not learned yet (songs you made shows for are skipped), into the
output nobody hears, through a silent jam (no speaker or light is touched). Each
song is learned as it ends, exactly as at a party, and only what is learned is
kept, never the audio. The card shows how many are learned and how long is left;
when all are learned, Spotify pauses. It takes as long as the songs do, so a
three-hour playlist is something to start the night before. One Spotify account
plays one thing at a time: if Spotify starts playing anywhere else (a phone
taking over) or a jam is started, pre-listen steps aside. Reading playlists
needs one more Spotify permission: sign in to Spotify again once in the Jam
panel.

## Party Hub: the jam from your phone

The studio also serves a phone app on the home network. On the iPhone, open
`http://<this PC's address>:8765/party` in Safari (here `http://192.168.1.11:8765/party`)
and choose **Share, Add to Home Screen**: it opens full screen like an app. It
shows the song the room is hearing (with previous, play/pause and next, which steer
Spotify the way the phone's own controls would: the room hears a skip within the few
seconds it is behind, and a song gone back to was heard or learned ahead already, so it
gets its full show), what comes next, and
each song's two colors. Tap a color to change a song's pair ahead of time from
sixteen solid colors (it is kept for that track, as in the studio; ↺ goes back to
the jam's). **Just these two colors** in the same sheet keeps a song to its own two
and nothing else: no accents in other colors and no rainbow drops (a red and
orange song stays red and orange); such a song shows a small *2*. A tap anywhere on a song brings up its colors on one sheet, with its **Vibe**:
*Auto* (the style its analysis found, shown beside it) or Chill, Groove, Dance or
Rave picked by hand, for a song the analysis gets wrong (a chill song taken for a
rager). Its **Genre** is there too: *Auto* (what the analysis heard) or one picked
by hand (Pop, Hip hop, Trap, EDM, House, Dubstep, Drum & bass, Rock, Indie, Metal,
R&B, Country, Folk, Jazz, Ambient). The genre turns on the effects that belong to it
(808s and hat rolls for hip hop and trap, wobbles and glitches for bass music,
candlelight for folk) and, with the vibe on Auto, sets the vibe too (Dubstep is
Rave, Folk is Chill). **Party colors / Album art** at the top of the sheet does what
the song's little *party* / *cover* chip does. Vibe and genre are kept for that
track (`shows/_jam/styles.json`, `shows/_jam/genres.json`), and the song playing is
rebuilt in them at once. **Party colors** switches the
party's colors (purple and green, or the pairs) on or off for every song: off,
each song plays in its album cover's two colors. On the phone's **Lights** tab the party colors
can be changed too: **Two colors** (every song in them) or **A pair each song**,
each color tappable into the sixteen, pairs added or removed; and kept as named
sets (**Save as…**, a Halloween set, a Christmas one) to switch between from the
**Set** menu. A set changed since it was chosen says so, and **Save** keeps the
change in it. Sets are kept in config.json (`color_sets`). Each song's **party** / **cover**
chip does the same for that song alone (kept for the track). A change of colors
reaches the song playing within a second: the studio rebuilds its show in them. The **Party** slider sets how hard every show goes, live: at 100% the
full show; lower, the song's main color is laid over the show, that much more
opaque (at 10%, 90% of the light is the song's color), so the room calms down
into one color without going dark. The level is remembered. The ⚙ button opens
**Settings**: the rooms (below), the volume of the cast the jam plays on (a
group's volume scales its speakers together), and the speakers by room: one
slider per room, which moves all its speakers together and keeps their mix (the
room is as loud as its loudest speaker), and **Edit mix** under it for each
speaker's own; speakers in no room are listed after. The Party Hub answers only
devices on the home network (private addresses) and this PC.

## Cues: the doorbell, shot o'clock

The Party Hub's **Cues** are big buttons for the moments the party stops for: tap
one and the music fades out, Spotify pauses (in a jam the room pauses with it and
keeps its buffer), one of your saved shows plays, then Spotify resumes and the
music fades back in where it left off. Tap the running cue to end its show early.
In **Settings**, each cue has a name, its show (any saved show, with its length),
and whether the music fades out and back in, and over how many seconds (up to
30). Doorbell and Shot o'clock are there to start with (the doorbell cuts straight
to its show, then fades the music back in); **+ Add cue** adds more.
Cues are kept in config.json (`cues`).

**In a jam, the show's song plays inside the jam's own stream.** The speakers get
one fresh stream, the way a skip jumps at once, that crossfades from the room's
music into the show's song (over the fade-out time; with it off, a straight cut,
as the doorbell does) and, as the song ends, crossfades back into the music where
the room left off (over the fade-in time). The two overlap instead of one stopping
before the other starts, and the speakers are never paused and never load the
show's song in between, so nothing waits. Spotify is paused while the song plays
(the buffer is held) and resumes as the crossfade back begins. Ending a cue early
brings the music back at once, fading in. Each cue's song is decoded ahead of time
(when the studio starts and when cues are saved) and kept in memory; one that is
not ready yet, or longer than ten minutes, plays the way it does without a jam.

The fades are made in the audio itself, never on the speakers' volume: a Google
speaker chimes at every volume change, so a fade on the volume was a string of
beeps.

Where the show plays otherwise:

- **No jam, the studio is casting**: the room stops at once, the show's song goes
  to the speaker or group, and the studio page plays its lights following that
  speaker's position (less its measured delay). Then Spotify resumes.
- **No jam, nothing casting**: the studio page plays the song on this PC with its
  lights.
- **A silent preview jam**: the lights only.

Without a jam there is no audio of ours to fade, so the music just stops and
starts, and Spotify is paused only if something is playing, and resumed only if
it was. Each cue's show is analysed ahead of time, and the studio page keeps every
cue's show loaded and asks for the running cue five times a second, so its lights
start with its song; when the cue is over, the jam's show is taken up again as it
was, not loaded again. A test copy of the studio (`--no-lights`) only ever
rehearses: every step on its real timing, marked *rehearsal* on the Party Hub,
with nothing touched. Cues have not yet been tried on the real speakers.

## The doorbell rings a cue

A Reolink doorbell can ring a cue by itself: in the Party Hub's **Settings**,
**Doorbell**, enter its address on the home network, its username and password
(the ones its Reolink app uses) and which cue it rings (Doorbell to start), and
switch it on. The studio logs in to the doorbell and listens to its own event
push over the home network (reolink_aio, the library Home Assistant's Reolink
integration uses; nothing goes through the cloud). A press runs the cue exactly
as tapping it would; a second press within 10 s is the same visitor, and a press
while a cue is running is noted, not run. The connection is checked every minute
and made again if the doorbell drops off the Wi-Fi; the card shows whether it is
listening, when it last rang, or why it cannot reach it. The settings are kept
in config.json (`doorbell`), the password included (it is never sent back to a
page). A test copy of the studio (`--no-lights`) never connects to the doorbell.

## Rooms

Lights and speakers can be put in rooms (the studio's ⚙ menu, **Rooms…**, or the
Party Hub's Settings): name a room, then tap the lights and speakers that stand
in it (each stands in one room). Every room has a switch, in both places and on
the Party Hub's home screen. Switch a room off (karaoke in the living room while
beer pong goes on in the game room) and:

- its lights fade over 5 seconds from the show to how they were before the show
  took them (their own color and brightness), and are then handed back, exactly
  as when the studio closes;
- its speakers mute at once. They are not faded: a Google speaker chimes at every
  volume change, so a fade was a string of beeps; muting is one change. Each
  one's own level is kept as it is: the Party Hub still shows it (marked *muted*)
  and its slider still sets it, for when the room is back. (A level set while
  muted is only kept, as setting a level on a Google speaker unmutes it.)

Switch it on again and the lights fade back into the show over 5 seconds while
the speakers unmute at their level. Chases and other effects that run
around the room skip the rooms that are off. A room left off stays off when the
studio starts again; when the studio quits, every speaker a room muted plays
again. The levels of muted speakers are kept in config.json (`room_muted`), so
only speakers a room muted are ever unmuted.

A light unticked in the studio is handed back at once. When the show takes a
light, it asks how the light is; that state is also kept in config.json
(`light_states`), so a light that does not answer on busy Wi-Fi still goes back
to how it was last time rather than staying on the show's last frame. A light
handed back gets its own brightness first, while the show still has it (the
show runs it at 100%), and only then leaves the show, so it comes back in its
own color at its own brightness instead of flashing at full brightness first.

The "now and then" extras are picked for each song from its own two colors: the
two hues farthest from both, either side of the middle of the wider gap between
them on the color wheel (purple and green get red and orange, red and blue get
lime and green), so they can be seen ahead of time on the phone.

## Jam mode: someone else picks the music

Somebody runs a Spotify Jam from their phone, adding and reordering songs all
night, and the lights still know what is coming. Press **Jam** in the studio.

**How it works.** Spotify plays on this PC. The studio listens to the PC's own
audio output, holds it in memory, and sends it to the Cast speakers about 90
seconds late (**Stay behind by**; a longer lead gives the analysis more of each
song to work on). In that gap it studies music the room has not heard yet, so a drop
is on the timeline, with a buildup running into it, before anybody hears it.
Spotify is only ever asked what a remote control could ask: what is playing, how
far in, whether it is paused, what is queued. It is never asked for audio, and
no audio is written to disk: it exists only in a rolling memory buffer.

**Learn ahead: the room only seconds behind.** With spotDL installed (below),
the studio learns every queued song before it plays, so the room needs to be only
seconds behind Spotify instead of a whole song. For each song in the Spotify queue
(up to 12, the next to play first), the studio looks it up on YouTube Music by its
Spotify details (an album recording by one of its artists, its name, within 3 s of
its length) and has spotDL download that one. (spotDL's own YouTube Music search came
back empty for songs plainly there, and fell back to music videos.) Failing that,
spotDL's own pick, and then a search of YouTube that only takes the artist's own
channel (their "Topic" channel first, which carries the album audio), never a live
take, a cover or a remix; the full analysis runs on it, in a process of its own at below-normal priority
on at most four cores (the models on the GPU), so the lights and the music always
come first (about 45 s for a 3-minute song, while
the next one downloads), and the copy is deleted the moment it is done: only the
analysis is kept, with the songs the jam learned by listening (`shows/_jam/learned`).
A copy whose length is not within 4 seconds of Spotify's is another version and is
not used. The speakers only ever play Spotify's own audio, relayed as always; the
copy is never heard, so a music video's intro never reaches the room.

The copy and Spotify's audio are checked against each other by ear, so the show
lands on Spotify's beats: from 7 seconds into the song (before the room hears it,
when it is 7 or more seconds behind) the song is lined up, on a clear match only, otherwise at 14
seconds as before; and every 20 seconds while it plays, the last ten seconds heard
are checked against the same stretch of the copy (a fingerprint of the whole song
is kept for this), a small slip put right. If what Spotify plays does not sound like
the copy, at the start or part way (another edit), that song's show is built as it
plays from Spotify's audio, like a first listen, and the copy is not used again. A
copy that does not line up near Spotify's start (a video's intro, a few seconds more
silence) is searched for up to 10 seconds either way, from 30 seconds in, before it is
given up on. A download YouTube refuses (it does now and then) is tried again at once,
twice, and again five minutes later. A seek on the phone within a song learned ahead
(or one of your shows) keeps its show: it carries on from the new place, lined up again
by ear on what is heard after the seek. A
song playing that was not learned ahead (tapped directly, or one Spotify chose after
the queue) is a first listen; if its copy is learned while it plays, the full show
takes over from there.

In the Jam panel each song's badge says where it stands: *analyzed*, *analyzing…*,
*queued*, *first listen*, or *error* when anything failed along the way (no copy
found, the stems, the section model or the genre model failing), with what failed
when the pointer rests on it; the show is made regardless, from what did work. Why a
model would not start goes to `studio.log` and `models.log`, and it is tried again
every 2 minutes. **Learn ahead** in the Jam panel turns it on or off (on wherever spotDL is
installed); with it on, the room stays **Stay behind by** (5 s to start with, at
least 3) behind, and no opening song is needed. A song playing before its copy is learned is a stopgap: its main color at
nearly full brightness, pulsing on the beat, and only its beat is worked out (no
stems, sections, instruments or genre), so the processor goes to the copy; what is
heard is not kept, and the full show takes over when the copy is learned. Less is livelier, at a cost: the
studio hears of a new song about a second late (Spotify is asked once a second), a
speaker holds only that much against a Wi-Fi hiccup, and below 7 s each song starts
on Spotify's clock and may shift a little when it is lined up by ear. 5 to 10 s hides
all of that. **Pre-listen** learns a playlist the same way, from
copies, about a minute a song, without playing anything or tying up Spotify. Setup,
once (spotDL runs in its own Python so its packages never meet the studio's; it
needs ffmpeg, and Deno so YouTube's downloader can fetch every video):

```
py -3.12 -m venv .venv-spotdl
.venv-spotdl\Scripts\python -m pip install spotdl==4.5.2
.venv-spotdl\Scripts\spotdl --download-deno
```

Spotify's own audio is never downloaded: spotDL reads only a song's details from
Spotify. The copies come from YouTube, whose terms do not allow downloading; each
exists only for as long as its analysis takes.

Recording and playback each run in a small process of their own, handing audio
to the studio through shared memory. The analysis is heavy Python work, and when
the audio shared a process with it, playback stalled and skipped and the lights,
which follow playback, lagged along with it. In their own processes the recorder
and player keep exact time however busy the studio gets.
They also ask Windows for real-time audio scheduling, while the models and the
analysis step down to low priority, so a busy analysis pass waits for the sound
card and the lights, never the other way round. If the studio is closed or
killed, they notice and exit instead of holding the sound device.

Each track is one of three kinds, shown as a badge in the panel and the queue:

| | |
|---|---|
| **your show** | The track matches a show you built (by title and artist, or by length; or pin it yourself). Your show plays, lined up to the music by ear. |
| **learned** | The track has played through in a jam before. The studio kept its analysis: beat grid, sections, drops, the riff and the fills. It gets a full auto-built show from the first beat. Numbers only are kept, in `shows/_jam/learned/`. |
| **first listen** | Analysed as it arrives. The show for the part the room has not reached is rebuilt every few seconds as more is learned; what is already playing is never changed under you. About half a minute in, the genre model has heard enough to pick the show style, and what is ahead is rebuilt in it. When it finishes it becomes *learned*. |

**Setting up (once).**

1. Spotify Premium is needed, since the studio pauses and resumes playback.
2. At [developer.spotify.com](https://developer.spotify.com/dashboard) create an
   app. In its settings add the Redirect URI the Jam panel shows you, exactly
   (`http://127.0.0.1:8765/api/spotify/callback`), and tick *Web API*.
3. Paste the app's **Client ID** into the Jam panel and press **Sign in to
   Spotify**. You sign in on Spotify's own page. There is no client secret; the
   studio keeps a refresh token in `config.json`, which is not in git.
4. Choose where Spotify plays, under **Spotify plays in**:
   - **The studio itself** (recommended). While the studio is open it is a
     Spotify speaker called **Light Show Studio**. It runs Spotify's official
     player (the Web Playback SDK) in Microsoft Edge with no window at all, and
     sends Edge's sound to **Digital Audio (S/PDIF)**, an output nobody listens
     to. The studio records that output and plays it to the room late on your
     default output. There is nothing to route by hand and nothing in the
     taskbar. Pick Light Show Studio from your phone, or host a Jam with it and
     let others join and queue songs. It needs a sign-in made after this was
     added (the Jam panel says if yours is older).

     While the studio runs, every Edge window plays into S/PDIF, so Edge you
     open yourself is silent too. When the studio closes it hands Edge back to
     your default output. If the studio crashes, Edge stays on S/PDIF until the
     studio next closes normally, or until you change it back in Windows' sound
     settings (App volume and device preferences).

     Windows' own settings page is what sets an app's output; no program is
     meant to. The studio uses the same unofficial way that tools like
     EarTrumpet do, which a Windows update could break. The Jam panel says so
     if Windows refuses.

     With *Start the jam by itself* ticked, music starting on Light Show Studio
     starts a jam with the settings used last time, so casting to it from your
     phone is all it takes. After **Stop jam** it waits for the music to be
     paused or started again before it starts one by itself.
   - **The Spotify app on this PC.** In Windows' Volume mixer, set the Spotify
     app's output to Digital Audio (S/PDIF).
   To use another hidden output instead of S/PDIF, set `jam_hidden_output` in
   `config.json` to part of its name (a virtual audio cable works well).
5. Under **Play on**, choose where the delayed music is heard: the **Cast**
   speakers (press Cast first), **This PC**, or nowhere, to watch the show
   build without sound. This PC plays on Windows' default output (set
   `jam_room_output` in `config.json` to pin another). The studio refuses to
   start if the default output is the hidden one, since it would record its
   own delayed copy. The lights assume the PC output is heard 0.3 s after it
   is sent; for Bluetooth headphones, which add their own delay, set
   `pc_output_offset_ms` in `config.json` (try 450).

**Casting.** When a Cast speaker is connected, the jam plays on it, whether it
started by itself or was already running on this PC: the sound moves at the
point the room had reached. When the Cast goes away the sound comes back here.

**Cast over Wi-Fi.** The speaker may buffer up to 20 s ahead, so a Wi-Fi dropout
shorter than that goes unheard. If it loses the stream anyway and comes back for
it, the studio restarts the stream at the point the room had reached, so the
lights stay in step (the Jam log says when this happens). A Cast speaker that
is not playing within 15 s (off, or on weak Wi-Fi) hands the room to this PC;
press Cast to try it again. The stream is raw audio: MP3 was tried to lighten
the Wi-Fi load, but the installed encoder (libsndfile 1.2.2) turned music into
noise.

**How the auto-built shows look.**

- **The drums lead.** First the song is pulled apart (Demucs, in the model
  environment, about 3 s a song on the GPU, in memory, never written to disk)
  into drums, bass, vocals and the rest. Kicks and snares are then found both in
  the full mix and in the drum track alone, and for each drum the studio keeps
  whichever is more believable (hits on a steady pattern at a sensible number per
  bar): separation makes most drums far clearer, but a clap can land among the
  "other" instruments and strums can leak into the drums. The drum track also
  says when the drums are playing at all, moment to moment: a hit at a moment the
  drum track is silent is dropped, the beat pulse goes only where the drums play,
  and a song whose drum track is near silent (under 10% of its level) **has no
  drums**: no kicks, no snares, and a calm, chill show unless a style is set by
  hand. A drumless song that swells (its loud parts, the top tenth two seconds at
  a time, reach 0.65 and half again its middle) stays calm, soft effects only, but
  its drops lift a little more, their motion livelier. (Such songs used to get a
  groove show, with flashes on the bass line; on The Wldlfe's *Heaven is a place*
  that was far too much.)
  A calm song's sparkle is
  **Starlight**: single segments glow up to white and fade away slowly, a few at
  a time at random spots along every strip and lamp, like stars coming out, over
  a soft color wash (a handful per 50 segments, each twinkling for a second or two; more
  and a little quicker in its louder parts, fewer and slower as it ends). Stars
  keep their full brightness however the rest of a section is dimmed. Other
  songs get stars in their cute parts only: an intro, breakdown, outro or verse
  with no drums playing and at most half as loud as the song gets (a soft
  intro, a hushed breakdown), the three quietest at most, so they stay special.
  Over 18 songs that was mostly intros, outros and the odd breakdown; songs that
  never go quiet (COIN, ELEY, The 1975) get none. Over 20 songs, 18 drum songs were all at 39% or more and the two drumless
  ones at 1-2%. Without the model environment the drums come from the mix alone.
  The drum pattern is learned per song, in the song's own meter: four beats or
  three, in sixteenths or triplets (a shuffle, 6/8), decided from how tightly all
  its percussion gathers on each division of the beat and whether the kick's
  pattern comes round every 4 beats or every 3. The kick and snare are then
  calibrated per song: every song mixes its drums differently (a deep sub kick
  or a punchy 150 Hz one; a fat 200 Hz snare, a dry 3 kHz crack, a dubstep
  snare around 600 Hz). A drum pattern repeats bar after bar, so each frequency
  band's hits are laid over the sixteen sixteenths of a bar: the kick band is
  the low band whose pattern stands out most, the snare band the one that stands
  out where the kick does not land and stays quiet where only the kick does
  (hi-hats, on every eighth, have no standout places). A snare may also sit on
  top of a kick (dance music: a kick on every beat, a clap on 2 and 4): a kick
  place counts as a snare place where the band jumps far more, for the kick
  band's jump, than at the kick's other places. Every hit in those bands
  counts, rolls and fills included; a hit next to a kick counts as a snare only
  if its band jumps well beyond the kick's own share. Then a check: most real
  snares land on the pattern's places (a backbeat's twin half a bar away
  counts), so if under 60% do, the band was hearing a synth or a vocal and is
  dropped. Over 20 songs of different genres, 8 had a clear snare band and
  gained 1.5-2.5x the snares, concentrated on the backbeat; the rest kept the
  fixed bands; with the clap on top of the kick found as well, 3 more songs got
  one (Mansionz from 50 snares to 152). A song with no clear pattern falls back
  to fixed bands (the kick below 150 Hz; the snare by its crack plus the body a
  hi-hat lacks). The bands chosen are saved with the
  analysis (`drum_calibration`). Where a song's kick is consistent (a produced
  kick), its kicks are then found again by a fingerprint: the average shape of its
  confident kicks over their first 140 ms, from 20 to 400 Hz, which tells a kick
  from a bass note in the same range; songs with live, varied kicks keep the band
  detection, since each song measures how well its fingerprint separates.
  Beat pulse follows the hits: the kick blooms from the center, the snare from
  the ends. It goes only where there are drums (at least 4 hits, one every 4
  beats); a drumless breakdown gets none. A **buildup** is hits all around the
  room: each snare when it is a snare build (so it speeds up exactly as the roll
  does), else each kick and snare, else (no drums found) a build of beats,
  eighths and sixteenths, lights random stretches of the strips and lamps, more
  of them, brighter and snappier toward the drop, over a dim bed; a calm song's
  build is gentle, on the beat, two stretches at most. The lamps are stretches
  of the room there, not blooming on every hit, and the whole-strip strobes and
  bass swells stay out of buildups. The **Drum strobe** flashes on the hits
  themselves and nothing else, on the kicks in drops. How hard
  depends on the style: subtle for groove, clear for dance, full for rave, none
  for chill.
- **The bass.** With the bass separated out, the bass notes and the bass level
  come from the bass track alone, quick enough to follow a wobble. A drop's base
  layer breathes with it (clip setting **Follow the bass**: half for dance and
  rave, 30% for groove, none for chill): brighter as the bass swells, dimmer
  when it drops out for a beat.
- **Each song draws its own mix.** There is no look to pick: shows are made from
  five families of effects, each suited to a vibe, and every song draws its own
  beds, verse movers, motions and drop moves from them, the families as likely as
  they suit the song (its style, drive, brightness, key and genre):
  - *Haunted* (dark, minor, mid-tempo): embers and aurora, meteors and a
    bouncing comet, color battles in the drops.
  - *Neon club* (bright dance music): fast gradients and waves, ripples and
    chases, hyper chases and flashing chases in the drops.
  - *Dreamy* (soft and bright): color washes, aurora and twinkles, slow
    sparkles and orbits, never a flash.
  - *Rock show* (live-drum rock and indie): gradients and breathing, a scanner
    sweeping, alternating and random blocks in the drops.
  - *Storm* (hard, minor, driving): battles and halves, lightning and meteors.
  The family that fits best carries the song, close ones often join in, and any
  other may lend a move or two, so two rock songs are two different shows. A calm
  song draws soft things only, a song without drums never a rock show, and a
  dance or rave song rarely a gentle drop. The draw is seeded by the song, so a
  rebuild gives the same show. Each song uses the first three drop moves it drew
  that fit (in turn; the flashing ones only for dance and rave), and two motions
  (comets, sparkles, meteors, orbits, ripples, a bouncing comet, a scanner),
  alternating from drop to drop. Songs whose mix is mostly haunted and storm get
  thunder on the biggest hits. Over 44 learned songs, 41 drew different drops.
  **Stage steps** is one of the motions and verse movers too: the strip in
  eighths, each flashing in turn and fading, along the strip, back and forth or
  at random (each song its own order). What drifts over a song's intro,
  breakdowns and outro is drawn too: meteors, slow random stage steps, or a few
  sparkles. The hyper chase is for dance and rave drops only. Motions that
  sweep the strip (comets, a scanner, a bounce, meteors, an orbit) are drawn half
  as often as the others, and stage steps a little less: over 57 songs, sweeps
  are 4 in 10 drop motions, and 7 songs got two.
- **Long strips and short lamps play different parts.** With both kinds ticked
  (a short lamp has a dozen segments or fewer, like a 7-segment floor lamp), the
  things that travel (comets, chases, sparkles, scanners) run on the long strips,
  where they have room to move, and the lamps play the drums: **Drum hits** makes
  the whole lamp bloom in the first color on every kick and the second on every
  snare. Where there are no drums the lamps breathe slowly in the other color.
  When a drop's base itself travels, the lamps get a color wash of their own
  under the drums. A clip for one kind of light is drawn in its half of the
  layer (strips on top, lamps below), and the clip inspector's **Lights** sets
  it by hand.
- **Ceiling-long strips play in zones.** A long strip (more than a dozen
  segments) runs the length of a ceiling, so a hit can land on one stretch of it
  and leave the rest dark: **Zone hits** splits each long strip into zones (5 to
  start) and counts a small lamp as one zone of its own. In a hard song's buildup
  (every snare build, and every other buildup otherwise) the first half is dark
  and each hit lights one zone somewhere in the room, never the same one twice
  running, so the light jumps strip to lamp to strip; then more stretches light
  per hit over a dim bed, and the drop lands out of a beat of black. The
  hits are the snares when there are plenty, else kick and snare, else (no drums
  found) a build of beats, then eighths, then sixteenths. Hard drops take turns
  between the whole-strip strobe and the ceiling split in halves (the kick on one
  half, the snare on the other); groove drops flash a fifth of each strip on the
  snare; verses land each snare on a quarter of the ceiling, going round.
- **A drop starts where its bass comes in.** Loudness puts a drop on the bar line
  where the drums hit, but you hear the drop when the bass lands, which in many
  songs is a beat or a few later (Great Good Fine Ok's last drop: drums on the bar
  line, bass one beat after). So a drop whose bass is silent going in moves to the
  first beat of its first bar where the separated bass plays; one whose bass is
  already playing stays. Over 24 songs that moved 10 of 64 drops, by 1 to 3 beats.
  But a drop that comes out of a pause (quiet, then a sudden hit that stays loud)
  starts at the hit, even when the bass fills in a beat later (Baggy Jeans at 0:53).
  And a drop whose bass comes in a beat or two before the bar line (dropping out
  for a moment before it) starts where the bass comes in, if the song stays loud
  from there (Nightly's *the feeling* at 0:42.7, not 0:43.9).
- **Sections are checked against the drums and the hook**, on every analysis,
  saved ones too (so learned songs need no relearning):
  - where a song's hook (the riff it keeps coming back to) plays nearly only in
    its drops, a "drop" the hook never plays in and that has no snare backbeat is
    a verse; a drop starts on the bar near its first hook where the melody lifts,
    when the found start does not lift at all; and a long drop whose hook stops
    for 10 bars or more while the melody falls away is two drops with a verse
    between. Coldplay's *Higher Power*: verse at 0:14 (not 0:17), no drop at 0:52,
    the chorus at 1:43 (not 1:40), the bridge at 2:05 its own verse, the last
    chorus at 2:31;
  - an intro ends where the drums come in to stay, not a bar or two later;
  - a breakdown with the drums playing through it (three quarters of it, with a
    backbeat) is a verse (*Baggy Jeans* 0:11 to 0:44);
  - a verse or breakdown that repeats a drop (its snares, and its hook at the
    same places with nearly its sound, or its sound almost exactly) is a drop:
    Nightly's *don't even think about it* at 1:30 is its 0:34 chorus again.
  **Re-detect** analyses the song again from scratch with the newest analysis:
  everything saved for it is thrown away first.
- **A buildup where the snares build.** A drop with no buildup marked before it,
  where the snares do build into it (half again the song's usual backbeat in its
  last 2 or 4 bars, rolls counting: a rock song's steady snare on 2 and 4 is no
  build; at least 8 in 4 bars, the last 2 as busy as the 2 before, or at least 4
  in 2 bars), gets a buildup section over those bars: the section row says so, it
  can be moved or retyped like any other, and it is built as one. A drop that
  simply arrives (no snares building, or no drums) stays that way. Shows built
  before this have their lifts named buildups the same way when opened.
- **Drops are the brightest thing in a song.** Once a show is built it is looked
  at as the room would see it (twice a second, every light), and evened out: each
  drop gets most of the light of the brightest one (a quieter drop a little less,
  a dim one has its bed and motion turned up and a bed that sinks to black a
  floor), and every other section gets a share of the drops' light by how loud it
  is next to them (a quiet verse about half, a near-chorus up to 85%), never
  outshining the drop it leads to. A section brighter than its share is turned
  down; buildups, fades and stars are left alone.
- **Each song's own flavor.** A few effects only appear where a song has the
  thing they are made for, so a night of songs does not look the same all
  through (the separated tracks are what hear them; see `flavor.py`):
  - *Vocal spotlight*: where the singer is nearly alone (the voice loud, the
    instruments low, no drums), a soft warm glow in the middle of every strip
    swells with the voice; the four longest such moments of a song at most.
  - *Wobble*: where the bass track's level pulses steadily faster than the beat
    (1.5 to 8 wobbles a beat, found four bars at a time) and swells into each
    wobble rather than striking it (a bass playing separate notes at that speed
    is not a wobble), the drop's light opens out from the middle and closes again
    at the wobble's own speed. Over 20 songs: the EDM tracks' drops, and none of
    the indie, rock or grunge songs.
  - *808 glow*: a bass hit that holds, low (under about 110 Hz) and long (still
    at half its peak 0.35 s later), is an 808. In a hip-hop or trap song, where
    they are the song's sound, the room sinks into the song's main color on each
    one and it dies away as slowly as the 808 does (elsewhere a held bass note is
    just a held bass note).
  - *Hat ticks*: through a trap hi-hat roll (the drum track's top band at five
    or more hits a beat for half a beat or more) in a hip-hop, trap or
    electronic song, tiny white ticks race along the strip as fast as the roll
    (in a rock song the same thing is a drummer's fill).
  - *Candlelight*: an acoustic or folk song's quiet parts flicker warm and low,
    segment by segment, like a room lit by candles.
  - *Thunder* and *Heartbeat*, for the darkest looks (Haunted, Storm): the three
    biggest hits inside the drops get a double white flicker and a beat of
    black; an intro or breakdown where only a kick plays beats deep red on it, a
    strong pulse and a softer one just after.
  - *A key change*: when the song moves up a semitone or two in its last part
    and stays there (the key of every 8 s, by the Krumhansl profiles), the
    colors lift with it from there on (in two jam colors, the pair swaps), with
    a white sweep where it happens.
  - *The finale*: the last drop gets a sweep in and everything a little
    brighter, and in a song that goes all out, *Fireworks*: rockets climb the
    strip and burst into falling sparks. Then the song's last hit holds in its
    main color and fades away slowly over the last bars.
  - *Glitch*: in an electronic song, the stutter edits (the music cut for under
    0.4 s mid-phrase, which the cut-to-black moment leaves alone) break the
    strip into jittering slices.
  A jam's first listen gets all of these too: every pass adds what it has
  heard that is still ahead of the room, never twice, and the finale and a key
  change, which need the whole song, come once the jam has heard its end (about
  a minute and a half before the room does).
- **Drum rolls.** In the drum track, a stretch where the hits above the kick come
  three or more to a beat, and twice as thick as the song's own groove, for a bar
  or more, is a roll. Its hits (as close as 46 ms) count as a buildup's hits,
  so the buildup speeds up exactly with the roll, and a roll that runs straight into a drop with no buildup marked
  becomes that drop's buildup. Steady sixteenth-note grooves are not rolls.
- **The song's moments.** The separated tracks also mark the moments a
  person would light by hand:
  - *Bass hits.* A drop built on a big, clear bass line (a bass hit on most
    beats, but not a steady eighth-note pulse) flashes with the bass instead of
    the kick: the bass track's attacks, on the eighth notes of the beat grid,
    where the bass is sounding. In REZZ and Grabbitz's *Someone Else*, the first
    drop's hand-placed red flashes were 27; this found 20 of them, plus 6 more.
    With strips and lamps both ticked, the strips take the bass and the lamps keep
    the drums.
  - *Audio cuts.* Where the whole mix drops to almost nothing for 0.2 to 4 s and
    comes back (against the level around it, so a quiet song's quiet parts do not
    count), the lights fade to black over the gap and return on a white flash with
    the music. A short stutter in the middle of a section is left alone; a cut of
    0.4 s or more, or one that runs into a new section, gets it.
  - *Entrances.* Where the synths and guitars come back after two bars away, on
    a bar line and away from a section change, a color slam marks it and the
    colors flip from there on.
  None of this goes into a calm song.
- **Tempo tops out at 135 bpm.** Anything faster is counted in half time, on the
  half of the beats the kick lands on.
- **Solid colors.** Every palette color is full saturation and brightness; greys
  become white or black.
- **Jam colors.** In the Jam panel, **Colors** sets how every jam song is shown:
  in two colors you pick ahead of time (a primary and a secondary, say purple
  and green for a Halloween party); in **a different pair each song**, from a
  set of pairs (purple/green, purple/orange, green/orange, red/blue to start),
  chosen by the song's vibe (soft or hard, minor or major, as learned the last
  time it played; new songs take turns) and never the same pair twice in a row,
  with **Rainbow for epic songs** giving a song that goes all out (rave, or a
  hard dance track with several drops) rainbow drops; or in its album cover's
  colors (its dominant bright color and a second one from the cover). Each song in the room and in
  *Up next* has its own two color swatches: change them and that track keeps
  those colors whenever it plays again (↺ puts it back to the jam's). In two
  colors the primary leads verses and drops, buildups and breakdowns swap to the
  secondary, and overlays take the other color of the pair, so nothing else
  comes in, except the extras: tick **Now and then** and a few of the small
  accents (a hook strike, a spark, a sweep, a color slam, a meteor shower; about
  a third of them) take two more colors instead, picked from the song's own two
  (they are not shown: a song shows its two colors), unless the song is kept
  to **Just these two colors** (in its color popover), which also keeps it out
  of rainbow drops. Every color box in the Jam panel opens sixteen solid colors
  instead of the system picker. The base,
  the comets and the drums stay in the two. A song with a show you made keeps
  that show's colors.
- **Dual comets** are for drops only.
- **Concert-style layers.** The bed (gradients, aurora, breathe) sets color and
  mood at about 30% (40% in drops); motion runs up to 85%; hits (beat pulse,
  strikes, flashes) own full brightness. The bed and motion *duck under the
  drums* (a per-clip setting): they dip on every kick and snare and swell back,
  so hits land on a dark strip. A buildup that runs into a drop ends in one beat
  of black, and the drop lands out of the dark.
- **Buildups only where the music climbs.** The run-in to a drop is a buildup
  only if its sound opens up (brightens) toward the drop; a song that just
  arrives at its chorus gets none. Songs learned by an older analysis are heard
  afresh the next time they play.
- **Main color.** In the Song panel (click empty timeline), pick a *Main color*
  and press *Rebuild*: the show is built around it and two neighboring hues,
  the way a jam song is built around its album cover. *Auto* goes back to the
  automatic palettes. Setting it does not touch the show until you rebuild.
- **Contrast.** Effects that are mostly dark with bright things moving (comets,
  meteors, orbits, sparks, sweeps) take the opposite colors of the base under
  them: a comet over red is cyan-blue. Buildups and breakdowns run on the
  flipped palette, so the drop lands back on the song's own colors.
- **The lights dim with the music.** When the sound falls well below what is
  usual for its section (a gap in a drop, a sudden hush), every light falls with
  it, down to 4%, whatever the effects are doing. Quiet sections are only a
  little dimmer overall, never below 30%. Turn it off per show with *Dim with
  the music* in the Song panel.
- **A first listen rebuilds what changed.** When the analysis moves a section or
  changes its kind (a buildup that turns out to be a verse), that section is
  rebuilt, the one playing included, from the next beat.

**Starting a night: the opening song.** A buffer only fills while the room
listens to something else, so every jam opens with one of your saved shows
(**Open with**, *If You're Too Shy (Let Me Know)* unless you pick another).
Press **Start jam**: the opener plays in the room with its full show while
Spotify plays, unheard, from the start of the jam's first song. As the opener
ends, the speakers switch to the jam, a whole opener behind, and stay that far
behind all night. So the lights hear each song about a song ahead of the room:
songs up to the opener's length are heard to their end before the room gets to
them, and get their full show from the first beat. A longer opener gives a
longer lead.

- If Spotify starts late (nothing was up yet), the jam simply stays as far behind
  as it got; it never adds silence later to make up the difference.
- A cue during the opener (the doorbell) has the speakers as usual; the jam
  joins once it is over.
- Set **Open with** to *Nothing* to start the old way: the room waits in
  silence until the **Stay behind by** delay (90 s) has filled, then joins.
- A jam that starts by itself (music started on Light Show Studio) opens the
  same way. A pre-listen never does: it plays nothing in the room.

**What the phone does and how the room reacts.**

| On the phone | In the room |
|---|---|
| Add, remove, reorder the queue | Nothing to notice; it just works |
| Pause, resume | The speakers pause and resume within about a second, and the buffer is kept |
| Skip, or tap a different song | By default the room hears the skip when it gets there, the full delay later, like everything else, so the lights see the new song coming. Set *On a skip* to *Jump right away* for the room to join the new song in a second or two instead: that song then runs on a short lookahead, and a few seconds of quiet are added between the next songs until the delay is back. A change in a song's last 30 seconds always counts as the song ending |
| Let a song end | Seamless |

The phone shows what Spotify is playing, which is the delay ahead of the room.
If Spotify's crossfade is on, enter its length in the panel: the studio cannot
read it and needs it to tell a skip from a song that simply ended.

**Sync.** The speaker delay measured by the ordinary Cast sync check carries
over. **Sync check** in the Jam panel measures it again against the relay's own
buffer. It listens until two clear readings agree, and says so rather than
guessing when the music is too repetitive to tell one beat from the next.

**Silent demo.** *Silent demo* runs the whole thing on synthetic songs with a
pretend Spotify: no sound, no speakers, no sign-in. It is the quickest way to
see what a jam looks like, and it is how Jam mode is tested:

```bash
.venv\Scripts\python -m unittest tests.test_jam
```

## How it works

`lightshow/audio.py` captures the input with `sounddevice`, runs a 512-sample
FFT ~90 times a second and extracts loudness, bass/mid/treble energy, 24
log-spaced spectrum bins, a spectral centroid and an energy-based beat
detector. Everything is auto-gain controlled so a quiet mic and a loud line-in
both fill the 0..1 range.

`lightshow/music.py` tracks structure on the raw (not auto-gained) energies:
slow references with a 40 s half-life give an *intensity* relative to the last
minute, short vs. long averages of loudness, brightness and onset density give
a *rising* score, and a bass-absence timer catches breakdowns. A drop fires
when bass slams back after a breakdown or a buildup; sections (chill, groove,
buildup, heavy) switch with hysteresis so they do not flicker.

Intensity is judged against the dynamic range the tracker has heard, so the
first loud section of a session is "heavy" until something louder arrives.
The peak/trough references are saved to `config.json` after each run that
heard real music and restored on the next start, so the show knows your room's
loudness from the first bar. Delete the `calibration` block to reset it.

`lightshow/show.py` maps those features to a color per segment, per light,
for the selected mode and rate-limits the sends.

`lightshow/govee_lan.py` is a small client for the Govee LAN API: discovery
by a scan to UDP 4001, sent to the multicast group 239.255.255.250 and also as
a broadcast on the home subnet (some networks pass no multicast at all: on one
with a VPN adapter installed, only the broadcast found anything), control on
UDP 4003, plus the binary
"razer" real-time protocol (`BB 00 len cmd data.. xor`, cmd `B1` = enable,
`B0` = per-LED colors). The light leaves real-time mode by itself after about
a minute without packets, so the show re-enables it periodically.

## Segment counts

Govee does not report how many zones a light has, so the tool cannot know
whether your light has 6, 12 or 20 controllable segments. Run `probe` once
per light, count the zones that light up, and save it with `--save N`. Sending
more colors than the light has segments is harmless; the extras are ignored.

## Pinning to the taskbar

The studio's window is its own app on the taskbar, "Light Show Studio" with its
icon, not Chrome: right-click its taskbar button and choose *Pin to taskbar*. The
pinned button starts the whole studio (the same as the desktop shortcut). Windows
files windows by their app ID, and the studio sets its own on its window as it
opens (`lightshow/studio/window_identity.py`). If you pinned Chrome by accident
before, unpin that one.

## Priority for the lights on Wi-Fi

The studio marks its light packets as real-time traffic (DSCP 46), so an access
point that honors the mark (WMM, on by default in most) sends them ahead of other
traffic, such as a Cast stream. Windows drops marks set by ordinary programs, so
allow it once, in PowerShell run as administrator:

```powershell
New-NetQosPolicy -Name "Light Show Studio lights" -AppPathNameMatchCondition "pythonw.exe" -IPProtocolMatchCondition UDP -IPDstPortMatchCondition 4003 -DSCPAction 46 -NetworkProfile All
New-NetQosPolicy -Name "Light Show Studio lights console" -AppPathNameMatchCondition "python.exe" -IPProtocolMatchCondition UDP -IPDstPortMatchCondition 4003 -DSCPAction 46 -NetworkProfile All
```

It only applies to Python programs sending to port 4003, which is Govee's control
port. `/api/output` reports `wifi_priority: true` once the policy is there. To
undo it: `Remove-NetQosPolicy -Name "Light Show Studio lights*"`. This helps when
the lights compete with other traffic; it cannot fix a weak signal.

## Capturing what is playing on the PC instead of a mic

`sounddevice` on Windows does not expose WASAPI loopback devices. Options:
enable "Stereo Mix" in Windows sound settings and pick it with `-a "Stereo Mix"`,
use a virtual cable (VB-Cable) as the default output and pick its input here,
or route a physical output back into a line-in.
