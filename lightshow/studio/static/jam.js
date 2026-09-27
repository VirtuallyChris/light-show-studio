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
          <div class="hint">While the studio is open it is a Spotify speaker called <b>Light Show Studio</b>: pick it from the device list on your phone, or host the Jam with it. Its sound comes out of this window's output, which is Windows' default output unless you changed it; choose that output under <i>Listen to</i>.</div>
          <label class="jam-check"><input id="jamAuto" type="checkbox" ${st.auto ? 'checked' : ''}> Start the jam by itself when music starts playing on Light Show Studio, with these settings</label>
          ${st.auto_error ? `<div class="warn">The jam could not start by itself: ${esc(st.auto_error)}</div>` : ''}
          ${sp.signed_in && !sp.can_stream ? '<div class="warn">Sign in to Spotify again: the studio player needs a permission the earlier sign-in did not ask for.</div>' : ''}
        </div>
        <div class="jam-row"><label>Listen to</label><select id="jamDevice"><option value="">Default output</option></select></div>
        <div class="hint" id="jamAppHint">Point the Spotify app at an output nobody hears (a muted device, a monitor without speakers, a virtual cable), and choose it here. The house hears it through the studio, late.</div></div>
      <div class="jam-sec"><h4>3 · Where the house hears it</h4>
        <div class="jam-row"><label>Play on</label><select id="jamWhere">
          <option value="cast" ${st.speaker_connected ? '' : 'disabled'}>Cast: ${st.speaker_connected ? esc(st.speaker) : 'press Cast first'}</option>
          <option value="pc">This PC</option>
          <option value="none">Nowhere (preview only)</option></select></div>
        <div class="jam-row" id="jamPcRow"><label>PC output</label><select id="jamPcDevice"><option value="">Default output</option></select></div>
        <div class="hint" id="jamPcHint">Pick your headphones or speakers here, and a different, silent output for Spotify above.</div>
        <div class="jam-row"><label>Stay behind by</label><span><input id="jamDelay" type="number" min="10" max="120" step="5" value="45" class="num"> s</span></div>
        <div class="jam-row"><label title="Spotify: Settings, Playback, Crossfade. The studio cannot read it, and needs it to tell a skip from a song that simply ended.">Spotify crossfade</label><span><input id="jamXfade" type="number" min="0" max="12" step="1" value="0" class="num"> s</span></div>
        <div class="jam-row"><label>On a skip</label><select id="jamOnSkip">
          <option value="keep">Keep the delay</option>
          <option value="jump">Jump right away</option></select></div>
        <div class="hint">The lights study this much music before the room hears it. Keeping the delay, the room hears a skip that much later, and the lights see the new song coming. Jumping, the room hears it at once, but the new song starts with almost no lead, and the delay only grows back a few seconds between later songs. Play and pause follow the phone.</div></div>
      <div class="jam-actions">
        <button id="jamStart" class="accent" ${sp.signed_in ? '' : 'disabled'}>Start jam</button>
        <button id="jamOpener" ${sp.signed_in && st.speaker_connected ? '' : 'disabled'} title="Play one of your shows from disk first. Spotify is released as it ends, so the jam begins with the buffer already full.">Start behind this song</button>
        <button id="jamDemo" title="Synthetic songs and a pretend Spotify. No sound, no speakers, no sign-in.">Silent demo</button></div>`;
    } else {
      const room = st.room, ahead = st.ahead; const pct = Math.max(0, Math.min(100, 100 * (st.delay || 0) / (st.target_delay || 45)));
      html += `<div class="jam-sec"><h4>${esc(st.mode || 'jam')}</h4>
        <div class="jam-now"><div class="lbl">In the room</div><div class="ttl">${room ? esc(room.label) : (st.streaming ? 'between songs' : `filling the buffer… ${Math.round(st.buffered || 0)} s of ${Math.round(st.target_delay)} s`)}</div>${room ? badge(room.kind) + styleTag(room) + (room.project ? ` <span class="hint">${esc(room.project)}</span>` : '') : ''}</div>
        <div class="jam-now"><div class="lbl">On Spotify now, ${Math.round(st.delay || 0)} s ahead of the room</div><div class="ttl">${ahead ? esc(ahead.label) : '—'}</div>${ahead ? badge(ahead.kind) + styleTag(ahead) + (ahead.kind === 'live' ? ` <span class="hint">analysed to ${fmt(ahead.horizon)}</span>` : '') : ''}</div>
        <div class="jam-meter" title="How far the room is behind Spotify"><div style="width:${pct}%"></div><span>${(st.delay || 0).toFixed(1)} s behind · aiming for ${Math.round(st.target_delay)} s</span></div>
        ${st.paused ? '<div class="hint">Paused on the phone. The buffer is held.</div>' : ''}
        ${st.handover ? `<div class="hint">${st.handover === 'armed' ? 'Spotify is held until your opening song is nearly over.' : 'Filling the buffer behind the opening song…'}</div>` : ''}
        ${st.capture && st.capture.error ? `<div class="warn">Capture: ${esc(st.capture.error)}</div>` : ''}
        ${st.output_error ? `<div class="warn">Playback: ${esc(st.output_error)}</div>` : ''}
        ${sp.error ? `<div class="warn">Spotify: ${esc(sp.error)}</div>` : ''}</div>
      <div class="jam-sec"><h4>Up next</h4>${(st.queue || []).slice(0, 8).map(q => `<div class="jam-q"><span>${esc(q.label)}</span>${badge(q.kind)}</div>`).join('') || '<div class="hint">Nothing queued.</div>'}</div>
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
    const busy = !running && active && el.contains(active) && /^(INPUT|SELECT|TEXTAREA)$/.test(active.tagName);
    const shown = html + JSON.stringify(J.outputs || null);          // the device lists are filled in by wire()
    if (shown !== J.shown && !busy) { el.innerHTML = html; J.shown = shown; wire(keep); }
    $('#btnJam').classList.toggle('on', running);
    $('#btnJam').textContent = running ? (st.streaming ? 'Jam · live' : 'Jam · filling') : 'Jam';
  }
  const fmt = t => `${Math.floor(t / 60)}:${String(Math.floor(t % 60)).padStart(2, '0')}`;
  function playerLine(st) {
    const p = st.player || {};
    if (p.error) return `<span class="warn">Player: ${esc(p.error)}</span>`;
    if (p.ready) return `<b class="ok">Light Show Studio is in Spotify</b>${p.track ? ` · ${esc(p.track)}` : ''}`;
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
    if (source) { source.onchange = () => { showSource(); api('jam/prefs', { source: source.value }).then(st => { J.state = st; ensurePlayer(st); }).catch(() => {}); }; showSource(); }
    const auto = $('#jamAuto'); if (auto) auto.onchange = () => api('jam/prefs', { auto: auto.checked }).then(st => { J.state = st; }).catch(() => {});
    on('jamPlayerHere', async () => { await api('jam/player/transfer', {}); toast('Spotify is now playing on Light Show Studio'); });
    const dev = $('#jamDevice');
    if (dev) { dev.innerHTML = '<option value="">Default output</option>' + (J.outputs || []).filter(o => o.name).map(o => `<option value="${esc(o.name)}">${esc(o.name)}${o.default ? ' (default)' : ''}</option>`).join(''); if (keep.jamDevice) dev.value = keep.jamDevice; }
    const cid = $('#jamClient'); if (cid) cid.onchange = () => api('spotify/client', { client_id: cid.value.trim() }).then(s => { J.state = { ...J.state, ...s }; render(); }).catch(e => toast(e.message, true));
    on('jamSignIn', async () => { if (cid && cid.value.trim() !== ((J.state.spotify || {}).client_id || '')) await api('spotify/client', { client_id: cid.value.trim() }); window.open('/api/spotify/login', 'spotify', 'width=520,height=760'); });
    on('jamSignOut', async () => { J.state = await api('spotify/signout', {}); render(); });
    const start = extra => api('jam/start', { device: dev ? dev.value : '', target_delay: parseFloat(($('#jamDelay') || {}).value) || 45, crossfade: parseFloat(($('#jamXfade') || {}).value) || 0, skip_jumps: (($('#jamOnSkip') || {}).value) === 'jump', source: (source && source.value) || 'studio', speakers: (where && where.value) || 'none', pc_device: (pcDev && pcDev.value) || '', ...extra });
    on('jamStart', async () => { J.state = await start({}); render(); toast({ cast: 'Jam started. The speakers join once the buffer is full.', pc: 'Jam started. This PC starts playing once the buffer is full.' }[J.state.output_kind] || 'Jam started as a preview: nothing plays.'); });
    on('jamOpener', async () => { if (!S.cast.active || !S.audio.playing) return toast('Cast one of your shows and press play first, then press this', true); J.state = await start({ opener: true, speakers: 'cast' }); render(); toast('Armed. Spotify will be released as this song ends.'); });
    on('jamDemo', async () => { J.state = await start({ demo: true, speakers: 'none', target_delay: 20 }); render(); toast('Silent demo running: synthetic songs, no sound.'); });
    on('jamStop', async () => { J.state = await api('jam/stop', {}); leave(); render(); });
    on('jamSkip', () => api('jam/demo/skip', {}));
    on('jamPause', () => api('jam/demo/' + (J.state.paused ? 'resume' : 'pause'), {}));
    on('jamSync', async () => { toast('Listening to the room…'); const r = await api('jam/synccheck', {}); toast(r.confidence >= 0.5 ? `Speakers corrected by ${r.offset_ms} ms (now ${r.speaker_offset_ms} ms)` : (r.reason || 'Could not get a clear reading; try again in a moment'), r.confidence < 0.5); });
  }

  // ------------------------------------------------------------------ following the room
  /** The studio is a Spotify speaker while it is open: Spotify's own player runs in a hidden frame. */
  function ensurePlayer(st) {
    // (?player=off in the address leaves it out, for a test window beside the real studio)
    const sp = st.spotify || {}, want = sp.signed_in && sp.can_stream && ((st.last || {}).source || 'studio') === 'studio'
      && new URLSearchParams(location.search).get('player') !== 'off';
    let f = document.getElementById('spotifyPlayer');
    if (want && !f) {
      f = document.createElement('iframe');
      f.id = 'spotifyPlayer'; f.src = '/static/player.html?embedded=1'; f.allow = 'encrypted-media; autoplay';
      f.title = 'Spotify player'; f.tabIndex = -1; f.setAttribute('aria-hidden', 'true');
      f.style.cssText = 'position:fixed;width:1px;height:1px;left:-10px;top:-10px;border:0;opacity:0;pointer-events:none';
      document.body.appendChild(f);
    } else if (!want && f) f.remove();
  }

  async function tick() {
    if (J.fetching) return; J.fetching = true;
    try {
      const st = await api('jam/state'); const was = J.state && J.state.running;
      J.state = st;
      ensurePlayer(st);
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

  async function load(room) {
    const doc = await api('jam/analysis?key=' + encodeURIComponent(room.key));
    J.live = true;
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
    p.sections = a.sections.map(s => ({ start: s.start, end: s.end, type: s.type }));
    $('#bpm').value = p.bpm.toFixed(2);
    const safeBeat = St.timeToBeat(J.pos()) + 8;          // two bars of grace: never change what is about to be seen
    const keys = sectionKeys(); const counts = {};
    const style = St.showStyle(), restyled = style !== J.style;   // the genre arrived: rebuild what is ahead in its style
    if (restyled) toast(`${(doc.track || {}).name || 'This track'}: ${style} style from here on`);
    p.sections.forEach((s, i) => {
      const n = counts[s.type] = (counts[s.type] || 0) + 1;
      const startBeat = Math.round(St.timeToBeat(s.start));
      if (startBeat >= safeBeat && (keys[i] !== J.built[i] || restyled)) { makeRoom(startBeat, Math.round(St.timeToBeat(s.end))); St.buildSection(s, i, false, n - 1); }
      else if (startBeat < safeBeat && keys[i] !== J.built[i]) {
        // the section being played grew: stretch the clips that ran to its old end
        const endBeat = Math.round(St.timeToBeat(s.end));
        for (const lane of p.lanes) for (const c of lane.clips) if (c.start >= startBeat - 0.5 && c.start < endBeat && c.len > 8 && c.start + c.len < endBeat && !lane.clips.some(k => k !== c && k.start >= c.start + c.len && k.start < endBeat)) c.len = endBeat - c.start;
      }
    });
    J.built = keys; J.version = doc.version; J.style = style;
    St.draw();
  }

  /** Clear [b0, b1) for a rebuild. A clip that began earlier is cut back to b0, not deleted: it is
   *  what the room is looking at right now, and removing it would black the lights out mid-verse. */
  function makeRoom(b0, b1) {
    for (const lane of St.P().lanes) {
      lane.clips = lane.clips.filter(c => !(c.start >= b0 && c.start < b1));
      for (const c of lane.clips) if (c.start < b0 && c.start + c.len > b0) c.len = b0 - c.start;
    }
  }

  function leave() {
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
