/* Light Show Studio: sequencer UI, playback, light output. */
(() => {
  'use strict';
  const $ = s => document.querySelector(s);
  const clamp = (x, a, b) => x < a ? a : x > b ? b : x;
  const frac = x => x - Math.floor(x);
  const uid = () => Math.random().toString(36).slice(2, 10);

  // ------------------------------------------------------------------ state
  const S = {
    project: null, analysis: null, name: '',
    audio: { ctx: null, buffer: null, source: null, startCtx: 0, startPos: 0, playing: false, pos: 0 },
    view: { pps: 40, scroll: 0, sel: null, multi: [], drag: null, hoverDrop: null, mouse: null },
    lights: [], ws: null, wsOpen: false, outputActive: false, lastLightSend: 0,
    cast: { active: false, device: '', state: 'IDLE', pos: 0, at: 0, poll: null, devices: [] },
    defaults: { castOffset: 0, castOffsets: {} },
    undo: [], redo: [], saveTimer: null, lastFrame: null,
  };
  const HEADER = 96, ROW = { ruler: 22, sections: 20, wave: 84, lane: 48 };
  // The canvases draw in "logical" pixels scaled by UI, which follows the page's text size (the
  // stylesheet sizes that to the window). Sizes and mouse positions on the timeline go through it.
  let UI = 1;
  function updateUI() { const fs = parseFloat(getComputedStyle(document.documentElement).fontSize) || 13; UI = Math.max(0.85, Math.min(1.7, fs / 13)); }
  updateUI(); window.addEventListener('resize', updateUI);
  const TW = () => tl.clientWidth / UI, TH = () => tl.clientHeight / UI;
  const SECTION_TYPES = ['intro', 'verse', 'buildup', 'drop', 'breakdown', 'outro'];
  const SECTION_COLORS = { intro: '#4b5563', verse: '#3b82f6', buildup: '#f59e0b', drop: '#ef4444', breakdown: '#8b5cf6', outro: '#6b7280' };
  const DEFAULT_LANES = 4;
  const newLane = () => ({ name: '', clips: [] });

  const tl = $('#timeline'), tctx = tl.getContext('2d');
  const ov = $('#overview'), ovctx = ov.getContext('2d');
  const pv = $('#preview'), pctx = pv.getContext('2d');

  // ------------------------------------------------------------------ project model
  /** Sections as the lights should see them: two of the same kind side by side are one (a second
   *  breakdown right after a breakdown only restarted every effect), and a sliver under 2 seconds
   *  goes to the section before it. */
  function tidySections(list) {
    const join = (arr, keep) => {
      const out = [];
      for (const s of arr) { const prev = out[out.length - 1]; if (prev && keep(prev, s)) prev.end = Math.max(prev.end, s.end); else out.push(s); }
      return out;
    };
    let out = join((list || []).map(s => ({ start: s.start, end: s.end, type: s.type })), (a, b) => a.type === b.type);
    out = join(out, (a, b) => b.end - b.start < 2);                                  // slivers go to the section before
    if (out.length > 1 && out[0].end - out[0].start < 2) { out[1].start = out[0].start; out.shift(); }
    return join(out, (a, b) => a.type === b.type);                                  // a sliver may have joined two alike
  }

  /** How many beats of snare buildup run into beat `end`: 16 when the last 4 bars hold half again the
   *  song's usual snares (rolls count; at least 8) and the last 2 bars are at least as busy as the 2
   *  before, else 8 when the last 2 bars hold half again the usual (at least 4), else 0: no build there
   *  to call a buildup. The usual is the song's backbeat (its typical 2 bars that have a snare at all):
   *  a rock song's snare on 2 and 4 is not a build, however steady (it used to make every section
   *  before a drop one: Coldplay's Higher Power). */
  function snareBuild(end, room = 16) {
    const a = S.analysis || {}, ev = a.events || {}, hits = [...(ev.snare || []), ...(ev.roll || [])].map(e => timeToBeat(e[0]));
    const n = (b0, b1) => hits.filter(b => b >= b0 && b < b1).length;
    if (a._backbeat === undefined) {                               // snares in a typical 8 beats that have any
      const w = []; for (let b = 0; b + 8 <= timeToBeat(a.duration || 0); b += 8) { const k = n(b, b + 8); if (k) w.push(k); }
      w.sort((x, y) => x - y); a._backbeat = w.length >= 4 ? w[Math.floor(w.length / 2)] : 0;
    }
    const usual = a._backbeat;
    // ...and clearly more of them than the bars before (half again): a song with a busy snare all through
    // passed on its count alone, and Seeb's Grip, Heads Will Roll and Guiding Light got buildups where nothing built
    if (room >= 16 && n(end - 16, end) >= Math.max(8, 3 * usual) && n(end - 8, end) >= n(end - 16, end - 8) && n(end - 16, end) >= 1.5 * n(end - 32, end - 16)) return 16;
    if (room >= 8 && n(end - 8, end) >= Math.max(4, 1.5 * usual) && n(end - 8, end) >= 1.5 * n(end - 16, end - 8)) return 8;
    return 0;
  }
  /** A drop with no buildup marked before it, where the snares do build into it, gets one: those last
   *  bars of the section before become a buildup, so the section row says so and it can be moved or
   *  retyped like any other. Not in a calm song, and not where nothing builds (a drop that simply
   *  arrives stays that way). On the project's own beat grid. */
  function withBuildups(sections) {
    if (!P() || !P().beats || !P().beats.length || showStyle() === 'chill') return sections;
    const out = [];
    for (let i = 0; i < sections.length; i++) {
      const s = sections[i], next = sections[i + 1];
      const b0 = Math.round(timeToBeat(s.start)), b1 = Math.round(timeToBeat(s.end)), len = b1 - b0;
      const lift = next && next.type === 'drop' && !['buildup', 'drop'].includes(s.type) && len >= 12 ? snareBuild(b1, len >= 24 ? 16 : 8) : 0;
      if (lift) {
        const cut = beatToTime(b1 - lift);
        out.push({ ...s, end: cut }, { start: cut, end: s.end, type: 'buildup', lift: true });
      } else out.push(s);
    }
    return out;
  }

  function newProject(a, name) {
    return {
      version: 1, name, bpm: a.bpm, phase: a.downbeat_phase, beats: a.beats.slice(),
      sections: tidySections(a.sections),
      drops: a.drops.slice(), palettes: JSON.parse(JSON.stringify(Effects.PALETTES)), defaultPalette: 'Neon',
      paletteClips: [], lanes: Array.from({ length: DEFAULT_LANES }, newLane),
      settings: { latency: 30 },
    };
  }
  const P = () => S.project;

  function timeToBeat(t) {
    const b = P().beats; const m = b.length; if (m < 2) return t * P().bpm / 60;
    if (t <= b[0]) return (t - b[0]) / (b[1] - b[0]);
    if (t >= b[m - 1]) return m - 1 + (t - b[m - 1]) / (b[m - 1] - b[m - 2]);
    let lo = 0, hi = m - 1;
    while (hi - lo > 1) { const mid = (lo + hi) >> 1; if (b[mid] <= t) lo = mid; else hi = mid; }
    return lo + (t - b[lo]) / (b[hi] - b[lo]);
  }
  function beatToTime(x) {
    const b = P().beats; const m = b.length; if (m < 2) return x * 60 / P().bpm;
    if (x <= 0) return b[0] + x * (b[1] - b[0]);
    if (x >= m - 1) return b[m - 1] + (x - (m - 1)) * (b[m - 1] - b[m - 2]);
    const k = Math.floor(x); return b[k] + (x - k) * (b[k + 1] - b[k]);
  }
  const snapVal = () => parseFloat($('#snap').value);
  function snapBeat(x) { const s = snapVal(); if (!s) return x; const ph = P().phase; return ph + Math.round((x - ph) / s) * s; }
  // the playhead follows the Snap setting too when you click or drag it; Alt lets it go anywhere
  function snapTime(t, e) { if (!snapVal() || (e && e.altKey)) return t; return clamp(beatToTime(snapBeat(timeToBeat(t))), 0, duration()); }
  const barOf = b => Math.floor((b - P().phase) / 4);
  const barLabel = b => { const bar = barOf(b); const beat = Math.floor(b - P().phase - bar * 4); return `${bar + 1}.${beat + 1}`; };
  const duration = () => S.analysis ? S.analysis.duration : 0;
  const endBeat = () => timeToBeat(duration());

  /** a clip's own colors: a named palette, a custom color list, or the palette lane */
  function clipPalette(clip, fallback) {
    const cp = clip.palette;
    if (Array.isArray(cp) && cp.length) return cp.map(Effects.hex2rgb);
    if (typeof cp === 'string' && P().palettes[cp]) return P().palettes[cp].map(Effects.hex2rgb);
    return fallback;
  }
  function currentPalette(b) {
    const name = P().defaultPalette;
    return (P().palettes[name] || P().palettes[Object.keys(P().palettes)[0]]).map(Effects.hex2rgb);
  }
  /** the hex colors a clip renders with */
  function clipPaletteHex(clip) {
    const p = P(); const cp = clip.palette;
    if (Array.isArray(cp) && cp.length) return cp;
    if (typeof cp === 'string' && p.palettes[cp]) return p.palettes[cp];
    return p.palettes[p.defaultPalette] || p.palettes[Object.keys(p.palettes)[0]] || ['#888'];
  }
  /** older projects had a palette lane: bake it into the clips, then drop it */
  function migrateProject(p) {
    for (const lane of p.lanes) lane.hidden = !!lane.hidden;
    // Lead trace and Bass note used to color by note by default, ignoring the clip's colors: once, switch that off
    p.settings = p.settings || {};
    if (!p.settings.byNoteOff) {
      for (const lane of p.lanes) for (const c of lane.clips) if ((c.effect === 'leadtrace' || c.effect === 'bassnote') && c.params) c.params.byNote = false;
      p.settings.byNoteOff = true;
    }
    // the old color-scramble Glitch (changes per beat, 1 to 16) is Scramble now: the one-shot Glitch took its name
    for (const lane of p.lanes) for (const c of lane.clips) if (c.effect === 'glitch' && c.params && (c.params.rate >= 1 || 'overlay' in c.params)) c.effect = 'scramble';
    // the strobe riser used to speed up smoothly between two rates; now it steps through note values
    for (const lane of p.lanes) for (const c of lane.clips) if (c.effect === 'strobeRiser' && c.params && c.params.startRate !== undefined) {
      const note = (r, opts) => opts.reduce((best, o) => Math.abs(Math.log2(o / r)) < Math.abs(Math.log2(best / r)) ? o : best, opts[0]);
      const from = note(c.params.startRate || 1, [1, 2, 4]); const to = Math.max(from * 2, note(c.params.maxRate || 8, [2, 4, 8]));
      c.params.from = String(from); c.params.to = String(Math.min(8, to));
      delete c.params.startRate; delete c.params.maxRate;
    }
    // the strip-filling riser is gone: a show that had one gets Build-up hits over the same stretch,
    // on the same drums (hits all around the room instead of a strip filling up)
    for (const lane of p.lanes) for (const c of lane.clips) if (c.effect === 'riser') {
      const follow = { drums: 'drums', snare: 'snare', beat: 'beat' }[(c.params || {}).follow] || 'drums';
      c.effect = 'buildhits'; c.params = { ...Effects.defaults('buildhits'), follow };
    }
    // speeds once worked out as 2 / 1.3 (1.538… beats) snap to the nearest quarter beat
    for (const lane of p.lanes) for (const c of lane.clips) {
      const e = Effects.byId[c.effect]; if (!e || !c.params) continue;
      for (const [k, d] of Object.entries(e.params)) if (d.type === 'beats' && typeof c.params[k] === 'number' && Math.abs(c.params[k] * 4 - Math.round(c.params[k] * 4)) > 1e-6)
        c.params[k] = Math.max(0.25, Math.round(c.params[k] * 4) / 4);
    }
    // shows built before buildups were sections of their own: a lift (Build-up hits ending right on a
    // drop, inside another kind of section) gets its stretch named a buildup, clips untouched
    if (p.beats && p.beats.length && p.sections) {
      const bt = t => { const b = p.beats; let i = 0; while (i < b.length - 2 && b[i + 1] <= t) i++; return i + (t - b[i]) / Math.max(1e-6, b[i + 1] - b[i]); };
      const tb = x => { const b = p.beats, k = Math.max(0, Math.min(b.length - 2, Math.floor(x))); return b[k] + (x - k) * (b[k + 1] - b[k]); };
      const lifts = p.lanes.flatMap(l => l.clips.filter(c => c.effect === 'buildhits'));
      const out = [];
      p.sections.forEach((sc, i) => {
        const next = p.sections[i + 1], end = Math.round(bt(sc.end));
        const lift = next && next.type === 'drop' && !['buildup', 'drop'].includes(sc.type) && snareBuild(end) > 0
          && lifts.find(c => Math.abs(c.start + c.len - end) <= 1 && c.start > bt(sc.start) + 1);
        if (lift) { const cut = tb(lift.start); out.push({ ...sc, end: cut }, { start: cut, end: sc.end, type: 'buildup', lift: true }); } else out.push(sc);
      });
      p.sections = out;
    }
    // lights are chosen once for the room, never by the song
    delete p.lights;
    for (const lane of p.lanes) for (const c of lane.clips) delete c.lights;
    // presets added since the project was made (Rainbow) show up without touching edited ones
    for (const [name, cols] of Object.entries(Effects.PALETTES)) if (!p.palettes[name]) p.palettes[name] = cols.slice();
    // solid colors only: palettes saved before that rule get their pastels and dull shades made solid
    for (const name of Object.keys(p.palettes)) p.palettes[name] = Effects.solidPalette(p.palettes[name]);
    // the Rainbow effect and the "rainbow" color choice are now the Rainbow preset on an ordinary effect
    for (const lane of p.lanes) for (const c of lane.clips) {
      // both painted a rainbow whatever colors the clip carried, so they keep looking that way
      if (c.effect === 'rainbow') { c.effect = 'gradient'; c.params = { ...Effects.defaults('gradient'), speed: c.params.speed, direction: c.params.direction, intensity: c.params.intensity, stretch: 1 / (c.params.stretch || 1) }; c.palette = 'Rainbow'; }
      if (c.params && c.params.color === 'rainbow') { c.params.color = 'gradient'; c.palette = 'Rainbow'; }
      // Flash was always white and Chaos always rainbow; now they take the clip's colors, so pin the old look
      if (c.effect === 'whiteflash' && !c.params.color) { c.params.color = 'first'; if (!c.palette) c.palette = ['#ffffff']; }
      if (c.effect === 'chaos' && !c._colorsChosen) { c.palette = 'Rainbow'; c._colorsChosen = true; }
    }
    if (p.paletteClips && p.paletteClips.length) {
      for (const lane of p.lanes) for (const c of lane.clips) {
        if (c.palette) continue;
        const pc = p.paletteClips.find(k => c.start >= k.start && c.start < k.start + k.len);
        if (pc) c.palette = pc.palette;
      }
    }
    p.paletteClips = [];
  }

  // ------------------------------------------------------------------ undo / save
  function snapshot() { return JSON.stringify(P()); }
  function beginChange() {
    if (!P()) return; S.undo.push(snapshot()); if (S.undo.length > 100) S.undo.shift(); S.redo.length = 0;
    if (!S.building) P().handEdited = true;                          // changed by hand: never rebuilt by itself
  }
  // How shows are built (the effects chosen for each part of a song, how hard each style goes) is
  // versioned: SHOW_VERSION goes up with every change to it. A saved show records the version it was
  // built with, and the analysis it was built on; one built by older logic is rebuilt when it is opened
  // (in the studio, then saved, the old one kept as project.before-rebuild.json; in a jam or as a cue,
  // in memory), unless it was changed by hand since it was built, which only says so.
  // Saved shows built by an older SHOW_VERSION are built again. What each version changed:
  //   3: each song draws its own mix of effects
  //   4: drumless songs are always calm
  //   5: a backbeat is no buildup
  //   6: stage steps, quicker stars, no hyper chase in groove
  //   7: sweeping motions half as likely
  //   8: sections start after a gap
  //   9: sections on bar lines, outro where it rings out
  //   10: one-hits unrationed, star colors, buildups glow
  //   11: no Alternate drops
  //   12: the energy meter in rock drops
  //   13: the poles as a stereo pair
  //   14: white hits, room sweeps, moody breakdowns
  //   15: built for every light in the rooms
  //   16: moody breakdowns two bars, party rooms only
  //   17: effects start from anywhere, the scanner crosses both rooms, no energy meter in groove
  //   18: hyperchase in rave only
  //   19: white snare strobe in clear EDM builds
  //   20: that strobe alone
  //   21: drops never move like their verses
  //   22: no breakdown fade, black as a color, fill one-shots, no same-color motion, a wilder rave
  //   23: one-shots on what the separated tracks hear
  //   24: one-shots solid, on top, in a color apart, glitches, fill floods
  //   25: white never leads a drop
  //   26: white motion in a third, black swipes
  //   27: heavy drops chopped by the drums
  //   28: black swipes that show, No Limits beds duck harder
  //   29: fewer black swipes
  //   30: fewer sweeps
  //   31: sweeps rush, drift, bounce and ramp
  //   32: No Limits from 55 palettes
  //   33: groove cuts black only in moments, colors barely by genre
  //   34: groove hits in one color a section
  //   35: hardcore drops in pure solid colors
  //   36: No Limits palettes: near-twins on the LEDs retired, fire, neon, acid, ice and tropical added
  //   37: shows by scene (21 kinds of music, from the genre model) instead of five vibes; buildups in five
  //       kinds; how hard a section goes follows its loudness within the song
  //   38: buildups only where something builds (the snares speeding up, not just busy)
  //   39: the kitchen's meteor tubes: the show across them, meteors falling down them
  //   40: real fills only (a burst, a bar or less); the tubes' meteors are moments, their shapes per section
  //   41: the motion over a bed never in the bed's own colors (steps, bounce, scanner, chase too); hitswap drops
  //       for dance, rave and heavy scenes only
  //   42: know the song (songmap.py): a part heard again gets its look, the repeats grow to a peak, a breath
  //       before each release, the key moments' own moves; the strings breathe with the bass
  //   43: no light follows the singer's voice (no concert does): no voice orbs on the tubes, the vocal
  //       spotlight a steady glow while the singer is nearly alone
  //   (calm music, calmAt: brightness only swells, set as the show plays, not a rebuild)
  //   44: the words (lyrics.py): a color sung turns the room that color for a moment, a big word (fire,
  //       lights, stars, thunder, heart...) gets its hit, the hook line the same room sweep every time
  //   45: a long EDM snare build strobes white alone for its last 16 beats only; before, a build like any other
  //   46: the build's white strobe holds each snare 0.12 s (it was 0.05: one frame of the lights), never past 60%
  //       of the way to the next snare (drumstrobe), so a fast roll still strobes
  //   47: two colors (the jam's pair) the other way round, the primary on the hits; put back the same night
  //   48: a color sung in the lyrics holds the room in it for a bar (3 beats, then a beat's fade), not a 2-beat blip
  const SHOW_VERSION = 48;
  const showStale = (p, a) => !p.built || p.built.show !== SHOW_VERSION || !!(a && a.version != null && p.built.analysis !== a.version);
  /** A saved show (just opened, S.project) brought up to date if it was built by older logic and not
   *  edited by hand since: built again on the current analysis, keeping its settings (style, color...).
   *  Returns "rebuilt", "edited" (out of date, but changed by hand: left as it is), or "" (up to date). */
  function freshenSaved(data) {
    const p = S.project;
    if (!p || !data.saved || !showStale(p, S.analysis)) return '';
    // a show from before versions: built by the studio if it was rebuilt before (a backup beside it), else by hand
    if (p.handEdited || (!p.built && !data.rebuilt_before)) return 'edited';
    const q = newProject(S.analysis, data.project);
    const { look, ...settings } = p.settings || {};
    q.settings = { ...q.settings, ...settings };
    q.palettes = { ...q.palettes, ...(p.palettes || {}) };
    q.name = p.name || q.name;
    S.project = q; migrateProject(q);
    autoBuild(true);
    return 'rebuilt';
  }
  function changed() { reviewRebuild(true); reviewCustom(); scheduleSave(); draw(); }
  function undo() { if (!S.undo.length) return; S.redo.push(snapshot()); S.project = JSON.parse(S.undo.pop()); S.view.sel = null; S.view.multi = []; inspector(); changed(); }
  function redo() { if (!S.redo.length) return; S.undo.push(snapshot()); S.project = JSON.parse(S.redo.pop()); S.view.sel = null; S.view.multi = []; inspector(); changed(); }
  function scheduleSave() { clearTimeout(S.saveTimer); S.saveTimer = setTimeout(save, 1500); }
  /** copy the UI-held settings into the project and hand it back, ready to send */
  function collectProject() {
    P().name = $('#projectName').value || S.name;
    P().settings.latency = parseFloat($('#latency').value) || 0;
    P().settings.castOffset = parseFloat($('#castOffset').value) || 0;
    return P();
  }
  async function save() {
    if (!P() || !S.name || (S.jam && S.jam.live) || S.review) return false;   // a jam never writes over a saved show; a song under review is not a show
    collectProject();
    try {
      const r = await fetch(`/api/projects/${encodeURIComponent(S.name)}`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(P()) });
      if (!r.ok) throw new Error(`the studio answered ${r.status}`);
      return true;
    } catch (e) { toast('Save failed: ' + e.message, true); return null; }
  }
  /** Save a show rebuilt because it was out of date, keeping the version it replaces. */
  async function saveBackedUp() {
    if (!P() || !S.name || (S.jam && S.jam.live)) return false;
    clearTimeout(S.saveTimer);
    try { return (await fetch(`/api/projects/${encodeURIComponent(S.name)}?backup=1`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(collectProject()) })).ok; }
    catch (e) { return false; }
  }
  /** Save, and say what happened (the button and Ctrl+S). */
  async function saveNow() {
    if (S.jam && S.jam.live) return toast('Jam songs are not saved: a jam never writes over a saved show.', true);
    if (!P() || !S.name) return toast('Open a song first.', true);
    if (await save()) toast(`Saved ${S.name}`);
  }

  // ------------------------------------------------------------------ loading songs
  async function openFile(file) {
    busy(`Analysing ${file.name}…`);
    const fd = new FormData(); fd.append('file', file);
    try { const r = await fetch('/api/song', { method: 'POST', body: fd }); await loadResponse(r); }
    catch (e) { toast('Open failed: ' + e, true); } finally { busy(null); }
  }
  async function openPath(path) {
    busy(`Analysing ${path}…`);
    try { const r = await fetch('/api/open', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ path }) }); await loadResponse(r); }
    catch (e) { toast('Open failed: ' + e, true); } finally { busy(null); }
  }
  async function openProject(name) {
    busy(`Loading ${name}…`);
    try { const r = await fetch(`/api/projects/${encodeURIComponent(name)}`); await loadResponse(r); }
    catch (e) { toast('Load failed: ' + e, true); } finally { busy(null); }
  }
  async function loadResponse(r) {
    const data = await r.json();
    if (data.error) { toast(data.error, true); return; }
    S.reviewData = data.review || null;                                  // (a song in review: its truth, Claude's suggestion)
    if (!data.review && S.review) { await reviewSaveCustom(); await reviewKeep(false); S.review = null; reviewBar(); }   // a saved show opened: review ends
    stopPlayback();
    S.analysis = data.analysis; S.name = data.project;
    S.project = data.saved || newProject(data.analysis, data.project);
    if (!S.project.lanes) S.project = newProject(data.analysis, data.project);
    migrateProject(S.project);
    const fresh = freshenSaved(data);
    $('#projectName').value = S.project.name || S.name;
    if (fresh === 'rebuilt') { clearTimeout(S.saveTimer); await saveBackedUp(); }    // now, before its usual save could
    $('#bpm').value = S.project.bpm.toFixed(2);
    $('#latency').value = S.defaults.lightOffset ?? 30;           // the lights' own delay: the same for every song
    // an offset typed for this project wins; otherwise the connected speaker's own measured delay
    const measured = S.cast.active ? S.defaults.castOffsets[S.cast.device] : null;
    $('#castOffset').value = S.project.settings.castOffsetCustom ? (S.project.settings.castOffset || 0)
      : (measured != null ? measured : S.defaults.castOffset);
    renderLights(); sendSelect();
    $('#emptyState').hidden = true;
    S.undo.length = 0; S.redo.length = 0; S.view.sel = null; S.view.multi = []; S.audio.pos = 0;
    busy('Decoding audio…');
    await loadAudio(data.audio_url);
    zoomFit(); inspector(); renderPalettes(); draw(); refreshRecent();
    toast(`${S.name}: ${S.project.bpm.toFixed(1)} bpm, ${S.project.sections.length} sections, ${S.project.drops.length} drops` + (S.analysis.structure_source === 'loudness+songformer' ? ' · timed by SongFormer' : ''));
    if (fresh === 'rebuilt') toast(`${S.name}: rebuilt with the latest show logic (the old show is kept as a backup)`);
    else if (fresh === 'edited') toast(`${S.name} was built by older show logic. It has your own changes, so it is left as it is: press Auto-build show to rebuild it.`, true);
  }
  // ---- Review: the learned Jam songs with no saved show, one after another, their sections set right by
  // ear (review.py). Moving on keeps any change to the sections in the section truth (what the rules that
  // read songs are tuned against); the check mark says they were right as found. Never saved as a show.
  const secKey = () => JSON.stringify((P() ? P().sections : []).map(s => [s.type, +s.start.toFixed(2), +s.end.toFixed(2)]).concat([P() && P().beats.length ? +P().beats[0].toFixed(3) : 0]));
  /** how far the grid was moved under review (seconds; the median beat's move, when the beats still pair up) */
  const gridShift = () => { const R = S.review, b = P() && P().beats; if (!R || !R.beats || !b || b.length !== R.beats.length) return 0;
    const d = b.map((x, i) => x - R.beats[i]).sort((x, y) => x - y); return d[Math.floor(d.length / 2)] || 0; };
  const reviewBody = ok => JSON.stringify({ id: S.review.id, ok, beat_shift: gridShift(), sections: P().sections.map(s => ({ type: s.type, start: s.start, end: s.end })) });
  /** Under review, the lights follow the sections as they move: rebuilt with the sections exactly as set (a
   *  build takes about 30 ms: a drag rebuilds about 12 times a second, letting go once more); only when the
   *  sections changed. */
  function reviewRebuild(final) {
    const R = S.review; if (!R || !R.id || !P() || R.custom) return;
    const k = secKey(), now = performance.now();
    if (k === R.built || (!final && now - (R.builtAt || 0) < 80)) return;
    R.built = k; R.builtAt = now;
    autoBuild(true, true);
  }
  // The lights as built (to tell a hand edit from a rebuild): every clip, and the layers shown
  const clipSig = () => JSON.stringify((P() ? P().lanes : []).map(l => [!!l.hidden, l.clips.map(c => [c.effect, c.start, c.len, c.params, c.palette, c.opacity, c.blend, c.target, c.fadeIn, c.fadeOut])]));
  /** A song under review whose lights are changed by hand (a clip added, moved, recolored...) is a show of its own:
   *  saved with the song as you go, and the jam plays it. Undone back to the lights as built, it is the jam's own
   *  again (that show set aside, to the Recycle Bin). Moving sections alone only keeps the sections (reviewKeep). */
  function reviewCustom() {
    const R = S.review; if (!R || !R.id || !P()) return;
    const mine = clipSig() !== R.clips;
    if (mine !== !!R.custom) {
      R.custom = mine; draw();
      toast(mine ? `${S.name}: your own show now. It is saved as you go, and the jam plays it.` : `${S.name}: back to the jam's own show`);
    }
    if (mine || R.savedAs) { clearTimeout(R.saveT); R.saveT = setTimeout(reviewSaveCustom, 1200); }
  }
  async function reviewSaveCustom() {
    const R = S.review; if (!R || !R.id) return;
    clearTimeout(R.saveT);
    const post = (url, body) => fetch(url, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) }).then(r => r.json());
    try {
      if (R.custom) {
        collectProject(); P().handEdited = true;
        const d = await post('/api/review/custom', { track_id: R.id, project: P() }); if (d.error) throw new Error(d.error);
        if (!R.savedAs) refreshRecent();
        R.savedAs = d.name;
      } else if (R.savedAs) {
        const d = await post('/api/review/uncustom', { track_id: R.id, name: R.savedAs }); if (d.error) throw new Error(d.error);
        R.savedAs = null; refreshRecent();
      }
    } catch (e) { toast('Your show was not saved: ' + e.message, true); }
  }
  /** A song with Claude's suggestion: its sections as suggested or as the labeler had them (R.showing), the show built on them. */
  function showMapping() {
    const R = S.review; if (!R || !R.proposal) return;
    P().sections = (R.showing === 'claude' ? R.proposal : R.labelerSecs).map(x => ({ ...x }));
    autoBuild(true, true); draw(); R.clips = clipSig();                  // (rebuilt, not hand-edited)
  }
  async function reviewKeep(ok) {
    const R = S.review; if (!R || !R.id || !P()) return;
    if (!ok && (secKey() === R.orig || (R.proposal && secKey() === R.found))) return;   // nothing changed (or only flipped): nothing to keep
    if (ok && secKey() !== R.found) ok = false;                         // changed: a correction, whatever was pressed
    try {
      const r = await fetch('/api/review/truth', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: reviewBody(ok) });
      const d = await r.json(); if (d.error) throw new Error(d.error);
      const song = R.songs.find(x => x.id === R.id); if (song) { song.reviewed = d.entry.reviewed; song.proposal = false; }
      R.proposal = null;
      R.orig = secKey(); R.beats = P().beats.slice();                   // (a grid move is kept once)
      toast(ok ? `${S.name}: the sections are right, kept` : `${S.name}: your sections are kept`);
    } catch (e) { toast('The review was not kept: ' + e.message, true); }
  }
  async function reviewOpen(i) {
    const R = S.review; if (!R || !R.songs.length) return;
    await reviewSaveCustom(); await reviewKeep(false);
    R.i = (i + R.songs.length) % R.songs.length;
    const song = R.songs[R.i];
    if (!R.edit) try { localStorage.setItem('lss-review', song.id); } catch (e) { /* no storage: it starts at the first unreviewed */ }
    busy(`Opening ${song.label}…`);
    try {
      const r = await fetch('/api/review/song/' + encodeURIComponent(song.id));
      R.id = null; await loadResponse(r);
      if (S.analysis) { autoBuild(true); draw(); }                       // (its show, to judge the sections by)
      R.id = song.id; R.orig = R.found = R.built = secKey(); R.beats = P().beats.slice(); R.clips = clipSig(); R.custom = false; R.savedAs = null;   // (the lights as built: a change to them is a hand edit)
      // Claude suggested sections for it: they show first, M flips to the labeler's (R.found), Done keeps what shows;
      // just looking keeps nothing (R.orig is the suggestion, and the labeler's own counts as untouched too)
      const prop = S.reviewData && !S.reviewData.truth && S.reviewData.proposal; R.proposal = null;
      if (prop && prop.length) {
        R.labelerSecs = P().sections.map(x => ({ ...x })); R.proposal = tidySections(prop); R.showing = 'claude'; showMapping();
        R.orig = R.built = secKey();
        toast("Claude's sections: M flips to the old ones, Done keeps what is showing");
      }
    } catch (e) { toast('Open failed: ' + e, true); } finally { busy(null); }
    reviewBar();
  }
  function reviewBar() {
    const R = S.review, on = !!(R && R.on);
    $('#reviewBar').hidden = !on; $('#btnReview').hidden = on;
    if (!on) return;
    $('#revPrev').hidden = $('#revPos').hidden = !!R.edit;              // one song from the song box: nothing to step through
    $('#revDone').title = R.edit ? 'Done: your sections are kept (moved lights make it your own show as you go)' : 'Done, the next song (]): your changes are kept as corrections; with none, it is marked right as it was';
    const song = R.songs[R.i] || {};
    $('#revPos').textContent = `${R.i + 1} / ${R.songs.length}${song.reviewed ? ' ✓' : ''}`;
    $('#revPos').title = (song.label || '') + (song.reviewed ? ` (reviewed: ${song.reviewed})` : '');
  }
  async function reviewStart() {
    busy('Finding learned songs…');
    let songs = [];
    try { songs = (await (await fetch('/api/review/songs')).json()).songs || []; } catch (e) { /* none */ } finally { busy(null); }
    if (!songs.length) { toast('No learned songs to review: a song needs its copy kept (Jam settings) and no saved show.', true); return; }
    let last = null; try { last = localStorage.getItem('lss-review'); } catch (e) { /* none */ }
    // the first song not yet reviewed (the list puts the most doubted first); else where the last review left off
    let i = songs.findIndex(s => !s.reviewed); if (i < 0) i = songs.findIndex(s => s.id === last); if (i < 0) i = 0;
    S.review = { on: true, songs, i, id: null, orig: '' };
    await reviewOpen(i);
  }
  // (the song stays on screen, but no longer under its name: a later tweak saved it as a show with no song beside
  // it, a folder of its own in shows/ that the jam could not play)
  // (made your own show: it stays open as that show, saved like any other from here)
  async function reviewStop() {
    const R = S.review; if (R) await reviewSaveCustom();
    await reviewKeep(false);
    const mine = R && R.custom && R.savedAs;
    S.review = null; S.name = mine || ''; if (mine) { P().name = mine; $('#projectName').value = mine; }
    reviewBar(); draw();
  }
  /** Done: the song as it stands is the truth (a correction if anything was changed), and on to the next. */
  async function reviewDone() {
    const R = S.review; if (!R) return;
    if (R.edit) { await reviewStop(); return; }                         // one song opened from the song box: changes kept, done
    await reviewKeep(true); await reviewOpen(R.i + 1);
  }
  // the window closing with changes not yet kept: sent on the way out
  addEventListener('pagehide', () => {
    if (S.review && S.review.id && P() && secKey() !== S.review.orig)
      try { navigator.sendBeacon('/api/review/truth', new Blob([reviewBody(false)], { type: 'application/json' })); } catch (e) { /* closing anyway */ }
  });
  /** The song box: your saved shows and every song learned in a jam (its show as the jam plays it), by name. */
  async function refreshRecent() {
    const esc = x => String(x).replace(/&/g, '&amp;').replace(/"/g, '&quot;').replace(/</g, '&lt;');
    try {
      const [list, every] = await Promise.all([fetch('/api/projects').then(r => r.json()),
        fetch('/api/review/every').then(r => r.json()).then(d => d.songs || []).catch(() => [])]);
      const to = new Map();
      for (const p of list) to.set(p.name, { show: p.name, note: p.has_project ? 'your show' : 'song only' });
      for (const s of every) if (!s.show && !to.has(s.label)) to.set(s.label, { jam: s, note: 'jam song' });
      S.songBox = to;
      $('#songList').innerHTML = [...to].map(([v, t]) => `<option value="${esc(v)}" label="${t.note}"></option>`).join('');
    } catch (e) { /* server away */ }
  }
  /** Open what the song box names: a saved show, or a jam song in review (one song, no stepping on). */
  async function openNamed(v) {
    const t = S.songBox && S.songBox.get(v); if (!t) return false;
    $('#recent').value = ''; $('#recent').blur();
    if (t.show) { await openProject(t.show); return true; }
    if (S.review) await reviewStop();
    S.review = { on: true, edit: true, songs: [{ ...t.jam, reviewed: '' }], i: 0, id: null, orig: '' };
    await reviewOpen(0);
    return true;
  }

  // ------------------------------------------------------------------ audio
  async function loadAudio(url) {
    if (!S.audio.ctx) S.audio.ctx = new (window.AudioContext || window.webkitAudioContext)();
    const ab = await (await fetch(url)).arrayBuffer();
    S.audio.buffer = await S.audio.ctx.decodeAudioData(ab);
  }
  function pos() {
    if (S.cue && S.cue.live) return clamp(S.cue.pos(), 0, duration());     // a Party Hub cue is playing one of your shows
    if (S.jam && S.jam.live) return clamp(S.jam.pos(), 0, duration());     // Spotify is driving: the room's place in the track
    if (S.cast.active) { const c = S.cast; const off = (parseFloat($('#castOffset').value) || 0) / 1000; const p = c.state === 'PLAYING' ? c.pos + (performance.now() - c.at) / 1000 : c.pos; return clamp(p - off, 0, duration()); }
    const a = S.audio; return a.playing ? clamp(a.startPos + (a.ctx.currentTime - a.startCtx), 0, duration()) : a.pos;
  }
  function play(from = pos()) {
    const a = S.audio; if (!a.buffer) return;
    if (S.cast.active) { castPlay(from); return; }
    if (a.ctx.state === 'suspended') a.ctx.resume();
    stopSource();
    if (from >= duration() - 0.05) from = 0;
    a.source = a.ctx.createBufferSource(); a.source.buffer = a.buffer; a.source.connect(a.ctx.destination);
    a.source.start(0, from); a.startCtx = a.ctx.currentTime; a.startPos = from; a.playing = true;
    const src = a.source; src.onended = () => { if (a.source === src && a.playing) { a.pos = duration(); a.playing = false; updateTransport(); } };
    if ($('#outputOn').checked) startOutput();
    updateTransport();
  }
  function pause() { if (S.cast.active) { castPause(); return; } const a = S.audio; if (!a.playing) return; a.pos = pos(); a.playing = false; stopSource(); updateTransport(); }
  function stopSource() { const a = S.audio; if (a.source) { try { a.source.onended = null; a.source.stop(); } catch (e) { } a.source = null; } }
  function stopPlayback() { pause(); S.audio.pos = 0; }
  function seek(t) { if ((S.jam && S.jam.live) || (S.cue && S.cue.live)) return; t = clamp(t, 0, duration()); if (S.cast.active) { castSeek(t); return; } const a = S.audio; if (a.playing) play(t); else { a.pos = t; pushFrameIfIdle(); } }
  function togglePlay() { if (S.cue && S.cue.live) return toast('A cue is playing: tap it on the Party Hub to end it'); if (S.jam && S.jam.live) return toast('Spotify is in charge during a jam: play and pause from the phone'); S.audio.playing ? pause() : play(); }
  function updateTransport() { $('#btnPlay').textContent = S.audio.playing ? '❚❚' : '▶'; }

  // ------------------------------------------------------------------ Google Cast
  async function castApi(path, body) {
    const isGet = path.startsWith('status') || path.startsWith('devices');
    const r = await fetch('/api/cast/' + path, isGet ? {} : { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body || {}) });
    const j = await r.json(); if (j.error) { toast('Cast: ' + j.error, true); throw new Error(j.error); } return j;
  }
  function castGone() {                                   // the speaker went away without Stop cast being pressed
    clearInterval(S.cast.poll); S.cast.poll = null; clearTimeout(S.cast.syncT);
    S.cast.active = false; S.cast.state = 'IDLE'; stopSource(); S.audio.localCast = false;
    $('#btnCast').textContent = 'Cast'; $('#btnCast').classList.remove('on'); $('#castOffsetWrap').hidden = true; $('#castVolume').hidden = true; $('#castLocalWrap').hidden = true; $('#btnSync').hidden = true;
  }
  function applyCastStatus(st) {
    const c = S.cast, jam = S.jam && S.jam.live;
    if (c.active && st.connected === false) {
      castGone();
      if (jam) toast('The Cast speaker stopped playing, so the jam is on this PC now. Press Cast to try it again.', true);
      else { S.audio.playing = false; updateTransport(); toast('The Cast speaker disconnected.', true); }
      return;
    }
    c.state = st.state; c.at = performance.now();
    if (['PLAYING', 'PAUSED', 'BUFFERING'].includes(st.state)) c.pos = st.position;   // only a loaded session has a real position; otherwise keep the playhead
    if (st.volume != null) $('#castVolume').value = Math.round(st.volume * 100);
    // In a jam the jam owns the clock and play/pause: a speaker's own state must not fight it
    // (a stopped Cast flipping "playing" off twice a second held the lights at a paused trickle).
    if (jam) return;
    const playing = st.state === 'PLAYING' || st.state === 'BUFFERING';
    syncLocalToCast();
    if (S.audio.playing !== playing) { S.audio.playing = playing; updateTransport(); }
    if (st.state === 'IDLE' && c.wasPlaying && st.position >= duration() - 2) { c.wasPlaying = false; }
    c.wasPlaying = playing;
  }
  async function loadCastDevices(rescan = false) {
    const sel = $('#castDevice'); const cur = sel.value;
    try { const list = await castApi('devices' + (rescan ? '?rescan=1' : '')); S.cast.devices = list;
      sel.innerHTML = '<option value="">Cast: this PC</option>' + list.map(d => `<option value="${d.name}">${d.type === 'group' ? '🔊 ' : ''}${d.name}${d.type === 'group' ? ' (group)' : ''}</option>`).join('');
      sel.value = cur; } catch (e) { /* no cast library or no devices */ }
  }
  async function castStart() {
    const name = $('#castDevice').value; if (!name) return toast('Pick a Cast speaker or group first', true);
    if (S.audio.playing && !S.cast.active) pause();
    const startPos = pos();
    busy(`Connecting to ${name}…`);
    if (!(P() && P().settings.castOffsetCustom) && S.defaults.castOffsets[name] != null) $('#castOffset').value = S.defaults.castOffsets[name];
    try { const st = await castApi('connect', { name }); S.cast.active = true; S.cast.device = name; S.cast.verified = false; S.cast.pos = startPos; S.cast.at = performance.now(); S.cast.state = 'IDLE'; applyCastStatus(st); $('#btnCast').textContent = 'Stop cast'; $('#btnCast').classList.add('on'); $('#castOffsetWrap').hidden = false; $('#castVolume').hidden = false; $('#castLocalWrap').hidden = false; $('#btnSync').hidden = false;
      clearInterval(S.cast.poll); S.cast.poll = setInterval(async () => { try { applyCastStatus(await castApi('status')); } catch (e) { } }, 500);
      toast(`Casting to ${name}. Press play.`); }
    finally { busy(null); }
  }
  async function castEnd() {
    clearInterval(S.cast.poll); S.cast.poll = null; clearTimeout(S.cast.syncT); const wasPos = pos();
    try { await castApi('disconnect'); } catch (e) { }
    stopSource(); S.audio.localCast = false;
    S.cast.active = false; S.cast.state = 'IDLE';
    if (!(S.jam && S.jam.live)) { S.audio.playing = false; S.audio.pos = wasPos; updateTransport(); }   // a jam carries on, on this PC
    $('#btnCast').textContent = 'Cast'; $('#btnCast').classList.remove('on'); $('#castOffsetWrap').hidden = true; $('#castVolume').hidden = true; $('#castLocalWrap').hidden = true; $('#btnSync').hidden = true;
  }
  async function castPlay(from) {
    if (S.cast.state === 'PAUSED' && Math.abs(from - S.cast.pos) < 0.05) { applyCastStatus(await castApi('resume')); return; }
    S.cast.pos = from; S.cast.at = performance.now(); S.cast.state = 'BUFFERING'; S.audio.playing = true; updateTransport();
    if ($('#outputOn').checked) startOutput();
    try { applyCastStatus(await castApi('play', { project: S.name, start: from })); } catch (e) { S.audio.playing = false; updateTransport(); return; }
    // calibrate once per cast session, as soon as there is something to listen to
    if (!S.cast.verified && !S.cast.syncing && !(P() && P().settings.castOffsetCustom)) {
      clearTimeout(S.cast.syncT);
      S.cast.syncT = setTimeout(() => runSyncCheck(true), 3500);
    }
  }
  function syncLocalToCast() {
    const a = S.audio; if (!S.cast.active || !a.buffer) return;
    const want = $('#castLocal').checked && S.cast.state === 'PLAYING';
    if (!want) { if (a.localCast) { stopSource(); a.localCast = false; } return; }
    if (a.ctx.state === 'suspended') a.ctx.resume();
    const target = pos();
    const cur = (a.localCast && a.source) ? a.startPos + (a.ctx.currentTime - a.startCtx) : null;
    if (cur === null || Math.abs(cur - target) > 0.15) {   // (re)start the PC audio at the speakers' position
      stopSource(); const src = a.ctx.createBufferSource(); src.buffer = a.buffer; src.connect(a.ctx.destination);
      src.start(0, Math.max(0, target)); a.source = src; a.startCtx = a.ctx.currentTime; a.startPos = target; a.localCast = true;
    }
  }
  /** Measure how far behind the speakers are, and keep the answer.
   *
   *  The studio hands the speaker a stream that already begins at the right
   *  second, so the speaker never seeks and never guesses. What is left to
   *  measure is that speaker's own output delay, which stays put for the whole
   *  session and for every song, so one listen is enough.
   */
  async function runSyncCheck(auto = false) {
    // during a jam there is no song file to compare against: the jam checks against what it sent
    const js = S.jam && S.jam.state;
    if (S.jam && S.jam.live && js && js.output_kind === 'cast') {
      if (auto) return;                                   // the jam runs its own check when a speaker starts
      const btn = $('#btnSync'); btn.disabled = true; btn.textContent = 'Listening…';
      toast('Listening to the room to measure the speaker\u2019s delay\u2026');
      try {
        const r = await (await fetch('/api/jam/synccheck', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: '{}' })).json();
        if (r.error) toast(r.error, true);
        else if (r.saved_for) toast(`${r.saved_for} is ${r.speaker_offset_ms} ms behind (corrected by ${r.offset_ms > 0 ? '+' : ''}${r.offset_ms} ms).`);
        else toast(r.reason || 'Could not hear the speakers clearly enough. Turn them up or move closer to the mic and try again.', true);
      } catch (e) { toast('Sync check failed: ' + e.message, true); }
      finally { btn.disabled = false; btn.textContent = 'Sync check'; }
      return;
    }
    if (!S.cast.active || S.cast.state !== 'PLAYING') {
      if (!auto) toast('Start the song on the speakers first', true);
      return;
    }
    if (S.cast.syncing) return;
    S.cast.syncing = true;
    const device = S.cast.device;
    const known = S.defaults.castOffsets[device] != null;
    // the PC's own speakers would be heard too and confuse the measurement
    const pcWas = $('#castLocal').checked;
    if (pcWas) { $('#castLocal').checked = false; syncLocalToCast(); }
    const btn = $('#btnSync'); btn.disabled = true; btn.textContent = 'Listening…';
    // deliberately not a blocking overlay: this runs while the show is playing
    toast(auto && known ? `Checking ${device}’s delay…`
                        : `Listening to the room to measure ${device}’s delay…`);
    try {
      const r = await castApi('synccheck', { project: S.name, seconds: 7, repeats: known && auto ? 1 : 2 });
      const weak = r.confidence < 0.25;
      if (auto && (weak || S.cast.state !== 'PLAYING')) {
        toast('Could not hear the song well enough to calibrate. Press Sync check to retry.', true);
      } else {
        S.defaults.castOffsets[device] = r.offset_ms;
        if (!(P() && P().settings.castOffsetCustom)) $('#castOffset').value = r.offset_ms;
        S.cast.verified = true;
        syncLocalToCast();
        toast(weak
          ? `Measured ${r.offset_ms} ms, but the match was weak (${Math.round(r.confidence * 100)}%). Turn the speakers up or move the mic closer and try again.`
          : `${device} is ${r.offset_ms} ms behind (confidence ${Math.round(r.confidence * 100)}%). Locked in for this session.`,
          weak);
      }
    } catch (e) { /* castApi already reported it */ }
    finally {
      S.cast.syncing = false;
      btn.disabled = false; btn.textContent = 'Sync check';
      if (pcWas) { $('#castLocal').checked = true; syncLocalToCast(); }
    }
  }

  async function castPause() { try { applyCastStatus(await castApi('pause')); } catch (e) { } S.audio.playing = false; updateTransport(); }
  async function castSeek(t) { S.cast.pos = t; S.cast.at = performance.now(); try { applyCastStatus(await castApi('seek', { t })); } catch (e) { } pushFrameIfIdle(); }

  // ------------------------------------------------------------------ lights / websocket
  async function loadLights(rescan = false, quiet = false) {
    try {
      const list = await (await fetch('/api/devices' + (rescan ? '?rescan=1' : ''))).json();
      const prev = Object.fromEntries(S.lights.map(l => [l.ip, l]));
      const next = list.map(d => ({ ...d, on: prev[d.ip] !== undefined ? prev[d.ip].on : !!d.on }));
      // a background scan changes nothing on screen unless a light came, went or changed, and never
      // while you are typing a name or a segment count there
      const sig = ls => JSON.stringify(ls.map(l => [l.ip, l.online, l.name, l.segments, l.flip, l.on]));
      if (quiet && (sig(next) === sig(S.lights) || $('#lightList').contains(document.activeElement))) return;
      S.lights = next;
      renderLights(); sendSelect(); autoLights();
    } catch (e) { if (!quiet) toast('Could not list lights', true); }
  }
  // Lights are looked for by themselves: every half minute while nothing plays (a scan is a burst
  // of multicast on the lights' own Wi-Fi), and when the window comes back to the front.
  const SCAN_EVERY_MS = 30000;
  S.lastScan = performance.now();
  function autoScan() {
    if (document.hidden || S.audio.playing || (S.jam && S.jam.live) || S.scanning) return;
    if (performance.now() - S.lastScan < SCAN_EVERY_MS - 500) return;
    S.scanning = true; S.lastScan = performance.now();
    loadLights(true, true).finally(() => { S.scanning = false; });
  }
  setInterval(autoScan, 5000);
  window.addEventListener('focus', () => { if (performance.now() - S.lastScan > 10000) { S.lastScan = 0; autoScan(); } });
  // Lights come on by themselves when the app opens, unless they were last switched off.
  // (?lights=off in the address keeps them off without changing that, for testing.)
  const LIGHTS_KEY = 'lightShowStudio.lightsOn';
  const lightsForcedOff = new URLSearchParams(location.search).get('lights') === 'off';
  function lightsWanted() { if (lightsForcedOff) return false; try { return localStorage.getItem(LIGHTS_KEY) !== '0'; } catch (e) { return true; } }
  function rememberLights(on) { if (lightsForcedOff) return; try { localStorage.setItem(LIGHTS_KEY, on ? '1' : '0'); } catch (e) { /* not kept: fine */ } }
  function autoLights() {
    if (S.autoLightsDone || !S.wsOpen || !S.lights.length) return;
    S.autoLightsDone = true;
    if (!lightsWanted() || !selectedLights().length) return;
    // armed, not started: the lights keep what they are showing until a song plays
    $('#outputOn').checked = true; S.lightsArmed = true;
  }
  function renderLights() {
    const el = $('#lightList'); el.innerHTML = '';
    if (!S.lights.length) { el.innerHTML = '<div class="hint">No lights found yet. Turn on LAN Control in the Govee app; the studio keeps looking.</div>'; return; }
    for (const l of S.lights) {
      const row = document.createElement('label');
      const rm = roomOf(l);
      if (rm && !rm.on) row.classList.add('room-off');
      row.innerHTML = `<span class="grip" title="Drag to put the lights in the order they stand around the room">&#8942;&#8942;</span><input type="checkbox" ${l.on ? 'checked' : ''}> <span class="light-id"><input class="light-name" type="text" maxlength="40" spellcheck="false" placeholder="${l.sku}" title="Click to name this light"><div class="sku">${rm ? esc(rm.name) + (rm.on ? '' : ' (off)') + ' · ' : ''}${l.name ? l.sku + ' · ' : ''}${l.ip}${l.online === false ? ' · not answering' : ''}</div></span> <button type="button" class="flip icon small ${l.flip ? 'on' : ''}" title="${l.flip ? 'Flipped: shows run the other way on this light. Click to undo' : 'Flip: run shows the other way on this light (for one mounted right to left)'}">&#8644;</button><input class="seg" type="number" min="1" max="255" value="${l.segments}" title="Segments (LED zones)">`;
      const nm = row.querySelector('.light-name'); nm.value = l.name || '';
      nm.onkeydown = e => { if (e.key === 'Enter' || e.key === 'Escape') { if (e.key === 'Escape') nm.value = l.name || ''; nm.blur(); } };
      nm.onchange = async () => { l.name = nm.value.trim(); await fetch('/api/devices/name', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ mac: l.mac, device_id: l.device_id, name: l.name }) }); renderLights(); if (S.view.sel) inspector(); draw(); };
      if (l.online === false) row.style.opacity = '0.6';
      row.querySelector('input[type=checkbox]').onchange = e => { l.on = e.target.checked; sendSelect(); saveSelection(); draw(); };
      const grip = row.querySelector('.grip');
      grip.onmousedown = () => { row.draggable = true; };
      grip.onclick = e => e.preventDefault();
      row.ondragstart = e => { S.dragLight = l.ip; e.dataTransfer.effectAllowed = 'move'; e.dataTransfer.setData('text/plain', 'light:' + l.ip); row.classList.add('dragging'); };
      row.ondragend = () => { row.draggable = false; S.dragLight = null; row.classList.remove('dragging'); };
      row.ondragover = e => { if (!S.dragLight || S.dragLight === l.ip) return; e.preventDefault(); const r = row.getBoundingClientRect(); row.classList.toggle('drop-above', e.clientY < r.top + r.height / 2); row.classList.toggle('drop-below', e.clientY >= r.top + r.height / 2); };
      row.ondragleave = () => row.classList.remove('drop-above', 'drop-below');
      row.ondrop = e => {
        if (!S.dragLight) return; e.preventDefault(); e.stopPropagation();
        const below = row.classList.contains('drop-below'); row.classList.remove('drop-above', 'drop-below');
        const moving = S.lights.find(x => x.ip === S.dragLight); if (!moving || moving === l) return;
        S.lights.splice(S.lights.indexOf(moving), 1); S.lights.splice(S.lights.indexOf(l) + (below ? 1 : 0), 0, moving);
        fetch('/api/devices/order', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ keys: S.lights.map(x => x.mac || x.ip) }) }).catch(() => { });
        renderLights(); sendSelect(); draw(); S.forceSend = true;
      };
      row.querySelector('.flip').onclick = e => { e.preventDefault(); l.flip = !l.flip; renderLights(); S.forceSend = true;
        fetch('/api/devices/flip', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ mac: l.mac, device_id: l.device_id, ip: l.ip, flip: l.flip }) }).catch(() => { });
        toast(`${lightLabel(l)}: ${l.flip ? 'flipped' : 'normal direction'}`); };
      row.querySelector('.seg').onchange = async e => { l.segments = parseInt(e.target.value) || 1; await fetch('/api/devices/segments', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ key: l.mac || l.ip, segments: l.segments }) }); };
      el.appendChild(row);
    }
  }
  const selectedLights = () => S.lights.filter(l => l.on);
  /** Every light the show is made for: the ticked ones and every light in a room that is on, ticked or
   *  not. Testing on the Office strip alone, the show is still built for the poles and strings of the
   *  party rooms (their parts only play on them), so the room view shows it as the party would see it. */
  const showLights = () => S.lights.filter(l => l.on || (roomOf(l) && !roomOff(l)));
  /** The party rooms' lights (rooms with two or more lights, switched on), ticked or not: what the room
   *  view shows. A room with one light (the Office, a test room) is not part of the party. */
  function partyLights() {
    const inRooms = S.lights.filter(l => roomOf(l) && !roomOff(l)), per = new Map();
    for (const l of inRooms) per.set(roomOf(l), (per.get(roomOf(l)) || 0) + (lightKind(l) === 'tubes' ? 2 : 1));   // (the kitchen's tubes are a party room alone)
    return inRooms.filter(l => per.get(roomOf(l)) >= 2);
  }

  // ------------------------------------------------------------------ rooms
  // Which lights and speakers stand in which room, and which rooms the party is in. A room switched
  // off (here or on the Party Hub) leaves the show: the studio fades its lights back to how they
  // were and mutes its speakers. The page only needs to know so the room order skips it.
  S.rooms = S.rooms || [];
  function keysOf(l) { return [l.mac, l.device_id, l.ip].filter(Boolean); }
  function roomOf(l) { return (S.rooms || []).find(r => r.lights.some(k => keysOf(l).includes(k))) || null; }
  /** Where each light stands, for the effects that play the room as a whole (sweeps, the poles' call and
   *  response, halves): { of: Map(light -> { i, n, side, kinds }) }. A light alone in its room (the Office,
   *  a test room no one else can see) plays them by itself. The others are the party space, rooms next to
   *  each other, ordered as a line across them: the first room's lamps (a light pole in its corner), its
   *  long strips (overhead), then the next room's strips and lamps, so a sweep crosses from one corner to
   *  the other and the first room is one side, the last the other. Rooms switched off are left out (a
   *  chase never runs into one that is off). Without rooms set up, every light is one space, in list order. */
  function roomSpace(lights) {
    const on = lights.filter(l => !roomOff(l)), of = new Map(), rooms = S.rooms || [];
    const roomIx = l => rooms.indexOf(roomOf(l));
    const count = new Map();
    for (const l of on) count.set(roomIx(l), (count.get(roomIx(l)) || 0) + 1);
    const alone = l => rooms.length && roomIx(l) >= 0 && count.get(roomIx(l)) === 1;
    for (const l of on) if (alone(l)) of.set(l, { i: 0, n: 1, side: 0, kinds: [lightKind(l)] });
    const party = on.filter(l => !alone(l));
    const ids = [...new Set(party.map(roomIx))].sort((a, b) => a - b);
    const line = [];
    ids.forEach((r, k) => {
      const here = party.filter(l => roomIx(l) === r), lamps = here.filter(l => lightKind(l) === 'short'), strips = here.filter(l => lightKind(l) !== 'short');
      line.push(...(k === 0 && ids.length > 1 ? [...lamps, ...strips] : [...strips, ...lamps]));   // the corners at the ends
    });
    const kinds = line.map(lightKind);
    line.forEach((l, i) => {
      const k = ids.indexOf(roomIx(l));
      of.set(l, { i, n: line.length, kinds, side: ids.length > 1 ? (k < ids.length / 2 ? 0 : 1) : (i < Math.ceil(line.length / 2) ? 0 : 1) });
    });
    return { of, key: roomSpaceKey(lights) };
  }
  const roomSpaceKey = lights => JSON.stringify([lights.map(l => l.ip + (roomOff(l) ? '-' : '+')), (S.rooms || []).map(r => [r.id, r.lights])]);
  function roomOff(l) { const r = roomOf(l); return !!(r && !r.on); }
  async function loadRooms() {
    try {
      const d = await (await fetch('/api/rooms')).json();
      const was = JSON.stringify(S.rooms); S.rooms = d.rooms || [];
      if (JSON.stringify(S.rooms) !== was) { if (!$('#lightList').contains(document.activeElement)) renderLights(); S.forceSend = true; }
    } catch (e) { /* the next look will do */ }
  }
  setInterval(() => { if (!document.hidden) loadRooms(); }, 3000);
  loadRooms();

  let roomsDoc = null;
  async function saveRooms(rooms) {
    try {
      const r = await fetch('/api/rooms?devices=1', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ rooms }) });
      roomsDoc = await r.json(); S.rooms = roomsDoc.rooms; renderRooms(); renderLights(); S.forceSend = true;
    } catch (e) { toast('Could not save the rooms', true); }
  }
  async function openRooms() {
    const pop = $('#roomsPop');
    closeMenus(); pop.hidden = false;
    const r = $('#btnSettings').getBoundingClientRect(); pop.style.top = (r.bottom + 6) + 'px'; pop.style.right = Math.max(8, innerWidth - r.right) + 'px';
    if (!roomsDoc) pop.innerHTML = '<div class="hint">Looking for lights and speakers…</div>';
    try { roomsDoc = await (await fetch('/api/rooms?devices=1')).json(); S.rooms = roomsDoc.rooms; } catch (e) { pop.innerHTML = '<div class="hint">Could not load the rooms.</div>'; return; }
    renderRooms();
  }
  function renderRooms() {
    const pop = $('#roomsPop'); if (pop.hidden || !roomsDoc) return;
    if (pop.contains(document.activeElement) && document.activeElement.classList.contains('room-name')) return;   // never under the cursor
    const d = roomsDoc, rooms = d.rooms.map(r => ({ ...r, lights: r.lights.slice(), speakers: r.speakers.slice() }));
    const lightName = l => l.name || `${l.sku} ${l.ip}`;
    pop.innerHTML = `<div class="rooms-head"><div class="menu-title">Rooms</div><button class="small" data-act="add">+ Add room</button></div>` +
      (rooms.length ? '' : '<div class="hint">No rooms yet: every light and speaker plays.</div>') +
      rooms.map((r, i) => `<div class="room ${r.on ? '' : 'off'}" data-i="${i}">
        <div class="room-top"><input class="room-name" value="${esc(r.name)}" maxlength="40" spellcheck="false" title="Room name">
          <label class="toggle" title="${r.on ? 'In the party. Off: its lights go back to how they were and its speakers mute' : 'Out of the party. On: its lights fade back in and its speakers unmute'}"><input type="checkbox" data-act="on" ${r.on ? 'checked' : ''}></label>
          <button class="icon small" data-act="del" title="Delete this room">&times;</button></div>
        <div class="chips"><span class="kind">Lights</span>${(d.lights || []).map(l => `<button class="chip ${r.lights.includes(l.key) ? 'on' : ''}" data-light="${esc(l.key)}">${esc(lightName(l))}</button>`).join('') || '<span class="hint">none found</span>'}</div>
        <div class="chips"><span class="kind">Speakers</span>${(d.speakers || []).map(sp => `<button class="chip ${r.speakers.includes(sp) ? 'on' : ''}" data-speaker="${esc(sp)}">${esc(sp)}</button>`).join('') || '<span class="hint">none found</span>'}</div>
      </div>`).join('');
    pop.querySelector('[data-act=add]').onclick = () => saveRooms([...rooms, { name: `Room ${rooms.length + 1}`, on: true, lights: [], speakers: [] }]);
    pop.querySelectorAll('.room').forEach(el => {
      const i = +el.dataset.i, r = rooms[i];
      el.querySelector('.room-name').onchange = e => { r.name = e.target.value.trim() || r.name; e.target.blur(); saveRooms(rooms); };
      el.querySelector('.room-name').onkeydown = e => { if (e.key === 'Enter') e.target.blur(); };
      el.querySelector('[data-act=on]').onchange = e => { r.on = e.target.checked; saveRooms(rooms); };
      el.querySelector('[data-act=del]').onclick = () => { if (confirm(`Delete ${r.name}?`)) { rooms.splice(i, 1); saveRooms(rooms); } };
      // a light or a speaker stands in one room: putting it here takes it out of any other
      el.querySelectorAll('[data-light]').forEach(b => b.onclick = () => { const k = b.dataset.light, has = r.lights.includes(k);
        rooms.forEach(o => { o.lights = o.lights.filter(x => x !== k); }); if (!has) r.lights.push(k); saveRooms(rooms); });
      el.querySelectorAll('[data-speaker]').forEach(b => b.onclick = () => { const k = b.dataset.speaker, has = r.speakers.includes(k);
        rooms.forEach(o => { o.speakers = o.speakers.filter(x => x !== k); }); if (!has) r.speakers.push(k); saveRooms(rooms); });
    });
  }
  /** Short lamps (a dozen segments or fewer) and long strips play different parts of a show; the meteor
   *  tubes (H6821: 20 hanging tubes, every LED its own color) a part of their own. */
  const TUBE_SKUS = new Set(['H6821']), TUBE_COUNT = 20;
  const lightKind = l => TUBE_SKUS.has(l.sku) ? 'tubes' : (l.segments || 10) <= 12 ? 'short' : 'long';
  /** the ticked lights belong to the room, not the song: saved with the studio's settings */
  function saveSelection() {
    fetch('/api/devices/selected', { method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ lights: selectedLights().map(l => ({ mac: l.mac, device_id: l.device_id })) }) }).catch(() => { });
  }
  const esc = t => String(t ?? '').replace(/[&<>"]/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]));
  /** what to call a light: the name you gave it, or its model and address */
  const lightLabel = l => l.name || `${l.sku} ${l.ip}`;
  function connectWs() {
    const ws = new WebSocket(`${location.protocol === 'https:' ? 'wss' : 'ws'}://${location.host}/ws`);
    ws.onopen = () => { S.wsOpen = true; sendSelect(); autoLights(); if ($('#outputOn').checked) pushFrameIfIdle(); };
    ws.onclose = () => { S.wsOpen = false; S.outputActive = false; setTimeout(connectWs, 2000); };
    ws.onmessage = e => { const m = JSON.parse(e.data); if (m.type === 'started') S.outputActive = true; if (m.type === 'stopped') S.outputActive = false; };
    S.ws = ws;
  }
  const wsSend = o => { if (S.wsOpen) S.ws.send(JSON.stringify(o)); };
  function sendSelect() { wsSend({ type: 'select', ips: selectedLights().map(l => l.ip) }); }
  /** Every light the show drives back to 100% brightness: the jam sends it between songs. */
  const fullBrightness = () => wsSend({ type: 'full_brightness' });
  function startOutput() { S.lightsArmed = false; if (!S.outputActive && selectedLights().length) wsSend({ type: 'start' }); }
  function stopOutput() { if (S.outputActive) wsSend({ type: 'stop' }); }
  // Paused or idle: only a show that has already taken the lights over refreshes them. When the app
  // opens armed, nothing touches the lights until something plays (startOutput does that).
  function pushFrameIfIdle() { if ($('#outputOn').checked && !S.lightsArmed) { if (!S.outputActive) startOutput(); S.forceSend = true; } }

  // ------------------------------------------------------------------ engine
  const bufs = new Map();
  function buf(key, n) { let b = bufs.get(key); if (!b || b.length !== n * 4) { b = new Float32Array(n * 4); bufs.set(key, b); } return b; }
  function featureAt(t) {
    const a = S.analysis; const i = clamp(Math.floor(t * a.feature_fps), 0, a.energy.length - 1);
    return { energy: a.energy[i], bands: a.bands[i] };
  }
  /** index of the last entry at or before t, or -1 (entries are [time, ...]) */
  function lastBefore(arr, t) {
    let lo = 0, hi = arr.length - 1, best = -1;
    while (lo <= hi) { const m = (lo + hi) >> 1; if (arr[m][0] <= t) { best = m; lo = m + 1; } else hi = m - 1; }
    return best;
  }
  const EMPTY_HIT = { age: 99, strength: 0, pitch: 0, index: -1 };
  function hitAt(stream, t) {
    const arr = (S.analysis.events || {})[stream] || [];
    const i = lastBefore(arr, t);
    return i < 0 ? EMPTY_HIT : { age: t - arr[i][0], strength: arr[i][1], pitch: arr[i][2], index: i };
  }
  /** everything the song is doing at time t, for effects that follow the music */
  function musicAt(t) {
    const a = S.analysis;
    const i = clamp(Math.floor(t * a.feature_fps), 0, a.energy.length - 1);
    const cur = a.curves || {};
    const hookTimes = (a.hook && a.hook.times) || [];
    let hook = { age: 99, index: -1, progress: 1 };
    if (hookTimes.length) {
      const k = lastBefore(hookTimes.map(x => [x]), t);
      if (k >= 0) hook = { age: t - hookTimes[k], index: k, progress: clamp((t - hookTimes[k]) / (a.hook.length || 2), 0, 1) };
    }
    let fill = { active: false, progress: 0 };
    for (const [f0, f1] of a.fills || []) if (t >= f0 && t < f1) { fill = { active: true, progress: (t - f0) / Math.max(0.1, f1 - f0) }; break; }
    return {
      lead: hitAt('lead', t), ghost: hitAt('ghost', t), bass: hitAt('bass', t),
      kick: hitAt('kick', t), snare: hitAt('snare', t), roll: hitAt('roll', t), basshit: hitAt('basshit', t), hasDrums: (((a.events || {}).kick) || []).length > 0,
      hitsBetween: (stream, t0, t1) => {                          // the times of one instrument's hits in [t0, t1)
        const arr = (a.events || {})[stream] || []; const out = [];
        for (let i = Math.max(0, lastBefore(arr, t0)); i < arr.length && arr[i][0] < t1; i++) if (arr[i][0] >= t0) out.push(arr[i][0]);
        return out;
      },
      hook, fill,
      leadLevel: (cur.lead || [])[i] || 0, bassLevel: (cur.bass || [])[i] || 0, drumLevel: (cur.drum || [])[i] || 0,
      vocalLevel: (cur.vocals || [])[i] ?? 0.5,
      b808: (() => { const h = hitAt('b808', t); if (h.index >= 0) h.hold = ((a.events.b808 || [])[h.index] || [])[3] || 0.8; return h; })(),
      hatroll: (() => { for (const [r0, r1, rate] of a.hatrolls || []) if (t >= r0 && t < r1) return { active: true, rate, progress: (t - r0) / Math.max(0.05, r1 - r0) }; return null; })(),
      wobble: (() => { for (const [w0, w1, per] of a.wobbles || []) if (t >= w0 && t < w1) return { per }; return null; })(),
    };
  }
  /** How bright the whole show may be at time t, from how loud the music is there.
   *
   *  Two parts. A dip: when the sound falls well below what is usual for the section it is in
   *  (a gap in a drop, a sudden hush), every light falls with it, down to 4%, whatever the effects
   *  are doing. And the song's overall level, gently: a quiet section is a little dimmer than a
   *  loud one, never below 30%, so a calm breakdown stays calm rather than dark. Loudness is held
   *  over a quarter second either side, so the space between two kicks never dims. */
  const DIM = new WeakMap();
  function loudnessGain(t) {
    const a = S.analysis, p = P();
    if (!a || !a.energy || !a.energy.length || !p || (p.settings && p.settings.followLoudness === false)) return 1;
    const fps = a.feature_fps || 30, sig = p.sections.map(x => x.start.toFixed(2)).join(',');
    let d = DIM.get(a);
    if (!d || d.sig !== sig) {
      const e = a.energy, n = e.length, w = Math.max(1, Math.round(0.25 * fps));
      const held = new Float32Array(n);
      for (let i = 0; i < n; i++) { let m = 0; for (let j = Math.max(0, i - w); j <= Math.min(n - 1, i + w); j++) m = Math.max(m, e[j]); held[i] = m; }
      const pct = (arr, q) => { const v = arr.filter(x => x > 0).sort((x, y) => x - y); return v.length ? v[Math.floor(v.length * q)] : 0; };
      const ref = pct(Array.from(held), 0.9) || 1;          // a first listen is zero where not yet heard
      const g = new Float32Array(n).fill(1);
      // a first listen knows the music only up to what it has heard: past that, full brightness, not dark
      const heard = a.heard != null ? Math.min(n, Math.floor(a.heard * fps)) : n;
      const spans = p.sections.length ? p.sections.map(x => [x.start, x.end]) : [[0, n / fps]];
      for (const [s0, s1] of spans) {
        const i0 = Math.max(0, Math.floor(s0 * fps)), i1 = Math.min(n, Math.ceil(s1 * fps));
        const usual = pct(Array.from(held.subarray(i0, i1)), 0.5) || ref;
        for (let i = i0; i < Math.min(i1, heard); i++) {
          const dip = Math.pow(Math.min(1, held[i] / (0.55 * usual + 1e-6)), 1.6);
          const overall = Math.max(0.3, Math.pow(Math.min(1, held[i] / (0.5 * ref + 1e-6)), 0.8));
          g[i] = Math.max(0.04, dip * overall);
        }
      }
      d = { g, sig }; DIM.set(a, d);
    }
    return d.g[clamp(Math.floor(t * fps), 0, d.g.length - 1)];
  }
  /** How calm a song is overall, 0..1: a chill show is; else how much of its look comes from the calm scenes
   *  (indie pop, dreamy, R&B, soundtrack, folk, jazz), less as the genre model hears it drive harder (its
   *  level: 0.85 or under counts in full, gone by 1.25: an indie song that pushes is not a calm one). */
  function songCalm() {
    if (showStyle() === 'chill') return 1;
    const w = sceneWeights(), total = Object.values(w).reduce((x, y) => x + y, 0) || 1;
    const share = Object.entries(w).filter(([k]) => SCENES[k].calm).reduce((x, [, v]) => x + v, 0) / total;
    const g = (S.analysis && S.analysis.genre) || {}, lv = typeof g.level === 'number' ? g.level : 1.2;
    return clamp(share, 0, 1) * clamp((1.25 - lv) / 0.4, 0, 1);
  }
  /** How calm the music is at t, 0..1. In a chill song or a chill part the brightness only swells, slowly:
   *  quick pulsing reads as a crazy part of the song whatever the colors and effects are (the user).
   *  An intro, breakdown or outro without drums is calm; so is a verse without drums that is quiet for the
   *  song. In a calm song (songCalm) its verses and quieter parts are calm as far as the song is, its
   *  buildups and choruses nearly so (0.8 of it; a chill show's drops 0.6): they swell in too; a part well below the rest of its song
   *  (relLoud) is calm partly whatever the song. Drops and buildups of a song that drives are never calm,
   *  and the calm lets go over the beat before a drop so the breath and the hit land. A low party level
   *  calms the show too (a quarter: a calm background, all swells). */
  const CALM = new WeakMap();
  function calmAt(t) {
    const a = S.analysis, p = P(); if (!a || !p) return 0;
    const sig = p.sections.map(x => x.type + x.start.toFixed(2)).join(',') + showStyle();
    let d = CALM.get(a);
    if (!d || d.sig !== sig) {
      const fps = a.feature_fps || 30, dr = (a.curves || {}).drums_present || [], en = a.energy || [];
      const mean = (c, t0, t1) => { let m = 0, n = 0; for (let i = Math.floor(t0 * fps); i < Math.min(c.length, t1 * fps); i++) { m += c[i]; n++; } return n ? m / n : 0; };
      const sorted = Array.from(en).filter(x => x > 0).sort((x, y) => x - y), ref = sorted.length ? sorted[Math.floor(sorted.length * 0.9)] : 1;
      const chill = showStyle() === 'chill', sc = songCalm();
      d = { sig, song: sc, drops: p.sections.filter(x => x.type === 'drop').map(x => x.start), secs: p.sections.map(x => {
        const drums = mean(dr, x.start, x.end), loud = mean(en, x.start, x.end) / (ref || 1), rel = relLoud(x);
        let c;
        if (x.type === 'drop' || x.type === 'buildup') c = chill ? (x.type === 'drop' ? 0.6 : 0.8) : 0.8 * sc;
        else if (['intro', 'breakdown', 'outro'].includes(x.type)) c = drums < 0.3 ? 1 : Math.max(0.4, sc);
        else c = drums < 0.3 && loud < 0.6 ? 1 : Math.max(sc, rel <= -0.4 ? 0.5 : 0);
        return [x.start, x.end, c];
      }) };
      CALM.set(a, d);
    }
    let c = 0;
    for (const [s0, s1, k] of d.secs) if (t >= s0 && t < s1) { c = k; break; }
    const beat = 60 / (a.bpm || 120);
    if (c > 0) for (const t0 of d.drops) if (t0 > t && t0 - t < beat) c *= (t0 - t) / beat;
    return Math.max(c, 1 - calmRamp(0.3, 0.9));
  }
  /** The brightness speed limit (calmAt). Each output (the lights, the preview, the room view) remembers what
   *  it last showed. In calm music each light as a whole may brighten or dim only so fast (all the way in
   *  2 s when fully calm, 1 s half calm), so a pulse becomes a swell and a cut a crossfade, while things
   *  moving along it keep moving (a comet does not change how bright the light is overall); each pixel too,
   *  more loosely (all the way in half a second when fully calm), which takes the edge off a hard flash.
   *  Outside the calm nothing is held back, so a drop still hits at once. Paused, a seek or a new song: the
   *  frame as it is. */
  const SMOOTH = {};
  function calmSmooth(key, t, frame, calm) {
    const song = (S.name || '') + '|' + ((S.analysis && S.analysis.duration) || 0), st = SMOOTH[key];
    const fresh = () => { SMOOTH[key] = { song, t, px: new Map(frame.map(f => [f.light.ip, Float32Array.from(f.px)])) }; return frame; };
    if (!S.audio.playing || !st || st.song !== song || t < st.t - 0.05 || t - st.t > 0.5) return fresh();
    const dt = Math.max(0, t - st.t), c = calm ?? calmAt(t); st.t = t;
    if (c <= 0.01) { for (const f of frame) st.px.set(f.light.ip, Float32Array.from(f.px)); return frame; }
    const pxStep = 255 * dt / (0.5 * c), meanStep = 255 * dt / (2 * c);
    for (const f of frame) {
      const prev = st.px.get(f.light.ip);
      if (!prev || prev.length !== f.px.length) { st.px.set(f.light.ip, Float32Array.from(f.px)); continue; }
      const nx = new Float32Array(f.px.length); let m0 = 0, m1 = 0;
      for (let i = 0; i < nx.length; i++) { nx[i] = prev[i] + clamp(f.px[i] - prev[i], -pxStep, pxStep); m0 += prev[i]; m1 += nx[i]; }
      const gap = Math.abs(m1 - m0) / nx.length;
      if (gap > meanStep) { const k = meanStep / gap; for (let i = 0; i < nx.length; i++) nx[i] = prev[i] + (nx[i] - prev[i]) * k; }
      st.px.set(f.light.ip, nx);
      f.px = Array.from(nx, v => Math.round(v));
    }
    return frame;
  }

  /** The song's main color, as 0..1 RGB: the jam's primary for this song, else its chosen main color,
   *  else the first color of its palette. */
  /** The party level (the Party Hub's slider) is how intense the show is, not only how bright: at 100% the
   *  show as built; lower, it calms down, live, as it plays. The strobes, flashes, glitches, blackouts and
   *  black cuts go first (none under half); the one-shots thin out (each kept or not by the level, the same
   *  every time: none at 25%, all from 95%); the drum hits soften to a gentle breath; the moving things ease
   *  back and the meteors stop; the beds play on at full, and the pumping with the kick and bass fades. So at
   *  a quarter it is a nice background in the song's colors (the user's idea), at 100% the concert. */
  const PARTY_FLASHY = new Set(['strobe', 'drumstrobe', 'whiteflash', 'flash', 'lightning', 'glitch', 'scramble', 'hyperchase', 'chaos',
    'dropblast', 'fillflood', 'colorslam', 'drumgate', 'dark', 'blackout', 'thunder', 'fireworks', 'strobeRiser']);
  const PARTY_ONE_SHOTS = new Set(['sweepout', 'ripple', 'hookstrike', 'fillrush', 'roomsweep', 'ghostspark', 'wobble', 'deep808', 'ticks', 'heartbeat', 'spotlight']);
  const PARTY_HITS = new Set(['pulse', 'drumhits', 'basshits', 'zonehits', 'buildhits', 'bassnote', 'leadtrace', 'leadglow', 'halves', 'roomsplit', 'hitswap', 'battle', 'blocks', 'alternate', 'roomchase', 'pulsar']);
  const calmRamp = (lo, hi) => clamp(((S.partyLevel ?? 1) - lo) / (hi - lo), 0, 1);
  function partyScale(clip) {
    const L = S.partyLevel ?? 1; if (L >= 0.999) return 1;
    const fx = clip.effect;
    if (clip.alone) return 0.3 + 0.7 * calmRamp(0.2, 0.9);                     // a build's strobe that plays alone: softer, never gone
    if (clip.blend === 'cut' || PARTY_FLASHY.has(fx)) return calmRamp(0.5, 0.9);
    if (PARTY_ONE_SHOTS.has(fx) || (clip.len <= 4 && clip.blend !== 'cut' && !BED.has(fx))) {
      let h = 0; for (const ch of String(clip.id || clip.start)) h = (h * 31 + ch.charCodeAt(0)) >>> 0;
      return Effects.hash(h) < calmRamp(0.3, 0.95) ? 0.5 + 0.5 * calmRamp(0.3, 0.9) : 0;
    }
    if (fx === 'meteorfall') return calmRamp(0.4, 0.85);
    if (PARTY_HITS.has(fx)) return 0.3 + 0.7 * calmRamp(0.2, 0.9);
    if (BED.has(fx) || fx === 'tubeform' || fx === 'candle' || fx === 'starlight' || fx === 'solid' || fx === 'roomwave' || fx === 'twinkle') return 1;
    return 0.55 + 0.45 * calmRamp(0.2, 0.85);                                  // the moving things
  }
  function partyColor() {
    const p = P(); const hx = (S.jamColors && S.jamColors[0]) || (p && p.settings && p.settings.mainColor)
      || (p && (p.palettes[p.defaultPalette] || [])[0]) || '#ffffff';
    return Effects.hex2rgb(Effects.solid(hx));
  }
  function renderFrame(t, lights = selectedLights(), smoothKey = null) {
    const p = P(); const b = timeToBeat(t); const f = featureAt(t); const lanePal = currentPalette(b);
    const music = musicAt(t), calm = calmAt(t);
    // (in calm music the beat's pulse is a slow swell over the bar instead of a kick every beat)
    const kick = Math.exp(-frac(b) * 6) * (1 - calm) + calm * (0.5 + 0.5 * Math.cos(frac((b - p.phase) / 4) * Math.PI * 2));
    const master = loudnessGain(t);                 // always full brightness; only the music's loudness dims it
    const out = [];
    S.roomSpaces = S.roomSpaces || {};
    const key = roomSpaceKey(lights);
    if (!S.roomSpaces[key] && Object.keys(S.roomSpaces).length >= 16) S.roomSpaces = {};   // (one per set of lights and rooms seen: kept from growing all night)
    const space = S.roomSpaces[key] || (S.roomSpaces[key] = roomSpace(lights));
    for (const L of lights) {
      const room = space.of.get(L) || { i: 0, n: 1, side: 0, kinds: [lightKind(L)] };
      const kind = lightKind(L), tubes = kind === 'tubes' ? { count: TUBE_COUNT, per: Math.max(1, Math.round(L.segments / TUBE_COUNT)) } : null;
      // a light alone in its room plays no party-only clips (the Office); the kitchen's tubes are party
      const alone = !tubes && room.n === 1 && !!(S.rooms && S.rooms.length);
      const n = Math.max(1, L.segments); let dst = buf('dst' + L.ip, n); dst.fill(0);
      // The meteor tubes hang scattered over the kitchen: the show plays across the tubes (each tube one
      // pixel of it: washes, hits, flashes, a chase tube to tube), each tube's color runs down its LEDs
      // (a little softer at the bottom), and their own layers (the meteors falling, target 'tubes') play on
      // every LED on top, as bright as every other light (dimmed under the meteors, they were too dark: the user).
      const layers = (out, np, keyOf, wanted, sub) => {
      for (const pass of [0, 1]) p.lanes.forEach((lane, li) => {
        if (lane.hidden) return;
        let clip = null;
        for (const c of lane.clips) if (b >= c.start && b < c.start + c.len && wanted(c) && !(c.party && alone) && (!clip || c.start >= clip.start)) clip = c;
        if (!clip || (clip.blend === 'cut') !== (pass === 1)) return;
        const e = Effects.byId[clip.effect]; if (!e) return;
        const n = np, src = buf('src' + li + keyOf, n); src.fill(0);
        for (let i = 0; i < n; i++) src[i * 4 + 3] = 1;  // full coverage unless the effect clears
        const clipBeat = b - clip.start;
        const c = { n, out: src, t, beat: b, beatSec: Math.max(0.05, beatToTime(b + 1) - beatToTime(b)), phase: frac(b), bar: (b - p.phase) / 4, barPhase: frac((b - p.phase) / 4),
          clipT0: beatToTime(clip.start), clipT1: beatToTime(clip.start + clip.len),
          clipT: clamp(clipBeat / clip.len, 0, 1), clipBeats: clip.len, clipBeat, p: { ...Effects.defaults(clip.effect), ...clip.params },
          pal: clipPalette(clip, lanePal), energy: f.energy, bands: f.bands, kick, rnd: Effects.hash, music, room, tubes: sub };
        e.render(c);
        let alpha = (clip.opacity ?? 1) * partyScale(clip);
        if (calm > 0 && (clip.blend === 'cut' || PARTY_FLASHY.has(clip.effect))) alpha *= 1 - calm;   // (no flashes in calm music)
        if (alpha <= 0.002) return;
        // Ducking, like sidechain compression: the layer dips on every kick and snare and swells back,
        // so the hits on top land on a dark strip and the whole show breathes with the drums.
        if (clip.duck > 0 && music.hasDrums) {
          let env = 0;
          for (const h of [music.kick, music.snare]) if (h && h.index >= 0) env = Math.max(env, Math.exp(-h.age / 0.16) * (0.6 + 0.4 * h.strength));
          alpha *= 1 - clamp(clip.duck, 0, 1) * env * calmRamp(0.3, 0.9) * (1 - calm);
        }
        // a drop's bed can breathe with the bass: brighter as the bass swells, dimmer as it drops out
        if (clip.bassPump > 0) { const pump = clip.bassPump * calmRamp(0.3, 0.9) * (1 - calm); alpha *= (1 - pump) + pump * clamp(music.bassLevel * 1.15, 0, 1); }
        if (clip.fadeIn > 0) alpha *= clamp(clipBeat / clip.fadeIn, 0, 1);
        if (clip.fadeOut > 0) alpha *= clamp((clip.len - clipBeat) / clip.fadeOut, 0, 1);
        Effects.composite(out, src, n, alpha, clip.blend || 'normal');
      });
      };
      if (!tubes) layers(dst, n, L.ip, c => !c.target || c.target === kind, null);
      else {
        const across = buf('tubes' + L.ip, tubes.count); across.fill(0);
        layers(across, tubes.count, L.ip + 't', c => !c.target, null);
        let form = null;
        for (const lane of p.lanes) if (!lane.hidden) for (const c of lane.clips) if (c.target === 'tubes' && b >= c.start && b < c.start + c.len) {
          if (c.effect === 'tubeform') form = c.params || {};
        }
        const shape = tubeShape(form, tubes, b);
        for (let tb = 0; tb < tubes.count; tb++) for (let k = 0; k < tubes.per; k++) {
          const i = (tb * tubes.per + k) * 4, j = tb * 4, sh = shape(tb, k);
          if (i + 3 >= dst.length) break;
          dst[i] = across[j] * sh; dst[i + 1] = across[j + 1] * sh; dst[i + 2] = across[j + 2] * sh; dst[i + 3] = 1;
        }
        layers(dst, n, L.ip + 'm', c => c.target === 'tubes', tubes);
      }
      // The party level (set on the Party Hub): below 100% the song's main color is laid over the whole
      // show, that much more opaque, so the room calms down into one color without going dark.
      const cover = 0.5 * (1 - clamp(S.partyLevel ?? 1, 0, 1)), pc = cover > 0 ? partyColor() : null;   // (gentler: the show calms itself, partyScale)
      if (pc) for (let i = 0; i < n; i++) for (let k = 0; k < 3; k++) dst[i * 4 + k] = clamp(dst[i * 4 + k], 0, 1) * (1 - cover) + pc[k] * cover;
      const flat = new Array(n * 3);
      for (let i = 0; i < n; i++) { flat[i * 3] = Math.round(clamp(dst[i * 4], 0, 1) * 255 * master); flat[i * 3 + 1] = Math.round(clamp(dst[i * 4 + 1], 0, 1) * 255 * master); flat[i * 3 + 2] = Math.round(clamp(dst[i * 4 + 2], 0, 1) * 255 * master); }
      // between songs (the jam): a dim glow of a song's main color where the show is darker (see jam.js
      // setGlow), laid on after the brightness follows the music, so the quiet does not fade it away
      const g = S.glowAt ? S.glowAt(t) : null;
      if (g && g.level > 0) for (let i = 0; i < n * 3; i++) flat[i] = Math.max(flat[i], Math.round(g.rgb[i % 3] * g.level * 255));
      out.push({ light: L, px: flat });
    }
    return smoothKey ? calmSmooth(smoothKey, t, out, calm) : out;
  }
  /** How a tube carries its color down its LEDs (0 at the top): the whole tube (a little softer at the bottom),
   *  an orb floating in the middle fading to both ends (the user's idea), orbs drifting up and down (each tube
   *  in its own time, a slow bob over 8 beats), or a glow from the top fading down. */
  function tubeShape(form, tubes, beat) {
    const per = tubes.per, last = Math.max(1, per - 1), shape = (form && form.shape) || 'full';
    const bell = (k, mid, w) => Math.exp(-0.5 * ((k - mid) / w) ** 2);
    if (shape === 'orb') return (tb, k) => bell(k, last / 2, per * 0.2);
    if (shape === 'drift') return (tb, k) => bell(k, last / 2 + Math.sin((beat / 8 + Effects.hash(tb * 7919 + 3)) * Math.PI * 2) * per * 0.28, per * 0.17);
    if (shape === 'top') return (tb, k) => Math.pow(1 - k / (last + 1), 1.6);
    return (tb, k) => 1 - 0.15 * k / last;
  }
  // The room view (roomview.html, its own window): the party rooms' lights as the party would see them,
  // simulated, whichever lights are ticked (testing on the Office strip, it shows the living room and the
  // game room). It says hello every second; frames go to it ~30 times a second while it does.
  const roomViewCh = typeof BroadcastChannel === 'function' ? new BroadcastChannel('lss-roomview') : null;
  if (roomViewCh) {
    // it says hello every second: the studio answers whether or not a song is open (the room view closes
    // itself when the studio stops answering), and says goodbye as its window closes
    roomViewCh.onmessage = e => { if (e.data && e.data.type === 'hello') { S.roomViewAt = performance.now(); roomViewCh.postMessage({ type: 'alive' }); } };
    addEventListener('pagehide', () => { try { roomViewCh.postMessage({ type: 'bye' }); } catch (e) { /* closing anyway */ } });
  }
  function sendRoomView(t) {
    if (!roomViewCh || !S.roomViewAt || performance.now() - S.roomViewAt > 3000) return;
    if (performance.now() - (S.roomViewSent || 0) < 30) return;
    S.roomViewSent = performance.now();
    // (and the kitchen's meteor tubes whenever they are ticked, their room switched on or not: the user)
    const party = partyLights(), lights = [...party, ...S.lights.filter(l => l.on && lightKind(l) === 'tubes' && !party.includes(l))];
    const frame = lights.length ? renderFrame(t, lights, 'room') : [];
    const space = S.roomSpaces[roomSpaceKey(lights)];
    roomViewCh.postMessage({ type: 'frame', song: S.name || '', lights: frame.map(fr => {
      const r = roomOf(fr.light), at = space && space.of.get(fr.light);
      return { name: lightLabel(fr.light), room: r ? r.name : '', kind: lightKind(fr.light), px: fr.px, at: at ? at.i : 0, n: at ? at.n : 1 };
    }) });
  }
  // (opened without a link back to the studio: a window opened from a page runs on that page's thread, so the
  // room view's drawing and the studio's own took turns and both stuttered, full screen on a 4K monitor most
  // of all; on its own it gets its own process. The frames come over the BroadcastChannel either way.)
  function openRoomView() { window.open('/static/roomview.html', 'lss-roomview', 'noopener,width=1000,height=560'); }
  function drawPreview(frame) {
    const W = Math.floor(pv.clientWidth * devicePixelRatio), H = Math.floor(pv.clientHeight * devicePixelRatio);
    if (pv.width !== W || pv.height !== H) { pv.width = W; pv.height = H; }
    const w = pv.clientWidth / UI, h = pv.clientHeight / UI;
    pctx.setTransform(devicePixelRatio * UI, 0, 0, devicePixelRatio * UI, 0, 0);
    pctx.fillStyle = '#0b0c0f'; pctx.fillRect(0, 0, w, h);
    if (!frame || !frame.length) { pctx.fillStyle = '#555'; pctx.font = '12px system-ui'; pctx.fillText('No lights selected — tick a light on the left to preview.', 12, 20); return; }
    const rowH = Math.min(36, (h - 8) / frame.length);
    frame.forEach((fr, r) => {
      const y = 6 + r * rowH; const n = fr.px.length / 3; const x0 = 110, x1 = w - 12; const pw = (x1 - x0) / n;
      const away = roomOff(fr.light);
      pctx.globalAlpha = away ? 0.35 : 1;
      pctx.fillStyle = '#9aa'; pctx.font = '11px system-ui'; pctx.fillText(lightLabel(fr.light), 8, y + rowH / 2 + 4);
      for (let i = 0; i < n; i++) {
        pctx.fillStyle = `rgb(${fr.px[i * 3]},${fr.px[i * 3 + 1]},${fr.px[i * 3 + 2]})`;
        const px = x0 + i * pw; pctx.beginPath(); pctx.roundRect(px + 1, y + 3, Math.max(1, pw - 2), rowH - 8, 3); pctx.fill();
      }
      pctx.globalAlpha = 1;
    });
  }
  // Each frame redraws only what changed: while nothing moves (paused, no drag or hover) the timeline and the
  // overview are left as they are, and the light preview refreshes ~5 times a second (the lights' own paused rate),
  // so a paused studio no longer redraws everything 60 times a second.
  const _shown = { key: '', preview: 0, text: {} };
  function setText(id, v) { if (_shown.text[id] !== v) { _shown.text[id] = v; $('#' + id).textContent = v; } }
  function loop() {
    requestAnimationFrame(loop);
    if (!S.analysis) return;
    const t = pos();
    if (S.audio.playing && S.view.follow !== false) followPlayhead(t);
    setText('timeDisplay', fmtTime(t));
    setText('barDisplay', 'bar ' + barLabel(timeToBeat(t)));
    const v = S.view, d = v.hoverDrop, m = v.drag && v.drag.type === 'marquee' ? v.drag : null;
    const key = [t.toFixed(4), v.scroll, v.pps, v.vscroll, tl.clientWidth, tl.clientHeight, ov.clientWidth, ov.clientHeight,
      d ? [d.x, d.y, d.w, d.h, d.replace].join() : '', m ? [m.x0, m.y0, m.x, m.y].join() : ''].join('|');
    const now = performance.now(), moved = key !== _shown.key, dirty = S.dirty || S.ovDirty;
    if (moved || dirty || now - _shown.preview > 200) { _shown.preview = now; drawPreview(renderFrame(t, undefined, 'preview')); }
    if (!moved && !dirty) return;
    _shown.key = key;
    if (S.dirty) { S.dirty = false; _draw(); }
    compose();
    drawOverview();
  }
  // Light output runs on its own clock in a Web Worker: browsers freeze
  // requestAnimationFrame and throttle timers when the tab is not in front,
  // which would silently stop the lights. Worker ticks keep coming regardless.
  /** a light mounted the other way round: its segments in reverse order (each keeps its r, g, b) */
  function flipPx(px) { const n = px.length / 3, o = new Array(px.length); for (let i = 0; i < n; i++) { const j = (n - 1 - i) * 3; o[i * 3] = px[j]; o[i * 3 + 1] = px[j + 1]; o[i * 3 + 2] = px[j + 2]; } return o; }
  function outputTick() {
    if (!S.analysis || !S.outputActive) return;
    if (!S.audio.playing && !S.forceSend && performance.now() - S.lastLightSend < 200) return;   // paused: ~5 fps hold
    const t = pos();
    const lt = S.audio.playing ? t + (parseFloat($('#latency').value) || 0) / 1000 : t;
    const lf = renderFrame(lt, undefined, 'lights');
    wsSend({ type: 'frame', ips: lf.map(f => f.light.ip), frames: lf.map(f => f.light.flip ? flipPx(f.px) : f.px) });
    S.forceSend = false; S.framesSent = (S.framesSent || 0) + 1; S.lastLightSend = performance.now();
  }
  // (the room view's frames go on this clock too: it keeps ticking with the studio minimized or behind)
  function clockTick() { outputTick(); if (S.analysis) sendRoomView(pos()); }
  (function startOutputClock() {
    try {
      const src = 'setInterval(function(){ postMessage(0); }, 16);';
      const w = new Worker(URL.createObjectURL(new Blob([src], { type: 'application/javascript' })));
      w.onmessage = clockTick;
    } catch (e) { setInterval(clockTick, 16); }
  })();
  function fmtTime(t) { const m = Math.floor(t / 60), s = t - m * 60; return `${m}:${s.toFixed(3).padStart(6, '0')}`; }

  // ------------------------------------------------------------------ timeline geometry
  const xOf = t => HEADER + (t - S.view.scroll) * S.view.pps;
  const tOf = x => S.view.scroll + (x - HEADER) / S.view.pps;
  const lanesTop = () => ROW.ruler + ROW.sections + ROW.wave;
  const laneTop = i => lanesTop() + i * ROW.lane - (S.view.vscroll || 0);
  const ADD_ROW = 26;  // the "+ layer" strip under the last layer
  function lanesOverflow() { const h = TH(); return Math.max(0, lanesTop() + P().lanes.length * ROW.lane + ADD_ROW - h); }
  function vscrollTo(v) { S.view.vscroll = clamp(v, 0, lanesOverflow()); draw(); }
  const waveTop = () => ROW.ruler + ROW.sections;
  function viewWidth() { return TW() - HEADER; }
  function zoomFit() { if (!S.analysis) return; if (viewWidth() <= 0) { S.view.fitPending = true; return; } S.view.pps = Math.max(1, viewWidth() / duration()); S.view.scroll = 0; draw(); }
  const wheelZoom = e => clamp(Math.pow(1.002, -(e.deltaMode === 1 ? e.deltaY * 16 : e.deltaY)), 0.4, 2.5);
  function zoomAt(factor, xPix) {
    const t = tOf(xPix); const minPps = viewWidth() / Math.max(1, duration() * 4);   // out to four songs' width: room past the end
    S.view.pps = clamp(S.view.pps * factor, minPps, 600);
    S.view.scroll = clamp(t - (xPix - HEADER) / S.view.pps, 0, Math.max(0, duration() - viewWidth() / S.view.pps)); draw();
  }
  function scrollTo(s) { S.view.scroll = clamp(s, 0, Math.max(0, duration() - viewWidth() / S.view.pps)); draw(); }
  function followPlayhead(t) { const x = xOf(t); const w = TW(); if (x > w - 40 || x < HEADER) scrollTo(t - 0.1 * viewWidth() / S.view.pps); }

  // ------------------------------------------------------------------ drawing
  // The static timeline (grid, waveform, clips) renders into an offscreen canvas only
  // when something changes; the playhead and drag ghost are composed on top every frame.
  const off = document.createElement('canvas'), octx = off.getContext('2d');
  function draw() { S.dirty = true; S.ovDirty = true; }
  function _draw() {
    if (!S.analysis) return;
    const w = TW(), h = TH();
    if (!w || !h) { S.dirty = true; return; }   // hidden or collapsed: try again next frame
    // Layers share the height there is, rather than sitting in a strip. On a short window everything
    // still fits: the waveform gives up height first, then the layers go down to a compact one-line
    // size, so "+ add layer" is never pushed off the bottom.
    const nl = Math.max(4, P().lanes.length), fixed = ROW.ruler + ROW.sections + ADD_ROW + 2;
    ROW.wave = 84;
    let lane = (h - fixed - ROW.wave) / nl;
    if (lane < 44) { ROW.wave = Math.round(Math.max(44, h - fixed - 44 * nl)); lane = (h - fixed - ROW.wave) / nl; }
    ROW.lane = Math.round(Math.max(28, Math.min(78, lane)));
    if ((S.view.vscroll || 0) > lanesOverflow()) S.view.vscroll = lanesOverflow();
    if (S.view.fitPending) { S.view.fitPending = false; S.view.pps = Math.max(1, viewWidth() / duration()); S.view.scroll = 0; }
    const W = Math.floor(tl.clientWidth * devicePixelRatio), H = Math.floor(tl.clientHeight * devicePixelRatio);
    if (tl.width !== W || tl.height !== H) { tl.width = W; tl.height = H; }
    if (off.width !== W || off.height !== H) { off.width = W; off.height = H; }
    const g = octx; g.setTransform(devicePixelRatio * UI, 0, 0, devicePixelRatio * UI, 0, 0);
    g.fillStyle = '#121317'; g.fillRect(0, 0, w, h);
    const p = P(), a = S.analysis; const t0 = tOf(HEADER), t1 = tOf(w);
    const beatPx = S.view.pps * 60 / p.bpm;

    // beat / bar grid across the whole timeline
    const b0 = Math.max(0, Math.floor(timeToBeat(t0))), b1 = Math.min(p.beats.length - 1, Math.ceil(timeToBeat(t1)));
    for (let i = b0; i <= b1; i++) {
      const isBar = ((i - p.phase) % 4 + 4) % 4 === 0; const x = xOf(p.beats[i]);
      if (!isBar && beatPx < 5) continue;
      g.strokeStyle = isBar ? 'rgba(255,255,255,0.13)' : 'rgba(255,255,255,0.05)'; g.beginPath(); g.moveTo(x, ROW.ruler); g.lineTo(x, h); g.stroke();
      if (isBar && (beatPx * 4 > 26 || barOf(i) % Math.ceil(30 / Math.max(1, beatPx * 4)) === 0)) { g.fillStyle = '#8b92a3'; g.font = '10px system-ui'; g.fillText(String(barOf(i) + 1), x + 3, 14); }
    }
    // ruler: seconds
    g.fillStyle = '#1a1c22'; g.fillRect(HEADER, 0, w, ROW.ruler); g.fillStyle = '#8b92a3'; g.font = '10px system-ui';
    const secStep = S.view.pps > 80 ? 1 : S.view.pps > 25 ? 5 : S.view.pps > 8 ? 10 : 30;
    for (let s = Math.floor(t0 / secStep) * secStep; s < t1; s += secStep) { const x = xOf(s); g.fillStyle = '#3a3f4b'; g.fillRect(x, ROW.ruler - 6, 1, 6); g.fillStyle = '#8b92a3'; g.fillText(fmtTime(s).slice(0, -4), x + 2, ROW.ruler - 8); }
    // drops
    for (const d of p.drops) { const x = xOf(d); g.fillStyle = '#ef4444'; g.beginPath(); g.moveTo(x - 6, 0); g.lineTo(x + 6, 0); g.lineTo(x, 9); g.fill(); g.fillRect(x, 0, 1, h); }

    // sections
    const sy = ROW.ruler;
    p.sections.forEach((s, i) => {
      const x0 = xOf(s.start), x1 = xOf(s.end); if (x1 < HEADER || x0 > w) return;
      const vx = Math.max(HEADER, x0), vw = Math.max(0, x1 - vx - 1);        // measured from where it is drawn
      g.fillStyle = SECTION_COLORS[s.type] || '#555'; g.globalAlpha = 0.85; g.fillRect(vx, sy + 2, vw, ROW.sections - 4); g.globalAlpha = 1;
      g.save(); g.beginPath(); g.rect(vx, sy, Math.max(0, vw - 2), ROW.sections); g.clip();
      g.fillStyle = '#fff'; g.font = 'bold 10px system-ui'; g.fillText(s.type.toUpperCase(), vx + 4, sy + 14); g.restore();
      if (S.view.sel && S.view.sel.type === 'section' && S.view.sel.index === i) { g.strokeStyle = '#fff'; g.lineWidth = 2; g.strokeRect(x0, sy + 1, x1 - x0, ROW.sections - 2); g.lineWidth = 1; }
    });

    // waveform
    const wy = waveTop(), wh = ROW.wave; g.fillStyle = '#0e0f13'; g.fillRect(HEADER, wy, w, wh);
    const pps = a.wave_pps; const i0 = Math.max(0, Math.floor(t0 * pps)), i1 = Math.min(a.peaks.length - 1, Math.ceil(t1 * pps));
    g.fillStyle = '#4b6bff';
    if (S.view.pps / pps >= 1) { for (let i = i0; i <= i1; i++) { const x = xOf(i / pps); const ph = a.peaks[i] * (wh / 2 - 4); g.fillRect(x, wy + wh / 2 - ph, Math.max(1, S.view.pps / pps), ph * 2); } }
    else { const step = pps / S.view.pps; for (let x = HEADER; x < w; x++) { const j0 = Math.floor((tOf(x)) * pps), j1 = Math.floor(tOf(x + 1) * pps); let m = 0; for (let j = j0; j <= j1 && j < a.peaks.length; j++) if (j >= 0) m = Math.max(m, a.peaks[j]); const ph = m * (wh / 2 - 4); g.fillRect(x, wy + wh / 2 - ph, 1, ph * 2); } }
    // energy line
    g.strokeStyle = 'rgba(255,255,255,0.35)'; g.beginPath();
    for (let x = HEADER; x < w; x += 2) { const e = featureAt(clamp(tOf(x), 0, duration())).energy; const y = wy + wh - 2 - e * (wh - 6); x === HEADER ? g.moveTo(x, y) : g.lineTo(x, y); } g.stroke();
    // beat markers on the waveform (editable)
    const be = $('#beatEdit').checked;
    if (beatPx >= 3 || be) for (let i = b0; i <= b1; i++) {
      const x = xOf(p.beats[i]); const isBar = ((i - p.phase) % 4 + 4) % 4 === 0; const selB = S.view.sel && S.view.sel.type === 'beat' && S.view.sel.index === i;
      g.fillStyle = selB ? '#fff' : isBar ? '#ffd166' : be ? '#8ad8ff' : 'rgba(138,216,255,0.5)';
      g.beginPath(); g.moveTo(x - 4, wy); g.lineTo(x + 4, wy); g.lineTo(x, wy + 7); g.fill();
      if (be) g.fillRect(x, wy, 1, wh);
    }
    if (be) { g.fillStyle = 'rgba(255,209,102,0.9)'; g.font = 'bold 10px system-ui'; g.fillText('BEAT EDIT: click adds a beat, Space taps while playing, drag moves, Delete removes', HEADER + 8, wy + wh - 6); }

    // lanes (scrollable region under the fixed rows)
    g.save(); g.beginPath(); g.rect(0, lanesTop(), w, h - lanesTop()); g.clip();
    p.lanes.forEach((lane, li) => {
      const y = laneTop(li); g.fillStyle = lane.hidden ? '#0c0d10' : li % 2 ? '#14161b' : '#121317'; g.fillRect(HEADER, y, w, ROW.lane); g.strokeStyle = '#2f333d'; g.beginPath(); g.moveTo(HEADER, y); g.lineTo(w, y); g.stroke();
      // a hidden layer is greyed out as a whole: its clips in grey, faded
      // (a jam song under review whose lights are the jam's own, untouched: every layer at half, until a change to
      // the lights makes it your own show; undone back to the jam's, half again)
      const ga = (lane.hidden ? 0.35 : 1) * (S.review && S.review.id && !S.review.custom ? 0.5 : 1); g.globalAlpha = ga; g.filter = lane.hidden ? 'grayscale(1)' : 'none';
      for (const c of lane.clips) {
        const x0 = xOf(beatToTime(c.start)), x1 = xOf(beatToTime(c.start + c.len)); if (x1 < HEADER || x0 > w) continue;
        // a clip that starts off to the left is drawn from the timeline's edge: its width must be
        // measured from there too, or it (and its label) spills over the next clip
        const vx = Math.max(HEADER, x0), vw = Math.max(0, x1 - vx - 2);
        const e = Effects.byId[c.effect]; const sw = e ? e.swatch : ['#666', '#333'];
        const [cy, ch] = clipBox(c, y);
        const grad = g.createLinearGradient(x0, cy, x0, cy + ch); grad.addColorStop(0, sw[0]); grad.addColorStop(1, sw[1]);
        g.fillStyle = grad; g.globalAlpha = 0.55 * ga; g.beginPath(); g.roundRect(vx, cy, vw, ch, 5); g.fill(); g.globalAlpha = ga;
        const isSel = isSelected(c.id);
        g.strokeStyle = isSel ? '#fff' : 'rgba(255,255,255,0.25)'; g.lineWidth = isSel ? 2 : 1;
        g.beginPath(); g.roundRect(vx, cy, vw, ch, 5); g.stroke(); g.lineWidth = 1;
        g.save(); g.beginPath(); g.rect(vx, cy, Math.max(0, vw - 4), ch); g.clip();
        const compact = ch < 34;
        g.fillStyle = '#fff'; g.font = 'bold 11px system-ui'; g.fillText((e ? e.name : c.effect) + (c.target && compact ? (c.target === 'long' ? ' \u00b7 strips' : ' \u00b7 lamps') : ''), Math.max(HEADER, x0) + 6, cy + (compact ? Math.min(12, ch - 4) : 15));
        if (!compact) { g.fillStyle = 'rgba(255,255,255,0.7)'; g.font = '10px system-ui'; g.fillText(clipSummary(c), Math.max(HEADER, x0) + 6, cy + 27); }
        g.restore();
        // the clip's own colors, as a bar along its bottom edge
        const cols = clipPaletteHex(c); const bx0 = Math.max(HEADER, x0) + 5, bx1 = Math.min(w, x1) - 7;
        if (bx1 > bx0 + 4) { const pg = g.createLinearGradient(bx0, 0, bx1, 0); cols.forEach((col, i) => pg.addColorStop(i / Math.max(1, cols.length - 1), col)); g.fillStyle = pg; g.beginPath(); if (compact) g.roundRect(bx0, cy + ch - 5, bx1 - bx0, 3, 1.5); else g.roundRect(bx0, cy + ch - 8, bx1 - bx0, 6, 3); g.fill(); }
      }
      g.globalAlpha = 1; g.filter = 'none';
    });
    g.restore();
    // past the end of the song (zoomed out far enough to see it): shaded, with a line at the end
    const xe = xOf(duration());
    if (xe < w) { const x0 = Math.max(HEADER, xe); g.fillStyle = 'rgba(0,0,0,0.45)'; g.fillRect(x0, 0, w - x0, h);
      if (xe >= HEADER) { g.fillStyle = 'rgba(255,255,255,0.35)'; g.fillRect(xe, 0, 1, h); g.fillStyle = '#8b92a3'; g.font = '10px system-ui'; g.fillText('end', xe + 5, ROW.ruler - 7); } }
    // lane headers
    g.fillStyle = '#1a1c22'; g.fillRect(0, 0, HEADER, h); g.strokeStyle = '#2f333d'; g.beginPath(); g.moveTo(HEADER - 0.5, 0); g.lineTo(HEADER - 0.5, h); g.stroke();
    g.save(); g.beginPath(); g.rect(0, lanesTop(), w, h - lanesTop()); g.clip();
    p.lanes.forEach((lane, li) => {
      const ly = laneTop(li);
      const small = ROW.lane < 42, mid = small ? ly + ROW.lane / 2 : ly + 22;
      g.fillStyle = lane.hidden ? '#6b7280' : '#e6e8ee'; g.font = 'bold 12px system-ui'; g.fillText(`Layer ${li + 1}`, 8, mid + 4);
      if (!small) { g.fillStyle = '#8b92a3'; g.font = '10px system-ui'; g.fillText(lane.hidden ? 'hidden' : li === p.lanes.length - 1 ? 'top' : li === 0 ? 'bottom' : '', 8, ly + 36); }
      // eye toggle
      const ex = HEADER - 16, ey = mid; g.strokeStyle = lane.hidden ? '#6b7280' : '#e6e8ee'; g.fillStyle = g.strokeStyle; g.lineWidth = 1.5;
      g.beginPath(); g.ellipse(ex, ey, 8, 5, 0, 0, Math.PI * 2); g.stroke();
      if (!lane.hidden) { g.beginPath(); g.arc(ex, ey, 2.4, 0, Math.PI * 2); g.fill(); }
      else { g.beginPath(); g.moveTo(ex - 9, ey + 7); g.lineTo(ex + 9, ey - 7); g.stroke(); }
      g.lineWidth = 1;
    });
    const ay = laneTop(p.lanes.length); g.fillStyle = '#1f2229'; g.fillRect(0, ay, w, ADD_ROW); g.fillStyle = '#6c8cff'; g.font = 'bold 11px system-ui'; g.fillText('+ add layer', 8, ay + 17);
    g.fillStyle = getComputedStyle(document.body).backgroundColor || '#0b0c10'; g.fillRect(0, ay + ADD_ROW, w, Math.max(0, h - ay - ADD_ROW));
    g.restore();
  }
  /** Where a clip sits in its layer: the whole height, or for one kind of light, its half (strips on top). */
  function clipBox(c, y) {
    if (!c.target) return [y + 4, ROW.lane - 8];
    const h = ROW.lane / 2 - 3;
    return c.target === 'long' ? [y + 3, h] : [y + ROW.lane / 2, h];
  }
  function clipSummary(c) { const e = Effects.byId[c.effect]; if (!e) return ''; const parts = []; if (c.target) parts.push(c.target === 'long' ? 'long strips' : 'lamps'); if (Array.isArray(c.palette)) parts.push('custom colors'); else if (typeof c.palette === 'string') parts.push(c.palette); for (const [k, d] of Object.entries(e.params)) { const v = c.params[k] ?? d.def; if (d.type === 'beats') parts.push(`${+(+v).toFixed(2)}b`); } if (c.opacity != null && c.opacity < 1) parts.push(`${Math.round(c.opacity * 100)}%`); if (c.blend && c.blend !== 'normal') parts.push(c.blend); return parts.join(' · '); }
  function compose() {
    if (!S.analysis || !off.width || !off.height) return;   // nothing rendered yet (or a zero-size canvas)
    const w = TW(), h = TH(); const g = tctx;
    g.setTransform(1, 0, 0, 1, 0, 0); g.drawImage(off, 0, 0);
    g.setTransform(devicePixelRatio * UI, 0, 0, devicePixelRatio * UI, 0, 0);
    if (S.view.hoverDrop) { const d = S.view.hoverDrop; g.fillStyle = d.replace ? 'rgba(255,94,168,0.35)' : 'rgba(108,140,255,0.35)'; g.fillRect(d.x, d.y, d.w, d.h); if (d.replace) { g.strokeStyle = '#ff5ea8'; g.lineWidth = 2; g.strokeRect(d.x, d.y, d.w, d.h); g.lineWidth = 1; g.fillStyle = '#fff'; g.font = 'bold 11px system-ui'; g.fillText('replace', d.x + 6, d.y + d.h - 6); } }
    const x = xOf(pos());
    const bottom = Math.min(h, laneTop(P().lanes.length) + ADD_ROW);
    if (x >= HEADER && x <= w) { g.fillStyle = '#fff'; g.fillRect(x - 0.5, 0, 1.5, bottom); g.beginPath(); g.moveTo(x - 5, 0); g.lineTo(x + 5, 0); g.lineTo(x, 8); g.fill(); }
    const m = S.view.drag && S.view.drag.type === 'marquee' ? S.view.drag : null;
    if (m) { const rx = Math.min(m.x0, m.x), ry = Math.min(m.y0, m.y), rw = Math.abs(m.x - m.x0), rh = Math.abs(m.y - m.y0);
      g.fillStyle = 'rgba(108,140,255,0.18)'; g.fillRect(rx, ry, rw, rh); g.strokeStyle = 'rgba(108,140,255,0.9)'; g.lineWidth = 1; g.strokeRect(rx + 0.5, ry + 0.5, rw, rh); }
  }

  // ---------------------------------------------------------------- overview strip
  // A miniature of the whole song across the top: section blocks, a summary waveform,
  // drop markers, and the current view as a draggable window with zoom handles.
  const ovOff = document.createElement('canvas'), ovOffCtx = ovOff.getContext('2d');
  const ovX = t => (t / Math.max(1e-6, duration())) * ov.clientWidth;       // song time -> overview x
  const ovT = x => clamp((x / Math.max(1, ov.clientWidth)) * duration(), 0, duration());
  const visibleSpan = () => Math.min(duration(), viewWidth() / S.view.pps);  // seconds on screen

  function drawOverviewStatic(w, h) {
    const g = ovOffCtx, p = P(), a = S.analysis;
    g.setTransform(devicePixelRatio, 0, 0, devicePixelRatio, 0, 0);
    g.fillStyle = '#14161b'; g.fillRect(0, 0, w, h);
    const top = 7;                                    // section ribbon height
    for (const s of p.sections) {
      const x0 = ovX(s.start), x1 = ovX(s.end);
      g.fillStyle = SECTION_COLORS[s.type] || '#555';
      g.fillRect(x0, 0, Math.max(1, x1 - x0 - 0.5), top);
      g.globalAlpha = 0.17; g.fillRect(x0, top, Math.max(1, x1 - x0 - 0.5), h - top); g.globalAlpha = 1;
    }
    const mid = top + (h - top) / 2, amp = (h - top) / 2 - 2;
    g.fillStyle = 'rgba(120,150,255,0.85)';
    const pps = a.wave_pps;
    for (let x = 0; x < w; x++) {
      const j0 = Math.floor(ovT(x) * pps), j1 = Math.floor(ovT(x + 1) * pps);
      let m = 0;
      for (let j = j0; j <= j1 && j < a.peaks.length; j++) if (j >= 0 && a.peaks[j] > m) m = a.peaks[j];
      const ph = m * amp;
      if (ph > 0) g.fillRect(x, mid - ph, 1, ph * 2);
    }
    for (const d of p.drops) { const x = ovX(d); g.fillStyle = '#ef4444'; g.fillRect(x - 0.5, 0, 1.5, h); g.beginPath(); g.moveTo(x - 4, 0); g.lineTo(x + 4, 0); g.lineTo(x, 6); g.fill(); }
  }

  function drawOverview() {
    if (!S.analysis) return;
    const w = ov.clientWidth, h = ov.clientHeight;
    if (!w || !h) return;
    const W = Math.floor(w * devicePixelRatio), H = Math.floor(h * devicePixelRatio);
    if (ov.width !== W || ov.height !== H) { ov.width = W; ov.height = H; ovOff.width = W; ovOff.height = H; S.ovDirty = true; }
    if (S.ovDirty) { S.ovDirty = false; drawOverviewStatic(w, h); }
    const g = ovctx;
    g.setTransform(1, 0, 0, 1, 0, 0); g.drawImage(ovOff, 0, 0);
    g.setTransform(devicePixelRatio, 0, 0, devicePixelRatio, 0, 0);
    // everything outside the visible window is dimmed
    const x0 = ovX(S.view.scroll), x1 = ovX(S.view.scroll + visibleSpan());
    g.fillStyle = 'rgba(10,11,14,0.62)';
    g.fillRect(0, 0, Math.max(0, x0), h); g.fillRect(x1, 0, Math.max(0, w - x1), h);
    g.strokeStyle = '#8fa6ff'; g.lineWidth = 1.5;
    g.strokeRect(x0 + 0.75, 0.75, Math.max(2, x1 - x0 - 1.5), h - 1.5);
    g.fillStyle = 'rgba(143,166,255,0.18)'; g.fillRect(x0, 0, Math.max(2, x1 - x0), h);
    g.fillStyle = '#8fa6ff';                                  // grab handles
    for (const hx of [x0 + 1.5, x1 - 4.5]) g.fillRect(hx, h / 2 - 7, 3, 14);
    g.lineWidth = 1;
    const px = ovX(pos());
    g.fillStyle = '#fff'; g.fillRect(px - 0.5, 0, 1.5, h);
  }

  (() => {   // overview: drag to scroll, drag the edges to zoom, click to jump
    const EDGE = 7;
    let drag = null;
    const zone = x => {
      const x0 = ovX(S.view.scroll), x1 = ovX(S.view.scroll + visibleSpan());
      if (Math.abs(x - x0) <= EDGE) return 'l';
      if (Math.abs(x - x1) <= EDGE) return 'r';
      return (x > x0 && x < x1) ? 'move' : 'jump';
    };
    ov.addEventListener('mousedown', e => {
      if (!S.analysis || e.button !== 0) return;
      const x = e.clientX - ov.getBoundingClientRect().left;
      let z = zone(x);
      if (z === 'jump') { scrollTo(ovT(x) - visibleSpan() / 2); z = 'move'; }
      drag = { z, grab: ovT(x) - S.view.scroll, span: visibleSpan(), scroll: S.view.scroll };
      ov.style.cursor = z === 'move' ? 'grabbing' : 'ew-resize';
      e.preventDefault();
    });
    window.addEventListener('mousemove', e => {
      const x = e.clientX - ov.getBoundingClientRect().left;
      // only measure while hovering the strip or dragging it: a layout read on every
      // global mouse move would add jank to clip dragging
      if (!drag) { if (S.analysis && e.target === ov) ov.style.cursor = { l: 'ew-resize', r: 'ew-resize', move: 'grab', jump: 'pointer' }[zone(x)]; return; }
      const t = ovT(x);
      if (drag.z === 'move') scrollTo(t - drag.grab);
      else {
        const right = drag.scroll + drag.span;
        const span = drag.z === 'r' ? t - S.view.scroll : right - t;
        if (span > 0.2) {
          S.view.pps = clamp(viewWidth() / span, viewWidth() / duration(), 600);
          scrollTo(drag.z === 'r' ? S.view.scroll : right - visibleSpan());
        }
      }
    });
    window.addEventListener('mouseup', () => { if (drag) { drag = null; ov.style.cursor = 'grab'; } });
    ov.addEventListener('wheel', e => { e.preventDefault(); zoomAt(wheelZoom(e), (e.clientX - ov.getBoundingClientRect().left) / Math.max(1, ov.clientWidth) * viewWidth() + HEADER); }, { passive: false });
  })();

  // ------------------------------------------------------------------ hit testing
  function hitTest(x, y) {
    if (!S.analysis) return null; const p = P();
    if (x < HEADER) {
      if (y >= lanesTop()) { const li = Math.floor((y - laneTop(0)) / ROW.lane); if (li >= 0 && li < p.lanes.length) return x >= HEADER - 28 ? { type: 'laneEye', lane: li } : { type: 'laneHeader', lane: li }; if (li === p.lanes.length) return { type: 'addLane' }; }
      return { type: 'header' };
    }
    if (y >= lanesTop() && Math.floor((y - laneTop(0)) / ROW.lane) === p.lanes.length) return { type: 'addLane' };
    const t = tOf(x), b = timeToBeat(t);
    if (y < ROW.ruler) { for (let i = 0; i < p.drops.length; i++) if (Math.abs(xOf(p.drops[i]) - x) < 6) return { type: 'drop', index: i }; return { type: 'ruler', t }; }
    if (y < ROW.ruler + ROW.sections) {
      for (let i = 1; i < p.sections.length; i++) if (Math.abs(xOf(p.sections[i].start) - x) < 5) return { type: 'section-edge', index: i };
      const i = p.sections.findIndex(s => t >= s.start && t < s.end); return i >= 0 ? { type: 'section', index: i } : { type: 'ruler', t };
    }
    if (y < waveTop() + ROW.wave) {
      let best = -1, bd = 6; for (let i = 0; i < p.beats.length; i++) { const d = Math.abs(xOf(p.beats[i]) - x); if (d < bd) { bd = d; best = i; } }
      if (best >= 0 && ($('#beatEdit').checked || y < waveTop() + 10)) return { type: 'beat', index: best };
      return { type: 'wave', t };
    }
    const li = Math.floor((y - laneTop(0)) / ROW.lane); if (li < 0 || li >= p.lanes.length) return null;
    for (const c of p.lanes[li].clips) {
      const x0 = xOf(beatToTime(c.start)), x1 = xOf(beatToTime(c.start + c.len)); if (x < x0 || x > x1) continue;
      if (c.target) { const [cy, ch] = clipBox(c, laneTop(li)); if (y < cy - 2 || y > cy + ch + 2) continue; }
      return { type: 'clip', lane: li, id: c.id, edge: x - x0 < 7 ? 'l' : x1 - x < 7 ? 'r' : null, b };
    }
    return { type: 'lane', lane: li, b };
  }
  const findClip = id => { for (const lane of P().lanes) { const c = lane.clips.find(c => c.id === id); if (c) return { clip: c, lane }; } return null; };

  // ---------------------------------------------------------------- selection
  // One clip is the primary selection (S.view.sel, what the inspector edits). A
  // group selection lives in S.view.multi: box-select, shift-click, or select all.
  // The group moves, copies, duplicates and deletes as one block, like a DAW.
  const isSelected = id => (S.view.sel && S.view.sel.type === 'clip' && S.view.sel.id === id) || S.view.multi.includes(id)
    || (S.view.drag && S.view.drag.type === 'marquee' && S.view.drag.hits.includes(id));
  const groupSize = () => S.view.multi.length;
  /** every selected clip with its layer, in layer order then time order */
  function selectedClips() {
    const ids = S.view.multi.length ? S.view.multi : (S.view.sel && S.view.sel.type === 'clip' ? [S.view.sel.id] : []);
    const out = []; P().lanes.forEach((lane, li) => lane.clips.forEach(c => { if (ids.includes(c.id)) out.push({ clip: c, lane, li }); }));
    return out.sort((a, b) => a.li - b.li || a.clip.start - b.clip.start);
  }
  function setSelection(ids) {
    ids = [...new Set(ids)].filter(id => findClip(id));
    if (ids.length > 1) { S.view.multi = ids; S.view.sel = { type: 'clip', id: ids[0] }; }
    else { S.view.multi = []; S.view.sel = ids.length ? { type: 'clip', id: ids[0] } : null; }
    inspector(); draw();
  }
  function clearSelection() { if (S.view.sel || S.view.multi.length) { S.view.sel = null; S.view.multi = []; inspector(); draw(); } }
  function selectAll(laneIdx = null) { const p = P(); setSelection(p.lanes.flatMap((l, i) => laneIdx === null || i === laneIdx ? l.clips.map(c => c.id) : [])); }
  /** clips whose box on screen touches the rectangle (x0,y0)-(x1,y1) */
  function clipsInRect(x0, y0, x1, y1) {
    const p = P(); const ids = [];
    p.lanes.forEach((lane, li) => {
      const ly = laneTop(li); if (ly + ROW.lane < y0 || ly > y1 || ly + ROW.lane <= lanesTop()) return;
      for (const c of lane.clips) { const cx0 = xOf(beatToTime(c.start)), cx1 = xOf(beatToTime(c.start + c.len)); if (cx1 >= x0 && cx0 <= x1 && cx1 > HEADER) ids.push(c.id); }
    });
    return ids;
  }
  /** does [start, start+len) collide with any clip in `lane` outside the moving group */
  const overlapsOutside = (lane, start, len, moving) => lane.clips.some(c => !moving.has(c) && start < c.start + c.len && c.start < start + len);
  /** slide the whole group by `delta` beats; refuses (returns false) if any clip would collide */
  function moveGroup(items, delta) {
    const moving = new Set(items.map(i => i.clip));
    if (items.some(i => i.start0 + delta < 0)) return false;
    if (items.some(i => overlapsOutside(i.lane, i.start0 + delta, i.clip.len, moving))) return false;
    for (const i of items) i.clip.start = i.start0 + delta;
    return true;
  }

  // ------------------------------------------------------------------ mouse
  tl.addEventListener('mousedown', e => {
    if (e.button !== 0) return;
    const r = tl.getBoundingClientRect(); const x = (e.clientX - r.left) / UI, y = (e.clientY - r.top) / UI; const h = hitTest(x, y); if (!h) return;
    const p = P(); S.view.mouse = { x, y };
    if (h.type === 'ruler' || h.type === 'wave') {
      if (h.type === 'wave' && $('#beatEdit').checked) { beginChange(); const t = h.t; const i = p.beats.findIndex(b => b > t); if (i < 0) p.beats.push(t); else p.beats.splice(i, 0, t); S.view.sel = { type: 'beat', index: i < 0 ? p.beats.length - 1 : i }; inspector(); changed(); return; }
      const st = snapTime(h.t, e); seek(st); S.view.sel = null; inspector(); S.view.drag = { type: 'scrub', last: st }; draw(); return;
    }
    if (h.type === 'beat') { S.view.sel = { type: 'beat', index: h.index }; inspector(); if ($('#beatEdit').checked) { beginChange(); S.view.drag = { type: 'beat', index: h.index }; } draw(); return; }
    if (h.type === 'drop') { S.view.sel = { type: 'drop', index: h.index }; inspector(); beginChange(); S.view.drag = { type: 'drop', index: h.index }; draw(); return; }
    if (h.type === 'section') { S.view.sel = { type: 'section', index: h.index }; inspector(); draw(); return; }
    if (h.type === 'section-edge') {
      beginChange();
      // a drop marker on this edge belongs to it: dragging the edge takes the marker along,
      // so a drop section and its blast can never drift apart
      const edge = p.sections[h.index].start;
      const mk = p.drops.findIndex(t => Math.abs(t - edge) < 0.35);
      S.view.drag = { type: 'section-edge', index: h.index, marker: mk >= 0 ? mk : null }; return;
    }
    if (h.type === 'clip') {
      const { clip } = findClip(h.id);
      if (e.shiftKey) {   // shift-click adds to or removes from the group
        const cur = selectedClips().map(i => i.clip.id);
        setSelection(cur.includes(h.id) ? cur.filter(id => id !== h.id) : cur.concat(h.id)); return;
      }
      if (S.view.multi.includes(h.id) && h.edge) {    // an edge of a member: every selected clip changes by the same amount
        beginChange();
        S.view.drag = { type: 'groupedge', edge: h.edge, ref: clip, b0: h.edge === 'r' ? clip.start + clip.len : clip.start, moved: false,
          items: selectedClips().map(i => ({ clip: i.clip, lane: i.lane, start0: i.clip.start, len0: i.clip.len })) };
        return;
      }
      if (S.view.multi.includes(h.id) && !h.edge) {   // drag a member: the whole group comes along
        beginChange();
        S.view.drag = { type: 'group', id: h.id, items: selectedClips().map(i => ({ clip: i.clip, lane: i.lane, start0: i.clip.start })), b0: h.b, delta: 0, moved: false };
        return;
      }
      S.view.multi = []; S.view.sel = { type: 'clip', id: h.id }; inspector(); beginChange(); S.view.drag = { type: 'clip', clip, lane: h.lane, edge: h.edge, offset: h.b - clip.start, start0: clip.start, len0: clip.len, moved: false, copy: e.altKey }; draw(); return;
    }
    if (h.type === 'lane') {   // empty space: click clears, dragging draws a selection box
      if (!e.shiftKey) clearSelection();
      S.view.drag = { type: 'marquee', x0: x, y0: y, x, y, add: e.shiftKey ? selectedClips().map(i => i.clip.id) : [], hits: [] }; return;
    }
    if (h.type === 'laneEye') { beginChange(); p.lanes[h.lane].hidden = !p.lanes[h.lane].hidden; changed(); }
    if (h.type === 'addLane') { beginChange(); p.lanes.push(newLane()); changed(); vscrollTo(lanesOverflow()); }
  });
  window.addEventListener('mousemove', e => {
    const r = tl.getBoundingClientRect(); const x = (e.clientX - r.left) / UI, y = (e.clientY - r.top) / UI; const d = S.view.drag;
    if (!d) { const h = hitTest(x, y); tl.style.cursor = h && h.type === 'clip' && h.edge ? 'ew-resize' : h && (h.type === 'section-edge' || (h.type === 'beat' && $('#beatEdit').checked)) ? 'col-resize' : h && (h.type === 'clip' || h.type === 'drop') ? 'grab' : h && (h.type === 'laneEye' || h.type === 'addLane') ? 'pointer' : 'default'; return; }
    const p = P(); const t = clamp(tOf(x), 0, duration()); const b = timeToBeat(t);
    if (d.type === 'scrub') { const st = snapTime(t, e); if (st !== d.last) { d.last = st; seek(st); } return; }
    if (d.type === 'marquee') {
      d.x = clamp(x, HEADER, TW()); d.y = clamp(y, lanesTop(), TH());
      // clips inside the box show as selected while it is still being drawn
      const hits = Math.abs(d.x - d.x0) < 4 && Math.abs(d.y - d.y0) < 4 ? [] : clipsInRect(Math.min(d.x0, d.x), Math.min(d.y0, d.y), Math.max(d.x0, d.x), Math.max(d.y0, d.y));
      if (hits.length !== d.hits.length || hits.some((id, i) => id !== d.hits[i])) { d.hits = hits; draw(); }
      return;
    }
    if (d.type === 'groupedge') {
      const delta = snapBeat(b) - d.b0;
      for (const it of d.items) {
        const c = it.clip, others = it.lane.clips.filter(k => k !== c);
        if (d.edge === 'r') {
          const nextStart = Math.min(Infinity, ...others.filter(k => k.start >= it.start0 + it.len0 - 0.001).map(k => k.start));
          c.len = Math.max(0.25, Math.min(it.len0 + delta, nextStart - it.start0));
        } else {
          const end = it.start0 + it.len0;
          const prevEnd = Math.max(0, ...others.filter(k => k.start + k.len <= it.start0 + 0.001).map(k => k.start + k.len));
          const ns = Math.min(end - 0.25, Math.max(prevEnd, it.start0 + delta));
          c.start = ns; c.len = end - ns;
        }
      }
      d.moved = d.moved || delta !== 0; draw(); return;
    }
    if (d.type === 'group') {
      const delta = snapBeat(d.items[0].start0 + (b - d.b0)) - d.items[0].start0;
      if (delta !== 0) d.tried = true;   // a real drag, even if the block is boxed in and cannot go there
      if (delta !== d.delta && moveGroup(d.items, delta)) { d.delta = delta; d.moved = true; draw(); }
      return;
    }
    if (d.type === 'beat') { p.beats[d.index] = t; p.beats.sort((a, b) => a - b); d.index = p.beats.indexOf(t); S.view.sel = { type: 'beat', index: d.index }; draw(); return; }
    if (d.type === 'drop') { p.drops[d.index] = beatToTime(snapBeat(b)); draw(); return; }
    if (d.type === 'section-edge') { const s = p.sections[d.index], prev = p.sections[d.index - 1]; const nt = beatToTime(snapBeat(b)); if (nt > prev.start + 0.5 && nt < s.end - 0.5) { s.start = nt; prev.end = nt; if (d.marker !== null && d.marker !== undefined) p.drops[d.marker] = nt; reviewRebuild(false); draw(); } return; }
    if (d.type === 'clip') {
      const c = d.clip; d.moved = true;
      const laneOf = p.lanes[d.lane];
      if (d.edge === 'l') { let ns = snapBeat(b); const ne = d.start0 + d.len0; if (laneOf) { const prevEnd = Math.max(0, ...laneOf.clips.filter(k => k !== c && k.start + k.len <= d.start0 + 0.001).map(k => k.start + k.len)); ns = Math.max(ns, prevEnd); } if (ns < ne - 0.25) { c.start = ns; c.len = ne - ns; } }
      else if (d.edge === 'r') { let ne = snapBeat(b); if (laneOf) { const nextStart = Math.min(Infinity, ...laneOf.clips.filter(k => k !== c && k.start >= d.start0 + d.len0 - 0.001).map(k => k.start)); ne = Math.min(ne, nextStart); } if (ne > d.start0 + 0.25) c.len = ne - d.start0; }
      else {
        let ns = Math.max(0, snapBeat(b - d.offset));
        let li = d.lane;
        if (d.type === 'clip') { const l2 = Math.floor((y - laneTop(0)) / ROW.lane); if (l2 >= 0 && l2 < p.lanes.length) li = l2; }
        if (d.type === 'clip') {
          const fit = fitInLane(p.lanes[li], ns, c.len, c);
          if (fit === null) { ns = c.start; li = d.lane; }   // no room: stay where it was
          else ns = fit;
          if (li !== d.lane) { p.lanes[d.lane].clips = p.lanes[d.lane].clips.filter(k => k !== c); p.lanes[li].clips.push(c); d.lane = li; }
        }
        c.start = ns;
      }
      draw();
    }
  });
  window.addEventListener('mouseup', () => { const d = S.view.drag; S.view.drag = null; if (!d) return; if (d.type === 'scrub') return;
    if (d.type === 'marquee') {
      if (Math.abs(d.x - d.x0) < 4 && Math.abs(d.y - d.y0) < 4) { draw(); return; }   // a plain click, already cleared
      setSelection(d.add.concat(d.hits)); return;
    }
    if (d.type === 'groupedge') {
      if (!d.moved) { S.undo.pop(); return; }
      inspector(); changed(); return;
    }
    if (d.type === 'group') {
      if (!d.moved) { S.undo.pop(); if (!d.tried) setSelection([d.id]); return; }   // a plain click on a member picks just that clip
      inspector(); changed(); return;
    } if (d.type === 'clip' && d.copy && d.moved) { /* alt-drag copies: restore original and add copy */ const orig = { ...d.clip, id: uid(), start: d.start0, len: d.len0, params: { ...d.clip.params } }; P().lanes[d.lane].clips.push(orig); } if (d.type === 'section-edge' && d.marker !== null) P().drops.sort((a, b) => a - b); if (d.type === 'clip') inspector(); changed(); });
  tl.addEventListener('dblclick', e => { const r = tl.getBoundingClientRect(); const h = hitTest((e.clientX - r.left) / UI, (e.clientY - r.top) / UI); if (!h) return; if (h.type === 'section') { beginChange(); const s = P().sections[h.index]; s.type = SECTION_TYPES[(SECTION_TYPES.indexOf(s.type) + 1) % SECTION_TYPES.length]; inspector(); changed(); } });
  tl.addEventListener('wheel', e => {
    e.preventDefault(); const r = tl.getBoundingClientRect(); const x = (e.clientX - r.left) / UI;
    // plain wheel zooms around the pointer; shift pans; ctrl/alt scrolls the layer stack
    if (e.ctrlKey || e.metaKey || e.altKey) return vscrollTo((S.view.vscroll || 0) + (e.deltaY || e.deltaX));
    if (e.shiftKey || Math.abs(e.deltaX) > Math.abs(e.deltaY)) return scrollTo(S.view.scroll + (e.shiftKey ? e.deltaY : e.deltaX) / S.view.pps * 0.8);
    zoomAt(wheelZoom(e), x);
  }, { passive: false });
  tl.addEventListener('contextmenu', e => { e.preventDefault(); const r = tl.getBoundingClientRect(); const x = (e.clientX - r.left) / UI, y = (e.clientY - r.top) / UI; const h = hitTest(x, y); if (!h) return;
    if (h.type === 'clip' && !isSelected(h.id)) setSelection([h.id]);
    contextMenu(e.clientX, e.clientY, h); });

  // ------------------------------------------------------------------ context menu
  function contextMenu(cx, cy, h) {
    const m = $('#ctxMenu'); m.innerHTML = ''; const p = P(); const b = h.b ?? timeToBeat(h.t ?? pos());
    const item = (label, fn) => { const d = document.createElement('div'); d.textContent = label; d.onclick = () => { m.hidden = true; fn(); }; m.appendChild(d); };
    const hr = () => m.appendChild(document.createElement('hr'));
    const nClip = n => `${n} clip${n === 1 ? '' : 's'}`;
    const pasteItem = laneIdx => { if (clipboard) item(`Paste ${nClip(clipboard.clips.length)} here`, () => pasteAt(laneIdx, snapBeat(b))); };
    if (h.type === 'clip' && groupSize() > 1) {
      const n = groupSize();
      item(`Copy ${nClip(n)}`, copySelection); item(`Cut ${nClip(n)}`, cutSelection); pasteItem(h.lane);
      item(`Duplicate ${nClip(n)} after`, duplicateSelection);
      hr(); item(`Delete ${nClip(n)}`, () => deleteSelection());
    } else if (h.type === 'clip') {
      const { clip, lane } = findClip(h.id); const li = p.lanes.indexOf(lane);
      item('Copy', copySelection); item('Cut', cutSelection); pasteItem(li); hr();
      item('Duplicate after', () => { beginChange(); lane.clips.push({ ...clip, id: uid(), start: clip.start + clip.len, params: { ...clip.params } }); changed(); });
      item('Split at playhead', () => { const pb = timeToBeat(pos()); if (pb <= clip.start || pb >= clip.start + clip.len) return; beginChange(); const right = { ...clip, id: uid(), start: pb, len: clip.start + clip.len - pb, params: { ...clip.params } }; clip.len = pb - clip.start; lane.clips.push(right); changed(); });
      item('Fill section', () => { const s = p.sections.find(s => beatToTime(clip.start) >= s.start - 0.01 && beatToTime(clip.start) < s.end); if (!s) return; const st = Math.round(timeToBeat(s.start)), ln = Math.round(timeToBeat(s.end)) - st; if (overlaps(lane, st, ln, clip)) return toast('Another clip on this layer is in the way', true); beginChange(); clip.start = st; clip.len = ln; changed(); });
      if (li < p.lanes.length - 1) item('Move up a layer', () => { if (overlaps(p.lanes[li + 1], clip.start, clip.len)) return toast('No room on the layer above', true); beginChange(); lane.clips = lane.clips.filter(c => c !== clip); p.lanes[li + 1].clips.push(clip); changed(); });
      if (li > 0) item('Move down a layer', () => { if (overlaps(p.lanes[li - 1], clip.start, clip.len)) return toast('No room on the layer below', true); beginChange(); lane.clips = lane.clips.filter(c => c !== clip); p.lanes[li - 1].clips.push(clip); changed(); });
      hr(); item('Delete', () => deleteSelection({ type: 'clip', id: h.id }));
    } else if (h.type === 'section' || h.type === 'section-edge') {
      const i = h.index; const s = p.sections[i];
      for (const ty of SECTION_TYPES) item((ty === s.type ? '● ' : '○ ') + ty, () => { beginChange(); s.type = ty; changed(); });
      hr();
      item('Split at playhead', () => { const t = pos(); if (t <= s.start + 0.5 || t >= s.end - 0.5) return; beginChange(); const nt = beatToTime(snapBeat(timeToBeat(t))); p.sections.splice(i + 1, 0, { start: nt, end: s.end, type: s.type }); s.end = nt; changed(); });
      if (i > 0) item('Merge with previous', () => { beginChange(); p.sections[i - 1].end = s.end; p.sections.splice(i, 1); changed(); });
      item('Add drop marker at start', () => { beginChange(); p.drops.push(s.start); p.drops.sort((a, b) => a - b); changed(); });
    } else if (h.type === 'drop') {
      item('Delete drop marker', () => { beginChange(); p.drops.splice(h.index, 1); changed(); });
    } else if (h.type === 'ruler' || h.type === 'wave') {
      item('Add drop marker here', () => { beginChange(); p.drops.push(beatToTime(snapBeat(b))); p.drops.sort((a, b) => a - b); changed(); });
      item('Split section here', () => { const t = beatToTime(snapBeat(b)); const i = p.sections.findIndex(s => t > s.start + 0.5 && t < s.end - 0.5); if (i < 0) return; beginChange(); const s = p.sections[i]; p.sections.splice(i + 1, 0, { start: t, end: s.end, type: s.type }); s.end = t; changed(); });
      item('Set downbeat to nearest beat', () => { beginChange(); const k = Math.round(b); p.phase = ((k % 4) + 4) % 4; changed(); });
    } else if (h.type === 'lane' || h.type === 'laneHeader') {
      if (h.type === 'lane') { pasteItem(h.lane); item('Select all on this layer', () => selectAll(h.lane)); item('Select all clips', () => selectAll()); hr(); }
      item('Insert layer above', () => { beginChange(); p.lanes.splice(h.lane + 1, 0, newLane()); changed(); });
      item('Insert layer below', () => { beginChange(); p.lanes.splice(h.lane, 0, newLane()); changed(); });
      if (h.lane < p.lanes.length - 1) item('Move layer up', () => { beginChange(); [p.lanes[h.lane], p.lanes[h.lane + 1]] = [p.lanes[h.lane + 1], p.lanes[h.lane]]; changed(); });
      if (h.lane > 0) item('Move layer down', () => { beginChange(); [p.lanes[h.lane], p.lanes[h.lane - 1]] = [p.lanes[h.lane - 1], p.lanes[h.lane]]; changed(); });
      hr();
      item('Clear this layer', () => { beginChange(); p.lanes[h.lane].clips = []; changed(); });
      if (p.lanes.length > 1) item('Remove this layer', () => { const n = p.lanes[h.lane].clips.length; if (n && !confirm(`Remove layer ${h.lane + 1} and its ${n} clip(s)?`)) return; beginChange(); p.lanes.splice(h.lane, 1); S.view.sel = null; inspector(); changed(); vscrollTo(S.view.vscroll || 0); });
    }
    if (!m.children.length) return;
    m.style.left = cx + 'px'; m.style.top = cy + 'px'; m.hidden = false;
    // keep the whole menu on screen: long ones near the bottom or right edge slide back in
    const r = m.getBoundingClientRect();
    if (r.bottom > innerHeight - 4) m.style.top = Math.max(4, innerHeight - r.height - 4) + 'px';
    if (r.right > innerWidth - 4) m.style.left = Math.max(4, innerWidth - r.width - 4) + 'px';
  }
  document.addEventListener('mousedown', e => { if (!e.target.closest('#ctxMenu')) $('#ctxMenu').hidden = true; });

  // ------------------------------------------------------------------ editing helpers
  // Clips never overlap inside a layer. A clip that would collide goes to the next
  // layer up that has room (new layers are created as needed), so effects stack.
  /** Clips for different lights (long strips, short lamps) never collide: they share a layer, one half each. */
  const sameLights = (a, b) => !a || !b || a === b;
  const overlaps = (lane, start, len, ignore, target) => lane.clips.some(c => c !== ignore && sameLights(c.target, target) && start < c.start + c.len && c.start < start + len);
  function freeLaneFor(start, len, from = 0, ignore = null, target = undefined) {
    const p = P();
    // a clip may ask for a layer several above the top one: add every layer up to it, not just one
    for (let i = Math.max(0, from); ; i++) { while (i >= p.lanes.length) p.lanes.push(newLane()); if (!overlaps(p.lanes[i], start, len, ignore, target)) return i; }
  }
  /** nearest start position in `lane` where [start, start+len) fits, or null if it cannot */
  function fitInLane(lane, start, len, ignore) {
    if (!overlaps(lane, start, len, ignore)) return start;
    const cands = [];
    for (const c of lane.clips) { if (c === ignore) continue; cands.push(c.start - len, c.start + c.len); }
    const ok = cands.filter(s0 => s0 >= 0 && !overlaps(lane, s0, len, ignore)).sort((a, b) => Math.abs(a - start) - Math.abs(b - start));
    return ok.length && Math.abs(ok[0] - start) <= 8 ? ok[0] : null;
  }
  /** the colors an effect asks for on a new clip: a preset name, a color list, or null for the song default */
  const effectColors = id => { const e = Effects.byId[id]; return e && (e.palette || (e.colors ? e.colors.slice() : null)) || null; };
  function addClip(laneIdx, effectId, startBeat, len) {
    const e = Effects.byId[effectId]; if (!e) return null; beginChange();
    len = len ?? (e.cat === 'accent' ? 2 : 16);
    const start = Math.max(0, startBeat);
    const li = freeLaneFor(start, len, laneIdx);
    const clip = { id: uid(), effect: effectId, start, len, params: Effects.defaults(effectId), opacity: 1, blend: 'normal', fadeIn: 0, fadeOut: 0, palette: effectColors(effectId) };
    P().lanes[li].clips.push(clip); S.view.sel = { type: 'clip', id: clip.id }; inspector(); changed();
    if (li !== laneIdx) toast(`Layer ${laneIdx + 1} was busy there, placed on layer ${li + 1}`);
    return clip;
  }
  function deleteSelection(sel = S.view.sel) {
    if (!sel) return; const p = P(); beginChange();
    if (sel.type === 'clip') { const ids = sel === S.view.sel && S.view.multi.length ? S.view.multi : [sel.id]; for (const lane of p.lanes) lane.clips = lane.clips.filter(c => !ids.includes(c.id)); S.view.multi = []; }
    if (sel.type === 'beat') p.beats.splice(sel.index, 1);
    if (sel.type === 'drop') p.drops.splice(sel.index, 1);
    if (sel.type === 'section' && p.sections.length > 1) { const i = sel.index; if (i > 0) { p.sections[i - 1].end = p.sections[i].end; } else { p.sections[1].start = p.sections[0].start; } p.sections.splice(i, 1); }
    S.view.sel = null; inspector(); changed();
  }
  // The clipboard is a block: each clip remembers its offset from the block's first
  // beat and its layer relative to the block's lowest layer, so a pasted section
  // lands with the same shape wherever it goes.
  let clipboard = null;
  function copySelection() {
    const items = selectedClips(); if (!items.length) return;
    const minStart = Math.min(...items.map(i => i.clip.start)), minLane = Math.min(...items.map(i => i.li));
    clipboard = { minLane, length: Math.max(...items.map(i => i.clip.start + i.clip.len)) - minStart,
      clips: items.map(i => ({ data: JSON.parse(JSON.stringify(i.clip)), rel: i.clip.start - minStart, lane: i.li - minLane })) };
    toast(items.length === 1 ? 'Copied' : `Copied ${items.length} clips`);
  }
  function cutSelection() { const n = selectedClips().length; if (!n) return; copySelection(); deleteSelection(); toast(n === 1 ? 'Cut' : `Cut ${n} clips`); }
  /** lay the clipboard block down starting at startBeat, its lowest layer on laneIdx */
  function pasteAt(laneIdx, startBeat) {
    if (!clipboard) return; beginChange(); const p = P(); const ids = []; let end = startBeat;
    for (const k of clipboard.clips) {
      const c = { ...k.data, id: uid(), start: Math.max(0, startBeat + k.rel), params: { ...k.data.params } };
      const li = freeLaneFor(c.start, c.len, Math.max(0, laneIdx + k.lane));
      p.lanes[li].clips.push(c); ids.push(c.id); end = Math.max(end, c.start + c.len);
    }
    setSelection(ids); changed();
    // the playhead moves to the end of what was pasted, so the next paste lands right after it
    // (not while playing: that would jump the music)
    if (!S.audio.playing && !(S.jam && S.jam.live)) {
      const t = clamp(beatToTime(end), 0, duration());
      if (S.cast.active) { S.cast.pos = t; S.cast.at = performance.now(); draw(); } else seek(t);
    }
  }
  /** the selection again, straight after itself: one clip after its own end, a block after the block */
  function duplicateSelection() {
    const items = selectedClips(); if (!items.length) return;
    const minStart = Math.min(...items.map(i => i.clip.start)), end = Math.max(...items.map(i => i.clip.start + i.clip.len)), minLane = Math.min(...items.map(i => i.li));
    beginChange(); const p = P(); const ids = [];
    for (const i of items) {
      const c = { ...i.clip, id: uid(), start: i.clip.start - minStart + end, params: { ...i.clip.params } };
      const li = freeLaneFor(c.start, c.len, i.li); p.lanes[li].clips.push(c); ids.push(c.id);
    }
    setSelection(ids); changed();
  }
  function regrid() { beginChange(); const p = P(); const bpm = parseFloat($('#bpm').value) || p.bpm; const period = 60 / bpm; const first = p.beats.length ? p.beats[0] % period : 0; p.bpm = bpm; const beats = []; for (let t = first; t < duration(); t += period) beats.push(+t.toFixed(4)); p.beats = beats; changed(); }
  function nudge(ms) { beginChange(); const p = P(); p.beats = p.beats.map(b => b + ms / 1000); changed(); }
  /** Halve (0.5) or double (2) the beat grid. Clips keep their position in *time*;
   *  a clip's beat-based rates now refer to the new, longer or shorter beat. */
  function scaleTempo(factor) {
    const p = P(); if (!p || p.beats.length < 4) return;
    beginChange();
    let shift = 0;
    if (factor === 0.5) {
      const keep = p.phase % 2;                        // keep the half of the grid the downbeats are on
      p.beats = p.beats.filter((_, i) => i % 2 === keep);
      p.phase = Math.floor(p.phase / 2) % 4;
      shift = keep;                                    // old beat `keep` is the new beat 0
    } else {
      const out = [];
      for (let i = 0; i < p.beats.length; i++) {
        const b = p.beats[i];
        const next = i + 1 < p.beats.length ? p.beats[i + 1] : b + (b - p.beats[i - 1] || 60 / p.bpm);
        out.push(b, (b + next) / 2);
      }
      p.beats = out; p.phase = (p.phase * 2) % 4;
    }
    p.bpm *= factor;
    // beat numbers shrink or grow with the grid; halving also drops the leading beats
    // before the new beat 0, so subtract that before scaling to keep clips at the same time
    const toNew = b => factor === 0.5 ? (b - shift) / 2 : b * 2;
    for (const lane of p.lanes) for (const c of lane.clips) {
      const end = toNew(c.start + c.len);
      c.start = Math.max(0, toNew(c.start));
      c.len = Math.max(0.25, end - c.start);
    }
    $('#bpm').value = p.bpm.toFixed(2);
    S.view.sel = null; inspector(); changed();
    toast(`Tempo ${factor === 0.5 ? 'halved' : 'doubled'}: ${p.bpm.toFixed(1)} bpm. Clips kept their place in the song.`);
  }

  // ------------------------------------------------------------------ drag & drop from the library
  function libraryDrag(el, kind, id) { el.draggable = true; el.addEventListener('dragstart', e => { e.dataTransfer.setData('text/plain', kind + ':' + id); e.dataTransfer.effectAllowed = 'copy'; }); }
  tl.addEventListener('dragover', e => { e.preventDefault(); e.dataTransfer.dropEffect = 'copy'; const r = tl.getBoundingClientRect(); const x = (e.clientX - r.left) / UI, y = (e.clientY - r.top) / UI; const h = hitTest(x, y); if (!h) { S.view.hoverDrop = null; return; } const b = snapBeat(h.b ?? timeToBeat(h.t ?? tOf(x))); if (h.type === 'clip') { const { clip } = findClip(h.id); const x0 = xOf(beatToTime(clip.start)), x1 = xOf(beatToTime(clip.start + clip.len)); S.view.hoverDrop = { x: x0, y: laneTop(h.lane) + 4, w: x1 - x0 - 2, h: ROW.lane - 8, replace: true }; }
    else if (h.type === 'lane') S.view.hoverDrop = { x: xOf(beatToTime(b)), y: laneTop(h.lane) + 4, w: 16 * 60 / P().bpm * S.view.pps, h: ROW.lane - 8 }; else S.view.hoverDrop = null; });
  tl.addEventListener('dragleave', () => { S.view.hoverDrop = null; });
  tl.addEventListener('drop', e => {
    e.preventDefault(); S.view.hoverDrop = null; const data = e.dataTransfer.getData('text/plain'); if (!data || !P()) { draw(); return; }
    const [kind, id] = data.split(':'); const r = tl.getBoundingClientRect(); const x = (e.clientX - r.left) / UI, y = (e.clientY - r.top) / UI; const h = hitTest(x, y); if (!h) return;
    const b = snapBeat(h.b ?? timeToBeat(h.t ?? tOf(x)));
    if (kind === 'effect' && h.type === 'clip') { const { clip } = findClip(h.id); if (Effects.byId[id]) { beginChange(); const wasDefault = JSON.stringify(clip.palette || null) === JSON.stringify(effectColors(clip.effect)); clip.effect = id; clip.params = Effects.defaults(id); if (wasDefault) clip.palette = effectColors(id); S.view.sel = { type: 'clip', id: clip.id }; inspector(); changed(); toast(`Replaced with ${Effects.byId[id].name}, same timing`); } }
    else if (kind === 'effect' && h.type === 'lane') addClip(h.lane, id, b);
    else if (kind === 'palette' && h.type === 'clip') { beginChange(); const { clip } = findClip(h.id); clip.palette = id; S.view.sel = { type: 'clip', id: h.id }; inspector(); changed(); toast(`${Effects.byId[clip.effect]?.name || clip.effect}: colors set to ${id}`); }
    else if (kind === 'palette') toast('Drop a palette onto a clip to recolor it', true);
    else if (kind === 'effect') addClip(0, id, b);
    draw();
  });
  // files dropped anywhere
  let dragDepth = 0;
  document.addEventListener('dragenter', e => { if ([...e.dataTransfer.types].includes('Files')) { dragDepth++; $('#dropOverlay').hidden = false; } });
  document.addEventListener('dragleave', e => { if ([...e.dataTransfer.types].includes('Files')) { dragDepth--; if (dragDepth <= 0) { dragDepth = 0; $('#dropOverlay').hidden = true; } } });
  document.addEventListener('dragover', e => { if ([...e.dataTransfer.types].includes('Files')) e.preventDefault(); });
  document.addEventListener('drop', e => { if ([...e.dataTransfer.types].includes('Files')) { e.preventDefault(); dragDepth = 0; $('#dropOverlay').hidden = true; const f = e.dataTransfer.files[0]; if (f) openFile(f); } });

  // ------------------------------------------------------------------ library panels
  function renderLibrary() {
    const list = $('#effectList'); list.innerHTML = ''; const q = ($('#effectSearch').value || '').toLowerCase();
    for (const [cat, label] of Object.entries(Effects.CATS)) {
      const items = Effects.list.filter(e => e.cat === cat && !e.hidden && (!q || e.name.toLowerCase().includes(q) || e.desc.toLowerCase().includes(q)));
      if (!items.length) continue; const h = document.createElement('div'); h.className = 'cat'; h.textContent = label; list.appendChild(h);
      for (const e of items) { const card = document.createElement('div'); card.className = 'effect-card'; card.title = e.desc + '\nDrag onto a layer, or double-click to add at the playhead.'; card.innerHTML = `<div class="sw" style="background:linear-gradient(90deg,${e.swatch[0]},${e.swatch[1]})"></div><div><div class="nm">${e.name}</div><div class="ds">${e.desc.slice(0, 44)}${e.desc.length > 44 ? '…' : ''}</div></div>`; libraryDrag(card, 'effect', e.id); card.ondblclick = () => { if (P()) addClip(e.cat === 'accent' ? P().lanes.length - 1 : 0, e.id, snapBeat(timeToBeat(pos()))); }; list.appendChild(card); }
    }
  }
  function renderPalettes() {
    const list = $('#paletteList'); if (!list) return; list.innerHTML = ''; const pals = P() ? P().palettes : Effects.PALETTES;
    for (const [name, cols] of Object.entries(pals)) { const card = document.createElement('div'); card.className = 'palette-card'; card.innerHTML = `<span class="nm">${name}</span><div class="bar" style="background:linear-gradient(90deg,${cols.join(',')})"></div>`; libraryDrag(card, 'palette', name); card.title = 'Drag onto a clip to give it these colors. Double-click to make it the default for clips without their own.'; card.ondblclick = () => { if (P()) { beginChange(); P().defaultPalette = name; changed(); toast(`Default palette: ${name}`); } }; list.appendChild(card); }
  }

  // ------------------------------------------------------------------ toolbar menus
  const SHORTCUTS = `<h3>Shortcuts</h3><div class="help">
      <kbd>Space</kbd> play / pause &nbsp; <kbd>Home</kbd> start<br>Wheel zooms · <kbd>Shift</kbd>+wheel pans · <kbd>Ctrl</kbd>+wheel scrolls layers<br>Drag the strip on top to scroll, its edges to zoom<br>
      Click waveform to seek, drag the ruler to scrub (snaps like clips; hold <kbd>Alt</kbd> to go anywhere)<br>Drag effects onto layers; drag edges to resize<br>Drag across empty space to box-select clips · <kbd>Shift</kbd>+click adds · <kbd>Ctrl</kbd>+<kbd>A</kbd> selects all<br>Drag any selected clip to move the whole block<br>Drop an effect on a clip to swap the effect, same timing<br>Click a clip to pick its colors in the inspector<br>Click a layer's eye to hide it · right-click its name to insert, remove or reorder<br><kbd>Alt</kbd>+drag copies a clip<br>
      <kbd>Del</kbd> delete · <kbd>Ctrl</kbd>+<kbd>C</kbd>/<kbd>X</kbd>/<kbd>V</kbd>/<kbd>D</kbd> copy / cut / paste / duplicate<br><kbd>Ctrl</kbd>+<kbd>Z</kbd>/<kbd>Y</kbd> undo / redo · <kbd>Ctrl</kbd>+<kbd>S</kbd> save<br>
      Double-click a section to cycle its type<br>Right-click anything for more<br>
      <b>Beat edit</b> on: click adds a beat, <kbd>Space</kbd> taps while playing, <kbd>Enter</kbd> plays<br>
      Review, a song Claude suggested sections for: <kbd>M</kbd> flips between Claude's and the old ones, <kbd>]</kbd> Done keeps what is showing</div>`;
  function toggleMenu(btn, menu, fill) {
    const open = menu.hidden; closeMenus(); if (!open) return;
    if (fill) menu.innerHTML = fill;
    menu.hidden = false; btn.classList.add('on');
    if (menu.classList.contains('popover')) { const r = btn.getBoundingClientRect(); menu.style.top = (r.bottom + 6) + 'px'; menu.style.right = Math.max(8, innerWidth - r.right) + 'px'; }
  }
  function closeMenus() { for (const [b, m] of [['#btnGridMenu', '#gridMenu'], ['#btnSettings', '#settingsMenu'], ['#btnHelp', '#helpPop'], ['#btnSettings', '#roomsPop']]) { $(m).hidden = true; $(b).classList.remove('on'); } }
  $('#btnGridMenu').onclick = e => { e.stopPropagation(); toggleMenu($('#btnGridMenu'), $('#gridMenu')); };
  $('#btnSettings').onclick = e => { e.stopPropagation(); toggleMenu($('#btnSettings'), $('#settingsMenu')); };
  $('#btnHelp').onclick = e => { e.stopPropagation(); toggleMenu($('#btnHelp'), $('#helpPop'), SHORTCUTS); };
  for (const id of ['#gridMenu', '#settingsMenu', '#helpPop', '#roomsPop']) $(id).addEventListener('mousedown', e => e.stopPropagation());   // clicks inside keep it open (Tap!)
  $('#btnRooms').onclick = e => { e.stopPropagation(); openRooms(); };
  document.addEventListener('mousedown', closeMenus);
  document.addEventListener('keydown', e => { if (e.key === 'Escape') closeMenus(); });
  // narrow windows: the library is a drawer
  $('#btnLib').onclick = e => { e.stopPropagation(); document.body.classList.toggle('lib-open'); };
  document.addEventListener('mousedown', e => { if (document.body.classList.contains('lib-open') && !e.target.closest('.library') && !e.target.closest('#btnLib')) document.body.classList.remove('lib-open'); });

  // ------------------------------------------------------------------ inspector
  function inspector() {
    const el = $('#inspector'); const sel = S.view.sel; const p = P();
    if (!p) { el.innerHTML = ''; return; }
    const help = '';                                  // the shortcuts live in the ? popover now
    if (!sel) {
      const g = S.analysis && S.analysis.genre; const auto = g && STYLE_NAMES[g.style] ? g.style : null;
      const heard = g ? [...(g.genres || []), ...((g.spotify || []).length ? ['Spotify: ' + g.spotify.slice(0, 2).join(', ')] : [])].join(', ') : '';
      const styleRow = `<div class="row"><label title="How hard the lights go when you auto-build. Chill never strobes; groove moves but lifts drops without flashing; dance flashes on the beat; rave goes all out.">Show style</label><select id="songStyle">
        <option value="">Auto${auto ? ': ' + STYLE_NAMES[auto] : ''}</option>${Object.entries(STYLE_NAMES).map(([k, n]) => `<option value="${k}" ${p.settings && p.settings.style === k ? 'selected' : ''}>${n}</option>`).join('')}</select></div>
        ${heard ? `<div class="row heard"><label>Sounds like</label><span class="genre-heard">${esc(heard)}</span></div>` : ''}`;
      el.innerHTML = `<div class="panel"><h3>Song</h3>${styleRow}<div class="row"><label title="Auto-build builds the show around this color, the way a jam song is built around its album cover">Main color</label><span class="main-color"><input id="mainColor" type="color" value="${p.settings.mainColor || '#ff0000'}"><button id="mainColorRebuild" class="small" title="Auto-build the show with this color now">Rebuild</button>${p.settings.mainColor ? '<button id="mainColorAuto" class="small" title="Back to the automatic palettes">Auto</button>' : ''}</span></div><div class="row"><label>Default palette</label><select id="defPal">${Object.keys(p.palettes).map(n => `<option ${n === p.defaultPalette ? 'selected' : ''}>${n}</option>`).join('')}</select></div><div class="row"><label title="When the music goes quiet, every light dims with it, even in a drop">Dim with the music</label><input id="followLoud" type="checkbox" ${p.settings.followLoudness === false ? '' : 'checked'}></div>${S.analysis && S.analysis.structure_source === 'loudness' ? `<div class="hint warn">Section timing was not refined: ${esc(S.analysis.structure_error || 'SongFormer did not run')}. Boundaries may be a bar or two off. Press Re-detect to try again.</div>` : ''}</div>` + help; $('#mainColor').onchange = e => { beginChange(); p.settings.mainColor = Effects.solid(e.target.value); changed(); inspector(); toast('Main color set. Press Rebuild (or Auto-build show) to build the show around it.'); };
      $('#mainColorRebuild').onclick = () => { if (!p.settings.mainColor) p.settings.mainColor = Effects.solid($('#mainColor').value); autoBuild(); inspector(); };
      if ($('#mainColorAuto')) $('#mainColorAuto').onclick = () => { beginChange(); delete p.settings.mainColor; changed(); inspector(); toast('Back to the automatic palettes. Press Auto-build show to rebuild.'); };
      $('#defPal').onchange = e => { beginChange(); p.defaultPalette = e.target.value; changed(); };
      $('#followLoud').onchange = e => { beginChange(); p.settings.followLoudness = e.target.checked; changed(); pushFrameIfIdle(); };
      $('#songStyle').onchange = e => { beginChange(); p.settings = p.settings || {}; p.settings.style = e.target.value; changed(); inspector();
        toast(`Show style: ${e.target.value ? STYLE_NAMES[e.target.value] : 'Auto (' + STYLE_NAMES[showStyle()] + ')'}. Press Auto-build show to rebuild in it.`); };
      return; }
    if (sel.type === 'clip' && groupSize() > 1) {
      const items = selectedClips(); const n = items.length;
      const lanes = new Set(items.map(i => i.li)); const b0 = Math.min(...items.map(i => i.clip.start)), b1 = Math.max(...items.map(i => i.clip.start + i.clip.len));
      const op = items.every(i => (i.clip.opacity ?? 1) === (items[0].clip.opacity ?? 1)) ? (items[0].clip.opacity ?? 1) : null;
      const single = items.every(i => Array.isArray(i.clip.palette) && i.clip.palette.length === 1) ? items[0].clip.palette[0] : null;
      el.innerHTML = `<div class="panel"><div class="title">${n} clips selected</div><div class="desc">Across ${lanes.size} layer${lanes.size === 1 ? '' : 's'}, bar ${barLabel(b0)} to bar ${barLabel(b1)}.</div>
        <div class="row"><label>Opacity</label><input id="gOp" type="range" min="0" max="1" step="0.01" value="${op ?? 1}"><span class="val">${op === null ? 'mixed' : Math.round(op * 100) + '%'}</span></div>
        <div class="btns"><button id="gCopy" class="small">Copy</button><button id="gCut" class="small">Cut</button><button id="gDup" class="small">Duplicate</button><button id="gDel" class="small">Delete</button></div></div>
        <div class="panel"><h3>Colors</h3><div class="pal-grid" id="gPalGrid">
          <div class="pal-opt" data-pal=""><div class="bar" style="background:linear-gradient(90deg,${(p.palettes[p.defaultPalette] || ['#888']).join(',')})"></div><div class="nm">Default (${p.defaultPalette})</div></div>
          ${Object.entries(p.palettes).map(([nm, cols]) => `<div class="pal-opt" data-pal="${nm}"><div class="bar" style="background:linear-gradient(90deg,${cols.join(',')})"></div><div class="nm">${nm}</div></div>`).join('')}
          <div class="pal-opt ${single ? 'sel' : ''}" data-pal="__single"><div class="bar" style="background:${single || clipPaletteHex(items[0].clip)[0]}"></div><div class="nm">Single color…</div></div>
        </div>${single ? `<div class="color-row big-swatches"><input type="color" id="gSingle" value="${single}" title="One color for every selected clip"></div>` : ''}</div>` + help;
      const gs = $('#gSingle'); if (gs) { gs.oninput = () => { for (const i of items) i.clip.palette = [gs.value]; scheduleSave(); draw(); }; gs.onchange = () => beginChange(); }
      $('#gOp').oninput = ev => { const v = parseFloat(ev.target.value); for (const i of items) i.clip.opacity = v; ev.target.nextElementSibling.textContent = Math.round(v * 100) + '%'; scheduleSave(); draw(); };
      $('#gOp').onchange = () => beginChange();
      $('#gCopy').onclick = copySelection; $('#gCut').onclick = cutSelection; $('#gDup').onclick = duplicateSelection; $('#gDel').onclick = () => deleteSelection();
      el.querySelectorAll('#gPalGrid .pal-opt').forEach(opt => opt.onclick = () => { beginChange(); const v = opt.dataset.pal; const one = clipPaletteHex(items[0].clip)[0]; for (const i of items) i.clip.palette = v === '__single' ? [one] : v === '' ? null : v; inspector(); changed(); });
      return;
    }
    if (sel.type === 'clip') {
      const f = findClip(sel.id); if (!f) { S.view.sel = null; return inspector(); } const { clip, lane } = f; const e = Effects.byId[clip.effect];
      let html = `<div class="panel"><div class="title">${e.name}</div><div class="desc">${e.desc}</div>
        <div class="row"><label>Layer</label><select id="cLane">${p.lanes.map((l, i) => `<option value="${i}" ${l === lane ? 'selected' : ''}>Layer ${i + 1}</option>`).join('')}</select></div>
        <div class="row"><label>Start</label><span>bar ${barLabel(clip.start)} · ${fmtTime(beatToTime(clip.start))}</span></div>
        <div class="row"><label>Length (beats)</label><input id="cLen" type="number" step="0.25" min="0.25" value="${clip.len}"></div>
        <div class="row"><label>Opacity</label><input id="cOp" type="range" min="0" max="1" step="0.01" value="${clip.opacity ?? 1}"><span class="val">${Math.round((clip.opacity ?? 1) * 100)}%</span></div>
        <div class="row"><label title="Dips this layer on every kick and snare, so what is on top of it punches through">Duck under drums</label><input id="cDuck" type="range" min="0" max="1" step="0.05" value="${clip.duck || 0}"><span class="val">${Math.round((clip.duck || 0) * 100)}%</span></div>
        <div class="row"><label title="Brightens as the bass line swells and dims as it drops out (with the bass separated out, it follows a wobble)">Follow the bass</label><input id="cBass" type="range" min="0" max="1" step="0.05" value="${clip.bassPump || 0}"><span class="val">${Math.round((clip.bassPump || 0) * 100)}%</span></div>
        <div class="row"><label title="Which lights play this clip: long strips carry motion, short lamps the drums">Lights</label><select id="cTarget"><option value="">All</option><option value="long" ${clip.target === 'long' ? 'selected' : ''}>Long strips</option><option value="short" ${clip.target === 'short' ? 'selected' : ''}>Short lamps</option></select></div>
        <div class="row"><label>Blend</label><select id="cBlend">${Object.entries(Effects.BLENDS).map(([k, v]) => `<option value="${k}" ${(clip.blend || 'normal') === k ? 'selected' : ''}>${v}</option>`).join('')}</select></div>
        <div class="row"><label>Fade in (beats)</label><input id="cFi" type="number" step="0.5" min="0" value="${clip.fadeIn || 0}"></div>
        <div class="row"><label>Fade out (beats)</label><input id="cFo" type="number" step="0.5" min="0" value="${clip.fadeOut || 0}"></div></div>
        <div class="panel"><h3>Colors</h3>
        <div class="big-bar" style="background:linear-gradient(90deg,${clipPaletteHex(clip).join(',')})"></div>
        <div class="pal-grid" id="cPalGrid">
          <div class="pal-opt ${!clip.palette ? 'sel' : ''}" data-pal=""><div class="bar" style="background:linear-gradient(90deg,${(p.palettes[p.defaultPalette] || ['#888']).join(',')})"></div><div class="nm">Default (${p.defaultPalette})</div></div>
          ${Object.entries(p.palettes).map(([n, cols]) => `<div class="pal-opt ${clip.palette === n ? 'sel' : ''}" data-pal="${n}"><div class="bar" style="background:linear-gradient(90deg,${cols.join(',')})"></div><div class="nm">${n}</div></div>`).join('')}
          <div class="pal-opt ${Array.isArray(clip.palette) && clip.palette.length === 1 ? 'sel' : ''}" data-pal="__single"><div class="bar" style="background:${clipPaletteHex(clip)[0]}"></div><div class="nm">Single color…</div></div>
          <div class="pal-opt ${Array.isArray(clip.palette) && clip.palette.length > 1 ? 'sel' : ''}" data-pal="__custom"><div class="bar" style="background:${Array.isArray(clip.palette) && clip.palette.length > 1 ? `linear-gradient(90deg,${clip.palette.join(',')})` : 'repeating-linear-gradient(90deg,#444 0 6px,#666 6px 12px)'}"></div><div class="nm">Custom…</div></div>
        </div>
        ${Array.isArray(clip.palette) ? `<div class="color-row big-swatches" id="cCols">${clip.palette.map((col, i) => `<input type="color" value="${col}" data-i="${i}" title="Click to change">`).join('')}<button id="cColAdd" class="small">+</button><button id="cColRem" class="small">−</button></div>` : ''}
        </div>`;
      html += `<div class="panel"><h3>Parameters</h3>`;
      for (const [k, d] of Object.entries(e.params)) {
        const v = clip.params[k] ?? d.def;
        // a value off the usual list (a drop's motion sped up to 1.5 beats) is offered too, so it shows as it is
        if (d.type === 'beats') html += `<div class="row"><label>${d.label}</label><select data-p="${k}">${[...new Set([...Effects.BEAT_OPTS, +v])].sort((x, y) => x - y).map(o => `<option value="${o}" ${o == v ? 'selected' : ''}>${+o.toFixed(2)}</option>`).join('')}</select></div>`;
        else if (d.type === 'range') html += `<div class="row"><label>${d.label}</label><input data-p="${k}" type="range" min="${d.min}" max="${d.max}" step="${d.step}" value="${v}"><span class="val">${(+v).toFixed(d.step >= 1 ? 0 : 2)}</span></div>`;
        else if (d.type === 'select') html += `<div class="row"><label>${d.label}</label><select data-p="${k}">${Object.entries(d.options).map(([o, l]) => `<option value="${o}" ${o === v ? 'selected' : ''}>${l}</option>`).join('')}</select></div>`;
        else if (d.type === 'bool') html += `<div class="row"><label>${d.label}</label><input data-p="${k}" type="checkbox" ${v ? 'checked' : ''}></div>`;
      }
      if (!Object.keys(e.params).length) html += '<div class="hint">No parameters.</div>';
      html += `</div><div class="panel"><div class="btns"><button id="cDup" class="small">Duplicate</button><button id="cFill" class="small">Fill section</button><button id="cDel" class="small">Delete</button></div></div>`;
      el.innerHTML = html;
      const upd = () => { scheduleSave(); draw(); };
      $('#cLane').onchange = ev => { const target = p.lanes[+ev.target.value]; if (overlaps(target, clip.start, clip.len)) { toast('No room on that layer at this position', true); ev.target.value = String(p.lanes.indexOf(lane)); return; } beginChange(); lane.clips = lane.clips.filter(c => c !== clip); target.clips.push(clip); inspector(); upd(); };
      $('#cLen').onchange = ev => { beginChange(); let L = Math.max(0.25, parseFloat(ev.target.value) || 1); const nextStart = Math.min(Infinity, ...lane.clips.filter(k => k !== clip && k.start >= clip.start + clip.len - 0.001).map(k => k.start)); if (clip.start + L > nextStart) { L = nextStart - clip.start; toast('Shortened to the next clip on this layer'); } clip.len = L; ev.target.value = L; upd(); };
      $('#cOp').oninput = ev => { clip.opacity = parseFloat(ev.target.value); ev.target.nextElementSibling.textContent = Math.round(clip.opacity * 100) + '%'; upd(); };
      $('#cOp').onchange = () => beginChange();
      $('#cBlend').onchange = ev => { beginChange(); clip.blend = ev.target.value; upd(); };
      $('#cTarget').onchange = ev => { beginChange(); if (ev.target.value) clip.target = ev.target.value; else delete clip.target; upd(); };
      $('#cDuck').oninput = ev => { clip.duck = parseFloat(ev.target.value); ev.target.nextElementSibling.textContent = Math.round(clip.duck * 100) + '%'; upd(); };
      $('#cDuck').onchange = () => beginChange();
      $('#cBass').oninput = ev => { clip.bassPump = parseFloat(ev.target.value); ev.target.nextElementSibling.textContent = Math.round(clip.bassPump * 100) + '%'; upd(); };
      $('#cBass').onchange = () => beginChange();
      $('#cFi').onchange = ev => { beginChange(); clip.fadeIn = parseFloat(ev.target.value) || 0; upd(); };
      $('#cFo').onchange = ev => { beginChange(); clip.fadeOut = parseFloat(ev.target.value) || 0; upd(); };
      el.querySelectorAll('#cPalGrid .pal-opt').forEach(opt => opt.onclick = () => {
        beginChange(); const v = opt.dataset.pal; const cur = clipPaletteHex(clip);
        if (v === '__single') clip.palette = [cur[0]];
        else if (v === '__custom') clip.palette = Array.isArray(clip.palette) && clip.palette.length > 1 ? clip.palette : cur.length > 1 ? cur.slice() : [cur[0], '#ffffff'];
        else clip.palette = v === '' ? null : v;
        inspector(); upd();
      });
      el.querySelectorAll('#cCols input[type=color]').forEach(inp => { inp.oninput = () => { clip.palette[+inp.dataset.i] = inp.value; upd(); }; inp.onchange = () => beginChange(); });
      const colAdd = $('#cColAdd'), colRem = $('#cColRem');
      if (colAdd) colAdd.onclick = () => { beginChange(); clip.palette.push('#ffffff'); inspector(); upd(); };
      if (colRem) colRem.onclick = () => { if (clip.palette.length > 1) { beginChange(); clip.palette.pop(); inspector(); upd(); } };
      el.querySelectorAll('[data-p]').forEach(inp => {
        const k = inp.dataset.p, d = e.params[k];
        const read = () => d.type === 'bool' ? inp.checked : d.type === 'select' ? inp.value : parseFloat(inp.value);
        inp.oninput = () => { clip.params[k] = read(); if (d.type === 'range') inp.nextElementSibling.textContent = (+inp.value).toFixed(d.step >= 1 ? 0 : 2); upd(); };
        inp.onchange = () => { beginChange(); clip.params[k] = read(); upd(); };
      });
      $('#cDup').onclick = duplicateSelection; $('#cDel').onclick = () => deleteSelection();
      $('#cFill').onclick = () => { const s = p.sections.find(s => beatToTime(clip.start) >= s.start - 0.01 && beatToTime(clip.start) < s.end); if (!s) return; const st = Math.round(timeToBeat(s.start)), ln = Math.round(timeToBeat(s.end)) - st; if (overlaps(lane, st, ln, clip)) return toast('Another clip on this layer is in the way', true); beginChange(); clip.start = st; clip.len = ln; inspector(); upd(); };
      return;
    }
    if (sel.type === 'section') {
      const s = p.sections[sel.index]; if (!s) { S.view.sel = null; return inspector(); }
      el.innerHTML = `<div class="panel"><div class="title">Section ${sel.index + 1}</div><div class="row"><label>Type</label><select id="secType">${SECTION_TYPES.map(t => `<option ${t === s.type ? 'selected' : ''}>${t}</option>`).join('')}</select></div>
        <div class="row"><label>Start</label><span>${fmtTime(s.start)} · bar ${barLabel(timeToBeat(s.start))}</span></div><div class="row"><label>End</label><span>${fmtTime(s.end)}</span></div><div class="row"><label>Length</label><span>${(timeToBeat(s.end) - timeToBeat(s.start)).toFixed(1)} beats</span></div>
        <div class="btns"><button id="secSplit" class="small">Split at playhead</button><button id="secBuild" class="small">Auto-build this section</button><button id="secDel" class="small">Merge away</button></div></div>`;
      $('#secType').onchange = ev => { beginChange(); s.type = ev.target.value; changed(); };
      $('#secSplit').onclick = () => { const t = pos(); if (t <= s.start + 0.5 || t >= s.end - 0.5) return toast('Move the playhead inside the section first'); beginChange(); const nt = beatToTime(snapBeat(timeToBeat(t))); p.sections.splice(sel.index + 1, 0, { start: nt, end: s.end, type: s.type }); s.end = nt; inspector(); changed(); };
      $('#secBuild').onclick = () => { beginChange(); buildSection(s, sel.index, true); changed(); };
      $('#secDel').onclick = () => deleteSelection();
      return;
    }
    if (sel.type === 'beat') { el.innerHTML = `<div class="panel"><div class="title">Beat ${sel.index + 1}</div><div class="row"><label>Time</label><span>${fmtTime(p.beats[sel.index])}</span></div><div class="btns"><button id="bDel" class="small">Delete beat</button></div></div>`; $('#bDel').onclick = () => deleteSelection(); return; }
    if (sel.type === 'drop') { el.innerHTML = `<div class="panel"><div class="title">Drop marker</div><div class="row"><label>Time</label><span>${fmtTime(p.drops[sel.index])}</span></div><div class="btns"><button id="dDel" class="small">Delete marker</button></div></div>`; $('#dDel').onclick = () => deleteSelection(); return; }
  }

  // ------------------------------------------------------------------ auto-build
  /** the song's palette, rotated so each section reads differently while staying in family */
  function paletteVariant(name, n) {
    const cols = (P().palettes[name] || ['#ffffff']).slice();
    const k = ((n % cols.length) + cols.length) % cols.length;
    return cols.slice(k).concat(cols.slice(0, k));
  }
  /** The material shows are made from: a book of moves for each scene, a kind of music as a lighting
   *  designer would light it (pop punk, house, R&B, metal...). A song is not given one book: it draws its
   *  own mix (its beds, what moves over its verses, its motions, its drop moves, its buildups) from the
   *  scenes its genres belong to, each as likely as the genre model heard it (see sceneWeights), so two
   *  songs of one scene still differ. How hard any of it goes is the show style's (chill | groove | dance
   *  | rave), section by section (sectionStyle): the scene says what it looks like, the style how much.
   *  Each drop recipe is [base effect, its settings] and what goes over it: 'motion' (the song's motion),
   *  or [effect, settings, clip settings]. `flash` marks one that flashes and `fast` one that races (dance
   *  and rave only), `rave` one for rave alone, `room` one that needs two lights or more. `calm` scenes
   *  light a chill song; `dark` ones bring thunder and heartbeats. Builds: see buildSection's buildup. */
  const SCENES = {
    indiepop: { name: 'Indie pop', calm: true, near: ['dreamy', 'synthpop'],
      intro: ['twinkle', { density: 0.2, speed: 4, base: 0.12 }], verse: [['colorwash', { speed: 8, intensity: 0.45 }], ['wave', { speed: 4, waves: 1, floor: 0.1 }], ['roomwave', { speed: 8, floor: 0.1 }]],
      verseTop: [['bounce', { speed: 2, tail: 0.3 }], ['ripple', { every: 2, travel: 2, width: 0.12 }], ['pulsar', { every: 1, count: 1, size: 0.08, decay: 0.6 }, { opacity: 0.8 }]],
      breakdown: ['aurora', { speed: 16, intensity: 0.4, depth: 0.7 }], outro: ['colorwash', { speed: 16, intensity: 0.35 }], motion: ['bounce', 'ripples', 'sparkles', 'steps'], builds: ['room', 'climb', 'scan'],
      drops: [{ base: ['alternate', { every: 1, size: 2, intensity: 0.8 }], top: 'motion', lift: true }, { base: ['wave', { speed: 2, waves: 2, floor: 0.15 }], top: 'motion' },
              { base: ['halves', { follow: 'drums', every: 1, dim: 0.2 }], top: ['ripple', { every: 1, travel: 1.5, width: 0.12 }, { opacity: 0.8 }], lift: true },
              { base: ['roomchase', { every: 1, floor: 0.2 }], top: 'motion', room: true, lift: true }] },
    synthpop: { name: 'Synth-pop', near: ['dancepop', 'indiepop'],
      intro: ['gradient', { speed: 16, intensity: 0.4 }], verse: [['gradient', { speed: 8, intensity: 0.45 }], ['wave', { speed: 4, waves: 1.5, floor: 0 }]],
      verseTop: [['scanner', { speed: 2, size: 0.12 }, { opacity: 0.75 }], ['steps', { zones: 8, every: 1, decay: 0.8, order: 'run' }, { opacity: 0.7 }], ['chase', { speed: 2, size: 0.1, gap: 0.7 }, { opacity: 0.7 }]],
      breakdown: ['wave', { speed: 16, waves: 1, floor: 0.1 }], outro: ['gradient', { speed: 16, intensity: 0.35 }], motion: ['scanner', 'steps', 'comets', 'ripples'], builds: ['scan', 'room', 'climb'],
      drops: [{ base: ['gradient', { speed: 4, stretch: 0.67, intensity: 0.8 }], top: 'motion' }, { base: ['blocks', { every: 1, size: 3, density: 0.5 }], top: ['scanner', { speed: 1, size: 0.15 }, { opacity: 0.85 }] },
              { base: ['chase', { speed: 1, size: 0.12, gap: 0.6 }], top: ['flash', { every: 2, decay: 0.3 }, { opacity: 0.45, blend: 'add' }], flash: true },
              { base: ['roomsplit', { every: 1, pump: 0.35 }], top: 'motion', room: true }] },
    altrock: { name: 'Alternative rock', near: ['poprock', 'indiepop'],
      intro: ['breathe', { speed: 4, floor: 0.05 }], verse: [['gradient', { speed: 16, intensity: 0.4 }], ['breathe', { speed: 2, floor: 0.1 }]],
      verseTop: [['scanner', { speed: 2, size: 0.12 }, { opacity: 0.75 }], ['bounce', { speed: 2, tail: 0.3 }], ['meteors', { every: 2, chance: 0.5, travel: 3, tail: 0.3 }]],
      breakdown: ['aurora', { speed: 16, depth: 0.8, intensity: 0.35 }], outro: ['ember', { speed: 2, intensity: 0.5 }], motion: ['scanner', 'comets', 'bounce', 'meteors'], builds: ['room', 'dark', 'scan'],
      drops: [
              { base: ['roomsplit', { every: 2, pump: 0.35 }], top: ['scanner', { speed: 1, size: 0.15 }, { opacity: 0.8 }], room: true },
              { base: ['battle', { speed: 4, surge: 0.25 }], top: ['bounce', { speed: 1, tail: 0.3 }, { opacity: 0.9 }] },
              { base: ['halves', { follow: 'drums', every: 1, dim: 0 }], top: 'motion' }, { base: ['blocks', { every: 1, size: 3, density: 0.5 }], top: 'motion' },
              { base: ['scanner', { speed: 1, size: 0.2, floor: 0.15 }], top: ['flash', { every: 2, decay: 0.3 }, { opacity: 0.45, blend: 'add' }], flash: true },
              { base: ['meter', { direction: 'out', low: 0.45, gain: 1.1, floor: 0.12, color: 'cycle' }], top: 'motion', fast: true }] },   // (a VU meter from the center: in groove it flickers)
    dancepop: { name: 'Dance-pop', near: ['synthpop', 'house'],
      intro: ['gradient', { speed: 16, intensity: 0.4 }], verse: [['colorwash', { speed: 8, intensity: 0.45 }], ['alternate', { every: 2, size: 2, intensity: 0.6 }]],
      verseTop: [['ripple', { every: 2, travel: 2, width: 0.12 }], ['steps', { zones: 8, every: 1, decay: 1, order: 'bounce' }, { opacity: 0.7 }], ['pulsar', { every: 1, count: 2, size: 0.08, decay: 0.5 }, { opacity: 0.8 }]],
      breakdown: ['breathe', { speed: 8, floor: 0.1 }], outro: ['gradient', { speed: 16, intensity: 0.35 }], motion: ['ripples', 'sparkles', 'steps', 'orbit'], builds: ['room', 'climb', 'poles'],
      drops: [{ base: ['halves', { follow: 'drums', every: 1, dim: 0.1 }], top: 'motion' }, { base: ['hitswap', { follow: 'hits', punch: 0.3 }], top: 'motion' },
              { base: ['roomsplit', { every: 1, pump: 0.4 }], top: ['flash', { every: 1, decay: 0.3 }, { opacity: 0.45, blend: 'add' }], flash: true, room: true },
              { base: ['gradient', { speed: 4, stretch: 0.67, intensity: 0.8 }], top: 'motion' }] },
    poppunk: { name: 'Pop punk', near: ['altrock', 'poprock'],
      intro: ['breathe', { speed: 2, floor: 0.05 }], verse: [['halves', { follow: 'drums', every: 1, dim: 0.3 }], ['gradient', { speed: 8, intensity: 0.45 }]],
      verseTop: [['chase', { speed: 1, size: 0.1, gap: 0.6 }, { opacity: 0.7 }], ['steps', { zones: 8, every: 0.5, decay: 0.5, order: 'random' }, { opacity: 0.75 }], ['bounce', { speed: 1, tail: 0.3 }]],
      breakdown: ['breathe', { speed: 4, floor: 0.1 }], outro: ['breathe', { speed: 2, floor: 0.05 }], motion: ['steps', 'bounce', 'comets', 'scanner'], builds: ['dark', 'room', 'poles'],
      drops: [{ base: ['chase', { speed: 0.5, size: 0.12, gap: 0.5 }], top: 'motion' }, { base: ['halves', { follow: 'drums', every: 1, dim: 0 }], top: ['steps', { zones: 8, every: 0.5, decay: 0.4, order: 'random' }, { opacity: 0.85 }] },
              { base: ['blocks', { every: 0.5, size: 3, density: 0.5 }], top: ['flash', { every: 1, decay: 0.25 }, { opacity: 0.5, blend: 'add' }], flash: true },
              { base: ['roomchase', { every: 0.5, floor: 0.15 }], top: 'motion', room: true }] },
    poprock: { name: 'Pop rock', near: ['altrock', 'dancepop'],
      intro: ['breathe', { speed: 4, floor: 0.05 }], verse: [['gradient', { speed: 16, intensity: 0.45 }], ['colorwash', { speed: 8, intensity: 0.5 }], ['roomwave', { speed: 8, floor: 0.1 }]],
      verseTop: [['scanner', { speed: 4, size: 0.15 }, { opacity: 0.7 }], ['meteors', { every: 2, chance: 0.5, travel: 3, tail: 0.3 }], ['sparkle', { density: 0.08, life: 0.3 }, { opacity: 0.8 }]],
      breakdown: ['aurora', { speed: 16, depth: 0.8, intensity: 0.35 }], outro: ['colorwash', { speed: 16, intensity: 0.35 }], motion: ['comets', 'scanner', 'sparkles', 'orbit'], builds: ['room', 'scan', 'poles'],
      drops: [{ base: ['battle', { speed: 8, surge: 0.25 }], top: 'motion' }, { base: ['halves', { follow: 'drums', every: 2, dim: 0.15 }], top: 'motion' },
              { base: ['colorwash', { speed: 4, intensity: 0.85 }], top: ['scanner', { speed: 1, size: 0.2 }, { opacity: 0.8 }] },
              { base: ['roomsplit', { every: 2, pump: 0.3 }], top: 'motion', room: true }] },
    house: { name: 'House', near: ['dancepop', 'electro'],
      intro: ['gradient', { speed: 16, intensity: 0.4 }], verse: [['wave', { speed: 4, waves: 1.5, floor: 0.05 }], ['colorwash', { speed: 8, intensity: 0.45 }]],
      verseTop: [['pulsar', { every: 1, count: 2, size: 0.08, decay: 0.5 }, { opacity: 0.85 }], ['ripple', { every: 1, travel: 1, width: 0.1 }], ['sparkle', { density: 0.12, life: 0.2 }, { opacity: 0.8 }]],
      breakdown: ['aurora', { speed: 16, intensity: 0.4, depth: 0.6 }], outro: ['wave', { speed: 8, waves: 1, floor: 0.1 }], motion: ['ripples', 'sparkles', 'pulsar', 'steps'], builds: ['room', 'climb', 'scan'],
      drops: [{ base: ['roomchase', { every: 1, floor: 0.15 }], top: 'motion', room: true }, { base: ['alternate', { every: 1, size: 2, intensity: 0.85 }], top: ['sparkle', { density: 0.2, life: 0.15 }, { opacity: 0.9 }] },
              { base: ['wave', { speed: 1, waves: 2, floor: 0.1 }], top: 'motion' },
              { base: ['chase', { speed: 1, size: 0.12, gap: 0.6 }], top: ['flash', { every: 1, decay: 0.3 }, { opacity: 0.5, blend: 'add' }], flash: true }] },
    rnb: { name: 'R&B', calm: true, near: ['dreamy', 'hiphop'],
      intro: ['breathe', { speed: 8, floor: 0.1 }], verse: [['aurora', { speed: 16, intensity: 0.45, depth: 0.7 }], ['colorwash', { speed: 16, intensity: 0.45 }], ['roomwave', { speed: 16, floor: 0.15 }]],
      verseTop: [['orbit', { speed: 8, count: 2 }, { opacity: 0.8 }], ['sparkle', { density: 0.06, life: 0.4 }, { opacity: 0.8 }], ['pulsar', { every: 2, count: 1, size: 0.1, decay: 0.8 }, { opacity: 0.7 }]],
      breakdown: ['ember', { speed: 2, intensity: 0.5 }], outro: ['breathe', { speed: 8, floor: 0.1 }], motion: ['orbit', 'sparkles', 'meteors', 'ripples'], builds: ['room', 'climb'],
      drops: [
              { base: ['halves', { follow: 'drums', every: 2, dim: 0.2 }], top: ['orbit', { speed: 4, count: 2 }, { opacity: 0.85 }], lift: true },
              { base: ['hitswap', { follow: 'drums', punch: 0.25 }], top: 'motion', lift: true },
              { base: ['roomchase', { every: 1, floor: 0.2 }], top: ['sparkle', { density: 0.12, life: 0.25 }, { opacity: 0.85 }], room: true, lift: true },
              { base: ['wave', { speed: 4, waves: 1, floor: 0.15 }], top: 'motion' }, { base: ['colorwash', { speed: 8, intensity: 0.75 }], top: 'motion' },
              { base: ['roomwave', { speed: 8, floor: 0.25 }], top: 'motion', room: true },
              { base: ['breathe', { speed: 2, floor: 0.25 }], top: ['pulsar', { every: 1, count: 2, size: 0.1, decay: 0.6 }, { opacity: 0.85 }] }] },
    electro: { name: 'Electro house', near: ['house', 'bass'],
      intro: ['gradient', { speed: 16, intensity: 0.4 }], verse: [['wave', { speed: 4, waves: 1.5, floor: 0 }], ['halves', { every: 2, dim: 0.15 }]],
      verseTop: [['steps', { zones: 8, every: 1, decay: 0.6, order: 'random' }, { opacity: 0.8 }], ['chase', { speed: 2, size: 0.1, gap: 0.7 }, { opacity: 0.7 }], ['dualcomet', { speed: 4, tail: 0.3 }, { opacity: 0.8 }]],
      breakdown: ['twinkle', { density: 0.2, speed: 4, base: 0.1 }], outro: ['gradient', { speed: 16, intensity: 0.35 }], motion: ['steps', 'ripples', 'comets', 'orbit'], builds: ['scan', 'room', 'dark'],
      drops: [
              { base: ['halves', { follow: 'drums', every: 1, dim: 0 }], top: 'motion' },
              { base: ['roomsplit', { every: 0.5, pump: 0.5 }], top: ['flash', { every: 1, decay: 0.3 }, { opacity: 0.5, blend: 'add' }], flash: true, room: true },
              { base: ['hyperchase', { speed: 0.25, size: 0.08 }], top: 'motion', fast: true, rave: true },   // rave only: in a dance song it hurts the eyes
              { base: ['chase', { speed: 0.5, size: 0.12, gap: 0.6 }], top: 'motion' }, { base: ['hitswap', { follow: 'hits', punch: 0.4 }], top: 'motion' },
              { base: ['blocks', { every: 0.5, size: 3, density: 0.5 }], top: ['strobe', { every: 0.5, duty: 0.25 }, { opacity: 0.5, blend: 'add' }], flash: true }] },
    hiphop: { name: 'Hip-hop', near: ['rnb', 'bass'],
      intro: ['ember', { speed: 2, intensity: 0.5 }], verse: [['breathe', { speed: 2, floor: 0.05 }], ['ember', { speed: 1, intensity: 0.7 }]],
      verseTop: [['pulsar', { every: 1, count: 1, size: 0.1, decay: 0.7 }, { opacity: 0.85 }], ['meteors', { every: 2, chance: 0.4, travel: 2, tail: 0.3 }], ['bounce', { speed: 4, tail: 0.35 }]],
      breakdown: ['ember', { speed: 2, intensity: 0.45 }], outro: ['breathe', { speed: 4, floor: 0.05 }], motion: ['pulsar', 'meteors', 'bounce', 'orbit'], builds: ['dark', 'room'],
      drops: [{ base: ['hitswap', { follow: 'hits', punch: 0.3 }], top: 'motion' }, { base: ['halves', { follow: 'drums', every: 1, dim: 0 }], top: 'motion' },
              { base: ['wave', { speed: 2, waves: 1, floor: 0 }], top: ['pulsar', { every: 1, count: 2, size: 0.1, decay: 0.5 }, { opacity: 0.9 }] },
              { base: ['roomsplit', { every: 1, pump: 0.5 }], top: 'motion', room: true }] },
    metal: { name: 'Metal', dark: true, near: ['altrock', 'industrial'],
      intro: ['ember', { speed: 1, intensity: 0.6 }], verse: [['battle', { speed: 8, surge: 0.2 }], ['halves', { follow: 'drums', every: 1, dim: 0.2 }]],
      verseTop: [['scanner', { speed: 1, size: 0.15 }, { opacity: 0.8 }], ['lightning', { rate: 0.2 }, { opacity: 0.6, blend: 'add' }], ['steps', { zones: 8, every: 0.5, decay: 0.4, order: 'random' }, { opacity: 0.8 }]],
      breakdown: ['ember', { speed: 1, intensity: 0.6 }], outro: ['ember', { speed: 2, intensity: 0.5 }], motion: ['scanner', 'steps', 'meteors', 'bounce'], builds: ['dark', 'poles', 'room'],
      drops: [
              { base: ['battle', { speed: 4, surge: 0.3 }], top: ['scanner', { speed: 1, size: 0.15 }, { opacity: 0.85 }] },
              { base: ['blocks', { every: 1, size: 2, density: 0.45 }], top: 'motion' },
              { base: ['battle', { speed: 4, surge: 0.3 }], top: ['lightning', { rate: 0.5 }, { opacity: 0.85, blend: 'add' }], flash: true },
              { base: ['halves', { follow: 'drums', every: 1, dim: 0 }], top: 'motion' }, { base: ['hitswap', { follow: 'drums', punch: 0.5 }], top: 'motion' },
              { base: ['blocks', { every: 0.5, size: 2, density: 0.45 }], top: ['strobe', { every: 0.25, duty: 0.3 }, { opacity: 0.5, blend: 'add' }], flash: true, fast: true }] },
    bass: { name: 'Bass music', dark: true, near: ['electro', 'dnb'],
      intro: ['aurora', { speed: 16, intensity: 0.4, depth: 0.9 }], verse: [['battle', { speed: 16, surge: 0.1 }], ['wave', { speed: 4, waves: 1, floor: 0 }]],
      verseTop: [['meteors', { every: 1, chance: 0.5, travel: 2, tail: 0.25 }], ['dualcomet', { speed: 4, tail: 0.3 }, { opacity: 0.8 }], ['pulsar', { every: 1, count: 2, size: 0.08, decay: 0.5 }, { opacity: 0.8 }]],
      breakdown: ['ember', { speed: 2, intensity: 0.5 }], outro: ['aurora', { speed: 16, intensity: 0.35, depth: 0.9 }], motion: ['meteors', 'comets', 'ripples', 'steps'], builds: ['dark', 'scan'],
      drops: [
              { base: ['halves', { follow: 'drums', every: 1, dim: 0 }], top: ['pulsar', { every: 1, count: 2, size: 0.08, decay: 0.4 }, { opacity: 0.9 }] },
              { base: ['hitswap', { follow: 'hits', punch: 0.5 }], top: 'motion' },
              { base: ['battle', { speed: 4, surge: 0.3 }], top: ['lightning', { rate: 0.5 }, { opacity: 0.85, blend: 'add' }], flash: true },
              { base: ['roomsplit', { every: 0.5, pump: 0.5 }], top: 'motion', room: true }] },
    industrial: { name: 'Industrial', dark: true, near: ['metal', 'techno'],
      intro: ['ember', { speed: 1, intensity: 0.5 }], verse: [['halves', { follow: 'drums', every: 1, dim: 0.25 }], ['ember', { speed: 1, intensity: 0.7 }]],
      verseTop: [['scanner', { speed: 2, size: 0.1 }, { opacity: 0.8 }], ['lightning', { rate: 0.15 }, { opacity: 0.5, blend: 'add' }], ['pulsar', { every: 1, count: 1, size: 0.06, decay: 0.4 }, { opacity: 0.85 }]],
      breakdown: ['ember', { speed: 2, intensity: 0.4 }], outro: ['ember', { speed: 2, intensity: 0.4 }], motion: ['scanner', 'steps', 'meteors'], builds: ['dark', 'scan'],
      drops: [
              { base: ['battle', { speed: 4, surge: 0.3 }], top: ['scanner', { speed: 1, size: 0.12 }, { opacity: 0.85 }] },
              { base: ['blocks', { every: 0.5, size: 2, density: 0.4 }], top: 'motion' },
              { base: ['hitswap', { follow: 'drums', punch: 0.5 }], top: ['lightning', { rate: 0.35 }, { opacity: 0.7, blend: 'add' }], flash: true },
              { base: ['halves', { follow: 'drums', every: 1, dim: 0 }], top: 'motion' }] },
    trance: { name: 'Trance', near: ['electro', 'house'],
      intro: ['aurora', { speed: 16, intensity: 0.4, depth: 0.8 }], verse: [['gradient', { speed: 8, intensity: 0.45 }], ['wave', { speed: 8, waves: 1, floor: 0.1 }], ['roomwave', { speed: 8, floor: 0.1 }]],
      verseTop: [['orbit', { speed: 4, count: 2 }, { opacity: 0.8 }], ['sparkle', { density: 0.1, life: 0.3 }, { opacity: 0.8 }], ['scanner', { speed: 4, size: 0.2 }, { opacity: 0.7 }]],
      breakdown: ['aurora', { speed: 16, depth: 0.9, intensity: 0.4 }], outro: ['aurora', { speed: 16, intensity: 0.35, depth: 0.8 }], motion: ['orbit', 'sparkles', 'comets', 'ripples'], builds: ['scan', 'climb', 'room'],
      drops: [{ base: ['gradient', { speed: 2, stretch: 0.5, intensity: 0.85 }], top: 'motion' }, { base: ['wave', { speed: 1, waves: 2, floor: 0.15 }], top: 'motion' },
              { base: ['roomwave', { speed: 4, floor: 0.2 }], top: ['sparkle', { density: 0.25, life: 0.15 }, { opacity: 0.9 }], room: true },
              { base: ['chase', { speed: 0.5, size: 0.15, gap: 0.5 }], top: ['flash', { every: 1, decay: 0.35 }, { opacity: 0.4, blend: 'add' }], flash: true }] },
    cinematic: { name: 'Soundtrack', calm: true, near: ['dreamy'],
      intro: ['aurora', { speed: 16, intensity: 0.35, depth: 0.9 }], verse: [['aurora', { speed: 16, intensity: 0.45, depth: 0.8 }], ['colorwash', { speed: 16, intensity: 0.45 }]],
      verseTop: [['meteors', { every: 4, chance: 0.4, travel: 4, tail: 0.35 }], ['sparkle', { density: 0.05, life: 0.5 }, { opacity: 0.8 }], ['orbit', { speed: 16, count: 1 }, { opacity: 0.7 }]],
      breakdown: ['twinkle', { density: 0.2, speed: 4, base: 0.12 }], outro: ['aurora', { speed: 16, intensity: 0.3, depth: 0.9 }], motion: ['meteors', 'sparkles', 'orbit'], builds: ['room', 'climb'],
      drops: [
              { base: ['halves', { follow: 'drums', every: 2, dim: 0.2 }], top: 'motion', lift: true },
              { base: ['roomsplit', { every: 2, pump: 0.3 }], top: ['meteors', { every: 1, chance: 0.5, travel: 2, tail: 0.3 }], room: true, lift: true },
              { base: ['colorwash', { speed: 8, intensity: 0.85 }], top: 'motion' }, { base: ['wave', { speed: 4, waves: 1, floor: 0.2 }], top: 'motion' },
              { base: ['roomwave', { speed: 8, floor: 0.3 }], top: 'motion', room: true }] },
    techno: { name: 'Techno', near: ['industrial', 'house'],
      intro: ['breathe', { speed: 4, floor: 0.02 }], verse: [['pulsar', { every: 1, count: 1, size: 0.08, decay: 0.5 }], ['breathe', { speed: 1, floor: 0.05 }]],
      verseTop: [['scanner', { speed: 4, size: 0.1 }, { opacity: 0.7 }], ['steps', { zones: 8, every: 1, decay: 0.5, order: 'run' }, { opacity: 0.7 }], ['ripple', { every: 2, travel: 1, width: 0.08 }]],
      breakdown: ['breathe', { speed: 8, floor: 0.05 }], outro: ['breathe', { speed: 4, floor: 0.02 }], motion: ['steps', 'scanner', 'pulsar', 'ripples'], builds: ['dark', 'scan'],
      drops: [{ base: ['pulsar', { every: 0.5, count: 2, size: 0.08, decay: 0.4 }], top: 'motion' }, { base: ['roomchase', { every: 0.5, floor: 0 }], top: 'motion', room: true },
              { base: ['blocks', { every: 0.5, size: 3, density: 0.3 }], top: ['strobe', { every: 0.5, duty: 0.25 }, { opacity: 0.45, blend: 'add' }], flash: true },
              { base: ['halves', { follow: 'drums', every: 1, dim: 0 }], top: 'motion' }] },
    dreamy: { name: 'Dreamy', calm: true, near: ['indiepop', 'rnb'],
      intro: ['twinkle', { density: 0.25, speed: 4, base: 0.12 }], verse: [['colorwash', { speed: 16, intensity: 0.45 }], ['aurora', { speed: 16, intensity: 0.4, depth: 0.7 }]],
      verseTop: [['sparkle', { density: 0.08, life: 0.25 }, { opacity: 0.8 }], ['orbit', { speed: 8, count: 2 }, { opacity: 0.8 }], ['ripple', { every: 4, travel: 3, width: 0.15 }, { opacity: 0.8 }]],
      breakdown: ['twinkle', { density: 0.2, speed: 4, base: 0.15 }], outro: ['colorwash', { speed: 16, intensity: 0.35 }], motion: ['sparkles', 'orbit', 'meteors'], builds: ['room', 'climb'],
      drops: [
              { base: ['alternate', { every: 2, size: 3, intensity: 0.7 }], top: 'motion', lift: true },
              { base: ['wave', { speed: 4, waves: 1, floor: 0.1 }], top: 'motion' }, { base: ['colorwash', { speed: 8, intensity: 0.75 }], top: 'motion' },
              { base: ['roomwave', { speed: 8, floor: 0.25 }], top: 'motion', room: true },
              { base: ['aurora', { speed: 8, intensity: 0.8, depth: 0.6 }], top: ['pulsar', { every: 2, count: 2, size: 0.1, decay: 0.8 }, { opacity: 0.8 }] }] },
    dnb: { name: 'Drum and bass', near: ['bass', 'electro'],
      intro: ['aurora', { speed: 16, intensity: 0.4, depth: 0.8 }], verse: [['wave', { speed: 2, waves: 2, floor: 0 }], ['battle', { speed: 8, surge: 0.2 }]],
      verseTop: [['dualcomet', { speed: 2, tail: 0.3 }, { opacity: 0.8 }], ['scanner', { speed: 1, size: 0.12 }, { opacity: 0.75 }], ['steps', { zones: 8, every: 0.5, decay: 0.4, order: 'run' }, { opacity: 0.75 }]],
      breakdown: ['aurora', { speed: 16, depth: 0.9, intensity: 0.35 }], outro: ['wave', { speed: 4, waves: 1, floor: 0.05 }], motion: ['comets', 'scanner', 'steps', 'ripples'], builds: ['scan', 'dark'],
      drops: [{ base: ['hyperchase', { speed: 0.25, size: 0.08 }], top: 'motion', fast: true, rave: true }, { base: ['chase', { speed: 0.5, size: 0.1, gap: 0.5 }], top: 'motion' },
              { base: ['hitswap', { follow: 'hits', punch: 0.4 }], top: 'motion' }, { base: ['blocks', { every: 0.25, size: 2, density: 0.4 }], top: 'motion', fast: true }] },
    folk: { name: 'Folk', calm: true, near: ['dreamy', 'jazz'],
      intro: ['candle', { level: 0.45 }], verse: [['ember', { speed: 1, intensity: 0.6 }], ['breathe', { speed: 8, floor: 0.15 }]],
      verseTop: [['meteors', { every: 4, chance: 0.3, travel: 4, tail: 0.35 }], ['sparkle', { density: 0.04, life: 0.5 }, { opacity: 0.7 }], ['ripple', { every: 4, travel: 3, width: 0.15 }]],
      breakdown: ['candle', { level: 0.4 }], outro: ['candle', { level: 0.4 }], motion: ['meteors', 'ripples', 'sparkles'], builds: ['room', 'climb'],
      drops: [
              { base: ['halves', { follow: 'drums', every: 2, dim: 0.25 }], top: 'motion', lift: true },
              { base: ['ember', { speed: 0.5, intensity: 1 }], top: 'motion' }, { base: ['wave', { speed: 4, waves: 1, floor: 0.2 }], top: 'motion' },
              { base: ['roomwave', { speed: 8, floor: 0.3 }], top: 'motion', room: true }] },
    latin: { name: 'Latin', near: ['dancepop', 'house'],
      intro: ['gradient', { speed: 16, intensity: 0.45 }], verse: [['alternate', { every: 1, size: 2, intensity: 0.6 }], ['wave', { speed: 4, waves: 1.5, floor: 0.1 }]],
      verseTop: [['bounce', { speed: 2, tail: 0.3 }], ['ripple', { every: 1, travel: 1.5, width: 0.12 }], ['steps', { zones: 8, every: 1, decay: 0.8, order: 'bounce' }, { opacity: 0.75 }]],
      breakdown: ['colorwash', { speed: 8, intensity: 0.45 }], outro: ['gradient', { speed: 16, intensity: 0.35 }], motion: ['bounce', 'ripples', 'sparkles', 'steps'], builds: ['room', 'poles', 'climb'],
      drops: [{ base: ['halves', { follow: 'drums', every: 1, dim: 0.1 }], top: 'motion' }, { base: ['roomchase', { every: 1, floor: 0.2 }], top: 'motion', room: true },
              { base: ['alternate', { every: 0.5, size: 2, intensity: 0.85 }], top: ['sparkle', { density: 0.15, life: 0.2 }, { opacity: 0.85 }] },
              { base: ['hitswap', { follow: 'hits', punch: 0.3 }], top: 'motion' }] },
    jazz: { name: 'Jazz and blues', calm: true, near: ['rnb', 'folk'],
      intro: ['breathe', { speed: 8, floor: 0.15 }], verse: [['ember', { speed: 1, intensity: 0.65 }], ['colorwash', { speed: 16, intensity: 0.45 }]],
      verseTop: [['sparkle', { density: 0.05, life: 0.4 }, { opacity: 0.8 }], ['orbit', { speed: 8, count: 1 }, { opacity: 0.75 }], ['pulsar', { every: 2, count: 1, size: 0.1, decay: 0.8 }, { opacity: 0.7 }]],
      breakdown: ['ember', { speed: 2, intensity: 0.5 }], outro: ['breathe', { speed: 8, floor: 0.1 }], motion: ['orbit', 'sparkles', 'bounce'], builds: ['room'],
      drops: [
              { base: ['halves', { follow: 'drums', every: 2, dim: 0.2 }], top: ['bounce', { speed: 2, tail: 0.3 }, { opacity: 0.85 }], lift: true },
              { base: ['wave', { speed: 4, waves: 1, floor: 0.2 }], top: 'motion' }, { base: ['ember', { speed: 0.5, intensity: 1 }], top: 'motion' },
              { base: ['alternate', { every: 2, size: 3, intensity: 0.7 }], top: 'motion' }] },
  };
  // Which scene each of the genre model's labels belongs to: MAEST's Discogs styles ("Genre---Style") by the
  // whole label where a style name means two things, else by the style, else by its genre; AudioSet's
  // labels (the older model) and Spotify's words by keyword.
  const SCENE_LABEL = { 'Rock---Hardcore': 'metal', 'Electronic---Hardcore': 'dnb', 'Hip Hop---Trap': 'hiphop', 'Electronic---Trap': 'bass',
    'Electronic---Ambient': 'dreamy', 'Rock---Psychedelic Rock': 'altrock', 'Funk / Soul---Disco': 'house', 'Electronic---Disco': 'house' };
  const SCENE_STYLE = {
    indiepop: ['Indie Pop', 'Twee Pop', 'Bubblegum', 'Power Pop', 'Chamber Pop', 'Baroque Pop'],
    synthpop: ['Synth-pop', 'Synthwave', 'New Wave', 'Electroclash', 'Italo-Disco', 'Minimal Wave', 'Darkwave', 'Electropop', 'Hi NRG'],
    altrock: ['Alternative Rock', 'Indie Rock', 'Grunge', 'Post-Punk', 'Garage Rock', 'Math Rock', 'Britpop', 'Shoegaze', 'Noise Rock', 'Lo-Fi', 'Post Rock', 'Psychedelic Rock', 'Stoner Rock', 'Art Rock', 'Experimental'],
    dancepop: ['Dance-pop', 'Europop', 'K-pop', 'J-pop', 'Teen Pop', 'Schlager', 'Eurodance', 'Novelty', 'Vocal'],
    poppunk: ['Pop Punk', 'Punk', 'Emo', 'Ska', 'Skate Punk', 'Melodic Hardcore', 'Oi', 'Psychobilly'],
    poprock: ['Pop Rock', 'Soft Rock', 'Classic Rock', 'Hard Rock', 'Arena Rock', 'AOR', 'Rock & Roll', 'Blues Rock', 'Southern Rock', 'Glam', 'Rockabilly', 'Prog Rock', 'Symphonic Rock'],
    house: ['House', 'Deep House', 'Disco', 'Nu-Disco', 'Tech House', 'Tropical House', 'UK Garage', 'Speed Garage', 'Acid House', 'Euro House', 'Italo House', 'Garage House', 'Boogie', 'Funk', 'Afro House', 'Electro Swing'],
    rnb: ['Contemporary R&B', 'RnB/Swing', 'Rhythm & Blues', 'Neo Soul', 'Soul', 'Gospel', 'Ballad', 'Smooth Jazz', 'Quiet Storm', 'Motown'],
    electro: ['Electro House', 'Electro', 'Big Room', 'Hard House', 'Big Beat', 'Breakbeat', 'Breaks', 'Bassline', 'Hands Up', 'Fidget', 'Moombahton', 'Complextro', 'Jersey Club'],
    hiphop: ['Hip Hop', 'Trap', 'Pop Rap', 'Gangsta', 'Boom Bap', 'Grime', 'Cloud Rap', 'Drill', 'Conscious', 'Crunk', 'Bounce', 'Hardcore Hip-Hop', 'Horrorcore', 'Instrumental'],
    metal: ['Heavy Metal', 'Metalcore', 'Nu Metal', 'Post-Hardcore', 'Death Metal', 'Thrash', 'Gothic Metal', 'Alternative Metal', 'Deathcore', 'Black Metal', 'Doom Metal', 'Power Metal', 'Speed Metal', 'Sludge Metal', 'Progressive Metal', 'Grindcore', 'Industrial Metal'],
    bass: ['Dubstep', 'Bass Music', 'Halftime', 'Glitch Hop', 'Future Bass', 'Riddim', 'Juke'],
    industrial: ['Industrial', 'EBM', 'Goth Rock', 'Coldwave', 'Gothic Rock', 'Aggrotech', 'Noise', 'Power Electronics', 'Dark Ambient', 'Witch House'],
    trance: ['Trance', 'Progressive Trance', 'Progressive House', 'Psy-Trance', 'Goa Trance', 'Hard Trance', 'Tech Trance', 'Progressive Breaks', 'Uplifting Trance'],
    cinematic: ['Soundtrack', 'Score', 'Musical', 'Theme', 'Modern Classical', 'Neo-Classical', 'Opera', 'Romantic', 'Baroque', 'Classical', 'Video Game Music', 'Spoken Word'],
    techno: ['Techno', 'Minimal', 'Minimal Techno', 'Hard Techno', 'Deep Techno', 'Schranz', 'Acid', 'Dub Techno', 'Tribal', 'Detroit Techno'],
    dreamy: ['Downtempo', 'Trip Hop', 'Chillwave', 'Ambient', 'Lo-Fi', 'Dream Pop', 'IDM', 'Abstract', 'New Age', 'Chillout', 'Leftfield', 'Vaporwave'],
    dnb: ['Drum n Bass', 'Jungle', 'Breakcore', 'Liquid Funk', 'Hardstyle', 'Gabber', 'Happy Hardcore', 'Speedcore', 'Jumpstyle', 'Neurofunk'],
    folk: ['Folk', 'Folk Rock', 'Acoustic', 'Country', 'Bluegrass', 'Singer/Songwriter', 'Americana', 'Country Rock', 'Celtic'],
    latin: ['Reggaeton', 'Dancehall', 'Afrobeat', 'Cumbia', 'Salsa', 'Soca', 'Bachata', 'Merengue', 'Latin', 'Reggae', 'Dub', 'Samba', 'Bossanova', 'Afrobeats', 'Kuduro'],
    jazz: ['Swing', 'Big Band', 'Lounge', 'Cool Jazz', 'Bebop', 'Jazz-Funk', 'Fusion', 'Piano Blues', 'Delta Blues', 'Chicago Blues', 'Easy Listening', 'Cabaret'],
  };
  const SCENE_OF_STYLE = Object.fromEntries(Object.entries(SCENE_STYLE).flatMap(([k, v]) => v.map(st => [st, k])));
  const SCENE_GENRE = { Electronic: 'electro', Rock: 'altrock', Pop: 'dancepop', 'Hip Hop': 'hiphop', 'Funk / Soul': 'rnb', Jazz: 'jazz', Blues: 'jazz',
    Classical: 'cinematic', 'Stage & Screen': 'cinematic', 'Folk, World, & Country': 'folk', Latin: 'latin', Reggae: 'latin', "Children's": 'indiepop',
    'Brass & Military': 'cinematic', 'Non-Music': 'cinematic' };
  const SCENE_WORDS = [[/dubstep|riddim|brostep|bass music/, 'bass'], [/drum and bass|drum n bass|dnb|jungle|hardstyle|hardcore techno/, 'dnb'], [/techno/, 'techno'],
    [/trance/, 'trance'], [/electro house|big room|edm|electro/, 'electro'], [/house|disco|garage/, 'house'], [/metal|core\b|hardcore/, 'metal'], [/industrial|goth/, 'industrial'],
    [/punk|emo|ska/, 'poppunk'], [/hip hop|hip-hop|rap|trap|drill|grime/, 'hiphop'], [/r&b|rhythm and blues|soul|gospel/, 'rnb'], [/reggae|latin|salsa|reggaeton|dancehall|afro/, 'latin'],
    [/jazz|blues|swing/, 'jazz'], [/folk|country|acoustic|bluegrass/, 'folk'], [/ambient|downtempo|chill|lo-fi|lofi|new-age|new age|trip hop/, 'dreamy'],
    [/soundtrack|classical|orchestral|opera|score|musical/, 'cinematic'], [/synth|new wave/, 'synthpop'], [/indie pop|dream pop/, 'indiepop'],
    [/alternative|indie|grunge|psychedelic|rock music|^rock/, 'altrock'], [/rock/, 'poprock'], [/pop/, 'dancepop']];
  function sceneOf(label) {
    if (SCENE_LABEL[label]) return SCENE_LABEL[label];
    const [g, st] = label.includes('---') ? label.split('---') : [null, label];
    if (SCENE_OF_STYLE[st]) return SCENE_OF_STYLE[st];
    if (g && SCENE_GENRE[g]) return SCENE_GENRE[g];
    const w = String(st).toLowerCase(), hit = SCENE_WORDS.find(([re]) => re.test(w));
    return hit ? hit[1] : null;
  }
  /** How likely each scene's moves are in this song (the best fit 1). From the genre model's scores, each
   *  scene by its strongest label and a little more for every other of its labels (a rock song has many
   *  rock labels; summing them all buried Passion Pit's indie and synth pop under its one rock guess). The
   *  scenes heard at a third of the best or more take part, the best one's neighbors a little (a move or
   *  two from next door), nothing from scenes the song is not. A chill song draws from calm scenes only. */
  function sceneWeights() {
    const g = (S.analysis && S.analysis.genre) || {}, st = showStyle(), best = {}, rest = {};
    const add = (k, v) => { if (!k || !SCENES[k]) return; if (v > (best[k] || 0)) { rest[k] = (rest[k] || 0) + (best[k] || 0); best[k] = v; } else rest[k] = (rest[k] || 0) + v; };
    for (const [label, v] of Object.entries(g.scores || {})) add(sceneOf(label), label.startsWith('Stage & Screen---') ? v * 0.5 : v);
    if (!Object.keys(best).length) (g.genres || []).forEach((label, i) => add(sceneOf(label), 1 - 0.25 * i));
    if (!Object.keys(best).length) for (const sp of g.spotify || []) add(sceneOf(sp), 0.5);
    if (!Object.keys(best).length) add({ chill: 'dreamy', groove: 'poprock', dance: 'dancepop', rave: 'electro' }[st], 1);
    const raw = Object.fromEntries(Object.keys(best).map(k => [k, best[k] + 0.3 * (rest[k] || 0)]));
    let keys = Object.keys(raw);
    if (st === 'chill') { keys = keys.filter(k => SCENES[k].calm); if (!keys.length) { keys = ['dreamy']; raw.dreamy = 1; } }
    const top = Math.max(...keys.map(k => raw[k])), w = {};
    for (const k of keys) { const r = raw[k] / top; if (r >= 0.33) w[k] = r * r; }
    const lead = keys.sort((x, y) => raw[y] - raw[x])[0];
    for (const k of SCENES[lead].near || []) if (!(k in w) && (st !== 'chill' || SCENES[k].calm)) w[k] = 0.1;
    return w;
  }
  /** Draw the song's own mix of effects (S.mix), its motions, its buildups, and which kinds of light are
   *  ticked. Jam mode calls it again when it learns a song's style part way through, before building the
   *  rest. The draw is seeded by the song (its length and tempo): the same song gets the same show on
   *  every rebuild, and two songs of one scene get different ones. */
  function pickLook() {
    const a0 = S.analysis || {}, w = sceneWeights();
    const seed = Math.round((a0.duration || 0) * 10) + Math.round((a0.bpm || 0) * 100);
    S.lookSeed = seed;
    let n = 0; const rnd = () => Effects.hash(seed + 7919 * ++n);
    // a weighted draw without repeats: each scene's entries as likely as the scene suits the song
    // (Efraimidis-Spirakis keys), one entry per effect so two picks are never the same thing twice
    // (sharp > 1 leans harder on the song's own scene: its drops are its signature, a second scene's are a guest)
    const order = (slot, name = x => x[0], like = () => 1, sharp = 1) => {
      const seen = new Set();
      return Object.entries(SCENES).flatMap(([k, l]) => (w[k] > 0 ? [].concat(slot(l) || []).map(x => [x, Math.pow(rnd(), 1 / (Math.pow(w[k], sharp) * like(x)))]) : []))
        .sort((x, y) => y[1] - x[1]).map(x => x[0]).filter(x => !seen.has(name(x)) && seen.add(name(x)));
    };
    const bed = slot => order(l => [slot(l)])[0];
    const verse = order(l => l.verse), tops = order(l => l.verseTop), motions = order(l => l.motion, x => x, x => (SWEEPS.has(x) ? 0.5 : x === 'steps' ? 0.6 : 1));   // sweeps (and steps) less likely: not in every drop
    S.mix = {
      intro: bed(l => l.intro), breakdown: bed(l => l.breakdown), outro: bed(l => l.outro),
      verse: [verse[0], verse[1] || verse[0]], verseTop: [tops[0], tops[1] || tops[0]],
      drops: order(l => l.drops, r => r.base[0], () => 1, 2),        // in the song's own order; the first that fit are used
    };
    // a long breakdown's every other 8 bars on a second bed (a different one, its own scene's or a neighbor's)
    { const b2 = order(l => [l.breakdown, l.intro]).find(x => x[0] !== S.mix.breakdown[0]); S.mix.breakdown2 = b2 || S.mix.breakdown; }
    S.builds = order(l => l.builds || ['room'], x => x).slice(0, 2);          // how its buildups go (two, in turn)
    S.stepOrder = ['run', 'bounce', 'random'][Math.floor(rnd() * 3)];          // how this song's stage steps go
    const tint = rnd();                                                         // its stars: white half the time
    S.starTint = tint < 0.5 ? 'white' : tint < 0.8 ? 'palette' : 'warm';
    // what drifts over its intro, breakdowns and outro (it used to be meteors in every song)
    S.drift = [['meteors', { every: 4, chance: 0.35, travel: 4 }], ['steps', { zones: 8, every: 2, decay: 2.5, order: 'random' }, { opacity: 0.6 }],
               ['sparkle', { density: 0.05, life: 0.4 }, { opacity: 0.8 }]][Math.floor(rnd() * 3)];
    const ranked = Object.entries(w).sort((x, y) => y[1] - x[1]);
    S.look = ranked[0][0];                                            // the scene it leans to most
    S.sceneNames = ranked.filter(([, v]) => v >= 0.25).slice(0, 2).map(([k]) => SCENES[k].name);
    const total = ranked.reduce((x, [, v]) => x + v, 0);
    S.dark = ranked.filter(([k]) => SCENES[k].dark).reduce((x, [, v]) => x + v, 0) >= 0.5 * total;   // thunder and heartbeats
    // the song's own motion, and a second for every other drop
    // ...never the move its verses already make (stage steps over the verse and again over the drop: the
    // drop looked just like the verse)
    const FX = { sparkles: 'sparkle', ripples: 'ripple', comets: 'dualcomet' }, inVerse = new Set(S.mix.verseTop.map(t => t && t[0]));
    const lifts = motions.filter(m => !inVerse.has(FX[m] || m)), own = lifts.length ? lifts : motions;
    S.motionFam = own[0];
    S.motionFams = [own[0], own[1] || own[0]];
    const kinds = new Set(showLights().map(lightKind));
    S.mixedLights = kinds.has('long') && kinds.has('short');        // long strips and short lamps: each plays its own part
    S.hasTubes = kinds.has('tubes');
  }
  const SWEEPS = new Set(['comets', 'scanner', 'bounce', 'meteors', 'orbit']);    // motions that travel the strip (drawn half as often)
  /** How hard one section goes: the song's show style, moved one step (groove and dance only) by how loud
   *  the section is next to the rest of the same song, never by how loud it is outright: a song mastered
   *  loud from start to end, or one mixed quiet, has no contrast to go on and keeps its style. A drop or a
   *  buildup that stands out can go up, a verse, intro or breakdown well below the song's run can come
   *  down, and only as far as the genre model's own level allows (a song near the line crosses it, one far
   *  from it does not). A calm song stays calm, a rave stays a rave, and a style set by hand is kept. */
  function sectionStyle(s) {
    const st = showStyle(), p = P(), g = (S.analysis && S.analysis.genre) || {};
    if ((p && p.settings && STYLE_NAMES[p.settings.style]) || st === 'chill' || st === 'rave' || typeof g.level !== 'number') return st;
    const r = relLoud(s), up = s.type === 'drop' || s.type === 'buildup';
    if (up ? r <= 0 : r >= 0) return st;
    return g.level + 0.4 * r >= 1.6 ? 'dance' : 'groove';            // (genre.py's line between groove and dance)
  }
  /** How loud a section is within its own song, -1 (as quiet as it gets) to 1 (as loud as its loudest
   *  stretches); 0 when the song has little contrast to judge by. */
  function relLoud(s) {
    const a = S.analysis; if (!a || !a.energy || !a.energy.length) return 0;
    if (!a._loud) {
      const w = Math.max(1, Math.round(2 * (a.feature_fps || 30))), en = a.energy, e = [];
      for (let i = 0; i + w <= en.length; i += w) { let m = 0; for (let j = i; j < i + w; j++) m += en[j]; if (m / w > 0.02) e.push(m / w); }
      e.sort((x, y) => x - y);
      const q = k => e[Math.min(e.length - 1, Math.floor(k * e.length))] || 0;
      a._loud = { lo: q(0.15), mid: q(0.5), hi: q(0.9), ok: e.length > 15 && q(0.9) >= 1.25 * q(0.5) };
    }
    const L = a._loud; if (!L.ok) return 0;
    const e = energyIn(s.start, s.end);
    return e >= L.mid ? clamp((e - L.mid) / Math.max(1e-6, L.hi - L.mid), 0, 1) : -clamp((L.mid - e) / Math.max(1e-6, L.mid - L.lo), 0, 1);
  }
  /** A buildup that is a clear EDM snare build, in a song that goes with one (dance, rave or electronic): a
   *  steady snare in its first half (3 in 4 beats or more) that has at least doubled, to two a beat or more,
   *  in its last quarter. */
  function edmBuild(s) {
    const a = S.analysis || {}, ev = a.events || {}, st = showStyle(), beat = 60 / (a.bpm || 120), L = s.end - s.start;
    const g = songGenres();
    if (!(st === 'dance' || st === 'rave' || ELECTRONIC.test(g)) || L < 8 * beat) return false;
    const hits = [...(ev.snare || []), ...(ev.roll || [])].map(e => e[0]);
    const q = [0, 1, 2, 3].map(k => hits.filter(t => t >= s.start + k * L / 4 && t < s.start + (k + 1) * L / 4).length / (L / 4 / beat));
    const early = (q[0] + q[1]) / 2;
    return early >= 0.75 && q[3] >= 2 && q[3] >= 1.8 * early;
  }
  /** A song's mean energy from t0 to t1 (0..1). */
  function energyIn(t0, t1) {
    const a = S.analysis, fps = (a && a.feature_fps) || 30; if (!a || !a.energy) return 0;
    let sum = 0, n = 0; for (let i = Math.max(0, Math.floor(t0 * fps)); i < Math.min(a.energy.length, t1 * fps); i++) { sum += a.energy[i]; n++; }
    return n ? sum / n : 0;
  }
  /** The cute parts of a song that is not calm overall: a quiet stretch with no drums (a soft intro, a
   *  hushed breakdown, a gentle last verse), at most half as loud as the song gets. They get the calm
   *  songs' stars. Only the three quietest, so the stars stay special. Returns their [start, end]s. */
  function cuteParts() {
    const a = S.analysis, p = P(); if (!a || !a.energy || !p || showStyle() === 'chill') return [];
    const top = (() => { const e = Array.from(a.energy).filter(v => v > 0.02).sort((x, y) => x - y); return e[Math.floor(0.9 * e.length)] || 1; })();
    const present = (a.curves || {}).drums_present, fps = a.feature_fps || 30;
    const drumless = sct => { if (noDrums()) return true; if (!present || !present.length) return false;
      let on = 0, n = 0; for (let i = Math.floor(sct.start * fps); i < Math.min(present.length, sct.end * fps); i++) { on += present[i] >= 0.3; n++; } return n > 0 && on / n < 0.2; };
    const quiet = p.sections.filter(sct => ['intro', 'breakdown', 'outro', 'verse'].includes(sct.type) && timeToBeat(sct.end) - timeToBeat(sct.start) >= 8
      && drumless(sct) && energyIn(sct.start, sct.end) <= 0.5 * top);
    return quiet.sort((x, y) => energyIn(x.start, x.end) - energyIn(y.start, y.end)).slice(0, 3).map(sct => [sct.start, sct.end]);
  }
  /** A song that goes all out (rainbow drops, fireworks): rave only. Dance used to count too when it drove
   *  hard with two drops, which was nearly every dance song, and every one felt like a rave. */
  function isEpic() {
    return showStyle() === 'rave';
  }
  /** chill | groove | dance | rave: chosen for the song from its genre, unless set by hand in the Song panel */
  const STYLE_NAMES = { chill: 'Chill', groove: 'Groove', dance: 'Dance', rave: 'Rave' };
  /** The analysis found no drums at all (the separated drum track is near silent): a calm song. */
  const noDrums = () => !!(S.analysis && S.analysis.drum_calibration && S.analysis.drum_calibration.has_drums === false);
  /** A song without drums that still lifts: its loud parts (the top tenth) are at least 0.65 and half
   *  again as loud as its middle, like an indie song whose choruses swell on synths and bass. It stays
   *  a calm (chill) show, soft effects only, but its drops lift a little more: their motion moves at a
   *  livelier pace. (It used to get a groove show, with flashes on the bass line and drop blasts, which
   *  was far too much for a song like The Wldlfe's Heaven is a place.) */
  function swells() {
    const a = S.analysis; if (!a || !a.energy || !a.energy.length) return false;
    if (a._swells === undefined) {
      // judged on the energy two seconds at a time (a single loud moment is not a chorus), and never for
      // a song that sounds calm to the listening model (a lullaby-quiet song stays calm however it swells)
      const w = Math.max(1, Math.round(2 * (a.feature_fps || 30))), en = Array.from(a.energy), e = [];
      for (let i = 0; i + w <= en.length; i += w) { let m = 0; for (let j = i; j < i + w; j++) m += en[j]; if (m / w > 0.02) e.push(m / w); }
      e.sort((x, y) => x - y);
      const q = k => e[Math.min(e.length - 1, Math.floor(k * e.length))] || 0, g = a.genre || {};
      a._swells = e.length > 15 && g.style !== 'chill' && q(0.9) >= 0.65 && q(0.9) >= 1.45 * q(0.5);
    }
    return a._swells;
  }
  function showStyle() {
    const p = P(), g = S.analysis && S.analysis.genre;
    if (p && p.settings && STYLE_NAMES[p.settings.style]) return p.settings.style;
    if (noDrums()) return 'chill';                                   // nothing keeps time: a calm show (one that swells lifts its drops a little more)
    if (g && STYLE_NAMES[g.style]) return g.style;
    return ((S.analysis && S.analysis.mood && S.analysis.mood.drive) || 0.6) > 0.72 ? 'dance' : 'groove';
  }
  function autoBuild(quiet = false, keepSections = false) {
    if (!P()) return; if (!quiet) { S.building = true; beginChange(); S.building = false; } const p = P();
    p.built = { show: SHOW_VERSION, analysis: (S.analysis && S.analysis.version) ?? null, at: new Date().toISOString() };
    p.handEdited = false;
    if (!keepSections) p.sections = withBuildups(p.sections);      // (keepSections: the sections exactly as the user set them)
    for (const lane of p.lanes) lane.clips = []; p.paletteClips = [];
    while (p.lanes.length < DEFAULT_LANES) p.lanes.push(newLane());
    // one palette carries the whole song, chosen from its key, brightness and drive
    // A handful of palettes that suit this song, cycled and rotated section by section:
    // coherent across the track, but two drops never open on the same color.
    const mood = S.analysis.mood || {};
    S.songPals = (mood.palettes || [mood.palette]).filter(n => p.palettes[n]);
    // Jam mode's two colors (the jam's, or the ones picked for this track in the queue) come first
    S.pairMode = !S.noLimits && !!(S.jamColors && S.jamColors.length === 2);
    if (S.noLimits) { S.lookSeed = Math.round((S.analysis.duration || 0) * 10) + Math.round((S.analysis.bpm || 0) * 100);
      const dropPal = (wildPlan().find((x, i) => p.sections[i].type === 'drop') || wildPlan()[0] || { build: ['#ff0000', '#0030ff'] }).build;
      p.palettes['No limits'] = Effects.solidPalette(dropPal); S.songPals = ['No limits']; }
    else if (S.pairMode) { p.palettes.Jam = Effects.solidPalette(S.jamColors); S.songPals = ['Jam']; }
    else if (p.settings && p.settings.mainColor) { p.palettes.Main = mainPalette(p.settings.mainColor); S.songPals = ['Main']; }   // chosen by hand
    else if (S.albumPalette && S.albumPalette.length) { p.palettes.Album = Effects.solidPalette(S.albumPalette); S.songPals = ['Album']; }   // the cover's own colors
    if (!S.songPals.length) S.songPals = [p.defaultPalette];
    S.songPal = S.songPals[0];
    p.defaultPalette = S.songPal;
    // Each song gets its own kind of motion in the drops (not comets every time), chosen from the song
    // itself so it is the same on every rebuild, and a song that goes all out may get rainbow drops.
    const a0 = S.analysis || {};
    pickLook();
    S.epic = !!(S.pairMode && S.jamRainbow && isEpic());
    S.cute = cuteParts();
    const counts = {};
    // The song's own parts (songmap.py): a part heard again gets the look it had (the same drop move, motion,
    // bed and colors), so the room knows the chorus the moment it is back; a new part gets a new look. The
    // repeats grow, the last of them the song's peak (buildSection).
    const sp = sectionParts(p.sections), keyOf = i => sp[i] ? p.sections[i].type + ':' + sp[i] : null, total = {}, first = {}, seen = {}, variantOf = {};
    p.sections.forEach((s, i) => { const k = keyOf(i); if (k) { total[k] = (total[k] || 0) + 1; if (!(k in first)) first[k] = i; } });
    p.sections.forEach((s, i) => {
      const k = keyOf(i);
      let variant;
      if (k && k in variantOf) variant = variantOf[k];
      else { counts[s.type] = counts[s.type] || 0; variant = counts[s.type]++; if (k) variantOf[k] = variant; }
      if (k) seen[k] = (seen[k] || 0) + 1;
      S.secPart = k ? { key: k, occ: seen[k], of: total[k], first: first[k] } : null;
      buildSection(s, i, false, variant);
    });
    S.secPart = null;
    addKeyMoments();
    // drop markers that are not section starts still get a blast (a chill song never flashes, so it gets none)
    if (showStyle() !== 'chill') for (const d of p.drops) { const b = Math.round(timeToBeat(d)); if (!p.lanes.some(l => l.clips.some(c => c.effect === 'dropblast' && Math.abs(c.start - b) < 3))) clip(3, 'dropblast', b, 2); }
    addMoments();
    addFlavor();
    addLyricMoments();
    balance();
    if (S.review) S.review.clips = clipSig();                         // (a song under review: these lights are the built ones, not a hand edit)
    S.view.sel = null; inspector(); changed();
    if (quiet) return;
    const a = S.analysis, hooks = (a.hook && a.hook.times || []).length, fills = (a.fills || []).length;
    const g = S.analysis.genre;
    const bits = [`${STYLE_NAMES[showStyle()]} style${g && g.genres && g.genres.length && !(p.settings && p.settings.style) ? ` (${g.genres.slice(0, 2).join(', ')})` : ''}`, (S.sceneNames || []).join(' + '),
      `${p.sections.length} sections`, `palettes ${(S.songPals || []).join(' / ')}`];
    if (hooks) bits.push(`riff x${hooks}`);
    if (fills) bits.push(`${fills} fills`);
    if ((a.events && a.events.lead || []).length) bits.push(`${a.events.lead.length} lead hits`);
    toast('Built: ' + bits.filter(Boolean).join(' \u00b7 '));
  }
  /** Each section's part (songmap.py: the letter of the part it is, by most of its time), or null. */
  function sectionParts(sections) {
    const ps = (((S.analysis || {}).songmap || {}).parts) || [];
    return sections.map(s => {
      let best = null, most = 0;
      for (const q of ps) { const o = Math.min(s.end, q.end) - Math.max(s.start, q.start); if (o > most) { most = o; best = q; } }
      return best && most >= 0.5 * (s.end - s.start) ? best.part : null;
    });
  }
  /** The song's key moments (songmap.py: its top ten, ranked) as moves designed for each, in place of whatever
   *  small one-shot was near; and a breath before each release: half a beat of near-dark right before the
   *  music lands, as the studio hears it coming. Never in a calm song. */
  function addKeyMoments() {
    const p = P(), sm = (S.analysis || {}).songmap || {}; if (showStyle() === 'chill' || !p) return;
    const near = (fx, b, w) => p.lanes.some(l => l.clips.some(c => c.effect === fx && Math.abs(c.start - b) < w));
    const clearShots = b => { for (const l of p.lanes) l.clips = l.clips.filter(c => !(ONE_SHOTS.has(c.effect) && c.len <= 4 && Math.abs(c.start - b) < 2)); };
    const dropAt = b => p.sections.some(x => x.type === 'drop' && Math.abs(timeToBeat(x.start) - b) < 1);
    for (const r of ((sm.tension || {}).releases) || []) {
      const b = Math.round(timeToBeat(r.t));
      if (r.strength >= 0.35 && b >= 2 && !near('dark', b - 0.5, 1.5)) clip(7, 'dark', b - 0.5, 0.5);
    }
    for (const m of sm.moments || []) {
      const b = Math.round(timeToBeat(m.t)); if (b < 2) continue;
      const punch = { palette: [punchColor(b)], opacity: 1 }, k = m.kind || '';
      if (k === 'release' && dropAt(b)) { if (!near('roomsweep', b, 2)) clip(8, 'roomsweep', b, 2, { every: 2, width: 0.7 }, { blend: 'add', opacity: 0.9 }); continue; }
      if (k === 'the music stops' || k === 'the key changes') continue;          // (addMoments and addFlavor have these)
      clearShots(b);
      if (k === 'release' || k === 'the bass comes in') clip(9, 'colorslam', b, 2, {}, punch);
      else if (k === 'the biggest hit' || k === 'the drums come in') {
        if (!near('dark', b - 0.25, 1)) clip(7, 'dark', b - 0.25, 0.25);
        clip(9, 'whiteflash', b, 1.5, {}, { palette: ['#ffffff'], opacity: k === 'the biggest hit' ? 1 : 0.9 });
      } else if (k === 'the lead comes in') clip(9, 'glitch', b, 1.5, {}, punch);
      else clip(9, 'sweepout', b, 2, { origin: 0.5 }, { ...punch, opacity: 0.95 });   // the voice coming in, a new part
    }
  }
  // The words the lights know (lyrics.py): a color sung turns the room that color for a moment, a big word
  // gets its hit, and the hook line (the line sung most, three times or more) the same move every time.
  const LYRIC_COLORS = { red: '#ff0000', crimson: '#dc0030', scarlet: '#ff2000', cherry: '#e0002a', blue: '#0040ff', navy: '#0020a0',
    green: '#00ff40', emerald: '#00c060', purple: '#8000ff', violet: '#8a00ff', indigo: '#4000ff', pink: '#ff40a0', magenta: '#ff00c0',
    orange: '#ff6000', yellow: '#ffd000', gold: '#ffb000', golden: '#ffb000', silver: '#c0d0ff', white: '#ffffff', cyan: '#00e0ff',
    teal: '#00c0b0', neon: '#00ffd0', black: 'dark', rainbow: 'rainbow' };
  const LYRIC_HITS = {                                            // a big word: [effect, length in beats, its own colors or null]
    fire: ['colorslam', 2, ['#ff3000', '#ffa000']], fires: ['colorslam', 2, ['#ff3000', '#ffa000']], burn: ['colorslam', 2, ['#ff3000', '#ffa000']],
    burning: ['colorslam', 2, ['#ff3000', '#ffa000']], flame: ['colorslam', 2, ['#ff3000', '#ffa000']], flames: ['colorslam', 2, ['#ff3000', '#ffa000']],
    light: ['whiteflash', 1.5, ['#ffffff']], lights: ['whiteflash', 1.5, ['#ffffff']], shine: ['whiteflash', 1.5, ['#ffffff']], bright: ['whiteflash', 1.5, ['#ffffff']],
    star: ['sparkle', 2, ['#ffffff']], stars: ['sparkle', 2, ['#ffffff']], sky: ['sweepout', 2, null], fly: ['sweepout', 2, null], high: ['sweepout', 2, null],
    lightning: ['lightning', 1.5, ['#ffffff', '#8ad8ff']], thunder: ['lightning', 1.5, ['#ffffff', '#8ad8ff']], storm: ['lightning', 1.5, ['#ffffff', '#8ad8ff']],
    boom: ['whiteflash', 1, ['#ffffff']], bang: ['whiteflash', 1, ['#ffffff']], explode: ['whiteflash', 1.5, ['#ffffff']], fireworks: ['fireworks', 4, null],
    heart: ['colorslam', 1.5, ['#c00020']], heartbeat: ['colorslam', 1.5, ['#c00020']], jump: ['sweepout', 1.5, null], dance: ['sweepout', 1.5, null],
    sun: ['colorslam', 2, ['#ffb000', '#ff6000']], sunshine: ['colorslam', 2, ['#ffb000', '#ff6000']] };
  function addLyricMoments() {
    const p = P(), ly = (S.analysis || {}).lyrics; if (!p || !ly || !(ly.words || []).length) return;
    const norm = x => String(x || '').toLowerCase().normalize('NFKD').replace(/[^a-z0-9 ]+/g, '').replace(/\s+/g, ' ').trim();
    const ps = ly.words.map(w => w.p).filter(x => typeof x === 'number').sort((x, y) => x - y), pLow = ps.length >= 20 ? ps[Math.floor(ps.length * 0.1)] : -1;
    const busy = new Set([...ONE_SHOTS, ...PARTY_FLASHY, ...PARTY_ONE_SHOTS]), placed = [], lastOf = {};
    const free = (b, w = 1) => !placed.some(x => Math.abs(x - b) < 2) && !p.lanes.some(l => l.clips.some(c => c.len <= 4 && busy.has(c.effect) && Math.abs(c.start - b) < w));
    const at = t => Math.round(timeToBeat(t) * 4) / 4;
    const put = (b, fx, len, pal, key, params = {}) => {
      if (b < 1 || !free(b) || (key && lastOf[key] != null && b - lastOf[key] < 8)) return false;
      const calm = calmAt(beatToTime(b)) >= 0.5;                  // calm music: a soft sweep in the color (no flashes there)
      if (fx === 'dark') { if (calm) return false; clip(7, 'dark', b, 0.5); }
      else if (calm) clip(9, 'sweepout', b, Math.max(2, len), { origin: 0.5 }, { palette: pal || [punchColor(b)], opacity: 0.85 });
      else if (fx === 'sparkle') clip(9, 'sparkle', b, len, { density: 0.35, life: 0.25 }, { palette: pal, blend: 'add', opacity: 1 });
      else clip(9, fx, b, len, fx === 'sweepout' ? { origin: 0.5, ...params } : params, { palette: pal || [punchColor(b)], opacity: 1 });
      placed.push(b); if (key) lastOf[key] = b; return true;
    };
    // the hook line: the same move, in the same color, every time it is sung
    const count = {}; for (const ln of ly.lines || []) { const k = norm(ln.text); if (k.split(' ').length >= 2) count[k] = (count[k] || 0) + 1; }
    const hook = Object.entries(count).filter(([, n]) => n >= 3).sort((x, y) => y[1] - x[1] || y[0].length - x[0].length)[0];
    if (hook) {
      const times = (ly.lines || []).filter(ln => norm(ln.text) === hook[0]).map(ln => at(ln.t)), col = punchColor(times[0] || 0);
      for (const b of times) {
        if (b < 1 || !free(b)) continue;
        if (calmAt(beatToTime(b)) >= 0.5) clip(9, 'sweepout', b, 2, { origin: 0.5 }, { palette: [col], opacity: 0.85 });
        else clip(8, 'roomsweep', b, 2, { every: 2, width: 0.7 }, { palette: [col], blend: 'add', opacity: 0.9 });
        placed.push(b);
      }
    }
    // the words themselves, as they are sung (not the backing vocals, nor a word the aligner barely found)
    for (const w of ly.words) {
      if (w.bg || (typeof w.p === 'number' && w.p < pLow)) continue;
      const k = norm(w.w).replace(/ /g, ''), b = at(w.t);
      const col = LYRIC_COLORS[k];
      if (col === 'dark') put(b, 'dark', 0.5, null, k);
      // a color sung holds the room in it for a bar: 3 beats full, then a beat to fade (2 beats fading from the
      // start read as a blip: the user)
      else if (col === 'rainbow') put(b, 'colorslam', 4, ['#ff0000', '#ffd000', '#00ff40', '#0040ff', '#8000ff'], k, { hold: 0.75 });
      else if (col) put(b, 'colorslam', 4, [col], k, { hold: 0.75 });
      else if (LYRIC_HITS[k]) { const [fx, len, pal] = LYRIC_HITS[k]; put(b, fx, len, pal, k); }
    }
  }
  // Effects that are mostly dark with a few bright things moving (comets, sparks, sweeps): laid
  // over a base, they take the opposite colors, so a comet over red is cyan-blue and stands out.
  // (the stage steps, a bounce, a scanner, a chase and pulsar flares as the motion over a bed too: Knock Yourself
  // Out's drop had steps in the very red and blue its bed swapped between, so nothing was seen to move)
  const OVERLAY = new Set(['comet', 'dualcomet', 'meteors', 'orbit', 'sparkle', 'ghostspark', 'leadtrace', 'hookstrike',
                           'sweepout', 'ripple', 'colorslam', 'fillrush', 'pulse', 'steps', 'bounce', 'scanner', 'roomscan', 'chase', 'pulsar']);
  // Concert lighting in three tiers. The bed sets the color and the mood and stays low; motion keeps
  // things alive in the middle; the hits (drums, strikes, flashes) own the top of the range.
  const BED = new Set(['gradient', 'aurora', 'breathe', 'colorwash', 'wave', 'solid', 'ember', 'twinkle']);
  const HIT = new Set(['hitswap', 'pulse', 'hookstrike', 'flash', 'dropblast', 'whiteflash', 'colorslam', 'sweepout', 'ripple', 'strobe', 'fillrush', 'drumstrobe', 'drumhits', 'lightning', 'basshits', 'zonehits']);
  const FREE = new Set(['strobeRiser', 'buildhits', 'fadein', 'fadeout', 'dark', 'blackout', 'starlight', 'glitch', 'scramble', 'fillflood', 'meteorfall', 'tubeform']);   // set as placed: never tiered or balanced (a star is one bright segment)
  function tier(c) {
    // the base layer of a verse, intro, breakdown or outro is a bed whatever the effect: a room wave or
    // a color battle at full motion brightness made a verse outshine its drop
    if (BED.has(c.effect) || (S.bedBase && c === S.bedBase)) {
      // the bed's brightness is set here, whatever intensity the effect itself runs at
      const own = typeof c.params.intensity === 'number' && c.params.intensity > 0 ? c.params.intensity : 1;
      c.opacity = Math.min(1, (S.bedLevel || 0.35) / own);
      c.duck = S.bedDuck || 0;
    } else if (!HIT.has(c.effect) && !FREE.has(c.effect)) {
      c.opacity = Math.min(c.opacity ?? 1, 0.85);
      c.duck = 0.6 * (S.bedDuck || 0);                // motion gives way to the drums too, a little less
    }
  }
  /** A song's palette from one main color: it, and a neighbor on either side of the color wheel,
   *  all solid (the same shape as a palette taken from an album cover). */
  function mainPalette(hexColor) {
    const h = Effects.hueOf(Effects.solid(hexColor), 0);
    if (h === null) return [Effects.solid(hexColor)];              // white (or black): nothing to turn
    return Effects.solidPalette([Effects.hueHex(h), Effects.hueHex(h + 35 / 360), Effects.hueHex(h - 30 / 360)]);
  }
  /** No Limits Colors: every section in a palette of its own, from the colors a lighting designer reaches
   *  for. Singles (a breakdown all blue, a buildup all red), pairs that sit well together (red and blue,
   *  magenta and cyan, purple and orange, UV and green...), a few sets and the rainbow. How much each suits
   *  each kind of section (k), and whether it is warm: see wildPlan. */
  const WC = { red: '#ff0000', blue: '#0030ff', indigo: '#3000ff', purple: '#8000ff', uv: '#5000ff', magenta: '#ff0080', pink: '#ff40a0',
               cyan: '#00c8ff', teal: '#00ffc0', green: '#00ff40', amber: '#ffa000', orange: '#ff4000', gold: '#ffc000', white: '#ffffff',
               lime: '#80ff00', yellow: '#ffe000', violet: '#c000ff', sky: '#0090ff', rose: '#ff2050', aqua: '#00ffe0' };
  const WARM = new Set(['red', 'magenta', 'pink', 'amber', 'orange', 'gold', 'yellow', 'rose']);
  const PARTNER = { blue: 'magenta', indigo: 'cyan', purple: 'cyan', uv: 'green', cyan: 'magenta', teal: 'purple', green: 'purple', red: 'gold',
                    magenta: 'cyan', pink: 'cyan', amber: 'purple', orange: 'blue', gold: 'red', white: 'cyan',
                    lime: 'violet', yellow: 'purple', violet: 'lime', sky: 'orange', rose: 'aqua', aqua: 'rose' };   // the motion over a single
  const WILD = [
    { c: ['blue'], k: { intro: 1, verse: 1, breakdown: 1.2, outro: 1 } }, { c: ['indigo'], k: { intro: 1, verse: 0.6, breakdown: 1.2, outro: 1 } },
    { c: ['purple'], k: { intro: 0.8, verse: 1, breakdown: 1, outro: 0.8, buildup: 0.3 } }, { c: ['cyan'], k: { intro: 0.6, verse: 0.9, breakdown: 0.8, outro: 0.6 } },
    { c: ['green'], k: { verse: 0.7, breakdown: 0.5, buildup: 0.4 } }, { c: ['teal'], k: { intro: 0.5, verse: 0.7, breakdown: 0.7 } },
    { c: ['red'], k: { verse: 0.4, buildup: 1.3, drop: 0.3 } }, { c: ['magenta'], k: { verse: 0.6, buildup: 0.9, breakdown: 0.4 } },
    { c: ['amber'], k: { intro: 0.6, verse: 0.7, outro: 0.8, buildup: 0.6 } }, { c: ['white'], k: { buildup: 0.9 } },
    { c: ['pink'], k: { verse: 0.6, intro: 0.4 } }, { c: ['orange'], k: { buildup: 0.7, verse: 0.4 } },
    { c: ['red', 'blue'], k: { drop: 1.3, verse: 0.3 } }, { c: ['magenta', 'cyan'], k: { drop: 1.1, verse: 0.3 } },
    { c: ['purple', 'orange'], k: { drop: 1, verse: 0.3 } }, { c: ['uv', 'green'], k: { drop: 0.9 } }, { c: ['red', 'white'], k: { buildup: 0.4 } },
    { c: ['blue', 'white'], k: { breakdown: 0.3 } }, { c: ['cyan', 'amber'], k: { drop: 0.7, verse: 0.3 } },
    { c: ['blue', 'purple'], k: { verse: 0.8, breakdown: 0.6, intro: 0.5, outro: 0.5 } }, { c: ['red', 'gold'], k: { drop: 0.7 } }, { c: ['green', 'blue'], k: { drop: 0.5, verse: 0.5 } },
    { c: ['red', 'orange', 'gold'], k: { drop: 0.6 } }, { c: ['blue', 'cyan', 'teal'], k: { drop: 0.4, verse: 0.4, breakdown: 0.4 } },
    { c: ['magenta', 'cyan', 'gold'], k: { drop: 0.6 } }, { c: 'rainbow', k: { drop: 0.7, buildup: 0.2 } },
    // more singles
    { c: ['lime'], k: { verse: 0.5, buildup: 0.5 } }, { c: ['yellow'], k: { buildup: 0.6, verse: 0.3 } },
    { c: ['violet'], k: { intro: 0.6, verse: 0.8, breakdown: 0.8, outro: 0.6 } }, { c: ['sky'], k: { intro: 0.7, verse: 0.8, breakdown: 0.7, outro: 0.7 } },
    // more pairs a lighting designer reaches for
    { c: ['blue', 'orange'], k: { drop: 1, verse: 0.3 } }, { c: ['purple', 'cyan'], k: { drop: 0.9, verse: 0.4, breakdown: 0.3 } },
    { c: ['magenta', 'gold'], k: { drop: 0.8, verse: 0.3 } }, { c: ['red', 'cyan'], k: { drop: 0.8 } }, { c: ['teal', 'orange'], k: { drop: 0.7, verse: 0.4 } },
    { c: ['uv', 'pink'], k: { drop: 0.7, verse: 0.4, breakdown: 0.3 } }, { c: ['green', 'magenta'], k: { drop: 0.7 } }, { c: ['violet', 'lime'], k: { drop: 0.6 } },
    { c: ['sky', 'rose'], k: { drop: 0.6, verse: 0.4 } },
    { c: ['red', 'purple'], k: { drop: 0.6, verse: 0.3, buildup: 0.3 } }, { c: ['yellow', 'purple'], k: { drop: 0.6 } },
    // and three-color sets: sunset, Miami, galaxy, lava, aurora, tropical
    { c: ['orange', 'magenta', 'purple'], k: { drop: 0.6, verse: 0.2 } }, { c: ['pink', 'cyan', 'purple'], k: { drop: 0.6 } },
    { c: ['indigo', 'purple', 'magenta'], k: { drop: 0.5, breakdown: 0.4 } }, { c: ['red', 'orange', 'magenta'], k: { drop: 0.5, buildup: 0.3 } },
    { c: ['green', 'teal', 'violet'], k: { drop: 0.4, breakdown: 0.4 } }, { c: ['teal', 'orange', 'magenta'], k: { drop: 0.5 } },
    // fire, neon, acid and ice (white never leads a drop)
    { c: ['red', 'orange'], k: { drop: 0.7, buildup: 0.6 } }, { c: ['pink', 'lime'], k: { drop: 0.6 } }, { c: ['blue', 'lime'], k: { drop: 0.5 } },
    { c: ['cyan', 'white'], k: { breakdown: 0.4, buildup: 0.3 } },
  ];
  /** The song's plan: a palette for each of its sections, in order (the same on every rebuild). Each is drawn
   *  by how well it suits that kind of section and the song (a dark or minor song leans cool, a bright major
   *  one warm; rock to red, white and amber; electronic to UV, green, magenta and cyan; a calm song gets no
   *  rainbow, a rave more of it), never the one just before it, and less often one already used. The drops
   *  lean to palettes that share a color with the song's first drop, so they still feel like one song. */
  function wildPlan() {
    const p = P(), sig = p.sections.map(s => s.type).join(',') + '|' + (S.lookSeed || 0);
    if (S.wild && S.wild.sig === sig) return S.wild.plan;
    const a = S.analysis || {}, m = a.mood || {}, st = showStyle();
    const bright = m.brightness ?? 0.5, minor = m.mode === 'minor';
    const g = songGenres();
    const rocky = /rock|punk|grunge|metal|emo/.test(g), electronic = ELECTRONIC.test(g);
    const names = e => e.c === 'rainbow' ? ['rainbow'] : e.c;
    const mood = e => names(e).reduce((w, n) => {
      if (n === 'rainbow') return w * (st === 'chill' ? 0 : st === 'rave' ? 1.6 : 1);
      let f = WARM.has(n) ? (minor ? 0.75 : 1) * (0.7 + 0.6 * bright) : (minor ? 1.3 : 1) * (1.3 - 0.6 * bright);
      // (the genre only nudges, 10%: the colors are not meant to follow the genre much)
      if (rocky) f *= ['red', 'white', 'amber', 'orange', 'gold', 'yellow', 'rose'].includes(n) ? 1.1 : ['green', 'teal', 'uv', 'cyan', 'lime', 'aqua', 'violet'].includes(n) ? 0.9 : 1;
      if (electronic) f *= ['uv', 'green', 'magenta', 'cyan', 'purple', 'violet', 'lime', 'aqua', 'pink'].includes(n) ? 1.1 : 1;
      return w * Math.pow(f, 1 / names(e).length);
    }, st === 'chill' && names(e).length > 2 ? 0.3 : 1);
    let n = 0; const rnd = () => Effects.hash((S.lookSeed || 0) * 31 + 7907 * ++n);
    const plan = [], used = new Map();
    let firstDrop = null;
    for (const s of p.sections) {
      const prev = plan.length ? plan[plan.length - 1].e : null;
      const scored = WILD.map(e => {
        let w = (e.k[s.type] ?? 0.05) * mood(e);
        if (s.type === 'drop' && names(e).includes('white')) w = 0;      // white never leads a drop
        if (e === prev) w = 0;
        w *= Math.pow(0.6, used.get(e) || 0);
        if (s.type === 'drop' && firstDrop && e !== firstDrop && names(e).some(x => names(firstDrop).includes(x))) w *= 2.5;
        return [e, w > 0 ? Math.pow(rnd(), 1 / w) : -1];
      }).sort((x, y) => y[1] - x[1]);
      const e = scored[0][0];
      if (s.type === 'drop' && !firstDrop) firstDrop = e;
      used.set(e, (used.get(e) || 0) + 1);
      const build = e.c === 'rainbow' ? Effects.PALETTES.Rainbow.slice() : e.c.map(x => WC[x]);
      const over = s.type === 'buildup' ? build.slice() : e.c === 'rainbow' ? build.slice().reverse() : e.c.length === 1 ? [WC[PARTNER[e.c[0]]]] : build.slice().reverse();
      plan.push({ e, build, over });
    }
    S.wild = { sig, plan };
    return plan;
  }
  /** The opposite hue of each color (white and black stay as they are), as solid colors. */
  function contrastPalette(cols) {
    return Effects.solidPalette((cols || []).map(hexColor => {
      const h = Effects.hueOf(hexColor);
      return h === null ? hexColor : Effects.hueHex(h + 0.5);        // half way round the color wheel
    }));
  }
  // In the jam's two colors, a few of these small accents now and then take the jam's extra colors
  // (orange and red at Halloween): little extras, never the base, the comets or the drums.
  const EXTRAS = new Set(['hookstrike', 'sweepout', 'colorslam', 'ghostspark', 'fillrush', 'sparkle', 'meteors', 'ripple', 'whiteflash']);
  const extraHere = (effect, start) => S.pairMode && S.jamExtras && EXTRAS.has(effect) && Effects.hash(Math.round(start * 4) * 7919 + effect.length * 104729) < 0.35;
  /** Where an effect that opens from a point starts, mixed up clip by clip (the same on every rebuild):
   *  the middle a little under half the time, else an end or a quarter of the way along. */
  function originFor(effect, start) {
    const e = Effects.byId[effect]; if (!e || !e.params.origin) return {};
    const h = Effects.hash(Math.round(start * 4) * 7919 + effect.length * 131 + effect.charCodeAt(0) * 17 + (S.lookSeed || 0));
    return { origin: h < 0.4 ? 0.5 : [0, 1, 0.25, 0.75][Math.floor((h - 0.4) / 0.6 * 4)] };
  }
  // Black as a color: in the moving two-color effects (a chase, halves, a color battle...) one of the two is
  // black now and then, so the light walks in and out of the dark; in a rave more often, never in a calm song.
  const BLACKABLE = new Set(['chase', 'hyperchase', 'halves', 'roomsplit', 'battle', 'blocks', 'wave', 'steps']);
  function withBlack(effect, start, pal) {
    if (!BLACKABLE.has(effect) || !pal || !pal.length) return pal;
    const st = showStyle(); if (st === 'chill') return pal;
    const h = Effects.hash(Math.round(start * 4) * 104729 + effect.length * 7 + (S.lookSeed || 0) + 3);
    return h < (st === 'rave' ? 0.55 : 0.3) ? [pal[0], '#000000'] : pal;
  }
  // Motion over a base in the very same colors does nothing (meteors over a gradient of the same pair): it
  // takes the jam's extra colors then, or in about a third of the sections white (white moving over a
  // color looked great in Mission's drop), else a color apart from the base. Never white everywhere: every
  // drop's moving things in white made white the drops' own color.
  const whiteMotion = start => Effects.hash(Math.round(start / 16) * 7907 + (S.lookSeed || 0) + 11) < 0.33;
  const sameColors = (a, b) => { const B = new Set((b || []).map(x => String(x).toLowerCase())); return !!(a && a.length) && a.every(x => B.has(String(x).toLowerCase())); };
  /** How a sweep moves, mixed up clip by clip (the same on every rebuild): steady a third of the time, else
   *  smooth, a rush through the middle, hanging in the middle, a bounce, ramping up or down; a calm song's
   *  only steady or smooth. Effects without the setting keep theirs. */
  function easeFor(effect, start) {
    const e = Effects.byId[effect]; if (!e || !e.params.ease) return {};
    const h = Effects.hash(Math.round(start * 4) * 6271 + effect.length * 59 + (S.lookSeed || 0) + 23);
    if (showStyle() === 'chill') return { ease: h < 0.6 ? e.params.ease.def : 'smooth' };
    const pick = [['linear', 0.3], ['smooth', 0.2], ['rush', 0.2], ['drift', 0.1], ['bounce', 0.1], ['accel', 0.05], ['decel', 0.05]];
    let acc = 0; for (const [k, w] of pick) { acc += w; if (h < acc) return { ease: k }; }
    return { ease: 'linear' };
  }
  function clip(lane, effect, start, len, params = {}, extra = {}) { const c = { id: uid(), effect, start, len, params: { ...Effects.defaults(effect), ...originFor(effect, start), ...easeFor(effect, start), ...params }, opacity: 1, blend: 'normal', fadeIn: 0, fadeOut: 0, palette: (extraHere(effect, start) ? Effects.solidPalette(S.jamExtras) : null) || (S.pairMode || S.noLimits ? null : effectColors(effect)) || (lane > 0 && OVERLAY.has(effect) ? S.contrastPal : S.buildPal) || null, ...extra }; if (!('palette' in extra)) { c.palette = withBlack(effect, start, c.palette);
      if (lane > 0 && OVERLAY.has(effect) && effect !== 'pulse' && sameColors(c.palette, S.buildPal)) c.palette = S.jamExtras ? Effects.solidPalette(S.jamExtras) : whiteMotion(start) ? ['#ffffff'] : [punchColor(start)]; }
    if (S.sectionClips && !S.sectionClips.length && S.bedSectionNow && lane === 0) S.bedBase = c; tier(c); P().lanes[freeLaneFor(start, len, lane, null, c.target)].clips.push(c); if (S.sectionClips) S.sectionClips.push(c); return c; }
  function buildSection(s, idx, clearFirst, variant = 0) {
    const p = P(); const b0 = Math.round(timeToBeat(s.start)), b1 = Math.round(timeToBeat(s.end)); const len = b1 - b0; if (len < 1) return;
    if (clearFirst) { for (const lane of p.lanes) lane.clips = lane.clips.filter(c => c.start + c.len <= b0 || c.start >= b1); }
    const fam = S.songPals && S.songPals.length ? S.songPals : [p.defaultPalette];
    // Two colors (Jam mode): the primary leads, the secondary is "the other one". Nothing else comes
    // in: the opposite of the pair is the pair swapped, not the opposite side of the color wheel.
    // (The bright hits take the secondary, so it shows the most: tried the other way round, 2026-10-21, and
    // put back the same night; the user loved it as it was.)
    const pair = S.pairMode && p.palettes.Jam;
    const pidx = S.secPart ? S.secPart.first : idx;                   // (a part heard again: the colors it had)
    S.buildPal = pair ? p.palettes.Jam.slice() : paletteVariant(fam[pidx % fam.length], pidx);
    const opposite = pair ? (cols => cols.slice().reverse()) : contrastPalette;
    // Buildups and breakdowns run on the opposite colors, so the drop lands back on the song's
    // own palette and the change of color hits with it.
    if (s.type === 'buildup' || s.type === 'breakdown') S.buildPal = opposite(S.buildPal);
    S.contrastPal = opposite(S.buildPal);
    if (S.noLimits) { const sp = wildPlan()[pidx]; if (sp) { S.buildPal = Effects.solidPalette(sp.build); S.contrastPal = Effects.solidPalette(sp.over); } }
    const chillStyle = showStyle() === 'chill';
    S.bedLevel = ({ drop: 0.4, buildup: 0.35 }[s.type] || 0.3) + (chillStyle ? 0.1 : 0);
    S.bedDuck = chillStyle ? 0.35 : ({ drop: 0.7, verse: 0.55, buildup: 0.5 }[s.type] || 0.4);
    // No Limits: a section in one bold color (a while of yellow) pumps harder with the drums, so it moves
    if (S.noLimits && !chillStyle) S.bedDuck = Math.min(0.9, S.bedDuck + 0.2);
    const v = variant % 2;
    // Two rules shape everything here.
    //  1. Dark space: dim bases with a few bright things moving over them.
    //  2. The lights follow the song. Intros, breakdowns and outros get slow fades
    //     and drifting comets and never flash. How hard the rest goes depends on the
    //     kind of music (see genre.py): a chill song never strobes, a groove song
    //     moves on the beat but lifts its drops without flashing, dance drops flash,
    //     and rave goes all the way.
    const style = sectionStyle(s);                                   // the song's style, a step either way by how loud this part is
    const hard = style === 'dance' || style === 'rave', chill = style === 'chill';
    const rainbow = S.albumPalette || S.pairMode || S.noLimits ? {} : { palette: 'Rainbow' };      // with the album's (or the jam's) colors, drops stay in them
    // Two or more lights: some moves go around the room, light to light, instead of along each strip.
    const room = showLights().length >= 2;
    // The beat pulse follows the real drums (kick from the center, snare from the ends), and it is
    // only placed where there are drums to follow: a section with at least 4 kick or snare hits and
    // one every 4 beats. When the song's drums are not known yet, the quiet kinds of section get none.
    const ev = (S.analysis && S.analysis.events) || {};
    const drumsKnown = (ev.kick || []).length > 0 || noDrums();
    const hitsHere = drumsKnown ? [...(ev.kick || []), ...(ev.snare || [])].filter(e => e[0] >= s.start && e[0] < s.end).length : 0;
    // With the drums separated out, whether they are playing is known moment by moment: a section
    // has drums when the drum track plays through most of it (a drumless intro is 0 however loud).
    const present = ((S.analysis && S.analysis.curves) || {}).drums_present;
    const playing = present && present.length ? (() => { const fps = S.analysis.feature_fps || 30; const i0 = Math.floor(s.start * fps), i1 = Math.min(present.length, Math.ceil(s.end * fps));
      let on = 0; for (let i = i0; i < i1; i++) on += present[i] >= 0.3; return i1 > i0 ? on / (i1 - i0) >= 0.5 : false; })() : true;
    const drumsHere = !noDrums() && playing && (drumsKnown ? hitsHere >= Math.max(4, len / 4) : !['intro', 'breakdown', 'outro'].includes(s.type));
    const drums = (opacity, size = 0.75) => drumsHere && clip(2, 'pulse', b0, len, { follow: 'drums', decay: 0.45, size }, { opacity, blend: 'add' });
    const snaresHere = drumsKnown ? [...(ev.snare || []), ...(ev.roll || [])].filter(e => e[0] >= s.start && e[0] < s.end).length : 0;
    // the drum strobe: flashes on the real hits only; how hard depends on the kind of song
    const strobeLevel = { rave: 0.9, dance: 0.45, groove: 0.3 }[style] || 0;
    // the song's motion over a drop: pace 1 is a normal drop, below 1 slower
    let mAt = b0, mLen = len, mVar = variant;                         // where motion() and top() place (a long drop's later phrases: see the drop)
    const motion = (pace = 1, extra = {}) => {
      const fam = (S.motionFams && s.type === 'drop' ? S.motionFams[mVar % 2] : S.motionFam) || 'comets', sp = x => Math.max(0.5, Math.round(x / pace * 4) / 4);   // on quarter beats: 2 / 1.3 is 1.5, not 1.538…
      // (a groove song's sparkles glint, slow and sparse, in the section's own colors: dense quick twinkles in a
      // color apart flickered purple all over Wicked's green drop; dance and rave keep them quick and bright)
      if (fam === 'sparkles') return hard ? clip(1, 'sparkle', mAt, mLen, { density: 0.2 * pace + 0.05, life: 0.15 }, { opacity: 0.95, ...extra })
        : clip(1, 'sparkle', mAt, mLen, { density: 0.08 * pace + 0.03, life: 0.45 }, { opacity: 0.9, palette: (S.buildPal || []).slice(), ...extra });
      if (fam === 'meteors') return clip(1, 'meteors', mAt, mLen, { every: sp(1), chance: 0.6, travel: sp(2), tail: 0.3 }, { opacity: 0.95, ...extra });
      if (fam === 'orbit') return clip(1, 'orbit', mAt, mLen, { speed: sp(2), count: 3 }, { opacity: 0.9, ...extra });
      if (fam === 'ripples') return clip(1, 'ripple', mAt, mLen, { every: sp(1), travel: sp(1.5), width: 0.12 }, { opacity: 0.9, ...extra });
      if (fam === 'bounce') return clip(1, 'bounce', mAt, mLen, { speed: sp(2), tail: 0.35 }, { opacity: 0.9, ...extra });
      if (fam === 'scanner' && acrossRooms()) return clip(1, 'roomscan', mAt, mLen, { speed: sp(8), width: 0.7 }, { opacity: 0.9, ...extra });   // slower, across both rooms
      if (fam === 'scanner') return clip(1, 'scanner', mAt, mLen, { speed: sp(2), size: 0.15, floor: 0 }, { opacity: 0.85, ...extra });
      if (fam === 'pulsar') return clip(1, 'pulsar', mAt, mLen, { every: sp(1), count: 2, size: 0.08, decay: 0.5 }, { opacity: 0.95, ...extra });
      if (fam === 'steps') return clip(1, 'steps', mAt, mLen, { zones: 8, every: sp(1), decay: Math.max(0.5, sp(1)) * 0.8, order: S.stepOrder || 'run' }, { opacity: 0.95, ...extra });
      return clip(1, 'dualcomet', mAt, mLen, { speed: sp(2), tail: 0.35 }, { opacity: 0.9, ...extra });
    };
    const look = S.mix;                                                // the song's own mix (pickLook)
    // a scanner goes across both rooms (slowly) in about half the sections that have one, when there are rooms to cross
    const acrossRooms = () => room && Effects.hash(b0 * 31 + 5 + (S.lookSeed || 0)) < 0.5;
    // long strips (more than a dozen segments) run the length of a ceiling: room for hits on part of them
    const longStrips = showLights().some(l => lightKind(l) === 'long');
    let zoneBuild = false;
    // A long verse or breakdown (over 16 bars) is built in 8-bar stretches that alternate between the
    // look's two verse styles, so two minutes the analysis calls one verse do not look the same.
    if ((s.type === 'verse' || s.type === 'breakdown') && len > 64 && !s.chunk) {
      for (let k = 0, c0 = b0; c0 < b1; k++, c0 += 32) {
        const c1 = b1 - (c0 + 32) < 16 ? b1 : c0 + 32;                      // no stub of under four bars at the end
        buildSection({ ...s, start: beatToTime(c0), end: beatToTime(c1), chunk: true }, idx, false, variant + k);
        if (c1 === b1) break;
      }
      return;
    }
    const first = S.sectionClips = [];
    S.bedBase = null; S.buildKind = null;
    S.bedSectionNow = ['intro', 'verse', 'breakdown', 'outro'].includes(s.type);
    const top = (spec, extra = {}) => spec && clip(1, spec[0], mAt, mLen, spec[1], { opacity: 0.85, ...(spec[2] || {}), ...extra });
    // under a calm song's stars, a soft color rather than colored twinkles of its own
    const cute = !chill && (S.cute || []).some(([t0, t1]) => s.start >= t0 - 0.01 && s.start < t1), stars = chill || cute;   // a calm song, or a cute part of another
    const starBed = spec => stars && spec[0] === 'twinkle' ? ['colorwash', { speed: 16, intensity: 0.3 }] : spec;
    switch (s.type) {
      case 'intro':
        clip(0, starBed(look.intro)[0], b0, len, starBed(look.intro)[1]);
        if (stars) clip(1, 'starlight', b0, len, { density: 0.08, life: 4, tint: S.starTint || 'white' }, { blend: 'add' });   // a calm song: the stars come out
        else top(S.drift, { opacity: ((S.drift || [])[2] || {}).opacity || 1 });
        drums(0.45, 0.55);
        clip(3, 'fadein', b0, Math.min(8, len)); break;
      case 'verse':
        if (look.verse[v][0] === 'roomwave' && !room) clip(0, 'wave', b0, len, { speed: 8, waves: 1, floor: 0.1 });
        else clip(0, look.verse[v][0], b0, len, chill && look.verse[v][0] === 'roomwave' ? { ...look.verse[v][1], speed: 16 } : look.verse[v][1]);
        if (stars) clip(1, 'starlight', b0, len, { density: 0.12, life: 3, tint: S.starTint || 'white' }, { blend: 'add' });   // single white stars, twinkling here and there
        else if (look.verseTop[v] && look.verseTop[v][0] === 'scanner' && acrossRooms()) clip(1, 'roomscan', b0, len, { speed: 16, width: 0.7 }, { opacity: 0.75 });
        else top(look.verseTop[v]);                                     // drop material (dual comets, strobes) stays out of verses
        drums(chill ? 0.7 : 0.95);
        if (!chill && first[0]) first[0].bassPump = 0.2;                 // (the strings breathe with the bass line in a verse too)
        if (!chill && drumsHere && longStrips && snaresHere >= len / 8)   // the snare lands on a quarter of the ceiling, in turn
          clip(5, 'zonehits', b0, len, { follow: 'snare', mode: 'quarters', decay: 0.3 }, { opacity: 0.4, blend: 'add', ...(S.mixedLights ? { target: 'long' } : {}) });
        break;
      case 'buildup':
        // every crazy snare build, and every other buildup of a hard song, jumps around the room
        zoneBuild = hard && len >= 8 && longStrips && (snaresHere >= 8 || (style === 'rave' && variant % 2 === 0));
        // a soft glow under the build that brightens toward the drop (it used to breathe down to black
        // every bar, drums or not); the hits on top, following the snares where they pick up, build it
        // (a clear EDM snare build has its white strobe alone: nothing under it or over it, see below)
        const strobeBuild = !chill && len >= 8 && edmBuild(s);
        // A long build (Passion Pit's Seaweed Song: 64 beats) only strobes for its last 16 beats, the run into
        // the drop; before that it is a build like any other (the glow, the hits around the room, the song's
        // own kind of build). 36 s of white strobe alone was all the room got.
        const sLen = strobeBuild ? Math.min(len, 16) : 0, nb = b1 - sLen, nlen = nb - b0;   // (the normal part: b0..nb)
        // The song's kind of buildup (its scene's, two in turn; see pickLook), all of them hits around the room:
        //   room   the glow under the hits brightens toward the drop
        //   dark   the hits light the room out of the dark, a glow coming up only in the last quarter
        //   scan   a light crosses the room under the hits, twice as fast every quarter
        //   climb  stage steps run along the strips under the hits, twice as fast every quarter
        //   poles  the corner poles call and answer (kick one side, snare the other), then flash together
        const kinds = S.builds && S.builds.length ? S.builds : ['room'];
        let bk = chill || len < 8 ? 'room' : kinds[variant % kinds.length];
        if (bk === 'poles' && !S.mixedLights) bk = 'room';
        const normal = nlen >= 8;                                   // (a short strobe build has no normal part)
        const nEnd = sLen ? nb : b1 - 1;                             // where the normal part's moves stop
        if (normal && bk === 'dark') {
          const q3 = b0 + Math.round(nlen * 0.75);
          clip(1, 'colorwash', q3, nb - q3, { speed: 8, intensity: 0.4 }, { blend: 'add', fadeIn: Math.max(1, nb - q3 - 1) });
        } else if (normal) {
          clip(0, 'colorwash', b0, nlen, { speed: 8, intensity: bk === 'room' ? 0.3 : 0.2 });
          clip(1, 'colorwash', b0, nlen, { speed: 8, intensity: bk === 'room' ? 0.45 : 0.3 }, { blend: 'add', fadeIn: Math.max(1, Math.round(nlen * 0.85)) });
        }
        if (normal && (bk === 'scan' || bk === 'climb')) {
          const q = Math.max(1, Math.round(nlen / 4)), across = room && bk === 'scan';
          for (let k = 0, c0 = b0; k < 4 && c0 < nEnd; k++, c0 += q) {
            const ln = k === 3 ? nEnd - c0 : Math.min(q, nEnd - c0), f = Math.pow(2, k);
            if (ln <= 0) break;
            const op = { opacity: 0.45 + 0.15 * k, blend: 'add' };
            if (bk === 'climb') clip(2, 'steps', c0, ln, { zones: 8, every: Math.max(0.25, 2 / f), decay: Math.max(0.25, 2 / f) * 0.8, order: 'run' }, op);
            else if (across) clip(2, 'roomscan', c0, ln, { speed: Math.max(2, 16 / f), width: 0.6, ease: 'linear' }, op);
            else clip(2, 'scanner', c0, ln, { speed: Math.max(0.5, 4 / f), size: 0.12, ease: 'linear' }, op);
          }
        }
        // The build is felt all around the room: every snare (else every hit, else a build of beats,
        // eighths and sixteenths) lights random stretches of the strips and lamps, more of them,
        // brighter and snappier as it goes. A hard song's crazier builds start out in the dark, so the
        // light jumps around the room; a calm song's build is gentle, on the beat, two at most.
        const follow = chill ? 'beat' : snaresHere >= 4 ? 'snare' : drumsHere ? 'drums' : 'build';
        if (normal) clip(3, 'buildhits', b0, nlen, chill ? { follow, most: 2, decay: 0.6, zones: 5 } : { follow, most: style === 'rave' ? 4 : 3, decay: 0.22, darkFor: zoneBuild || bk === 'dark' ? 0.5 : 0 }, { opacity: 1 });
        S.buildKind = normal ? bk : 'strobe';
        // A clear EDM snare build (a steady snare that doubles and doubles again: quarters, eighths, sixteenths)
        // in a dance, rave or electronic song: the whole room strobes white on every snare, faint at first, full
        // into the drop, and nothing else: black between the snares, or the flashes are lost (strobeAlone).
        // Gold Skies' two builds (1, 1, 2, 3-5 snares a beat by quarter); 3 of 55 learned builds.
        if (strobeBuild) clip(4, 'drumstrobe', normal ? nb : b0, (normal ? sLen : len) - 1, { follow: 'snare', white: true, grows: true, length: 0.12 }, { blend: 'add', opacity: 1, alone: true });
        // the concert move: one beat of black at the top of the buildup, and the drop lands out of the dark
        if (!chill && len >= 8 && p.sections[idx + 1] && p.sections[idx + 1].type === 'drop') clip(7, 'dark', b0 + len - 1, 1);
        // and the build's last bar crosses the room (left pole, along the ceiling, right pole), twice, into the drop
        if (!chill && len >= 8 && !strobeBuild) clip(8, 'roomsweep', b0 + len - 5, 4, { every: 2, width: 0.7 }, { blend: 'add', opacity: 0.95 });
        break;
      case 'drop': {
        const kind = variant % 4;
        // A hardcore drop (dubstep, bass music, riddim, brostep, drum and bass, hardstyle) goes crazier: the whole
        // room in pure, solid colors (red, blue...) flipping to the other on every drum hit, the song's motion on
        // top faster and in the same pure colors, the white flash on every kick, the sixteenth-note strobe every
        // 4 bars (not 8), a glitch half way through each 4, and the chop between the hits.
        const hardcore = !chill && drumsHere && heavyDrops();
        if (hardcore) {
          // pure primaries, one pair for all of the song's drops (its order swapping drop to drop); the party's
          // own pair when the jam has one
          const PURE = [['#ff0000', '#0000ff'], ['#ff00ff', '#00ff00'], ['#00ffff', '#ff0000'], ['#0000ff', '#ffff00']];
          const pair = S.pairMode ? (S.buildPal || []).slice(0, 2) : PURE[Math.floor(Effects.hash((S.lookSeed || 0) + 4211) * PURE.length)];
          const pal = variant % 2 ? pair.slice().reverse() : pair;
          S.buildPal = Effects.solidPalette(pal); S.contrastPal = Effects.solidPalette(pal.slice().reverse());
          clip(0, 'hitswap', b0, len, { punch: 0.5 }, { palette: Effects.solidPalette(pal) });   // (slams bright on each hit, sinks between)
          motion(style === 'rave' ? 1.6 : 1.3);
          // and a hyperchase racing over it in the other pure color (blue tearing across red)
          clip(2, 'hyperchase', b0, len, { speed: 0.25, size: 0.08 }, { palette: S.contrastPal, opacity: 1 });
          for (let b = b0 + 8; b + 1.5 <= b1; b += 16) clip(9, 'glitch', b, 1.5, {}, { palette: [punchColor(b)], opacity: 1 });
        } else if (S.epic) {                                            // all out: the whole room in a running rainbow
          clip(0, 'gradient', b0, len, { speed: 4, stretch: 0.67, intensity: 0.85 }, { palette: 'Rainbow' });
          motion(1.5);
        } else {
          // The song's drop moves (the first three it drew that fit), one per drop, in turn. A chill or
          // groove song lifts its drops with motion rather than flashing: the flashy ones are for dance and rave.
          // (a calm scene's livelier drops, `lift`, are for songs that are not calm)
          // (the whole room swapping its two colors on every hit, hitswap, is a hard drop's move: dance and rave,
          // or a heavy scene's; carrying a groove drop alone it was two colors flashing, Knock Yourself Out)
          const heavy = !!(SCENES[S.look] || {}).dark;
          const fit = look.drops.filter(r => (hard || (!r.flash && !r.fast)) && (!r.rave || style === 'rave') && (room || !r.room) && (!chill || !r.lift)
            && (r.base[0] !== 'hitswap' || hard || heavy));
          // the song's own scene leads: its first and third drops are that scene's, the second a guest's (a song
          // heard as pop punk, pop rock and alternative rock alike got pop rock's waves in every drop)
          // (a calm scene's livelier drops first when the song is not calm: Unholy is R&B, but no ballad)
          const own = new Set((SCENES[S.look] || {}).drops || []), lifts = arr => chill ? arr : [...arr.filter(r => r.lift), ...arr.filter(r => !r.lift)];
          const mine = lifts(fit.filter(r => own.has(r))), guest = lifts(fit.filter(r => !own.has(r)));
          const ok = [...new Set([mine[0], guest[0], mine[1], ...fit].filter(Boolean))].slice(0, 3);
          // A long drop (over 16 bars) changes look on the phrase: in two or three stretches of whole 4-bar
          // phrases, each the song's next drop move and its other motion (a minute of one look went stale)
          const parts = len > 64 && ok.length > 1 ? Math.min(3, Math.round(len / 48)) : 1, edge = k => k >= parts ? b1 : b0 + Math.round(k * len / parts / 16) * 16;
          for (let k = 0; k < parts; k++) {
            const c0 = edge(k), c1 = edge(k + 1); if (c1 <= c0) continue;
            const r = ok[(kind + k) % ok.length] || look.drops.find(r => !r.flash && !r.fast && !r.room && (!chill || !r.lift)) || SCENES.dreamy.drops[0];
            mAt = c0; mLen = c1 - c0; mVar = variant + k;
            clip(0, r.base[0], c0, c1 - c0, chill ? { ...r.base[1], speed: Math.max(r.base[1].speed || 8, 8) } : r.base[1]);
            const grow = 1 + 0.12 * Math.min(3, ((S.secPart || {}).occ || 1) - 1);   // (each time the part is back, a little quicker)
            if (r.top === 'motion') motion((chill ? (swells() ? 0.75 : 0.5) : style === 'rave' ? 1.3 : 1) * grow);
            // a dance drop's flashes come every other beat and softer: flashing on every beat is rave's
            else if (style === 'dance' && r.flash && Array.isArray(r.top)) top([r.top[0], 'rate' in r.top[1] ? { ...r.top[1], rate: r.top[1].rate * 0.6 } : { ...r.top[1], every: Math.max(2, r.top[1].every || 1) },
                                                                                   { ...(r.top[2] || {}), opacity: ((r.top[2] || {}).opacity || 1) * 0.6 }]);
            else top(r.top);
          }
          mAt = b0; mLen = len; mVar = variant;
        }
        if (chill) clip(4, 'starlight', b0, len, { density: 0.22, life: 2, tint: S.starTint || 'white' }, { blend: 'add' });   // a calm song's lift: more stars, a little quicker
        // the last time a chorus comes back is the song's peak: sparkle over everything, a blinder on each phrase
        if (!chill && S.secPart && S.secPart.of >= 2 && S.secPart.occ === S.secPart.of) {
          const had = P().lanes.flatMap(l => l.clips).filter(c => c.effect === 'sparkle' && c.start < b0 + len && c.start + c.len > b0);
          if (had.length) for (const c of had) c.params.density = Math.max(c.params.density || 0, 0.28);   // (the look has sparkle: more of it)
          else clip(4, 'sparkle', b0, len, { density: 0.18, life: 0.15 }, { blend: 'add', opacity: 0.55 });
          for (let bt = b0 + 32; bt < b0 + len - 4; bt += 32) clip(9, 'whiteflash', bt, 1, {}, { palette: ['#ffffff'], opacity: 0.8 });
        }
        drums(chill ? 0.8 : 1, 0.85);
        // the bed breathes with the bass line (a wobble, a bass that drops out for a beat)
        const pump = chill ? 0 : style === 'rave' ? 0.5 : style === 'dance' ? 0.4 : 0.3;
        if (pump && !hardcore) for (const c of first) if (BED.has(c.effect) || c === first[0]) c.bassPump = pump;   // (a hardcore drop's colors stay solid)
        // A drop built on a clear, rhythmic bass line (most beats have a bass hit, but not every eighth:
        // a constant eighth-note bass would be a steady strobe) flashes with the bass, like a hand-made
        // show would; then it leaves the kick strobe out, so the two do not fight.
        const bassHits = ((ev.basshit || []).filter(e => e[0] >= s.start && e[0] < s.end)).length / Math.max(1, len);
        const bassy = !chill && bassHits >= 0.6 && bassHits <= 1.7;
        if (bassy) clip(5, 'basshits', b0, len, { decay: 0.3 }, { opacity: style === 'rave' ? 0.8 : style === 'dance' ? 0.6 : 0.45, blend: 'add', ...(S.mixedLights ? { target: 'long' } : {}) });
        else if (drumsHere && strobeLevel && longStrips && style === 'rave' && variant % 2)   // the ceiling split: kick on one half, snare on the other
          clip(5, 'zonehits', b0, len, { follow: 'both', mode: 'halves', decay: 0.14 }, { opacity: strobeLevel, blend: 'add', ...(S.mixedLights ? { target: 'long' } : {}) });
        else if (drumsHere && strobeLevel && longStrips && (style === 'groove' || style === 'dance'))     // a fifth of the strip on each snare
          clip(5, 'zonehits', b0, len, { follow: 'snare', mode: 'each', zones: 5, decay: 0.25 }, { opacity: style === 'dance' ? 0.65 : 0.55, blend: 'add', ...(S.mixedLights ? { target: 'long' } : {}) });
        else if (drumsHere && strobeLevel) clip(5, 'drumstrobe', b0, len, { follow: 'kick', length: 0.05 }, { opacity: strobeLevel, blend: 'add' });   // flashes on the kicks themselves
        if (chill) clip(3, 'fadein', b0, Math.min(4, len)); else clip(3, 'dropblast', b0, 2);
        // a rave goes jumpy and insane: a white flash on every kick over whatever else plays, a half beat of
        // black before every 4-bar phrase, and the last bar of each 8 a sixteenth-note white strobe with black
        // between the flashes
        // (a hardcore drop's kick flashes the other pure color, solid, not white: white washed its colors out)
        if ((style === 'rave' || hardcore) && drumsHere) clip(6, 'drumstrobe', b0, len, { follow: 'kick', white: !hardcore, length: hardcore ? 0.06 : 0.04 },
          hardcore ? { opacity: 1, palette: S.contrastPal } : { opacity: 0.7, blend: 'add' });
        // a heavy drop (dubstep, bass music, riddim...): every other 4 bars chopped by the drums, black between
        // the hits (the drop's first 4 bars land open); its one-shots still punch through on top
        if (!chill && drumsHere && len >= 16 && heavyDrops())
          for (let b = b0 + 16; b < b1; b += 32) clip(8, 'drumgate', b, Math.min(16, b1 - b), {}, { opacity: 1 });
        if ((style === 'rave' || hardcore) && len >= 16) for (let b = b0 + 16; b < b1; b += 16) {
          if ((b - b0) % 32 === 0 || hardcore) clip(7, 'strobe', b - 4, 4, { every: 0.25, duty: 0.35, white: !hardcore, color: 'cycle', overlay: false }, { opacity: 1, ...(hardcore ? { palette: S.buildPal } : {}) });
          else clip(7, 'dark', b - 0.5, 0.5);
        }
        // big white hits, as a rig's blinders, for rock and electronic songs (never a calm or a pop one): the
        // whole room on the drop's first hit, then on each 8-bar phrase a strong kick or snare opens (the crash)
        if (!chill && (hard || /rock|punk|metal|grunge|alternative/.test(genreWords()))) {
          clip(9, 'whiteflash', b0, 2, {}, { palette: ['#ffffff'], opacity: 1 });
          const strong = [...(ev.kick || []), ...(ev.snare || [])].filter(e => e[1] >= 0.6);
          for (let bt = b0 + 32; bt < b0 + len - 4; bt += 32)
            if (strong.some(e => Math.abs(e[0] - beatToTime(bt)) < 0.12)) clip(9, 'whiteflash', bt, 1, {}, { palette: ['#ffffff'], opacity: 0.9 });
        }
        break;
      }
      case 'breakdown':
        // (no fade to black at its start: the strings going dark there made no sense)
        { const bd = s.chunk && v ? look.breakdown2 || look.breakdown : look.breakdown; clip(0, starBed(bd)[0], b0, len, starBed(bd)[1]); }
        if (stars) clip(1, 'starlight', b0, len, { density: 0.1, life: 4, tint: S.starTint || 'white' }, { blend: 'add' });
        else top(S.drift, { opacity: 0.85 * (((S.drift || [])[2] || {}).opacity || 1) });
        drums(0.5, 0.5);
        break;
      case 'outro':
        clip(0, look.outro[0], b0, len, look.outro[1]);
        if (stars) clip(1, 'starlight', b0, len, { density: 0.06, life: 5, tint: S.starTint || 'white' }, { blend: 'add' });   // and go out again, one by one
        else top(S.drift, { opacity: ((S.drift || [])[2] || {}).opacity || 1 });
        drums(0.45, 0.55);
        clip(3, 'fadeout', Math.max(b0, b1 - 16), Math.min(16, len)); break;
    }
    addSignature(s, b0, len, variant, style);
    // A groove (or calm) song's hits flash in one color for the section, not the next color on every hit:
    // Wicked's drops (tropical house), its bass, beat pulse and drums each stepping through the palette, flashed
    // colors far too quickly. The color changes with the section; dance and rave keep their quick changes.
    if (!hard) for (const c of first) if (HIT_COLORS.has(c.effect)) {
      if (c.params && 'color' in c.params) c.params.color = 'first';
      if (c.effect === 'drumhits') c.palette = [((c.palette || S.buildPal || [])[0]) || '#ffffff'];
    }
    // Black swipes: some of the moving things over a lit section cut darkness through it instead of
    // light (a sweep crossing the room as a black bar, a chase of dark gaps), more of them the harder the
    // song; never in a calm song, nor in a section without much light to cut (intros, breakdowns, outros),
    // nor in a build whose strobe plays alone.
    // (only the big shapes: a spark or a traced note cut black is a speck nobody sees)
    // (every drop gets at least one: a song whose draw left its drops lit showed no black at all)
    const cutShare = { groove: 0.25, dance: 0.35, rave: 0.5 }[style] || 0;   // (50/65/80% was far too many)
    const cut = c => { c.blend = 'cut'; c.opacity = 1; c.duck = 0; c.palette = ['#ffffff']; };   // (no ducking: a swipe must stay black on the hits)
    if (cutShare && ['drop', 'verse', 'buildup'].includes(s.type)) {
      // (a groove song cuts black only in short moments: a black comet crossing all through a tropical house
      // drop, Wicked's, was too much; dance and rave may cut their long moving layers too)
      const big = first.filter(c => CUTTABLE.has(c.effect) && !p.lanes[0].clips.includes(c) && !c.alone && (hard || c.len <= 4));
      for (const c of big) if (Effects.hash(Math.round(c.start * 4) * 92821 + c.effect.length * 31 + (S.lookSeed || 0) + 17) < cutShare) cut(c);
      if (s.type === 'drop' && big.length && !big.some(c => c.blend === 'cut')) cut(big[0]);
    }
    // and a black band across the whole room (left pole, the ceiling, right pole) once in a drop, half way,
    // on the bar before a phrase: the room swiped dark for a moment. (0.7 of a light wide: across one strip
    // alone it is a swipe, not the strip going out.)
    if (cutShare && s.type === 'drop' && len >= 32)                 // (drops only: verses had them in every 8 bars)
      { const b = b0 + Math.max(4, Math.round(len / 2 / 4) * 4) - 4; if (b + 2 <= b1) clip(9, 'roomsweep', b, 2, { every: 2, width: 0.7 }, { blend: 'cut', opacity: 1, palette: ['#ffffff'] }); }   // once, half way
    // Long strips and short lamps together: the moving things (comets, chases, sparkles) run on the
    // long strips, where there is room for them to travel; on seven segments they read as flicker.
    // The lamps play the drums instead, the whole lamp blooming on every kick and snare, or breathe
    // slowly where there are no drums: different instruments in one show.
    if (S.mixedLights) {
      for (const c of first) if (MOVERS.has(c.effect)) {
        c.target = 'long';
        if (p.lanes[0].clips.includes(c))                  // it was the base: the lamps get a wash of their own beneath the drums
          clip(0, 'colorwash', c.start, c.len, { speed: 8, intensity: 0.55 }, { target: 'short', palette: c.palette });
      }
      if (s.type === 'buildup' && !chill && S.buildKind === 'poles' && drumsHere) {   // kick on one pole, snare on the other, then both on every snare
        const half = b0 + Math.round(len / 2);
        clip(6, 'drumhits', b0, half - b0, { follow: 'drums', split: 'split', decay: 0.3, floor: 0 }, { target: 'short', blend: 'add', opacity: 0.9 });
        clip(6, 'drumhits', half, b1 - half, { follow: snaresHere >= 4 ? 'snare' : 'drums', split: 'together', decay: 0.25, floor: 0 }, { target: 'short', blend: 'add', opacity: 1 });
      } else if (s.type === 'buildup' && !chill && snaresHere >= 4)            // in a build both poles flash on every snare
        clip(6, 'drumhits', b0, len, { follow: 'snare', split: 'together', decay: 0.3, floor: 0 }, { target: 'short', blend: 'add', opacity: 0.95 });
      else if (zoneBuild || s.type === 'buildup') { /* in a buildup the lamps are stretches of the room's hits */ }
      // the corner poles as a stereo pair, as a stage rig: in a drop both on every hit (the room as one);
      // elsewhere the kick on one side and the snare on the other, a call and response, each half as busy
      else if (drumsHere) clip(6, 'drumhits', b0, len, { decay: chill ? 0.6 : 0.35, floor: 0, split: s.type === 'drop' && !chill ? 'together' : 'split' },
                              { target: 'short', blend: 'add', opacity: chill ? 0.6 : 0.95, ...(hard ? {} : { palette: [(S.buildPal || ['#ffffff'])[0]] }) });   // (one color unless dance or rave)
      else if (!chill && noDrums() && ['drop', 'verse'].includes(s.type) && ((ev.basshit || []).filter(e => e[0] >= s.start && e[0] < s.end).length) >= 0.4 * len)
        clip(6, 'basshits', b0, len, { decay: 0.45 }, { target: 'short', blend: 'add', opacity: s.type === 'drop' ? 0.85 : 0.5 });   // no drums: the bass keeps time
      else clip(6, 'breathe', b0, len, { speed: 4, floor: 0.15 }, { target: 'short', opacity: 0.6, palette: S.contrastPal });
    }
    // The kitchen's meteor tubes mirror the show (its colors, washes, hits and flashes play across them) in
    // their own way: meteors fall down them. On every kick and snare of a drop (more tubes and quicker the
    // harder the song; a calm song's on the beat, slow), through a buildup more and quicker toward the drop,
    // on the snare in a verse, and a slow drip on the beat in the quiet parts.
    // (the user: the meteor is great, but not ALL THE TIME. The tubes play what the other lights play,
    // alternating, gradients, chases, in a shape of their own; the meteors are moments)
    if (S.hasTubes) {
      const T = { target: 'tubes', blend: 'add' };
      if (s.type === 'drop' && variant % 2 === 1) clip(10, 'meteorfall', b0, len, chill ? { follow: 'beat', many: 1, fall: 1.4, tail: 6 }
        : { follow: drumsHere ? 'drums' : 'beat', many: style === 'rave' ? 4 : hard ? 3 : 2, fall: style === 'rave' ? 0.35 : hard ? 0.45 : 0.6, tail: 4 }, { ...T, opacity: 1 });
      else if (s.type === 'buildup' && !chill) clip(10, 'meteorfall', b0, len, { follow: snaresHere >= 4 ? 'snare' : 'build', many: 5, fall: 0.7, tail: 3, grows: true }, { ...T, opacity: 1 });
      // their shape: the whole tube, an orb floating in the middle of each (the user's idea), orbs drifting up
      // and down, or a glow from the top; calm parts float, the big parts fill the tubes (the same on a rebuild)
      const calm = ['intro', 'breakdown', 'outro'].includes(s.type) || chill;
      const forms = calm ? ['orb', 'drift', 'drift', 'top'] : s.type === 'drop' ? ['full', 'full', 'orb'] : ['full', 'orb', 'drift', 'top'];
      const form = forms[Math.floor(Effects.hash((S.lookSeed || 0) * 13 + idx * 7919 + variant * 31) * forms.length)];
      if (form !== 'full') clip(10, 'tubeform', b0, len, { shape: form }, { target: 'tubes', opacity: 1 });
    }
    S.sectionClips = null;
  }
  const HIT_COLORS = new Set(['pulse', 'basshits', 'drumhits', 'zonehits', 'drumstrobe']);   // hits that could change color on every hit
  const CUTTABLE = new Set(['sweepout', 'scanner', 'roomscan', 'chase', 'hyperchase', 'steps', 'bounce', 'comet', 'dualcomet', 'meteors', 'orbit']);   // big enough to cut black
  const MOVERS = new Set(['comet', 'dualcomet', 'sparkle', 'meteors', 'orbit', 'ripple', 'chase', 'hyperchase', 'bounce', 'scanner', 'steps', 'leadtrace', 'ghostspark', 'fillrush', 'sweepout']);

  /** The genre words the listening model heard in the song, lowercased. */
  /** The genre model's own labels for the song, as one lowercase string (genreWords adds Spotify's too). */
  const songGenres = () => (((S.analysis || {}).genre || {}).genres || []).join(' ').toLowerCase();
  const ELECTRONIC = /electronic|house|techno|edm|dubstep|trance|dance|drum and bass|synth/;
  const HEAVY_DROP = /dubstep|bass music|riddim|brostep|drum and bass|hardstyle/;      // a hardcore drop's genres
  /** Does this song's drop go hardcore? Its scene is bass music or drum and bass, or a heavy genre is heard
   *  nearly as surely as its first (a faint third guess of dubstep made Porter Robinson's Flicker a riddim drop). */
  function heavyDrops() {
    if (S.look === 'bass' || S.look === 'dnb') return true;
    const sc = Object.entries(((S.analysis || {}).genre || {}).scores || {});
    if (!sc.length) return HEAVY_DROP.test(genreWords());
    const heavy = Math.max(0, ...sc.filter(([k]) => HEAVY_DROP.test(k.split('---').pop().toLowerCase().replace('drum n bass', 'drum and bass'))).map(([, v]) => v));
    return heavy >= 0.3 && heavy >= 0.75 * Math.max(...sc.map(([, v]) => v));
  }
  const genreWords = () => (((S.analysis && S.analysis.genre && S.analysis.genre.genres) || []).join(' ') + ' ' + ((S.analysis && S.analysis.genre && S.analysis.genre.spotify) || []).join(' ')).toLowerCase();
  /** A palette moved round the color wheel (a key change lifts the colors with it). */
  function turnPalette(cols, deg) {
    return Effects.solidPalette((cols || []).map(hx => {
      const h = Effects.hueOf(hx);
      return h === null ? hx : Effects.hueHex(h + deg / 360);
    }));
  }

  /** What makes this song unlike the others, each only where it belongs (see flavor.py for how they are heard):
   *  - the singer nearly alone (the voice loud, the instruments low, no drums): a soft spotlight in the
   *    middle of every strip, swelling with the voice;
   *  - the bass wobbling (bass music): the light pulses at the wobble's own speed;
   *  - 808s (a bass hit that holds, low and long): the room sinks into a deep glow that dies away with it;
   *  - a trap hi-hat roll: tiny white ticks racing along the strip;
   *  - an acoustic or folk song: candlelight in its quiet parts;
   *  - the darkest looks: thunder on the biggest hits, and a heartbeat where only a kick plays;
   *  - a key change near the end: the colors lift with it, from there on;
   *  - the finale: the last drop gets everything (fireworks in a song that goes all out), and the
   *    song's last hit holds in its main color and fades away slowly;
   *  - an electronic song's stutter edits (the music cut for a moment, mid-phrase): a glitch. */
  function addFlavor(fromBeat = -Infinity) { addFlavorHere(fromBeat); strobeAlone(); }
  /** A clip that plays alone (a clear EDM build's white strobe): every other clip over its stretch is taken
   *  out of it (cut short, started after it, or split around it), the beat of black into the drop kept.
   *  Runs after the moments and the flavor, which are added on top of the sections. */
  function strobeAlone() {
    const p = P(); if (!p) return;
    for (const solo of p.lanes.flatMap(l => l.clips).filter(c => c.alone)) {
      const s0 = solo.start, s1 = solo.start + solo.len;
      for (const lane of p.lanes) {
        const keep = [];
        for (const c of lane.clips) {
          const c1 = c.start + c.len;
          if (c === solo || c.alone || c.effect === 'dark' || c1 <= s0 || c.start >= s1) { keep.push(c); continue; }
          if (c.start < s0) keep.push({ ...c, len: s0 - c.start });                       // the part before it
          if (c1 > s1) keep.push({ ...c, id: c.start < s0 ? uid() : c.id, start: s1, len: c1 - s1 });   // and after it
        }
        lane.clips = keep;
      }
    }
  }
  function addFlavorHere(fromBeat = -Infinity) {
    const p = P(), a = S.analysis || {}, ev = a.events || {}, style = showStyle(), chill = style === 'chill', hard = style === 'dance' || style === 'rave';
    // A jam's first listen calls this after every pass: only what is still ahead of the room is added,
    // nothing twice, and what needs the whole song (the finale, a key change) waits until it is heard.
    const whole = !a.provisional;
    const has = (fx, b) => p.lanes.some(l => l.clips.some(c => c.effect === fx && Math.abs(c.start - b) < 0.5));
    const count = fx => p.lanes.reduce((k, l) => k + l.clips.filter(c => c.effect === fx).length, 0);
    const put = (lane, fx, b0, len, params = {}, extra = {}) => b0 >= fromBeat - 0.01 && len > 0 && !has(fx, b0) ? clip(lane, fx, b0, len, params, extra) : null;
    const fps = a.feature_fps || 30, cur = a.curves || {}, g = genreWords(), lt = S.mixedLights ? { target: 'long' } : {};
    const beatOf = t => timeToBeat(t), lastB = Math.floor(beatOf(duration()));
    const drop = s => s.type === 'drop', drops = p.sections.filter(drop);

    // the singer nearly alone: two-beat windows where the voice is loud and the rest is quiet, joined
    if (cur.vocals && cur.other) {
      const present = cur.drums_present || [], runs = [];
      for (let b = 0; b < lastB - 2; b += 2) {
        const i0 = Math.floor(beatToTime(b) * fps), i1 = Math.floor(beatToTime(b + 2) * fps); let v = 0, o = 0, d = 0, n = 0;
        for (let i = i0; i < Math.min(i1, cur.vocals.length); i++) { v += cur.vocals[i]; o += cur.other[i]; d += (present[i] || 0) >= 0.3; n++; }
        const alone = n && v / n >= 0.5 && o / n <= 0.35 && d / n < 0.2;
        if (alone && runs.length && runs[runs.length - 1][1] === b) runs[runs.length - 1][1] = b + 2;
        else if (alone) runs.push([b, b + 2]);
      }
      // the four longest, so it stays the singer's moment
      let left = 4 - count('spotlight');
      for (const [b0, b1] of runs.filter(r => r[1] - r[0] >= 4).sort((x, y) => (y[1] - y[0]) - (x[1] - x[0]))) if (left > 0 && put(4, 'spotlight', b0, b1 - b0, {}, { blend: 'add', opacity: 0.9, fadeIn: Math.min(2, (b1 - b0) / 4), fadeOut: Math.min(2, (b1 - b0) / 4) })) left--;
    }

    // the bass wobbling, inside the drops
    if (!chill) for (const [w0, w1] of a.wobbles || []) for (const s of drops) {
      const t0 = Math.max(w0, s.start), t1 = Math.min(w1, s.end); if (t1 - t0 < 2) continue;
      const b0 = Math.round(beatOf(t0)), b1 = Math.round(beatOf(t1));
      put(5, 'wobble', b0, b1 - b0, {}, { blend: 'add', opacity: style === 'rave' ? 0.85 : hard ? 0.7 : 0.6, ...lt });
    }

    // 808s: in hip-hop and trap, where they come often enough to be the song's sound (a held bass note
    // in any other song is just a held bass note)
    const b808 = ev.b808 || [], hiphop = /hip hop|hip-hop|trap|rap|drill|grime/.test(g);
    if (!chill && hiphop && b808.length >= 6) for (const s of p.sections) {
      if (!['drop', 'verse', 'buildup'].includes(s.type)) continue;
      const n = b808.filter(e => e[0] >= s.start && e[0] < s.end).length, b0 = Math.round(beatOf(s.start)), b1 = Math.round(beatOf(s.end));
      if (n >= Math.max(3, (b1 - b0) / 16)) put(2, 'deep808', b0, b1 - b0, { color: 'first' }, { blend: 'add', opacity: s.type === 'drop' ? 0.9 : 0.6, palette: S.songPal });
    }

    // trap hi-hat rolls (in hip-hop, trap and electronic songs: elsewhere a roll is a drummer's fill)
    if (!chill && (hiphop || /electronic|edm|dubstep|bass music/.test(g))) for (const [r0, r1] of a.hatrolls || []) {
      const b0 = beatOf(r0), b1 = beatOf(r1); if (b1 - b0 < 0.4) continue;
      put(6, 'ticks', b0, b1 - b0, {}, { blend: 'add', opacity: 0.9, ...lt });
    }

    // candlelight: an acoustic or folk song's quiet parts
    if (/folk|acoustic|singer-songwriter|country|bluegrass/.test(g) && style !== 'dance' && style !== 'rave') {
      for (const s of p.sections) if (['intro', 'verse', 'breakdown', 'outro'].includes(s.type) && energyIn(s.start, s.end) < 0.5) {
        const b0 = Math.round(beatOf(s.start)), b1 = Math.round(beatOf(s.end));
        put(0, 'candle', b0, b1 - b0, { level: 0.45 }, { opacity: 0.9 });
      }
    }

    // the darkest songs (their mix mostly haunted and storm): thunder on the three biggest hits inside the drops, a heartbeat where only a kick plays
    if (S.dark) {
      if (!chill) {
        const hits = [...(ev.kick || []), ...(ev.basshit || [])].filter(e => drops.some(s => e[0] >= s.start + 2 && e[0] < s.end - 1));
        const used = p.lanes.flatMap(l => l.clips.filter(c => c.effect === 'thunder').map(c => beatToTime(c.start)));   // three in all
        for (const e of hits.sort((x, y) => y[1] - x[1])) {
          if (used.length >= 3) break;
          if (used.some(u => Math.abs(u - e[0]) < 20) || (a.cuts || []).some(([c0, c1]) => Math.abs(c1 - e[0]) < 2)) continue;
          if (put(9, 'thunder', beatOf(e[0]), 1)) used.push(e[0]);
        }
      }
      for (const s of p.sections) {
        if (!['intro', 'breakdown'].includes(s.type)) continue;
        const k = (ev.kick || []).filter(e => e[0] >= s.start && e[0] < s.end).length, sn = (ev.snare || []).filter(e => e[0] >= s.start && e[0] < s.end).length;
        const b0 = Math.round(beatOf(s.start)), b1 = Math.round(beatOf(s.end));
        if (k >= Math.max(4, (b1 - b0) / 4) && sn <= 0.15 * k) put(6, 'heartbeat', b0, b1 - b0, {}, { blend: 'add', opacity: 0.9, palette: ['#c00018'], ...(S.mixedLights ? { target: 'short' } : {}) });
      }
    }

    // the finale: the last drop gets everything, and the last hit holds and fades away
    const last = drops[drops.length - 1];
    if (whole && last && !chill) {
      const b0 = Math.round(beatOf(last.start)), b1 = Math.round(beatOf(last.end));
      if (S.epic || style === 'rave') put(4, 'fireworks', Math.max(b0, Math.ceil(fromBeat)), b1 - Math.max(b0, Math.ceil(fromBeat)), { every: 2, life: 3 }, { blend: 'add', opacity: 0.9, ...lt });
      put(8, 'sweepout', b0, 2, {}, { opacity: 0.9 });
      for (const lane of p.lanes) for (const c of lane.clips) if (!c.finale && c.start >= Math.max(b0, fromBeat) - 0.5 && c.start < b1 && !HIT.has(c.effect) && !FREE.has(c.effect)) {
        c.opacity = Math.min(1, (c.opacity ?? 1) * 1.15); c.finale = true;
      }
      const end = Math.min(lastB, b1), hold = Math.max(4, Math.min(16, lastB - end));
      if (lastB - end >= 2) {
        const main = (S.jamColors && S.jamColors[0]) || (p.palettes[S.songPal] || [])[0] || '#ffffff';
        put(8, 'solid', end, hold, {}, { palette: [main], fadeOut: hold, opacity: 1 });
      }
    }

    // stutter edits in electronic songs: the short cuts left out of the song's moments
    if (/electronic|edm|house|dubstep|trance|techno|bass music/.test(g) && !chill) {
      for (const [t0, t1] of a.cuts || []) {
        if (t1 - t0 >= 0.4 || t1 - t0 < 0.08) continue;
        const period = 60 / (p.bpm || 120);                              // a cut into a new section is the song's moment, not a glitch
        if (p.sections.some(sc => Math.abs(sc.start - t1) < 1.5 * period) || (p.drops || []).some(d => Math.abs(d - t1) < 1.5 * period)) continue;
        const b0 = beatOf(t0), b1 = beatOf(t1);
        put(9, 'scramble', b0, Math.max(0.25, b1 - b0), { rate: 16, density: 0.7, overlay: false }, { opacity: 1 });
      }
    }
    // a key change near the end: the colors lift from there on, with a sweep where it happens (last,
    // so everything placed above is lifted with the rest, and only once)
    const kc = a.key_change;
    if (whole && kc && kc.time) {
      const kb = Math.round(beatOf(kc.time));
      for (const lane of p.lanes) for (const c of lane.clips) if (!c.keyLift && c.start >= Math.max(kb, fromBeat) - 0.5 && Array.isArray(c.palette) && c.palette.length) {
        c.palette = S.pairMode ? c.palette.slice().reverse() : turnPalette(c.palette, 25 * (kc.up || 1));
        c.keyLift = true;                                              // lifted once, however often this runs
      }
      put(8, 'sweepout', kb, 2, {}, { opacity: 0.95, palette: ['#ffffff'], keyLift: true });
    }
  }

  /** The last word on brightness: the show is looked at as the room would see it (twice a second),
   *  and no other section may outshine the drops. Each gets a share of the drops' light by how loud it
   *  is next to them (a quiet verse about half, a near-chorus up to 85%), and one brighter than that is
   *  turned down. Drops that come out dim (a dark wave for a bed) get their bed turned up first. */
  function balance() {
    const p = P(), a = S.analysis; if (!p || !a || !a.energy || !selectedLights().length) return;
    const drops = p.sections.filter(s => s.type === 'drop'); if (!drops.length) return;
    const fps = a.feature_fps || 30;
    const energyOf = s => { let sum = 0, n = 0; for (let i = Math.floor(s.start * fps); i < Math.min(a.energy.length, s.end * fps); i++) { sum += a.energy[i]; n++; } return n ? sum / n : 0; };
    const lightOf = s => { let sum = 0, n = 0; for (let t = s.start + 0.25; t < s.end; t += 0.5) for (const f of renderFrame(t)) { for (const v of f.px) sum += v; n += f.px.length; } return n ? sum / n : 0; };
    // a section's clips, on the same whole beats it was built on
    const inSec = s => { const b0 = Math.round(timeToBeat(s.start)) - 0.01, b1 = Math.round(timeToBeat(s.end)) - 0.01; return p.lanes.flatMap(l => l.clips.filter(c => c.start >= b0 && c.start < b1 && !FREE.has(c.effect) && !c.alone)); };
    // every drop gets most of the light of the brightest (a quieter drop a little less), and at least a
    // fair share: a dim one has its bed turned up, and a bed that sinks to black (a wave) a floor raised
    const Ld = new Map(drops.map(s => [s, lightOf(s)])), Emax = Math.max(...drops.map(energyOf)) || 1, Lmax = Math.max(...Ld.values());
    for (const s of drops) {
      const want = Math.max(60, 0.8 * Lmax * clamp(energyOf(s) / Emax, 0.6, 1)), L = Ld.get(s);
      if (L >= want) continue;
      const k = Math.min(2.5, want / Math.max(1, L));
      for (const c of inSec(s)) if (BED.has(c.effect)) {
        c.opacity = Math.min(1, (c.opacity ?? 1) * k);
        if (typeof c.params.floor === 'number') c.params.floor = Math.max(c.params.floor, 0.3);
      } else if (!HIT.has(c.effect)) c.opacity = Math.min(1, (c.opacity ?? 1) * k);   // a drop with no bed (blocks, a scanner): its motion comes up
      Ld.set(s, lightOf(s));
    }
    const D = [...Ld.values()].reduce((x, y) => x + y, 0) / drops.length, Ed = drops.reduce((x, s) => x + energyOf(s), 0) / drops.length || 1;
    p.sections.forEach((s, i) => {
      if (s.type === 'drop') return;
      let target = D * clamp(0.3 + 0.55 * energyOf(s) / Ed, 0.45, 0.85);
      const next = p.sections[i + 1];
      if (next && Ld.has(next)) target = Math.min(target, 0.85 * Ld.get(next));   // never outshine the drop it leads to
      const L = lightOf(s);
      if (L > target && L > 1) { const k = Math.max(0.4, target / L); for (const c of inSec(s)) c.opacity = (c.opacity ?? 1) * k; }
    });
  }
  /** The song's moments, from what the separated tracks heard:
   *  - an audio cut (the music drops out for a moment): the lights fade to black over the gap and come
   *    back on a white flash as the music does;
   *  - an entrance (the synths or guitars come in after being away, away from a section's start): a
   *    slam, and the colors flip from there on, so the room feels the band arrive.
   *  Only from fromBeat on, and never twice: a jam's first listen adds them as each pass hears them. */
  function addMoments(fromBeat = -Infinity) {
    const p = P(), a = S.analysis || {};
    const has = (fx, b) => p.lanes.some(l => l.clips.some(c => c.effect === fx && Math.abs(c.start - b) < 0.5));
    if (showStyle() === 'chill') return;                               // a calm song stays calm
    const period = 60 / (p.bpm || 120);
    for (const [t0, t1] of a.cuts || []) {
      if (t0 < 2 || t1 > duration() - 1) continue;
      // a real cut is a held breath: long enough to notice, or the gap right before a drop or a new
      // section. Short stops inside a section are the song's own stutter, already in its rhythm.
      const intoChange = p.sections.some(sc => Math.abs(sc.start - t1) < 1.5 * period) || (p.drops || []).some(d => Math.abs(d - t1) < 1.5 * period);
      if (t1 - t0 < 0.4 && !intoChange) continue;
      const b0 = timeToBeat(t0), b1 = timeToBeat(t1);
      if (b1 - b0 < 0.25 || b0 < fromBeat || has('dark', b0)) continue;
      clip(9, 'dark', b0, b1 - b0, {}, { fadeIn: Math.min(0.5, (b1 - b0) / 2) });
      clip(9, 'whiteflash', b1, 1.5, {}, { palette: ['#ffffff'], opacity: 0.9 });
    }
    const starts = p.sections.map(s => s.start);
    for (const te of a.entries || []) {
      if (starts.some(st => Math.abs(st - te) < 8 * period)) continue; // at a section change it already shows
      const be = Math.round(timeToBeat(te));
      if (be < fromBeat || has('colorslam', be)) continue;
      for (const li of [0, 1]) for (const c of [...(p.lanes[li] ? p.lanes[li].clips : [])]) {
        if (c.start < be - 0.5 && c.start + c.len > be + 1) {         // split, and flip the colors after the entrance
          const cols = clipPaletteHex(c), flipped = S.pairMode ? cols.slice().reverse() : contrastPalette(cols);
          p.lanes[li].clips.push({ ...c, id: uid(), start: be, len: c.start + c.len - be, palette: flipped });
          c.len = be - c.start;
        }
      }
      clip(6, 'colorslam', be, 2, {}, { opacity: 0.9 });
    }
  }
  /** The flair that belongs to *this* song: its riff, its lead line, its fills.
   *
   *  Two principles. Punctuation beats blanket: a one-shot fired at the moment
   *  something actually happens reads as the lights knowing the song, while a
   *  full-length layer just adds noise. And it belongs where the music is big:
   *  drops and hyped verses get the accents, quiet sections stay quiet.
   */
  // How much texture a section may carry on top of its drums and its base (the lead traced, ghost-note
  // sparks): the calmer the style, the less. One-hits are not rationed: every strong lead moment of a
  // drop or a hyped verse gets one, in every style (the user: "as many fun one-hits as possible, in
  // any genre"), a bar apart at least so each one lands; a calm song's are gentle (ripples, sweeps).
  const ACCENTS = { chill: { texture: 0 }, groove: { texture: 1 }, dance: { texture: 1 }, rave: { texture: 2 } };
  const ONE_SHOTS = new Set(['sweepout', 'colorslam', 'whiteflash', 'ripple', 'dropblast', 'hookstrike', 'glitch', 'fillflood']);   // (two never within 2 beats)
  /** A one-shot's color: one that stands apart from the section under it, so the moment breaks from it (a
   *  red fill over a blue verse): the jam's extra colors when it has them, else the stage color farthest
   *  round the color wheel from every color of the section. */
  const PUNCH = ['#ff0000', '#0030ff', '#ff0080', '#00ff40', '#00c8ff', '#ffa000', '#8000ff'];
  const hueOf = hx => { const h = Effects.hueOf(hx); return h === null ? null : h * 360; };   // (degrees)
  function punchColor(at = 0) {
    if (S.jamExtras && S.jamExtras.length) return S.jamExtras[Math.abs(Math.round(at)) % S.jamExtras.length];
    const base = (S.buildPal || []).map(hueOf).filter(h => h !== null);
    if (!base.length) return '#ff0000';
    const gap = h => Math.min(...base.map(x => Math.min(Math.abs(h - x), 360 - Math.abs(h - x))));
    return PUNCH.map(c => [c, gap(hueOf(c))]).sort((x, y) => y[1] - x[1])[0][0];
  }
  function addSignature(s, b0, len, variant, style) {
    const hard = style === 'dance' || style === 'rave';
    const budget = ACCENTS[style] || ACCENTS.groove;
    const a = S.analysis; if (!a || !a.events) return;
    const p = P();
    const quiet = s.type === 'intro' || s.type === 'breakdown' || s.type === 'outro';
    const loud = s.type === 'drop' || s.type === 'buildup';
    const t2b = t => clamp(Math.round(timeToBeat(t)), b0, b0 + len - 1);
    const within = arr => (arr || []).filter(e => e[0] >= s.start && e[0] < s.end);
    const hooks = ((a.hook && a.hook.times) || []).filter(t => t >= s.start && t < s.end);
    const leads = within(a.events.lead), ghosts = within(a.events.ghost), basses = within(a.events.bass);
    const fills = (a.fills || []).filter(([f0, f1]) => f1 > s.start && f0 < s.end);

    // The riff: a short strike each time it comes back. Skipped entirely in an intro
    // or outro (a repeating pad there is not a hook worth marking) and rationed in a
    // breakdown, so the quiet parts stay quiet.
    const hookBeats = Math.max(2, Math.round(((a.hook && a.hook.length) || 2) * p.bpm / 60));
    const hookList = s.type === 'intro' || s.type === 'outro' ? []
      : s.type === 'breakdown' ? hooks.slice(0, 2) : hooks;
    for (const t of hookList) {
      const soft = quiet || style === 'chill';                     // a calm song's riff sweeps, it never bursts
      clip(4, 'hookstrike', t2b(t), Math.min(hookBeats, b0 + len - t2b(t)),
           { shape: soft ? 'sweep' : 'burst', decay: soft ? 0.8 : 0.4 }, { opacity: soft ? 0.55 : style === 'groove' ? 0.8 : 1 });
    }
    // A fill rush is a lot of movement, so it is kept for the hardest drops only
    if (style === 'rave' && s.type === 'drop') for (const [f0, f1] of fills) {
      const st = t2b(Math.max(f0, s.start)), en = clamp(Math.round(timeToBeat(Math.min(f1, s.end))), b0, b0 + len);
      if (en - st >= 2) clip(5, 'fillrush', st, en - st, { size: 0.1 }, { opacity: 0.9 });
    }
    // A sick fill (from the separated drums: 2.5 hits a beat or more, for a beat or more): the whole room
    // floods in one solid color that breaks from the section (a red fill over a blue verse), white on every
    // hit, and the beat it lands on gets a white hit. Every style but chill; not in a
    // buildup (it has its own). About two a song.
    if (style !== 'chill' && s.type !== 'buildup') {
      const ev = a.events, hitsIn = (f0, f1) => ['kick', 'snare', 'roll'].reduce((n, k) => n + (ev[k] || []).filter(e => e[0] >= f0 && e[0] < f1).length, 0);
      // (a fill is a burst: a bar or less, busier than the two bars before it and the two after. The fills
      // found are often whole busy stretches, 8 to 24 beats: Bombs Away's first drop was 16 beats of solid cyan,
      // its drums busier than the quiet breakdown before it only because the drop had begun. 76 of 274 are fills.)
      const beatS = 60 / p.bpm, rate = (t0, t1) => hitsIn(t0, t1) / Math.max(1e-6, (t1 - t0) / beatS);
      for (const [f0, f1] of fills) {
        const beats = (f1 - f0) / beatS, r = rate(f0, f1);
        if (beats < 1 || beats > 4.5 || r < 2.5 || r < 1.4 * Math.max(0.25, rate(f0 - 8 * beatS, f0)) || r < 1.2 * Math.max(0.25, rate(f1, f1 + 8 * beatS))) continue;
        const st = t2b(Math.max(f0, s.start)), en = clamp(Math.ceil(timeToBeat(Math.min(f1, s.end))), b0, b0 + len);
        if (en - st >= 1) clip(9, 'fillflood', st, en - st, {}, { palette: [punchColor(st)], opacity: 1 });
        if (en - st >= 1 && S.hasTubes) clip(10, 'meteorfall', st, en - st, { follow: 'drums', many: 4, fall: 0.4, tail: 3 }, { target: 'tubes', blend: 'add', opacity: 1 });   // and a meteor burst in the kitchen
        const land = Math.round(timeToBeat(f1));
        if (land > b0 && land <= b0 + len) clip(8, 'whiteflash', land, 2, {}, { opacity: 0.9, blend: 'add' });
      }
    }
    // One-shots on moments the separated tracks hear, each with the move that fits it: the drums kicking
    // back in after a break (a white hit, or a slam), the bass dropping in (a slam), the singer coming in
    // (a sweep out from the middle) and a melody phrase starting (a sweep from anywhere) after a rest, on
    // the bar line, away from a section change (that shows already). About 1.6 a song.
    const bars = (a.downbeats || []).filter(t => t >= s.start && t < s.end);
    const curve = k => (a.curves || {})[k], fps = a.feature_fps || 30, barLen = 240 / (p.bpm || 120);
    const lvl = (c, t0, t1) => { let sum = 0, n = 0; for (let i = Math.max(0, Math.floor(t0 * fps)); i < Math.min(c.length, Math.floor(t1 * fps)); i++) { sum += c[i]; n++; } return n ? sum / n : 0; };
    const awayFromChange = t => p.sections.every(sc => Math.abs(sc.start - t) > 2 * barLen);
    const comesIn = (k, quiet, loudLv, rest) => { const c = curve(k); if (!c || !c.length) return [];
      return bars.filter(t => awayFromChange(t) && lvl(c, t - rest * barLen, t) < quiet && lvl(c, t, t + barLen) >= loudLv); };
    // on top of everything, in one solid color that breaks from the section (white stays white)
    const shotLen = fx => fx === 'ripple' ? 4 : fx === 'glitch' ? 1.5 : 2;
    const place = (bt, fx, prm = {}, ext = {}) => clip(fx === 'ripple' ? 6 : 9, fx, bt, shotLen(fx), fx === 'ripple' ? { every: 2, travel: 2, width: 0.12, ...prm } : prm,
      { ...(fx === 'whiteflash' ? {} : { palette: [punchColor(bt)] }), ...ext });
    const shot = (t, fx, prm = {}, ext = {}) => { const bt = t2b(t); if (p.lanes.some(l => l.clips.some(c => ONE_SHOTS.has(c.effect) && Math.abs(c.start - bt) < 2))) return; place(bt, fx, prm, ext); };
    const calm = style === 'chill';
    if (!calm) for (const t of comesIn('drums_present', 0.2, 0.6, 1)) shot(t, hard ? 'whiteflash' : 'colorslam', {}, { opacity: hard ? 0.85 : 0.95 });
    if (!calm) for (const t of comesIn('bass', 0.08, 0.3, 2)) shot(t, 'colorslam', {}, { opacity: 0.9 });
    for (const t of comesIn('vocals', 0.15, 0.4, 2)) shot(t, calm ? 'ripple' : 'sweepout', { origin: 0.5 }, { opacity: calm ? 0.55 : 0.85 });
    for (const t of comesIn('lead', 0.2, 0.5, 1)) shot(t, calm ? 'ripple' : hard ? 'glitch' : 'sweepout', {}, { opacity: calm ? 0.55 : 0.9 });
    // and the lead's real accents in a drop or a busy verse: a note that stands out from the line around it
    // (1.4 times the notes within a bar either side, and near the loudest), two bars apart. About 7 a song:
    // the loudest notes alone fired 32 times a song, every other bar, on whatever happened to be loud.
    const hype = loud || (s.type === 'verse' && leads.length >= 12);
    if (hype && leads.length) {
      const picks = [], top = Math.max(...leads.map(e => e[1])), beatS = 60 / (p.bpm || 120);
      for (const e of [...leads].sort((x, y) => y[1] - x[1])) {
        if (e[1] < 0.75 * top) break;
        const around = leads.filter(x => x !== e && Math.abs(x[0] - e[0]) <= 4 * beatS).map(x => x[1]).sort((x, y) => x - y);
        if (around.length && e[1] < 1.4 * around[Math.floor(around.length / 2)]) continue;
        const bt = t2b(e[0]);
        if (bt > b0 + 3 && bt < b0 + len - 3 && picks.every(q => Math.abs(q - bt) >= 8)) picks.push(bt);
      }
      // (sweeps were everywhere: the lead's accents are slams, glitches and flashes; a groove song's last a sweep)
      const shots = hard ? ['glitch', 'colorslam', 'whiteflash'] : calm ? ['ripple', 'sweepout'] : ['colorslam', 'glitch', 'colorslam', 'sweepout'];
      picks.sort((x, y) => x - y).forEach((bt, k) => {
        const fx = shots[(k + variant) % shots.length];
        if (p.lanes.some(l => l.clips.some(c => ONE_SHOTS.has(c.effect) && Math.abs(c.start - bt) < 2))) return;
        place(bt, fx, {}, { opacity: fx === 'whiteflash' ? 0.75 : 1 });
      });
    }
    // texture: only where the music is already busy
    if (loud) {
      let texture = budget.texture;
      if (texture && leads.length >= 3) { clip(4, 'leadtrace', b0, len, { tail: 0.2 }, { opacity: style === 'rave' ? 0.95 : 0.8 }); texture--; }
      if (texture && ghosts.length >= 8) { clip(5, 'ghostspark', b0, len, { count: 3 }, { opacity: 0.7 }); texture--; }
      // (not in a buildup: a whole strip swelling on every bass note would bury the build's hits)
      if (leads.length < 3 && basses.length >= 6 && s.type !== 'buildup') clip(2, 'bassnote', b0, len, {}, { opacity: 0.8, blend: 'add' });
    } else if (leads.length >= 4) {
      // in the calm, the lead only makes the strip breathe; it never flashes
      clip(1, 'leadglow', b0, len, { gain: quiet ? 1.1 : 1.3, floor: 0 }, { opacity: quiet ? 0.4 : 0.55, blend: 'add' });
    }
  }

  // ------------------------------------------------------------------ keyboard & controls
  document.addEventListener('keydown', e => {
    // Ctrl+S saves the project wherever the focus is (otherwise the browser offers to save the page)
    if ((e.ctrlKey || e.metaKey) && !e.shiftKey && e.key.toLowerCase() === 's') { e.preventDefault(); S.review ? reviewKeep(false) : saveNow(); return; }
    // e.target can be the document itself (or any non-element node), which has no matches()
    if (e.target && typeof e.target.matches === 'function' && e.target.matches('input, select, textarea')) {
      if (e.key === 'Escape') e.target.blur();
      return;
    }
    const be = $('#beatEdit').checked;
    if (S.review && (e.key === '[' || e.key === ']')) { e.key === ']' ? reviewDone() : S.review.edit || reviewOpen(S.review.i - 1); return; }
    if (e.code === 'Space') { e.preventDefault(); if (be && S.audio.playing) { beginChange(); const t = pos(); const p = P(); const i = p.beats.findIndex(b => b > t); if (i < 0) p.beats.push(t); else p.beats.splice(i, 0, t); changed(); } else togglePlay(); }
    else if (e.key === 'Enter') { togglePlay(); }
    else if (e.key.toLowerCase() === 'm' && !e.ctrlKey && !e.metaKey && S.review && S.review.proposal) {   // Claude's sections or the old ones
      S.review.showing = S.review.showing === 'claude' ? 'old' : 'claude'; showMapping();
      toast(S.review.showing === 'claude' ? "Claude's sections (Done keeps them)" : 'The old sections (Done keeps them)');
    }
    else if (e.key === 'Home') { seek(0); scrollTo(0); }
    else if (e.key === 'End') { seek(duration()); }
    else if (e.key === 'Delete' || e.key === 'Backspace') { deleteSelection(); }
    else if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === 'z') { e.preventDefault(); e.shiftKey ? redo() : undo(); }
    else if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === 'y') { e.preventDefault(); redo(); }
    else if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === 'c') { copySelection(); }
    else if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === 'x') { cutSelection(); }
    else if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === 'v') { pasteAt(clipboard ? clipboard.minLane : 0, snapBeat(timeToBeat(pos()))); }
    else if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === 'a') { e.preventDefault(); selectAll(); }
    else if (e.key === 'Escape') { clearSelection(); }
    else if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === 'd') { e.preventDefault(); duplicateSelection(); }
    else if (e.key === '+' || e.key === '=') { zoomAt(1.25, xOf(pos())); }
    else if (e.key === '-' || e.key === '_') { zoomAt(0.8, xOf(pos())); }
    else if (e.key === 'ArrowLeft' || e.key === 'ArrowRight') { const dir = e.key === 'ArrowLeft' ? -1 : 1; const s = snapVal() || 1; if (groupSize() > 1) { const items = selectedClips().map(i => ({ clip: i.clip, lane: i.lane, start0: i.clip.start })); beginChange(); if (!moveGroup(items, dir * s)) S.undo.pop(); else changed(); } else if (S.view.sel && S.view.sel.type === 'clip') { const { clip, lane } = findClip(S.view.sel.id); const ns = Math.max(0, clip.start + dir * s); if (overlaps(lane, ns, clip.len, clip)) return; beginChange(); clip.start = ns; inspector(); changed(); } else seek(beatToTime(timeToBeat(pos()) + dir * s)); }
    else if (e.key.toLowerCase() === 'b') { $('#beatEdit').checked = !be; draw(); }
  });
  $('#btnPlay').onclick = togglePlay; $('#btnStop').onclick = () => { if (S.cast.active) { castPause(); castSeek(0); } else stopPlayback(); draw(); };
  $('#btnCast').onclick = () => { S.cast.active ? castEnd() : castStart(); };
  $('#castDevice').onfocus = () => { if (S.cast.devices.length === 0) loadCastDevices(); };
  $('#castVolume').oninput = () => { clearTimeout(S.cast.volT); S.cast.volT = setTimeout(() => castApi('volume', { level: parseInt($('#castVolume').value) / 100 }).catch(() => { }), 150); };
  $('#castOffset').onchange = () => { if (P()) P().settings.castOffsetCustom = true; scheduleSave(); syncLocalToCast(); };
  $('#btnSync').onclick = () => runSyncCheck(false);
  $('#castLocal').onchange = syncLocalToCast; $('#btnStart').onclick = () => { seek(0); scrollTo(0); };
  $('#btnZoomIn').onclick = () => zoomAt(1.4, xOf(pos())); $('#btnZoomOut').onclick = () => zoomAt(0.7, xOf(pos())); $('#btnZoomFit').onclick = zoomFit;
  $('#btnUndo').onclick = undo; $('#btnRedo').onclick = redo;
  $('#btnRedetect').onclick = async () => {
    if (!S.analysis) return;
    // a fresh analysis from scratch: the server throws away everything it saved for this song and
    // runs the newest analysis (so nothing old, cached or half-done, can come back)
    if (S.name) {
      busy('Analysing from scratch…');
      try { const d = await (await fetch(`/api/projects/${encodeURIComponent(S.name)}?fresh=1`)).json(); if (d.analysis) S.analysis = d.analysis; }
      catch (e) { /* keep what was loaded */ } finally { busy(null); }
    }
    if (S.analysis.structure_source === 'loudness') toast(`Section timing not refined: ${S.analysis.structure_error || 'SongFormer did not run'}`, true);
    const a = S.analysis; const p = P();
    const all = confirm('Re-detect.' + String.fromCharCode(10, 10) + 'OK = replace the beat grid AND your sections and drop markers with the fresh analysis.' + String.fromCharCode(10) + 'Cancel = replace only the beat grid (BPM, beats, downbeat) and keep your sections and drops.');
    beginChange();
    p.bpm = a.bpm; p.phase = a.downbeat_phase; p.beats = a.beats.slice(); $('#bpm').value = p.bpm.toFixed(2);
    if (all) { p.sections = a.sections.map(s => ({ start: s.start, end: s.end, type: s.type })); p.drops = a.drops.slice(); }
    S.view.sel = null; inspector(); changed(); toast(all ? `Re-detected: ${p.sections.length} sections, ${p.drops.length} drops` : 'Beat grid refreshed; sections and drops kept');
  };
  $('#btnAuto').onclick = () => { if (!P()) return toast('Open a song first'); if (P().lanes.some(l => l.clips.length) && !confirm('Replace all clips with an auto-built show?')) return; autoBuild(); };
  $('#btnOpen').onclick = () => $('#fileInput').click(); $('#fileInput').onchange = e => { if (e.target.files[0]) openFile(e.target.files[0]); e.target.value = ''; };
  $('#btnPath').onclick = () => { const v = $('#pathInput').value.trim(); if (v) openPath(v); }; $('#pathInput').onkeydown = e => { if (e.key === 'Enter') $('#btnPath').click(); };
  $('#recent').addEventListener('input', e => { if (!e.inputType || e.inputType === 'insertReplacementText') openNamed(e.target.value); });   // picked from the list
  $('#recent').addEventListener('keydown', e => { if (e.key === 'Enter') { e.preventDefault(); openNamed(e.target.value) || toast('No show or jam song by that name', true); } if (e.key === 'Escape') { e.target.value = ''; e.target.blur(); } });
  $('#recent').addEventListener('focus', () => refreshRecent());
  $('#btnReview').onclick = reviewStart;
  $('#revPrev').onclick = () => reviewOpen(S.review.i - 1);
  $('#revDone').onclick = reviewDone;
  $('#revClose').onclick = reviewStop;
  const quarter = dir => { const p = P(); if (!p || p.beats.length < 2) return; nudge(dir * 250 * (p.beats[p.beats.length - 1] - p.beats[0]) / (p.beats.length - 1)); };
  $('#btnQuarterL').onclick = () => quarter(-1); $('#btnQuarterR').onclick = () => quarter(1);
  $('#btnHalf').onclick = () => scaleTempo(0.5); $('#btnDouble').onclick = () => scaleTempo(2); $('#btnRegrid').onclick = () => { if (P()) regrid(); }; $('#btnNudgeL').onclick = () => P() && nudge(-10); $('#btnNudgeR').onclick = () => P() && nudge(10);
  $('#btnPhase').onclick = () => { if (!P()) return; beginChange(); P().phase = (P().phase + 1) % 4; changed(); };
  $('#btnPhaseL').onclick = () => { if (!P()) return; beginChange(); P().phase = (P().phase + 3) % 4; changed(); };
  $('#bpm').onchange = () => { if (P()) { beginChange(); P().bpm = parseFloat($('#bpm').value) || P().bpm; changed(); toast('BPM stored — press Regrid to rebuild the beat grid'); } };
  $('#beatEdit').onchange = draw; $('#snap').onchange = draw; $('#effectSearch').oninput = renderLibrary;
  $('#roomViewBtn').onclick = openRoomView;
  $('#outputOn').onchange = e => { if (e.target.checked) { if (!selectedLights().length) { toast('Tick a light first', true); e.target.checked = false; return; } rememberLights(true); S.lightsArmed = false; if (S.audio.playing) startOutput(); else pushFrameIfIdle(); } else { rememberLights(false); stopOutput(); } };
  // Closing the window stops the studio at once, speakers included (a reload comes straight back,
  // and the studio waits a moment to tell the two apart).
  window.addEventListener('pagehide', () => { try { navigator.sendBeacon('/api/closing'); } catch (e) { /* the watchdog still notices */ } });
  $('#latency').onchange = () => {
    S.defaults.lightOffset = parseFloat($('#latency').value) || 0;
    fetch('/api/settings/light_offset', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ ms: S.defaults.lightOffset }) }).catch(() => {});
    scheduleSave();
  };
  $('#projectName').onchange = scheduleSave;
  if ($('#btnNewPalette')) $('#btnNewPalette').onclick = () => { if (!P()) return; const name = prompt('Palette name'); if (!name) return; beginChange(); P().palettes[name] = ['#ff0000', '#0000ff']; renderPalettes(); changed(); };
  window.addEventListener('resize', () => draw());
  window.addEventListener('beforeunload', () => {
    if (S.outputActive) stopOutput();
    // closing the app window is how the studio is quit, so a debounced save must not be lost
    if (S.saveTimer && P() && S.name) {
      clearTimeout(S.saveTimer); S.saveTimer = null;
      try {
        navigator.sendBeacon(`/api/projects/${encodeURIComponent(S.name)}`,
          new Blob([JSON.stringify(collectProject())], { type: 'application/json' }));
      } catch (e) { /* nothing more we can do on the way out */ }
    }
  });
  // keep the audio clock honest if the tab was throttled: nothing to do, WebAudio time is wall-clock

  function toast(msg, err = false) { const t = $('#toast'); t.textContent = msg; t.className = 'toast' + (err ? ' err' : ''); t.hidden = false; clearTimeout(t._t); t._t = setTimeout(() => t.hidden = true, err ? 6000 : 3000); }
  function busy(msg) { $('#busy').hidden = !msg; if (msg) $('#busyText').textContent = msg; }

  // ------------------------------------------------------------------ boot
  fetch('/api/settings').then(r => r.json()).then(s => { S.defaults.castOffset = s.cast_offset_ms || 0; S.defaults.castOffsets = s.cast_offsets || {};
    S.defaults.lightOffset = s.light_offset_ms ?? 30; $('#latency').value = S.defaults.lightOffset; if (!P()) $('#castOffset').value = S.defaults.castOffset; }).catch(() => { });
  renderLibrary(); renderPalettes(); inspector(); connectWs(); loadLights(); refreshRecent(); loadCastDevices(); loop();
  bootModels();
  /** As the studio opens, the models load (and warm up) in the model host: a card over the app shows them
   *  coming up, and goes when all are ready (or one fails: the analysis carries on without it). Nothing to
   *  wait for (no models installed, learn ahead off, a test copy): no card. */
  async function bootModels() {
    const NAMES = { songformer_infer: 'Sections', stems_infer: 'Stems', genre_infer: 'Genre', beats_infer: 'Beats', drums_infer: 'Drums' };
    const WORDS = { waiting: 'waiting', loading: 'loading…', warming: 'warming up…', ready: 'ready', failed: 'could not load' };
    const card = $('#bootModels'), t0 = performance.now();
    for (;;) {
      let d = null;
      try { d = await (await fetch('/api/models')).json(); } catch (e) { /* the server is starting */ }
      if (d && !d.expected) break;                                      // no models to wait for
      const m = (d && d.models) || null, names = Object.keys(NAMES);
      const done = m && names.every(n => m[n] === 'ready' || m[n] === 'failed');
      if (done || performance.now() - t0 > 120000) break;               // (never longer than two minutes)
      card.hidden = false;
      const score = { waiting: 0, loading: 0.25, warming: 0.7, ready: 1, failed: 1 };
      $('#bootBar').style.width = `${Math.round(100 * names.reduce((k, n) => k + (score[(m || {})[n]] || 0), 0) / names.length)}%`;
      $('#bootList').innerHTML = names.map(n => { const st = (m || {})[n] || 'waiting';
        return `<span>${NAMES[n]}</span><span class="${st === 'ready' ? 'ok' : st === 'failed' ? 'bad' : ''}">${WORDS[st] || st}</span>`; }).join('');
      await new Promise(r => setTimeout(r, 400));
    }
    card.hidden = true;
  }
  window.Studio = { S, P, $, addClip, autoBuild, addMoments, addFlavor, withBuildups, showStyle, buildSection, newProject, migrateProject, openPath, openProject, renderFrame, calmAt, songCalm, save, _draw, compose,
    sceneWeights, fullBrightness, draw, inspector, zoomFit, renderPalettes, renderLights, sendSelect, toast, timeToBeat, beatToTime, startOutput, stopOutput, pos, updateTransport, refreshRecent, loudnessGain, contrastPalette, tidySections, applyCastStatus, mainPalette, pickLook, freshenSaved, roomSpace, openRoomView, partyLights, bootModels };
})();
