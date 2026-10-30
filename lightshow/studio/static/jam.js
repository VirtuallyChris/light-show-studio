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
  // what each song's show is built from: your own show, an analysis made ahead (or the last time it played),
  // or a first listen; learning ahead, a first listen says where it stands: being analyzed, or next in line
  const KIND = { custom: ['your show', 'k-custom'], learned: ['analyzed', 'k-learned'], live: ['first listen', 'k-live'],
                 analyzing: ['analyzing…', 'k-learning'], queued: ['queued', 'k-live'], failed: ['error', 'k-error'] };
  // anything that went wrong analyzing a song (no copy found, a model that failed) says so: error, and why
  const badge = (k, t) => {
    const s = t && t.learning ? t.learning : k, [label, cls] = KIND[s] || KIND.live;
    const why = t && t.learning === 'failed' ? ` title="${esc(t.learning_why || 'something failed')}"` : '';
    return `<span class="jam-kind ${cls}"${why}>${label}</span>`;
  };
  const styleTag = t => t && t.style ? ` <span class="jam-style">${esc(t.style)}</span>` : '';

  // ------------------------------------------------------------------ colors
  // Every jam song is shown in two colors: the ones picked for that track (kept for next time), else
  // the jam's own pair, else (jam set to "album cover") its cover's. A show you made keeps its own.
  // The party's colors can be switched off for every song, or for one: then its cover's colors.
  const themeOn = () => ((J.state || {}).jam_colors || {}).on !== false;
  function colorsFor(track) {
    const st = J.state || {}, own = track && (st.track_colors || {})[track.id], jc = st.jam_colors || {};
    if (!themeOn() || (own && own.off)) return null;
    if (own) return [own.primary, own.secondary];
    if (jc.wild) return null;                                       // No Limits: the show picks its colors section by section
    if (jc.mode === 'pick') return [jc.primary, jc.secondary];
    if (jc.mode === 'mix') return (st.assigned_colors || {})[track.id] || (jc.pairs || [])[0] || null;
    return null;
  }
  /** The extras (a few small accents in two more colors), picked from the track's own two colors: the
   *  hues farthest from both, either side of the middle of the wider gap between them on the color
   *  wheel (purple and green get red and orange). The server's extras_for does the same for the phone. */
  const onlyTwo = track => !!(track && ((J.state || {}).track_colors || {})[track.id] || {}).only;
  function extrasFor(pair, track) {
    const jc = (J.state || {}).jam_colors || {};
    if (!jc.extras || !themeOn() || !pair || pair.length < 2 || onlyTwo(track)) return null;
    const hue = hx => { const r = parseInt(hx.slice(1, 3), 16) / 255, g = parseInt(hx.slice(3, 5), 16) / 255, b = parseInt(hx.slice(5, 7), 16) / 255;
      const mx = Math.max(r, g, b), mn = Math.min(r, g, b), d = mx - mn; if (!mx || d / mx < 0.2 || mx < 0.2) return null;
      let h = mx === r ? ((g - b) / d) % 6 : mx === g ? (b - r) / d + 2 : (r - g) / d + 4; return ((h * 60) + 360) % 360; };
    const h0 = hue(pair[0]), h1 = hue(pair[1]);
    if (h0 === null || h1 === null) return ['#ff6000', '#ff0000'];
    const a = ((h1 - h0) % 360 + 360) % 360, [start, span] = a >= 180 ? [h0, a] : [h1, 360 - a];
    const hex = h => { const f = n => { const k = (n + h / 60) % 6; return Math.round(255 * (1 - Math.max(0, Math.min(k, 4 - k, 1)))); };
      return '#' + [f(5), f(3), f(1)].map(x => x.toString(16).padStart(2, '0')).join(''); };
    return [hex((start + span * 0.38) % 360), hex((start + span * 0.62) % 360)];
  }
  /** The party's pairs, for the rest of a song: its own pair is its drops' colors, and the other parts now and then
   *  take another of these (app.js buildSection; the user). None for a song kept to just its two colors. */
  const pairsFor = track => { const jc = (J.state || {}).jam_colors || {};
    return themeOn() && !onlyTwo(track) && Array.isArray(jc.pairs) && jc.pairs.length > 1 ? jc.pairs : null; };
  /** No Limits Colors: every section in the stage colors that suit it (app.js wildPlan), for every song
   *  but one given its own colors (or its cover's) by hand */
  const wildOn = track => { const jc = (J.state || {}).jam_colors || {}, own = track && ((J.state || {}).track_colors || {})[track.id];
    return themeOn() && !!jc.wild && !own; };
  /** rainbow drops for the songs that go all out, in "a different pair each song" */
  const rainbowOn = track => { const jc = (J.state || {}).jam_colors || {}; return themeOn() && jc.mode === 'mix' && !!jc.rainbow && !onlyTwo(track); };
  function shownColors(t) {
    const cols = colorsFor(t) || ((J.art || {})[t.image] || []).slice(0, 2);
    if (!colorsFor(t) && t.image && J.art && J.art[t.image] === undefined) fetchArt(t);
    return [cols[0] || '#808080', cols[1] || cols[0] || '#808080'];
  }
  function fetchArt(t) {
    J.artPending = J.artPending || new Set(); if (J.artPending.has(t.image)) return;
    J.artPending.add(t.image);
    coverJob(t.image).then(() => {
      if (J.art[t.image] !== undefined) J.artPending.delete(t.image);
      else setTimeout(() => J.artPending.delete(t.image), 15000);   // not fetched: asked again in a while, not at once
      J.shown = null; render();
    });
  }
  // ------------------------------------------------------------------ the 16 colors
  // Every color box in this panel opens these sixteen instead of the system picker: solid colors the
  // lights show well, a click each. A song's own pair can also be kept to just its two colors.
  const SIXTEEN = ['#ff0000', '#ff4000', '#ff8000', '#ffbf00', '#ffff00', '#80ff00', '#00ff00', '#00ff80',
                   '#00ffff', '#0080ff', '#0000ff', '#4000ff', '#8000ff', '#ff00ff', '#ff0080', '#ffffff'];
  function openSixteen(input) {
    closeSixteen();
    const pair = input.closest('.jam-pair[data-id]'), id = pair && pair.dataset.id;
    const pop = document.createElement('div'); pop.className = 'sixteen'; pop.id = 'sixteen';
    const cur = (input.value || '').toLowerCase();
    pop.innerHTML = `<div class="sx-grid">${SIXTEEN.map(c => `<button style="background:${c}" data-c="${c}" class="${c === cur ? 'on' : ''}" title="${c}"></button>`).join('')}</div>`
      + (id ? `<label class="sx-only" title="No accents in other colors and no rainbow drops: only this song's two colors"><input type="checkbox" ${onlyTwo({ id }) ? 'checked' : ''}> Just these two colors</label>` : '');
    document.body.appendChild(pop);
    const r = input.getBoundingClientRect(), w = pop.offsetWidth, h = pop.offsetHeight;
    pop.style.left = Math.max(8, Math.min(innerWidth - w - 8, r.left)) + 'px';
    pop.style.top = (r.bottom + h + 8 > innerHeight ? Math.max(8, r.top - h - 6) : r.bottom + 6) + 'px';
    for (const b of pop.querySelectorAll('[data-c]')) b.onclick = () => {
      closeSixteen(); input.value = b.dataset.c;
      input.dispatchEvent(new Event('input', { bubbles: true })); input.dispatchEvent(new Event('change', { bubbles: true }));
    };
    const only = pop.querySelector('.sx-only input');
    if (only) only.onchange = async () => {
      const a = pair.querySelector('.jp-a'), b = pair.querySelector('.jp-b');
      try {
        J.state = await api('jam/colors', { track_id: id, primary: a.value, secondary: b.value, only: only.checked });
        if (id === roomTrackId()) recolor();
        J.shown = null; render();
      } catch (e) { toast(e.message, true); }
    };
  }
  function closeSixteen() { const p = $('#sixteen'); if (p) p.remove(); }
  document.addEventListener('click', e => {
    const input = e.target.closest && e.target.closest('#jamBody input[type=color]');
    if (input) { e.preventDefault(); openSixteen(input); return; }
    if (!e.target.closest || !e.target.closest('#sixteen')) closeSixteen();
  }, true);
  document.addEventListener('keydown', e => { if (e.key === 'Escape') closeSixteen(); });

  function pairPicker(t) {
    if (!t || !t.id || t.kind === 'custom') return '';
    if (wildOn(t)) return '';                                        // No Limits: the show picks them, section by section
    const [a, b] = shownColors(t), own = (J.state.track_colors || {})[t.id];
    // (shown the way the room sees them: the stored secondary is on the bright hits and shows most, so it is
    // the one called Primary, first; what is stored, and the show, are as before)
    if (!themeOn()) return `<span class="jam-pair"><span class="jam-sw" style="background:${b}" title="Its album cover's colors"></span><span class="jam-sw" style="background:${a}"></span></span>`;
    const off = !!(own && own.off);
    return `<span class="jam-pair" data-id="${esc(t.id)}"><input type="color" class="jp-b" value="${b}" title="Primary color for this song: the one seen most (kept for next time)"><input type="color" class="jp-a" value="${a}" title="Secondary color for this song">`
      + (['pick', 'mix'].includes((J.state.jam_colors || {}).mode) || off ? `<button class="jp-theme small ${off ? '' : 'on'}" title="${off ? 'This song plays in its album cover\'s colors. Click for the party\'s' : 'This song plays in the party\'s colors. Click for its album cover\'s'}">${off ? 'cover' : 'party'}</button>` : '')
      + `${own && !off ? '<button class="jp-reset small" title="Back to the jam\'s colors">&#8634;</button>' : ''}</span>`;
  }
  function colorsSection(st) {
    const jc = st.jam_colors || {};
    const source = jc.on === false ? 'album' : jc.wild ? 'wild' : jc.mode !== 'pick' && jc.mode !== 'mix' ? 'album' : 'party';
    const pick = `<div class="jam-row"><label>Colors</label><span class="jam-source" role="group">${[['album', 'Album art', 'Every song in its album cover\'s colors'], ['party', 'Party colors', 'Every song in the party\'s two colors, or a pair of them each'],
      ['wild', 'No Limits', 'Every section its own palette: a breakdown all blue, a red drop, a rainbow, green there…']].map(([k, n, t]) => `<button class="small${source === k ? ' on' : ''}" data-src="${k}" title="${t}">${n}</button>`).join('')}</span></div>`;
    if (source !== 'party') return `<div class="jam-sec"><h4>Colors</h4>${pick}</div>`;
    return `<div class="jam-sec"><h4>Colors</h4>${pick}
        <div class="jam-row"><label>Every song in</label><span class="jam-pair"><select id="jamColorMode"><option value="pick" ${jc.mode === 'pick' ? 'selected' : ''}>These two colors</option><option value="mix" ${jc.mode === 'mix' ? 'selected' : ''}>A different pair each song</option></select>${jc.mode === 'pick' ? `<input type="color" id="jamSecondary" value="${esc(jc.secondary || '#00ff00')}" title="Primary: the one seen most"><input type="color" id="jamPrimary" value="${esc(jc.primary || '#8000ff')}" title="Secondary">` : ''}</span></div>
        ${jc.mode === 'mix' ? `<div class="jam-row"><label title="Each song gets one of these, chosen by its vibe, never the same pair twice in a row">Pairs</label><span class="jam-pairs">${(jc.pairs || []).map((p, i) => `<span class="jam-pair"><input type="color" class="mx-b" data-i="${i}" value="${esc(p[1])}"><input type="color" class="mx-a" data-i="${i}" value="${esc(p[0])}"></span>`).join('')}</span></div>
        <div class="jam-row"><label title="A song that goes all out gets rainbow drops">Rainbow for epic songs</label><input type="checkbox" id="jamRainbow" ${jc.rainbow ? 'checked' : ''}></div>` : ''}
        ${jc.mode === 'pick' || jc.mode === 'mix' ? `<div class="jam-row"><label title="A few of the small accents (a strike, a spark, a sweep) take two more colors now and then, picked for each song from its own two (purple and green get red and orange)">Now and then</label><span class="jam-pair"><input type="checkbox" id="jamExtras" ${jc.extras ? 'checked' : ''}></span></div>` : ''}</div>`;
  }
  /** The song in the room got new colors (here, or on the Party Hub): build its show again in them. */
  // A vibe or a genre picked by hand for a song the analysis gets wrong. The genre gives the show the words
  // the genre effects listen for, and (the vibe left to the analysis) the vibe that goes with it.
  const trackGenre = t => { const k = t && ((J.state || {}).track_genres || {})[t.id]; return k && ((J.state || {}).genre_choices || []).find(g => g.key === k) || null; };
  const levelStyle = lv => lv < 0.7 ? 'chill' : lv < 1.6 ? 'groove' : lv < 2.4 ? 'dance' : 'rave';
  const trackStyle = t => (t && ((J.state || {}).track_styles || {})[t.id]) || (trackGenre(t) ? levelStyle(trackGenre(t).level) : null);
  const colorSig = () => { const t = J.doc && J.doc.track; return JSON.stringify([colorsFor(t), extrasFor(colorsFor(t), t), rainbowOn(t), wildOn(t), trackStyle(t), (trackGenre(t) || {}).key]); };
  function applyStyle(t) {
    const p = St.P(); if (!p) return;
    p.settings = { ...(p.settings || {}) };
    if (trackStyle(t)) p.settings.style = trackStyle(t); else delete p.settings.style;
    applyGenre(t);
  }
  function applyGenre(t) {
    const g = trackGenre(t), a = S.analysis; if (!a) return;
    if (!a._heardGenre) a._heardGenre = a.genre || null;             // what the analysis heard, to go back to
    a.genre = g ? { ...(a._heardGenre || {}), genres: [g.words], spotify: [], picked: g.key } : a._heardGenre;
    a._swells = undefined;
  }
  function recolor() {
    if (!J.live || !J.doc || J.doc.kind === 'custom' || J.doc.locked || J.custom) return;   // (a locked show, or one made yours, keeps its colors)
    // not while the song's show is still being loaded (a cover can arrive half way): the load uses it itself
    if (!St.P() || J.key !== J.doc.key) return;
    if (J.doc.light) { S.jamColors = colorsFor(J.doc.track); J.colorSig = colorSig(); buildLight(); return; }
    S.jamColors = colorsFor(J.doc.track); S.jamPairs = pairsFor(J.doc.track); S.jamExtras = extrasFor(S.jamColors, J.doc.track); S.jamRainbow = rainbowOn(J.doc.track); S.noLimits = wildOn(J.doc.track); J.colorSig = colorSig();
    const restyle = (St.P().settings || {}).style !== (trackStyle(J.doc.track) || undefined);
    applyStyle(J.doc.track);
    if (restyle) { St.pickLook(); J.style = St.showStyle(); }
    try { St.autoBuild(true); J.built = sectionKeys(); } catch (e) { console.error(e); }
    J.clips = St.clipSig();                                        // (rebuilt by the jam, not by hand)
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
        ${(st.fetch || {}).available ? `<div class="jam-row"><label title="Each queued song is found on YouTube by its Spotify details, analysed before it plays and the copy deleted, so the room needs to be only seconds behind. A song nobody queued is learned as it plays.">Learn ahead</label><label class="toggle"><input type="checkbox" id="jamFetch" ${st.fetch.on ? 'checked' : ''}></label></div>` : ''}
        <div class="jam-row" ${(st.fetch || {}).on ? 'hidden' : ''}><label title="One of your saved shows plays in the room first, with its lights, while Spotify plays unheard behind it. The room joins the jam as it ends, a whole song behind, so each song is heard to its end before the room gets to it.">Open with</label><select id="jamOpenWith">
          <option value="">Nothing (start ${esc(String(Math.round(((st.last || {}).target_delay) || 90)))} s behind)</option>
          ${(J.openerShows || []).filter(s => s.length >= 60 || s.name === st.opener_choice).map(s => `<option value="${esc(s.name)}" ${s.name === st.opener_choice ? 'selected' : ''}>${esc(s.name)}</option>`).join('')}</select></div>
        <div class="jam-row" ${st.opener_choice && !(st.fetch || {}).on ? 'hidden' : ''}><label>Stay behind by</label><span><input id="jamDelay" type="number" min="${(st.fetch || {}).on ? 3 : 10}" max="180" step="${(st.fetch || {}).on ? 1 : 5}" value="${(st.fetch || {}).on ? Math.round(st.fetch.delay || 5) : 90}" class="num"> s</span></div>
        <div class="jam-row"><label title="Spotify: Settings, Playback, Crossfade. The studio cannot read it, and needs it to tell a skip from a song that simply ended.">Spotify crossfade</label><span><input id="jamXfade" type="number" min="0" max="12" step="1" value="0" class="num"> s</span></div>
        <div class="jam-row"><label>On a skip</label><select id="jamOnSkip">
          <option value="keep">Keep the delay</option>
          <option value="jump">Jump right away</option></select></div>
        <div class="hint">The lights study this much music before the room hears it. Keeping the delay, the room hears a skip that much later, and the lights see the new song coming. Jumping, the room hears it at once, but the new song starts with almost no lead, and the delay only grows back a few seconds between later songs. Play and pause follow the phone.</div></div>
      ${colorsSection(st)}
      <div class="jam-actions">
        <button id="jamStart" class="accent" ${sp.signed_in ? '' : 'disabled'}>Start jam</button>
        <button id="jamDemo" title="Synthetic songs and a pretend Spotify. No sound, no speakers, no sign-in.">Silent demo</button></div>`;
    } else {
      const room = st.room, ahead = st.ahead; const pct = Math.max(0, Math.min(100, 100 * (st.delay || 0) / (st.target_delay || 90)));
      html += `<div class="jam-sec"><h4>${esc(st.mode || 'jam')}</h4>
        <div class="jam-now"><div class="lbl">In the room</div><div class="ttl">${room ? esc(room.label) : (st.streaming ? 'between songs' : `filling the buffer… ${Math.round(st.buffered || 0)} s of ${Math.round(st.target_delay)} s`)}</div>${room ? badge(room.kind, room) + styleTag(room) + (room.project ? ` <span class="hint">${esc(room.project)}</span>` : '') + pairPicker(room) : ''}</div>
        <div class="jam-now"><div class="lbl">On Spotify now, ${Math.round(st.delay || 0)} s ahead of the room</div><div class="ttl">${ahead ? esc(ahead.label) : '—'}</div>${ahead ? badge(ahead.kind, ahead) + styleTag(ahead) + (ahead.kind === 'live' ? ` <span class="hint">analysed to ${fmt(ahead.horizon)}</span>` : '') : ''}</div>
        <div class="jam-meter" title="How far the room is behind Spotify"><div style="width:${pct}%"></div><span>${(st.delay || 0).toFixed(1)} s behind · aiming for ${Math.round(st.target_delay)} s</span></div>
        ${st.paused ? '<div class="hint">Paused on the phone. The buffer is held.</div>' : ''}
        ${st.handover ? `<div class="hint">${st.handover === 'armed' ? 'Spotify is held until your opening song is nearly over.' : 'Filling the buffer behind the opening song…'}</div>` : ''}
        ${st.capture && st.capture.error ? `<div class="warn">Capture: ${esc(st.capture.error)}</div>` : ''}
        ${st.output_error ? `<div class="warn">Playback: ${esc(st.output_error)}</div>` : ''}
        ${st.sync_warning ? `<div class="warn">${esc(st.sync_warning)}</div>` : ''}
        ${sp.error ? `<div class="warn">Spotify: ${esc(sp.error)}</div>` : ''}</div>
      ${colorsSection(st)}
      <div class="jam-sec"><h4>Up next${fetchLine(st)}</h4>${(st.queue || []).slice(0, 8).map(q => `<div class="jam-q"><span>${esc(q.label)}</span>${pairPicker(q)}${badge(q.kind, q)}</div>`).join('') || '<div class="hint">Nothing queued.</div>'}</div>
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

  // the saved shows a jam can open with (a minute or longer)
  function loadOpenerShows() {
    if (J.loadingShows) return; J.loadingShows = true;
    fetch('/api/cues').then(r => r.json()).then(d => { J.openerShows = d.shows || []; render(); }).catch(() => { J.openerShows = J.openerShows || []; }).finally(() => { J.loadingShows = false; });
  }

  function fetchLine(st) {
    const f = st.fetch || {};
    if (!f.on) return '';
    const fails = (f.failed || []).map(x => `${x.label}: ${x.why}`).join('\n');
    return ` <span class="hint" title="${esc(fails || 'Queued songs are learned from a copy on YouTube before they play')}">${f.working ? `learning ${esc(f.working)}` + (f.waiting ? `, ${f.waiting} more` : '') : 'learned ahead'}${(f.failed || []).length ? ` · ${f.failed.length} not found` : ''}</span>`;
  }

  function wire(keep) {
    const on = (id, fn) => { const e = $('#' + id); if (e) e.onclick = () => fn().catch(err => toast('Jam: ' + err.message, true)); };
    // the form starts from the settings last used, then keeps whatever is being changed
    const last = (J.state && J.state.last) || {};
    const fromLast = { jamDelay: ((J.state || {}).fetch || {}).on ? (J.state.fetch.delay || 5) : last.target_delay, jamXfade: last.crossfade, jamOnSkip: last.skip_jumps === undefined ? undefined : (last.skip_jumps ? 'jump' : 'keep'),
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
    const fetchSw = $('#jamFetch');
    if (fetchSw) fetchSw.onchange = () => api('jam/prefs', { fetch: fetchSw.checked }).then(st => { J.state = st; J.shown = null; render(); }).catch(e => toast(e.message, true));
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
    for (const b of el2.querySelectorAll('.jam-source button')) b.onclick = () => { if (!b.classList.contains('on')) saveJam({ source: b.dataset.src }).catch(err => toast(err.message, true)); };
    const ex = $('#jamExtras'); if (ex) ex.onchange = () => saveJam({ extras: ex.checked }).catch(err => toast(err.message, true));
    for (const box of el2.querySelectorAll('.jam-pair[data-id]')) {
      const id = box.dataset.id, a = box.querySelector('.jp-a'), b = box.querySelector('.jp-b'), reset = box.querySelector('.jp-reset'), theme = box.querySelector('.jp-theme');
      const save = async body => {
        J.state = await api('jam/colors', { track_id: id, ...body });
        if (id === roomTrackId()) recolor();
        if (document.activeElement) document.activeElement.blur();
        J.shown = null; render();
      };
      a.onchange = b.onchange = () => save({ primary: a.value, secondary: b.value, only: onlyTwo({ id }) }).catch(e => toast(e.message, true));
      if (reset) reset.onclick = () => save({}).catch(e => toast(e.message, true));
      if (theme) theme.onclick = () => save(theme.classList.contains('on') ? { theme: false } : {}).catch(e => toast(e.message, true));
    }
    on('jamPlayerHere', async () => { await api('jam/player/transfer', {}); toast('Spotify is now playing on Light Show Studio'); });
    const dev = $('#jamDevice');
    if (dev) { dev.innerHTML = '<option value="">Default output</option>' + (J.outputs || []).filter(o => o.name).map(o => `<option value="${esc(o.name)}">${esc(o.name)}${o.default ? ' (default)' : ''}</option>`).join(''); if (keep.jamDevice) dev.value = keep.jamDevice; }
    const cid = $('#jamClient'); if (cid) cid.onchange = () => api('spotify/client', { client_id: cid.value.trim() }).then(s => { J.state = { ...J.state, ...s }; render(); }).catch(e => toast(e.message, true));
    on('jamSignIn', async () => { if (cid && cid.value.trim() !== ((J.state.spotify || {}).client_id || '')) await api('spotify/client', { client_id: cid.value.trim() }); window.open('/api/spotify/login', 'spotify', 'width=520,height=760'); });
    on('jamSignOut', async () => { J.state = await api('spotify/signout', {}); render(); });
    const start = extra => api('jam/start', { device: dev ? dev.value : '', target_delay: parseFloat(($('#jamDelay') || {}).value) || 90, crossfade: parseFloat(($('#jamXfade') || {}).value) || 0, skip_jumps: (($('#jamOnSkip') || {}).value) === 'jump', source: (source && source.value) || 'studio', speakers: (where && where.value) || 'none', pc_device: (pcDev && pcDev.value) || '', ...extra });
    on('jamStart', async () => { if (window.Studio && Studio.modelsReady) await Studio.modelsReady('the jam'); J.state = await start({}); render(); const op = (J.state.cue || {}).id === 'opener' ? J.state.cue : null; if (op) return toast(`Jam started: ${op.show} plays first, and the room joins the jam as it ends.`); toast({ cast: 'Jam started. The speakers join once the buffer is full.', pc: 'Jam started. This PC starts playing once the buffer is full.' }[J.state.output_kind] || 'Jam started as a preview: nothing plays.'); });
    const openWith = $('#jamOpenWith');
    if (openWith) {
      if (J.openerShows === undefined) loadOpenerShows();
      openWith.onfocus = loadOpenerShows;                        // a show saved since shows up
      openWith.onchange = () => api('jam/prefs', { opener: openWith.value }).then(st => { J.state = st; render(); }).catch(e => toast(e.message, true));
    }
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
      if (typeof st.party_level === 'number' && st.party_level !== S.partyLevel) { S.partyLevel = st.party_level; S.forceSend = true; }
      if (J.live && J.doc && J.doc.kind !== 'custom' && J.colorSig !== undefined && colorSig() !== J.colorSig) recolor();   // changed on the phone: at once
      if (st.running && !was && st.auto_started) toast('Music started on Light Show Studio: the jam started by itself');
      await cueTick(st.cue);
      if (C.run) { /* a cue has the room: the jam waits */ }
      else if (st.running && st.streaming) await follow(st); else if (J.live && !(st.running)) leave();
      if (J.open || st.running !== was) render();
    } catch (e) { /* server away */ } finally { J.fetching = false; }
  }

  // ------------------------------------------------------------------ cues
  // A Party Hub cue (the doorbell, shot o'clock) stops the party for one of your saved shows. In a jam
  // the server plays the show's song inside the room's own stream (otherwise on the speakers, or leaves
  // it to this page on this PC); this page plays the show's lights, following the speaker, then hands
  // the room back to the jam. Every cue's show is kept loaded here, and the running cue is asked for
  // five times a second, so its lights start with its song instead of a second or two after.
  const C = { run: null, shows: {} };
  function showData(name) {
    if (!C.shows[name]) C.shows[name] = fetch('/api/projects/' + encodeURIComponent(name)).then(r => r.json())
      .then(d => { if (d.error) delete C.shows[name]; return d; }).catch(e => { delete C.shows[name]; return { error: String(e) }; });
    return C.shows[name];
  }
  async function preloadCues() {
    try {
      const d = await (await fetch('/api/cues')).json(), want = new Set((d.cues || []).flatMap(c => c.shows || [c.show]).filter(Boolean));   // (every show a cue may pick)
      for (const n of Object.keys(C.shows)) if (!want.has(n)) delete C.shows[n];
      for (const n of want) showData(n);
    } catch (e) { /* the studio is away: next time */ }
  }
  preloadCues(); setInterval(preloadCues, 60000);
  async function cueTick(cue) {
    // As the music comes back (fade_in) the lights cross-fade from the cue's show into the jam's over that very
    // fade: what is left of it as this page sees it begin, so both end together (the user: the same cross-fade as
    // that doorbell's). A doorbell with no fade back: the lights change over at once, as the music does.
    const active = !!(cue && cue.phase && cue.phase !== 'done' && cue.show), showing = active && cue.phase === 'show';
    if (active) showData(cue.show);
    if (showing && C.run !== cue.run && C.ended !== cue.run) await cueStart(cue);   // (a slower answer may still say it shows)
    else if (!showing && C.run) {
      const left = cue && cue.phase === 'fade_in' ? (cue.dur || 0) * (1 - (cue.progress || 0)) : 0;
      if (left > 0.05) St.crossfadeLights(left);
      cueEnd();
    }
  }
  setInterval(async () => {
    if (C.asking || !J.state || !J.state.running) return;
    C.asking = true;
    try { await cueTick(await (await fetch('/api/cues/now')).json()); } catch (e) { } finally { C.asking = false; }
  }, 200);
  async function cueStart(cue) {
    if (C.run) cueEnd();
    C.run = cue.run; C.via = cue.via;
    // the jam's show is kept as it is, to take up again the moment the cue is over (not loaded again)
    C.prev = { analysis: S.analysis, project: S.project, name: S.name, buffer: S.audio.buffer, jam: J.live, playing: S.audio.playing,
               key: J.key, doc: J.doc, version: J.version, built: J.built, style: J.style, drums: J.drums,
               palette: S.albumPalette, colors: S.jamColors, pairs: S.jamPairs, extras: S.jamExtras, rainbow: S.jamRainbow, wild: S.noLimits, glow: S.glowAt };
    S.glowAt = null;                                                // (a cue's show has no songs either side)
    J.live = false; J.key = null;                                   // the jam steps aside; the room is held
    // how far into the show the server already is (the page looks once a second), kept up to date below
    const asked = performance.now(), into = (cue.progress || 0) * (cue.dur || 0);
    const data = await showData(cue.show);
    if (C.run !== cue.run) return;
    if (data.error) { toast(`${cue.name}: ${data.error}`, true); return; }
    // the show itself, without touching playback (opening a song the usual way would pause the speaker)
    adopt(data.analysis, cue.show, data.saved || null, data); S.name = '';  // never saved over while it plays
    if (!data.saved) try { St.autoBuild(true); } catch (e) { console.error(e); }
    const started = asked - into * 1000;                             // when the show began, on this page's clock
    if (cue.via === 'cast') {
      // the lights follow the speaker's own position, less its measured delay
      const off = ((S.defaults.castOffsets || {})[cue.device] ?? S.defaults.castOffset ?? 0) / 1000;
      C.cast = { pos: 0, at: performance.now(), state: 'BUFFERING' };
      C.poll = setInterval(async () => { try { const s = await (await fetch('/api/cast/status')).json(); C.cast = { pos: s.position || 0, at: performance.now(), state: s.state }; } catch (e) { } }, 400);
      C.pos = () => Math.max(0, (C.cast.state === 'PLAYING' ? C.cast.pos + (performance.now() - C.cast.at) / 1000 : C.cast.pos) - off);
    } else if (cue.via === 'pc' && cue.stream) {
      C.pos = () => (performance.now() - started) / 1000;          // in the jam's stream on this PC: lights only
    } else if (cue.via === 'pc') {
      // this PC plays the song itself
      try {
        const a = S.audio; if (!a.ctx) a.ctx = new (window.AudioContext || window.webkitAudioContext)();
        const buf = await a.ctx.decodeAudioData(await (await fetch(data.audio_url)).arrayBuffer());
        if (C.run !== cue.run) return;
        if (a.ctx.state === 'suspended') await a.ctx.resume();
        C.src = a.ctx.createBufferSource(); C.src.buffer = buf; C.src.connect(a.ctx.destination);
        const lag = (performance.now() - started) / 1000;            // the server's clock started when it asked
        C.src.start(0, Math.min(lag, Math.max(0, buf.duration - 0.1))); C.t0 = a.ctx.currentTime - lag;
        C.pos = () => Math.max(0, S.audio.ctx.currentTime - C.t0);
      } catch (e) { toast(`${cue.name}: could not play the song here`, true); C.pos = () => (performance.now() - started) / 1000; }
    } else C.pos = () => (performance.now() - started) / 1000;      // a silent preview: the lights only
    S.cue = { live: true, pos: () => C.pos() };
    S.audio.playing = true; St.updateTransport();
    if ($('#outputOn').checked) St.startOutput();
    S.forceSend = true; St.zoomFit(); St.draw();
  }
  function cueEnd() {
    clearInterval(C.poll); C.poll = null;
    if (C.src) { try { C.src.stop(); } catch (e) { } C.src = null; }
    S.cue = null;
    const p = C.prev || {}; C.ended = C.run; C.run = null; C.prev = null;
    S.analysis = p.analysis; S.project = p.project; S.name = p.name || ''; S.audio.buffer = p.buffer;
    if (p.jam) {
      // the jam's show again, as it was: the next tick anchors it to the room, nothing is loaded
      J.live = true; J.key = p.key; J.doc = p.doc; J.version = p.version; J.built = p.built; J.style = p.style; J.drums = p.drums;
      S.albumPalette = p.palette; S.jamColors = p.colors; S.jamPairs = p.pairs || null; S.jamExtras = p.extras; S.jamRainbow = p.rainbow; S.noLimits = p.wild; S.glowAt = p.glow || null;
      S.forceSend = true;
      setTimeout(tick, 0);                                           // where the room is now, not a second from now
    }
    else { S.audio.playing = false; S.audio.pos = 0; St.updateTransport(); }
    if (S.project) { St.zoomFit(); St.draw(); }
  }
  J.cue = C;

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
      if (room.key !== J.key || (J.doc && room.kind !== J.doc.kind)) { await load(room); if (room.key !== J.key) return; }
      else if (room.kind === 'live' && room.version !== J.version) await refresh(room);
      else if (room.kind === 'live' && performance.now() - (J.lastFeat || 0) > 5000) await refresh(room, true);
    } catch (e) { buildFailed(room, e); }
    anchorTo(room);
    S.audio.playing = !!room.playing; St.updateTransport();
    if ($('#outputOn').checked) St.startOutput();
    prefetch(st.ahead && st.ahead.key !== room.key ? st.ahead : null);
    // The room's state is asked for once a second, so a new song was noticed up to a second late (the
    // lights sat dark meanwhile). With the song nearly over, it is asked again right as it ends, and every
    // 150 ms after that until the next song is there.
    const left = (room.duration || 0) - (room.position || 0);
    if (room.playing && left < 1.2 && left > -3 && !J.edgeTimer)
      J.edgeTimer = setTimeout(() => { J.edgeTimer = null; tick(); }, left > 0 ? left * 1000 + 40 : 150);
  }

  function buildFailed(room, e) {
    console.error('Jam show build failed', e);
    if (J.failedKey !== room.key) toast(`${room.label}: the show could not be built (${e.message}). The lights keep time with what there is.`, true);
    J.failedKey = room.key;
  }

  function adopt(analysis, name, saved, data) {
    S.analysis = analysis; S.name = saved ? name : '';
    S.project = saved || St.newProject(analysis, name);
    St.migrateProject(S.project);
    if (saved && data) St.freshenSaved(data);                     // built by older show logic: built again (in memory)
    $('#projectName').value = name; $('#bpm').value = S.project.bpm.toFixed(2);
    $('#emptyState').hidden = true;
    S.undo.length = 0; S.redo.length = 0; S.view.sel = null; S.view.multi = [];
  }

  /** The album cover's dominant bright color, and a second from the cover (or a neighbor of the
   *  first), as a palette. Colors are pushed to full brightness: the lights want solid color.
   *  A song's show waits at most 3 s for its cover; a cover that comes later still colors the list,
   *  and the show in the room if it is that song's. */
  async function albumPalette(track) {
    const url = track && track.image; if (!url) return null;
    J.art = J.art || {}; if (J.art[url] !== undefined) return J.art[url];
    return Promise.race([coverJob(url), new Promise(r => setTimeout(() => r(null), 3000))]);
  }
  // One read of each cover, however many ask. A cover that could not be fetched is not remembered as
  // colorless (that left a song gray for the whole night when the studio was busy): it is tried again.
  function coverJob(url) {
    J.art = J.art || {}; J.artJobs = J.artJobs || {};
    if (J.art[url] !== undefined) return Promise.resolve(J.art[url]);
    if (!J.artJobs[url]) J.artJobs[url] = readCover(url).then(pal => {
      if (pal !== undefined) {
        J.art[url] = pal;
        const t = J.doc && J.doc.track;
        if (pal && J.live && t && t.image === url && !S.albumPalette) { S.albumPalette = pal; recolor(); }   // came late: the show takes it now
      } else setTimeout(() => { delete J.artJobs[url]; }, 15000);
      return pal || null;
    });
    return J.artJobs[url];
  }
  /** A cover's palette; null when it has no bright color; undefined when it could not be fetched. */
  async function readCover(url) {
    let pal = null;
    try {
      // decoded from the bytes (an <img> waits while the window is hidden)
      const r = await fetch('/api/jam/art?url=' + encodeURIComponent(url));
      if (!r.ok) return undefined;
      const img = await createImageBitmap(await r.blob());
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
    } catch (e) { return undefined; }
    return pal;
  }

  J.albumPalette = albumPalette;                         // for checking a cover's colors by hand

  // The next song, readied while the room still plays this one (Spotify runs a few seconds ahead of the
  // room): its analysis, its cover's colors and a saved show of yours, fetched now, so the change of song
  // costs only the build (25-60 ms). A first listen is not readied: it changes as it is heard.
  function prefetch(t) {
    if (!t || !t.key || t.key === J.key || t.kind === 'live' || (J.pre && J.pre.key === t.key)) return;
    const doc = api('jam/analysis?key=' + encodeURIComponent(t.key));
    const pal = doc.then(d => albumPalette(d.track));
    const proj = doc.then(d => (d.kind === 'custom' && d.project ? fetch('/api/projects/' + encodeURIComponent(d.project)).then(r => r.json()) : null));
    J.pre = { key: t.key, doc, pal, proj };
    doc.catch(() => { if (J.pre && J.pre.key === t.key) J.pre = null; });
    pal.catch(() => {}); proj.catch(() => {});
    Promise.all([doc, pal]).then(([d, pl]) => { if (J.pre && J.pre.key === t.key) J.pre.hex = Effects.solid(mainOf(d.track, pl)); }).catch(() => {});
  }

  // Between songs the room is never fully dark (people walk about the party): as a song rings out, a glow
  // of the next song's main color fades in to 10% and holds through any gap; when the next song starts it
  // stays until its sound begins, then fades away as its lights come up.
  const GLOW = 0.10;
  const mainOf = (track, pal) => (colorsFor(track) || [])[0] || (pal || [])[0] || '#ffffff';
  function soundSpan(a) {
    const e = (a && a.energy) || [], fps = (a && a.feature_fps) || 30;
    let top = 0; for (const v of e) top = Math.max(top, v);
    const th = 0.05 * top; let i0 = e.findIndex(v => v > th), i1 = e.length - 1;
    while (i1 > 0 && e[i1] <= th) i1--;
    return [Math.max(0, i0) / fps, (i1 + 1) / fps];
  }
  function setGlow() {
    // (a first listen has heard only so much: it rings out at the track's end, not where the hearing stops)
    const span = soundSpan(S.analysis), t0 = span[0], inHex = J.glowIn;
    const t1 = J.doc && J.doc.kind === 'live' ? ((J.doc.track || {}).duration || 1e9) : span[1];
    S.glowAt = t => {
      if (inHex && t < t0 + 0.6) return { rgb: Effects.hex2rgb(inHex), level: GLOW * (t <= t0 ? 1 : 1 - (t - t0) / 0.6) };
      const nx = J.pre && J.pre.hex;
      if (nx && t > t1 - 0.5) return { rgb: Effects.hex2rgb(nx), level: GLOW * Math.min(1, (t - (t1 - 0.5)) / 1.5) };
      return null;
    };
  }

  async function load(room) {
    let pre = J.pre && J.pre.key === room.key ? J.pre : null;
    J.glowIn = pre && pre.hex || null;                              // the glow this song's start takes over from
    let doc = pre ? await pre.doc.catch(() => null) : null;
    if (!doc || doc.kind !== room.kind) { pre = null; doc = await api('jam/analysis?key=' + encodeURIComponent(room.key)); }
    J.live = true; J.doc = doc;
    S.albumPalette = pre ? await pre.pal.catch(() => albumPalette(doc.track)) : await albumPalette(doc.track);
    S.jamColors = doc.kind === 'custom' ? null : colorsFor(doc.track); S.jamPairs = doc.kind === 'custom' ? null : pairsFor(doc.track);
    S.jamExtras = extrasFor(S.jamColors, doc.track); S.jamRainbow = rainbowOn(doc.track); S.noLimits = doc.kind !== 'custom' && wildOn(doc.track); J.colorSig = colorSig();
    clearTimeout(J.saveT); J.auto = J.custom = J.secsByHand = false; J.savedAs = null;   // (see J.onEdit)
    if (doc.kind === 'custom' && doc.project) {
      const data = (pre && await pre.proj.catch(() => null)) || await (await fetch('/api/projects/' + encodeURIComponent(doc.project))).json();
      if (data.error) return toast('Jam: ' + data.error, true);
      adopt(data.analysis, data.project, data.saved || null, data);
      if (!data.saved) try { St.autoBuild(true); } catch (e) { buildFailed(room, e); }
      if (J.failedKey !== room.key) toast(`${room.label}: playing your show`);
    } else if (doc.locked) {
      // locked by the user (jam.py JamLibrary.lock): the show exactly as it was kept, on the analysis it was
      // built on; nothing is built, restyled or recolored (it is not saved anywhere either: S.name stays empty)
      adopt(doc.analysis, room.label, null);
      S.project = JSON.parse(JSON.stringify(doc.locked)); St.migrateProject(S.project);
      J.built = sectionKeys();
      if (J.failedKey !== room.key) toast(`${room.label}: its locked show`);
    } else if (doc.light) {
      adopt(doc.analysis, room.label, null);
      buildLight();                                                   // a stopgap until its copy is learned
      if (J.failedKey !== room.key) toast(`${room.label}: following the beat until its full show is ready`);
    } else {
      adopt(doc.analysis, room.label, null);
      applyStyle(doc.track);                                          // a vibe picked for it by hand
      try { St.autoBuild(true); } catch (e) { buildFailed(room, e); }
      J.built = sectionKeys(); J.auto = true;                         // the jam's own show: it follows the sections
      if (J.failedKey !== room.key) toast(doc.kind === 'learned' ? `${room.label}: heard before, full show` : `${room.label}: first listen, building as it plays`);
    }
    setGlow();
    St.fullBrightness();                                            // between songs: every light back to 100%
    J.key = room.key; J.version = doc.version; J.lastFeat = performance.now(); J.style = St.showStyle();
    J.secsSeen = { key: room.key, sig: St.sectionsSig() };          // (section changes from here on are kept: app.js jamSave)
    J.clips = St.clipSig();                                         // (the lights as built: a change to them is a hand edit)
    J.drums = !!(S.analysis && S.analysis.events && S.analysis.events.kick && S.analysis.events.kick.length);
    St.zoomFit(); St.inspector(); St.renderPalettes(); St.draw();
  }

  /** A section moved in a jam (as under review): the jam's own show follows it, rebuilt the way it is when the song
   *  is heard better (reconcile: what changed, the part playing now from the next beat on), its layers dimmed to
   *  say so. Lights changed by hand (a clip added, moved, recolored...) make it your own show: saved as you go
   *  (review.py make_custom), played by the jam from then on, and no longer rebuilt; undone back to the lights as
   *  built, it is the jam's own again. Your own saved shows are never rebuilt. (app.js changed() calls this.) */
  J.onEdit = () => {
    if (!J.live || !J.auto || !J.doc) return;
    const label = (J.doc.track && J.doc.track.name) || 'This song';
    const mine = St.clipSig() !== J.clips;
    if (mine !== J.custom) {
      J.custom = mine; St.draw();
      toast(mine ? `${label}: your own show now. It is saved as you go, and the jam plays it.` : `${label}: back to the jam's own show`);
    }
    if (J.custom || J.savedAs) { clearTimeout(J.saveT); J.saveT = setTimeout(saveCustom, 1200); }
    if (J.custom) return;
    const keys = sectionKeys();
    if (JSON.stringify(keys) === JSON.stringify(J.built)) return;
    J.secsByHand = true;                                            // (a first listen heard better keeps them: refresh)
    J.built = reconcile(J.built, Math.ceil(St.timeToBeat(J.pos()) + 0.1), false);
    J.clips = St.clipSig(); S.forceSend = true; St.draw();
  };
  async function saveCustom() {
    clearTimeout(J.saveT);
    const id = J.doc && J.doc.track && J.doc.track.id; if (!id) return;
    const post = (url, body) => fetch(url, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) }).then(r => r.json());
    try {
      if (J.custom) {
        const d = await post('/api/review/custom', { track_id: id, project: { ...St.P(), handEdited: true } }); if (d.error) throw new Error(d.error);
        J.savedAs = d.name;
      } else if (J.savedAs) {
        const d = await post('/api/review/uncustom', { track_id: id, name: J.savedAs }); if (d.error) throw new Error(d.error);
        J.savedAs = null;
      }
    } catch (e) { toast('Your show was not saved: ' + e.message, true); }
  }

  /** Learning ahead, a song playing before it is learned (the jam just started, a skip to a song not yet
   *  learned) is a stopgap until its copy is: the song's main color glowing slowly up and down (a 4 s swell,
   *  never dark), no beat followed and nothing else worked out, so the PC's effort goes to learning it (its
   *  analysis runs at above-normal priority meanwhile, fetch_ahead.submit). */
  function buildLight() {
    const p = St.P(); if (!p || !S.analysis) return;
    const col = (S.jamColors && S.jamColors[0]) || (S.albumPalette && S.albumPalette[0]) || '#ffffff';
    p.bpm = S.analysis.bpm || 120; p.phase = S.analysis.downbeat_phase || 0; p.beats = (S.analysis.beats || []).slice();
    for (const lane of p.lanes) lane.clips = [];
    p.paletteClips = [];
    p.settings = { ...(p.settings || {}), followLoudness: false };   // full brightness: not dimmed with the music
    const len = Math.max(4, Math.ceil(St.timeToBeat(S.analysis.duration || 240)) + 4);
    const swell = Math.max(2, Math.round(4 * (p.bpm || 120) / 60));      // ~4 s up and down, on the plain grid
    p.lanes[0].clips.push({ id: 'light' + Date.now().toString(36), effect: 'breathe', start: 0, len, opacity: 1, blend: 'normal', fadeIn: 0, fadeOut: 0,
      params: { ...Effects.defaults('breathe'), speed: swell, floor: 0.2, intensity: 1 }, palette: Effects.solidPalette([col]) });
    St.draw();
  }

  const sectionKeys = () => St.P().sections.map(s => `${s.type}@${s.start.toFixed(2)}-${s.end.toFixed(2)}`);

  /** A first listen has learned more: take the new grid and rebuild only what the room has not reached. */
  async function refresh(room, featuresOnly = false) {
    const doc = await api('jam/analysis?key=' + encodeURIComponent(room.key));
    if (!doc.analysis || room.key !== J.key) return;
    const a = doc.analysis, p = St.P();
    S.analysis = a; J.lastFeat = performance.now(); applyGenre(J.doc && J.doc.track);
    if (doc.light) { J.version = doc.version; buildLight(); return; }   // the beat, a little further along
    // your own lights, or sections you set by hand: kept as they are (the song's features above still update)
    if (J.custom || J.secsByHand) { J.version = doc.version; return; }
    if (featuresOnly && doc.version === J.version) { const nb = Math.ceil(St.timeToBeat(J.pos()) + 0.1); St.addMoments(nb); St.addFlavor(nb); J.clips = St.clipSig(); St.draw(); return; }
    p.bpm = a.bpm; p.phase = a.downbeat_phase; p.beats = a.beats.slice();
    p.sections = St.withBuildups(St.tidySections(a.sections));      // a drop with none still gets its buildup
    p.drops = St.dropsOf(p);
    J.secsSeen = { key: room.key, sig: St.sectionsSig() };
    $('#bpm').value = p.bpm.toFixed(2);
    // the genre arrived, or the drums did (the beat pulse goes only where there are drums): rebuild
    const drumsNow = !!(a.events && a.events.kick && a.events.kick.length);
    const style = St.showStyle(), restyled = style !== J.style || drumsNow !== !!J.drums;
    if (style !== J.style) toast(`${(doc.track || {}).name || 'This track'}: ${style} style from here on`);
    if (restyled) St.pickLook();                                   // the look follows the style it now has
    const nowBeat = Math.ceil(St.timeToBeat(J.pos()) + 0.1);
    const keys = reconcile(J.built, nowBeat, restyled);
    St.addMoments(nowBeat);                                        // cuts and entrances this pass heard, still ahead of the room
    St.addFlavor(nowBeat);                                         // and the song's own flavor: spotlights, wobbles, the finale once it is all heard
    J.built = keys; J.version = doc.version; J.style = style; J.drums = drumsNow;
    J.clips = St.clipSig();                                        // (the jam's own rebuild, not a hand edit)
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
    S.glowAt = null;
    S.albumPalette = null; S.jamColors = null; S.jamPairs = null; S.jamExtras = null; S.jamRainbow = false; S.noLimits = false; J.doc = null;
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
