// The desktop app's five places, on the rail, each answering one question:
//   Party     what is happening now?   (start and stop, the song, colors, moments, energy, rooms)
//   Shows     what will the lights do? (every song in one list; the timeline editor opens from it)
//   Home      what do I own?           (the map, rooms, lights, speakers)
//   Add-ons   what else can it do?     (a store of extras, each set up on its own card)
//   Settings  how is the app set up?   (Spotify, the phone, updates, and fine tuning, folded away)
// A control lives where it is used, and only there. Party uses the same server calls as the phone's remote.
(() => {
  const St = window.Studio, { $, esc } = St;
  const get = async url => { const r = await fetch(url); if (!r.ok) throw new Error(r.status); return r.json(); };
  const post = async (url, body) => { const r = await fetch(url, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body || {}) }); return r.json(); };
  const party = (path, body) => body === undefined ? get('/api/party/' + path) : post('/api/party/' + path, body);
  const fmt = t => (t == null || !isFinite(t)) ? '0:00' : `${Math.floor(t / 60)}:${String(Math.floor(t % 60)).padStart(2, '0')}`;
  const COLORS = [['#ffaa5a', 'Warm white', '#ffdcae'], ['#ffffff', 'White'], ['#ff0000', 'Red'], ['#ff6000', 'Orange'], ['#ffbf00', 'Amber'], ['#ffff00', 'Yellow'],
    ['#80ff00', 'Lime'], ['#00ff00', 'Green'], ['#00ff80', 'Mint'], ['#00ffff', 'Cyan'], ['#0080ff', 'Sky blue'], ['#0000ff', 'Blue'], ['#4000ff', 'Indigo'],
    ['#8000ff', 'Purple'], ['#ff00ff', 'Magenta'], ['#ff0080', 'Pink']];
  // the sixteen solid colors a song or a pair picks from (the ones the lights show well)
  const SIXTEEN = ['#ff0000', '#ff4000', '#ff8000', '#ffbf00', '#ffff00', '#80ff00', '#00ff00', '#00ff80', '#00ffff', '#0080ff', '#0000ff', '#4000ff', '#8000ff', '#ff00ff', '#ff0080', '#ffffff'];
  const VIBES = { chill: 'Chill', groove: 'Groove', dance: 'Dance', rave: 'Rave' };
  const shown = c => { const x = COLORS.find(y => y[0] === c); return x ? x[2] || x[0] : c; };
  const ink = c => { const v = parseInt(shown(c).slice(1), 16); return 0.299 * (v >> 16) + 0.587 * ((v >> 8) & 255) + 0.114 * (v & 255) > 150 ? '#1a1206' : '#fff'; };
  const swatches = picked => COLORS.map(([c, n]) => `<button type="button" data-c="${c}" class="${c === picked ? 'on' : ''}" style="background:${shown(c)}" aria-label="${n}" title="${n}"></button>`).join('');
  const NOTE = '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M9 18V6l11-2v12"></path><circle cx="6" cy="18" r="3"></circle><circle cx="17" cy="16" r="3"></circle></svg>';
  const card = inner => `<section class="st-card">${inner}</section>`;
  const row = (title, help, control) => `<div class="st-row"><div class="st-row-t"><b>${title}</b>${help ? `<span>${help}</span>` : ''}</div><div class="st-row-c">${control}</div></div>`;
  const toggle = (attrs, on) => `<label class="pv-switch"><input type="checkbox" ${attrs} ${on ? 'checked' : ''}><i></i></label>`;
  const copy = async text => { try { await navigator.clipboard.writeText(text); St.toast('Copied'); } catch (e) { St.toast('Select the address and copy it', true); } };

  // ------------------------------------------------------------------ switching places
  const VIEWS = { party: '#viewParty', shows: '#viewLibrary', editor: '#viewStudio', home: '#viewHome', addons: '#viewAddons', settings: '#viewSettings' };
  let view = 'party';
  function showView(v) {
    if (!VIEWS[v]) v = 'party';
    view = v;
    for (const [k, id] of Object.entries(VIEWS)) $(id).hidden = k !== v;
    for (const b of document.querySelectorAll('.view-btn[data-view]')) b.classList.toggle('on', b.dataset.view === v || (b.dataset.view === 'shows' && v === 'editor'));
    if (v === 'party') { pollParty(); loadSetup(); loadVolumes(); loadBulbs(); }
    if (v === 'shows') renderLibrary();
    if (v === 'home') renderHome();
    if (v === 'addons') renderAddons();
    if (v === 'settings') renderSettings();
    if (v === 'editor') requestAnimationFrame(() => { if (St.P()) St.draw(); });
    try { localStorage.setItem('lss-view', v === 'editor' ? 'shows' : v); } catch (e) { /* not kept */ }
  }
  for (const b of document.querySelectorAll('.view-btn[data-view]')) b.onclick = () => showView(b.dataset.view);
  $('#btnBackShows').onclick = () => showView('shows');
  // a song opened or dropped anywhere goes to the editor
  $('#fileInput').addEventListener('change', () => showView('editor'));
  window.addEventListener('drop', () => showView('editor'));

  // ------------------------------------------------------------------ Party
  let ps = null, partyTimer = null, clock = null, busy = {}, colorBusy = {};
  async function pollParty() {
    clearTimeout(partyTimer);
    if (view !== 'party') return;
    try {
      const [state, relay] = await Promise.all([party('state'), get('/api/relay/state')]);
      ps = { ...state, relay }; renderParty();
    } catch (e) { $('#pvStatus').textContent = 'Can’t reach the app'; }
    partyTimer = setTimeout(pollParty, 1500);
  }
  function renderParty() {
    const st = ps || {}, r = st.room, relay = st.relay || {};
    const status = $('#pvStatus');
    const lead = Math.round(st.delay || 0);
    status.textContent = !st.running ? 'Not playing' : st.paused ? 'Paused' : st.streaming ? (lead ? `Live, ${lead} s behind Spotify` : 'Live') : 'Getting ready…';
    status.className = 'pv-pill' + (st.running && st.streaming && !st.paused ? ' live' : '');
    renderHeader(st, relay); renderProblems(st, relay); renderNow(st, r); renderColors(st); renderMoments(st); renderQueue(st); renderLevel(st); renderRooms(st);
  }

  // start, stop, and where it plays
  let castNames = [], castLooked = 0;
  async function loadCasts() {
    if (performance.now() - castLooked < 20000 && castLooked) return;
    castLooked = performance.now();
    const d = await get('/api/cast/devices').catch(() => []); castNames = (Array.isArray(d) ? d : d.devices || []).map(x => x.name); headerSig = ''; if (ps) renderHeader(ps, ps.relay || {});
  }
  let headerSig = '';
  function renderHeader(st, relay) {
    loadCasts();
    const last = relay.last || {}, chosen = last.speakers === 'cast' && relay.cast_device ? `cast:${relay.cast_device}` : last.speakers === 'none' ? 'none' : last.speakers === 'cast' ? `cast:${relay.speaker || ''}` : 'pc';
    const signedIn = !!(relay.spotify || {}).signed_in;
    const sig = JSON.stringify([st.running, chosen, castNames, signedIn, String(st.mode || '')]);
    const sel = $('#pvPlayOn');
    if (sig !== headerSig && document.activeElement !== sel) {
      headerSig = sig;
      sel.innerHTML = [...castNames.map(n => `<option value="cast:${esc(n)}">${esc(n)}</option>`), '<option value="pc">This computer</option>', '<option value="none">Nowhere (preview)</option>'].join('');
      sel.value = chosen; if (sel.selectedIndex < 0) sel.value = 'pc';
      sel.disabled = !!st.running;
      const btn = $('#pvStart');
      btn.textContent = st.running ? 'End the party' : 'Start the party';
      btn.classList.toggle('accent', !st.running);
      btn.disabled = !st.running && !signedIn;
      btn.title = !st.running && !signedIn ? 'Connect Spotify in Settings first' : '';
    }
  }
  $('#pvPlayOn').onchange = async e => {
    const v = e.target.value;
    await post('/api/relay/prefs', v.startsWith('cast:') ? { speakers: 'cast', cast_device: v.slice(5) } : { speakers: v }).catch(() => {});
    headerSig = ''; pollParty();
  };
  $('#pvStart').onclick = async () => {
    const running = ps && ps.running, btn = $('#pvStart'); if (running && !confirm('End the party? The music and the lights stop.')) return; btn.disabled = true;
    try {
      if (running) await post('/api/relay/stop', {});
      else {
        await St.modelsReady('the party');
        const relay = (ps && ps.relay) || {}, last = relay.last || {};
        if (last.speakers === 'cast' && relay.cast_device && !(relay.speaker_connected && relay.speaker === relay.cast_device)) {
          const c = await post('/api/cast/connect', { name: relay.cast_device }).catch(e => ({ error: e.message }));
          if (c && c.error) St.toast(`${relay.cast_device}: ${c.error}`, true);
        }
        const r = await post('/api/relay/start', { saved: true });
        if (r && r.error) St.toast(r.error, true);
      }
    } catch (e) { St.toast(e.message, true); }
    headerSig = ''; btn.disabled = false; pollParty();
  };
  function renderProblems(st, relay) {
    const player = relay.player || {}, cap = relay.capture || {}, sp = relay.spotify || {};
    const list = [cap.error && `Recording: ${cap.error}`, relay.output_error && `Playback: ${relay.output_error}`, relay.sync_warning, sp.error && `Spotify: ${sp.error}`,
      relay.auto_error && `It could not start by itself: ${relay.auto_error}`, player.route_error && `Player: ${player.route_error}`, player.error && `Player: ${player.error}`,
      sp.signed_in && !sp.can_stream && 'Sign in to Spotify again: the player needs a permission the earlier sign-in did not ask for.'].filter(Boolean);
    const el = $('#pvProblems'), html = list.map(x => `<div class="warn">${esc(x)}</div>`).join('');
    if (el.dataset.sig !== html) { el.dataset.sig = html; el.innerHTML = html; }
    el.hidden = !list.length;
  }

  // Get started: shows here until the home is ready, then leaves
  async function loadSetup() {
    const d = await get('/api/setup').catch(() => null); if (!d) return;
    const GO = { lights: ['Find lights', () => showHome('lights')], rooms: ['Add rooms', () => showHome('rooms')], map: ['Draw your home', () => showHome('rooms')], speakers: ['Find speakers', () => showHome('speakers')],
      spotify: ['Connect Spotify', () => showView('settings')], sound: ['Sound devices', () => showView('settings')], models: ['See progress', () => showView('settings')], phone: ['Your phone', () => showView('settings')] };
    const todo = (d.items || []).filter(i => !i.done && GO[i.id]);
    const el = $('#pvSetup'); el.hidden = !todo.length;
    el.innerHTML = todo.length ? `<h2>Get started</h2><div class="pv-setup-list">${todo.map(i => `<div class="pv-setup-it"><span><b>${esc(i.title)}</b><span class="hint">${esc(i.detail)}</span></span><button data-go="${i.id}">${GO[i.id][0]}</button></div>`).join('')}</div>` : '';
    for (const b of el.querySelectorAll('[data-go]')) b.onclick = () => GO[b.dataset.go][1]();
  }

  // the song
  let nowSig = '';
  const pairOf = t => { const c = t.colors || []; return [c[0] || '#808080', c[1] || c[0] || '#808080']; };
  function renderNow(st, r) {
    const el = $('#pvNow');
    const sig = JSON.stringify([st.running, r && [r.id, r.playing, r.colors, r.theme_off, r.style, r.genre_found, r.vibe, r.genre]]);
    clock = r && st.running ? { pos: r.position || 0, at: performance.now(), playing: !!r.playing, dur: r.duration || 0 } : null;
    if (sig === nowSig) return tickClock();
    nowSig = sig;
    if (!st.running || !r) {
      el.innerHTML = `<div class="pv-art idle">${NOTE}</div><div class="pv-idle">${st.running ? 'Between songs' : 'Press Start the party, then play something on Spotify. The lights follow every song.'}</div>`;
      return;
    }
    const art = r.art || r.image, [a, b] = pairOf(r);
    el.innerHTML = `<div class="pv-art" ${art ? `style="background-image:url('${esc(art)}')"` : ''}>${art ? '' : NOTE}</div>
      <div class="pv-title">${esc(r.name || r.label)}</div><div class="pv-artist">${esc((r.artists || []).join(', '))}</div>
      <button class="pv-songbtn" id="pvSong" title="This song's colors, vibe and genre">${r.colors ? `<i style="background:${esc(b)}"></i><i style="background:${esc(a)}"></i>` : ''}<span>${esc([r.style && VIBES[r.style], r.genre_found].filter(Boolean).join(', ')) || 'This song'}</span><b>Change</b></button>
      <div class="pv-bar"><div id="pvProg"></div></div><div class="pv-times"><span id="pvPos">0:00</span><span>${fmt(r.duration)}</span></div>
      <div class="pv-controls"><button class="pv-btn" id="pvPrev" aria-label="Previous song">⏮</button>
        <button class="pv-btn big" id="pvPlay" aria-label="${r.playing ? 'Pause' : 'Play'}">${r.playing ? '❚❚' : '▶'}</button>
        <button class="pv-btn" id="pvNext" aria-label="Next song">⏭</button></div>`;
    $('#pvPlay').onclick = () => party('playback', { action: r.playing ? 'pause' : 'resume' }).then(pollParty);
    $('#pvPrev').onclick = () => party('playback', { action: 'previous' }).then(pollParty);
    $('#pvNext').onclick = () => party('playback', { action: 'next' }).then(pollParty);
    $('#pvSong').onclick = () => openSong(r.id);
    tickClock();
  }
  function tickClock() {
    const p = $('#pvProg'), pos = $('#pvPos'); if (!clock || !p) return;
    const t = Math.min(clock.dur || 1e9, clock.pos + (clock.playing ? (performance.now() - clock.at) / 1000 : 0));
    p.style.width = clock.dur ? `${Math.min(100, 100 * t / clock.dur)}%` : '0'; pos.textContent = fmt(t);
  }
  setInterval(() => { if (view === 'party') tickClock(); }, 250);

  // a sheet over the app, for what is opened from a card
  function openSheet(title, fill) {
    const sheet = $('#sheet'), body = $('#sheetBody');
    $('#sheetTitle').textContent = title; body.innerHTML = ''; sheet.hidden = false; fill(body);
  }
  const closeSheet = () => { $('#sheet').hidden = true; $('#sheetBody').innerHTML = ''; };
  $('#sheetClose').onclick = closeSheet;
  $('#sheet').addEventListener('mousedown', e => { if (e.target === $('#sheet')) closeSheet(); });
  document.addEventListener('keydown', e => { if (e.key === 'Escape' && !$('#sheet').hidden) closeSheet(); });
  const grid = (cur, label) => `<div class="sheet-colors" data-k="${label}">${SIXTEEN.map(c => `<button type="button" data-c="${c}" class="${c === cur ? 'on' : ''}" style="background:${c}" aria-label="${label} ${c}"></button>`).join('')}</div>`;

  // one song's own colors, vibe and genre. The stored secondary shows most, so it is the one called Primary.
  function openSong(id) {
    const find = () => ps && [ps.room, ...(ps.upcoming || [])].find(x => x && x.id === id);
    const t0 = find(); if (!t0) return;
    openSheet(t0.name || t0.label || 'This song', body => {
      const draw = () => {
        const t = find() || t0, [a0, b0] = pairOf(t), cur = { a: a0, b: b0 }, wild = (ps || {}).color_source === 'wild';
        const choice = (key, on, list) => `<div class="sheet-choices" data-group="${key}">${list.map(([k, label]) => `<button data-v="${esc(k)}" class="${(on || '') === k ? 'on' : ''}">${esc(label)}</button>`).join('')}</div>`;
        const genres = [['', t.genre_found ? `Auto: ${t.genre_found.replace(/ music$/, '')}` : 'Auto'], ...((ps && ps.genre_choices) || []).map(g => [g.key, g.label])];
        const vibes = [['', t.style ? `Auto: ${VIBES[t.style] || t.style}` : 'Auto'], ...Object.entries(VIBES)];
        body.innerHTML = `${t.kind === 'custom' ? '<p class="hint">You made this show, so it keeps its own colors.</p>' : `
          <h4>Colors</h4>${choice('theme', t.theme_off ? 'cover' : 'party', [['party', wild ? 'No limits' : 'Your colors'], ['cover', 'Album art']])}
          ${t.colors ? `<h4>Primary, seen most</h4>${grid(b0, 'Primary')}<h4>Secondary</h4>${grid(a0, 'Secondary')}
          <label class="sheet-check"><input type="checkbox" id="sheetOnly" ${t.only ? 'checked' : ''}> Just these two colors</label>` : ''}
          ${t.own ? '<button id="sheetBack">Back to tonight’s colors</button>' : ''}`}
          <h4>Vibe</h4>${choice('vibe', t.vibe, vibes)}<h4>Genre</h4>${choice('genre', t.genre, genres)}`;
        const saveColors = () => party('colors', { track_id: id, primary: cur.a, secondary: cur.b, only: !!(($('#sheetOnly') || {}).checked) }).then(st => { ps = { ...st, relay: (ps || {}).relay }; renderParty(); });
        for (const g of body.querySelectorAll('.sheet-colors')) for (const sw of g.querySelectorAll('[data-c]')) sw.onclick = () => {
          cur[g.dataset.k === 'Primary' ? 'b' : 'a'] = sw.dataset.c;
          for (const o of g.querySelectorAll('.on')) o.classList.remove('on');
          sw.classList.add('on'); saveColors();
        };
        const only = $('#sheetOnly'); if (only) only.onchange = saveColors;
        const back = $('#sheetBack'); if (back) back.onclick = async () => { const st = await party('colors', { track_id: id }); ps = { ...st, relay: (ps || {}).relay }; renderParty(); draw(); };
        for (const b of body.querySelectorAll('[data-group] button')) b.onclick = async () => {
          const group = b.closest('[data-group]').dataset.group, v = b.dataset.v;
          const st = group === 'theme' ? await party('colors', v === 'cover' ? { track_id: id, theme: false } : { track_id: id })
            : group === 'vibe' ? await party('style', { track_id: id, style: v || null }) : await party('genre', { track_id: id, genre: v || null });
          ps = { ...st, relay: (ps || {}).relay }; renderParty(); draw();
        };
      };
      draw();
    });
  }

  // colors for the whole night
  let colorSig = '';
  const theme = body => party('theme', body).then(st => { ps = { ...st, relay: (ps || {}).relay }; colorSig = ''; renderColors(ps); }).catch(() => {});
  function renderColors(st) {
    const pc = st.party_colors || {}, mode = pc.mode || st.color_mode, source = st.color_source || (st.theme_on === false ? 'album' : 'party'), on = source === 'party';
    const jc = (st.relay || {}).color_settings || {}, pairs = mode === 'mix' ? (pc.pairs || []) : [[pc.primary, pc.secondary]];
    const sig = JSON.stringify([source, mode, pairs, pc.set, pc.edited, pc.sets, jc.rainbow, jc.extras]);
    const box = $('#pvColors');
    if (sig === colorSig || box.contains(document.activeElement) && document.activeElement.tagName === 'SELECT') return;
    colorSig = sig;
    box.innerHTML = `<div class="pv-seg" role="group" aria-label="Colors tonight">${[['album', 'Album art'], ['party', 'Your colors'], ['wild', 'No limits']].map(([k, n]) => `<button data-s="${k}" class="${source === k ? 'on' : ''}">${n}</button>`).join('')}</div>
      <p class="hint">${{ album: 'Every song in its album cover’s colors.', wild: 'The show picks colors part by part: a breakdown all blue, a red drop, a rainbow.' }[source] || (mode === 'mix' ? 'Each song gets one of these pairs, by its vibe.' : 'Every song in these two colors.')}</p>
      ${on ? `<div class="pv-seg small" role="group" aria-label="How songs get the colors"><button data-m="pick" class="${mode === 'pick' ? 'on' : ''}">Two colors</button><button data-m="mix" class="${mode === 'mix' ? 'on' : ''}">A pair each song</button></div>
      <div class="pv-pairs">${pairs.map((p, i) => `<span class="pv-pair-ed" data-i="${i}"><button class="pv-sw" style="background:${esc(p[1])}" aria-label="Primary color"></button><button class="pv-sw" style="background:${esc(p[0])}" aria-label="Secondary color"></button>${mode === 'mix' && pairs.length > 1 ? '<button class="rm" aria-label="Remove this pair">&times;</button>' : ''}</span>`).join('')}
        ${mode === 'mix' && pairs.length < 8 ? '<button id="pvPairAdd" class="pv-chip">+ Pair</button>' : ''}</div>
      <div class="pv-sets"><select id="pvSet" aria-label="Color set"><option value="">${pc.set ? 'Choose a set…' : 'Not saved as a set'}</option>${(pc.sets || []).map(n => `<option value="${esc(n)}" ${n === pc.set ? 'selected' : ''}>${esc(n)}</option>`).join('')}</select>
        ${pc.set ? `<button id="pvSetSave" ${pc.edited ? '' : 'disabled'}>Save</button>` : ''}<button id="pvSetAs">Save as…</button>${pc.set ? '<button id="pvSetDel">Delete</button>' : ''}</div>
      <label class="sheet-check" title="A few of the small accents take two more colors now and then, picked for each song from its own two"><input type="checkbox" id="pvExtras" ${jc.extras ? 'checked' : ''}> Now and then, two more colors</label>
      ${mode === 'mix' ? `<label class="sheet-check" title="A song that goes all out gets rainbow drops"><input type="checkbox" id="pvRainbow" ${jc.rainbow ? 'checked' : ''}> Rainbow for epic songs</label>` : ''}` : ''}`;
    for (const b of box.querySelectorAll('[data-s]')) b.onclick = () => theme({ source: b.dataset.s });
    if (!on) return;
    for (const b of box.querySelectorAll('[data-m]')) b.onclick = () => { if (!b.classList.contains('on')) theme({ mode: b.dataset.m }); };
    for (const el of box.querySelectorAll('.pv-pair-ed')) {
      const i = +el.dataset.i;
      for (const sw of el.querySelectorAll('.pv-sw')) sw.onclick = () => openSheet(mode === 'mix' ? `Pair ${i + 1}` : 'Your colors', body => {
        const cur = { a: pairs[i][0], b: pairs[i][1] };
        body.innerHTML = `<h4>Primary, seen most</h4>${grid(cur.b, 'Primary')}<h4>Secondary</h4>${grid(cur.a, 'Secondary')}`;
        for (const g of body.querySelectorAll('.sheet-colors')) for (const c of g.querySelectorAll('[data-c]')) c.onclick = () => {
          cur[g.dataset.k === 'Primary' ? 'b' : 'a'] = c.dataset.c;
          for (const o of g.querySelectorAll('.on')) o.classList.remove('on');
          c.classList.add('on');
          theme(mode === 'mix' ? { pairs: pairs.map((p, j) => j === i ? [cur.a, cur.b] : p) } : { primary: cur.a, secondary: cur.b });
        };
      });
      const rm = el.querySelector('.rm'); if (rm) rm.onclick = () => theme({ pairs: pairs.filter((_, j) => j !== i) });
    }
    const add = $('#pvPairAdd'); if (add) add.onclick = () => theme({ pairs: [...pairs, ['#ff0000', '#0040ff']] });
    $('#pvSet').onchange = e => { if (e.target.value) theme({ use: e.target.value }); };
    const save = $('#pvSetSave'); if (save) save.onclick = () => theme({ save: true });
    $('#pvSetAs').onclick = () => { const n = (prompt('Name this set of colors', pc.set || '') || '').trim(); if (n) theme({ save_as: n }); };
    const del = $('#pvSetDel'); if (del) del.onclick = () => { if (confirm(`Delete the set “${pc.set}”? The colors stay as they are.`)) theme({ delete: pc.set }); };
    const partyColors = body => post('/api/relay/colors', body).then(() => { colorSig = ''; pollParty(); });
    $('#pvExtras').onchange = e => partyColors({ extras: e.target.checked });
    const rb = $('#pvRainbow'); if (rb) rb.onchange = e => partyColors({ rainbow: e.target.checked });
  }

  // moments: one press
  let momentsSig = '';
  function renderMoments(st) {
    const color_moments = st.color_moments || [], show_moments = (st.show_moments || []).filter(c => c.id !== st.doorbell_show_moment || (st.show_moment && st.show_moment.id === c.id && st.show_moment.phase !== 'done'));
    const sig = JSON.stringify([color_moments.map(m => [m.id, m.name]), show_moments.map(c => [c.id, c.name])]);
    if (sig !== momentsSig) {
      momentsSig = sig;
      $('#pvMoments').innerHTML = (color_moments.map(m => `<button class="pv-moment" data-mode="${esc(m.id)}" aria-pressed="false">${esc(m.name)}<span class="sub"></span></button>`).join('') +
        show_moments.map(c => `<button class="pv-moment show_moment" data-show_moment="${esc(c.id)}">${esc(c.name)}<span class="sub"></span></button>`).join('')) || '<div class="pv-empty">No moments yet.</div>';
      for (const b of $('#pvMoments').querySelectorAll('[data-mode]')) b.onclick = () => {
        const m = ((ps && ps.color_moments) || []).find(x => x.id === b.dataset.mode); if (!m) return;
        const on = !m.on;
        if (on && m.confirm && !(confirm(`Turn on ${m.name}?`) && confirm(`Are you sure? ${m.name} starts now.`))) return;
        busy[m.id] = performance.now() + 4000; m.on = on; if (on) m.color = m.own || m.color; paintMoments(ps);
        party('color_moment', { id: m.id, on }).then(pollParty).catch(() => { busy[m.id] = 0; });
      };
      for (const b of $('#pvMoments').querySelectorAll('[data-show_moment]')) b.onclick = async () => {
        const c = ((ps && ps.show_moments) || []).find(x => x.id === b.dataset.show_moment);
        const running = ps && ps.show_moment && ps.show_moment.id === b.dataset.show_moment && !['done', 'stopped'].includes(ps.show_moment.phase);
        if (!running && c && !(c.shows || []).length && !c.show) { openMoments(); St.toast(`Pick a show for ${c.name} first`, true); return; }
        await post(running ? '/api/show_moments/stop' : '/api/show_moments/run', { id: b.dataset.show_moment }).catch(() => {});
        pollParty();
      };
    }
    paintMoments(st);
  }
  function paintMoments(st) {
    for (const m of st.color_moments || []) {
      const b = $('#pvMoments').querySelector(`[data-mode="${CSS.escape(m.id)}"]`); if (!b) continue;
      if (performance.now() > (busy[m.id] || 0)) b.setAttribute('aria-pressed', m.on ? 'true' : 'false');
      b.style.setProperty('--on', shown(m.color)); b.style.setProperty('--on-text', ink(m.color));
      b.querySelector('.sub').textContent = b.getAttribute('aria-pressed') === 'true' ? 'On' : m.pause ? 'Pauses the music' : `Music ${Math.round(m.music * 100)}%`;
    }
    for (const b of $('#pvMoments').querySelectorAll('[data-show_moment]')) {
      const c = st.show_moment, run = c && c.id === b.dataset.show_moment && !['done', 'stopped'].includes(c.phase);
      b.classList.toggle('running', !!run); b.querySelector('.sub').textContent = run ? 'Playing, press to stop' : 'Plays a show';
    }
    const on = (st.color_moments || []).filter(m => m.on), box = $('#pvModeColors'), sig = on.map(m => m.id + m.color).join();
    if (box.dataset.sig === sig || on.some(m => performance.now() < (colorBusy[m.id] || 0))) return;
    box.dataset.sig = sig;
    box.innerHTML = on.map(m => `<div class="pv-modecolors" data-for="${esc(m.id)}"><div class="hint">${esc(m.name)}: its color</div><div class="pv-swatches">${swatches(m.color)}</div></div>`).join('');
    for (const g of box.querySelectorAll('[data-for]')) for (const sw of g.querySelectorAll('[data-c]')) sw.onclick = () => {
      colorBusy[g.dataset.for] = performance.now() + 4000;
      for (const x of g.querySelectorAll('[data-c]')) x.classList.toggle('on', x === sw);
      party('color_moment', { id: g.dataset.for, color: sw.dataset.c }).catch(() => { colorBusy[g.dataset.for] = 0; });
    };
  }
  $('#pvEditMoments').onclick = () => openMoments();

  // up next
  let queueSig = '';
  function renderQueue(st) {
    const up = st.upcoming || [], sig = JSON.stringify([st.running, up.map(t => [t.id, t.kind, t.learning, t.colors])]);
    const learning = up.filter(t => t.kind === 'live').length;
    $('#pvQueueNote').textContent = up.length ? `${up.length - learning} ready${learning ? `, ${learning} learning` : ''}` : '';
    if (sig === queueSig) return;
    queueSig = sig;
    $('#pvQueue').innerHTML = !st.running ? '<div class="pv-empty">The queue shows here once the party starts.</div>' : !up.length ? '<div class="pv-empty">Nothing queued.</div>'
      : up.map(t => { const learned = t.kind !== 'live', [a, b] = pairOf(t);
          return `<button class="pv-track" data-song="${esc(t.id)}"><div class="pv-art small" ${t.image ? `style="background-image:url('${esc(t.image)}')"` : ''}>${t.image ? '' : NOTE}</div>
            <div class="pv-track-t"><div class="t">${esc(t.name || t.label)}</div><div class="a">${esc((t.artists || []).join(', '))}</div></div>
            <span class="pv-tag ${learned ? 'ok' : 'wait'}">${learned ? 'Ready' : 'Learning'}</span>
            ${t.colors ? `<span class="pv-pair"><i style="background:${esc(b)}"></i><i style="background:${esc(a)}"></i></span>` : ''}</button>`; }).join('');
    for (const b of $('#pvQueue').querySelectorAll('[data-song]')) b.onclick = () => openSong(b.dataset.song);
  }

  // energy
  let levelDrag = false;
  function renderLevel(st) {
    const v = Math.round((st.party_energy ?? 1) * 100), s = $('#pvLevel');
    if (!levelDrag && document.activeElement !== s) s.value = v;
    $('#pvLevelVal').textContent = (levelDrag ? s.value : v) + '%';
    const ramp = st.energy_ramp;
    $('#pvRamp').textContent = ramp ? 'Stop the build-up' : 'Build to 100% over an hour';
    $('#pvRamp').hidden = !ramp && v >= 100;
    $('#pvRampNote').textContent = ramp && ramp.minutes ? `${Math.round(ramp.minutes)} min to go` : '';
  }
  $('#pvLevel').oninput = e => { levelDrag = true; $('#pvLevelVal').textContent = e.target.value + '%'; };
  $('#pvLevel').onchange = e => { party('energy', { level: +e.target.value / 100 }).finally(() => { setTimeout(() => { levelDrag = false; }, 800); }); };
  $('#pvRamp').onclick = () => party('energy_ramp', { on: !(ps && ps.energy_ramp) }).then(pollParty);

  // rooms: on or off, and how loud. The only place for either.
  let roomsSig = '', roomBusy = 0, volumes = { devices: [] }, volHeld = false, volTouched = 0;
  const mixOpen = new Set();
  async function loadVolumes() {
    const v = await get('/api/party/volumes').catch(() => null); if (!v || v.error) return;
    volumes = v; if (ps) renderRooms(ps);
  }
  setInterval(() => { if (view === 'party' && !document.hidden && !volHeld && performance.now() - volTouched > 4000) loadVolumes(); }, 5000);
  document.addEventListener('pointerup', () => { volHeld = false; }, true);
  function renderRooms(st) {
    const rooms = st.rooms || [], devs = volumes.devices || [], placed = new Set(rooms.flatMap(r => r.speakers || []));
    const loose = devs.filter(d => !placed.has(d.name));
    const sig = JSON.stringify([rooms.map(r => [r.id, r.name, r.on, r.speakers]), devs.map(d => [d.name, d.volume, d.muted]), [...mixOpen]]);
    const box = $('#pvRooms');
    if (sig === roomsSig || volHeld || performance.now() < roomBusy || performance.now() - volTouched < 3000) return;
    roomsSig = sig;
    const pct = v => v == null ? '–' : Math.round(v * 100) + '%';
    const speaker = d => `<div class="pv-vol"><span>${esc(d.name)}${d.muted ? ' <i class="hint">muted</i>' : ''}</span><input type="range" min="0" max="100" value="${d.volume == null ? 0 : Math.round(d.volume * 100)}" data-speaker="${esc(d.name)}" ${d.volume == null ? 'disabled' : ''} aria-label="${esc(d.name)} volume"><b>${pct(d.volume)}</b></div>`;
    box.innerHTML = (rooms.length ? rooms.map(r => {
      const mine = devs.filter(d => (r.speakers || []).includes(d.name)), known = mine.filter(d => d.volume != null), top = known.length ? Math.max(...known.map(d => d.volume)) : null;
      return `<div class="pv-room ${r.on ? '' : 'off'}" data-room="${esc(r.id)}"><div class="pv-room-top"><b>${esc(r.name)}</b>
          ${mine.length ? `<input type="range" min="0" max="100" value="${Math.round((top || 0) * 100)}" data-roomvol ${top == null ? 'disabled' : ''} aria-label="${esc(r.name)} volume"><span class="pv-volval">${pct(top)}</span>` : '<span class="grow hint">No speakers</span>'}
          ${toggle(`data-id="${esc(r.id)}" aria-label="${esc(r.name)} in the party"`, r.on)}</div>
        ${mixOpen.has(r.id) && mine.length > 1 ? `<div class="pv-mix">${mine.map(speaker).join('')}</div>` : ''}
        ${mine.length > 1 ? `<button class="pv-link" data-mix="${esc(r.id)}">${mixOpen.has(r.id) ? 'Hide each speaker' : 'Each speaker’s volume'}</button>` : ''}</div>`; }).join('')
      : '<div class="pv-empty">No rooms yet: <button class="pv-link" id="pvGoRooms">add them in Home</button></div>') +
      (loose.length ? `<div class="pv-room"><div class="hint">Not in a room</div>${loose.map(speaker).join('')}</div>` : '');
    for (const sw of box.querySelectorAll('input[data-id]')) sw.onchange = async () => { roomBusy = performance.now() + 6000; await post('/api/rooms/on', { id: sw.dataset.id, on: sw.checked }).catch(() => {}); };
    // one step when the slider is let go, not a stream of changes: Google speakers chime at each one
    for (const r of box.querySelectorAll('[data-speaker]')) {
      r.onpointerdown = () => { volHeld = true; volTouched = performance.now(); };
      r.oninput = () => { r.parentElement.querySelector('b').textContent = r.value + '%'; };
      r.onchange = () => { volTouched = performance.now(); const d = devs.find(x => x.name === r.dataset.speaker); if (d) d.volume = +r.value / 100; party('volume', { name: r.dataset.speaker, level: +r.value / 100 }).catch(() => {}); };
    }
    for (const r of box.querySelectorAll('[data-roomvol]')) {
      const room = r.closest('[data-room]'), rm = rooms.find(x => x.id === room.dataset.room), mine = devs.filter(d => (rm.speakers || []).includes(d.name));
      r.onpointerdown = () => { volHeld = true; volTouched = performance.now(); };
      r.oninput = () => { room.querySelector('.pv-volval').textContent = r.value + '%'; };
      r.onchange = () => {   // the room is as loud as its loudest speaker, and the rest keep their mix
        volTouched = performance.now();
        const top = Math.max(...mine.map(d => d.volume || 0)), v = +r.value / 100;
        const levels = Object.fromEntries(mine.map(d => [d.name, top > 0 ? (d.volume || 0) * v / top : v]));
        for (const d of mine) d.volume = levels[d.name];
        party('volume', { levels }).catch(() => {});
      };
    }
    for (const b of box.querySelectorAll('[data-mix]')) b.onclick = () => { const id = b.dataset.mix; mixOpen.has(id) ? mixOpen.delete(id) : mixOpen.add(id); roomsSig = ''; renderRooms(ps); };
    const go = $('#pvGoRooms'); if (go) go.onclick = () => showHome('rooms');
  }

  // Bulb Studio's shows, when that add-on is on
  async function loadBulbs() {
    const d = await party('bulbstudio').catch(() => null), el = $('#pvBulbs');
    const shows = d && d.shows || []; el.hidden = !(d && d.open !== undefined && shows.length);
    if (el.hidden) return;
    $('#pvBulbList').innerHTML = shows.map(s => `<label class="pv-room-top"><b>${esc(s.name)}</b><span class="grow"></span>${toggle(`data-bulb="${esc(s.id)}" aria-label="${esc(s.name)}"`, s.running || s.on)}</label>`).join('');
    for (const sw of el.querySelectorAll('[data-bulb]')) sw.onchange = async () => { const r = await party('bulbstudio', { id: sw.dataset.bulb, on: sw.checked }).catch(() => null); if (r && !r.ok) St.toast(r.error || 'It didn’t start', true); loadBulbs(); };
  }

  // editing moments, in a sheet over the Party screen
  function openMoments() {
    openSheet('Moments', async el => {
      const st = await party('state').catch(() => ({})), show_moments = await get('/api/show_moments').catch(() => ({ show_moments: [], shows: [] }));
      let color_moments = (st.color_moments || []).map(m => ({ ...m, color: m.own || m.color }));
      const FADES = [[0, 'At once'], [1, '1 second'], [2, '2 seconds'], [5, '5 seconds'], [10, '10 seconds']];
      const draw = () => {
        el.innerHTML = `<h4>Hold a color</h4><p class="hint">Every light one color at full brightness, and the music turned down or paused.</p>` +
          color_moments.map((m, i) => `<div class="st-mode" data-i="${i}">
            <div class="st-mode-top"><input class="m-name" value="${esc(m.name)}" aria-label="Name"><button class="m-del" aria-label="Delete ${esc(m.name)}">Delete</button></div>
            <div class="st-mode-grid">
              <label>Music <span class="st-inline">${toggle('class="m-pause"', m.pause)} Pause it</span></label>
              <label ${m.pause ? 'class="dim"' : ''}>Level <span class="st-inline"><input type="range" class="m-music" min="0" max="100" step="5" value="${Math.round(m.music * 100)}" ${m.pause ? 'disabled' : ''}><b class="m-val">${Math.round(m.music * 100)}%</b></span></label>
              <label>Lights fade <select class="m-fade">${FADES.map(([v, t]) => `<option value="${v}" ${Math.abs(m.fade - v) < 0.01 ? 'selected' : ''}>${t}</option>`).join('')}</select></label>
              <label>Ask before turning on <span class="st-inline">${toggle('class="m-confirm"', m.confirm)}</span></label>
            </div>
            <div class="pv-swatches">${swatches(m.color)}</div></div>`).join('') + '<button id="stAddColorMoment">+ Add a moment</button>' +
          `<h4>Play a show</h4><p class="hint">The music dips and a show of yours plays, then it fades back into the party.</p>` +
          show_moments.show_moments.map((c, i) => `<div class="st-mode" data-show_moment-row="${i}"><div class="st-mode-top"><input class="c-name" value="${esc(c.name)}" aria-label="Moment name"><button class="c-del">Delete</button></div>
            <p class="hint">${(c.shows || []).length > 1 ? 'One picked at random each time:' : 'Plays:'}</p><div class="st-shows">${show_moments.shows.filter(s => !s.name.startsWith('_')).map(s => `<label><input type="checkbox" value="${esc(s.name)}" ${(c.shows || []).includes(s.name) ? 'checked' : ''}> ${esc(St.niceName(s.name))}</label>`).join('') || '<span class="hint">Make a show in Shows first.</span>'}</div></div>`).join('') +
          '<button id="stAddShowMoment">+ Add a moment</button>';
        for (const r of el.querySelectorAll('[data-i]')) {
          const m = color_moments[+r.dataset.i], q = c => r.querySelector(c);
          q('.m-name').onchange = e => { m.name = e.target.value.trim() || m.name; saveColorMoments(); };
          q('.m-pause').onchange = e => { m.pause = e.target.checked; saveColorMoments(); };
          q('.m-music').oninput = e => { q('.m-val').textContent = e.target.value + '%'; };
          q('.m-music').onchange = e => { m.music = +e.target.value / 100; saveColorMoments(); };
          q('.m-fade').onchange = e => { m.fade = +e.target.value; saveColorMoments(); };
          q('.m-confirm').onchange = e => { m.confirm = e.target.checked; saveColorMoments(); };
          for (const sw of r.querySelectorAll('[data-c]')) sw.onclick = () => { m.color = sw.dataset.c; saveColorMoments(); };
          q('.m-del').onclick = () => { if (confirm(`Delete ${m.name}?`)) { color_moments.splice(+r.dataset.i, 1); saveColorMoments(); } };
        }
        $('#stAddColorMoment').onclick = () => { color_moments.push({ name: 'New moment', music: 0.2, pause: false, fade: 5, color: '#ffaa5a', confirm: false }); saveColorMoments(); };
        for (const r of el.querySelectorAll('[data-show_moment-row]')) {
          const c = show_moments.show_moments[+r.dataset.showMomentRow];
          r.querySelector('.c-name').onchange = e => { c.name = e.target.value.trim() || c.name; saveShowMoments(); };
          for (const box of r.querySelectorAll('.st-shows input')) box.onchange = () => { c.shows = [...r.querySelectorAll('.st-shows input:checked')].map(x => x.value); c.show = c.shows[0] || ''; saveShowMoments(false); };
          r.querySelector('.c-del').onclick = () => { if (confirm(`Delete ${c.name}?`)) { show_moments.show_moments.splice(+r.dataset.showMomentRow, 1); saveShowMoments(); } };
        }
        $('#stAddShowMoment').onclick = () => { show_moments.show_moments.push({ name: 'New moment', show: '', shows: [] }); saveShowMoments(); };
      };
      const saveColorMoments = async () => { const r = await party('color_moments', { color_moments }).catch(() => null); if (r && r.color_moments) color_moments = r.color_moments.map(m => ({ ...m, color: m.own || m.color })); momentsSig = ''; draw(); };
      const saveShowMoments = async (redraw = true) => { const r = await post('/api/show_moments', { show_moments: show_moments.show_moments }).catch(() => null); if (r && r.show_moments) show_moments.show_moments = r.show_moments; momentsSig = ''; if (redraw) draw(); };
      draw();
    });
  }

  // ------------------------------------------------------------------ Shows: every song in one list
  let libFilter = 'all', songs = [];
  async function renderLibrary() {
    const open = St.P() && $('#emptyState').hidden;
    const cur = $('#libContinue'); cur.hidden = !open; if (open) cur.textContent = `Back to ${$('#projectName').value || 'the editor'}`;
    if (!songs.length) $('#libGrid').innerHTML = '<div class="pv-empty">Finding your songs…</div>';
    const [list, every] = await Promise.all([get('/api/projects').catch(() => []), get('/api/review/every').then(d => d.songs || []).catch(() => [])]);
    const seen = new Set(); songs = [];
    for (const p of list) { seen.add(p.name); if (!p.name.startsWith('_')) songs.push({ name: p.name, label: St.niceName(p.name), kind: p.has_project ? 'mine' : 'song', length: p.length }); }
    for (const s of every) if (!s.show && !seen.has(s.label)) songs.push({ name: s.label, label: St.niceName(s.label), kind: 'auto', reviewed: s.reviewed });
    drawLibrary();
  }
  function drawLibrary() {
    const q = $('#libSearch').value.trim().toLowerCase();
    const TAG = { mine: ['Made by you', 'mine'], song: ['Song only', 'auto'], auto: ['Made by the app', 'auto'] };
    const show = songs.filter(s => (!q || s.label.toLowerCase().includes(q)) && (libFilter === 'all' || (libFilter === 'mine' && s.kind === 'mine') || (libFilter === 'auto' && s.kind !== 'mine') || (libFilter === 'review' && s.kind === 'auto' && !s.reviewed)));
    const hue = s => { let h = 0; for (const c of s) h = (h * 31 + c.charCodeAt(0)) % 360; return h; };
    const FIRST = 150, more = show.length - FIRST;
    $('#libMore').textContent = more > 0 ? `Showing ${FIRST} of ${show.length}. Search to find the rest.` : '';
    $('#libGrid').innerHTML = show.slice(0, FIRST).map(s => { const [text, cls] = s.kind === 'auto' && !s.reviewed ? ['Needs a review', 'need'] : TAG[s.kind], h = hue(s.label);
      return `<button class="lib-song" data-name="${esc(s.name)}"><span class="lib-art" style="background:linear-gradient(135deg,hsl(${h} 70% 42%),hsl(${(h + 50) % 360} 80% 58%))" aria-hidden="true"></span><span class="lib-t">${esc(s.label)}</span><span class="lib-tag ${cls}">${text}</span></button>`; }).join('')
      || `<div class="pv-empty">${songs.length ? 'No songs match.' : 'No songs yet. Add one, or start a party and the app learns each song as it plays.'}</div>`;
    for (const b of $('#libGrid').querySelectorAll('[data-name]')) b.onclick = async () => {
      await St.refreshRecent();
      showView('editor');
      if (!await St.openNamed(b.dataset.name)) St.toast('That song could not be opened', true);
    };
    for (const f of document.querySelectorAll('#libFilters button')) f.classList.toggle('on', f.dataset.f === libFilter);
  }
  $('#libSearch').oninput = drawLibrary;
  for (const f of document.querySelectorAll('#libFilters button')) f.onclick = () => { libFilter = f.dataset.f; drawLibrary(); };
  $('#libAdd').onclick = () => $('#fileInput').click();
  $('#libReview').onclick = () => { showView('editor'); St.reviewStart(); };
  $('#libContinue').onclick = () => showView('editor');
  // songs learned ahead of the party, silently: a playlist's, or the queue's
  $('#libLearn').onclick = () => openSheet('Learn a playlist', body => {
    const draw = async () => {
      const st = await party('state').catch(() => ({})), p = st.playlist_learning || {}, on = !!p.running;
      body.innerHTML = `<p class="hint">The app studies these songs ahead of the party, silently, so each has its full show from the first beat. It takes as long as the songs do.</p>
        <input id="preLink" placeholder="Spotify playlist link" autocomplete="off" aria-label="Spotify playlist link" ${on ? 'disabled' : ''}>
        <div class="store-actions">${on ? '<button id="preStop">Stop</button>' : '<button id="preGo" class="accent">Learn this playlist</button><button id="preQueue">Learn the queue</button>'}</div>
        <p class="hint" id="preState">${esc(p.status || p.error || '')}</p>`;
      const start = source => post('/api/playlist_learning', { source }).then(r => { if (r && r.error) St.toast(r.error, true); draw(); });
      const go = $('#preGo'); if (go) go.onclick = () => { const link = $('#preLink').value.trim(); if (link) start(link); else St.toast('Paste a Spotify playlist link first', true); };
      const queue = $('#preQueue'); if (queue) queue.onclick = () => start('queue');
      const stop = $('#preStop'); if (stop) stop.onclick = () => post('/api/playlist_learning/stop', {}).then(draw);
    };
    draw();
  });

  // ------------------------------------------------------------------ Home: the map, and what is in it
  let homeTab = 'lights';
  function showHome(tab) { homeTab = tab; showView('home'); }
  async function renderHome() {
    for (const b of document.querySelectorAll('#homeTabs button')) b.classList.toggle('on', b.dataset.t === homeTab);
    const el = $('#homePane');
    for (const id of ['#lightList', '#roomsPop']) { const n = $(id); if (el.contains(n)) $('#parked').appendChild(n); }
    el.innerHTML = '';
    if (homeTab === 'lights') {
      const hue = await get('/api/hue').catch(() => null);
      el.innerHTML = `<div id="homeLights"></div><div id="homeHue"></div><p class="hint">Not seeing a Govee light? Turn on LAN Control for it in the Govee Home app.</p>`;
      $('#homeLights').appendChild($('#lightList')); St.renderLights();
      const bridges = (hue && hue.bridges) || [];
      $('#homeHue').innerHTML = bridges.map(b => `<div class="home-hue"><span><b>${esc(b.name || 'Philips Hue bridge')}</b><span class="hint">${b.paired ? 'Connected' : 'Found on your network'}</span></span>${b.paired ? '' : `<button data-pair="${esc(b.id)}" class="accent">Connect</button>`}</div>`).join('');
      for (const b of $('#homeHue').querySelectorAll('[data-pair]')) b.onclick = async () => { await post('/api/hue/pair', { bridge: b.dataset.pair }); b.textContent = 'Press the bridge’s button now…'; b.disabled = true; };
    } else if (homeTab === 'rooms') {
      el.appendChild($('#roomsPop')); $('#roomsPop').classList.add('in-home'); St.openRooms();
    } else {
      const s = await get('/api/settings').catch(() => ({})), offs = s.cast_offsets || {};
      el.innerHTML = '<div id="homeCasts" class="hint">Looking for speakers…</div>';
      const d = await get('/api/cast/devices').catch(() => []), list = Array.isArray(d) ? d : d.devices || [];
      $('#homeCasts').outerHTML = list.length ? list.map(x => `<div class="home-hue"><span><b>${esc(x.name)}</b><span class="hint">${offs[x.name] != null ? `Plays ${offs[x.name]} ms late, measured` : 'Delay not measured yet'}</span></span></div>`).join('') : '<p class="hint">No Google Cast speakers found on your network.</p>';
      el.insertAdjacentHTML('beforeend', '<p class="hint">Put speakers in rooms on the Rooms tab. The sync check, in Settings › Fine tuning, measures each one’s delay.</p>');
    }
  }
  for (const b of document.querySelectorAll('#homeTabs button')) b.onclick = () => showHome(b.dataset.t);

  // ------------------------------------------------------------------ Add-ons: a small store
  const TILES = {
    bulbstudio: ['#be185d', '<circle cx="12" cy="9" r="5"></circle><path d="M10 19h4M9.5 16h5M12 1.5v1.5M4 9H2.5M21.5 9H20"></path>'],
    doorbell: ['#047857', '<path d="M6 19h12M8 19v-6a4 4 0 018 0v6M12 5V3"></path><circle cx="12" cy="13" r="1.2"></circle>'],
    homeassistant: ['#1d4ed8', '<path d="M4 11l8-7 8 7v9H4z"></path><circle cx="12" cy="14" r="2"></circle>'],
  };
  const STORE_NAMES = { bulbstudio: 'Bulb Studio', doorbell: 'Reolink doorbell', homeassistant: 'Home Assistant' };
  const STORE_ABOUT = { bulbstudio: 'Looping shows for one bulb and one speaker, like a spooky porch.', doorbell: 'When it rings, the music dips and a show plays.', homeassistant: 'Your automations leave the lights alone during a show.' };
  const tile = id => `<span class="store-tile" style="background:${(TILES[id] || ['#7c3aed'])[0]}"><svg viewBox="0 0 24 24" aria-hidden="true">${(TILES[id] || ['', ''])[1]}</svg></span>`;
  let storePick = null;
  async function renderAddons() {
    const add = await get('/api/addons').catch(() => ({ addons: [] })), list = add.addons || [];
    if (!list.some(a => a.id === storePick)) storePick = (list.find(a => a.on) || {}).id || null;
    $('#storeGrid').innerHTML = list.map(a => `<div class="store-app ${a.id === storePick ? 'sel' : ''}" data-app="${esc(a.id)}">${tile(a.id)}
        <button class="store-open" data-pick="${esc(a.id)}"><b>${esc(STORE_NAMES[a.id] || a.name)}</b><span>${esc(a.on && a.missing ? a.missing : STORE_ABOUT[a.id] || a.about)}</span></button>
        ${a.on ? '<span class="store-added">Added</span>' : `<button class="accent store-get" data-add="${esc(a.id)}">Add</button>`}</div>`).join('');
    for (const b of $('#storeGrid').querySelectorAll('[data-pick]')) b.onclick = () => { storePick = b.dataset.pick; renderAddons(); };
    for (const b of $('#storeGrid').querySelectorAll('[data-add]')) b.onclick = async () => { await post('/api/addons', { id: b.dataset.add, on: true }); storePick = b.dataset.add; renderAddons(); };
    renderStoreCard(list.find(a => a.id === storePick));
  }
  async function renderStoreCard(a) {
    const el = $('#storeCard'); el.hidden = !a; if (!a) return;
    const head = `<div class="store-head">${tile(a.id)}<div><h2>${esc(STORE_NAMES[a.id] || a.name)}</h2><p class="hint">${a.on ? 'Added' : 'Not added yet'}</p></div></div>`;
    const remove = a.on ? '<button id="storeRemove">Remove add-on</button>' : `<button id="storeAdd" class="accent">Add ${esc(STORE_NAMES[a.id] || a.name)}</button>`;
    let body = `<p class="hint">${esc(STORE_ABOUT[a.id] || a.about)}</p>`;
    if (a.on && a.id === 'doorbell') {
      const [bell, st] = await Promise.all([get('/api/doorbell').catch(() => ({})), party('state').catch(() => ({}))]);
      body += `<p class="${bell.connected ? 'store-good' : 'hint'}">${bell.live === false ? 'This copy of the app does not connect to the doorbell.' : bell.connected ? `Connected${bell.name ? ' to ' + esc(bell.name) : ''}, listening` : esc(bell.error || 'Not connected yet')}</p>
        <label class="store-field">Doorbell address<input id="stBellHost" value="${esc(bell.host || '')}" placeholder="192.168.1.50"></label>
        <label class="store-field">Username<input id="stBellUser" value="${esc(bell.username || '')}" autocomplete="off"></label>
        <label class="store-field">Password${bell.has_password ? ' (saved, type a new one to change it)' : ''}<input id="stBellPass" type="password" autocomplete="new-password"></label>
        <label class="store-field">When it rings, play<select id="stBellShowMoment">${(st.show_moments || []).map(c => `<option value="${esc(c.id)}" ${c.id === bell.show_moment ? 'selected' : ''}>${esc(c.name)}</option>`).join('')}</select></label>`;
      el.innerHTML = head + body + `<div class="store-actions">${remove}</div>`;
      const save = async () => { const b = { on: true, host: $('#stBellHost').value.trim(), username: $('#stBellUser').value.trim(), show_moment: $('#stBellShowMoment').value }; const pw = $('#stBellPass').value; if (pw) b.password = pw; await post('/api/doorbell', b); renderAddons(); };
      for (const id of ['stBellHost', 'stBellUser', 'stBellPass', 'stBellShowMoment']) $('#' + id).onchange = save;
    } else if (a.on && a.id === 'homeassistant') {
      const ha = await get('/api/homeassistant').catch(() => ({}));
      body += `<p class="${ha.connected && !ha.error ? 'store-good' : 'hint'}">${ha.error ? esc(ha.error) : ha.connected ? 'Home Assistant is being told' : 'Nothing sent yet'}</p>
        <label class="store-field">Home Assistant address<input id="stHaUrl" value="${esc(ha.url || '')}" placeholder="http://homeassistant.local:8123"></label>
        <label class="store-field">Webhook id<input id="stHaHook" value="${esc(ha.webhook_id || 'light_show_studio')}"></label>`;
      el.innerHTML = head + body + `<div class="store-actions"><button id="stHaTest">Send now</button>${remove}</div>`;
      const save = test => post('/api/homeassistant', { url: $('#stHaUrl').value.trim(), webhook_id: $('#stHaHook').value.trim(), test }).then(renderAddons);
      $('#stHaUrl').onchange = $('#stHaHook').onchange = () => save(false); $('#stHaTest').onclick = () => save(true);
    } else if (a.on && a.id === 'bulbstudio') {
      el.innerHTML = head + body + `<p class="hint">Its shows switch on and off on the Party screen. It opens in a window of its own to make them.</p><div class="store-actions"><button id="stBulbOpen" class="accent">Open Bulb Studio</button>${remove}</div>`;
      $('#stBulbOpen').onclick = St.openBulbs;
    } else el.innerHTML = head + body + `<div class="store-actions">${remove}</div>`;
    const off = $('#storeRemove'); if (off) off.onclick = async () => { await post('/api/addons', { id: a.id, on: false }); renderAddons(); };
    const on = $('#storeAdd'); if (on) on.onclick = async () => { await post('/api/addons', { id: a.id, on: true }); renderAddons(); };
  }

  // ------------------------------------------------------------------ Settings: short on purpose
  async function askUpdate() {
    const u = await get('/api/update').catch(() => null);
    if (u) $('#railUpdate').hidden = !u.available;
  }
  $('#railUpdate').onclick = () => showView('settings');
  askUpdate();
  setInterval(askUpdate, 30 * 60 * 1000);
  let tuningOpen = false;
  function renderSettings() {
    const el = $('#stMain');
    const park = $('#latency') && $('#latency').closest('label'); if (park && el.contains(park)) $('#parked').appendChild(park);
    el.innerHTML = `<div id="stSpotify"></div><div id="stPhone"></div><div id="stApp"></div><details id="stTuning" class="st-card st-fold" ${tuningOpen ? 'open' : ''}><summary><b>Fine tuning</b><span class="hint">Most people never need this</span></summary><div id="stTuningBody"></div></details>`;
    drawSpotify($('#stSpotify')); drawPhone($('#stPhone')); drawApp($('#stApp'));
    const fold = $('#stTuning');
    fold.ontoggle = () => { tuningOpen = fold.open; if (fold.open) drawTuning($('#stTuningBody')); };
    if (fold.open) drawTuning($('#stTuningBody'));
  }
  const on = (id, fn) => { const b = $('#' + id); if (b) b.onclick = fn; };
  async function drawSpotify(el) {
    const st = await get('/api/relay/state').catch(() => ({})), sp = st.spotify || {};
    el.innerHTML = sp.signed_in ? card(`<div class="st-h">Spotify</div>` + row('Connected', 'Spotify picks the music, and the app sets up its own audio.', '<button id="stSignAgain">Sign in again</button> <button id="stSignOut">Sign out</button>'))
      : card(`<div class="st-h">Spotify</div><ol class="st-steps">
          <li>You need <b>Spotify Premium</b>: Spotify only lets Premium accounts play through apps like this one.</li>
          <li><a href="https://developer.spotify.com/dashboard" target="_blank" rel="noopener">Open the Spotify developer dashboard</a>, sign in, and press <b>Create app</b> (any name and description).</li>
          <li>Under <b>Redirect URIs</b>, add this address exactly: <code>${esc(st.redirect_uri || '')}</code> <button id="stCopyUri">Copy</button></li>
          <li>Tick <b>Web API</b> and <b>Web Playback SDK</b>, then <b>Save</b>.</li>
          <li>In the app’s <b>User Management</b>, add the email of every Spotify account that will sign in here.</li>
          <li>Paste the app’s <b>Client ID</b>: <input id="stClient" value="${esc(sp.client_id || '')}" placeholder="Client ID" spellcheck="false"></li>
          <li><button id="stSignIn" class="accent" ${sp.client_id ? '' : 'disabled'}>Sign in to Spotify</button></li></ol>`);
    on('stSignOut', async () => { await post('/api/spotify/signout'); drawSpotify(el); });
    on('stCopyUri', () => copy(st.redirect_uri || ''));
    const cid = $('#stClient');
    if (cid) { cid.oninput = () => { $('#stSignIn').disabled = !cid.value.trim(); }; cid.onchange = () => post('/api/spotify/client', { client_id: cid.value.trim() }); }
    const signIn = async () => {
      if (cid && cid.value.trim() !== (sp.client_id || '')) await post('/api/spotify/client', { client_id: cid.value.trim() });
      window.open('/api/spotify/login', 'spotify', 'width=520,height=760');
      const t = setInterval(async () => { const s2 = await get('/api/relay/state').catch(() => ({})); if ((s2.spotify || {}).signed_in && !sp.signed_in) { clearInterval(t); if (view === 'settings') drawSpotify(el); } }, 2000);
      setTimeout(() => clearInterval(t), 180000);
    };
    on('stSignIn', signIn); on('stSignAgain', signIn);
  }
  async function drawPhone(el) {
    const link = await get('/api/party/link').catch(() => ({}));
    el.innerHTML = card(`<div class="st-h">Your phone</div><div class="st-phone">${link.qr_svg || ''}<div><p class="st-lead">Host the party from your phone, on the same Wi-Fi as this computer.</p><ol class="st-steps">
        <li>Scan the code with the camera, or open <code>${esc(link.url)}</code> <button id="stCopyLink">Copy</button></li>
        <li>On iPhone: Share › Add to Home Screen. On Android: the menu › Add to Home screen.</li></ol></div></div>`);
    el.querySelector('#stCopyLink').onclick = () => copy(link.url);
  }
  async function drawApp(el) {
    const draw = async () => {
      const [s, kept, u] = await Promise.all([get('/api/models/setup').catch(() => null), get('/api/songs/copies').catch(() => null), get('/api/update').catch(() => null)]);
      const pct = s ? Math.round(100 * (s.progress || 0)) : 0;
      el.innerHTML = card(`<div class="st-h">This app</div>` +
        (s ? row('AI models and song learning', s.state === 'running' ? `Setting up by itself: ${pct}%, ${esc(s.step || s.phase)}` : s.installed ? `Ready, on ${esc((s.variant || 'cpu').toUpperCase())}.` : 'Without them the app uses its own analysis, which works well too.',
            s.state === 'running' ? '<button id="stModelsCancel">Cancel</button>' : `<button id="stModelsGo">${s.installed ? 'Install again' : 'Install'}</button>`) +
          (s.state === 'running' ? `<div class="st-progress"><div style="width:${pct}%"></div></div>` : '') +
          (s.state === 'failed' ? `<p class="warn">Did not install: ${esc(s.error)}</p>` : '') +
          (s.songs_error ? `<p class="warn">Song learning did not install, so songs are learned as they play: ${esc(s.songs_error)}</p>` : '') : '') +
        (kept ? row('Keep a copy of each song', `So a show can be reviewed and fixed later. ${kept.count} kept, ${kept.gb} GB.`, toggle('id="stKeepCopies"', kept.keep) + (kept.count ? ' <button id="stClearCopies">Delete copies</button>' : '')) : '') +
        (u && u.packaged ? row(u.available ? `Version ${esc(u.latest)} is ready` : 'Up to date', `You have version ${esc(u.version)}.${u.error ? ' Could not reach GitHub just now.' : ''}`,
            u.available && u.can_install ? '<button id="stUpRestart" class="accent">Restart and update</button>' : u.available ? `<a href="${esc(u.page)}" target="_blank" rel="noopener"><button class="accent">Download</button></a>` : '<button id="stUpCheck">Check now</button>') +
          row('Update when the app opens', u.can_install ? 'Off: it only says here that there is a new version.' : 'This copy cannot replace itself where it is. On a Mac, move it to Applications.', toggle(`id="stUpAuto" ${u.can_install ? '' : 'disabled'}`, u.auto_update && u.can_install)) : ''));
        on('stModelsGo', async () => { await post('/api/models/setup', { action: 'install' }); draw(); });
      on('stModelsCancel', async () => { await post('/api/models/setup', { action: 'cancel' }); draw(); });
      const keep = $('#stKeepCopies'); if (keep) keep.onchange = e => post('/api/songs/copies', { keep: e.target.checked });
      on('stClearCopies', async () => { if (confirm(`Delete ${kept.count} song copies? Their shows stay, but they can't be reviewed until learned again.`)) { await post('/api/songs/copies', { clear: true }); draw(); } });
      on('stUpCheck', async () => { $('#stUpCheck').textContent = 'Checking…'; await post('/api/update', { check: true }); askUpdate(); draw(); });
      on('stUpRestart', async () => { await post('/api/update', { restart: true }); St.toast('Restarting to update'); });
      const auto = $('#stUpAuto'); if (auto) auto.onchange = e => post('/api/update', { auto_update: e.target.checked });
      if (s && s.state === 'running' && view === 'settings') setTimeout(() => { if (view === 'settings' && el.isConnected) draw(); }, 1500);
    };
    draw();
  }
  async function drawTuning(el) {
    const [relay, dev, shows, st] = await Promise.all([get('/api/relay/state').catch(() => ({})), get('/api/audio/devices').catch(() => ({ inputs: [], outputs: [] })), get('/api/show_moments').catch(() => ({ shows: [] })), party('state').catch(() => ({}))]);
    const last = relay.last || {}, f = relay.fetch || {}, help = dev.hidden_help || {};
    const pick = (id, list, value, auto) => `<select id="${id}"><option value="">${esc(auto)}</option>${list.map(n => `<option value="${esc(n)}" ${n === value ? 'selected' : ''}>${esc(n)}</option>`).join('')}</select>`;
    const hiddenFound = dev.party_hidden_output ? dev.outputs.some(o => o.toLowerCase().includes(dev.party_hidden_output.toLowerCase())) : !!dev.auto_output;
    $('#parked').appendChild($('#latency').closest('label'));      // (a second draw must not take it with the old panel)
    el.innerHTML = `<div class="st-h">Timing</div>` +
      row('Sync check', relay.streaming && relay.output_kind === 'cast' ? 'Listens with the microphone and sets the speakers’ delay by itself.' : 'Available while a party plays on speakers.', `<button id="stSync" ${relay.streaming && relay.output_kind === 'cast' ? '' : 'disabled'}>Run it</button>`) +
      row('Lights’ timing', 'Send the lights this far ahead of the music, for every song.', '<span id="stLatency"></span>') +
      `<div class="st-h">The party’s music</div>` +
      row('The room hears it this far behind Spotify', 'The lights study this much music first. Longer gives a better first listen.', `<input id="stDelay" class="num" type="number" min="${f.on ? 3 : 10}" max="180" step="${f.on ? 1 : 5}" value="${Math.round(f.on ? (f.delay || 5) : (last.target_delay || 90))}"> s`) +
      row('On a skip', 'Keeping the delay, the room hears a skip that much later. Jumping, it hears it at once but the new song starts with almost no lead.', `<select id="stSkip"><option value="keep" ${last.skip_jumps ? '' : 'selected'}>Keep the delay</option><option value="jump" ${last.skip_jumps ? 'selected' : ''}>Jump right away</option></select>`) +
      row('Spotify crossfade', 'If Spotify’s own crossfade is on, set the same here, so a song ending isn’t taken for a skip.', `<input id="stXfade" class="num" type="number" min="0" max="12" step="1" value="${Math.round(last.crossfade || 0)}"> s`) +
      row('Even out song volume', 'Brings quiet and loud songs closer together.', toggle('id="stEven"', st.even_levels !== false)) +
      row('Start by itself', 'Starts when music begins playing on Light Show Studio in Spotify.', toggle('id="stAuto"', relay.auto)) +
      (f.available ? row('Learn songs ahead', 'Studies queued songs before they play, from a copy found on YouTube.', toggle('id="stFetch"', f.on)) : '') +
      (f.on ? '' : row('Open with', 'A saved show plays first, while Spotify fills up behind it.', `<select id="stOpener"><option value="">Nothing</option>${(shows.shows || []).filter(s => s.length >= 60 || s.name === relay.opener_choice).map(s => `<option value="${esc(s.name)}" ${s.name === relay.opener_choice ? 'selected' : ''}>${esc(St.niceName(s.name))}</option>`).join('')}</select>`)) +
      row('Spotify plays in', 'This app, hidden, or the Spotify app on this computer.', `<select id="stSource"><option value="studio" ${last.source === 'app' ? '' : 'selected'}>This app</option><option value="app" ${last.source === 'app' ? 'selected' : ''}>The Spotify app</option></select>`) +
      `<div class="st-h">Sound devices</div>` +
      row('Microphone', 'Listens to the room for the sync check.', pick('stMic', dev.inputs, dev.mic_device, `Default${dev.default_input ? ': ' + dev.default_input : ''}`) + ' <button id="stMicTest">Test</button> <span id="stMicLevel" class="hint"></span>') +
      row('The room hears this computer on', 'When the party plays on this computer.', pick('stRoomOut', dev.outputs, dev.party_room_output, `Default${dev.default_output ? ': ' + dev.default_output : ''}`)) +
      (help.label === 'nothing to install' ? '' : row('Spotify plays into', 'An output nobody listens to: the app mutes the one it picks, records it, and plays it to the room in time with the lights.' +
        (hiddenFound || !help.url ? '' : ` This computer has one output: <a href="${esc(help.url)}" target="_blank" rel="noopener">install ${esc(help.label)}</a>.`), pick('stHidden', dev.outputs, dev.party_hidden_output, dev.auto_output ? `Automatic: ${dev.auto_output}` : 'Automatic'))) +
      `<div class="st-h">Troubleshooting</div>` +
      row('Silent demo', 'Made-up songs and a pretend Spotify, with no sound, to see the app work.', relay.running ? '' : '<button id="stDemo">Run it</button>') +
      (relay.running ? `<details class="st-log"><summary>Log</summary>${(relay.log || []).slice().reverse().map(l => `<div>${esc(l)}</div>`).join('')}</details>` : '');
    const lat = $('#latency').closest('label'); $('#stLatency').appendChild(lat); lat.title = '';
    const prefs = body => post('/api/relay/prefs', body);
    const change = (id, fn) => { const e = $('#' + id); if (e) e.onchange = () => fn(e); };
    change('stDelay', e => prefs({ delay: +e.value }));
    change('stSkip', e => prefs({ skip_jumps: e.value === 'jump' }));
    change('stXfade', e => prefs({ crossfade: +e.value }));
    change('stEven', e => party('even', { on: e.checked }));
    change('stAuto', e => prefs({ auto: e.checked }));
    change('stFetch', e => prefs({ fetch: e.checked }).then(() => drawTuning(el)));
    change('stOpener', e => prefs({ opener: e.value }));
    change('stSource', e => prefs({ source: e.value }));
    change('stMic', e => post('/api/audio/devices', { mic_device: e.value }));
    change('stRoomOut', e => post('/api/audio/devices', { party_room_output: e.value }));
    change('stHidden', e => post('/api/audio/devices', { party_hidden_output: e.value }));
    const mic = $('#stMicTest'); if (mic) mic.onclick = async () => {
      $('#stMicLevel').textContent = 'Listening…';
      const r = await get('/api/audio/miclevel').catch(e => ({ error: e.message }));
      $('#stMicLevel').textContent = r.error ? `Could not listen: ${r.error}` : r.level_db < -70 ? 'Heard nothing: is it muted?' : `Hears the room (${r.level_db} dB)`;
    };
    const sync = $('#stSync'); if (sync) sync.onclick = async () => { St.toast('Listening to the room…'); const r = await post('/api/relay/synccheck', {}); St.toast(r.confidence >= 0.5 ? `Speakers corrected by ${r.offset_ms} ms (now ${r.speaker_offset_ms} ms)` : (r.reason || 'Could not get a clear reading; try again in a moment'), !(r.confidence >= 0.5)); };
    const demo = $('#stDemo'); if (demo) demo.onclick = async () => { await post('/api/relay/start', { demo: true, speakers: 'none', target_delay: 20 }); St.toast('Silent demo running: made-up songs, no sound.'); showView('party'); };
  }

  // where the app opens: Party, or the last place; #party, #home/rooms and so on are links to a place
  (() => {
    let v = 'party'; try { v = { studio: 'shows' }[localStorage.getItem('lss-view')] || localStorage.getItem('lss-view') || 'party'; } catch (e) { /* the first */ }
    const [hv, hs] = location.hash.slice(1).split('/');
    if (VIEWS[hv]) { v = hv; if (hv === 'home' && hs) homeTab = hs; }
    showView(v);
  })();
  window.addEventListener('hashchange', () => { const [hv, hs] = location.hash.slice(1).split('/'); if (VIEWS[hv]) { if (hv === 'home' && hs) homeTab = hs; showView(hv); } });
})();
