// The side rail's Setup and Party panels.
// Setup: one checklist for a new home (lights, rooms, the map, speakers, Spotify, sound devices, AI models, the phone,
// add-ons), each step with the button that does it, every device picked from a list. It opens by itself while the
// home is not set up, until "Not now". Party: the Party Hub itself, so everything the phone can do is here too.
(() => {
  const St = window.Studio, $ = s => document.querySelector(s);
  const esc = s => String(s ?? '').replace(/[&<>"]/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]));
  const get = async url => (await fetch(url)).json();
  const post = async (url, body) => (await fetch(url, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) })).json();
  const ACTIONS = {
    lights: ['Show lights', () => St.showPanel('lights')],
    rooms: ['Edit rooms', () => St.openRooms()],
    map: ['Open the map', () => window.open('/party/map', 'lss-map', 'width=1200,height=820')],
    speakers: ['Look again', async () => { await fetch('/api/cast/devices?rescan=1'); load(); }],
    spotify: ['Set up Spotify', () => { if ($('#jamPanel').hidden) $('#btnJam').click(); }],
    models: ['Install', () => installModels()],
  };
  let shown = false;
  // the AI models: installed in the background (a few GB); the step shows the progress until it is done
  async function installModels() {
    const r = await post('/api/models/setup', { action: 'install' }).catch(() => ({ error: 'no answer' }));
    if (r.error && r.state !== 'running') St.toast(r.error, true);
    watchModels();
  }
  let modelsTimer = null;
  function watchModels() {
    clearInterval(modelsTimer);
    modelsTimer = setInterval(async () => {
      const s = await get('/api/models/setup').catch(() => null); const el = $('#setupModels');
      if (!s) return;
      if (el) el.textContent = s.state === 'running' ? `Installing (${s.variant || ''}): ${Math.round(100 * s.progress)}% · ${s.step || s.phase}`
        : s.state === 'failed' ? `Did not install: ${s.error}` : s.state === 'done' ? 'Installed' : '';
      if (s.state !== 'running') { clearInterval(modelsTimer); if (s.state === 'done') load(); }
    }, 1500);
  }

  async function load() {
    let d, dev, link, add;
    try { [d, dev, link, add] = await Promise.all([get('/api/setup'), get('/api/audio/devices'), get('/api/party/link'), get('/api/addons')]); }
    catch (e) { $('#setupList').innerHTML = '<div class="hint">Could not reach the studio.</div>'; return; }
    const pick = (id, label, list, value, auto, hint) => `<label class="setup-field"><span>${label}</span>
        <select id="${id}"><option value="">${esc(auto)}</option>${list.map(n => `<option value="${esc(n)}" ${n === value ? 'selected' : ''}>${esc(n)}</option>`).join('')}</select>${hint || ''}</label>`;
    const help = dev.hidden_help || {};
    const hiddenFound = dev.outputs.some(o => o.toLowerCase().includes((dev.jam_hidden_output || help.name || '').toLowerCase()));
    const extra = {
      sound: `<div class="setup-more">
        ${pick('setMic', 'Microphone (listens for the sync check)', dev.inputs, dev.mic_device, `This computer's default${dev.default_input ? ': ' + dev.default_input : ''}`,
          '<button id="setMicTest" class="small">Test</button><span id="setMicLevel" class="hint"></span>')}
        ${pick('setRoomOut', 'The room hears this computer on', dev.outputs, dev.jam_room_output, `This computer's default${dev.default_output ? ': ' + dev.default_output : ''}`)}
        ${pick('setHidden', 'Spotify plays into (nobody hears it)', dev.outputs, dev.jam_hidden_output, help.name ? `${help.name} (the usual one)` : 'The usual one')}
        ${hiddenFound || !help.url ? '' : `<div class="hint">Not on this computer yet: <a href="${esc(help.url)}" target="_blank" rel="noopener">install ${esc(help.label)}</a>, then look again.</div>`}
      </div>`,
      phone: `<div class="setup-more setup-phone">${link.qr_svg || ''}<div><div class="hint">Open this on your phone (same Wi-Fi), then Share › Add to Home Screen:</div>
        <code>${esc(link.url)}</code> <button id="setCopyLink" class="small">Copy</button></div></div>`,
      models: `<div class="setup-more"><span id="setupModels" class="hint"></span></div>`,
      addons: `<div class="setup-more">${(add.addons || []).map(a => `<label class="setup-addon"><span><b>${esc(a.name)}</b><br><span class="hint">${esc(a.on && a.missing ? a.missing : a.about)}</span></span>
        <span class="toggle"><input type="checkbox" data-addon="${esc(a.id)}" ${a.on ? 'checked' : ''}></span></label>`).join('')}</div>`,
    };
    $('#setupList').innerHTML = `<div class="setup-head"><span class="hint">${d.done ? 'All set.' : 'A few steps and your home is ready.'}</span>
        ${d.done ? '' : '<button id="setupLater" class="small">Not now</button>'}</div>` +
      d.items.map((it, i) => `<div class="setup-step ${it.done ? 'done' : ''}">
        <div class="setup-row"><span class="setup-n">${it.done ? '✓' : i + 1}</span>
          <span class="setup-t"><b>${esc(it.title)}</b><br><span class="hint">${esc(it.detail)}</span></span>
          ${ACTIONS[it.id] ? `<button class="small" data-act="${it.id}">${esc(ACTIONS[it.id][0])}</button>` : ''}</div>${extra[it.id] || ''}</div>`).join('');
    for (const b of document.querySelectorAll('#setupList [data-act]')) b.onclick = () => ACTIONS[b.dataset.act][1]();
    const save = (id, key) => { const el = $('#' + id); if (el) el.onchange = async () => { await post('/api/audio/devices', { [key]: el.value }); load(); }; };
    save('setMic', 'mic_device'); save('setRoomOut', 'jam_room_output'); save('setHidden', 'jam_hidden_output');
    const test = $('#setMicTest');
    if (test) test.onclick = async () => {
      $('#setMicLevel').textContent = 'Listening…';
      const r = await get('/api/audio/miclevel').catch(() => ({ error: 'no answer' }));
      $('#setMicLevel').textContent = r.error ? `Could not listen: ${r.error}` : r.level_db < -70 ? 'Heard nothing: is it muted?' : `Hears the room (${r.level_db} dB)`;
    };
    const copy = $('#setCopyLink');
    if (copy) copy.onclick = async () => { try { await navigator.clipboard.writeText(link.url); St.toast('Copied'); } catch (e) { St.toast('Select the address and copy it', true); } };
    for (const sw of document.querySelectorAll('#setupList input[data-addon]')) sw.onchange = async () => { await post('/api/addons', { id: sw.dataset.addon, on: sw.checked }); load(); };
    const later = $('#setupLater');
    if (later) later.onclick = () => { try { localStorage.setItem('lss-setup-later', '1'); } catch (e) { /* not kept */ } St.showPanel('setup'); };
    return d;
  }

  // a panel opened: Setup fills itself in; Party loads the Party Hub the first time
  document.addEventListener('lss-panel', e => {
    if (e.detail === 'setup') load();
    if (e.detail === 'party' && !shown) { shown = true; $('#partyFrame').src = '/party'; }
  });
  // a home not set up yet: the Setup panel opens by itself (until "Not now")
  (async () => {
    let later = false; try { later = localStorage.getItem('lss-setup-later') === '1'; } catch (e) { /* not kept */ }
    if (later) return;
    try { const d = await get('/api/setup'); if (!d.done && document.body.dataset.panel !== 'setup') St.showPanel('setup'); } catch (e) { /* later */ }
  })();
})();
