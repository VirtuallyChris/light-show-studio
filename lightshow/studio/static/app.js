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
    undo: [], redo: [], saveTimer: null, tap: [], lastFrame: null,
  };
  const HEADER = 96, ROW = { ruler: 22, sections: 20, wave: 84, pal: 0, lane: 48 };
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

  function newProject(a, name) {
    return {
      version: 1, name, bpm: a.bpm, phase: a.downbeat_phase, beats: a.beats.slice(),
      sections: tidySections(a.sections),
      drops: a.drops.slice(), palettes: JSON.parse(JSON.stringify(Effects.PALETTES)), defaultPalette: 'Neon',
      paletteClips: [], lanes: Array.from({ length: DEFAULT_LANES }, newLane),
      settings: { latency: 30, master: 1 },
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
    // the strobe riser used to speed up smoothly between two rates; now it steps through note values
    for (const lane of p.lanes) for (const c of lane.clips) if (c.effect === 'strobeRiser' && c.params && c.params.startRate !== undefined) {
      const note = (r, opts) => opts.reduce((best, o) => Math.abs(Math.log2(o / r)) < Math.abs(Math.log2(best / r)) ? o : best, opts[0]);
      const from = note(c.params.startRate || 1, [1, 2, 4]); const to = Math.max(from * 2, note(c.params.maxRate || 8, [2, 4, 8]));
      c.params.from = String(from); c.params.to = String(Math.min(8, to));
      delete c.params.startRate; delete c.params.maxRate;
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
  function beginChange() { if (!P()) return; S.undo.push(snapshot()); if (S.undo.length > 100) S.undo.shift(); S.redo.length = 0; }
  function changed() { scheduleSave(); draw(); }
  function undo() { if (!S.undo.length) return; S.redo.push(snapshot()); S.project = JSON.parse(S.undo.pop()); S.view.sel = null; S.view.multi = []; inspector(); changed(); }
  function redo() { if (!S.redo.length) return; S.undo.push(snapshot()); S.project = JSON.parse(S.redo.pop()); S.view.sel = null; S.view.multi = []; inspector(); changed(); }
  function scheduleSave() { clearTimeout(S.saveTimer); S.saveTimer = setTimeout(save, 1500); }
  /** copy the UI-held settings into the project and hand it back, ready to send */
  function collectProject() {
    P().name = $('#projectName').value || S.name;
    P().settings.latency = parseFloat($('#latency').value) || 0;
    P().settings.castOffset = parseFloat($('#castOffset').value) || 0;
    P().settings.master = parseFloat($('#master').value) / 100;
    return P();
  }
  async function save() {
    if (!P() || !S.name || (S.jam && S.jam.live)) return;                  // a jam never writes over a saved show
    collectProject();
    try { await fetch(`/api/projects/${encodeURIComponent(S.name)}`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(P()) }); }
    catch (e) { toast('Save failed: ' + e, true); }
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
    stopPlayback();
    S.analysis = data.analysis; S.name = data.project;
    S.project = data.saved || newProject(data.analysis, data.project);
    if (!S.project.lanes) S.project = newProject(data.analysis, data.project);
    migrateProject(S.project);
    $('#projectName').value = S.project.name || S.name;
    $('#bpm').value = S.project.bpm.toFixed(2);
    $('#latency').value = S.project.settings.latency;
    // an offset typed for this project wins; otherwise the connected speaker's own measured delay
    const measured = S.cast.active ? S.defaults.castOffsets[S.cast.device] : null;
    $('#castOffset').value = S.project.settings.castOffsetCustom ? (S.project.settings.castOffset || 0)
      : (measured != null ? measured : S.defaults.castOffset);
    $('#master').value = Math.round(S.project.settings.master * 100);
    renderLights(); sendSelect();
    $('#emptyState').hidden = true;
    S.undo.length = 0; S.redo.length = 0; S.view.sel = null; S.view.multi = []; S.audio.pos = 0;
    busy('Decoding audio…');
    await loadAudio(data.audio_url);
    zoomFit(); inspector(); renderPalettes(); draw(); refreshRecent();
    toast(`${S.name}: ${S.project.bpm.toFixed(1)} bpm, ${S.project.sections.length} sections, ${S.project.drops.length} drops` + (S.analysis.structure_source === 'loudness+songformer' ? ' · timed by SongFormer' : ''));
  }
  async function refreshRecent() {
    const sel = $('#recent'); const cur = sel.value;
    try {
      const list = await (await fetch('/api/projects')).json();
      sel.innerHTML = '<option value="">Saved shows…</option>' + list.map(p => `<option value="${p.name}">${p.name}${p.has_project ? '' : ' (song only)'}</option>`).join('');
      sel.value = cur;
    } catch (e) { /* server away */ }
  }

  // ------------------------------------------------------------------ audio
  async function loadAudio(url) {
    if (!S.audio.ctx) S.audio.ctx = new (window.AudioContext || window.webkitAudioContext)();
    const ab = await (await fetch(url)).arrayBuffer();
    S.audio.buffer = await S.audio.ctx.decodeAudioData(ab);
  }
  function pos() {
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
  function seek(t) { if (S.jam && S.jam.live) return; t = clamp(t, 0, duration()); if (S.cast.active) { castSeek(t); return; } const a = S.audio; if (a.playing) play(t); else { a.pos = t; pushFrameIfIdle(); } }
  function togglePlay() { if (S.jam && S.jam.live) return toast('Spotify is in charge during a jam: play and pause from the phone'); S.audio.playing ? pause() : play(); }
  function updateTransport() { $('#btnPlay').textContent = S.audio.playing ? '❚❚' : '▶'; }

  // ------------------------------------------------------------------ Google Cast
  async function castApi(path, body) {
    const isGet = path.startsWith('status') || path.startsWith('devices');
    const r = await fetch('/api/cast/' + path, isGet ? {} : { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body || {}) });
    const j = await r.json(); if (j.error) { toast('Cast: ' + j.error, true); throw new Error(j.error); } return j;
  }
  function applyCastStatus(st) {
    const c = S.cast; c.state = st.state; c.at = performance.now();
    if (['PLAYING', 'PAUSED', 'BUFFERING'].includes(st.state)) c.pos = st.position;   // only a loaded session has a real position; otherwise keep the playhead
    const playing = st.state === 'PLAYING' || st.state === 'BUFFERING';
    syncLocalToCast();
    if (S.audio.playing !== playing) { S.audio.playing = playing; updateTransport(); }
    if (st.volume != null) $('#castVolume').value = Math.round(st.volume * 100);
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
      S.cast.poll = setInterval(async () => { try { applyCastStatus(await castApi('status')); } catch (e) { } }, 500);
      toast(`Casting to ${name}. Press play.`); }
    finally { busy(null); }
  }
  async function castEnd() {
    clearInterval(S.cast.poll); S.cast.poll = null; clearTimeout(S.cast.syncT); const wasPos = pos();
    try { await castApi('disconnect'); } catch (e) { }
    stopSource(); S.audio.localCast = false;
    S.cast.active = false; S.cast.state = 'IDLE'; S.audio.playing = false; S.audio.pos = wasPos; updateTransport();
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
  async function loadLights(rescan = false) {
    try {
      const list = await (await fetch('/api/devices' + (rescan ? '?rescan=1' : ''))).json();
      const prev = Object.fromEntries(S.lights.map(l => [l.ip, l.on]));
      S.lights = list.map(d => ({ ...d, on: prev[d.ip] !== undefined ? prev[d.ip] : !!d.on }));
      renderLights(); sendSelect(); autoLights();
    } catch (e) { toast('Could not list lights', true); }
  }
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
    $('#outputOn').checked = true; pushFrameIfIdle();
  }
  function renderLights() {
    const el = $('#lightList'); el.innerHTML = '';
    if (!S.lights.length) { el.innerHTML = '<div class="hint">No lights found. Enable LAN Control in the Govee app and rescan.</div>'; return; }
    for (const l of S.lights) {
      const row = document.createElement('label');
      row.innerHTML = `<input type="checkbox" ${l.on ? 'checked' : ''}> <span class="light-id"><input class="light-name" type="text" maxlength="40" spellcheck="false" placeholder="${l.sku}" title="Click to name this light"><div class="sku">${l.name ? l.sku + ' · ' : ''}${l.ip}${l.online === false ? ' · not answering' : ''}</div></span> <input class="seg" type="number" min="1" max="200" value="${l.segments}" title="Segments (LED zones)">`;
      const nm = row.querySelector('.light-name'); nm.value = l.name || '';
      nm.onkeydown = e => { if (e.key === 'Enter' || e.key === 'Escape') { if (e.key === 'Escape') nm.value = l.name || ''; nm.blur(); } };
      nm.onchange = async () => { l.name = nm.value.trim(); await fetch('/api/devices/name', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ mac: l.mac, device_id: l.device_id, name: l.name }) }); renderLights(); if (S.view.sel) inspector(); draw(); };
      if (l.online === false) row.style.opacity = '0.6';
      row.querySelector('input[type=checkbox]').onchange = e => { l.on = e.target.checked; sendSelect(); saveSelection(); draw(); };
      row.querySelector('.seg').onchange = async e => { l.segments = parseInt(e.target.value) || 1; await fetch('/api/devices/segments', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ key: l.mac || l.ip, segments: l.segments }) }); };
      el.appendChild(row);
    }
  }
  const selectedLights = () => S.lights.filter(l => l.on);
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
    ws.onopen = () => { S.wsOpen = true; $('#wsState').classList.add('on'); sendSelect(); autoLights(); if ($('#outputOn').checked) pushFrameIfIdle(); };
    ws.onclose = () => { S.wsOpen = false; S.outputActive = false; $('#wsState').className = 'dot'; setTimeout(connectWs, 2000); };
    ws.onmessage = e => { const m = JSON.parse(e.data); if (m.type === 'started') { S.outputActive = true; $('#wsState').classList.add('out'); } if (m.type === 'stopped') { S.outputActive = false; $('#wsState').classList.remove('out'); } };
    S.ws = ws;
  }
  const wsSend = o => { if (S.wsOpen) S.ws.send(JSON.stringify(o)); };
  function sendSelect() { wsSend({ type: 'select', ips: selectedLights().map(l => l.ip) }); }
  function startOutput() { if (!S.outputActive && selectedLights().length) wsSend({ type: 'start' }); }
  function stopOutput() { if (S.outputActive) wsSend({ type: 'stop' }); }
  function pushFrameIfIdle() { if ($('#outputOn').checked) { if (!S.outputActive) startOutput(); S.forceSend = true; } }

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
      kick: hitAt('kick', t), snare: hitAt('snare', t), hasDrums: (((a.events || {}).kick) || []).length > 0,
      hitsBetween: (stream, t0, t1) => {                          // the times of one instrument's hits in [t0, t1)
        const arr = (a.events || {})[stream] || []; const out = [];
        for (let i = Math.max(0, lastBefore(arr, t0)); i < arr.length && arr[i][0] < t1; i++) if (arr[i][0] >= t0) out.push(arr[i][0]);
        return out;
      },
      hook, fill,
      leadLevel: (cur.lead || [])[i] || 0, bassLevel: (cur.bass || [])[i] || 0, drumLevel: (cur.drum || [])[i] || 0,
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
      const spans = p.sections.length ? p.sections.map(x => [x.start, x.end]) : [[0, n / fps]];
      for (const [s0, s1] of spans) {
        const i0 = Math.max(0, Math.floor(s0 * fps)), i1 = Math.min(n, Math.ceil(s1 * fps));
        const usual = pct(Array.from(held.subarray(i0, i1)), 0.5) || ref;
        for (let i = i0; i < i1; i++) {
          const dip = Math.pow(Math.min(1, held[i] / (0.55 * usual + 1e-6)), 1.6);
          const overall = Math.max(0.3, Math.pow(Math.min(1, held[i] / (0.5 * ref + 1e-6)), 0.8));
          g[i] = Math.max(0.04, dip * overall);
        }
      }
      d = { g, sig }; DIM.set(a, d);
    }
    return d.g[clamp(Math.floor(t * fps), 0, d.g.length - 1)];
  }

  function renderFrame(t) {
    const p = P(); const b = timeToBeat(t); const f = featureAt(t); const lanePal = currentPalette(b);
    const music = musicAt(t);
    const kick = Math.exp(-frac(b) * 6);
    const master = parseFloat($('#master').value) / 100 * loudnessGain(t);
    const out = [];
    for (const L of selectedLights()) {
      const n = Math.max(1, L.segments); const dst = buf('dst' + L.ip, n); dst.fill(0);
      p.lanes.forEach((lane, li) => {
        if (lane.hidden) return;
        let clip = null;
        for (const c of lane.clips) if (b >= c.start && b < c.start + c.len && (!clip || c.start >= clip.start)) clip = c;
        if (!clip) return;
        const e = Effects.byId[clip.effect]; if (!e) return;
        const src = buf('src' + li + L.ip, n); src.fill(0);
        for (let i = 0; i < n; i++) src[i * 4 + 3] = 1;  // full coverage unless the effect clears
        const clipBeat = b - clip.start;
        const c = { n, out: src, t, beat: b, beatSec: Math.max(0.05, beatToTime(b + 1) - beatToTime(b)), phase: frac(b), bar: (b - p.phase) / 4, barPhase: frac((b - p.phase) / 4),
          clipT0: beatToTime(clip.start), clipT1: beatToTime(clip.start + clip.len),
          clipT: clamp(clipBeat / clip.len, 0, 1), clipBeats: clip.len, clipBeat, p: { ...Effects.defaults(clip.effect), ...clip.params },
          pal: clipPalette(clip, lanePal), energy: f.energy, bands: f.bands, kick, rnd: Effects.hash, music };
        e.render(c);
        let alpha = clip.opacity ?? 1;
        // Ducking, like sidechain compression: the layer dips on every kick and snare and swells back,
        // so the hits on top land on a dark strip and the whole show breathes with the drums.
        if (clip.duck > 0 && music.hasDrums) {
          let env = 0;
          for (const h of [music.kick, music.snare]) if (h && h.index >= 0) env = Math.max(env, Math.exp(-h.age / 0.16) * (0.6 + 0.4 * h.strength));
          alpha *= 1 - clamp(clip.duck, 0, 1) * env;
        }
        if (clip.fadeIn > 0) alpha *= clamp(clipBeat / clip.fadeIn, 0, 1);
        if (clip.fadeOut > 0) alpha *= clamp((clip.len - clipBeat) / clip.fadeOut, 0, 1);
        Effects.composite(dst, src, n, alpha, clip.blend || 'normal');
      });
      const flat = new Array(n * 3);
      for (let i = 0; i < n; i++) { flat[i * 3] = Math.round(clamp(dst[i * 4], 0, 1) * 255 * master); flat[i * 3 + 1] = Math.round(clamp(dst[i * 4 + 1], 0, 1) * 255 * master); flat[i * 3 + 2] = Math.round(clamp(dst[i * 4 + 2], 0, 1) * 255 * master); }
      out.push({ light: L, px: flat });
    }
    return out;
  }
  function drawPreview(frame) {
    const w = pv.clientWidth, h = pv.clientHeight; if (pv.width !== w * devicePixelRatio) { pv.width = w * devicePixelRatio; pv.height = h * devicePixelRatio; }
    pctx.setTransform(devicePixelRatio, 0, 0, devicePixelRatio, 0, 0);
    pctx.fillStyle = '#0b0c0f'; pctx.fillRect(0, 0, w, h);
    if (!frame || !frame.length) { pctx.fillStyle = '#555'; pctx.font = '12px system-ui'; pctx.fillText('No lights selected — tick a light on the left to preview.', 12, 20); return; }
    const rowH = Math.min(36, (h - 8) / frame.length);
    frame.forEach((fr, r) => {
      const y = 6 + r * rowH; const n = fr.px.length / 3; const x0 = 110, x1 = w - 12; const pw = (x1 - x0) / n;
      pctx.fillStyle = '#9aa'; pctx.font = '11px system-ui'; pctx.fillText(lightLabel(fr.light), 8, y + rowH / 2 + 4);
      for (let i = 0; i < n; i++) {
        pctx.fillStyle = `rgb(${fr.px[i * 3]},${fr.px[i * 3 + 1]},${fr.px[i * 3 + 2]})`;
        const px = x0 + i * pw; pctx.beginPath(); pctx.roundRect(px + 1, y + 3, Math.max(1, pw - 2), rowH - 8, 3); pctx.fill();
      }
    });
  }
  function loop() {
    requestAnimationFrame(loop);
    if (!S.analysis) return;
    const t = pos();
    if (S.audio.playing && S.view.follow !== false) followPlayhead(t);
    $('#timeDisplay').textContent = fmtTime(t);
    $('#barDisplay').textContent = 'bar ' + barLabel(timeToBeat(t));
    const frame = renderFrame(t); drawPreview(frame);
    if (S.dirty) { S.dirty = false; _draw(); }
    compose();
    drawOverview();
  }
  // Light output runs on its own clock in a Web Worker: browsers freeze
  // requestAnimationFrame and throttle timers when the tab is not in front,
  // which would silently stop the lights. Worker ticks keep coming regardless.
  function outputTick() {
    if (!S.analysis || !S.outputActive) return;
    if (!S.audio.playing && !S.forceSend && performance.now() - S.lastLightSend < 200) return;   // paused: ~5 fps hold
    const t = pos();
    const lt = S.audio.playing ? t + (parseFloat($('#latency').value) || 0) / 1000 : t;
    const lf = renderFrame(lt);
    wsSend({ type: 'frame', frames: lf.map(f => f.px) });
    S.forceSend = false; S.framesSent = (S.framesSent || 0) + 1; S.lastLightSend = performance.now();
  }
  (function startOutputClock() {
    try {
      const src = 'setInterval(function(){ postMessage(0); }, 16);';
      const w = new Worker(URL.createObjectURL(new Blob([src], { type: 'application/javascript' })));
      w.onmessage = outputTick;
    } catch (e) { setInterval(outputTick, 16); }
    setInterval(() => {  // once a second: show the real send rate next to the connection dot
      const n = S.framesSent || 0; const fps = n - (S.framesSentPrev || 0); S.framesSentPrev = n;
      const dot = $('#wsState'); dot.title = S.outputActive ? `Lights: ${fps} frames/s` : 'Server connection';
      const lbl = $('#fpsLabel'); if (lbl) lbl.textContent = S.outputActive ? `${fps} fps` : '';
    }, 1000);
  })();
  function fmtTime(t) { const m = Math.floor(t / 60), s = t - m * 60; return `${m}:${s.toFixed(3).padStart(6, '0')}`; }

  // ------------------------------------------------------------------ timeline geometry
  const xOf = t => HEADER + (t - S.view.scroll) * S.view.pps;
  const tOf = x => S.view.scroll + (x - HEADER) / S.view.pps;
  const lanesTop = () => ROW.ruler + ROW.sections + ROW.wave + ROW.pal;
  const laneTop = i => lanesTop() + i * ROW.lane - (S.view.vscroll || 0);
  const ADD_ROW = 26;  // the "+ layer" strip under the last layer
  function lanesOverflow() { const h = tl.clientHeight; return Math.max(0, lanesTop() + P().lanes.length * ROW.lane + ADD_ROW - h); }
  function vscrollTo(v) { S.view.vscroll = clamp(v, 0, lanesOverflow()); draw(); }
  const palTop = () => ROW.ruler + ROW.sections + ROW.wave;
  const waveTop = () => ROW.ruler + ROW.sections;
  function viewWidth() { return tl.clientWidth - HEADER; }
  function zoomFit() { if (!S.analysis) return; if (viewWidth() <= 0) { S.view.fitPending = true; return; } S.view.pps = Math.max(1, viewWidth() / duration()); S.view.scroll = 0; draw(); }
  const wheelZoom = e => clamp(Math.pow(1.002, -(e.deltaMode === 1 ? e.deltaY * 16 : e.deltaY)), 0.4, 2.5);
  function zoomAt(factor, xPix) {
    const t = tOf(xPix); const minPps = viewWidth() / Math.max(1, duration());
    S.view.pps = clamp(S.view.pps * factor, minPps, 600);
    S.view.scroll = clamp(t - (xPix - HEADER) / S.view.pps, 0, Math.max(0, duration() - viewWidth() / S.view.pps)); draw();
  }
  function scrollTo(s) { S.view.scroll = clamp(s, 0, Math.max(0, duration() - viewWidth() / S.view.pps)); draw(); }
  function followPlayhead(t) { const x = xOf(t); const w = tl.clientWidth; if (x > w - 40 || x < HEADER) scrollTo(t - 0.1 * viewWidth() / S.view.pps); }

  // ------------------------------------------------------------------ drawing
  // The static timeline (grid, waveform, clips) renders into an offscreen canvas only
  // when something changes; the playhead and drag ghost are composed on top every frame.
  const off = document.createElement('canvas'), octx = off.getContext('2d');
  function draw() { S.dirty = true; S.ovDirty = true; }
  function _draw() {
    if (!S.analysis) return;
    const w = tl.clientWidth, h = tl.clientHeight;
    if (!w || !h) { S.dirty = true; return; }   // hidden or collapsed: try again next frame
    if (S.view.fitPending) { S.view.fitPending = false; S.view.pps = Math.max(1, viewWidth() / duration()); S.view.scroll = 0; }
    const W = Math.floor(w * devicePixelRatio), H = Math.floor(h * devicePixelRatio);
    if (tl.width !== W || tl.height !== H) { tl.width = W; tl.height = H; }
    if (off.width !== W || off.height !== H) { off.width = W; off.height = H; }
    const g = octx; g.setTransform(devicePixelRatio, 0, 0, devicePixelRatio, 0, 0);
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
      g.fillStyle = SECTION_COLORS[s.type] || '#555'; g.globalAlpha = 0.85; g.fillRect(Math.max(HEADER, x0), sy + 2, x1 - x0 - 1, ROW.sections - 4); g.globalAlpha = 1;
      g.fillStyle = '#fff'; g.font = 'bold 10px system-ui'; g.fillText(s.type.toUpperCase(), Math.max(HEADER, x0) + 4, sy + 14);
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
      const y = laneTop(li); g.fillStyle = li % 2 ? '#14161b' : '#121317'; g.fillRect(HEADER, y, w, ROW.lane); g.strokeStyle = '#2f333d'; g.beginPath(); g.moveTo(HEADER, y); g.lineTo(w, y); g.stroke();
      g.globalAlpha = lane.hidden ? 0.3 : 1;
      for (const c of lane.clips) {
        const x0 = xOf(beatToTime(c.start)), x1 = xOf(beatToTime(c.start + c.len)); if (x1 < HEADER || x0 > w) continue;
        const e = Effects.byId[c.effect]; const sw = e ? e.swatch : ['#666', '#333'];
        const grad = g.createLinearGradient(x0, y, x0, y + ROW.lane); grad.addColorStop(0, sw[0]); grad.addColorStop(1, sw[1]);
        g.fillStyle = grad; g.globalAlpha = 0.55; g.beginPath(); g.roundRect(Math.max(HEADER, x0), y + 4, x1 - x0 - 2, ROW.lane - 8, 5); g.fill(); g.globalAlpha = 1;
        const isSel = isSelected(c.id);
        g.strokeStyle = isSel ? '#fff' : 'rgba(255,255,255,0.25)'; g.lineWidth = isSel ? 2 : 1;
        g.beginPath(); g.roundRect(Math.max(HEADER, x0), y + 4, x1 - x0 - 2, ROW.lane - 8, 5); g.stroke(); g.lineWidth = 1;
        g.save(); g.beginPath(); g.rect(Math.max(HEADER, x0), y, x1 - x0 - 2, ROW.lane); g.clip();
        g.fillStyle = '#fff'; g.font = 'bold 11px system-ui'; g.fillText(e ? e.name : c.effect, Math.max(HEADER, x0) + 6, y + 19);
        g.fillStyle = 'rgba(255,255,255,0.7)'; g.font = '10px system-ui'; g.fillText(clipSummary(c), Math.max(HEADER, x0) + 6, y + 31); g.restore();
        // the clip's own colors, as a bar along its bottom edge
        const cols = clipPaletteHex(c); const bx0 = Math.max(HEADER, x0) + 5, bx1 = Math.min(w, x1) - 7;
        if (bx1 > bx0 + 4) { const pg = g.createLinearGradient(bx0, 0, bx1, 0); cols.forEach((col, i) => pg.addColorStop(i / Math.max(1, cols.length - 1), col)); g.fillStyle = pg; g.beginPath(); g.roundRect(bx0, y + ROW.lane - 12, bx1 - bx0, 6, 3); g.fill(); }
      }
      g.globalAlpha = 1;
    });
    g.restore();
    // lane headers
    g.fillStyle = '#1a1c22'; g.fillRect(0, 0, HEADER, h); g.strokeStyle = '#2f333d'; g.beginPath(); g.moveTo(HEADER - 0.5, 0); g.lineTo(HEADER - 0.5, h); g.stroke();
    g.fillStyle = '#8b92a3'; g.font = '11px system-ui'; g.fillText('sections', 8, sy + 14); g.fillText('waveform', 8, wy + 16);
    g.save(); g.beginPath(); g.rect(0, lanesTop(), w, h - lanesTop()); g.clip();
    p.lanes.forEach((lane, li) => {
      const ly = laneTop(li);
      g.fillStyle = lane.hidden ? '#6b7280' : '#e6e8ee'; g.font = 'bold 12px system-ui'; g.fillText(`Layer ${li + 1}`, 8, ly + 22);
      g.fillStyle = '#8b92a3'; g.font = '10px system-ui'; g.fillText(lane.hidden ? 'hidden' : li === p.lanes.length - 1 ? 'top' : li === 0 ? 'bottom' : '', 8, ly + 36);
      // eye toggle
      const ex = HEADER - 16, ey = ly + 22; g.strokeStyle = lane.hidden ? '#6b7280' : '#e6e8ee'; g.fillStyle = g.strokeStyle; g.lineWidth = 1.5;
      g.beginPath(); g.ellipse(ex, ey, 8, 5, 0, 0, Math.PI * 2); g.stroke();
      if (!lane.hidden) { g.beginPath(); g.arc(ex, ey, 2.4, 0, Math.PI * 2); g.fill(); }
      else { g.beginPath(); g.moveTo(ex - 9, ey + 7); g.lineTo(ex + 9, ey - 7); g.stroke(); }
      g.lineWidth = 1;
    });
    const ay = laneTop(p.lanes.length); g.fillStyle = '#1f2229'; g.fillRect(0, ay, w, ADD_ROW); g.fillStyle = '#6c8cff'; g.font = 'bold 11px system-ui'; g.fillText('+ add layer', 8, ay + 17);
    g.fillStyle = '#8b92a3'; g.font = '10px system-ui'; g.fillText('right-click a layer name to insert, remove or reorder', HEADER + 8, ay + 17);
    g.restore();
  }
  function clipSummary(c) { const e = Effects.byId[c.effect]; if (!e) return ''; const parts = []; if (Array.isArray(c.palette)) parts.push('custom colors'); else if (typeof c.palette === 'string') parts.push(c.palette); for (const [k, d] of Object.entries(e.params)) { const v = c.params[k] ?? d.def; if (d.type === 'beats') parts.push(`${v}b`); } if (c.opacity != null && c.opacity < 1) parts.push(`${Math.round(c.opacity * 100)}%`); if (c.blend && c.blend !== 'normal') parts.push(c.blend); return parts.join(' · '); }
  function compose() {
    if (!S.analysis || !off.width || !off.height) return;   // nothing rendered yet (or a zero-size canvas)
    const w = tl.clientWidth, h = tl.clientHeight; const g = tctx;
    g.setTransform(1, 0, 0, 1, 0, 0); g.drawImage(off, 0, 0);
    g.setTransform(devicePixelRatio, 0, 0, devicePixelRatio, 0, 0);
    if (S.view.hoverDrop) { const d = S.view.hoverDrop; g.fillStyle = d.replace ? 'rgba(255,94,168,0.35)' : 'rgba(108,140,255,0.35)'; g.fillRect(d.x, d.y, d.w, d.h); if (d.replace) { g.strokeStyle = '#ff5ea8'; g.lineWidth = 2; g.strokeRect(d.x, d.y, d.w, d.h); g.lineWidth = 1; g.fillStyle = '#fff'; g.font = 'bold 11px system-ui'; g.fillText('replace', d.x + 6, d.y + d.h - 6); } }
    const x = xOf(pos());
    if (x >= HEADER && x <= w) { g.fillStyle = '#fff'; g.fillRect(x - 0.5, 0, 1.5, h); g.beginPath(); g.moveTo(x - 5, 0); g.lineTo(x + 5, 0); g.lineTo(x, 8); g.fill(); }
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
    for (const c of p.lanes[li].clips) { const x0 = xOf(beatToTime(c.start)), x1 = xOf(beatToTime(c.start + c.len)); if (x >= x0 && x <= x1) return { type: 'clip', lane: li, id: c.id, edge: x - x0 < 7 ? 'l' : x1 - x < 7 ? 'r' : null, b }; }
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
    const r = tl.getBoundingClientRect(); const x = e.clientX - r.left, y = e.clientY - r.top; const h = hitTest(x, y); if (!h) return;
    const p = P(); S.view.mouse = { x, y };
    if (h.type === 'ruler' || h.type === 'wave') {
      if (h.type === 'wave' && $('#beatEdit').checked) { beginChange(); const t = h.t; const i = p.beats.findIndex(b => b > t); if (i < 0) p.beats.push(t); else p.beats.splice(i, 0, t); S.view.sel = { type: 'beat', index: i < 0 ? p.beats.length - 1 : i }; inspector(); changed(); return; }
      seek(h.t); S.view.sel = null; inspector(); S.view.drag = { type: 'scrub' }; draw(); return;
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
    const r = tl.getBoundingClientRect(); const x = e.clientX - r.left, y = e.clientY - r.top; const d = S.view.drag;
    if (!d) { const h = hitTest(x, y); tl.style.cursor = h && h.type === 'clip' && h.edge ? 'ew-resize' : h && (h.type === 'section-edge' || (h.type === 'beat' && $('#beatEdit').checked)) ? 'col-resize' : h && (h.type === 'clip' || h.type === 'drop') ? 'grab' : h && (h.type === 'laneEye' || h.type === 'addLane') ? 'pointer' : 'default'; return; }
    const p = P(); const t = clamp(tOf(x), 0, duration()); const b = timeToBeat(t);
    if (d.type === 'scrub') { seek(t); return; }
    if (d.type === 'marquee') {
      d.x = clamp(x, HEADER, tl.clientWidth); d.y = clamp(y, lanesTop(), tl.clientHeight);
      // clips inside the box show as selected while it is still being drawn
      const hits = Math.abs(d.x - d.x0) < 4 && Math.abs(d.y - d.y0) < 4 ? [] : clipsInRect(Math.min(d.x0, d.x), Math.min(d.y0, d.y), Math.max(d.x0, d.x), Math.max(d.y0, d.y));
      if (hits.length !== d.hits.length || hits.some((id, i) => id !== d.hits[i])) { d.hits = hits; draw(); }
      return;
    }
    if (d.type === 'group') {
      const delta = snapBeat(d.items[0].start0 + (b - d.b0)) - d.items[0].start0;
      if (delta !== 0) d.tried = true;   // a real drag, even if the block is boxed in and cannot go there
      if (delta !== d.delta && moveGroup(d.items, delta)) { d.delta = delta; d.moved = true; draw(); }
      return;
    }
    if (d.type === 'beat') { p.beats[d.index] = t; p.beats.sort((a, b) => a - b); d.index = p.beats.indexOf(t); S.view.sel = { type: 'beat', index: d.index }; draw(); return; }
    if (d.type === 'drop') { p.drops[d.index] = beatToTime(snapBeat(b)); draw(); return; }
    if (d.type === 'section-edge') { const s = p.sections[d.index], prev = p.sections[d.index - 1]; const nt = beatToTime(snapBeat(b)); if (nt > prev.start + 0.5 && nt < s.end - 0.5) { s.start = nt; prev.end = nt; if (d.marker !== null && d.marker !== undefined) p.drops[d.marker] = nt; draw(); } return; }
    if (d.type === 'clip' || d.type === 'palclip') {
      const c = d.clip; d.moved = true;
      const laneOf = d.type === 'clip' ? p.lanes[d.lane] : null;
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
    if (d.type === 'group') {
      if (!d.moved) { S.undo.pop(); if (!d.tried) setSelection([d.id]); return; }   // a plain click on a member picks just that clip
      inspector(); changed(); return;
    } if (d.type === 'clip' && d.copy && d.moved) { /* alt-drag copies: restore original and add copy */ const orig = { ...d.clip, id: uid(), start: d.start0, len: d.len0, params: { ...d.clip.params } }; P().lanes[d.lane].clips.push(orig); } if (d.type === 'section-edge' && d.marker !== null) P().drops.sort((a, b) => a - b); if (d.type === 'clip' || d.type === 'palclip') inspector(); changed(); });
  tl.addEventListener('dblclick', e => { const r = tl.getBoundingClientRect(); const h = hitTest(e.clientX - r.left, e.clientY - r.top); if (!h) return; if (h.type === 'section') { beginChange(); const s = P().sections[h.index]; s.type = SECTION_TYPES[(SECTION_TYPES.indexOf(s.type) + 1) % SECTION_TYPES.length]; inspector(); changed(); } });
  tl.addEventListener('wheel', e => {
    e.preventDefault(); const r = tl.getBoundingClientRect(); const x = e.clientX - r.left;
    // plain wheel zooms around the pointer; shift pans; ctrl/alt scrolls the layer stack
    if (e.ctrlKey || e.metaKey || e.altKey) return vscrollTo((S.view.vscroll || 0) + (e.deltaY || e.deltaX));
    if (e.shiftKey || Math.abs(e.deltaX) > Math.abs(e.deltaY)) return scrollTo(S.view.scroll + (e.shiftKey ? e.deltaY : e.deltaX) / S.view.pps * 0.8);
    zoomAt(wheelZoom(e), x);
  }, { passive: false });
  tl.addEventListener('contextmenu', e => { e.preventDefault(); const r = tl.getBoundingClientRect(); const x = e.clientX - r.left, y = e.clientY - r.top; const h = hitTest(x, y); if (!h) return;
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
  const overlaps = (lane, start, len, ignore) => lane.clips.some(c => c !== ignore && start < c.start + c.len && c.start < start + len);
  function freeLaneFor(start, len, from = 0, ignore = null) {
    const p = P();
    // a clip may ask for a layer several above the top one: add every layer up to it, not just one
    for (let i = Math.max(0, from); ; i++) { while (i >= p.lanes.length) p.lanes.push(newLane()); if (!overlaps(p.lanes[i], start, len, ignore)) return i; }
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
    len = len ?? (e.cat === 'accent' ? (effectId === 'riser' ? 16 : 2) : 16);
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
    if (sel.type === 'palclip') p.paletteClips = p.paletteClips.filter(c => c.id !== sel.id);
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
    if (!clipboard) return; beginChange(); const p = P(); const ids = [];
    for (const k of clipboard.clips) {
      const c = { ...k.data, id: uid(), start: Math.max(0, startBeat + k.rel), params: { ...k.data.params } };
      const li = freeLaneFor(c.start, c.len, Math.max(0, laneIdx + k.lane));
      p.lanes[li].clips.push(c); ids.push(c.id);
    }
    setSelection(ids); changed();
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
  function tapTempo() { const now = performance.now(); S.tap = S.tap.filter(t => now - t < 3000); S.tap.push(now); if (S.tap.length >= 4) { const iv = []; for (let i = 1; i < S.tap.length; i++) iv.push(S.tap[i] - S.tap[i - 1]); const bpm = 60000 / (iv.reduce((a, b) => a + b, 0) / iv.length); $('#bpm').value = bpm.toFixed(2); toast(`Tap: ${bpm.toFixed(1)} bpm — press Regrid to apply`); } }

  // ------------------------------------------------------------------ drag & drop from the library
  function libraryDrag(el, kind, id) { el.draggable = true; el.addEventListener('dragstart', e => { e.dataTransfer.setData('text/plain', kind + ':' + id); e.dataTransfer.effectAllowed = 'copy'; }); }
  tl.addEventListener('dragover', e => { e.preventDefault(); e.dataTransfer.dropEffect = 'copy'; const r = tl.getBoundingClientRect(); const x = e.clientX - r.left, y = e.clientY - r.top; const h = hitTest(x, y); if (!h) { S.view.hoverDrop = null; return; } const b = snapBeat(h.b ?? timeToBeat(h.t ?? tOf(x))); if (h.type === 'clip') { const { clip } = findClip(h.id); const x0 = xOf(beatToTime(clip.start)), x1 = xOf(beatToTime(clip.start + clip.len)); S.view.hoverDrop = { x: x0, y: laneTop(h.lane) + 4, w: x1 - x0 - 2, h: ROW.lane - 8, replace: true }; }
    else if (h.type === 'lane') S.view.hoverDrop = { x: xOf(beatToTime(b)), y: laneTop(h.lane) + 4, w: 16 * 60 / P().bpm * S.view.pps, h: ROW.lane - 8 }; else S.view.hoverDrop = null; });
  tl.addEventListener('dragleave', () => { S.view.hoverDrop = null; });
  tl.addEventListener('drop', e => {
    e.preventDefault(); S.view.hoverDrop = null; const data = e.dataTransfer.getData('text/plain'); if (!data || !P()) { draw(); return; }
    const [kind, id] = data.split(':'); const r = tl.getBoundingClientRect(); const x = e.clientX - r.left, y = e.clientY - r.top; const h = hitTest(x, y); if (!h) return;
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
      const items = Effects.list.filter(e => e.cat === cat && (!q || e.name.toLowerCase().includes(q) || e.desc.toLowerCase().includes(q)));
      if (!items.length) continue; const h = document.createElement('div'); h.className = 'cat'; h.textContent = label; list.appendChild(h);
      for (const e of items) { const card = document.createElement('div'); card.className = 'effect-card'; card.title = e.desc + '\nDrag onto a layer, or double-click to add at the playhead.'; card.innerHTML = `<div class="sw" style="background:linear-gradient(90deg,${e.swatch[0]},${e.swatch[1]})"></div><div><div class="nm">${e.name}</div><div class="ds">${e.desc.slice(0, 44)}${e.desc.length > 44 ? '…' : ''}</div></div>`; libraryDrag(card, 'effect', e.id); card.ondblclick = () => { if (P()) addClip(e.cat === 'accent' ? P().lanes.length - 1 : 0, e.id, snapBeat(timeToBeat(pos()))); }; list.appendChild(card); }
    }
  }
  function renderPalettes() {
    const list = $('#paletteList'); list.innerHTML = ''; const pals = P() ? P().palettes : Effects.PALETTES;
    for (const [name, cols] of Object.entries(pals)) { const card = document.createElement('div'); card.className = 'palette-card'; card.innerHTML = `<span class="nm">${name}</span><div class="bar" style="background:linear-gradient(90deg,${cols.join(',')})"></div>`; libraryDrag(card, 'palette', name); card.title = 'Drag onto a clip to give it these colors. Double-click to make it the default for clips without their own.'; card.ondblclick = () => { if (P()) { beginChange(); P().defaultPalette = name; changed(); toast(`Default palette: ${name}`); } }; list.appendChild(card); }
  }

  // ------------------------------------------------------------------ inspector
  function inspector() {
    const el = $('#inspector'); const sel = S.view.sel; const p = P();
    if (!p) { el.innerHTML = '<div class="panel"><h3>Inspector</h3><div class="hint">Open a song to begin.</div></div>'; return; }
    const help = `<div class="panel"><h3>Shortcuts</h3><div class="help">
      <kbd>Space</kbd> play / pause &nbsp; <kbd>Home</kbd> start<br>Wheel zooms · <kbd>Shift</kbd>+wheel pans · <kbd>Ctrl</kbd>+wheel scrolls layers<br>Drag the strip on top to scroll, its edges to zoom<br>
      Click waveform to seek, drag the ruler to scrub<br>Drag effects onto layers; drag edges to resize<br>Drag across empty space to box-select clips · <kbd>Shift</kbd>+click adds · <kbd>Ctrl</kbd>+<kbd>A</kbd> selects all<br>Drag any selected clip to move the whole block<br>Drop an effect on a clip to swap the effect, same timing<br>Drag a palette onto a clip to recolor it<br>Click a layer's eye to hide it<br><kbd>Alt</kbd>+drag copies a clip<br>
      <kbd>Del</kbd> delete · <kbd>Ctrl</kbd>+<kbd>C</kbd>/<kbd>X</kbd>/<kbd>V</kbd>/<kbd>D</kbd> copy / cut / paste / duplicate<br><kbd>Ctrl</kbd>+<kbd>Z</kbd>/<kbd>Y</kbd> undo / redo · <kbd>Ctrl</kbd>+<kbd>S</kbd> save<br>
      Double-click a section to cycle its type<br>Right-click anything for more<br>
      <b>Beat edit</b> on: click adds a beat, <kbd>Space</kbd> taps while playing, <kbd>Enter</kbd> plays</div></div>`;
    if (!sel) {
      const g = S.analysis && S.analysis.genre; const auto = g && STYLE_NAMES[g.style] ? g.style : null;
      const heard = g ? [...(g.genres || []), ...((g.spotify || []).length ? ['Spotify: ' + g.spotify.slice(0, 2).join(', ')] : [])].join(', ') : '';
      const styleRow = `<div class="row"><label title="How hard the lights go when you auto-build. Chill never strobes; groove moves but lifts drops without flashing; dance flashes on the beat; rave goes all out.">Show style</label><select id="songStyle">
        <option value="">Auto${auto ? ': ' + STYLE_NAMES[auto] : ''}</option>${Object.entries(STYLE_NAMES).map(([k, n]) => `<option value="${k}" ${p.settings && p.settings.style === k ? 'selected' : ''}>${n}</option>`).join('')}</select></div>
        ${heard ? `<div class="row"><label>Sounds like</label><span class="genre-heard">${esc(heard)}</span></div>` : ''}`;
      el.innerHTML = `<div class="panel"><h3>Song</h3>${styleRow}<div class="row"><label>Default palette</label><select id="defPal">${Object.keys(p.palettes).map(n => `<option ${n === p.defaultPalette ? 'selected' : ''}>${n}</option>`).join('')}</select></div><div class="row"><label title="When the music goes quiet, every light dims with it, even in a drop">Dim with the music</label><input id="followLoud" type="checkbox" ${p.settings.followLoudness === false ? '' : 'checked'}></div><div class="row"><label>Beats</label><span>${p.beats.length}</span></div><div class="row"><label>Sections</label><span>${p.sections.length}${S.analysis && S.analysis.structure_source === 'loudness+songformer' ? ' · timed by SongFormer' : ''}</span></div>${S.analysis && S.analysis.structure_source === 'loudness' ? `<div class="hint warn">Section timing was not refined: ${esc(S.analysis.structure_error || 'SongFormer did not run')}. Boundaries may be a bar or two off. Press Re-detect to try again.</div>` : ''}<div class="row"><label>Drops</label><span>${p.drops.length}</span></div><div class="row"><label>Lights selected</label><span>${selectedLights().length}</span></div></div>` + help; $('#defPal').onchange = e => { beginChange(); p.defaultPalette = e.target.value; changed(); };
      $('#followLoud').onchange = e => { beginChange(); p.settings.followLoudness = e.target.checked; changed(); pushFrameIfIdle(); };
      $('#songStyle').onchange = e => { beginChange(); p.settings = p.settings || {}; p.settings.style = e.target.value; changed(); inspector();
        toast(`Show style: ${e.target.value ? STYLE_NAMES[e.target.value] : 'Auto (' + STYLE_NAMES[showStyle()] + ')'}. Press Auto-build show to rebuild in it.`); };
      return; }
    if (sel.type === 'clip' && groupSize() > 1) {
      const items = selectedClips(); const n = items.length;
      const lanes = new Set(items.map(i => i.li)); const b0 = Math.min(...items.map(i => i.clip.start)), b1 = Math.max(...items.map(i => i.clip.start + i.clip.len));
      const op = items.every(i => (i.clip.opacity ?? 1) === (items[0].clip.opacity ?? 1)) ? (items[0].clip.opacity ?? 1) : null;
      const single = items.every(i => Array.isArray(i.clip.palette) && i.clip.palette.length === 1) ? items[0].clip.palette[0] : null;
      el.innerHTML = `<div class="panel"><div class="title">${n} clips selected</div><div class="desc">Across ${lanes.size} layer${lanes.size === 1 ? '' : 's'}, bar ${barLabel(b0)} to bar ${barLabel(b1)}. Drag any of them to move the whole block; changes here apply to all of them.</div>
        <div class="row"><label>Opacity</label><input id="gOp" type="range" min="0" max="1" step="0.01" value="${op ?? 1}"><span class="val">${op === null ? 'mixed' : Math.round(op * 100) + '%'}</span></div>
        <div class="btns"><button id="gCopy" class="small">Copy</button><button id="gCut" class="small">Cut</button><button id="gDup" class="small">Duplicate</button><button id="gDel" class="small">Delete</button></div></div>
        <div class="panel"><h3>Colors <span class="hint">all selected</span></h3><div class="pal-grid" id="gPalGrid">
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
        <div class="row"><label>Blend</label><select id="cBlend">${Object.entries(Effects.BLENDS).map(([k, v]) => `<option value="${k}" ${(clip.blend || 'normal') === k ? 'selected' : ''}>${v}</option>`).join('')}</select></div>
        <div class="row"><label>Fade in (beats)</label><input id="cFi" type="number" step="0.5" min="0" value="${clip.fadeIn || 0}"></div>
        <div class="row"><label>Fade out (beats)</label><input id="cFo" type="number" step="0.5" min="0" value="${clip.fadeOut || 0}"></div></div>
        <div class="panel"><h3>Colors <span class="hint">this clip</span></h3>
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
        if (d.type === 'beats') html += `<div class="row"><label>${d.label}</label><select data-p="${k}">${Effects.BEAT_OPTS.map(o => `<option value="${o}" ${o == v ? 'selected' : ''}>${o}</option>`).join('')}</select></div>`;
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
      $('#cDuck').oninput = ev => { clip.duck = parseFloat(ev.target.value); ev.target.nextElementSibling.textContent = Math.round(clip.duck * 100) + '%'; upd(); };
      $('#cDuck').onchange = () => beginChange();
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
        <div class="btns"><button id="secSplit" class="small">Split at playhead</button><button id="secBuild" class="small">Auto-build this section</button><button id="secDel" class="small">Merge away</button></div>
        <div class="hint" style="margin-top:8px">Drag a section's left edge in the section band to move the boundary.</div></div>`;
      $('#secType').onchange = ev => { beginChange(); s.type = ev.target.value; changed(); };
      $('#secSplit').onclick = () => { const t = pos(); if (t <= s.start + 0.5 || t >= s.end - 0.5) return toast('Move the playhead inside the section first'); beginChange(); const nt = beatToTime(snapBeat(timeToBeat(t))); p.sections.splice(sel.index + 1, 0, { start: nt, end: s.end, type: s.type }); s.end = nt; inspector(); changed(); };
      $('#secBuild').onclick = () => { beginChange(); buildSection(s, sel.index, true); changed(); };
      $('#secDel').onclick = () => deleteSelection();
      return;
    }
    if (sel.type === 'beat') { el.innerHTML = `<div class="panel"><div class="title">Beat ${sel.index + 1}</div><div class="row"><label>Time</label><span>${fmtTime(p.beats[sel.index])}</span></div><div class="hint">Drag it on the waveform (beat edit mode), or press Delete to remove it. Use <b>Regrid</b> to rebuild an even grid from the BPM.</div><div class="btns"><button id="bDel" class="small">Delete beat</button></div></div>`; $('#bDel').onclick = () => deleteSelection(); return; }
    if (sel.type === 'drop') { el.innerHTML = `<div class="panel"><div class="title">Drop marker</div><div class="row"><label>Time</label><span>${fmtTime(p.drops[sel.index])}</span></div><div class="hint">Drag it on the ruler. Auto-build puts a blast here.</div><div class="btns"><button id="dDel" class="small">Delete marker</button></div></div>`; $('#dDel').onclick = () => deleteSelection(); return; }
  }

  // ------------------------------------------------------------------ auto-build
  /** the song's palette, rotated so each section reads differently while staying in family */
  function paletteVariant(name, n) {
    const cols = (P().palettes[name] || ['#ffffff']).slice();
    const k = ((n % cols.length) + cols.length) % cols.length;
    return cols.slice(k).concat(cols.slice(0, k));
  }
  /** chill | groove | dance | rave: chosen for the song from its genre, unless set by hand in the Song panel */
  const STYLE_NAMES = { chill: 'Chill', groove: 'Groove', dance: 'Dance', rave: 'Rave' };
  function showStyle() {
    const p = P(), g = S.analysis && S.analysis.genre;
    if (p && p.settings && STYLE_NAMES[p.settings.style]) return p.settings.style;
    if (g && STYLE_NAMES[g.style]) return g.style;
    return ((S.analysis && S.analysis.mood && S.analysis.mood.drive) || 0.6) > 0.72 ? 'dance' : 'groove';
  }
  function autoBuild(quiet = false) {
    if (!P()) return; if (!quiet) beginChange(); const p = P();
    for (const lane of p.lanes) lane.clips = []; p.paletteClips = [];
    while (p.lanes.length < DEFAULT_LANES) p.lanes.push(newLane());
    // one palette carries the whole song, chosen from its key, brightness and drive
    // A handful of palettes that suit this song, cycled and rotated section by section:
    // coherent across the track, but two drops never open on the same color.
    const mood = S.analysis.mood || {};
    S.songPals = (mood.palettes || [mood.palette]).filter(n => p.palettes[n]);
    if (S.albumPalette && S.albumPalette.length) { p.palettes.Album = Effects.solidPalette(S.albumPalette); S.songPals = ['Album']; }   // the cover's own colors
    if (!S.songPals.length) S.songPals = [p.defaultPalette];
    S.songPal = S.songPals[0];
    p.defaultPalette = S.songPal;
    const counts = {};
    p.sections.forEach((s, i) => { counts[s.type] = (counts[s.type] || 0); buildSection(s, i, false, counts[s.type]); counts[s.type]++; });
    // drop markers that are not section starts still get a blast (a chill song never flashes, so it gets none)
    if (showStyle() !== 'chill') for (const d of p.drops) { const b = Math.round(timeToBeat(d)); if (!p.lanes.some(l => l.clips.some(c => c.effect === 'dropblast' && Math.abs(c.start - b) < 3))) clip(3, 'dropblast', b, 2); }
    S.view.sel = null; inspector(); changed();
    if (quiet) return;
    const a = S.analysis, hooks = (a.hook && a.hook.times || []).length, fills = (a.fills || []).length;
    const g = S.analysis.genre;
    const bits = [`${STYLE_NAMES[showStyle()]} style${g && g.genres && g.genres.length && !(p.settings && p.settings.style) ? ` (${g.genres.slice(0, 2).join(', ')})` : ''}`,
      `${p.sections.length} sections`, `palettes ${(S.songPals || []).join(' / ')}`];
    if (hooks) bits.push(`riff x${hooks}`);
    if (fills) bits.push(`${fills} fills`);
    if ((a.events && a.events.lead || []).length) bits.push(`${a.events.lead.length} lead hits`);
    toast('Built: ' + bits.join(' \u00b7 '));
  }
  // Effects that are mostly dark with a few bright things moving (comets, sparks, sweeps): laid
  // over a base, they take the opposite colors, so a comet over red is cyan-blue and stands out.
  const OVERLAY = new Set(['comet', 'dualcomet', 'meteors', 'orbit', 'sparkle', 'ghostspark', 'leadtrace', 'hookstrike',
                           'sweepout', 'ripple', 'colorslam', 'fillrush', 'pulse']);
  // Concert lighting in three tiers. The bed sets the color and the mood and stays low; motion keeps
  // things alive in the middle; the hits (drums, strikes, flashes) own the top of the range.
  const BED = new Set(['gradient', 'aurora', 'breathe', 'colorwash', 'wave', 'solid', 'ember', 'twinkle']);
  const HIT = new Set(['pulse', 'hookstrike', 'flash', 'dropblast', 'whiteflash', 'colorslam', 'sweepout', 'ripple', 'strobe', 'fillrush']);
  const FREE = new Set(['riser', 'strobeRiser', 'fadein', 'fadeout', 'dark', 'blackout']);
  function tier(c) {
    if (BED.has(c.effect)) {
      // the bed's brightness is set here, whatever intensity the effect itself runs at
      const own = typeof c.params.intensity === 'number' && c.params.intensity > 0 ? c.params.intensity : 1;
      c.opacity = Math.min(1, (S.bedLevel || 0.35) / own);
      c.duck = S.bedDuck || 0;
    } else if (!HIT.has(c.effect) && !FREE.has(c.effect)) {
      c.opacity = Math.min(c.opacity ?? 1, 0.85);
      c.duck = 0.6 * (S.bedDuck || 0);                // motion gives way to the drums too, a little less
    }
  }
  /** The opposite hue of each color (white and black stay as they are), as solid colors. */
  function contrastPalette(cols) {
    return Effects.solidPalette((cols || []).map(hexColor => {
      const [r, g, b] = Effects.hex2rgb(hexColor), mx = Math.max(r, g, b), mn = Math.min(r, g, b), d = mx - mn;
      if (!mx || d / mx < 0.2) return hexColor;
      let h = mx === r ? ((g - b) / d) % 6 : mx === g ? (b - r) / d + 2 : (r - g) / d + 4;
      h = ((h / 6) + 1.5) % 1;                                      // half way round the color wheel
      return '#' + Effects.hsv(h, 1, 1).map(x => Math.round(x * 255).toString(16).padStart(2, '0')).join('');
    }));
  }
  function clip(lane, effect, start, len, params = {}, extra = {}) { const c = { id: uid(), effect, start, len, params: { ...Effects.defaults(effect), ...params }, opacity: 1, blend: 'normal', fadeIn: 0, fadeOut: 0, palette: effectColors(effect) || (lane > 0 && OVERLAY.has(effect) ? S.contrastPal : S.buildPal) || null, ...extra }; tier(c); P().lanes[freeLaneFor(start, len, lane)].clips.push(c); return c; }
  function buildSection(s, idx, clearFirst, variant = 0) {
    const p = P(); const b0 = Math.round(timeToBeat(s.start)), b1 = Math.round(timeToBeat(s.end)); const len = b1 - b0; if (len < 1) return;
    if (clearFirst) { for (const lane of p.lanes) lane.clips = lane.clips.filter(c => c.start + c.len <= b0 || c.start >= b1); }
    const fam = S.songPals && S.songPals.length ? S.songPals : [p.defaultPalette];
    S.buildPal = paletteVariant(fam[idx % fam.length], idx);
    // Buildups and breakdowns run on the opposite colors, so the drop lands back on the song's
    // own palette and the change of color hits with it.
    if (s.type === 'buildup' || s.type === 'breakdown') S.buildPal = contrastPalette(S.buildPal);
    S.contrastPal = contrastPalette(S.buildPal);
    const chillStyle = showStyle() === 'chill';
    S.bedLevel = ({ drop: 0.4, buildup: 0.35 }[s.type] || 0.3) + (chillStyle ? 0.1 : 0);
    S.bedDuck = chillStyle ? 0.35 : ({ drop: 0.7, verse: 0.55, buildup: 0.5 }[s.type] || 0.4);
    const v = variant % 2;
    // Two rules shape everything here.
    //  1. Dark space: dim bases with a few bright things moving over them.
    //  2. The lights follow the song. Intros, breakdowns and outros get slow fades
    //     and drifting comets and never flash. How hard the rest goes depends on the
    //     kind of music (see genre.py): a chill song never strobes, a groove song
    //     moves on the beat but lifts its drops without flashing, dance drops flash,
    //     and rave goes all the way.
    const style = showStyle();
    const hard = style === 'dance' || style === 'rave', chill = style === 'chill';
    const rainbow = S.albumPalette ? {} : { palette: 'Rainbow' };      // with the album's colors, drops stay in them
    // The beat pulse follows the real drums (kick from the center, snare from the ends), and it is
    // only placed where there are drums to follow: a section with at least 4 kick or snare hits and
    // one every 4 beats. When the song's drums are not known yet, the quiet kinds of section get none.
    const ev = (S.analysis && S.analysis.events) || {};
    const drumsKnown = (ev.kick || []).length > 0;
    const hitsHere = drumsKnown ? [...(ev.kick || []), ...(ev.snare || [])].filter(e => e[0] >= s.start && e[0] < s.end).length : 0;
    const drumsHere = drumsKnown ? hitsHere >= Math.max(4, len / 4) : !['intro', 'breakdown', 'outro'].includes(s.type);
    const drums = (opacity, size = 0.75) => drumsHere && clip(2, 'pulse', b0, len, { follow: 'drums', decay: 0.45, size }, { opacity, blend: 'add' });
    switch (s.type) {
      case 'intro':
        clip(0, 'aurora', b0, len, { speed: 16, intensity: 0.45, depth: 0.8 });
        clip(1, 'meteors', b0, len, { every: 4, chance: 0.35, travel: 4 });
        drums(0.45, 0.55);
        clip(3, 'fadein', b0, Math.min(8, len)); break;
      case 'verse':
        clip(0, v ? 'wave' : 'gradient', b0, len, v ? { speed: chill ? 16 : 8, waves: 1.5, floor: 0 } : { speed: 16, intensity: 0.35 });
        // dual comets are drop material; a verse gets calmer movement
        clip(1, v ? 'comet' : 'orbit', b0, len, v ? { every: chill ? 4 : 2, travel: chill ? 4 : 2, tail: 0.3 } : { speed: chill ? 8 : 4, count: 2 }, { opacity: 0.85 });
        drums(chill ? 0.7 : 0.95);
        break;
      case 'buildup':
        clip(0, 'breathe', b0, len, { speed: 2, floor: 0.02 });
        clip(1, 'orbit', b0, len, { speed: 4, count: 2 }, { opacity: 0.9 });
        drums(0.85, 0.6);
        // chill songs build by filling slowly; the strobe riser is for rave only
        clip(3, 'riser', b0, len, chill ? { follow: 'beat', step: '2', accel: false } : { follow: 'drums', accel: false });
        if (style === 'rave') clip(4, 'strobeRiser', b0, len, { strobeFrom: 0.3, from: '1', to: '8' });
        // the concert move: one beat of black at the top of the buildup, and the drop lands out of the dark
        if (!chill && len >= 8 && p.sections[idx + 1] && p.sections[idx + 1].type === 'drop') clip(7, 'dark', b0 + len - 1, 1);
        break;
      case 'drop': {
        const kind = variant % 4;
        if (hard) {
          if (kind === 0) { clip(0, 'chase', b0, len, { speed: 1, size: 0.12, gap: 0.6 }); clip(1, 'flash', b0, len, { every: 1, decay: 0.3 }, { opacity: 0.6, blend: 'add' }); }
          else if (kind === 1) { clip(0, 'gradient', b0, len, { speed: 8, stretch: 0.67, intensity: 0.7 }, rainbow); clip(1, 'dualcomet', b0, len, { speed: 2, tail: 0.35 }); }
          else if (kind === 2) { clip(0, 'battle', b0, len, { speed: 8, surge: 0.2 }); clip(1, 'sparkle', b0, len, { density: 0.2 }, { opacity: 0.9 }); }
          else { clip(0, 'halves', b0, len, { every: 1, dim: 0 }); clip(1, 'comet', b0, len, { every: 1, travel: 1, tail: 0.25 }, { opacity: 0.9 }); }
        } else if (chill) {
          // the biggest moment of a chill song: a slow, full wash, nothing on the beat
          if (kind % 2 === 0) { clip(0, 'gradient', b0, len, { speed: 16, stretch: 1, intensity: 0.8 }); clip(1, 'dualcomet', b0, len, { speed: 4, tail: 0.4 }, { opacity: 0.85 }); }
          else { clip(0, 'colorwash', b0, len, { speed: 8, intensity: 0.75 }); clip(1, 'meteors', b0, len, { every: 2, chance: 0.5, travel: 3 }, { opacity: 0.9 }); }
        } else {
          // motion rather than flashing: it still lifts, without turning into a strobe
          if (kind === 0) { clip(0, 'gradient', b0, len, { speed: 8, stretch: 0.67, intensity: 0.75 }, rainbow); clip(1, 'dualcomet', b0, len, { speed: 2, tail: 0.35 }); }
          else if (kind === 1) { clip(0, 'gradient', b0, len, { speed: 8, stretch: 1, intensity: 0.8 }); clip(1, 'comet', b0, len, { every: 1, travel: 1.5, tail: 0.3 }, { opacity: 0.95 }); }
          else if (kind === 2) { clip(0, 'wave', b0, len, { speed: 2, waves: 2, floor: 0.05 }); clip(1, 'orbit', b0, len, { speed: 2, count: 3 }, { opacity: 0.9 }); }
          else { clip(0, 'battle', b0, len, { speed: 8, surge: 0.25 }); clip(1, 'dualcomet', b0, len, { speed: 1.5, tail: 0.3 }, { opacity: 0.9 }); }
        }
        drums(chill ? 0.8 : 1, 0.85);
        if (chill) clip(3, 'fadein', b0, Math.min(4, len)); else clip(3, 'dropblast', b0, 2);
        break;
      }
      case 'breakdown':
        clip(0, 'aurora', b0, len, { speed: 16, depth: 0.85, intensity: 0.35 });
        clip(1, 'meteors', b0, len, { every: 4, chance: 0.3, travel: 4 }, { opacity: 0.85 });
        drums(0.5, 0.5);
        break;
      case 'outro':
        clip(0, 'colorwash', b0, len, { speed: 8, intensity: 0.4 });
        clip(1, 'meteors', b0, len, { every: 4, chance: 0.3, travel: 4 });
        drums(0.45, 0.55);
        clip(3, 'fadeout', Math.max(b0, b1 - 16), Math.min(16, len)); break;
    }
    addSignature(s, b0, len, variant, style);
  }

  /** The flair that belongs to *this* song: its riff, its lead line, its fills.
   *
   *  Two principles. Punctuation beats blanket: a one-shot fired at the moment
   *  something actually happens reads as the lights knowing the song, while a
   *  full-length layer just adds noise. And it belongs where the music is big:
   *  drops and hyped verses get the accents, quiet sections stay quiet.
   */
  function addSignature(s, b0, len, variant, style) {
    const hard = style === 'dance' || style === 'rave';
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
      clip(4, 'hookstrike', t2b(t), Math.min(hookBeats, b0 + len - t2b(t)),
           { shape: quiet ? 'sweep' : 'burst', decay: quiet ? 0.8 : 0.4 }, { opacity: quiet ? 0.55 : 1 });
    }
    // A fill rush is a lot of movement, so it is kept for the hardest drops only
    if (style === 'rave' && s.type === 'drop') for (const [f0, f1] of fills) {
      const st = t2b(Math.max(f0, s.start)), en = clamp(Math.round(timeToBeat(Math.min(f1, s.end))), b0, b0 + len);
      if (en - st >= 2) clip(5, 'fillrush', st, en - st, { size: 0.1 }, { opacity: 0.9 });
    }
    // one-shots sprinkled on the biggest lead moments, so a long drop keeps surprising
    const hype = loud || (s.type === 'verse' && leads.length >= 12);
    if (hype && leads.length) {
      const picks = [];
      for (const e of [...leads].sort((x, y) => y[1] - x[1])) {
        const bt = t2b(e[0]);
        if (bt > b0 + 3 && bt < b0 + len - 3 && picks.every(q => Math.abs(q - bt) >= 8)) picks.push(bt);
        if (picks.length >= (s.type === 'drop' ? 5 : 3)) break;
      }
      const shots = hard ? ['sweepout', 'colorslam', 'whiteflash'] : style === 'chill' ? ['ripple', 'sweepout'] : ['sweepout', 'colorslam', 'ripple'];
      picks.sort((x, y) => x - y).forEach((bt, k) => {
        const fx = shots[(k + variant) % shots.length];
        clip(6, fx, bt, fx === 'ripple' ? 4 : 2, fx === 'ripple' ? { every: 2, travel: 2, width: 0.12 } : {},
             { opacity: fx === 'whiteflash' ? 0.75 : 0.95 });
      });
    }
    // texture: only where the music is already busy
    if (loud) {
      if (leads.length >= 3) clip(4, 'leadtrace', b0, len, { tail: 0.2 }, { opacity: 0.95 });
      if (ghosts.length >= 8) clip(5, 'ghostspark', b0, len, { count: 3 }, { opacity: 0.7 });
      if (leads.length < 3 && basses.length >= 6) clip(2, 'bassnote', b0, len, {}, { opacity: 0.8, blend: 'add' });
    } else if (leads.length >= 4) {
      // in the calm, the lead only makes the strip breathe; it never flashes
      clip(1, 'leadglow', b0, len, { gain: quiet ? 1.1 : 1.3, floor: 0 }, { opacity: quiet ? 0.4 : 0.55, blend: 'add' });
    }
  }

  // ------------------------------------------------------------------ keyboard & controls
  document.addEventListener('keydown', e => {
    // e.target can be the document itself (or any non-element node), which has no matches()
    if (e.target && typeof e.target.matches === 'function' && e.target.matches('input, select, textarea')) {
      if (e.key === 'Escape') e.target.blur();
      return;
    }
    const be = $('#beatEdit').checked;
    if (e.code === 'Space') { e.preventDefault(); if (be && S.audio.playing) { beginChange(); const t = pos(); const p = P(); const i = p.beats.findIndex(b => b > t); if (i < 0) p.beats.push(t); else p.beats.splice(i, 0, t); changed(); } else togglePlay(); }
    else if (e.key === 'Enter') { togglePlay(); }
    else if (e.key === 'Home') { seek(0); scrollTo(0); }
    else if (e.key === 'End') { seek(duration()); }
    else if (e.key === 'Delete' || e.key === 'Backspace') { deleteSelection(); }
    else if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === 'z') { e.preventDefault(); e.shiftKey ? redo() : undo(); }
    else if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === 'y') { e.preventDefault(); redo(); }
    else if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === 's') { e.preventDefault(); save(); toast('Saved'); }
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
  $('#btnUndo').onclick = undo; $('#btnRedo').onclick = redo; $('#btnSave').onclick = () => { save(); toast('Saved'); };
  $('#btnRedetect').onclick = async () => {
    if (!S.analysis) return;
    // ask the server again rather than reusing what was loaded: if SongFormer could not run
    // before, this is where it gets another go
    if (S.name) {
      busy('Analysing…');
      try { const d = await (await fetch(`/api/projects/${encodeURIComponent(S.name)}`)).json(); if (d.analysis) S.analysis = d.analysis; }
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
  $('#recent').onchange = e => { if (e.target.value) openProject(e.target.value); };
  $('#btnRescan').onclick = () => loadLights(true);
  $('#btnTap').onclick = tapTempo;
  $('#btnHalf').onclick = () => scaleTempo(0.5); $('#btnDouble').onclick = () => scaleTempo(2); $('#btnRegrid').onclick = () => { if (P()) regrid(); }; $('#btnNudgeL').onclick = () => P() && nudge(-10); $('#btnNudgeR').onclick = () => P() && nudge(10);
  $('#btnPhase').onclick = () => { if (!P()) return; beginChange(); P().phase = (P().phase + 1) % 4; changed(); };
  $('#btnPhaseL').onclick = () => { if (!P()) return; beginChange(); P().phase = (P().phase + 3) % 4; changed(); };
  $('#bpm').onchange = () => { if (P()) { beginChange(); P().bpm = parseFloat($('#bpm').value) || P().bpm; changed(); toast('BPM stored — press Regrid to rebuild the beat grid'); } };
  $('#beatEdit').onchange = draw; $('#snap').onchange = draw; $('#effectSearch').oninput = renderLibrary;
  $('#outputOn').onchange = e => { if (e.target.checked) { if (!selectedLights().length) { toast('Tick a light first', true); e.target.checked = false; return; } rememberLights(true); if (S.audio.playing) startOutput(); else pushFrameIfIdle(); } else { rememberLights(false); stopOutput(); } };
  $('#latency').onchange = scheduleSave; $('#master').oninput = () => { scheduleSave(); pushFrameIfIdle(); };
  $('#projectName').onchange = scheduleSave;
  $('#btnNewPalette').onclick = () => { if (!P()) return; const name = prompt('Palette name'); if (!name) return; beginChange(); P().palettes[name] = ['#ff0000', '#0000ff']; renderPalettes(); changed(); };
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
  fetch('/api/settings').then(r => r.json()).then(s => { S.defaults.castOffset = s.cast_offset_ms || 0; S.defaults.castOffsets = s.cast_offsets || {}; if (!P()) $('#castOffset').value = S.defaults.castOffset; }).catch(() => { });
  renderLibrary(); renderPalettes(); inspector(); connectWs(); loadLights(); refreshRecent(); loadCastDevices(); loop();
  window.Studio = { S, P, $, addClip, autoBuild, showStyle, buildSection, newProject, migrateProject, openPath, openProject, renderFrame, save, _draw, compose,
    draw, inspector, zoomFit, renderPalettes, renderLights, sendSelect, toast, timeToBeat, beatToTime, startOutput, stopOutput, pos, updateTransport, refreshRecent, loudnessGain, contrastPalette, tidySections };
})();
