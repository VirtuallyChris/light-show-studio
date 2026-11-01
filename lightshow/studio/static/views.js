// The app's three views, on the rail: Studio (the editor), Party (the jam: what plays, what is next, the moments, the
// room, and the jam's own controls) and Settings (everything about the home, in one place). Party and Settings use
// the same server calls as the phone's Party Hub, so the desktop can do everything the phone can.
(() => {
  const St = window.Studio, $ = s => document.querySelector(s);
  const esc = s => String(s ?? '').replace(/[&<>"]/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]));
  const get = async url => { const r = await fetch(url); if (!r.ok) throw new Error(r.status); return r.json(); };
  const post = async (url, body) => { const r = await fetch(url, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body || {}) }); return r.json(); };
  const party = (path, body) => body === undefined ? get('/api/party/' + path) : post('/api/party/' + path, body);
  const fmt = t => (t == null || !isFinite(t)) ? '0:00' : `${Math.floor(t / 60)}:${String(Math.floor(t % 60)).padStart(2, '0')}`;
  const COLORS = [['#ffaa5a', 'Warm white', '#ffdcae'], ['#ffffff', 'White'], ['#ff0000', 'Red'], ['#ff6000', 'Orange'], ['#ffbf00', 'Amber'], ['#ffff00', 'Yellow'],
    ['#80ff00', 'Lime'], ['#00ff00', 'Green'], ['#00ff80', 'Mint'], ['#00ffff', 'Cyan'], ['#0080ff', 'Sky blue'], ['#0000ff', 'Blue'], ['#4000ff', 'Indigo'],
    ['#8000ff', 'Purple'], ['#ff00ff', 'Magenta'], ['#ff0080', 'Pink']];
  const shown = c => { const x = COLORS.find(y => y[0] === c); return x ? x[2] || x[0] : c; };
  const ink = c => { const v = parseInt(shown(c).slice(1), 16); return 0.299 * (v >> 16) + 0.587 * ((v >> 8) & 255) + 0.114 * (v & 255) > 150 ? '#1a1206' : '#fff'; };
  const swatches = picked => COLORS.map(([c, n]) => `<button type="button" data-c="${c}" class="${c === picked ? 'on' : ''}" style="background:${shown(c)}" aria-label="${n}" title="${n}"></button>`).join('');
  const NOTE = '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M9 18V6l11-2v12"></path><circle cx="6" cy="18" r="3"></circle><circle cx="17" cy="16" r="3"></circle></svg>';

  // ------------------------------------------------------------------ switching views
  let view = 'studio';
  function showView(v) {
    view = v;
    for (const [k, id] of [['studio', '#viewStudio'], ['party', '#viewParty'], ['settings', '#viewSettings']]) $(id).hidden = k !== v;
    for (const b of document.querySelectorAll('.view-btn[data-view]')) b.classList.toggle('on', b.dataset.view === v);
    if (window.Jam) window.Jam.toggle(v === 'party');
    if (v === 'party') pollParty();
    if (v === 'settings') showSection(section);
    try { localStorage.setItem('lss-view', v); } catch (e) { /* not kept */ }
  }
  for (const b of document.querySelectorAll('.view-btn[data-view]')) b.onclick = () => showView(b.dataset.view);
  $('#btnJam').onclick = () => showView('party');
  $('#btnSettings').onclick = e => { e.stopPropagation(); showView('settings'); };

  // ------------------------------------------------------------------ the Party view
  let ps = null, partyTimer = null, clock = null, busy = {}, colorBusy = {};
  async function pollParty() {
    clearTimeout(partyTimer);
    if (view !== 'party') return;
    try { ps = await party('state'); renderParty(); } catch (e) { $('#pvStatus').textContent = 'Can’t reach the studio'; }
    partyTimer = setTimeout(pollParty, 1500);
  }
  function renderParty() {
    const st = ps || {}, r = st.room;
    const status = $('#pvStatus');
    status.textContent = !st.running ? 'Jam not running' : st.paused ? 'Paused' : st.streaming ? `Live · ${Math.round(st.delay || 0)} s behind Spotify` : 'Filling the buffer…';
    status.className = 'pv-pill' + (st.running && st.streaming && !st.paused ? ' live' : '');
    const where = String(st.mode || '').split(' to ')[1];
    $('#pvWhere').hidden = !(st.running && where); $('#pvWhere').textContent = where ? `Playing on ${where}` : '';
    renderNow(st, r); renderMoments(st); renderQueue(st); renderLevel(st); renderRooms(st); renderSource(st);
  }
  let nowSig = '';
  function renderNow(st, r) {
    const el = $('#pvNow');
    const sig = JSON.stringify([st.running, r && [r.id, r.playing, r.colors, r.theme_off, r.style, r.genre_found]]);
    clock = r && st.running ? { pos: r.position || 0, at: performance.now(), playing: !!r.playing, dur: r.duration || 0 } : null;
    if (sig === nowSig) return tickClock();
    nowSig = sig;
    if (!st.running || !r) {
      el.innerHTML = `<div class="pv-art idle">${NOTE}</div><div class="pv-idle">${st.running ? 'Between songs' : 'Start the jam on the right: Spotify picks the music, the lights follow every song.'}</div>`;
      return;
    }
    const art = r.art || r.image, colors = r.colors || [];
    el.innerHTML = `<div class="pv-art" ${art ? `style="background-image:url('${esc(art)}')"` : ''}>${art ? '' : NOTE}</div>
      <div class="pv-eyebrow">In the room</div><div class="pv-title">${esc(r.name || r.label)}</div><div class="pv-artist">${esc((r.artists || []).join(', '))}</div>
      <div class="pv-tags">${colors.length ? colors.slice(0, 2).map((c, i) => `<button class="pv-sw" data-k="${i ? 'secondary' : 'primary'}" style="background:${esc(c)}" aria-label="${i ? 'Secondary' : 'Primary'} color" title="Change"></button>`).join('') : ''}
        <button class="pv-chip ${r.theme_off ? '' : 'on'}" id="pvTheme" title="${r.theme_off ? 'Plays in its album cover’s colors: click for the party’s' : 'Plays in the party’s colors: click for its album cover’s'}">${r.theme_off ? 'Album colors' : 'Party colors'}</button>
        ${r.style || r.genre_found ? `<span class="pv-chip quiet">${esc([r.style && r.style[0].toUpperCase() + r.style.slice(1), r.genre_found].filter(Boolean).join(' · '))}</span>` : ''}</div>
      <div class="pv-bar"><div id="pvProg"></div></div><div class="pv-times"><span id="pvPos">0:00</span><span>${fmt(r.duration)}</span></div>
      <div class="pv-controls"><button class="pv-btn" id="pvPrev" aria-label="Previous song">⏮</button>
        <button class="pv-btn big" id="pvPlay" aria-label="${r.playing ? 'Pause' : 'Play'}">${r.playing ? '❚❚' : '▶'}</button>
        <button class="pv-btn" id="pvNext" aria-label="Next song">⏭</button></div>`;
    $('#pvPlay').onclick = () => party('playback', { action: r.playing ? 'pause' : 'resume' }).then(pollParty);
    $('#pvPrev').onclick = () => party('playback', { action: 'previous' }).then(pollParty);
    $('#pvNext').onclick = () => party('playback', { action: 'next' }).then(pollParty);
    $('#pvTheme').onclick = () => party('colors', r.theme_off ? { track_id: r.id } : { track_id: r.id, theme: false }).then(pollParty);
    for (const b of el.querySelectorAll('.pv-sw')) b.onclick = e => pickColor(e.currentTarget, c => party('colors', { track_id: r.id, primary: b.dataset.k === 'primary' ? c : colors[0], secondary: b.dataset.k === 'secondary' ? c : colors[1] }).then(pollParty));
    tickClock();
  }
  function tickClock() {
    const p = $('#pvProg'), pos = $('#pvPos'); if (!clock || !p) return;
    const t = Math.min(clock.dur || 1e9, clock.pos + (clock.playing ? (performance.now() - clock.at) / 1000 : 0));
    p.style.width = clock.dur ? `${Math.min(100, 100 * t / clock.dur)}%` : '0'; pos.textContent = fmt(t);
  }
  setInterval(() => { if (view === 'party') tickClock(); }, 250);
  // a small color picker beside what was clicked
  function pickColor(anchor, done) {
    let pop = $('#pvPick'); if (!pop) { pop = document.createElement('div'); pop.id = 'pvPick'; pop.className = 'pv-pick'; document.body.appendChild(pop); }
    pop.innerHTML = `<div class="pv-swatches">${swatches('')}</div>`; pop.hidden = false;
    const r = anchor.getBoundingClientRect(); pop.style.left = Math.min(innerWidth - 300, r.left) + 'px'; pop.style.top = (r.bottom + 8) + 'px';
    for (const b of pop.querySelectorAll('[data-c]')) b.onclick = () => { pop.hidden = true; done(b.dataset.c); };
    setTimeout(() => document.addEventListener('mousedown', function off(e) { if (!pop.contains(e.target)) { pop.hidden = true; document.removeEventListener('mousedown', off); } }), 0);
  }
  let momentsSig = '';
  function renderMoments(st) {
    const modes = st.modes || [], cues = (st.cues || []).filter(c => c.id !== st.doorbell_cue || (st.cue && st.cue.id === c.id && st.cue.phase !== 'done'));
    $('#pvMomentsCard').hidden = !modes.length && !cues.length;
    const sig = JSON.stringify([modes.map(m => [m.id, m.name]), cues.map(c => [c.id, c.name])]);
    if (sig !== momentsSig) {
      momentsSig = sig;
      $('#pvMoments').innerHTML = modes.map(m => `<button class="pv-moment" data-mode="${esc(m.id)}" aria-pressed="false">${esc(m.name)}<span class="sub"></span></button>`).join('') +
        cues.map(c => `<button class="pv-moment cue" data-cue="${esc(c.id)}">${esc(c.name)}<span class="sub"></span></button>`).join('');
      for (const b of $('#pvMoments').querySelectorAll('[data-mode]')) b.onclick = () => {
        const m = ((ps && ps.modes) || []).find(x => x.id === b.dataset.mode); if (!m) return;
        const on = !m.on;
        if (on && m.confirm && !(confirm(`Turn on ${m.name}?`) && confirm(`Are you sure? ${m.name} starts now.`))) return;
        busy[m.id] = performance.now() + 4000; m.on = on; if (on) m.color = m.own || m.color; paintMoments(ps);
        party('mode', { id: m.id, on }).then(pollParty).catch(() => { busy[m.id] = 0; });
      };
      for (const b of $('#pvMoments').querySelectorAll('[data-cue]')) b.onclick = async () => {
        const running = ps && ps.cue && ps.cue.id === b.dataset.cue && !['done', 'stopped'].includes(ps.cue.phase);
        await fetch(running ? '/api/cues/stop' : '/api/cues/run', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ id: b.dataset.cue }) }).catch(() => {});
        pollParty();
      };
    }
    paintMoments(st);
  }
  function paintMoments(st) {
    for (const m of st.modes || []) {
      const b = $('#pvMoments').querySelector(`[data-mode="${CSS.escape(m.id)}"]`); if (!b) continue;
      if (performance.now() > (busy[m.id] || 0)) b.setAttribute('aria-pressed', m.on ? 'true' : 'false');
      b.style.setProperty('--on', shown(m.color)); b.style.setProperty('--on-text', ink(m.color));
      b.querySelector('.sub').textContent = b.getAttribute('aria-pressed') === 'true' ? 'On' : m.pause ? 'Pauses the music' : `Music ${Math.round(m.music * 100)}%`;
    }
    for (const b of $('#pvMoments').querySelectorAll('[data-cue]')) {
      const c = st.cue, run = c && c.id === b.dataset.cue && !['done', 'stopped'].includes(c.phase);
      b.classList.toggle('running', !!run); b.querySelector('.sub').textContent = run ? 'Playing · click to stop' : 'Cue';
    }
    const on = (st.modes || []).filter(m => m.on), box = $('#pvModeColors'), sig = on.map(m => m.id + m.color).join();
    if (box.dataset.sig === sig || on.some(m => performance.now() < (colorBusy[m.id] || 0))) return;
    box.dataset.sig = sig;
    box.innerHTML = on.map(m => `<div class="pv-modecolors" data-for="${esc(m.id)}"><div class="hint">${esc(m.name)}: its color</div><div class="pv-swatches">${swatches(m.color)}</div></div>`).join('');
    for (const g of box.querySelectorAll('[data-for]')) for (const sw of g.querySelectorAll('[data-c]')) sw.onclick = () => {
      colorBusy[g.dataset.for] = performance.now() + 4000;
      for (const x of g.querySelectorAll('[data-c]')) x.classList.toggle('on', x === sw);
      party('mode', { id: g.dataset.for, color: sw.dataset.c }).catch(() => { colorBusy[g.dataset.for] = 0; });
    };
  }
  let queueSig = '';
  function renderQueue(st) {
    const up = st.upcoming || [], sig = JSON.stringify(up.map(t => [t.id, t.kind, t.learning, t.colors]));
    const learning = up.filter(t => t.kind === 'live').length;
    $('#pvQueueNote').textContent = up.length ? `${up.length - learning} learned${learning ? ` · ${learning} not yet` : ''}` : '';
    if (sig === queueSig) return;
    queueSig = sig;
    $('#pvQueue').innerHTML = !st.running ? '<div class="pv-empty">The queue shows here once the jam is on.</div>' : !up.length ? '<div class="pv-empty">Nothing queued.</div>'
      : up.map(t => { const learned = t.kind !== 'live', c = t.colors || [];
          return `<div class="pv-track"><div class="pv-art small" ${t.image ? `style="background-image:url('${esc(t.image)}')"` : ''}>${t.image ? '' : NOTE}</div>
            <div class="pv-track-t"><div class="t">${esc(t.name || t.label)}</div><div class="a">${esc((t.artists || []).join(', '))}</div></div>
            <span class="pv-tag ${learned ? 'ok' : 'wait'}">${learned ? 'Learned' : 'Learning'}</span>
            ${c.length ? `<span class="pv-pair"><i style="background:${esc(c[0])}"></i><i style="background:${esc(c[1] || c[0])}"></i></span>` : ''}</div>`; }).join('');
  }
  let levelDrag = false;
  function renderLevel(st) {
    const v = Math.round((st.party_level ?? 1) * 100), s = $('#pvLevel');
    if (!levelDrag && document.activeElement !== s) s.value = v;
    $('#pvLevelVal').textContent = (levelDrag ? s.value : v) + '%';
    const ramp = st.party_ramp;
    $('#pvRamp').textContent = ramp ? 'Stop building up' : 'Build to 100% over an hour';
    $('#pvRampNote').textContent = ramp && ramp.minutes ? `to 100% over ${Math.round(ramp.minutes)} min` : '';
  }
  $('#pvLevel').oninput = e => { levelDrag = true; $('#pvLevelVal').textContent = e.target.value + '%'; };
  $('#pvLevel').onchange = e => { party('level', { level: +e.target.value / 100 }).finally(() => { setTimeout(() => { levelDrag = false; }, 800); }); };
  $('#pvRamp').onclick = () => party('ramp', { on: !(ps && ps.party_ramp) }).then(pollParty);
  let roomsSig = '', roomBusy = 0;
  function renderRooms(st) {
    const rooms = st.rooms || [], sig = JSON.stringify(rooms.map(r => [r.id, r.name, r.on]));
    if (sig === roomsSig || performance.now() < roomBusy) return;
    roomsSig = sig;
    $('#pvRooms').innerHTML = rooms.length ? rooms.map(r => `<label class="pv-room"><span><b>${esc(r.name)}</b><span class="hint">${r.on ? 'In the party' : 'Lights back to normal, speakers muted'}</span></span>
        <span class="pv-switch"><input type="checkbox" data-id="${esc(r.id)}" ${r.on ? 'checked' : ''} aria-label="${esc(r.name)} in the party"><i></i></span></label>`).join('')
      : '<div class="pv-empty">No rooms yet: <button class="pv-link" data-go="rooms">set them up</button></div>';
    for (const sw of $('#pvRooms').querySelectorAll('input[data-id]')) sw.onchange = async () => {
      roomBusy = performance.now() + 6000;
      await post('/api/rooms/on', { id: sw.dataset.id, on: sw.checked }).catch(() => {});
    };
    for (const b of $('#pvRooms').querySelectorAll('[data-go]')) b.onclick = () => { section = b.dataset.go; showView('settings'); };
  }
  function renderSource(st) {
    if (!$('#pvSource')) return;                                      // (the jam's own Colors, on the right, says it)
    const src = st.color_source || (st.theme_on === false ? 'album' : 'party');
    for (const b of document.querySelectorAll('#pvSource button')) b.classList.toggle('on', b.dataset.s === src);
    $('#pvSourceNote').textContent = { album: 'Each song in the colors of its album cover.', party: 'Every song in the party’s colors.', wild: 'The show picks colors part by part, from all of them.' }[src] || '';
  }
  for (const b of document.querySelectorAll('#pvSource button')) b.onclick = () => party('theme', { source: b.dataset.s }).then(pollParty);
  $('#pvEditMoments').onclick = () => { section = 'moments'; showView('settings'); };

  // ------------------------------------------------------------------ the Settings view
  const SECTIONS = [
    ['start', 'Get started', '✓'], ['lights', 'Lights', 'L'], ['rooms', 'Rooms and house map', 'R'], ['sound', 'Speakers and sound', 'S'],
    ['spotify', 'Spotify', '♪'], ['moments', 'Moments', 'M'], ['addons', 'Add-ons', '+'], ['models', 'AI models', 'AI'], ['phone', 'Your phone', 'P'],
  ];
  let section = 'start', setup = null;
  // a downloaded app's updates (a copy run from source has none of this: git pull updates it)
  async function askUpdate() {
    const u = await get('/api/update').catch(() => null);
    if (!u) return;
    if (u.packaged && !SECTIONS.some(s => s[0] === 'updates')) SECTIONS.push(['updates', 'Updates', '↑']);
    $('#railUpdate').hidden = !u.available;
  }
  $('#railUpdate').onclick = () => { showView('settings'); showSection('updates'); };
  askUpdate();
  setInterval(askUpdate, 30 * 60 * 1000);
  try { section = localStorage.getItem('lss-section') || 'start'; } catch (e) { /* the first */ }
  async function renderNav() {
    try { setup = await get('/api/setup'); } catch (e) { setup = null; }
    const by = Object.fromEntries(((setup && setup.items) || []).map(i => [i.id, i]));
    const note = { start: setup ? `${setup.items.filter(i => i.done).length} of ${setup.items.length} done` : '', lights: by.lights && by.lights.detail.split(':')[0],
      rooms: by.rooms && by.rooms.done ? by.rooms.detail : '', spotify: by.spotify && by.spotify.detail, models: by.models && (by.models.done ? 'Installed' : 'Optional') };
    $('#stNav').innerHTML = SECTIONS.map(([id, name, icon]) => `<button class="st-item ${id === section ? 'on' : ''}" data-s="${id}"><span class="st-icon">${icon}</span><span class="st-name">${name}</span><span class="st-note">${esc(note[id] || '')}</span></button>`).join('');
    for (const b of $('#stNav').querySelectorAll('[data-s]')) b.onclick = () => showSection(b.dataset.s);
  }
  function showSection(id) {
    section = SECTIONS.some(s => s[0] === id) ? id : 'start';
    try { localStorage.setItem('lss-section', section); } catch (e) { /* not kept */ }
    renderNav();
    const [, title] = SECTIONS.find(s => s[0] === section);
    $('#stMain').innerHTML = `<div class="st-head"><h2>${title}</h2><p class="st-lead">${LEADS[section]}</p></div><div id="stBody" class="st-body"></div>`;
    (RENDER[section] || (() => {}))($('#stBody'));
  }
  const LEADS = {
    start: 'A few steps and your home is ready. Each one has the button that does it.',
    lights: 'The lights the studio plays on. Govee lights need LAN Control switched on in the Govee Home app; Philips Hue lights come through their bridge.',
    rooms: 'Put your lights and speakers in rooms, then draw your home and place each light where it stands, so shows can move across the house.',
    sound: 'Where the music plays, and which of this computer’s sound devices the studio uses.',
    spotify: 'The jam plays what Spotify plays. It needs Spotify Premium and a free developer app of your own.',
    moments: 'Buttons that hold the party for a moment (every light one color, the music down or paused) and cues that play a show of their own.',
    addons: 'Features for one kind of home. Each does nothing until it is switched on.',
    models: 'Optional: AI models that hear beats, sections and drums better. A few GB, downloaded once, on NVIDIA, AMD, Apple or the CPU.',
    updates: 'A new version installs by itself when you open the app. Nothing restarts while it is open unless you ask.',
    phone: 'Run the party from your phone: the Party Hub works in any phone browser on the same Wi-Fi.',
  };
  const card = (inner, cls = '') => `<section class="st-card ${cls}">${inner}</section>`;
  const row = (title, help, control) => `<div class="st-row"><div class="st-row-t"><b>${title}</b>${help ? `<span>${help}</span>` : ''}</div><div class="st-row-c">${control}</div></div>`;
  const toggle = (attrs, on) => `<label class="pv-switch"><input type="checkbox" ${attrs} ${on ? 'checked' : ''}><i></i></label>`;

  const RENDER = {
    async start(el) {
      const d = setup || await get('/api/setup');
      const ACTION = { lights: ['Open lights', () => showSection('lights')], rooms: ['Rooms', () => showSection('rooms')], map: ['House map', () => showSection('rooms')],
        speakers: ['Speakers', () => showSection('sound')], spotify: ['Spotify', () => showSection('spotify')], sound: ['Sound devices', () => showSection('sound')],
        models: ['AI models', () => showSection('models')], phone: ['Your phone', () => showSection('phone')], addons: ['Add-ons', () => showSection('addons')] };
      el.innerHTML = card(d.items.map((it, i) => `<div class="st-step ${it.done ? 'done' : ''}"><span class="st-n">${it.done ? '✓' : i + 1}</span>
        <span class="st-row-t"><b>${esc(it.title)}</b><span>${esc(it.detail)}</span></span>${ACTION[it.id] ? `<button data-a="${it.id}">${ACTION[it.id][0]} ›</button>` : ''}</div>`).join(''));
      for (const b of el.querySelectorAll('[data-a]')) b.onclick = () => ACTION[b.dataset.a][1]();
    },
    async lights(el) {
      const [lights, hue, cfg] = await Promise.all([get('/api/devices').catch(() => []), get('/api/hue').catch(() => null), get('/api/settings').catch(() => ({}))]);
      const list = Array.isArray(lights) ? lights : lights.devices || [];
      el.innerHTML = card(`<div class="st-h">On the network</div>` + (list.length ? list.map(l => row(esc(l.name || l.sku), `${esc(l.sku)} · ${esc(l.ip)} · ${l.segments} segments${l.online === false ? ' · not answering' : ''}`, `<span class="st-pill ${l.on ? 'on' : ''}">${l.on ? 'Plays the show' : 'Not ticked'}</span>`)).join('')
          : '<p class="hint">None found yet. In the Govee Home app, open each light, then Settings (the gear), and switch on LAN Control.</p>') +
        `<p class="hint">Name lights, set their LED count and tick which ones play in Studio › Lights.</p><button id="stGoLights">Open Studio › Lights</button>`) +
        card(`<div class="st-h">Timing</div><div id="stLatency"></div>`) +
        card(`<div class="st-h">Philips Hue</div>` + (hue && hue.bridges && hue.bridges.length ? hue.bridges.map(b => row(esc(b.name || 'Hue Bridge'), esc(b.ip) + (b.paired ? ' · paired' : ' · press the button on the bridge to pair'),
          b.paired ? '<span class="st-pill on">Paired</span>' : `<button data-pair="${esc(b.id)}">Pair</button>`)).join('') : '<p class="hint">No Hue Bridge found on the network. Hue lights play through an Entertainment area set up in the Hue app.</p>'));
      $('#stGoLights').onclick = () => { showView('studio'); St.showPanel('lights'); };
      const lat = $('#latency').closest('label'); $('#stLatency').appendChild(lat); lat.classList.add('st-latency');
      lat.title = 'Send the lights this many milliseconds ahead of the music, for every song.';
      for (const b of el.querySelectorAll('[data-pair]')) b.onclick = async () => { await post('/api/hue/pair', { bridge: b.dataset.pair }); b.textContent = 'Press the bridge’s button now…'; };
    },
    async rooms(el) {
      el.innerHTML = card(`<div id="stRooms"></div>`) + card(`<div class="st-h">House map</div><iframe class="st-map" src="/party/map" title="House map"></iframe>`, 'flush');
      const pop = $('#roomsPop'); $('#stRooms').appendChild(pop); St.openRooms();
    },
    async sound(el) {
      const [dev, s] = await Promise.all([get('/api/audio/devices'), get('/api/settings').catch(() => ({}))]);
      const help = dev.hidden_help || {}, hiddenFound = dev.jam_hidden_output ? dev.outputs.some(o => o.toLowerCase().includes(dev.jam_hidden_output.toLowerCase())) : !!dev.auto_output;
      const pick = (id, list, value, auto) => `<select id="${id}"><option value="">${esc(auto)}</option>${list.map(n => `<option value="${esc(n)}" ${n === value ? 'selected' : ''}>${esc(n)}</option>`).join('')}</select>`;
      const offs = s.cast_offsets || {};
      el.innerHTML = card(
        row('Microphone', 'Listens to the room for the sync check and to measure each speaker’s delay.', pick('stMic', dev.inputs, dev.mic_device, `Default${dev.default_input ? ': ' + dev.default_input : ''}`) + '<button id="stMicTest">Test</button><span id="stMicLevel" class="hint"></span>') +
        row('The room hears this computer on', 'When the jam plays on this computer instead of the Cast speakers.', pick('stRoomOut', dev.outputs, dev.jam_room_output, `Default${dev.default_output ? ': ' + dev.default_output : ''}`)) +
        (help.label === 'nothing to install' ? '' : row('Spotify plays into', 'An output nobody listens to: the studio mutes the one it picks, records it, and plays it to the room in time with the lights.' +
          (hiddenFound || !help.url ? '' : ` Not on this computer yet: <a href="${esc(help.url)}" target="_blank" rel="noopener">install ${esc(help.label)}</a>.`), pick('stHidden', dev.outputs, dev.jam_hidden_output, dev.auto_output ? `Automatic: ${dev.auto_output}` : 'Automatic')))) +
        card(`<div class="st-h">Cast speakers</div><p class="hint">Each speaker’s delay is measured with the microphone while the jam plays (the sync check), so the lights land on the beat.</p>
          ${Object.keys(offs).length ? Object.entries(offs).map(([n, ms]) => row(esc(n), `Measured: ${ms} ms late`, '<span class="st-pill on">Measured</span>')).join('') : '<p class="hint">None measured yet.</p>'}
          <div id="stCasts" class="hint">Looking for speakers…</div>`);
      const save = (id, key) => { $('#' + id).onchange = async () => { await post('/api/audio/devices', { [key]: $('#' + id).value }); }; };
      save('stMic', 'mic_device'); save('stRoomOut', 'jam_room_output'); save('stHidden', 'jam_hidden_output');
      $('#stMicTest').onclick = async () => {
        $('#stMicLevel').textContent = 'Listening…';
        const r = await get('/api/audio/miclevel').catch(e => ({ error: e.message }));
        $('#stMicLevel').textContent = r.error ? `Could not listen: ${r.error}` : r.level_db < -70 ? 'Heard nothing: is it muted?' : `Hears the room (${r.level_db} dB)`;
      };
      get('/api/cast/devices').then(d => { const list = Array.isArray(d) ? d : d.devices || []; $('#stCasts').textContent = list.length ? `On the network: ${list.map(x => x.name).join(', ')}` : 'No Cast speakers found.'; }).catch(() => { $('#stCasts').textContent = ''; });
    },
    async spotify(el) {
      const st = await get('/api/jam/state').catch(() => ({})), sp = st.spotify || {};
      el.innerHTML = sp.signed_in ? card(row('Signed in to Spotify', 'The jam plays what Spotify plays.', '<button id="stSignOut">Sign out</button>') +
          `<p class="hint">Start the jam, and choose where Spotify plays and where the house hears it, in Party.</p><button id="stGoParty">Open Party</button>`)
        : card(`<ol class="st-steps">
          <li>You need <b>Spotify Premium</b>: Spotify only lets Premium accounts play through apps like this one.</li>
          <li><a href="https://developer.spotify.com/dashboard" target="_blank" rel="noopener">Open the Spotify developer dashboard</a>, sign in, and press <b>Create app</b> (any name and description).</li>
          <li>Under <b>Redirect URIs</b>, add this address exactly: <code>${esc(st.redirect_uri || '')}</code> <button id="stCopyUri">Copy</button></li>
          <li>Tick <b>Web API</b> and <b>Web Playback SDK</b>, then <b>Save</b>.</li>
          <li>In the app’s <b>User Management</b>, add the email of every Spotify account that will sign in here.</li>
          <li>Paste the app’s <b>Client ID</b>: <input id="stClient" value="${esc(sp.client_id || '')}" placeholder="Client ID" spellcheck="false"></li>
          <li><button id="stSignIn" class="accent" ${sp.client_id ? '' : 'disabled'}>Sign in to Spotify</button></li></ol>`);
      const on = (id, fn) => { const b = $('#' + id); if (b) b.onclick = fn; };
      on('stSignOut', async () => { await post('/api/spotify/signout'); showSection('spotify'); });
      on('stGoParty', () => showView('party'));
      on('stCopyUri', async () => { try { await navigator.clipboard.writeText(st.redirect_uri || ''); St.toast('Copied'); } catch (e) { St.toast('Select the address and copy it', true); } });
      const cid = $('#stClient');
      if (cid) { cid.oninput = () => { $('#stSignIn').disabled = !cid.value.trim(); }; cid.onchange = () => post('/api/spotify/client', { client_id: cid.value.trim() }); }
      on('stSignIn', async () => {
        if (cid && cid.value.trim() !== (sp.client_id || '')) await post('/api/spotify/client', { client_id: cid.value.trim() });
        window.open('/api/spotify/login', 'spotify', 'width=520,height=760');
        const t = setInterval(async () => { const s2 = await get('/api/jam/state').catch(() => ({})); if ((s2.spotify || {}).signed_in) { clearInterval(t); if (section === 'spotify') showSection('spotify'); } }, 2000);
        setTimeout(() => clearInterval(t), 180000);
      });
    },
    async moments(el) {
      const st = await party('state').catch(() => ({})), cues = await get('/api/cues').catch(() => ({ cues: [], shows: [] }));
      let modes = (st.modes || []).map(m => ({ ...m, color: m.own || m.color }));
      const FADES = [[0, 'At once'], [1, '1 second'], [2, '2 seconds'], [5, '5 seconds'], [10, '10 seconds']];
      const draw = () => {
        el.innerHTML = card(`<div class="st-h">Modes</div><p class="hint">Every light one color at full brightness, and the music turned down (in the stream: the speakers keep their volume) or paused.</p>` +
          modes.map((m, i) => `<div class="st-mode" data-i="${i}">
            <div class="st-mode-top"><input class="m-name" value="${esc(m.name)}" aria-label="Name"><button class="m-del" aria-label="Delete ${esc(m.name)}">Delete</button></div>
            <div class="st-mode-grid">
              <label>Music <span class="st-inline">${toggle('class="m-pause"', m.pause)} Pause it</span></label>
              <label ${m.pause ? 'class="dim"' : ''}>Level <span class="st-inline"><input type="range" class="m-music" min="0" max="100" step="5" value="${Math.round(m.music * 100)}" ${m.pause ? 'disabled' : ''}><b class="m-val">${Math.round(m.music * 100)}%</b></span></label>
              <label>Lights fade <select class="m-fade">${FADES.map(([v, t]) => `<option value="${v}" ${Math.abs(m.fade - v) < 0.01 ? 'selected' : ''}>${t}</option>`).join('')}</select></label>
              <label>Ask before turning on <span class="st-inline">${toggle('class="m-confirm"', m.confirm)}</span></label>
            </div>
            <div class="pv-swatches">${swatches(m.color)}</div></div>`).join('') + '<button id="stAddMode">+ Add mode</button>') +
          card(`<div class="st-h">Cues</div><p class="hint">A button that dips the music and plays a show of its own (a doorbell, a countdown), then fades back into the party.</p>` +
          cues.cues.map((c, i) => `<div class="st-mode" data-c="${i}"><div class="st-mode-top"><input class="c-name" value="${esc(c.name)}" aria-label="Cue name"><button class="c-del">Delete</button></div>
            <details><summary>${(c.shows || []).length} show${(c.shows || []).length === 1 ? '' : 's'} (one picked at random each time)</summary><div class="st-shows">${cues.shows.map(s => `<label><input type="checkbox" value="${esc(s.name)}" ${(c.shows || []).includes(s.name) ? 'checked' : ''}> ${esc(s.name)}</label>`).join('')}</div></details></div>`).join('') +
          '<button id="stAddCue">+ Add cue</button>');
        for (const r of el.querySelectorAll('[data-i]')) {
          const m = modes[+r.dataset.i], q = c => r.querySelector(c);
          q('.m-name').onchange = e => { m.name = e.target.value.trim() || m.name; saveModes(); };
          q('.m-pause').onchange = e => { m.pause = e.target.checked; saveModes(); };
          q('.m-music').oninput = e => { q('.m-val').textContent = e.target.value + '%'; };
          q('.m-music').onchange = e => { m.music = +e.target.value / 100; saveModes(); };
          q('.m-fade').onchange = e => { m.fade = +e.target.value; saveModes(); };
          q('.m-confirm').onchange = e => { m.confirm = e.target.checked; saveModes(); };
          for (const sw of r.querySelectorAll('[data-c]')) sw.onclick = () => { m.color = sw.dataset.c; saveModes(); };
          q('.m-del').onclick = () => { if (confirm(`Delete ${m.name}?`)) { modes.splice(+r.dataset.i, 1); saveModes(); } };
        }
        $('#stAddMode').onclick = () => { modes.push({ name: 'New mode', music: 0.2, pause: false, fade: 5, color: '#ffaa5a', confirm: false }); saveModes(); };
        for (const r of el.querySelectorAll('[data-c]')) {
          const c = cues.cues[+r.dataset.c];
          r.querySelector('.c-name').onchange = e => { c.name = e.target.value.trim() || c.name; saveCues(); };
          for (const box of r.querySelectorAll('.st-shows input')) box.onchange = () => { c.shows = [...r.querySelectorAll('.st-shows input:checked')].map(x => x.value); c.show = c.shows[0] || ''; saveCues(false); };
          r.querySelector('.c-del').onclick = () => { if (confirm(`Delete ${c.name}?`)) { cues.cues.splice(+r.dataset.c, 1); saveCues(); } };
        }
        $('#stAddCue').onclick = () => { cues.cues.push({ name: 'New cue', show: '', shows: [] }); saveCues(); };
      };
      const saveModes = async () => { const r = await party('modes', { modes }).catch(() => null); if (r && r.modes) modes = r.modes.map(m => ({ ...m, color: m.own || m.color })); draw(); };
      const saveCues = async (redraw = true) => { const r = await post('/api/cues', { cues: cues.cues }).catch(() => null); if (r && r.cues) cues.cues = r.cues; if (redraw) draw(); };
      draw();
    },
    async addons(el) {
      const [add, bell, ha, st] = await Promise.all([get('/api/addons'), get('/api/doorbell').catch(() => ({})), get('/api/homeassistant').catch(() => ({})), party('state').catch(() => ({}))]);
      const on = id => ((add.addons || []).find(a => a.id === id) || {}).on;
      const extra = {
        doorbell: () => `<div class="st-sub">${row('Address', 'The doorbell on your home network.', `<input id="stBellHost" value="${esc(bell.host || '')}" placeholder="192.168.1.50">`)}
          ${row('Username', '', `<input id="stBellUser" value="${esc(bell.username || '')}" autocomplete="off">`)}
          ${row('Password', bell.has_password ? 'Saved. Type a new one to change it.' : '', '<input id="stBellPass" type="password" autocomplete="new-password">')}
          ${row('It rings', '', `<select id="stBellCue">${(st.cues || []).map(c => `<option value="${esc(c.id)}" ${c.id === bell.cue ? 'selected' : ''}>${esc(c.name)}</option>`).join('')}</select>`)}
          ${row('Listening', bell.live === false ? 'This copy of the studio does not connect to the doorbell.' : bell.connected ? `Connected${bell.name ? ' to ' + esc(bell.name) : ''}` : esc(bell.error || 'Not connected'), toggle('id="stBellOn"', bell.on))}</div>`,
        homeassistant: () => `<div class="st-sub">${row('Home Assistant address', 'Like http://homeassistant.local:8123', `<input id="stHaUrl" value="${esc(ha.url || '')}" placeholder="http://homeassistant.local:8123">`)}
          ${row('Webhook id', 'The webhook your Home Assistant automation listens on.', `<input id="stHaHook" value="${esc(ha.webhook_id || 'light_show_studio')}">`)}
          ${row('Status', ha.error ? esc(ha.error) : ha.connected ? 'Home Assistant is being told' : 'Nothing sent yet', '<button id="stHaTest">Send now</button>')}</div>`,
        bulbshow: () => `<div class="st-sub">${row('Bulb Show', 'Its shows run from Party, and it opens in a window of its own.', '<button id="stBulbOpen">Open Bulb Show</button>')}</div>`,
      };
      el.innerHTML = (add.addons || []).map(a => card(row(esc(a.name), esc(a.on && a.missing ? a.missing : a.about), toggle(`data-addon="${esc(a.id)}"`, a.on)) + (a.on && extra[a.id] ? extra[a.id]() : ''))).join('');
      for (const sw of el.querySelectorAll('[data-addon]')) sw.onchange = async () => { await post('/api/addons', { id: sw.dataset.addon, on: sw.checked }); showSection('addons'); St.showAddons && St.showAddons(); };
      const bellSave = async () => { const body = { on: $('#stBellOn').checked, host: $('#stBellHost').value.trim(), username: $('#stBellUser').value.trim(), cue: $('#stBellCue').value }; const pw = $('#stBellPass').value; if (pw) body.password = pw; await post('/api/doorbell', body); showSection('addons'); };
      if (on('doorbell')) for (const id of ['stBellHost', 'stBellUser', 'stBellPass', 'stBellCue', 'stBellOn']) $('#' + id).onchange = bellSave;
      if (on('homeassistant')) {
        const haSave = test => post('/api/homeassistant', { url: $('#stHaUrl').value.trim(), webhook_id: $('#stHaHook').value.trim(), test }).then(() => showSection('addons'));
        $('#stHaUrl').onchange = () => haSave(false); $('#stHaHook').onchange = () => haSave(false); $('#stHaTest').onclick = () => haSave(true);
      }
      if (on('bulbshow')) $('#stBulbOpen').onclick = () => $('#railBulbs').click();
    },
    async models(el) {
      const draw = async () => {
        const s = await get('/api/models/setup').catch(() => null);
        if (!s) { el.innerHTML = card('<p class="hint">Could not ask about the models.</p>'); return; }
        const pct = Math.round(100 * (s.progress || 0));
        el.innerHTML = card(row(s.installed ? 'Installed' : 'Not installed', s.installed ? `In ${esc(s.where || '')}` : 'Without them the studio uses its own analysis, which works well too.',
            s.state === 'running' ? `<button id="stModelsCancel">Cancel</button>` : `<button id="stModelsGo" class="accent">${s.installed ? 'Install again' : 'Install'}</button>`) +
          row('This computer', esc(s.why || ''), `<span class="st-pill on">${esc((s.variant || 'cpu').toUpperCase())}</span>`) +
          (s.state === 'running' ? `<div class="st-progress"><div style="width:${pct}%"></div></div><p class="hint">${pct}% · ${esc(s.step || s.phase)}</p>` : '') +
          (s.state === 'failed' ? `<p class="warn">Did not install: ${esc(s.error)}</p>` : ''));
        const go = $('#stModelsGo'), stop = $('#stModelsCancel');
        if (go) go.onclick = async () => { await post('/api/models/setup', { action: 'install' }); draw(); };
        if (stop) stop.onclick = async () => { await post('/api/models/setup', { action: 'cancel' }); draw(); };
        if (s.state === 'running' && section === 'models' && view === 'settings') setTimeout(draw, 1500);
      };
      draw();
    },
    async updates(el) {
      const draw = async (u, busy) => {
        el.innerHTML = card(row(u.available ? `Version ${esc(u.latest)} is ready` : 'Up to date', `You have version ${esc(u.version)}.${u.error ? ' Could not reach GitHub just now.' : ''}`,
            u.available && u.can_install ? `<button id="stUpRestart" class="accent">Restart and update</button>`
              : u.available ? `<a href="${esc(u.page)}" target="_blank" rel="noopener"><button class="accent">Download</button></a>`
              : `<button id="stUpCheck" ${busy ? 'disabled' : ''}>${busy ? 'Checking...' : 'Check now'}</button>`) +
          row('Update when the app opens', u.can_install ? 'Off: it only says here that there is a new version.' : 'This copy cannot replace itself where it is. On a Mac, move it to Applications.', toggle(`id="stUpAuto" ${u.can_install ? '' : 'disabled'}`, u.auto_update && u.can_install)));
        $('#stUpAuto').onchange = async e => post('/api/update', { auto_update: e.target.checked });
        if ($('#stUpCheck')) $('#stUpCheck').onclick = async () => { draw(u, true); draw(await post('/api/update', { check: true })); askUpdate(); };
        if ($('#stUpRestart')) $('#stUpRestart').onclick = async () => { await post('/api/update', { restart: true }); St.toast('Restarting to update'); };
      };
      draw(await get('/api/update'));
    },
    async phone(el) {
      const link = await get('/api/party/link');
      el.innerHTML = card(`<div class="st-phone">${link.qr_svg || ''}<div><ol class="st-steps"><li>Connect your phone to the same Wi-Fi as this computer.</li>
        <li>Scan the code with the camera, or open <code>${esc(link.url)}</code> <button id="stCopyLink">Copy</button></li>
        <li>On iPhone: Share › Add to Home Screen. On Android: the menu › Add to Home screen.</li></ol></div></div>`);
      $('#stCopyLink').onclick = async () => { try { await navigator.clipboard.writeText(link.url); St.toast('Copied'); } catch (e) { St.toast('Select the address and copy it', true); } };
    },
  };

  // where the app opens: the view used last, or Settings › Get started for a home not set up yet
  (async () => {
    let v = 'studio'; try { v = localStorage.getItem('lss-view') || 'studio'; } catch (e) { /* the first */ }
    try { const d = await get('/api/setup'); if (!d.done && !localStorage.getItem('lss-setup-seen')) { v = 'settings'; section = 'start'; localStorage.setItem('lss-setup-seen', '1'); } } catch (e) { /* as it was */ }
    const [hv, hs] = location.hash.slice(1).split('/');                 // (#party, #settings/sound: a link to a view)
    if (['studio', 'party', 'settings'].includes(hv)) { v = hv; if (hs) section = hs; }
    showView(['studio', 'party', 'settings'].includes(v) ? v : 'studio');
  })();
  window.Views = { showView, showSection };
})();
