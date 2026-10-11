/* Jam mode in the studio.
 *
 * Spotify plays on this PC, the speakers play it back late, and the server works
 * out what the room is hearing and how each track should be shown. This file
 * follows along: it swaps the timeline to whatever track the room is on, builds
 * a show for it (or loads the one you made), keeps the playhead where the room
 * is, and rebuilds the part of a first listen that has not been reached yet
 * each time the analysis learns more.
 *
 * A jam never saves: tracks it builds have no project name, and app.js refuses
 * to save while a jam is live, so a saved show cannot be overwritten from here.
 */
(() => {
  const St = window.Studio; if (!St) return;
  const { S, $, toast } = St;
  const J = S.jam = { live: false, open: false, state: null, key: null, version: -1, anchor: null, built: [], timer: null, fetching: false,
    pos() { const a = this.anchor; if (!a) return 0; return a.playing ? a.position + (performance.now() - a.at) / 1000 : a.position; } };

  const api = async (path, body) => {
    const r = await fetch('/api/' + path, body === undefined ? {} : { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) });
    const j = await r.json().catch(() => ({}));
    if (j && j.error) throw new Error(j.error);
    return j;
  };
  const esc = s => String(s ?? '').replace(/[&<>"]/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]));
  const KIND = { custom: ['your show', 'k-custom'], learned: ['learned', 'k-learned'], live: ['first listen', 'k-live'] };
  const badge = k => `<span class="jam-kind ${(KIND[k] || KIND.live)[1]}">${(KIND[k] || KIND.live)[0]}</span>`;
  const styleTag = t => t && t.style ? ` <span class="jam-style">${esc(t.style)}</span>` : '';

  // ------------------------------------------------------------------ colors
  // Every jam song is shown in two colors: the ones picked for that track (kept for next time), else
  // the jam's own pair, else (jam set to "album cover") its cover's. A show you made keeps its own.
  function colorsFor(track) {
    const st = J.state || {}, own = track && (st.track_colors || {})[track.id], jc = st.jam_colors || {};
    if (own) return [own.primary, own.secondary];
    if (jc.mode === 'pick') return [jc.primary, jc.secondary];
    if (jc.mode === 'mix') return (st.assigned_colors || {})[track.id] || (jc.pairs || [])[0] || null;
    return null;
  }
  /** The extras (a few small accents in two more colors), with the jam's two colors only. */
  function extrasFor() {
    const jc = (J.state || {}).jam_colors || {};
    return jc.extras ? [jc.accent1, jc.accent2] : null;
  }
  /** rainbow drops for the songs that go all out, in "a different pair each song" */
  const rainbowOn = () => { const jc = (J.state || {}).jam_colors || {}; return jc.mode === 'mix' && !!jc.rainbow; };
  function shownColors(t) {
    const cols = colorsFor(t) || ((J.art || {})[t.image] || []).slice(0, 2);
    if (!colorsFor(t) && t.image && J.art && J.art[t.image] === undefined) fetchArt(t);
    return [cols[0] || '#808080', cols[1] || cols[0] || '#808080'];
  }
  function fetchArt(t) {
    J.artPending = J.artPending || new Set(); if (J.artPending.has(t.image)) return;
    J.artPending.add(t.image);
    albumPalette(t).finally(() => { J.artPending.delete(t.image); J.shown = null; render(); });
  }
  function pairPicker(t) {
    if (!t || !t.id || t.kind === 'custom') return '';
    const [a, b] = shownColors(t), own = (J.state.track_colors || {})[t.id];
    return `<span class="jam-pair" data-id="${esc(t.id)}"><input type="color" class="jp-a" value="${a}" title="Primary color for this song (kept for next time)"><input type="color" class="jp-b" value="${b}" title="Secondary color for this song">${own ? '<button class="jp-reset small" title="Back to the jam\'s colors">&#8634;</button>' : ''}</span>`;
  }
  function colorsSection(st) {
    const jc = st.jam_colors || {};
    return `<div class="jam-sec"><h4>Colors</h4>
        <div class="jam-row"><label>Every song in</label><span class="jam-pair"><select id="jamColorMode"><option value="pick" ${jc.mode === 'pick' ? 'selected' : ''}>These two colors</option><option value="mix" ${jc.mode === 'mix' ? 'selected' : ''}>A different pair each song</option><option value="album" ${jc.mode !== 'pick' && jc.mode !== 'mix' ? 'selected' : ''}>Its album cover's colors</option></select>${jc.mode === 'pick' ? `<input type="color" id="jamPrimary" value="${esc(jc.primary || '#8000ff')}" title="Primary"><input type="color" id="jamSecondary" value="${esc(jc.secondary || '#00ff00')}" title="Secondary">` : ''}</span></div>
        ${jc.mode === 'mix' ? `<div class="jam-row"><label title="Each song gets one of these, chosen by its vibe, never the same pair twice in a row">Pairs</label><span class="jam-pairs">${(jc.pairs || []).map((p, i) => `<span class="jam-pair"><input type="color" class="mx-a" data-i="${i}" value="${esc(p[0])}"><input type="color" class="mx-b" data-i="${i}" value="${esc(p[1])}"></span>`).join('')}</span></div>
        <div class="jam-row"><label title="A song that goes all out gets rainbow drops">Rainbow for epic songs</label><input type="checkbox" id="jamRainbow" ${jc.rainbow ? 'checked' : ''}></div>` : ''}
        ${jc.mode === 'pick' || jc.mode === 'mix' ? `<div class="jam-row"><label title="A few of the small accents (a strike, a spark, a sweep) take these instead, now and then">Now and then</label><span class="jam-pair"><input type="checkbox" id="jamExtras" ${jc.extras ? 'checked' : ''}><input type="color" id="jamAccent1" value="${esc(jc.accent1 || '#ff6000')}" title="Extra color"><input type="color" id="jamAccent2" value="${esc(jc.accent2 || '#ff0000')}" title="Extra color"></span></div>` : ''}</div>`;
  }
  /** The song in the room got new colors: build its show again in them. */
  function recolor() {
    if (!J.live || !J.doc || J.doc.kind === 'custom') return;
    S.jamColors = colorsFor(J.doc.track); S.jamExtras = S.jamColors ? extrasFor() : null; S.jamRainbow = rainbowOn();
    try { St.autoBuild(true); J.built = sectionKeys(); } catch (e) { console.error(e); }
    St.draw();
  }
  const roomTrackId = () => J.doc && J.doc.track && J.doc.track.id;

  // ------------------------------------------------------------------ the panel
  function render() {
    const el = $('#jamBody'); const st = J.state || {}; const sp = st.spotify || {};
    const running = !!st.running;
    let html = '';
    if (!running) {
      html += `<div class="jam-sec"><h4>1 · Spotify</h4>
        <div class="jam-row"><label>Client ID</label><input id="jamClient" value="${esc(sp.client_id || '')}" placeholder="from your app at developer.spotify.com" spellcheck="false"></div>
        <div class="hint">In your Spotify app's settings, add this Redirect URI exactly: <code>${esc(st.redirect_uri || '')}</code></div>
        <div class="jam-row"><span>${sp.signed_in ? '<b class="ok">Signed in</b>' : 'Not signed in'}</span>
          <span><button id="jamSignIn" class="small">${sp.signed_in ? 'Sign in again' : 'Sign in to Spotify'}</button>${sp.signed_in ? ' <button id="jamSignOut" class="small">Sign out</button>' : ''}</span></div></div>
      <div class="jam-sec"><h4>2 · Where Spotify plays</h4>
        <div class="jam-row"><label>Spotify plays in</label><select id="jamSource">
          <option value="studio">The studio itself</option>
          <option value="app">The Spotify app on this PC</option></select></div>
        <div id="jamPlayerBox">
          <div class="jam-row"><span>${playerLine(st)}</span><span>${st.player && st.player.ready ? '<button id="jamPlayerHere" class="small">Play here</button>' : ''}</span></div>
          <div class="hint">While the studio is open it is a Spotify speaker called <b>Light Show Studio</b>: pick it from the device list on your phone, or host the Jam with it. It plays in a hidden Microsoft Edge whose sound goes to <b>${esc(((st.player || {}).hidden_output) || 'S/PDIF')}</b>, where nobody hears it; the room hears the studio's delayed copy on your default output.</div>
          <label class="jam-check"><input id="jamAuto" type="checkbox" ${st.auto ? 'checked' : ''}> Start the jam by itself when music starts playing on Light Show Studio, with these settings</label>
          ${st.auto_error ? `<div class="warn">The jam could not start by itself: ${esc(st.auto_error)}</div>` : ''}
          ${sp.signed_in && !sp.can_stream ? '<div class="warn">Sign in to Spotify again: the studio player needs a permission the earlier sign-in did not ask for.</div>' : ''}
        </div>
        <div class="hint" id="jamAppHint">In Windows' Volume mixer, set the Spotify app's output to <b>${esc(((st.player || {}).hidden_output) || 'S/PDIF')}</b>. The house hears it through the studio, late.</div></div>
      <div class="jam-sec"><h4>3 · Where the house hears it</h4>
        <div class="jam-row"><label>Play on</label><select id="jamWhere">
          <option value="cast" ${st.speaker_connected ? '' : 'disabled'}>Cast: ${st.speaker_connected ? esc(st.speaker) : 'press Cast first'}</option>
          <option value="pc">This PC</option>
          <option value="none">Nowhere (preview only)</option></select></div>
        <div class="hint" id="jamPcHint">This PC plays on Windows' default output.</div>
        <div class="jam-row"><label>Stay behind by</label><span><input id="jamDelay" type="number" min="10" max="180" step="5" value="90" class="num"> s</span></div>
        <div class="jam-row"><label title="Spotify: Settings, Playback, Crossfade. The studio cannot read it, and needs it to tell a skip from a song that simply ended.">Spotify crossfade</label><span><input id="jamXfade" type="number" min="0" max="12" step="1" value="0" class="num"> s</span></div>
        <div class="jam-row"><label>On a skip</label><select id="jamOnSkip">
          <option value="keep">Keep the delay</option>
          <option value="jump">Jump right away</option></select></div>
        <div class="hint">The lights study this much music before the room hears it. Keeping the delay, the room hears a skip that much later, and the lights see the new song coming. Jumping, the room hears it at once, but the new song starts with almost no lead, and the delay only grows back a few seconds between later songs. Play and pause follow the phone.</div></div>
      ${colorsSection(st)}
      <div class="jam-actions">
        <button id="jamStart" class="accent" ${sp.signed_in ? '' : 'disabled'}>Start jam</button>
        <button id="jamOpener" ${sp.signed_in && st.speaker_connected ? '' : 'disabled'} title="Play one of your shows from disk first. Spotify is released as it ends, so the jam begins with the buffer already full.">Start behind this song</button>
        <button id="jamDemo" title="Synthetic songs and a pretend Spotify. No sound, no speakers, no sign-in.">Silent demo</button></div>`;
    } else {
      const room = st.room, ahead = st.ahead; const pct = Math.max(0, Math.min(100, 100 * (st.delay || 0) / (st.target_delay || 90)));
      html += `<div class="jam-sec"><h4>${esc(st.mode || 'jam')}</h4>
        <div class="jam-now"><div class="lbl">In the room</div><div class="ttl">${room ? esc(room.label) : (st.streaming ? 'between songs' : `filling the buffer… ${Math.round(st.buffered || 0)} s of ${Math.round(st.target_delay)} s`)}</div>${room ? badge(room.kind) + styleTag(room) + (room.project ? ` <span class="hint">${esc(room.project)}</span>` : '') + pairPicker(room) : ''}</div>
        <div class="jam-now"><div class="lbl">On Spotify now, ${Math.round(st.delay || 0)} s ahead of the room</div><div class="ttl">${ahead ? esc(ahead.label) : '—'}</div>${ahead ? badge(ahead.kind) + styleTag(ahead) + (ahead.kind === 'live' ? ` <span class="hint">analysed to ${fmt(ahead.horizon)}</span>` : '') : ''}</div>
        <div class="jam-meter" title="How far the room is behind Spotify"><div style="width:${pct}%"></div><span>${(st.delay || 0).toFixed(1)} s behind · aiming for ${Math.round(st.target_delay)} s</span></div>
        ${st.paused ? '<div class="hint">Paused on the phone. The buffer is held.</div>' : ''}
        ${st.handover ? `<div class="hint">${st.handover === 'armed' ? 'Spotify is held until your opening song is nearly over.' : 'Filling the buffer behind the opening song…'}</div>` : ''}
        ${st.capture && st.capture.error ? `<div class="warn">Capture: ${esc(st.capture.error)}</div>` : ''}
        ${st.output_error ? `<div class="warn">Playback: ${esc(st.output_error)}</div>` : ''}
        ${sp.error ? `<div class="warn">Spotify: ${esc(sp.error)}</div>` : ''}</div>
      ${colorsSection(st)}
      <div class="jam-sec"><h4>Up next</h4>${(st.queue || []).slice(0, 8).map(q => `<div class="jam-q"><span>${esc(q.label)}</span>${pairPicker(q)}${badge(q.kind)}</div>`).join('') || '<div class="hint">Nothing queued.</div>'}</div>
      <div class="jam-sec"><h4>Log</h4><div class="jam-log">${(st.log || []).slice().reverse().map(l => `<div>${esc(l)}</div>`).join('')}</div></div>
      <div class="jam-actions">
        <button id="jamStop">Stop jam</button>
        ${st.streaming && st.output_kind === 'cast' ? '<button id="jamSync" title="Listens to the room and measures how late the speakers really are">Sync check</button>' : ''}
        ${(st.mode || '').startsWith('demo') ? '<button id="jamSkip">Skip (demo)</button><button id="jamPause">' + (st.paused ? 'Resume' : 'Pause') + ' (demo)</button>' : ''}
        ${room && room.kind !== 'custom' && S.name ? '' : ''}</div>`;
    }
    // keep what is being typed
    const keep = {}; for (const id of ['jamClient', 'jamDelay', 'jamXfade', 'jamOnSkip', 'jamDevice', 'jamWhere', 'jamPcDevice', 'jamSource']) { const e = $('#' + id); if (e) keep[id] = e.value; }
    // Rebuild only when something shown changed, and never under a control in use: rebuilding
    // replaced an open dropdown with a new, closed one, so it shut half a second after opening.
    const active = document.activeElement;
    const busy = active && el.contains(active) && /^(INPUT|SELECT|TEXTAREA)$/.test(active.tagName) && (!running || active.type === 'color' || active.tagName === 'SELECT');
    const shown = html;
    if (shown !== J.shown && !busy) { el.innerHTML = html; J.shown = shown; wire(keep); }
    $('#btnJam').classList.toggle('on', running);
    $('#btnJam').textContent = running ? (st.streaming ? 'Jam · live' : 'Jam · filling') : 'Jam';
  }
  const fmt = t => `${Math.floor(t / 60)}:${String(Math.floor(t % 60)).padStart(2, '0')}`;
  function playerLine(st) {
    const p = st.player || {};
    if (p.route_error) return `<span class="warn">Player: ${esc(p.route_error)}</span>`;
    if (p.error) return `<span class="warn">Player: ${esc(p.error)}</span>`;
    if (p.ready && !p.routed) return '<span class="warn">Light Show Studio is in Spotify, but its sound is not on the hidden output yet</span>';
    if (p.ready) return `<b class="ok">Light Show Studio is in Spotify</b>${p.track ? ` · ${esc(p.track)}` : ''}`;
    if (p.running) return 'Starting the hidden player…';
    if (!((J.state || {}).spotify || {}).can_stream) return 'Sign in to Spotify to use the studio as a speaker';
    return 'Connecting to Spotify…';
  }

  function wire(keep) {
    const on = (id, fn) => { const e = $('#' + id); if (e) e.onclick = () => fn().catch(err => toast('Jam: ' + err.message, true)); };
    // the form starts from the settings last used, then keeps whatever is being changed
    const last = (J.state && J.state.last) || {};
    const fromLast = { jamDelay: last.target_delay, jamXfade: last.crossfade, jamOnSkip: last.skip_jumps === undefined ? undefined : (last.skip_jumps ? 'jump' : 'keep'),
      jamSource: last.source, jamWhere: last.speakers, jamDevice: last.device, jamPcDevice: last.pc_device };
    for (const [id, v] of Object.entries(fromLast)) if (keep[id] === undefined && v !== undefined && v !== null) keep[id] = String(v);
    for (const [id, v] of Object.entries(keep)) { const e = $('#' + id); if (e && v !== undefined && id !== 'jamDevice' && id !== 'jamPcDevice') e.value = v; }
    const where = $('#jamWhere'), pcDev = $('#jamPcDevice');
    if (where && !keep.jamWhere) where.value = J.state.speaker_connected ? 'cast' : 'pc';
    if (pcDev) { pcDev.innerHTML = '<option value="">Default output</option>' + (J.outputs || []).filter(o => o.name).map(o => `<option value="${esc(o.name)}">${esc(o.name)}${o.default ? ' (default)' : ''}</option>`).join(''); if (keep.jamPcDevice) pcDev.value = keep.jamPcDevice; }
    const showPc = () => { const pc = where && where.value === 'pc'; for (const id of ['jamPcRow', 'jamPcHint']) { const e = $('#' + id); if (e) e.hidden = !pc; } };
    if (where) { where.onchange = showPc; showPc(); }
    const source = $('#jamSource');
    const showSource = () => { const studio = !source || source.value === 'studio'; const box = $('#jamPlayerBox'), hint = $('#jamAppHint'); if (box) box.hidden = !studio; if (hint) hint.hidden = studio; };
    if (source) { source.onchange = () => { showSource(); api('jam/prefs', { source: source.value }).then(st => { J.state = st; }).catch(() => {}); }; showSource(); }
    const auto = $('#jamAuto'); if (auto) auto.onchange = () => api('jam/prefs', { auto: auto.checked }).then(st => { J.state = st; }).catch(() => {});
    // colors: the jam's pair, and any track's own (saved for the next time it plays)
    const saveJam = async body => {
      J.state = await api('jam/colors', body);
      recolor();
      if (document.activeElement && el2.contains(document.activeElement)) document.activeElement.blur();
      J.shown = null; render();
    };
    const el2 = $('#jamBody');
    const mode = $('#jamColorMode');
    if (mode) mode.onchange = () => saveJam({ mode: mode.value }).catch(e => toast(e.message, true));
    for (const id of ['jamPrimary', 'jamSecondary']) { const e = $('#' + id); if (e) e.onchange = () => saveJam({ primary: $('#jamPrimary').value, secondary: $('#jamSecondary').value }).catch(err => toast(err.message, true)); }
    for (const e of el2.querySelectorAll('.mx-a, .mx-b')) e.onchange = () => {
      const pairs = [...el2.querySelectorAll('.mx-a')].map(a => [a.value, el2.querySelector(`.mx-b[data-i="${a.dataset.i}"]`).value]);
      saveJam({ pairs }).catch(err => toast(err.message, true));
    };
    const rb = $('#jamRainbow'); if (rb) rb.onchange = () => saveJam({ rainbow: rb.checked }).catch(err => toast(err.message, true));
    for (const id of ['jamExtras', 'jamAccent1', 'jamAccent2']) { const e = $('#' + id); if (e) e.onchange = () => saveJam({ extras: $('#jamExtras').checked, accent1: $('#jamAccent1').value, accent2: $('#jamAccent2').value }).catch(err => toast(err.message, true)); }
    for (const box of el2.querySelectorAll('.jam-pair[data-id]')) {
      const id = box.dataset.id, a = box.querySelector('.jp-a'), b = box.querySelector('.jp-b'), reset = box.querySelector('.jp-reset');
      const save = async body => {
        J.state = await api('jam/colors', { track_id: id, ...body });
        if (id === roomTrackId()) recolor();
        if (document.activeElement) document.activeElement.blur();
        J.shown = null; render();
      };
      a.onchange = b.onchange = () => save({ primary: a.value, secondary: b.value }).catch(e => toast(e.message, true));
      if (reset) reset.onclick = () => save({}).catch(e => toast(e.message, true));
    }
    on('jamPlayerHere', async () => { await api('jam/player/transfer', {}); toast('Spotify is now playing on Light Show Studio'); });
    const dev = $('#jamDevice');
    if (dev) { dev.innerHTML = '<option value="">Default output</option>' + (J.outputs || []).filter(o => o.name).map(o => `<option value="${esc(o.name)}">${esc(o.name)}${o.default ? ' (default)' : ''}</option>`).join(''); if (keep.jamDevice) dev.value = keep.jamDevice; }
    const cid = $('#jamClient'); if (cid) cid.onchange = () => api('spotify/client', { client_id: cid.value.trim() }).then(s => { J.state = { ...J.state, ...s }; render(); }).catch(e => toast(e.message, true));
    on('jamSignIn', async () => { if (cid && cid.value.trim() !== ((J.state.spotify || {}).client_id || '')) await api('spotify/client', { client_id: cid.value.trim() }); window.open('/api/spotify/login', 'spotify', 'width=520,height=760'); });
    on('jamSignOut', async () => { J.state = await api('spotify/signout', {}); render(); });
    const start = extra => api('jam/start', { device: dev ? dev.value : '', target_delay: parseFloat(($('#jamDelay') || {}).value) || 90, crossfade: parseFloat(($('#jamXfade') || {}).value) || 0, skip_jumps: (($('#jamOnSkip') || {}).value) === 'jump', source: (source && source.value) || 'studio', speakers: (where && where.value) || 'none', pc_device: (pcDev && pcDev.value) || '', ...extra });
    on('jamStart', async () => { J.state = await start({}); render(); toast({ cast: 'Jam started. The speakers join once the buffer is full.', pc: 'Jam started. This PC starts playing once the buffer is full.' }[J.state.output_kind] || 'Jam started as a preview: nothing plays.'); });
    on('jamOpener', async () => { if (!S.cast.active || !S.audio.playing) return toast('Cast one of your shows and press play first, then press this', true); J.state = await start({ opener: true, speakers: 'cast' }); render(); toast('Armed. Spotify will be released as this song ends.'); });
    on('jamDemo', async () => { J.state = await start({ demo: true, speakers: 'none', target_delay: 20 }); render(); toast('Silent demo running: synthetic songs, no sound.'); });
    on('jamStop', async () => { J.state = await api('jam/stop', {}); leave(); render(); });
    on('jamSkip', () => api('jam/demo/skip', {}));
    on('jamPause', () => api('jam/demo/' + (J.state.paused ? 'resume' : 'pause'), {}));
    on('jamSync', async () => { toast('Listening to the room…'); const r = await api('jam/synccheck', {}); toast(r.confidence >= 0.5 ? `Speakers corrected by ${r.offset_ms} ms (now ${r.speaker_offset_ms} ms)` : (r.reason || 'Could not get a clear reading; try again in a moment'), r.confidence < 0.5); });
  }

  // ------------------------------------------------------------------ following the room
  async function tick() {
    if (J.fetching) return; J.fetching = true;
    try {
      const st = await api('jam/state'); const was = J.state && J.state.running;
      J.state = st;
      if (st.running && !was && st.auto_started) toast('Music started on Light Show Studio: the jam started by itself');
      if (st.running && st.streaming) await follow(st); else if (J.live && !(st.running)) leave();
      if (J.open || st.running !== was) render();
    } catch (e) { /* server away */ } finally { J.fetching = false; }
  }

  function anchorTo(room) {
    // keep a smooth local clock; only re-seat it when the server disagrees by more than a frame or two
    const mine = J.pos();
    if (!J.anchor || J.anchor.key !== room.key || J.anchor.playing !== room.playing || Math.abs(mine - room.position) > 0.08)
      J.anchor = { key: room.key, position: room.position, playing: room.playing, at: performance.now() };
  }

  async function follow(st) {
    const room = st.room;
    if (!room) { if (J.anchor) J.anchor.playing = false; S.audio.playing = false; return; }
    // A show that fails to build or update must not stop the clock: the lights would sit at 0:00.
    try {
      if (room.key !== J.key) { await load(room); if (room.key !== J.key) return; }
      else if (room.kind === 'live' && room.version !== J.version) await refresh(room);
      else if (room.kind === 'live' && performance.now() - (J.lastFeat || 0) > 5000) await refresh(room, true);
    } catch (e) { buildFailed(room, e); }
    anchorTo(room);
    S.audio.playing = !!room.playing; St.updateTransport();
    if ($('#outputOn').checked) St.startOutput();
  }

  function buildFailed(room, e) {
    console.error('Jam show build failed', e);
    if (J.failedKey !== room.key) toast(`${room.label}: the show could not be built (${e.message}). The lights keep time with what there is.`, true);
    J.failedKey = room.key;
  }

  function adopt(analysis, name, saved) {
    S.analysis = analysis; S.name = saved ? name : '';
    S.project = saved || St.newProject(analysis, name);
    St.migrateProject(S.project);
    $('#projectName').value = name; $('#bpm').value = S.project.bpm.toFixed(2);
    $('#emptyState').hidden = true;
    S.undo.length = 0; S.redo.length = 0; S.view.sel = null; S.view.multi = [];
  }

  /** The album cover's dominant bright color, and a second from the cover (or a neighbor of the
   *  first), as a palette. Colors are pushed to full brightness: the lights want solid color. */
  async function albumPalette(track) {
    const url = track && track.image; if (!url) return null;
    J.art = J.art || {}; if (J.art[url] !== undefined) return J.art[url];
    let pal = null;
    try {
      // decoded from the bytes (an <img> waits while the window is hidden), and never for long:
      // a song's show does not wait on its cover
      const blob = await Promise.race([fetch('/api/jam/art?url=' + encodeURIComponent(url)).then(r => r.ok ? r.blob() : null),
                                       new Promise(r => setTimeout(() => r(null), 3000))]);
      if (!blob) throw new Error('no cover');
      const img = await createImageBitmap(blob);
      const cv = document.createElement('canvas'); cv.width = cv.height = 48;
      const g = cv.getContext('2d', { willReadFrequently: true }); g.drawImage(img, 0, 0, 48, 48);
      const px = g.getImageData(0, 0, 48, 48).data, bins = new Array(24).fill(0), sx = new Array(24).fill(0), sy = new Array(24).fill(0);
      for (let i = 0; i < px.length; i += 4) {
        const r = px[i] / 255, gg = px[i + 1] / 255, b = px[i + 2] / 255, mx = Math.max(r, gg, b), mn = Math.min(r, gg, b), d = mx - mn;
        const s = mx ? d / mx : 0; if (s < 0.3 || mx < 0.3) continue;              // greys and shadows do not count
        let h = d === 0 ? 0 : mx === r ? ((gg - b) / d) % 6 : mx === gg ? (b - r) / d + 2 : (r - gg) / d + 4; h = ((h * 60) + 360) % 360;
        const w = s * s * mx, k = Math.floor(h / 15) % 24; bins[k] += w; sx[k] += w * Math.cos(h * Math.PI / 180); sy[k] += w * Math.sin(h * Math.PI / 180);
      }
      const order = bins.map((w, k) => [w, k]).sort((a, b) => b[0] - a[0]);
      if (order[0][0] > 0) {
        const hueOf = k => (Math.atan2(sy[k], sx[k]) * 180 / Math.PI + 360) % 360;
        const h1 = hueOf(order[0][1]);
        const other = order.find(([w, k]) => w >= 0.25 * order[0][0] && Math.min(Math.abs(k - order[0][1]), 24 - Math.abs(k - order[0][1])) >= 3);
        const h2 = other ? hueOf(other[1]) : (h1 + 35) % 360;
        const hex = (h, s, v) => { const f = n => { const k = (n + h / 60) % 6; return Math.round(255 * (v - v * s * Math.max(0, Math.min(k, 4 - k, 1)))); };
          return '#' + [f(5), f(3), f(1)].map(x => x.toString(16).padStart(2, '0')).join(''); };
        pal = [hex(h1, 1, 1), hex(h2, 1, 1), hex((h1 + 330) % 360, 1, 1)];               // solid colors only
      }
    } catch (e) { pal = null; }
    J.art[url] = pal;
    return pal;
  }

  J.albumPalette = albumPalette;                         // for checking a cover's colors by hand

  async function load(room) {
    const doc = await api('jam/analysis?key=' + encodeURIComponent(room.key));
    J.live = true; J.doc = doc;
    S.albumPalette = await albumPalette(doc.track);
    S.jamColors = doc.kind === 'custom' ? null : colorsFor(doc.track);
    S.jamExtras = S.jamColors ? extrasFor() : null; S.jamRainbow = rainbowOn();
    if (doc.kind === 'custom' && doc.project) {
      const data = await (await fetch('/api/projects/' + encodeURIComponent(doc.project))).json();
      if (data.error) return toast('Jam: ' + data.error, true);
      adopt(data.analysis, data.project, data.saved || null);
      if (!data.saved) try { St.autoBuild(true); } catch (e) { buildFailed(room, e); }
      if (J.failedKey !== room.key) toast(`${room.label}: playing your show`);
    } else {
      adopt(doc.analysis, room.label, null);
      try { St.autoBuild(true); } catch (e) { buildFailed(room, e); }
      J.built = sectionKeys();
      if (J.failedKey !== room.key) toast(doc.kind === 'learned' ? `${room.label}: heard before, full show` : `${room.label}: first listen, building as it plays`);
    }
    J.key = room.key; J.version = doc.version; J.lastFeat = performance.now(); J.style = St.showStyle();
    J.drums = !!(S.analysis && S.analysis.events && S.analysis.events.kick && S.analysis.events.kick.length);
    St.zoomFit(); St.inspector(); St.renderPalettes(); St.draw();
  }

  const sectionKeys = () => St.P().sections.map(s => `${s.type}@${s.start.toFixed(2)}-${s.end.toFixed(2)}`);

  /** A first listen has learned more: take the new grid and rebuild only what the room has not reached. */
  async function refresh(room, featuresOnly = false) {
    const doc = await api('jam/analysis?key=' + encodeURIComponent(room.key));
    if (!doc.analysis || room.key !== J.key) return;
    const a = doc.analysis, p = St.P();
    S.analysis = a; J.lastFeat = performance.now();
    if (featuresOnly && doc.version === J.version) { St.draw(); return; }
    p.bpm = a.bpm; p.phase = a.downbeat_phase; p.beats = a.beats.slice(); p.drops = a.drops.slice();
    p.sections = St.tidySections(a.sections);
    $('#bpm').value = p.bpm.toFixed(2);
    // the genre arrived, or the drums did (the beat pulse goes only where there are drums): rebuild
    const drumsNow = !!(a.events && a.events.kick && a.events.kick.length);
    const style = St.showStyle(), restyled = style !== J.style || drumsNow !== !!J.drums;
    if (style !== J.style) toast(`${(doc.track || {}).name || 'This track'}: ${style} style from here on`);
    const keys = reconcile(J.built, Math.ceil(St.timeToBeat(J.pos()) + 0.1), restyled);
    J.built = keys; J.version = doc.version; J.style = style; J.drums = drumsNow;
    St.draw();
  }

  /** Bring the timeline in line with the latest sections, given the keys it was built for.
   *
   *  A section whose kind or start changed is rebuilt wherever it is: ahead of the room, behind it
   *  (so the timeline tells the truth), and the one playing now from the next beat on. A section
   *  that only grew keeps its clips: a little growth (under a bar) stretches them, more gets the
   *  new stretch built, so its effects do not restart on every pass. Last, a safety net: from the
   *  playhead on, any stretch of a section with no base layer is built, so no change in the
   *  sections can leave the lights dark (a drop that grew from 2 s to a minute once did).
   *  Returns the new keys. */
  function reconcile(built, nowBeat, restyled) {
    const p = St.P(), keys = sectionKeys(), counts = {};
    const head = k => (k || '').split('-')[0];                        // "type@start", without the end
    const endOf = k => { const m = /-(\d+(?:\.\d+)?)$/.exec(k || ''); return m ? parseFloat(m[1]) : null; };
    const build = (s, i, n, b0, b1) => {                               // [b0, b1) of section s, afresh
      if (b1 - b0 < 1) return;
      makeRoom(b0, b1);
      St.buildSection({ ...s, start: St.beatToTime(b0), end: St.beatToTime(b1) }, i, false, n - 1);
    };
    p.sections.forEach((s, i) => {
      const n = counts[s.type] = (counts[s.type] || 0) + 1;
      const startBeat = Math.round(St.timeToBeat(s.start)), endBeat = Math.round(St.timeToBeat(s.end));
      if (keys[i] === (built || [])[i] && !restyled) return;
      if (head(keys[i]) === head((built || [])[i]) && !restyled) {
        const oldEnd = endOf(built[i]), oldEndBeat = oldEnd === null ? endBeat : Math.round(St.timeToBeat(oldEnd));
        if (endBeat - oldEndBeat >= 4) build(s, i, n, Math.max(oldEndBeat, nowBeat), endBeat);    // grew a lot: build the new part
        else for (const lane of p.lanes) for (const c of lane.clips)                                  // a little: stretch what ran to the old end
          if (c.start >= startBeat - 0.5 && c.start < endBeat && c.len > 8 && c.start + c.len < endBeat && !lane.clips.some(k => k !== c && k.start >= c.start + c.len && k.start < endBeat)) c.len = endBeat - c.start;
        return;
      }
      build(s, i, n, startBeat < nowBeat && endBeat > nowBeat ? nowBeat : startBeat, endBeat);
    });
    joinClips(nowBeat);
    // the safety net: no part of what is still to come goes without a base layer
    const counts2 = {};
    p.sections.forEach((s, i) => {
      const n = counts2[s.type] = (counts2[s.type] || 0) + 1;
      const b0 = Math.max(Math.round(St.timeToBeat(s.start)), nowBeat), b1 = Math.round(St.timeToBeat(s.end));
      const base = (p.lanes[0] && p.lanes[0].clips) || [];
      let run = null;
      for (let b = b0; b <= b1; b++) {
        const covered = b < b1 && base.some(c => b >= c.start - 0.01 && b < c.start + c.len - 0.01);
        if (!covered && b < b1) { if (run === null) run = b; }
        else if (run !== null) { if (b - run >= 2) build(s, i, n, run, b); run = null; }
      }
    });
    joinClips(nowBeat);
    return keys;
  }
  J.reconcile = reconcile;

  /** Rebuilds cut clips at the playhead and start them again; on the same layer, the same effect with
   *  the same settings and colors, end to end, is one clip, so it runs on without restarting. Clips
   *  that are already over are left as they are. */
  function joinClips(nowBeat) {
    const same = (x, y) => x.effect === y.effect && (x.opacity ?? 1) === (y.opacity ?? 1) && x.blend === y.blend
      && JSON.stringify(x.params) === JSON.stringify(y.params) && JSON.stringify(x.palette) === JSON.stringify(y.palette);
    for (const lane of St.P().lanes) {
      lane.clips.sort((x, y) => x.start - y.start);
      const kept = [];
      for (const c of lane.clips) {
        const prev = kept[kept.length - 1];
        if (prev && prev.start + prev.len > nowBeat - 1 && Math.abs(prev.start + prev.len - c.start) < 0.01 && same(prev, c)) {
          prev.len = c.start + c.len - prev.start; prev.fadeOut = c.fadeOut;
        } else kept.push(c);
      }
      lane.clips = kept;
    }
  }                                  // for checking by hand

  /** Clear [b0, b1) for a rebuild. A clip that began earlier is cut back to b0, not deleted: it is
   *  what the room is looking at right now, and removing it would black the lights out mid-verse. */
  function makeRoom(b0, b1) {
    for (const lane of St.P().lanes) {
      lane.clips = lane.clips.filter(c => !(c.start >= b0 && c.start < b1));
      for (const c of lane.clips) if (c.start < b0 && c.start + c.len > b0) c.len = b0 - c.start;
    }
  }

  function leave() {
    S.albumPalette = null; S.jamColors = null; S.jamExtras = null; S.jamRainbow = false; J.doc = null;
    if (!J.live) return;
    J.live = false; J.key = null; J.version = -1; J.anchor = null; S.audio.playing = false; S.audio.pos = 0;
    St.updateTransport(); St.stopOutput(); St.draw();
    toast('Jam stopped.');
  }

  // ------------------------------------------------------------------ open / close
  async function toggle(open) {
    J.open = open ?? !J.open;
    $('#jamPanel').hidden = !J.open;
    if (J.open) {
      try { J.state = await api('jam/state'); } catch (e) { J.state = {}; }
      render();
      if (!J.outputs) api('jam/outputs').then(o => { J.outputs = Array.isArray(o) ? o : []; render(); }).catch(() => { J.outputs = []; });
    }
  }
  $('#btnJam').onclick = () => toggle();
  $('#jamClose').onclick = () => toggle(false);
  window.addEventListener('focus', () => { if (J.open) tick(); });     // back from the Spotify sign-in window
  J.timer = setInterval(tick, 1000);
  tick();
})();
