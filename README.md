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

1. **Pick lights** on the left. The strip you started with is ticked by
   default; tick others to include them. Set each light's segment count here.
2. **Auto-build show** generates a complete show from the detected structure
   as a starting point: aurora in intros, gradients and beat pulses in verses,
   an accelerating riser through buildups, a white blast and rainbow on drops,
   and fades on the outro, with the palette changing per section.
   **How hard it goes depends on the kind of music.** Each song gets a show
   style:

   | Style | Typical music | What the lights do |
   |---|---|---|
   | Chill | ambient, jazz, folk, soul, classical | Slow fades and drifting light. No strobes, no flashes |
   | Groove | rock, pop, hip hop, indie, R&B | Moves on the beat. Drops lift without flashing; buildups strobe to 1/8 notes |
   | Dance | house, trance, EDM, metal | Drops flash on the beat; buildups strobe to 1/16 notes |
   | Rave | dubstep, drum and bass, techno | Everything: 1/32 strobes, blasts, rushes on drum fills |

   The genre comes from a model that listens to the song: MIT's Audio
   Spectrogram Transformer, trained on Google's AudioSet (open source, about
   350 MB, downloaded once, runs in the model environment described below). In
   Jam mode the genres Spotify lists for the artist count too. The song's own
   energy can move it a little, but a loud rock song is still rock. The Song
   panel (nothing selected) shows what the model heard and lets you choose the
   style yourself; press **Auto-build show** again to rebuild in it.
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
   color swatches, or drag a palette card from the library straight onto a
   clip. Clips without their own choice use the project's default palette
   (double-click a palette card to set it). Dropping an *effect* onto an
   existing clip swaps the effect and keeps the clip's timing, layer, colors
   and opacity. Click the eye on a layer name to hide that layer while you
   work on the others.
5. **Play** with **Lights on** ticked to drive the real lights in sync with a
   live preview of every strip. The **Offset** box sends the lights a few
   milliseconds ahead of the audio to line them up by eye.
   **Lights on** comes on by itself when the studio opens, unless you last
   switched it off.

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
still gets it wrong, press **÷2** to halve the tempo; **×2** doubles it.
Clips keep their place in the song, so nothing moves, but a "1 beat" effect
now means the new, longer beat. If the bar lines sit in the wrong place, press
**Bar−1** or **Bar+1** to shift the downbeat a beat at a time, or right-click
the waveform on a downbeat and choose *Set downbeat to nearest beat*. Sections
snap to bar lines, so getting this right first saves nudging every boundary.

**Beats**: turn on **Beat edit** to fix the grid. Click the waveform to add a
beat, tap <kbd>Space</kbd> along with the music while it plays to lay beats
down, drag a beat to move it, <kbd>Delete</kbd> to remove one. Or set the
**BPM** (or **Tap** it) and press **Regrid** for a fresh even grid, and nudge
the whole grid with the 10 ms buttons. **Bar+1** moves the downbeat.

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
%LOCALAPPDATA%\Programs\Python\Python312\python.exe -m venv %LOCALAPPDATA%\lanlightshowenv-songformer
%LOCALAPPDATA%\lanlightshowenv-songformer\Scripts\python.exe -m pip install torch torchaudio --index-url https://download.pytorch.org/whl/cu126
%LOCALAPPDATA%\lanlightshowenv-songformer\Scripts\python.exe -m pip install "transformers==4.51.1" huggingface_hub librosa soundfile scipy numpy ema-pytorch loguru muq omegaconf tqdm x-transformers einops nnAudio
```

The weights download automatically on first use. The environment lives on
the local disk on purpose: a network share is far too slow for PyTorch.
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

**Getting around**: the strip across the top is an overview of the whole song
(section colors, a summary waveform and the drop markers). The bright box on
it is what you are looking at: drag it to scroll, drag its edges to zoom, or
click anywhere to jump there. In the timeline the wheel zooms around the
pointer, <kbd>Shift</kbd>+wheel pans, and <kbd>Ctrl</kbd>+wheel scrolls the
layer stack. <kbd>Space</kbd> plays and pauses, clicking the waveform seeks,
dragging the ruler scrubs, and <kbd>+</kbd>/<kbd>-</kbd>/<kbd>Fit</kbd> zoom
from the keyboard.
<kbd>Ctrl</kbd>+<kbd>Z</kbd>/<kbd>Y</kbd> undo/redo, <kbd>Ctrl</kbd>+<kbd>S</kbd>
to save. Everything auto-saves a couple of seconds after you stop editing.

There are around 40 effects grouped as slow (aurora, gradient, breathe, wave,
twinkle, embers), groove (pulse, chase, comets, dual comets, meteors, pulsar,
orbit, color battle, spectrum, scanner, ripples, sparkles), fast
(strobe, blackout strobe, hyper chase, lightning, chaos, glitch,
halves) and one-shot accents (drop blast, riser, fades, flash, slam, sweep
out). Effect timing is always in beats, so a "1 beat" pulse stays locked to
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

The lead is taken from the stereo side channel (left minus right), which
cancels anything panned dead center, so lead vocals and kick largely drop out
and the wide instruments remain. That is why it follows the synth rather than
the singer.

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
first beats is extra. The buildup riser is two effects: **Riser** fills the strip toward the center
and heats up, and **Strobe riser** is a separate blackout strobe on the layer
above that accelerates smoothly to its maximum (default 8 flashes a beat)
right at the drop. Auto-build stacks them; use one without the other if you
prefer.

## Jam mode: someone else picks the music

Somebody runs a Spotify Jam from their phone, adding and reordering songs all
night, and the lights still know what is coming. Press **Jam** in the studio.

**How it works.** Spotify plays on this PC. The studio listens to the PC's own
audio output, holds it in memory, and sends it to the Cast speakers about 45
seconds late. In that gap it studies music the room has not heard yet, so a drop
is on the timeline, with a buildup running into it, before anybody hears it.
Spotify is only ever asked what a remote control could ask: what is playing, how
far in, whether it is paused, what is queued. It is never asked for audio, and
no audio is written to disk: it exists only in a rolling memory buffer.

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

**How the auto-built shows look.**

- **The drums lead.** The analysis finds the kick and snare hits themselves (the
  kick below 150 Hz; the snare by its crack plus the body that a hi-hat lacks).
  Beat pulse follows them: the kick blooms from the center, the snare from the
  ends. It is in every section, strongest in verses and drops, and stays dark
  where there are no drums. Set *Follows* to *every beat* for the old behavior.
- **Tempo tops out at 135 bpm.** Anything faster is counted in half time, on the
  half of the beats the kick lands on.
- **Solid colors.** Every palette color is full saturation and brightness; greys
  become white or black. A jam track takes its palette from the album cover: its
  dominant bright color and a second one from the cover.
- **Dual comets** are for drops only; the **strobe riser** for the rave style
  only.
- **A first listen rebuilds what changed.** When the analysis moves a section or
  changes its kind (a buildup that turns out to be a verse), that section is
  rebuilt, the one playing included, from the next beat.

**Starting a night.** A buffer only fills while the room listens to something
else, so there are two ways in:

- **Start jam** waits in silence until the buffer is full, then the speakers join.
- **Start behind this song** is the good one. Cast one of your own shows and
  press play, then press this. Spotify is held paused, released when your song
  has exactly the delay left to run, and the speakers switch to the jam as it
  ends. The room hears one song follow another.

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

`lightshow/govee_lan.py` is a small client for the Govee LAN API: multicast
discovery on 239.255.255.250:4001, control on UDP 4003, plus the binary
"razer" real-time protocol (`BB 00 len cmd data.. xor`, cmd `B1` = enable,
`B0` = per-LED colors). The light leaves real-time mode by itself after about
a minute without packets, so the show re-enables it periodically.

## Segment counts

Govee does not report how many zones a light has, so the tool cannot know
whether your light has 6, 12 or 20 controllable segments. Run `probe` once
per light, count the zones that light up, and save it with `--save N`. Sending
more colors than the light has segments is harmless; the extras are ignored.

## Capturing what is playing on the PC instead of a mic

`sounddevice` on Windows does not expose WASAPI loopback devices. Options:
enable "Stereo Mix" in Windows sound settings and pick it with `-a "Stereo Mix"`,
use a virtual cable (VB-Cable) as the default output and pick its input here,
or route a physical output back into a line-in.
