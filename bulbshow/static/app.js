// Bulb Show: the show list and the one-bulb timeline editor.
// The server works out every color (effects.py) and sends one loop of them as frames; this page
// only draws them, so the bulb on screen is exactly what the real bulb gets.
'use strict';

const $ = id => document.getElementById(id);
const api = (path, opts = {}) => fetch(path, opts).then(r => r.json());
const post = (path, body) => api(path, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body || {}) });

const EFFECTS = {
  color: { name: 'Color', len: 4, defaults: { color: '#7a2cff', level: 0.8, fade: 0 } },
  pulse: { name: 'Pulse', len: 4, defaults: { color: '#ff6a00', level: 1, low: 0.1, period: 2 } },
  lightning: { name: 'Lightning', len: 1.5, defaults: { color: '#dfe8ff', level: 1, flashes: 3 } },
  scene: { name: 'Scene', len: 10, defaults: { scene: null } },
};
const RULER = 22, WAVE = 72, RIBBON = 16, GAP = 8, LANE = 46, ADDROW = 24, HEAD = 64;
const LANES_TOP = RULER + WAVE + RIBBON + GAP;

const S = {
  view: 'home', show: null, frames: [], fps: 20, perHour: null,
  tl: { start: 0, pps: 40 }, sel: null, playhead: 0,
  play: null,                 // {mode: 'here'|'room', ...}
  here: false,                // preview on this PC instead of the speaker and bulb
  audio: null, wave: null, ctx: null, src: null, gain: null,
  speakers: [], bulbs: [], scenes: {}, running: {}, usage: null, shows: [],
  undo: [], redo: [], drag: null,
};

// ---------- small things ---------------------------------------------------------------
function toast(msg, bad) {
  const t = $('toast');
  t.textContent = msg; t.className = 'toast' + (bad ? ' bad' : ''); t.hidden = false;
  clearTimeout(toast.timer); toast.timer = setTimeout(() => { t.hidden = true; }, bad ? 6000 : 2500);
}
const fmt = s => { s = Math.max(0, s || 0); const m = Math.floor(s / 60); return `${m}:${(s - m * 60).toFixed(1).padStart(4, '0')}`; };
const fmtShort = s => { s = Math.round(s || 0); return `${Math.floor(s / 60)}:${String(s % 60).padStart(2, '0')}`; };
const uid = () => 'c' + Math.random().toString(36).slice(2, 9);
const clamp = (v, a, b) => Math.max(a, Math.min(b, v));
function setBulb(el, hex) {
  hex = hex || '#000000';
  const scene = hex.startsWith('s:');
  el.classList.toggle('scene', scene);
  el.title = scene ? `Scene: ${hex.slice(2)} (played by the bulb itself)` : '';
  if (scene) return;
  const n = parseInt(hex.slice(1), 16), r = n >> 16, g = (n >> 8) & 255, b = n & 255;
  const lum = Math.max(r, g, b) / 255;
  el.style.backgroundColor = lum < 0.02 ? '#111318' : hex;
  el.style.boxShadow = lum < 0.02 ? 'none' : `0 0 ${Math.round(10 + 40 * lum)}px ${Math.round(4 + 14 * lum)}px rgba(${r},${g},${b},${0.25 + 0.5 * lum})`;
}
const bulbLabel = b => b && b.id ? `${b.name || b.sku}${b.via === 'cloud' ? ' (cloud)' : b.via === 'virtual' ? ' (virtual)' : ''}` : '';

// ---------- routing ----------------------------------------------------------------------
function route() {
  $('settings').hidden = true;
  const m = location.hash.match(/^#\/show\/([a-z0-9-]+)/);
  if (m) openShow(m[1]); else openHome();
}
window.addEventListener('hashchange', route);

// ---------- home ---------------------------------------------------------------------------
async function openHome() {
  stopHere();
  S.view = 'home'; S.show = null;
  $('editor').hidden = true; $('home').hidden = false;
  document.title = 'Bulb Show';
  S.shows = await api('/api/shows');
  drawCards();
}

function drawCards() {
  const box = $('cards');
  box.innerHTML = '';
  if (!S.shows.length) { box.innerHTML = '<div class="empty">No shows yet</div>'; return; }
  for (const s of S.shows) {
    const r = S.running[s.id];
    const on = r && !['stopped', 'error'].includes(r.state);
    const card = document.createElement('div');
    card.className = 'card' + (on ? ' running' : '');
    const bulb = document.createElement('div'); bulb.className = 'bulb'; setBulb(bulb, on ? r.color : null);
    const name = document.createElement('div'); name.className = 'name'; name.textContent = s.name;
    const meta = document.createElement('div'); meta.className = 'meta';
    const parts = [s.speaker || 'no speaker', bulbLabel(s.bulb) || 'no bulb', s.duration ? fmtShort(s.duration) : 'no sound'];
    if (s.per_hour != null) parts.push(`≈${s.per_hour.toLocaleString()} cloud/h`);
    meta.textContent = parts.join(' · ');
    meta.title = s.per_hour != null ? `Govee cloud commands an hour while it loops (10,000 a day for every show together)` : '';
    const go = document.createElement('button'); go.className = 'icon go';
    go.textContent = on ? '■' : '▶'; go.title = on ? 'Stop' : 'Run, looping';
    go.disabled = !on && !s.ready;
    if (!s.ready && !on) go.title = 'Needs a sound, a speaker and a bulb';
    go.onclick = async e => {
      e.stopPropagation();
      if (on) await post(`/api/stop/${s.id}`);
      else { const res = await post(`/api/run/${s.id}`, { at: 0 }); if (!res.ok) toast(`${s.name} can't start: ${res.error}`, true); }
      poll();
    };
    card.onclick = () => { location.hash = `#/show/${s.id}`; };
    card.append(bulb, name, go, meta);
    if (r && r.error) { const err = document.createElement('div'); err.className = 'err'; err.textContent = r.error; card.append(err); }
    card.dataset.id = s.id;
    box.append(card);
  }
}

$('btnNew').onclick = async () => {
  const s = await post('/api/shows', { name: 'New show' });
  location.hash = `#/show/${s.id}`;
  setTimeout(() => { $('showName').focus(); $('showName').select(); }, 150);
};
$('btnRunAll').onclick = async () => {
  const res = await post('/api/run-all');
  if (!res.ok) toast(Object.entries(res.problems).map(([id, why]) => `${(S.shows.find(s => s.id === id) || {}).name || id}: ${why}`).join(' · '), true);
  poll();
};
$('btnStopAll').onclick = async () => { await post('/api/stop-all'); poll(); };

// ---------- what is running (every page, all the time) ---------------------------------------
async function poll() {
  let res;
  try { res = await api('/api/running'); } catch (e) { return; }
  const was = S.running;
  S.running = res.shows || {}; S.usage = res.usage;
  const at = performance.now();
  for (const r of Object.values(S.running)) r.at = at;
  drawUsage();
  if (S.view === 'home') {
    const changed = JSON.stringify(Object.keys(was).map(k => [k, was[k].state, was[k].error])) !== JSON.stringify(Object.keys(S.running).map(k => [k, S.running[k].state, S.running[k].error]));
    if (changed) { S.shows = await api('/api/shows'); if (S.view === 'home') drawCards(); }
    else for (const card of document.querySelectorAll('.card')) {
      const r = S.running[card.dataset.id];
      if (r) setBulb(card.querySelector('.bulb'), r.color);
    }
  } else if (S.show) {
    const r = S.running[S.show.id];
    const gone = !r || r.state === 'stopped' || r.state === 'error';
    if (S.play && S.play.mode === 'room') {
      if (gone) { S.play = null; if (r && r.error) toast(r.error, true); drawTransport(); }
    } else if (S.play && S.play.mode === 'here' && S.play.bulb) {              // the bulb's side of a preview ended
      if (gone) { stopHere(false); if (r && r.error) toast(r.error, true); drawTransport(); }
    } else if (!S.play && !gone && !r.here) { S.play = { mode: 'room' }; drawTransport(); }
  }
}
setInterval(poll, 400);

function drawUsage() {
  const u = S.usage, el = $('usage');
  const cloud = S.shows.some(s => s.bulb && s.bulb.via === 'cloud');
  el.hidden = !u || (!cloud && !u.count);
  if (el.hidden) return;
  el.textContent = `Cloud ${u.count.toLocaleString()} / ${u.limit.toLocaleString()} today`;
  el.className = 'usage' + (u.exhausted ? ' bad' : u.count > u.limit * 0.8 ? ' warn' : '');
}

// ---------- editor ---------------------------------------------------------------------------
async function openShow(id) {
  stopHere();
  const show = await api(`/api/shows/${id}`);
  if (!show || show.error) { location.hash = ''; return; }
  S.view = 'editor'; S.show = show; S.sel = null; S.undo = []; S.redo = []; S.playhead = 0; S.play = null;
  S.audio = null; S.wave = null; S.frames = [];
  $('home').hidden = true; $('editor').hidden = false;
  document.title = `${show.name} · Bulb Show`;
  $('showName').value = show.name;
  $('volume').value = Math.round((show.volume == null ? 1 : show.volume) * 100);
  $('dropHint').hidden = !!show.duration;
  fillSpeakers(); fillBulbs();
  loadDevices();
  resize(); fitView();
  drawInspector(); drawTransport();
  if (show.duration) loadSound();
  rerender();
}

async function loadSound() {
  const id = S.show.id;
  const buf = await fetch(`/api/shows/${id}/sound.wav?t=${Date.now()}`).then(r => r.arrayBuffer());
  const ctx = audioCtx();
  const audio = await ctx.decodeAudioData(buf);
  if (!S.show || S.show.id !== id) return;
  S.audio = audio;
  // peaks: the loudest sample in every 1/100 s, every channel together
  const rate = 100, n = Math.ceil(audio.duration * rate), peaks = new Float32Array(n);
  const step = audio.sampleRate / rate;
  for (let c = 0; c < audio.numberOfChannels; c++) {
    const d = audio.getChannelData(c);
    for (let i = 0; i < n; i++) {
      let m = 0;
      for (let j = Math.floor(i * step), e = Math.min(d.length, Math.floor((i + 1) * step)); j < e; j += 4) { const v = Math.abs(d[j]); if (v > m) m = v; }
      if (m > peaks[i]) peaks[i] = m;
    }
  }
  let top = 0; for (const p of peaks) if (p > top) top = p;
  S.wave = { peaks, rate, top: top || 1 };
  draw();
}

function audioCtx() {
  if (!S.ctx) S.ctx = new AudioContext({ sampleRate: 44100 });
  return S.ctx;
}

// a dropped or opened file: decoded here, sent as plain 16-bit WAV
async function takeSound(file) {
  if (!S.show) return;
  toast(`Reading ${file.name}…`);
  let audio;
  try { audio = await audioCtx().decodeAudioData(await file.arrayBuffer()); }
  catch (e) { toast(`Can't read ${file.name} as sound`, true); return; }
  const ch = Math.min(2, audio.numberOfChannels), n = audio.length, sr = audio.sampleRate;
  const out = new DataView(new ArrayBuffer(44 + n * ch * 2));
  const w = (o, s) => { for (let i = 0; i < s.length; i++) out.setUint8(o + i, s.charCodeAt(i)); };
  w(0, 'RIFF'); out.setUint32(4, 36 + n * ch * 2, true); w(8, 'WAVE'); w(12, 'fmt ');
  out.setUint32(16, 16, true); out.setUint16(20, 1, true); out.setUint16(22, ch, true); out.setUint32(24, sr, true);
  out.setUint32(28, sr * ch * 2, true); out.setUint16(32, ch * 2, true); out.setUint16(34, 16, true); w(36, 'data'); out.setUint32(40, n * ch * 2, true);
  const data = []; for (let c = 0; c < ch; c++) data.push(audio.getChannelData(c));
  let o = 44;
  for (let i = 0; i < n; i++) for (let c = 0; c < ch; c++) { out.setInt16(o, Math.round(clamp(data[c][i], -1, 1) * 32767), true); o += 2; }
  const res = await fetch(`/api/shows/${S.show.id}/sound?name=${encodeURIComponent(file.name)}`, { method: 'POST', body: out.buffer }).then(r => r.json());
  if (res.error) { toast(res.error, true); return; }
  stopHere();
  S.show.duration = res.duration; S.show.source = file.name;
  if (!S.show.name || S.show.name === 'New show') { S.show.name = file.name.replace(/\.[^.]+$/, ''); $('showName').value = S.show.name; save(); }
  $('dropHint').hidden = true;
  fitView(); loadSound(); rerender(); drawInspector();
}
$('btnOpen').onclick = () => $('fileInput').click();
$('fileInput').onchange = e => { if (e.target.files[0]) takeSound(e.target.files[0]); e.target.value = ''; };
window.addEventListener('dragover', e => { if (e.dataTransfer.types.includes('Files')) { e.preventDefault(); $('dropHint').classList.add('over'); } });
window.addEventListener('dragleave', () => $('dropHint').classList.remove('over'));
window.addEventListener('drop', e => {
  $('dropHint').classList.remove('over');
  if (e.dataTransfer.files.length && S.view === 'editor') { e.preventDefault(); takeSound(e.dataTransfer.files[0]); }
});

// ---------- saving and the server's frames ------------------------------------------------------
function save() {
  clearTimeout(save.timer);
  save.timer = setTimeout(() => { if (S.show) post(`/api/shows/${S.show.id}`, S.show); }, 400);
}
function rerender() {
  clearTimeout(rerender.timer);
  rerender.timer = setTimeout(async () => {
    if (!S.show) return;
    const id = S.show.id;
    const res = await post('/api/render', S.show);
    if (!S.show || S.show.id !== id) return;
    S.frames = res.frames; S.fps = res.fps; S.perHour = res.per_hour;
    const cost = $('cost');
    cost.hidden = res.per_hour == null;
    if (res.per_hour != null) {
      cost.textContent = `≈${res.per_hour.toLocaleString()} cloud/h`;
      cost.className = 'usage' + (res.per_hour * 4 > 10000 ? ' warn' : '');
      cost.title = `Govee cloud commands this show costs per hour while it loops. All shows together get 10,000 a day: about ${Math.floor(10000 / Math.max(1, res.per_hour))} hours of this one.`;
    }
    draw();
  }, 120);
}
function changed() { save(); rerender(); draw(); drawInspector(); }
function beginChange() { S.undo.push(JSON.stringify(S.show.layers)); if (S.undo.length > 100) S.undo.shift(); S.redo = []; }
function undo(redo) {
  const from = redo ? S.redo : S.undo, to = redo ? S.undo : S.redo;
  if (!from.length) return;
  to.push(JSON.stringify(S.show.layers));
  S.show.layers = JSON.parse(from.pop());
  if (!findClip(S.sel)) S.sel = null;
  changed();
}

// ---------- speakers and bulbs -------------------------------------------------------------------
async function loadDevices() {
  api('/api/speakers').then(res => { S.speakers = res.speakers || []; if (res.error) toast(`Speakers: ${res.error}`, true); fillSpeakers(); });
  api('/api/bulbs').then(res => { S.bulbs = res.bulbs || []; if (res.problem) toast(`Govee cloud: ${res.problem}`, true); fillBulbs(); });
}
function fillSpeakers() {
  const sel = $('speaker'), cur = S.show && S.show.speaker;
  const names = S.speakers.filter(s => !s.group).map(s => s.name);
  if (cur && !names.includes(cur)) names.unshift(cur);
  sel.innerHTML = '<option value="">Speaker…</option>' + names.map(n => `<option>${n.replace(/</g, '&lt;')}</option>`).join('');
  sel.value = cur || '';
}
function fillBulbs() {
  const sel = $('bulb'), cur = S.show && S.show.bulb;
  const list = S.bulbs.slice();
  if (cur && !list.some(b => b.id === cur.id)) list.unshift(cur);
  sel.innerHTML = '<option value="">Bulb…</option>' + list.map(b => `<option value="${b.id}">${bulbLabel(b).replace(/</g, '&lt;')}</option>`).join('');
  sel.value = cur ? cur.id : '';
  fillBulbs.list = list;
}
$('speaker').onchange = e => { S.show.speaker = e.target.value; save(); };
$('bulb').onchange = e => { S.show.bulb = (fillBulbs.list || []).find(b => b.id === e.target.value) || null; changed(); };
$('volume').oninput = e => { S.show.volume = e.target.value / 100; if (S.gain) S.gain.gain.value = S.show.volume; save(); };
$('showName').oninput = e => { S.show.name = e.target.value; document.title = `${S.show.name} · Bulb Show`; save(); };

// ---------- playing ------------------------------------------------------------------------------
function now() {
  const p = S.play, dur = S.show ? S.show.duration : 0;
  if (!p || !dur) return S.playhead;
  if (p.mode === 'here') return ((p.offset + S.ctx.currentTime - p.started) % dur + dur) % dur;
  const r = S.running[S.show.id];
  if (!r || r.position == null) return S.playhead;
  return r.state === 'playing' ? (r.position + (performance.now() - r.at) / 1000) % dur : r.position;
}
// Preview on this PC: the sound plays here, and the show's bulb (if it has one) follows it for real. The server
// runs the show on its own clock, with no speaker; the sound starts on that clock once the bulb is ready.
async function play() {
  if (!S.show || !S.show.duration) return;
  if (S.here) {
    if (!S.audio) return;
    stopHere();
    const id = S.show.id;
    if (!(S.show.bulb && S.show.bulb.id)) { startSound(S.playhead, false); drawTransport(); return; }
    clearTimeout(save.timer); await post(`/api/shows/${id}`, S.show);
    const res = await post(`/api/run/${id}`, { at: S.playhead, here: true });
    if (!res.ok) { toast(`Can't start: ${res.error}`, true); return; }
    const mine = S.play = { mode: 'starting', id }; drawTransport();
    const t0 = performance.now();
    while (S.play === mine && performance.now() - t0 < 20000) {
      const q0 = performance.now(), st = await api('/api/running'), q1 = performance.now();
      const r = (st.shows || {})[id];
      if (S.play !== mine) return;                                       // stopped while the bulb got ready
      if (!r || r.state === 'error' || r.state === 'stopped') { S.play = null; drawTransport(); toast((r && r.error) || 'The bulb did not start', true); return; }
      if (r.state === 'playing' && r.heard != null) { startSound(r.heard + (q1 - q0) / 2000, true); drawTransport(); return; }
      await new Promise(ok => setTimeout(ok, 100));
    }
    if (S.play === mine) { S.play = null; post(`/api/stop/${id}`); drawTransport(); toast('The bulb did not start', true); }
  } else {
    clearTimeout(save.timer); await post(`/api/shows/${S.show.id}`, S.show);
    const res = await post(`/api/run/${S.show.id}`, { at: S.playhead });
    if (!res.ok) { toast(`Can't start: ${res.error}`, true); return; }
    S.play = { mode: 'room' };
    poll();
  }
  drawTransport();
}
function startSound(at, bulb) {
  const ctx = audioCtx(); ctx.resume();
  S.gain = ctx.createGain(); S.gain.gain.value = S.show.volume == null ? 1 : S.show.volume; S.gain.connect(ctx.destination);
  S.src = ctx.createBufferSource(); S.src.buffer = S.audio; S.src.loop = true; S.src.connect(S.gain);
  const off = ((at % S.show.duration) + S.show.duration) % S.show.duration;
  S.src.start(0, off);
  S.play = { mode: 'here', offset: off, started: ctx.currentTime, bulb, id: S.show.id };
}
// stops a preview's sound, and (unless told not to) its bulb on the server
function stopHere(bulbToo = true) {
  if (S.src) { try { S.src.stop(); } catch (e) { /* already stopped */ } S.src.disconnect(); S.src = null; }
  const p = S.play;
  if (p && (p.mode === 'here' || p.mode === 'starting')) {
    S.play = null;
    if (bulbToo && (p.bulb || p.mode === 'starting')) post(`/api/stop/${p.id || (S.show && S.show.id)}`);
  }
}
async function stop() {
  if (!S.play) return;
  if (S.play.mode === 'here' || S.play.mode === 'starting') stopHere();
  else { await post(`/api/stop/${S.show.id}`); S.play = null; }
  drawTransport();
}
function drawTransport() {
  $('btnPlay').classList.toggle('on', !!S.play);
  $('btnHere').classList.toggle('on', S.here);
  $('btnPlay').title = S.here ? 'Preview on this PC, looping (Space)' : 'Play on the speaker and bulb, looping (Space)';
}
$('btnPlay').onclick = play;
$('btnStop').onclick = stop;
$('btnHere').onclick = () => { S.here = !S.here; drawTransport(); };

// ---------- the timeline -------------------------------------------------------------------------
const cv = $('tl'), g = cv.getContext('2d');
let W = 0, H = 0;
function resize() {
  const r = cv.getBoundingClientRect(), dpr = window.devicePixelRatio || 1;
  W = r.width; H = r.height;
  cv.width = Math.round(W * dpr); cv.height = Math.round(H * dpr);
  g.setTransform(dpr, 0, 0, dpr, 0, 0);
  draw();
}
window.addEventListener('resize', resize);
function fitView() {
  const dur = (S.show && S.show.duration) || 10;
  const width = (cv.getBoundingClientRect().width || 800) - HEAD - 16;
  S.tl.pps = Math.max(5, width / dur); S.tl.start = 0;
}
const tx = t => HEAD + (t - S.tl.start) * S.tl.pps;
const xt = x => S.tl.start + (x - HEAD) / S.tl.pps;
const layers = () => S.show.layers;
const laneY = i => LANES_TOP + (layers().length - 1 - i) * LANE;
function laneAt(y) {
  const i = layers().length - 1 - Math.floor((y - LANES_TOP) / LANE);
  return i >= 0 && i < layers().length && y >= LANES_TOP ? i : null;
}
function findClip(id) {
  for (let li = 0; li < layers().length; li++) { const c = layers()[li].find(c => c.id === id); if (c) return { clip: c, lane: li }; }
  return null;
}

function draw() {
  if (!S.show || !W) return;
  g.clearRect(0, 0, W, H);
  g.fillStyle = '#0f1014'; g.fillRect(0, 0, W, H);
  const dur = S.show.duration || 0, end = tx(dur);
  // ruler
  g.fillStyle = '#171920'; g.fillRect(0, 0, W, RULER);
  g.font = '11px Segoe UI, system-ui'; g.fillStyle = '#8e95a6'; g.strokeStyle = '#2a2e38';
  const steps = [0.1, 0.25, 0.5, 1, 2, 5, 10, 15, 30, 60];
  const step = steps.find(s => s * S.tl.pps >= 60) || 60;
  for (let t = Math.ceil(S.tl.start / step) * step; tx(t) < W; t += step) {
    const x = Math.round(tx(t)) + 0.5;
    g.beginPath(); g.moveTo(x, RULER - 6); g.lineTo(x, H); g.stroke();
    g.fillText(step < 1 ? t.toFixed(step < 0.5 ? 2 : 1) : fmtShort(t), x + 3, 14);
  }
  // waveform
  const wy = RULER, mid = wy + WAVE / 2;
  if (S.wave) {
    g.fillStyle = '#3d4a7a';
    const { peaks, rate, top } = S.wave;
    for (let x = Math.max(HEAD, 0); x < Math.min(W, end); x++) {
      const a = Math.floor(xt(x) * rate), b = Math.max(a + 1, Math.floor(xt(x + 1) * rate));
      let m = 0; for (let i = a; i < b && i < peaks.length; i++) if (peaks[i] > m) m = peaks[i];
      const h = (m / top) * (WAVE / 2 - 4);
      g.fillRect(x, mid - h, 1, Math.max(1, h * 2));
    }
  }
  // the light, as the bulb will get it
  const ry = RULER + WAVE;
  if (S.frames.length) {
    for (let x = HEAD; x < Math.min(W, end); x++) {
      const f = S.frames[Math.min(S.frames.length - 1, Math.floor(xt(x) * S.fps))] || '#000';
      g.fillStyle = f.startsWith('s:') ? `hsl(${(x * 3) % 360},55%,42%)` : f;    // a scene: the bulb's own, shown as a rainbow
      g.fillRect(x, ry + 2, 1, RIBBON - 4);
    }
  }
  // lanes
  for (let i = 0; i < layers().length; i++) {
    const y = laneY(i);
    g.fillStyle = i % 2 ? '#13151b' : '#15171e'; g.fillRect(HEAD, y, W - HEAD, LANE);
    g.fillStyle = '#171920'; g.fillRect(0, y, HEAD, LANE);
    g.fillStyle = '#5d6474'; g.fillText(`Layer ${i + 1}`, 8, y + LANE / 2 + 4);
    g.strokeStyle = '#2a2e38'; g.beginPath(); g.moveTo(0, y + 0.5); g.lineTo(W, y + 0.5); g.stroke();
    for (const c of layers()[i]) drawClip(c, y);
  }
  const ay = laneY(0) + LANE;
  g.fillStyle = '#5d6474'; g.fillText('+ layer', 8, ay + 16);
  // past the end of the sound
  if (end < W) { g.fillStyle = 'rgba(0,0,0,.45)'; g.fillRect(Math.max(HEAD, end), RULER, W, H); }
  // playhead
  const px = Math.round(tx(now())) + 0.5;
  if (px >= HEAD) {
    g.strokeStyle = '#ff5ea8'; g.lineWidth = 1.5; g.beginPath(); g.moveTo(px, 0); g.lineTo(px, ay); g.stroke(); g.lineWidth = 1;
  }
}
function drawClip(c, y) {
  const x0 = tx(c.start), x1 = tx(c.end), sel = c.id === S.sel;
  if (x1 < HEAD || x0 > W) return;
  const col = c.effect === 'scene' ? '#b58cff' : c.color || EFFECTS[c.effect].defaults.color;
  g.save();
  g.beginPath(); g.rect(HEAD, y, W - HEAD, LANE); g.clip();
  g.fillStyle = '#20232c'; roundRect(x0 + 1, y + 4, x1 - x0 - 2, LANE - 8, 5); g.fill();
  g.fillStyle = col; g.globalAlpha = 0.85; roundRect(x0 + 1, y + 4, Math.max(2, x1 - x0 - 2), 4, 2); g.fill(); g.globalAlpha = 1;
  if (c.effect === 'pulse') {                          // the breaths, drawn small
    g.strokeStyle = col; g.globalAlpha = 0.6; g.beginPath();
    const p = Math.max(0.2, c.period || 2), lo = c.low == null ? 0.1 : c.low, hi = c.level == null ? 1 : c.level;
    for (let x = x0 + 2; x < x1 - 2; x += 2) {
      const t = xt(x) - c.start, lv = lo + (hi - lo) * (0.5 - 0.5 * Math.cos(2 * Math.PI * t / p));
      const yy = y + LANE - 8 - lv * (LANE - 22);
      if (x === x0 + 2) g.moveTo(x, yy); else g.lineTo(x, yy);
    }
    g.stroke(); g.globalAlpha = 1;
  }
  if (sel) { g.strokeStyle = '#ffffff'; g.lineWidth = 1.5; roundRect(x0 + 1, y + 4, x1 - x0 - 2, LANE - 8, 5); g.stroke(); g.lineWidth = 1; }
  g.fillStyle = '#e8eaf0'; g.font = '12px Segoe UI, system-ui';
  const label = c.effect === 'scene' ? (c.scene ? c.scene.name : 'Scene (pick one)') : EFFECTS[c.effect].name;
  if (x1 - x0 > 40) g.fillText(label, Math.max(x0, HEAD) + 7, y + 25);
  g.restore();
}
function roundRect(x, y, w, h, r) {
  g.beginPath(); g.moveTo(x + r, y); g.arcTo(x + w, y, x + w, y + h, r); g.arcTo(x + w, y + h, x, y + h, r);
  g.arcTo(x, y + h, x, y, r); g.arcTo(x, y, x + w, y, r); g.closePath();
}

// where a clip of `len` can go in a lane near `want`: the free stretch closest to it (clips never overlap)
function placeIn(lane, len, want, skipId) {
  const dur = S.show.duration || 0;
  const cs = layers()[lane].filter(c => c.id !== skipId).sort((a, b) => a.start - b.start);
  let best = null, edge = 0;
  const gaps = [];
  for (const c of cs) { gaps.push([edge, c.start]); edge = c.end; }
  gaps.push([edge, dur]);
  for (const [a, b] of gaps) {
    if (b - a < len - 1e-6) continue;
    const s = clamp(want, a, b - len), d = Math.abs(s - want);
    if (!best || d < best.d) best = { s, d };
  }
  return best ? best.s : null;
}

// palette tiles dragged onto a lane
for (const tile of document.querySelectorAll('.tile')) {
  tile.addEventListener('dragstart', e => { e.dataTransfer.setData('text/effect', tile.dataset.effect); e.dataTransfer.effectAllowed = 'copy'; });
}
cv.addEventListener('dragover', e => { if (e.dataTransfer.types.includes('text/effect')) { e.preventDefault(); e.dataTransfer.dropEffect = 'copy'; } });
cv.addEventListener('drop', e => {
  const kind = e.dataTransfer.getData('text/effect');
  if (!kind || !S.show.duration) return;
  e.preventDefault(); e.stopPropagation();
  const r = cv.getBoundingClientRect(), x = e.clientX - r.left, y = e.clientY - r.top;
  addClip(kind, Math.max(0, xt(x)), laneAt(y) ?? 0);
});
function addClip(kind, at, lane) {
  const fx = EFFECTS[kind], dur = S.show.duration;
  const len = Math.min(fx.len, dur);
  beginChange();
  for (let li = lane; ; li++) {
    if (li >= layers().length) layers().push([]);
    const s = placeIn(li, len, at);
    if (s != null) {
      const c = { id: uid(), effect: kind, start: +s.toFixed(3), end: +(s + len).toFixed(3), ...fx.defaults };
      layers()[li].push(c); S.sel = c.id; changed(); return;
    }
  }
}

// pointer: select, move, resize, set the playhead, zoom and pan
cv.addEventListener('mousedown', e => {
  if (!S.show) return;
  const r = cv.getBoundingClientRect(), x = e.clientX - r.left, y = e.clientY - r.top;
  const lane = laneAt(y);
  if (y > laneY(0) + LANE && y < laneY(0) + LANE + ADDROW && x < HEAD + 40) { beginChange(); layers().push([]); changed(); return; }
  if (lane != null && x > HEAD) {
    const t = xt(x);
    const c = layers()[lane].find(c => t >= c.start && t <= c.end);
    if (c) {
      S.sel = c.id;
      const ex = 6 / S.tl.pps;
      const mode = t - c.start < ex && c.end - c.start > 3 * ex ? 'left' : c.end - t < ex && c.end - c.start > 3 * ex ? 'right' : 'move';
      beginChange();
      S.drag = { mode, id: c.id, grab: t - c.start, lane, moved: false };
      drawInspector(); draw(); return;
    }
    S.sel = null; drawInspector();
  }
  if (x > HEAD) { S.drag = { mode: 'seek' }; seekTo(xt(x)); }
});
window.addEventListener('mousemove', e => {
  const d = S.drag;
  if (!d) { hoverCursor(e); return; }
  const r = cv.getBoundingClientRect(), x = e.clientX - r.left, y = e.clientY - r.top, t = xt(x);
  if (d.mode === 'seek') { seekTo(t); return; }
  const f = findClip(d.id); if (!f) return;
  const c = f.clip, dur = S.show.duration;
  const others = layers()[f.lane].filter(o => o.id !== c.id);
  if (d.mode === 'move') {
    const len = c.end - c.start;
    let lane = laneAt(y); if (lane == null) lane = f.lane;
    let s = placeIn(lane, len, t - d.grab, c.id);
    if (s == null) { lane = f.lane; s = placeIn(lane, len, t - d.grab, c.id); }
    if (s == null) return;
    if (lane !== f.lane) { layers()[f.lane].splice(layers()[f.lane].indexOf(c), 1); layers()[lane].push(c); }
    c.start = +s.toFixed(3); c.end = +(s + len).toFixed(3);
  } else if (d.mode === 'left') {
    const lo = Math.max(0, ...others.filter(o => o.end <= c.start + 1e-6).map(o => o.end));
    c.start = +clamp(t, lo, c.end - 0.1).toFixed(3);
  } else {
    const hi = Math.min(dur, ...others.filter(o => o.start >= c.end - 1e-6).map(o => o.start));
    c.end = +clamp(t, c.start + 0.1, hi).toFixed(3);
  }
  d.moved = true;
  draw(); drawInspector(); rerender();
});
window.addEventListener('mouseup', () => {
  const d = S.drag; S.drag = null;
  if (!d || d.mode === 'seek') return;
  if (d.moved) save(); else S.undo.pop();
});
function hoverCursor(e) {
  if (e.target !== cv || !S.show) return;
  const r = cv.getBoundingClientRect(), x = e.clientX - r.left, y = e.clientY - r.top, lane = laneAt(y);
  let cur = 'default';
  if (lane != null && x > HEAD) {
    const t = xt(x), c = layers()[lane].find(c => t >= c.start && t <= c.end);
    if (c) { const ex = 6 / S.tl.pps; cur = (t - c.start < ex || c.end - t < ex) && c.end - c.start > 3 * ex ? 'ew-resize' : 'grab'; }
  } else if (y > laneY(0) + LANE && y < laneY(0) + LANE + ADDROW && x < HEAD + 40) cur = 'pointer';
  cv.style.cursor = cur;
}
function seekTo(t) {
  S.playhead = clamp(t, 0, S.show.duration || 0);
  if (S.play && S.play.mode === 'here') {
    if (!S.play.bulb) play();
    else { clearTimeout(seekTo.timer); seekTo.timer = setTimeout(play, 350); }   // the bulb restarts once the playhead settles
  }
  draw();
}
cv.addEventListener('wheel', e => {
  e.preventDefault();
  const r = cv.getBoundingClientRect(), x = e.clientX - r.left;
  if (e.shiftKey) { S.tl.start = Math.max(0, S.tl.start + e.deltaY / S.tl.pps); }
  else {
    const at = xt(x), k = Math.exp(-e.deltaY * 0.0015);
    S.tl.pps = clamp(S.tl.pps * k, 2, 2000);
    S.tl.start = Math.max(0, at - (x - HEAD) / S.tl.pps);
  }
  draw();
}, { passive: false });

// keys
window.addEventListener('keydown', e => {
  if (e.key === 'Escape') { $('settings').hidden = true; return; }
  if (S.view !== 'editor' || /INPUT|SELECT|TEXTAREA/.test(document.activeElement.tagName)) return;
  if (e.code === 'Space') { e.preventDefault(); S.play ? stop() : play(); }
  else if ((e.key === 'Delete' || e.key === 'Backspace') && S.sel) { deleteSel(); }
  else if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === 'z') { e.preventDefault(); undo(e.shiftKey); }
  else if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === 'y') { e.preventDefault(); undo(true); }
  else if (e.key === 'Home') { seekTo(0); }
});
function deleteSel() {
  const f = findClip(S.sel); if (!f) return;
  beginChange();
  layers()[f.lane].splice(layers()[f.lane].indexOf(f.clip), 1);
  S.sel = null; changed();
}

// ---------- the inspector --------------------------------------------------------------------------
function drawInspector() {
  const box = $('inspector');
  const f = S.show && findClip(S.sel);
  if (!f) {
    box.innerHTML = ''; delete box.dataset.clip;
    if (S.show && S.show.duration) {
      const h = document.createElement('h3'); h.textContent = 'Sound';
      const p = document.createElement('div'); p.className = 'dim'; p.textContent = `${S.show.source || 'sound'} · ${fmt(S.show.duration)}`;
      box.append(h, p);
    }
    return;
  }
  if (box.dataset.clip === S.sel && box.children.length) { syncInspector(f.clip); return; }
  box.dataset.clip = S.sel;
  const c = f.clip, fx = EFFECTS[c.effect];
  box.innerHTML = `<h3>${fx.name}</h3>`;
  const row = (label, input, val, tip) => {
    const r = document.createElement('div'); r.className = 'row'; if (tip) r.title = tip;
    const l = document.createElement('label'); l.textContent = label; r.append(l, input);
    if (val) r.append(val);
    box.append(r); return r;
  };
  const slider = (key, label, min, max, step, show, tip) => {
    const i = document.createElement('input'); i.type = 'range'; i.min = min; i.max = max; i.step = step; i.dataset.key = key;
    const v = document.createElement('span'); v.className = 'val'; v.dataset.val = key; v.dataset.fmt = show;
    i.addEventListener('pointerdown', () => beginChange());
    i.oninput = () => { c[key] = +i.value; v.textContent = SHOW[show](c[key]); save(); rerender(); draw(); };
    row(label, i, v, tip);
  };
  if (c.effect === 'scene') sceneRow(c, row);
  else {
    const color = document.createElement('input'); color.type = 'color'; color.dataset.key = 'color';
    color.addEventListener('click', () => beginChange());
    color.oninput = () => { c.color = color.value; save(); rerender(); draw(); };
    row('Color', color);
    slider('level', 'Brightness', 0, 1, 0.01, 'pct');
  }
  if (c.effect === 'color') slider('fade', 'Fade', 0, 10, 0.1, 'sec', 'Fades in at the start and out at the end over this long');
  if (c.effect === 'pulse') {
    slider('low', 'Low', 0, 1, 0.01, 'pct', 'How dim each breath gets');
    slider('period', 'Breath', 0.2, 10, 0.1, 'sec', 'Seconds for one breath, dim to bright and back');
  }
  if (c.effect === 'lightning') slider('flashes', 'Flashes', 1, 12, 1, 'int', 'Flashes spread across the clip');
  const num = (key, label) => {
    const i = document.createElement('input'); i.type = 'number'; i.step = 0.05; i.className = 'num'; i.dataset.key = key;
    i.onchange = () => setTiming(c, key, +i.value);
    row(label, i);
  };
  num('start', 'Start'); num('length', 'Length');
  const del = document.createElement('button'); del.className = 'small danger'; del.textContent = 'Delete'; del.onclick = deleteSel;
  box.append(del);
  syncInspector(c);
}
// the bulb's own scenes, asked of Govee once per bulb
function sceneRow(c, row) {
  const sel = document.createElement('select');
  sel.style.maxWidth = '10rem';
  const b = S.show.bulb;
  const fill = list => {
    sel.innerHTML = '<option value="">Pick a scene…</option>' + list.map((o, i) => `<option value="${i}">${String(o.name).replace(/</g, '&lt;')}</option>`).join('');
    const at = c.scene ? list.findIndex(o => o.name === c.scene.name && o.instance === c.scene.instance) : -1;
    sel.value = at >= 0 ? String(at) : '';
    if (c.scene && at < 0) { sel.insertAdjacentHTML('afterbegin', `<option value="keep">${String(c.scene.name).replace(/</g, '&lt;')}</option>`); sel.value = 'keep'; }
  };
  sel.onchange = () => {
    const list = S.scenes[b && b.id] || [], o = list[+sel.value];
    if (!o) return;
    beginChange(); c.scene = { name: o.name, instance: o.instance, value: o.value }; changed();
  };
  row('Scene', sel, null, 'The bulb plays it by itself; what it looks like is set in the Govee app');
  if (!b || !b.id) { fill([]); sel.disabled = true; return; }
  if (S.scenes[b.id]) { fill(S.scenes[b.id]); return; }
  fill([]);
  api(`/api/scenes?sku=${encodeURIComponent(b.sku || '')}&id=${encodeURIComponent(b.id)}&via=${encodeURIComponent(b.via || '')}`).then(res => {
    if (res.problem) toast(res.problem, true);
    if (res.scenes && res.scenes.length) S.scenes[b.id] = res.scenes;
    fill(res.scenes || []);
  });
}
const SHOW = { pct: v => `${Math.round(v * 100)}%`, sec: v => `${(+v).toFixed(1)} s`, int: v => `${Math.round(v)}` };
function syncInspector(c) {
  const box = $('inspector'), fx = EFFECTS[c.effect];
  for (const i of box.querySelectorAll('[data-key]')) {
    const k = i.dataset.key;
    const v = k === 'length' ? +(c.end - c.start).toFixed(3) : k === 'start' ? c.start : (c[k] ?? fx.defaults[k]);
    if (document.activeElement !== i) i.value = v;
  }
  for (const v of box.querySelectorAll('[data-val]')) v.textContent = SHOW[v.dataset.fmt](c[v.dataset.val] ?? fx.defaults[v.dataset.val]);
}
function setTiming(c, key, v) {
  const f = findClip(c.id), dur = S.show.duration, len = c.end - c.start;
  beginChange();
  if (key === 'start') {
    const s = placeIn(f.lane, len, clamp(v, 0, dur - len), c.id);
    if (s != null) { c.start = +s.toFixed(3); c.end = +(s + len).toFixed(3); }
  } else {
    const others = layers()[f.lane].filter(o => o.id !== c.id && o.start >= c.end - 1e-6);
    const hi = Math.min(dur, ...others.map(o => o.start));
    c.end = +clamp(c.start + v, c.start + 0.1, hi).toFixed(3);
  }
  changed();
}

// ---------- settings ----------------------------------------------------------------------------------
for (const b of document.querySelectorAll('.btnSettings')) b.onclick = async e => {
  e.stopPropagation();
  const m = $('settings');
  if (!m.hidden) { m.hidden = true; return; }
  const r = b.getBoundingClientRect();
  m.style.top = `${r.bottom + 6}px`; m.style.right = `${window.innerWidth - r.right}px`;
  $('timingWrap').hidden = S.view !== 'editor';
  if (S.show) $('lightMs').value = S.show.light_ms || 0;
  const st = await api('/api/settings');
  $('keyState').textContent = st.has_key ? 'Key saved' : 'No key';
  $('apiKey').value = '';
  m.hidden = false;
};
document.addEventListener('mousedown', e => { if (!$('settings').contains(e.target) && !e.target.classList.contains('btnSettings')) $('settings').hidden = true; });
$('btnKey').onclick = async () => {
  const st = await post('/api/settings', { govee_api_key: $('apiKey').value });
  $('apiKey').value = ''; $('keyState').textContent = st.has_key ? 'Key saved' : 'No key';
  if (S.view === 'editor') loadDevices();
};
$('lightMs').onchange = e => { S.show.light_ms = +e.target.value || 0; save(); };
$('btnDelete').onclick = async () => {
  if (!S.show || !confirm(`Delete "${S.show.name}" and its sound?`)) return;
  await fetch(`/api/shows/${S.show.id}`, { method: 'DELETE' });
  $('settings').hidden = true; location.hash = '';
};
$('btnBack').onclick = () => { location.hash = ''; };

// ---------- every frame -----------------------------------------------------------------------------
function frame() {
  if (S.view === 'editor' && S.show) {
    const t = now();
    $('time').textContent = fmt(t);
    setBulb($('bulbBig'), S.frames.length ? S.frames[Math.min(S.frames.length - 1, Math.floor(t * S.fps))] : null);
    if (S.play) draw();
  }
  requestAnimationFrame(frame);
}
requestAnimationFrame(frame);

// the window's lifeline: the server stops every show and quits when this closes (like the studio)
(function lifeline() {
  const ws = new WebSocket(`ws://${location.host}/ws`);
  ws.onclose = () => setTimeout(lifeline, 2000);
})();

route();
poll();
