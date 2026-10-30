# Bulb Show

A small app of its own beside Light Show Studio: looping light shows for one bulb at a time.
Each show is one sound, one Google speaker and one Govee bulb (made for the H6008). Any number
of shows can loop at once, for example a witch laughing on one speaker with its bulb in one room,
and a ghost on another speaker and bulb in the next room.

Start it with **Bulb Show.vbs** in this folder, or `.venv\Scripts\python -m bulbshow` from the
project folder. It opens on http://127.0.0.1:8770.

## Making a show

1. **+ New show**, then drop a sound anywhere on the editor (mp3, m4a, wav...).
2. Drag **Color**, **Pulse** or **Lightning** onto a layer. Click a clip to set its color,
   brightness, fade, breath speed or number of flashes. Drag a clip to move it; drag its ends to
   resize it. Delete removes it; Ctrl+Z undoes.
3. Layers stack: lightning on layer 2 over a dim color on layer 1 flashes out of that glow.
   **Scene** plays one of the bulb's own effects from the Govee app (a cloud bulb only). The bulb
   runs it by itself, so it costs one command however long it plays. A color on a higher layer
   interrupts it and the scene starts again afterwards: a pulsing scene with red every 20 seconds
   costs about 360 commands an hour. On screen a scene shows as a rainbow, since only the bulb knows
   what it looks like. Effects you make in the Govee app (DIY: gradient, jumping, twinkle, breath,
   simple or combo) are scenes too, listed first under "Your effects"; one saved while Bulb Show is
   open shows up with **↻ Refresh list** at the bottom of the scene picker. (Govee's cloud can play
   them but not make them, so they are made in the Govee app.)
4. Pick the speaker and the bulb at the top. The volume slider sets the show's own volume, inside
   its stream, so the speaker's volume is never changed (Google speakers chime on every change).
5. ▶ plays it on the speaker and bulb, looping, from the playhead. 🎧 switches ▶ to preview on
   this PC instead (the sound plays here and only the bulb on screen lights).

The strip under the waveform is exactly what the bulb will show.

## Running shows

The show list's ▶ on each card runs that show on its own; **Run all** starts every show that has
a sound, a speaker and a bulb. Two running shows can never share a speaker or a bulb. Like Light
Show Studio, closing the window ends everything: every show stops, each bulb is put back as it was
before its show, and the app quits. Minimizing the window keeps the shows going. Closing Light Show
Studio closes Bulb Show too, the same way, however Bulb Show was started.

The light follows what the speaker reports it is playing, less the speaker's delay as measured in
Light Show Studio (`cast_offsets` in the studio's config.json). If a bulb still looks early or
late, ⚙ → Light timing nudges that show.

## From the Party Hub

Light Show Studio's Party Hub lists every show that is ready (a sound, a speaker and a bulb) under
**Lights → Bulb Shows**, each with an on/off switch. Switching one on while Bulb Show is closed
starts it without a window (opening Bulb Show then shows the running app). Started that way, it
quits by itself about 30 seconds after its last show is switched off, unless its window is open. A show is never started on the speaker the party is casting to. Under
Settings, a speaker playing a bulb show carries the show's name, and its volume slider works as for
any other speaker.

## The bulb: LAN or Govee's cloud

A bulb that answers on the LAN (LAN Control switched on in the Govee Home app, if the bulb offers
it) is driven directly, twenty colors a second. A bulb that doesn't is driven through Govee's cloud,
which needs an API key: Govee Home app → Profile → ⚙ → Apply for API Key; paste it under ⚙.

The cloud is slow and rationed: about two commands a second per bulb, and **10,000 commands a day
for the whole account**. A cloud bulb only gets changes, so pulses step between high and low and
flashes stay lit at least 0.3 s. The editor shows what a show costs per hour (≈ N cloud/h) and the
show list counts what today has used. A steady color costs nothing while it loops; every flash
costs two. Past the daily limit the cloud bulbs wait until the next day and the sound plays on.

Only bulbs are offered (Govee's H600x/H601x models, the H6008 among them); the account's strips,
downlights and lamps are left out. Tested on a real H6008 and Office Home 2 (October 2026): a cloud
command takes about 0.15 s, so cloud changes are sent that much early; black (color 0) is accepted;
the H6008 lists 63 scenes of its own (Halloween, Thunderstorm, Fire, Candlelight...).

Shows, sounds and the API key stay on this computer (`shows/` and `config.json` here are not in git).
