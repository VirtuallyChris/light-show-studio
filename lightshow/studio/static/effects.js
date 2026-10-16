/* Light Show Studio: effect presets and rendering engine.
 *
 * Every effect renders into an RGBA float buffer (values 0..1) for one light of
 * n pixels. Alpha lets overlays (comets, sparkles, strobes) sit on top of base
 * effects. Timing is expressed in beats so everything follows the song's grid.
 *
 * Render context `c`:
 *   n         pixel count            out    Float32Array(n*4) RGBA to fill
 *   t         song time (s)          beat   fractional beat index in the song
 *   phase     0..1 position inside the current beat
 *   bar, barPhase                    clipT  0..1 progress through the clip
 *   clipBeats length of clip in beats;  clipBeat  beats since clip start
 *   p         effect params          pal    palette, array of [r,g,b] 0..1
 *   energy    0..1 loudness at t     bands  12 band energies 0..1
 *   kick      envelope 1 at each beat decaying to 0
 *   rnd(k)    deterministic random 0..1 for an integer key
 */
const Effects = (() => {
  const PALETTES = {
    Rainbow:  ['#ff0000', '#ff8000', '#ffff00', '#00ff00', '#00c8ff', '#0040ff', '#8000ff'],
    Neon:     ['#ff2fa0', '#00ffd0', '#8a2be2', '#ffe600', '#0080ff'],
    Sunset:   ['#ff4e00', '#ff9a00', '#ff2d95', '#7a1fa2', '#2b0a3d'],
    Ocean:    ['#0028ff', '#00c8ff', '#00ffa0', '#3c00c8', '#0078dc'],
    Fire:     ['#ff1e00', '#ff7800', '#ffc814', '#c80000', '#ff460a'],
    Forest:   ['#0a7f3f', '#7ed957', '#e0ff4f', '#134f26', '#2e8b57'],
    Candy:    ['#ff6ec7', '#ffd166', '#06d6a0', '#118ab2', '#ef476f'],
    Ice:      ['#e0f7ff', '#8ad8ff', '#3d8bff', '#b39dff', '#ffffff'],
    'Red/Blue': ['#ff0000', '#0040ff'],
    'Purple/Gold': ['#7a00ff', '#ffb300'],
    'Cyan/Magenta': ['#00e5ff', '#ff00c8'],
    Mono:     ['#ffffff', '#808080'],
    Rasta:    ['#ff0000', '#ffd700', '#00a000'],
    Miami:    ['#ff5fd2', '#00e0ff', '#ffe066', '#a66bff'],
    Toxic:    ['#39ff14', '#c6ff00', '#00ff9f', '#0aff0a'],
    Blood:    ['#ff0000', '#8a0000', '#ff3030', '#400000'],
  };
  const BEAT_OPTS = [0.25, 0.5, 1, 2, 4, 8, 16, 32, 64];

  /** Lights look best in solid color: every palette color is pushed to full saturation and
   *  brightness, keeping its hue. Greys become white (light ones) or black (dark ones). */
  function solid(hexColor) {
    const v = parseInt(hexColor.slice(1), 16);
    const r = ((v >> 16) & 255) / 255, g = ((v >> 8) & 255) / 255, b = (v & 255) / 255;
    const mx = Math.max(r, g, b), mn = Math.min(r, g, b), d = mx - mn, s = mx ? d / mx : 0;
    if (s < 0.2) return mx > 0.55 ? '#ffffff' : '#000000';
    let h = mx === r ? ((g - b) / d) % 6 : mx === g ? (b - r) / d + 2 : (r - g) / d + 4;
    h = ((h * 60) + 360) % 360;
    const f = n => { const k = (n + h / 60) % 6; return Math.round(255 * (1 - Math.max(0, Math.min(k, 4 - k, 1)))); };
    return '#' + [f(5), f(3), f(1)].map(x => x.toString(16).padStart(2, '0')).join('');
  }
  const solidPalette = cols => cols.map(solid).filter((c, i, all) => all.indexOf(c) === i);
  for (const k of Object.keys(PALETTES)) PALETTES[k] = solidPalette(PALETTES[k]);

  // ---------- helpers ----------------------------------------------------------
  const clamp = (x, a = 0, b = 1) => x < a ? a : x > b ? b : x;
  const lerp = (a, b, t) => a + (b - a) * t;
  const frac = x => x - Math.floor(x);
  function hex2rgb(h) {
    const v = parseInt(h.slice(1), 16);
    return [((v >> 16) & 255) / 255, ((v >> 8) & 255) / 255, (v & 255) / 255];
  }
  function hsv(h, s, v) {
    h = frac(h); const i = Math.floor(h * 6), f = h * 6 - i;
    const p = v * (1 - s), q = v * (1 - s * f), t = v * (1 - s * (1 - f));
    switch (i % 6) {
      case 0: return [v, t, p]; case 1: return [q, v, p]; case 2: return [p, v, t];
      case 3: return [p, q, v]; case 4: return [t, p, v]; default: return [v, p, q];
    }
  }
  /** gradient sample through the palette, x wraps around */
  function palAt(pal, x) {
    const m = pal.length; if (m === 1) return pal[0];
    x = frac(x) * m; const i = Math.floor(x), f = x - i; const a = pal[i % m], b = pal[(i + 1) % m];
    return [lerp(a[0], b[0], f), lerp(a[1], b[1], f), lerp(a[2], b[2], f)];
  }
  const palIdx = (pal, i) => pal[((Math.floor(i) % pal.length) + pal.length) % pal.length];
  function hash(k) { k = (k ^ 0x9e3779b9) >>> 0; k = Math.imul(k ^ (k >>> 16), 0x85ebca6b) >>> 0; k = Math.imul(k ^ (k >>> 13), 0xc2b2ae35) >>> 0; return ((k ^ (k >>> 16)) >>> 0) / 4294967296; }
  function set(out, i, r, g, b, a = 1) { const j = i * 4; out[j] = r; out[j + 1] = g; out[j + 2] = b; out[j + 3] = a; }
  function add(out, i, r, g, b, a = 1) { const j = i * 4; out[j] += r; out[j + 1] += g; out[j + 2] += b; out[j + 3] = Math.max(out[j + 3], a); }
  function fillAll(out, n, r, g, b, a = 1) { for (let i = 0; i < n; i++) set(out, i, r, g, b, a); }
  function clear(out) { out.fill(0); }
  /** direction helpers: x in 0..1 along strip -> 0..1 in effect space */
  function dirX(c, x) {
    switch (c.p.direction) {
      case 'rev': return 1 - x;
      case 'out': return Math.abs(x - 0.5) * 2;      // center = 0, ends = 1
      case 'in': return 1 - Math.abs(x - 0.5) * 2;   // ends = 0, center = 1
      default: return x;
    }
  }
  const DIRS = { fwd: 'forward', rev: 'reverse', out: 'center out', in: 'ends in' };

  // common param definitions
  const P = {
    speed: (def = 4, label = 'Cycle (beats)') => ({ type: 'beats', def, label }),
    intensity: (def = 1) => ({ type: 'range', def, min: 0, max: 1, step: 0.01, label: 'Intensity' }),
    size: (def = 0.3, label = 'Size') => ({ type: 'range', def, min: 0.02, max: 1, step: 0.01, label }),
    direction: (def = 'fwd') => ({ type: 'select', def, options: DIRS, label: 'Direction' }),
    color: () => ({ type: 'select', def: 'cycle', options: { cycle: 'palette cycle', gradient: 'palette gradient', first: 'first color', second: 'second color' }, label: 'Color' }),
    decay: (def = 0.5) => ({ type: 'range', def, min: 0.05, max: 1, step: 0.01, label: 'Decay' }),
    count: (def = 2, max = 8) => ({ type: 'range', def, min: 1, max, step: 1, label: 'Count' }),
    bool: (label, def = false) => ({ type: 'bool', def, label }),
  };
  function colorFor(c, x, k) {
    switch (c.p.color) {
      case 'gradient': return palAt(c.pal, x);
      case 'first': return c.pal[0];
      case 'second': return c.pal[1 % c.pal.length];
      case 'rainbow': return hsv(x, 1, 1);
      default: return palIdx(c.pal, k);
    }
  }

  // ---------- effects ---------------------------------------------------------
  const E = [];
  const def = (e) => E.push(e);

  // ===== SLOW =====
  def({ id: 'solid', name: 'Solid color', cat: 'slow', desc: 'One palette color, optionally cycling per N beats.', swatch: ['#ff2fa0', '#ff2fa0'],
    params: { speed: P.speed(16, 'Change every (beats)'), color: P.color(), intensity: P.intensity() },
    render(c) { const k = Math.floor(c.beat / c.p.speed); const col = colorFor(c, 0.5, k); fillAll(c.out, c.n, col[0] * c.p.intensity, col[1] * c.p.intensity, col[2] * c.p.intensity); } });

  def({ id: 'gradient', name: 'Gradient sweep', cat: 'slow', desc: 'The palette gradient scrolls slowly along the strip.', swatch: ['#ff4e00', '#7a1fa2'],
    params: { speed: P.speed(16, 'One length per (beats)'), direction: P.direction(), stretch: { type: 'range', def: 1, min: 0.25, max: 4, step: 0.05, label: 'Stretch' }, intensity: P.intensity() },
    render(c) { const s = c.beat / c.p.speed; for (let i = 0; i < c.n; i++) { const x = dirX(c, i / Math.max(1, c.n - 1)); const col = palAt(c.pal, x / c.p.stretch - s); set(c.out, i, col[0] * c.p.intensity, col[1] * c.p.intensity, col[2] * c.p.intensity); } } });

  def({ id: 'aurora', name: 'Aurora', cat: 'slow', desc: 'Slow drifting ribbons of palette color, like northern lights.', swatch: ['#00ffa0', '#3c00c8'],
    params: { speed: P.speed(16), intensity: P.intensity(0.9), depth: { type: 'range', def: 0.5, min: 0, max: 1, step: 0.01, label: 'Depth' } },
    render(c) { const ph = c.beat / c.p.speed * 2 * Math.PI; for (let i = 0; i < c.n; i++) { const x = i / Math.max(1, c.n - 1); const w = 0.5 + 0.5 * Math.sin(x * 6.3 + ph) * Math.sin(x * 2.1 - ph * 0.7); const col = palAt(c.pal, x * 0.6 + 0.15 * Math.sin(ph * 0.5 + x * 3)); const v = c.p.intensity * (1 - c.p.depth + c.p.depth * w); set(c.out, i, col[0] * v, col[1] * v, col[2] * v); } } });

  def({ id: 'breathe', name: 'Breathe', cat: 'slow', desc: 'The whole strip swells and fades once per cycle.', swatch: ['#3d8bff', '#0a1a40'],
    params: { speed: P.speed(4), color: P.color(), floor: { type: 'range', def: 0.02, min: 0, max: 1, step: 0.01, label: 'Floor' }, intensity: P.intensity() },
    render(c) { const ph = frac(c.beat / c.p.speed); const v = c.p.floor + (1 - c.p.floor) * (0.5 - 0.5 * Math.cos(ph * 2 * Math.PI)); const col = colorFor(c, ph, Math.floor(c.beat / c.p.speed)); fillAll(c.out, c.n, col[0] * v * c.p.intensity, col[1] * v * c.p.intensity, col[2] * v * c.p.intensity); } });

  def({ id: 'colorwash', name: 'Color wash', cat: 'slow', desc: 'Crossfades smoothly from palette color to color.', swatch: ['#ff6ec7', '#06d6a0'],
    params: { speed: P.speed(8, 'Per color (beats)'), intensity: P.intensity() },
    render(c) { const x = c.beat / c.p.speed; const col = palAt(c.pal, x / c.pal.length); fillAll(c.out, c.n, col[0] * c.p.intensity, col[1] * c.p.intensity, col[2] * c.p.intensity); } });

  def({ id: 'wave', name: 'Wave', cat: 'slow', desc: 'A brightness wave rolls along the strip in the palette gradient.', swatch: ['#0028ff', '#00c8ff'],
    params: { speed: P.speed(4), direction: P.direction(), waves: { type: 'range', def: 1.5, min: 0.25, max: 6, step: 0.25, label: 'Waves' }, floor: { type: 'range', def: 0.0, min: 0, max: 1, step: 0.01, label: 'Floor' } },
    render(c) { const s = c.beat / c.p.speed; for (let i = 0; i < c.n; i++) { const x = dirX(c, i / Math.max(1, c.n - 1)); const v = c.p.floor + (1 - c.p.floor) * (0.5 + 0.5 * Math.sin((x * c.p.waves - s) * 2 * Math.PI)); const col = palAt(c.pal, x * 0.5 - s * 0.25); set(c.out, i, col[0] * v, col[1] * v, col[2] * v); } } });

  // Single lights coming on and going out, slowly, here and there along the strip: white stars over
  // whatever is below. For calm songs, where anything quicker would break the spell.
  def({ id: 'starlight', name: 'Starlight', cat: 'slow', desc: 'Single segments glow up and fade away slowly at random spots along the strip, in white (or warm white, or the palette), like stars coming out. Made for calm songs: put it over a dim bed.', swatch: ['#ffffff', '#0b1020'],
    params: { density: { type: 'range', def: 0.12, min: 0.01, max: 0.6, step: 0.01, label: 'How many stars' },
      life: { type: 'range', def: 3, min: 0.5, max: 32, step: 0.5, label: 'Each star lasts (beats)' },
      tint: { type: 'select', def: 'white', options: { white: 'white', warm: 'warm white', palette: 'the palette' }, label: 'Color' } },
    render(c) {
      clear(c.out);
      for (let i = 0; i < c.n; i++) {
        let v = 0, k = 0;
        // two overlapping cycles per segment, each with its own length and start, so stars never pulse together
        for (let j = 0; j < 2; j++) {
          const life = c.p.life * (0.7 + 0.6 * hash(i * 31 + j * 7)), u = c.beat / life + hash(i * 7 + j * 131), cyc = Math.floor(u);
          if (hash(cyc * 977 + i * 13 + j * 3) >= c.p.density) continue;
          const s = Math.sin(frac(u) * Math.PI), e = s * s * (0.55 + 0.45 * hash(cyc * 53 + i));      // a slow glow, each star its own brightness
          if (e > v) { v = e; k = cyc + i; }
        }
        if (v < 0.01) continue;
        const col = c.p.tint === 'palette' ? colorFor(c, i / c.n, k) : c.p.tint === 'warm' ? [1, 0.8, 0.55] : [1, 1, 1];
        set(c.out, i, col[0] * v, col[1] * v, col[2] * v, v);
      }
    } });

  // ---- Effects that belong to one kind of song or one kind of moment, placed only where they fit ----
  def({ id: 'spotlight', name: 'Vocal spotlight', cat: 'slow', desc: 'A soft warm glow in the middle of every strip that swells with the singer\'s voice (the separated vocal track), like a spotlight on a singer alone on stage.', swatch: ['#ffd9a0', '#000000'],
    params: { width: P.size(0.35, 'Width at full voice'), tint: { type: 'select', def: 'warm', options: { warm: 'warm white', white: 'white', palette: 'the palette' }, label: 'Color' } },
    render(c) {
      clear(c.out); const m = c.music || {}, v = clamp(m.vocalLevel ?? 0.5); if (v < 0.02) return;
      const w = Math.max(0.08, c.p.width * (0.45 + 0.55 * v)), col = c.p.tint === 'palette' ? c.pal[0] : c.p.tint === 'white' ? [1, 1, 1] : [1, 0.78, 0.5];
      for (let i = 0; i < c.n; i++) { const x = c.n <= 1 ? 0 : (i / (c.n - 1)) - 0.5, g = Math.exp(-((x / w) ** 2) * 2.2) * (0.25 + 0.75 * v); if (g > 0.01) set(c.out, i, col[0] * g, col[1] * g, col[2] * g, g); }
    } });

  def({ id: 'wobble', name: 'Wobble', cat: 'fast', desc: 'Pulses at the bass wobble\'s own speed (found on the separated bass track), not the beat\'s: the light opens out from the middle and closes again with every wobble.', swatch: ['#7c3aed', '#000000'],
    params: { per: { type: 'select', def: '2', options: { '1.5': '1.5 a beat', '2': '2 a beat', '3': '3 a beat (triplets)', '4': '4 a beat', '6': '6 a beat', '8': '8 a beat' }, label: 'Wobbles (when not found)' }, color: P.color() },
    render(c) {
      clear(c.out); const m = c.music || {}, per = (m.wobble && m.wobble.per) || parseFloat(c.p.per) || 2;
      const u = c.beat * per, env = Math.pow(0.5 - 0.5 * Math.cos(2 * Math.PI * u), 1.4) * (0.45 + 0.55 * clamp((m.bassLevel ?? 0.7) * 1.2));
      const col = colorFor(c, 0.5, Math.floor(u)), half = 0.12 + 0.38 * env;
      for (let i = 0; i < c.n; i++) { const x = Math.abs((i + 0.5) / c.n - 0.5); if (x <= half) { const v = env * (1 - 0.5 * x / Math.max(1e-3, half)); set(c.out, i, col[0] * v, col[1] * v, col[2] * v, v); } }
    } });

  def({ id: 'deep808', name: '808 glow', cat: 'medium', desc: 'On every 808 (a bass hit that holds, low and long) the whole room sinks into one deep color that dies away as slowly as the 808 does.', swatch: ['#7a0000', '#000000'],
    params: { color: P.color(), depth: { type: 'range', def: 0.75, min: 0.2, max: 1, step: 0.01, label: 'Brightness' } },
    render(c) {
      clear(c.out); const h = (c.music || {}).b808; if (!h || h.index < 0) return;
      const hold = Math.max(0.4, h.hold || 0.8), v = c.p.depth * (h.age < 0.04 ? h.age / 0.04 : Math.exp(-(h.age - 0.04) / (hold * 0.9))) * (0.6 + 0.4 * h.strength);
      if (v < 0.01) return; const col = colorFor(c, 0.5, h.index);
      for (let i = 0; i < c.n; i++) { const x = Math.abs((i + 0.5) / c.n - 0.5) * 2, g = v * (1 - 0.35 * x); set(c.out, i, col[0] * g, col[1] * g, col[2] * g, g); }
    } });

  def({ id: 'ticks', name: 'Hat ticks', cat: 'fast', desc: 'Tiny white ticks racing along the strip through a trap hi-hat roll, as fast as the roll.', swatch: ['#ffffff', '#000000'],
    params: { rate: { type: 'range', def: 6, min: 2, max: 12, step: 1, label: 'Ticks a beat (when not found)' }, travel: { type: 'range', def: 0.5, min: 0.1, max: 2, step: 0.05, label: 'Crossing (beats)' } },
    render(c) {
      clear(c.out); const m = c.music || {}, rate = (m.hatroll && m.hatroll.rate) || c.p.rate;
      const u = c.beat * rate, k0 = Math.floor(u), back = Math.ceil(c.p.travel * rate) + 1;
      for (let k = k0; k > k0 - back; k--) {
        const age = (c.beat - k / rate) / c.p.travel; if (age < 0 || age > 1) continue;
        const dir = hash(k * 13) < 0.5, x = dir ? age : 1 - age, i = Math.min(c.n - 1, Math.floor(x * c.n)), v = 1 - age * 0.6;
        set(c.out, i, v, v, v, v); if (c.n > 20) { const j = dir ? i - 1 : i + 1; if (j >= 0 && j < c.n) set(c.out, j, v * 0.35, v * 0.35, v * 0.35, v * 0.35); }
      }
    } });

  def({ id: 'candle', name: 'Candlelight', cat: 'slow', desc: 'A warm, low, uneven flicker, segment by segment, like a room lit by candles. For acoustic and folk songs.', swatch: ['#ff9a3c', '#3a1500'],
    params: { level: { type: 'range', def: 0.45, min: 0.1, max: 1, step: 0.01, label: 'Brightness' } },
    render(c) {
      for (let i = 0; i < c.n; i++) {
        // two slow random drifts and a quicker shiver, on seconds (a flame does not keep time)
        const t = c.t, n1 = Math.sin(t * 1.3 + hash(i * 7) * 6.28) * 0.5 + 0.5, n2 = Math.sin(t * 3.7 + hash(i * 11) * 6.28) * 0.5 + 0.5;
        const shiver = hash(Math.floor(t * 12) * 131 + i) * 0.15, dip = hash(Math.floor(t * 1.5) * 977 + i) < 0.06 ? 0.55 : 1;
        const v = c.p.level * (0.55 + 0.3 * n1 + 0.15 * n2 - shiver) * dip;
        set(c.out, i, v, v * 0.55, v * 0.16);
      }
    } });

  def({ id: 'thunder', name: 'Thunder', cat: 'accent', desc: 'A double white flicker, then black to the end of the clip before the colors come back: lightning and its thunder, for the darkest songs\' biggest hits.', swatch: ['#ffffff', '#000000'],
    params: {},
    render(c) {
      const s = c.t - c.clipT0; let v = 0;
      if (s < 0.06) v = 1; else if (s >= 0.13 && s < 0.2) v = 0.85; else if (s >= 0.2 && s < 0.45) v = 0.85 * Math.exp(-(s - 0.2) / 0.08);
      fillAll(c.out, c.n, v, v, v, 1);
    } });

  def({ id: 'heartbeat', name: 'Heartbeat', cat: 'medium', desc: 'On every kick, a strong pulse and a softer one just after (lub-dub), in a deep color: for a quiet stretch where only a kick plays.', swatch: ['#b00020', '#000000'],
    params: { color: P.color() },
    render(c) {
      clear(c.out); const h = (c.music || {}).kick; if (!h || h.index < 0) return;
      const a = h.age, v = Math.exp(-a / 0.11) + (a > 0.2 ? 0.55 * Math.exp(-(a - 0.2) / 0.13) : 0); if (v < 0.01) return;
      const col = colorFor(c, 0.5, 0), g = Math.min(1, v);
      for (let i = 0; i < c.n; i++) { const x = Math.abs((i + 0.5) / c.n - 0.5) * 2, w = g * (1 - 0.5 * x); set(c.out, i, col[0] * w, col[1] * w, col[2] * w, w); }
    } });

  def({ id: 'fireworks', name: 'Fireworks', cat: 'room', desc: 'Rockets climb the strip and burst into falling sparks of the palette\'s colors, a few a bar. For the last drop of a song that goes all out.', swatch: ['#ff3b3b', '#ffd23b'],
    params: { every: P.speed(2, 'A rocket every (beats)'), life: { type: 'range', def: 3, min: 1, max: 8, step: 0.5, label: 'Each lasts (beats)' } },
    render(c) {
      clear(c.out);
      const k0 = Math.floor(c.beat / c.p.every), back = Math.ceil(c.p.life / c.p.every);
      for (let k = k0; k > k0 - back - 1; k--) {
        const age = (c.beat - k * c.p.every) / c.p.life; if (age < 0 || age > 1) continue;
        const x0 = 0.25 + 0.5 * hash(k * 37 + 3), from = hash(k * 11) < 0.5 ? 0 : 1, col = palIdx(c.pal, Math.floor(hash(k * 53) * 8));
        if (age < 0.18) {                                             // the rocket: a white point climbing to where it bursts
          const x = from + (x0 - from) * (age / 0.18), i = Math.min(c.n - 1, Math.floor(x * c.n)); add(c.out, i, 1, 0.9, 0.7, 1);
        } else {                                                      // the burst: sparks flying out and fading, twinkling as they go
          const b = (age - 0.18) / 0.82, r = 0.04 + 0.22 * Math.sqrt(b), fade = Math.pow(1 - b, 1.6);
          for (let i = 0; i < c.n; i++) {
            const d = Math.abs((i + 0.5) / c.n - x0); if (d > r + 0.03) continue;
            const ring = Math.exp(-(((d - r * 0.85) / 0.05) ** 2)) + (b < 0.15 ? Math.max(0, 1 - d / 0.05) : 0);
            const tw = 0.6 + 0.4 * hash(Math.floor(c.beat * 16) * 7 + i * 3 + k), v = Math.min(1, ring) * fade * tw, w = 0.3 * v * (1 - b);
            if (v > 0.02) add(c.out, i, col[0] * v + w, col[1] * v + w, col[2] * v + w, v);
          }
        }
      }
    } });

  def({ id: 'twinkle', name: 'Twinkle', cat: 'slow', desc: 'Soft random sparkles fading in and out over the base color.', swatch: ['#e0f7ff', '#1b2a4a'],
    params: { density: { type: 'range', def: 0.3, min: 0.02, max: 1, step: 0.01, label: 'Density' }, speed: P.speed(2, 'Twinkle life (beats)'), base: { type: 'range', def: 0.15, min: 0, max: 1, step: 0.01, label: 'Base level' } },
    render(c) { const base = c.pal[0]; for (let i = 0; i < c.n; i++) { let v = 0; for (let k = 0; k < 3; k++) { const life = c.p.speed; const slot = Math.floor(c.beat / life + hash(i * 7 + k * 131) ); const on = hash(slot * 977 + i * 13 + k * 3) < c.p.density; if (on) { const ph = frac(c.beat / life + hash(i * 7 + k * 131)); v = Math.max(v, Math.sin(ph * Math.PI)); } } const col = palIdx(c.pal, 1 + i % (c.pal.length - 1 || 1)); set(c.out, i, base[0] * c.p.base + col[0] * v, base[1] * c.p.base + col[1] * v, base[2] * c.p.base + col[2] * v); } } });

  def({ id: 'ember', name: 'Embers', cat: 'slow', desc: 'Glowing flicker like a bed of coals, colors from the palette.', swatch: ['#ff1e00', '#ffc814'],
    params: { speed: P.speed(1, 'Flicker rate (beats)'), intensity: P.intensity(0.9) },
    render(c) { const tt = c.beat / c.p.speed * 3; for (let i = 0; i < c.n; i++) { const a = Math.floor(tt + hash(i)), f = frac(tt + hash(i)); const v0 = hash(a * 31 + i * 7), v1 = hash((a + 1) * 31 + i * 7); const v = lerp(v0, v1, f * f * (3 - 2 * f)); const col = palAt(c.pal, v * 0.35); const b = (0.25 + 0.75 * v) * c.p.intensity; set(c.out, i, col[0] * b, col[1] * b, col[2] * b); } } });

  // ===== MEDIUM =====
  def({ id: 'pulse', name: 'Beat pulse', cat: 'medium', desc: 'Blooms on the real drums: the kick from the center, the snare from the ends. On a song whose drums are not known yet it pulses on the beat.', swatch: ['#ffffff', '#ff2fa0'],
    params: { follow: { type: 'select', def: 'drums', options: { drums: 'kick and snare', kick: 'kick only', beat: 'every beat' }, label: 'Follows' },
      every: P.speed(1, 'Every (beats)'), direction: P.direction('out'), size: P.size(0.7), decay: P.decay(0.4), color: P.color(), floor: { type: 'range', def: 0.0, min: 0, max: 1, step: 0.01, label: 'Floor' } },
    render(c) {
      const m = c.music || {};
      if (c.p.follow !== 'beat' && m.hasDrums) {
        // each hit is lit where it was played; decay is in seconds here, so fast songs stay crisp
        const tau = c.p.decay * 0.25 + 0.03;
        const hit = h => (h && h.index >= 0 ? Math.exp(-h.age / tau) * (0.55 + 0.45 * h.strength) : 0);
        const ek = hit(m.kick), es = c.p.follow === 'drums' ? hit(m.snare) : 0;
        const ck = colorFor(c, 0.3, Math.max(0, m.kick ? m.kick.index : 0)), cs = colorFor(c, 0.8, Math.max(0, m.snare ? m.snare.index : 0) + 1);
        for (let i = 0; i < c.n; i++) {
          const x = dirX(c, i / Math.max(1, c.n - 1));
          const gk = ek * Math.exp(-1 * (x / (c.p.size + 0.01)) ** 2), gs = es * Math.exp(-1 * ((1 - x) / (c.p.size * 0.6 + 0.01)) ** 2);
          set(c.out, i, ck[0] * (c.p.floor + gk) + cs[0] * gs, ck[1] * (c.p.floor + gk) + cs[1] * gs, ck[2] * (c.p.floor + gk) + cs[2] * gs);
        }
        return;
      } const k = Math.floor(c.beat / c.p.every); const ph = frac(c.beat / c.p.every) * c.p.every; const env = Math.exp(-ph / (c.p.decay * 0.5 + 0.02)); const col = colorFor(c, 0.5, k); for (let i = 0; i < c.n; i++) { const x = dirX(c, i / Math.max(1, c.n - 1)); const g = Math.exp(-1 * ((x) / (c.p.size + 0.01)) ** 2); const v = c.p.floor + env * g; set(c.out, i, col[0] * v, col[1] * v, col[2] * v); } } });

  def({ id: 'chase', name: 'Chase', cat: 'medium', desc: 'Blocks of palette color run along the strip, one block per beat.', swatch: ['#ff2fa0', '#00ffd0'],
    params: { speed: P.speed(1, 'Beats per block'), size: P.size(0.12, 'Block size'), direction: P.direction(), gap: { type: 'range', def: 0.6, min: 0, max: 0.9, step: 0.01, label: 'Gap' } },
    render(c) { const blocks = Math.max(1, Math.round(1 / c.p.size)); const s = c.beat / c.p.speed; for (let i = 0; i < c.n; i++) { const x = dirX(c, i / c.n); const u = x * blocks - s; const k = Math.floor(u); const on = frac(u) > c.p.gap; const col = palIdx(c.pal, k); set(c.out, i, on ? col[0] : 0, on ? col[1] : 0, on ? col[2] : 0); } } });

  def({ id: 'steps', name: 'Stage steps', cat: 'medium', desc: 'The strip in parts (eighths by default): every beat the next part flashes in a palette color and fades, like stage lights firing in turn. Run goes along the strip, Bounce back and forth, Random anywhere (never the same part twice in a row).', swatch: ['#ffb000', '#000000'],
    params: { zones: { type: 'range', def: 8, min: 2, max: 16, step: 1, label: 'Parts' }, every: P.speed(1, 'Next part every (beats)'),
      decay: { type: 'range', def: 0.75, min: 0.1, max: 4, step: 0.05, label: 'Fade (beats)' },
      order: { type: 'select', def: 'run', options: { run: 'run', bounce: 'bounce', random: 'random' }, label: 'Order' }, overlay: P.bool('Transparent background', true) },
    render(c) {
      clear(c.out); if (!c.p.overlay) fillAll(c.out, c.n, 0, 0, 0, 1);
      const z = Math.max(2, Math.round(c.p.zones)), step = Math.floor(c.beat / c.p.every);
      const rnd = k => Math.floor(hash(k * 7919 + 13) * z);
      const zoneOf = k => {
        if (c.p.order === 'bounce') { const m = 2 * (z - 1), r = ((k % m) + m) % m; return r < z ? r : m - r; }
        if (c.p.order === 'random') { const q = rnd(k); return q === rnd(k - 1) ? (q + 1) % z : q; }
        return ((k % z) + z) % z;
      };
      const back = Math.ceil(c.p.decay / c.p.every) + 1;
      for (let k = step; k > step - back && k >= 0; k--) {
        const age = (c.beat - k * c.p.every) / c.p.decay; if (age < 0 || age > 1) continue;
        const v = (1 - age) * (1 - age), zi = zoneOf(k), col = colorFor(c, (zi + 0.5) / z, k);
        const i0 = Math.floor(zi * c.n / z), i1 = Math.max(i0 + 1, Math.floor((zi + 1) * c.n / z));
        for (let i = i0; i < Math.min(c.n, i1); i++) add(c.out, i, col[0] * v, col[1] * v, col[2] * v, v);
      }
    } });

  def({ id: 'comet', name: 'Comets', cat: 'medium', desc: 'Comets launch every N beats and streak along the strip with a fading tail.', swatch: ['#ffffff', '#0040ff'],
    params: { every: P.speed(2, 'Launch every (beats)'), travel: P.speed(2, 'Travel time (beats)'), tail: P.size(0.35, 'Tail length'), direction: P.direction(), color: P.color(), overlay: P.bool('Transparent background', true) },
    render(c) { clear(c.out); if (!c.p.overlay) fillAll(c.out, c.n, 0, 0, 0, 1); const alive = Math.ceil(c.p.travel / c.p.every) + 1; const cur = Math.floor(c.beat / c.p.every); for (let k = cur; k > cur - alive && k >= 0; k--) { const age = (c.beat - k * c.p.every) / c.p.travel; if (age < 0 || age > 1.2) continue; const head = age * 1.1; const col = colorFor(c, head, k); for (let i = 0; i < c.n; i++) { const x = dirX(c, i / Math.max(1, c.n - 1)); const behind = head - x; let v = 0; if (behind >= 0) v = Math.exp(-behind / (c.p.tail * 0.5)); v = Math.max(v, Math.exp(-1 * ((x - head) / 0.03) ** 2)); if (v > 0.01) add(c.out, i, col[0] * v, col[1] * v, col[2] * v, v); } } } });

  def({ id: 'bounce', name: 'Bounce', cat: 'medium', desc: 'A single comet ping-pongs end to end, one length per N beats.', swatch: ['#ffe600', '#ff4e00'],
    params: { speed: P.speed(2, 'One length per (beats)'), tail: P.size(0.3, 'Tail length'), color: P.color(), overlay: P.bool('Transparent background', true) },
    render(c) { clear(c.out); if (!c.p.overlay) fillAll(c.out, c.n, 0, 0, 0, 1); const u = c.beat / c.p.speed; const leg = Math.floor(u); const ph = frac(u); const fwd = leg % 2 === 0; const head = fwd ? ph : 1 - ph; const col = colorFor(c, head, leg); for (let i = 0; i < c.n; i++) { const x = i / Math.max(1, c.n - 1); const behind = fwd ? head - x : x - head; let v = behind >= 0 ? Math.exp(-behind / (c.p.tail * 0.5)) : 0; v = Math.max(v, Math.exp(-1 * ((x - head) / 0.03) ** 2)); if (v > 0.01) add(c.out, i, col[0] * v, col[1] * v, col[2] * v, v); } } });

  def({ id: 'alternate', name: 'Alternate', cat: 'medium', desc: 'Odd and even segments swap palette colors every beat.', swatch: ['#ff0000', '#0040ff'],
    params: { every: P.speed(1, 'Swap every (beats)'), size: { type: 'range', def: 1, min: 1, max: 8, step: 1, label: 'Segment width' }, intensity: P.intensity() },
    render(c) { const k = Math.floor(c.beat / c.p.every); const a = palIdx(c.pal, k), b = palIdx(c.pal, k + 1); for (let i = 0; i < c.n; i++) { const col = (Math.floor(i / c.p.size) + k) % 2 === 0 ? a : b; set(c.out, i, col[0] * c.p.intensity, col[1] * c.p.intensity, col[2] * c.p.intensity); } } });

  def({ id: 'wipe', name: 'Wipe', cat: 'medium', desc: 'Fills the strip with the next palette color every cycle.', swatch: ['#00ffd0', '#8a2be2'],
    params: { speed: P.speed(4, 'One wipe per (beats)'), direction: P.direction(), soft: { type: 'range', def: 0.05, min: 0, max: 0.5, step: 0.01, label: 'Soft edge' } },
    render(c) { const u = c.beat / c.p.speed; const k = Math.floor(u), ph = frac(u); const a = palIdx(c.pal, k - 1), b = palIdx(c.pal, k); for (let i = 0; i < c.n; i++) { const x = dirX(c, i / Math.max(1, c.n - 1)); const m = clamp((ph * (1 + c.p.soft) - x) / (c.p.soft + 0.001)); set(c.out, i, lerp(a[0], b[0], m), lerp(a[1], b[1], m), lerp(a[2], b[2], m)); } } });

  def({ id: 'meter', name: 'Energy meter', cat: 'medium', desc: 'The song’s loudness fills the strip like a VU meter.', swatch: ['#00a000', '#ff0000'],
    params: { direction: P.direction(), color: P.color(), floor: { type: 'range', def: 0.05, min: 0, max: 1, step: 0.01, label: 'Floor' }, gain: { type: 'range', def: 1, min: 0.5, max: 2, step: 0.05, label: 'Gain' } },
    render(c) { const lvl = clamp(c.energy * c.p.gain); for (let i = 0; i < c.n; i++) { const x = dirX(c, (i + 0.5) / c.n); const on = x < lvl; const col = c.p.color === 'cycle' ? palAt(c.pal, x) : colorFor(c, x, 0); const v = on ? 1 : c.p.floor; set(c.out, i, col[0] * v, col[1] * v, col[2] * v); } } });

  def({ id: 'spectrum', name: 'Spectrum', cat: 'medium', desc: 'A live spectrum analyser: bass at one end, treble at the other.', swatch: ['#ff0000', '#0000ff'],
    params: { direction: P.direction(), color: P.color(), floor: { type: 'range', def: 0.05, min: 0, max: 1, step: 0.01, label: 'Floor' } },
    render(c) { const nb = c.bands.length; for (let i = 0; i < c.n; i++) { const x = dirX(c, (i + 0.5) / c.n); const fb = x * (nb - 1); const j = Math.floor(fb); const e = lerp(c.bands[j], c.bands[Math.min(nb - 1, j + 1)], fb - j); const col = c.p.color === 'cycle' ? palAt(c.pal, x) : colorFor(c, x, 0); const v = c.p.floor + (1 - c.p.floor) * e * e; set(c.out, i, col[0] * v, col[1] * v, col[2] * v); } } });

  def({ id: 'scanner', name: 'Scanner', cat: 'medium', desc: 'A bright bar sweeps back and forth with a soft tail.', swatch: ['#ff0000', '#300000'],
    params: { speed: P.speed(2, 'Sweep (beats)'), size: P.size(0.12, 'Width'), color: P.color(), floor: { type: 'range', def: 0.0, min: 0, max: 1, step: 0.01, label: 'Floor' } },
    render(c) { const u = c.beat / c.p.speed; const pos = 0.5 - 0.5 * Math.cos(u * Math.PI); const col = colorFor(c, pos, Math.floor(u / 2)); for (let i = 0; i < c.n; i++) { const x = i / Math.max(1, c.n - 1); const v = c.p.floor + Math.exp(-1 * ((x - pos) / (c.p.size * 0.5)) ** 2); set(c.out, i, col[0] * v, col[1] * v, col[2] * v); } } });

  def({ id: 'blocks', name: 'Random blocks', cat: 'medium', desc: 'Every beat, random segments jump to random palette colors.', swatch: ['#ffd166', '#118ab2'],
    params: { every: P.speed(1, 'Change every (beats)'), size: { type: 'range', def: 3, min: 1, max: 12, step: 1, label: 'Block width' }, density: { type: 'range', def: 0.4, min: 0.1, max: 1, step: 0.01, label: 'Density' } },
    render(c) { const k = Math.floor(c.beat / c.p.every); for (let i = 0; i < c.n; i++) { const b = Math.floor(i / c.p.size); const on = hash(k * 131 + b * 17) < c.p.density; const col = palIdx(c.pal, Math.floor(hash(k * 977 + b * 31) * c.pal.length)); set(c.out, i, on ? col[0] : 0, on ? col[1] : 0, on ? col[2] : 0); } } });

  def({ id: 'battle', name: 'Color battle', cat: 'medium', desc: 'Two colors push against each other from both ends; the front line surges on every beat.', swatch: ['#ff0000', '#0040ff'],
    params: { speed: P.speed(8, 'Battle length (beats)'), surge: { type: 'range', def: 0.15, min: 0, max: 0.5, step: 0.01, label: 'Beat surge' }, soft: { type: 'range', def: 0.08, min: 0, max: 0.5, step: 0.01, label: 'Front softness' } },
    render(c) { const a = c.pal[0], b = c.pal[1 % c.pal.length]; const u = c.beat / c.p.speed; const base = 0.5 + 0.35 * Math.sin(u * 2 * Math.PI); const side = Math.floor(c.beat) % 2 === 0 ? 1 : -1; const front = clamp(base + side * c.p.surge * c.kick, 0.02, 0.98); for (let i = 0; i < c.n; i++) { const x = i / Math.max(1, c.n - 1); const m = clamp((x - front) / (c.p.soft + 0.001) + 0.5); const glow = 1 + 0.6 * Math.exp(-1 * ((x - front) / 0.05) ** 2); set(c.out, i, lerp(a[0], b[0], m) * glow, lerp(a[1], b[1], m) * glow, lerp(a[2], b[2], m) * glow); } } });

  def({ id: 'ripple', name: 'Ripples', cat: 'medium', desc: 'Rings expand from the center on every beat over a transparent background.', swatch: ['#ffffff', '#00c8ff'],
    params: { every: P.speed(1, 'Every (beats)'), travel: P.speed(1, 'Travel time (beats)'), width: P.size(0.1, 'Ring width'), color: P.color() },
    render(c) { clear(c.out); const cur = Math.floor(c.beat / c.p.every); const alive = Math.ceil(c.p.travel / c.p.every) + 1; for (let k = cur; k > cur - alive && k >= 0; k--) { const age = (c.beat - k * c.p.every) / c.p.travel; if (age < 0 || age > 1) continue; const r = age; const col = colorFor(c, age, k); const fade = 1 - 0.55 * age; for (let i = 0; i < c.n; i++) { const x = Math.abs(i / Math.max(1, c.n - 1) - 0.5) * 2; const v = Math.exp(-1 * ((x - r) / (c.p.width)) ** 2) * fade; if (v > 0.01) add(c.out, i, col[0] * v, col[1] * v, col[2] * v, v); } } } });

  def({ id: 'sparkle', name: 'Sparkles', cat: 'medium', desc: 'Sharp sparkles over a transparent background, denser when the music is louder.', swatch: ['#ffffff', '#000000'],
    params: { density: { type: 'range', def: 0.15, min: 0.01, max: 1, step: 0.01, label: 'Density' }, reactive: P.bool('Follow loudness', true), color: P.color(), life: { type: 'range', def: 0.12, min: 0.03, max: 0.5, step: 0.01, label: 'Life (beats)' } },
    render(c) { clear(c.out); const d = c.p.density * (c.p.reactive ? 0.3 + c.energy : 1); const slot = Math.floor(c.beat / c.p.life); for (let i = 0; i < c.n; i++) { for (let s = slot; s > slot - 2; s--) { if (hash(s * 7919 + i * 131) < d) { const ph = (c.beat - s * c.p.life) / c.p.life; const v = clamp(1 - ph * 0.5); const col = colorFor(c, i / c.n, s + i); add(c.out, i, col[0] * v, col[1] * v, col[2] * v, v); } } } } });

  def({ id: 'dualcomet', name: 'Dual comets', cat: 'medium', desc: 'Two comets in two colors launched from opposite ends, crossing in the middle.', swatch: ['#ff0000', '#0040ff'],
    params: { speed: P.speed(2, 'Crossing time (beats)'), tail: P.size(0.35, 'Tail length'), overlay: P.bool('Transparent background', true) },
    render(c) { clear(c.out); if (!c.p.overlay) fillAll(c.out, c.n, 0, 0, 0, 1); const u = c.beat / c.p.speed; const leg = Math.floor(u), ph = frac(u); const a = c.pal[0], b = c.pal[1 % c.pal.length]; const headA = leg % 2 === 0 ? ph : 1 - ph; const dirA = leg % 2 === 0 ? 1 : -1; for (let i = 0; i < c.n; i++) { const x = i / Math.max(1, c.n - 1); for (const [head, dir, col] of [[headA, dirA, a], [1 - headA, -dirA, b]]) { const behind = (head - x) * dir; let v = behind >= 0 ? Math.exp(-behind / (c.p.tail * 0.5)) : 0; v = Math.max(v, Math.exp(-1 * ((x - head) / 0.03) ** 2)); if (v > 0.01) add(c.out, i, col[0] * v, col[1] * v, col[2] * v, v); } } } });

  // ===== SPARSE (mostly dark) =====
  def({ id: 'meteors', name: 'Meteors', cat: 'medium', desc: 'Mostly dark. Now and then a meteor streaks in from a random spot with a fading tail.', swatch: ['#ffffff', '#101020'],
    params: { every: P.speed(1, 'Chance every (beats)'), chance: { type: 'range', def: 0.5, min: 0.05, max: 1, step: 0.05, label: 'Chance' }, travel: P.speed(2, 'Travel time (beats)'), tail: P.size(0.25, 'Tail length'), color: P.color() },
    render(c) { clear(c.out); const cur = Math.floor(c.beat / c.p.every); const alive = Math.ceil(c.p.travel / c.p.every) + 1;
      for (let k = cur; k > cur - alive && k >= 0; k--) { if (hash(k * 7919) > c.p.chance) continue; const age = (c.beat - k * c.p.every) / c.p.travel; if (age < 0 || age > 1) continue;
        // travel the whole strip, entering from one edge and leaving by the other: a head
        // that starts mid-strip and flies off leaves only a faint smear behind it
        const dir = hash(k * 31) < 0.5 ? 1 : -1;
        const head = dir > 0 ? -0.2 + age * 1.4 : 1.2 - age * 1.4;
        const col = colorFor(c, hash(k * 53), k); const fade = Math.sin(Math.PI * Math.min(1, age * 1.15)) ** 0.4;
        for (let i = 0; i < c.n; i++) { const x = i / Math.max(1, c.n - 1); const behind = (head - x) * dir; let v = behind >= 0 ? Math.exp(-behind / (c.p.tail * 0.5)) : 0; v = Math.max(v, Math.exp(-1 * ((x - head) / 0.025) ** 2)) * fade; if (v > 0.01) add(c.out, i, col[0] * v, col[1] * v, col[2] * v, v); } } } });

  def({ id: 'pulsar', name: 'Pulsar', cat: 'medium', desc: 'Dark strip; on every beat one or two points flare up somewhere and fade.', swatch: ['#8ad8ff', '#000010'],
    params: { every: P.speed(1, 'Flare every (beats)'), count: P.count(1, 4), size: P.size(0.08, 'Flare size'), decay: P.decay(0.6), color: P.color() },
    render(c) { clear(c.out); const k = Math.floor(c.beat / c.p.every); const ph = (c.beat - k * c.p.every) / c.p.every; const env = Math.exp(-ph / (c.p.decay * 0.6 + 0.02));
      for (let m = 0; m < c.p.count; m++) { const x0 = 0.05 + 0.9 * hash(k * 101 + m * 17); const col = colorFor(c, x0, k + m); for (let i = 0; i < c.n; i++) { const x = i / Math.max(1, c.n - 1); const v = Math.exp(-1 * ((x - x0) / c.p.size) ** 2) * env; if (v > 0.01) add(c.out, i, col[0] * v, col[1] * v, col[2] * v, v); } } } });

  def({ id: 'orbit', name: 'Orbit', cat: 'medium', desc: 'Two or three bright points bounce end to end at different speeds over darkness, crossing and separating.', swatch: ['#ff2fa0', '#000000'],
    params: { speed: P.speed(4, 'Slowest lap (beats)'), count: P.count(2, 4), tail: P.size(0.12, 'Tail length'), color: P.color() },
    render(c) { clear(c.out); for (let m = 0; m < c.p.count; m++) { const lap = c.p.speed / (1 + 0.5 * m); const u = c.beat / lap + m * 0.37; const ph = frac(u); const fwd = Math.floor(u) % 2 === 0; const head = fwd ? ph : 1 - ph; const col = colorFor(c, head, m);
        for (let i = 0; i < c.n; i++) { const x = i / Math.max(1, c.n - 1); const behind = fwd ? head - x : x - head; let v = behind >= 0 ? Math.exp(-behind / (c.p.tail * 0.5)) : 0; v = Math.max(v, Math.exp(-1 * ((x - head) / 0.02) ** 2)); if (v > 0.01) add(c.out, i, col[0] * v, col[1] * v, col[2] * v, v); } } } });

  // ===== SIGNATURE: follows what this particular song actually plays =====
  // These read c.music, filled from the offline analysis: hits from the wide
  // stereo instruments (lead), off-beat percussion (ghost), bass notes, drum
  // fills, and the repeating motif the song is built on (hook).
  const noteHue = pc => (pc % 12) / 12;

  def({ id: 'hookstrike', name: 'Hook strike', cat: 'signature', desc: 'Marks every return of the song\u2019s main riff with a burst. Dark in between, so the riff is the only thing you see.', swatch: ['#ffffff', '#ff2fa0'],
    params: { shape: { type: 'select', def: 'burst', options: { burst: 'burst from center', sweep: 'sweep across', flash: 'whole strip' }, label: 'Shape' }, decay: P.decay(0.45), size: P.size(0.3, 'Width'), color: P.color() },
    render(c) { clear(c.out); const h = c.music.hook; if (h.index < 0) return;
      const life = 0.25 + c.p.decay * 1.5; if (h.age > life) return;
      const q = h.age / life, env = Math.pow(1 - q, 0.8);
      // the figure finishes travelling at 70% of its life, so it is still bright when it
      // reaches the far end of the strip rather than dying out halfway along
      const travel = Math.min(1, q / 0.7);
      const col = colorFor(c, q, h.index);
      for (let i = 0; i < c.n; i++) {
        const x = i / Math.max(1, c.n - 1); let v = 0;
        if (c.p.shape === 'flash') v = env;
        else if (c.p.shape === 'sweep') v = Math.exp(-1 * (((x - travel) / c.p.size) ** 2)) * env;
        else v = Math.exp(-1 * (((Math.abs(x - 0.5) * 2 - travel) / c.p.size) ** 2)) * env;
        if (v > 0.01) add(c.out, i, col[0] * v, col[1] * v, col[2] * v, v);
      } } });

  def({ id: 'leadtrace', name: 'Lead trace', cat: 'signature', desc: 'A streak for every note of the wide lead instrument \u2014 a saw synth, horn line or riff. Its place on the strip follows the note played; its color is the clip\u2019s (or the note\u2019s, with Color by note).', swatch: ['#00ffd0', '#8a2be2'],
    params: { tail: P.size(0.25, 'Tail length'), decay: P.decay(0.4), byNote: P.bool('Color by note', false), spread: { type: 'range', def: 0.8, min: 0, max: 1, step: 0.05, label: 'Spread by pitch' } },
    render(c) { clear(c.out); const L = c.music.lead; if (L.index < 0) return;
      const life = 0.2 + c.p.decay * 1.2; if (L.age > life) return;
      const env = Math.pow(1 - L.age / life, 1.4) * (0.35 + 0.65 * L.strength);
      const col = c.p.byNote ? hsv(noteHue(L.pitch), 1, 1) : palIdx(c.pal, L.index);
      const home = 0.5 + (noteHue(L.pitch) - 0.5) * c.p.spread;      // low notes left, high notes right
      const head = home + (L.age / life) * 0.25 * (L.pitch % 2 ? 1 : -1);
      for (let i = 0; i < c.n; i++) {
        const x = i / Math.max(1, c.n - 1);
        const d = Math.abs(x - head);
        const v = Math.exp(-1 * ((d / (c.p.tail * 0.6)) ** 2)) * env;
        if (v > 0.01) add(c.out, i, col[0] * v, col[1] * v, col[2] * v, v);
      } } });

  def({ id: 'leadglow', name: 'Lead glow', cat: 'signature', desc: 'Lit only while the wide lead instrument is sounding, and dark the moment it stops. Good as a base under Lead trace.', swatch: ['#3d8bff', '#000010'],
    params: { color: P.color(), gain: { type: 'range', def: 1.2, min: 0.4, max: 3, step: 0.1, label: 'Sensitivity' }, floor: { type: 'range', def: 0, min: 0, max: 0.4, step: 0.01, label: 'Floor' }, direction: P.direction('out') },
    render(c) { const lvl = clamp(c.music.leadLevel * c.p.gain);
      for (let i = 0; i < c.n; i++) {
        const x = dirX(c, i / Math.max(1, c.n - 1));
        const col = c.p.color === 'cycle' ? palAt(c.pal, x * 0.4 + lvl * 0.2) : colorFor(c, x, 0);
        const v = c.p.floor + (1 - c.p.floor) * lvl * (1 - 0.35 * x);
        set(c.out, i, col[0] * v, col[1] * v, col[2] * v);
      } } });

  def({ id: 'ghostspark', name: 'Ghost sparks', cat: 'signature', desc: 'A spark for every drum hit that lands between the beats: fills, ghost notes and shakers. The off-beat detail a beat grid alone never catches.', swatch: ['#ffffff', '#101018'],
    params: { size: P.size(0.06, 'Spark size'), decay: P.decay(0.25), count: P.count(2, 5), color: P.color() },
    render(c) { clear(c.out); const g = c.music.ghost; if (g.index < 0) return;
      const life = 0.08 + c.p.decay * 0.5; if (g.age > life) return;
      const env = Math.pow(1 - g.age / life, 1.5) * (0.4 + 0.6 * g.strength);
      const col = c.p.color === 'cycle' ? palIdx(c.pal, g.index) : colorFor(c, 0.5, g.index);
      for (let m = 0; m < c.p.count; m++) {
        const pos = hash(g.index * 977 + m * 31);
        for (let i = 0; i < c.n; i++) {
          const x = i / Math.max(1, c.n - 1);
          const v = Math.exp(-1 * (((x - pos) / c.p.size) ** 2)) * env;
          if (v > 0.01) add(c.out, i, col[0] * v, col[1] * v, col[2] * v, v);
        }
      } } });

  def({ id: 'fillrush', name: 'Fill rush', cat: 'signature', desc: 'Wakes up only during drum fills and breaks, running a chase that speeds up as the fill builds, then vanishes.', swatch: ['#ffe600', '#ff1e00'],
    params: { speed: P.speed(2, 'Beats per lap'), size: P.size(0.2, 'Block size'), color: P.color(), direction: P.direction() },
    render(c) { clear(c.out); const f = c.music.fill; if (!f.active) return;
      const rate = 1 + 1.2 * f.progress;                                 // speeds up through the fill
      const s = c.beat / c.p.speed * rate;
      const blocks = Math.max(2, Math.round(1 / c.p.size));
      for (let i = 0; i < c.n; i++) {
        const x = dirX(c, i / c.n); const u = x * blocks - s;
        if (frac(u) > 0.55) { const col = colorFor(c, x, Math.floor(u)); add(c.out, i, col[0], col[1], col[2], 1); }
      } } });

  def({ id: 'bassnote', name: 'Bass note', cat: 'signature', desc: 'The strip swells on every new bass note, so the lights move with the bassline rather than the kick. In the clip\u2019s colors, or one per note with Color by note.', swatch: ['#7a00ff', '#ff9a00'],
    params: { byNote: P.bool('Color by note', false), decay: P.decay(0.5), floor: { type: 'range', def: 0.06, min: 0, max: 0.5, step: 0.01, label: 'Floor' }, direction: P.direction('out') },
    render(c) { const b = c.music.bass;
      const env = b.index < 0 ? 0 : Math.exp(-b.age / (0.1 + c.p.decay * 0.7)) * (0.4 + 0.6 * b.strength);
      const col = c.p.byNote ? hsv(noteHue(b.pitch), 0.95, 1) : palIdx(c.pal, Math.max(0, b.index));
      for (let i = 0; i < c.n; i++) {
        const x = dirX(c, i / Math.max(1, c.n - 1));
        const v = c.p.floor + (1 - c.p.floor) * env * Math.exp(-x * 1.6) + 0.25 * c.music.bassLevel;
        set(c.out, i, col[0] * v, col[1] * v, col[2] * v);
      } } });

  // ===== FAST =====
  def({ id: 'strobe', name: 'Strobe', cat: 'fast', desc: 'Hard flashes on the beat grid. Color from the palette or white.', swatch: ['#ffffff', '#222222'],
    params: { every: { type: 'beats', def: 0.5, label: 'Flash every (beats)' }, duty: { type: 'range', def: 0.25, min: 0.05, max: 0.9, step: 0.01, label: 'Flash length' }, white: P.bool('White', false), color: P.color(), overlay: P.bool('Transparent between flashes', true) },
    render(c) { clear(c.out); const u = c.beat / c.p.every; const on = frac(u) < c.p.duty; if (!on) { if (!c.p.overlay) fillAll(c.out, c.n, 0, 0, 0, 1); return; } const col = c.p.white ? [1, 1, 1] : colorFor(c, 0.5, Math.floor(u)); fillAll(c.out, c.n, col[0], col[1], col[2], 1); } });

  def({ id: 'basshits', name: 'Bass hits', cat: 'signature', desc: 'The whole strip flashes on every hit of the bass line, on the beat grid, found on the separated bass track. Made for drops built on a big, clear bass.', swatch: ['#ff0000', '#000000'],
    params: { decay: P.decay(0.3), color: P.color() },
    render(c) {
      clear(c.out); const h = c.music && c.music.basshit; if (!h || h.index < 0) return;
      const v = Math.exp(-h.age / (c.p.decay * 0.35 + 0.03)) * (0.6 + 0.4 * h.strength); if (v < 0.01) return;
      const col = colorFor(c, 0.5, h.index); fillAll(c.out, c.n, col[0] * v, col[1] * v, col[2] * v, v);
    } });

  def({ id: 'drumhits', name: 'Drum hits', cat: 'medium', desc: 'The whole light blooms on the drums: the first color on every kick, the second on every snare, and fades between. Made for small lamps, which play the drums while the long strips move.', swatch: ['#8000ff', '#00ff00'],
    params: { follow: { type: 'select', def: 'drums', options: { drums: 'kick and snare', kick: 'kick only' }, label: 'Follows' }, decay: P.decay(0.35),
      floor: { type: 'range', def: 0.0, min: 0, max: 1, step: 0.01, label: 'Floor' } },
    render(c) {
      clear(c.out); const m = c.music || {}, tau = c.p.decay * 0.4 + 0.03;
      let ek, es;
      if (m.hasDrums) {
        const hit = h => (h && h.index >= 0 ? Math.exp(-h.age / tau) * (0.6 + 0.4 * h.strength) : 0);
        ek = hit(m.kick); es = c.p.follow === 'drums' ? hit(m.snare) : 0;
      } else { ek = Math.exp(-c.phase * c.beatSec / tau); es = 0; }      // no drums known: on the beat
      const col = ek >= es ? c.pal[0] : c.pal[1 % c.pal.length], v = Math.max(c.p.floor, ek, es);
      fillAll(c.out, c.n, col[0] * v, col[1] * v, col[2] * v, v);
    } });

  def({ id: 'drumstrobe', name: 'Drum strobe', cat: 'fast', desc: 'A hard flash on every real kick (or snare) hit and nothing in between, so it is exactly as busy as the drummer. With Grows on it starts faint and is full on the drop.', swatch: ['#ffffff', '#000000'],
    params: { follow: { type: 'select', def: 'kick', options: { kick: 'the kick', snare: 'the snare', both: 'kick and snare' }, label: 'Flashes on' },
      length: { type: 'range', def: 0.05, min: 0.02, max: 0.25, step: 0.01, label: 'Flash length (s)' }, grows: P.bool('Grows toward the end', false), white: P.bool('White', false), color: P.color() },
    render(c) {
      clear(c.out); const m = c.music || {}; if (!m.hasDrums) return;
      let h = null;
      for (const x of [c.p.follow !== 'snare' ? m.kick : null, c.p.follow !== 'kick' ? m.snare : null, c.p.follow !== 'kick' ? m.roll : null]) if (x && x.index >= 0 && (!h || x.age < h.age)) h = x;
      if (!h || h.age > c.p.length) return;
      const v = (c.p.grows ? 0.2 + 0.8 * c.clipT * c.clipT : 1) * (0.6 + 0.4 * h.strength);
      const col = c.p.white ? [1, 1, 1] : colorFor(c, 0.5, h.index);
      fillAll(c.out, c.n, col[0] * v, col[1] * v, col[2] * v, v);
    } });

  def({ id: 'blackout', name: 'Blackout strobe', cat: 'fast', desc: 'Cuts whatever is below to black in a rhythm. Put it on top of a base effect.', swatch: ['#000000', '#444444'],
    params: { every: { type: 'beats', def: 0.5, label: 'Cut every (beats)' }, duty: { type: 'range', def: 0.5, min: 0.05, max: 0.95, step: 0.01, label: 'Dark length' } },
    render(c) { clear(c.out); const u = c.beat / c.p.every; if (frac(u) > 1 - c.p.duty) fillAll(c.out, c.n, 0, 0, 0, 1); } });

  def({ id: 'hyperchase', name: 'Hyper chase', cat: 'fast', desc: 'Tiny blocks race along the strip, several per beat.', swatch: ['#00ffd0', '#ff2fa0'],
    params: { speed: { type: 'beats', def: 0.25, label: 'Beats per block' }, size: P.size(0.08, 'Block size'), direction: P.direction() },
    render(c) { const blocks = Math.max(2, Math.round(1 / c.p.size)); const s = c.beat / c.p.speed; for (let i = 0; i < c.n; i++) { const x = dirX(c, i / c.n); const u = x * blocks - s; const k = Math.floor(u); const on = frac(u) > 0.7; const col = palIdx(c.pal, k); set(c.out, i, on ? col[0] : 0, on ? col[1] : 0, on ? col[2] : 0); } } });

  def({ id: 'lightning', name: 'Lightning', cat: 'fast', desc: 'Random bright flashes across random stretches of the strip.', swatch: ['#ffffff', '#8ad8ff'],
    params: { rate: { type: 'range', def: 0.5, min: 0.05, max: 1, step: 0.01, label: 'Rate' }, color: P.color() },
    render(c) { clear(c.out); const slot = Math.floor(c.beat * 8); if (hash(slot * 31) > c.p.rate) return; const a = hash(slot * 7 + 1), w = 0.1 + 0.5 * hash(slot * 7 + 2); const ph = frac(c.beat * 8); const v = Math.exp(-ph * 4); const col = colorFor(c, a, slot); for (let i = 0; i < c.n; i++) { const x = i / c.n; if (x >= a - w / 2 && x <= a + w / 2) set(c.out, i, col[0] * v, col[1] * v, col[2] * v, v); } } });

  def({ id: 'chaos', name: 'Chaos', cat: 'fast', desc: 'The clip\u2019s colors race along the strip, dense and fast, jumping direction on every beat. Starts out as a rainbow.', swatch: ['#ff0000', '#00ff00'], palette: 'Rainbow',
    params: { speed: P.speed(4, 'Cycle (beats)'), stretch: { type: 'range', def: 2, min: 0.5, max: 6, step: 0.25, label: 'Repeats' } },
    render(c) { const k = Math.floor(c.beat); const dir = hash(k * 13) < 0.5 ? 1 : -1; const s = frac(c.beat / c.p.speed) * dir + hash(k * 17); for (let i = 0; i < c.n; i++) { const x = i / c.n; const col = palAt(c.pal, x * c.p.stretch + s); set(c.out, i, col[0], col[1], col[2]); } } });

  def({ id: 'flash', name: 'Beat flash', cat: 'fast', desc: 'Whole strip flashes a palette color on every beat and decays.', swatch: ['#ff2fa0', '#000000'],
    params: { every: P.speed(1, 'Every (beats)'), decay: P.decay(0.3), color: P.color(), overlay: P.bool('Transparent when dark', true) },
    render(c) { clear(c.out); const u = c.beat / c.p.every; const ph = frac(u) * c.p.every; const v = Math.exp(-ph / (c.p.decay * 0.4 + 0.02)); const col = colorFor(c, 0.5, Math.floor(u)); fillAll(c.out, c.n, col[0] * v, col[1] * v, col[2] * v, c.p.overlay ? v : 1); } });

  def({ id: 'glitch', name: 'Glitch', cat: 'fast', desc: 'Random slices of the strip jump to random colors several times a beat.', swatch: ['#c6ff00', '#ff00c8'],
    params: { rate: { type: 'range', def: 4, min: 1, max: 16, step: 1, label: 'Changes per beat' }, density: { type: 'range', def: 0.5, min: 0.05, max: 1, step: 0.01, label: 'Density' }, overlay: P.bool('Transparent background', true) },
    render(c) { clear(c.out); if (!c.p.overlay) fillAll(c.out, c.n, 0, 0, 0, 1); const slot = Math.floor(c.beat * c.p.rate); const nseg = 6; for (let s = 0; s < nseg; s++) { if (hash(slot * 101 + s * 7) > c.p.density) continue; const a = Math.floor(hash(slot * 53 + s) * c.n), len = 1 + Math.floor(hash(slot * 59 + s) * c.n / 4); const col = palIdx(c.pal, Math.floor(hash(slot * 61 + s) * c.pal.length)); for (let i = a; i < Math.min(c.n, a + len); i++) set(c.out, i, col[0], col[1], col[2], 1); } } });

  def({ id: 'halves', name: 'Halves', cat: 'fast', desc: 'Left and right halves alternate in two palette colors every beat.', swatch: ['#ff0000', '#0040ff'],
    params: { every: P.speed(1, 'Swap every (beats)'), dim: { type: 'range', def: 0.0, min: 0, max: 1, step: 0.01, label: 'Off-side level' } },
    render(c) { const k = Math.floor(c.beat / c.p.every); const a = palIdx(c.pal, k), b = palIdx(c.pal, k + 1); for (let i = 0; i < c.n; i++) { const left = i < c.n / 2; const on = left === (k % 2 === 0); const col = left ? a : b; const v = on ? 1 : c.p.dim; set(c.out, i, col[0] * v, col[1] * v, col[2] * v); } } });

  // ===== AROUND THE ROOM =====
  // These see the whole room: c.room is this light's place in it ({ i, n, side }), the ticked lights
  // in the order they stand around the room, the first half one side and the rest the other. With
  // one light (no room), they play across that light's own strip instead.
  const inRoom = c => c.room && c.room.n >= 2;
  // The long strips run the length of a ceiling: a hit can land on one stretch of it and leave the rest
  // dark, so the light jumps around the room. A long strip is split into zones; a short lamp is one zone.
  def({ id: 'zonehits', name: 'Zone hits', cat: 'room', desc: 'Each hit lights one stretch of the strip and leaves the rest dark. Room: one zone anywhere in the room per hit (a small lamp is a zone of its own), so the light jumps light to light. Each light: one zone on every light. Halves: the kick on one half, the snare on the other. Quarters: the hits go round the quarters.', swatch: ['#ffffff', '#000000'],
    params: { follow: { type: 'select', def: 'snare', options: { kick: 'the kick', snare: 'the snare', both: 'kick and snare', beat: 'the beat', build: 'a build: beats, eighths, sixteenths' }, label: 'Hits on' },
      mode: { type: 'select', def: 'room', options: { room: 'one zone in the room', each: 'one zone on each light', halves: 'halves (kick, snare)', quarters: 'quarters, in turn' }, label: 'Where' },
      zones: { type: 'range', def: 5, min: 2, max: 12, step: 1, label: 'Zones on a long strip' },
      decay: { type: 'range', def: 0.18, min: 0.04, max: 1, step: 0.01, label: 'Fade (s)' },
      darkFor: { type: 'range', def: 0, min: 0, max: 1, step: 0.05, label: 'Dark for the first (share of the clip)' },
      grows: P.bool('Grows toward the end', false), color: P.color() },
    render(c) {
      clear(c.out); const m = c.music || {}, dark = c.clipT < c.p.darkFor;
      if (dark) fillAll(c.out, c.n, 0, 0, 0, 1);
      let h = null, kind = 'kick';
      if (c.p.follow === 'beat') h = { index: Math.floor(c.beat), age: frac(c.beat) * c.beatSec, strength: 1 };
      else if (c.p.follow === 'build') {                                // a build with no drums found: quarters, then eighths, then sixteenths
        const sub = c.clipT < 0.5 ? 1 : c.clipT < 0.75 ? 2 : 4, u = c.clipBeat * sub;
        h = { index: sub * 10007 + Math.floor(u), age: frac(u) * c.beatSec / sub, strength: 1 };
      }
      else if (m.hasDrums) for (const [x, k] of [[c.p.follow !== 'snare' ? m.kick : null, 'kick'], [c.p.follow !== 'kick' ? m.snare : null, 'snare'], [c.p.follow !== 'kick' ? m.roll : null, 'snare']])
        if (x && x.index >= 0 && (!h || x.age < h.age)) { h = x; kind = k; }
      if (!h || h.index < 0) return;
      const v = Math.exp(-h.age / c.p.decay) * (0.6 + 0.4 * (h.strength ?? 1)) * (c.p.grows && !dark ? (g => 0.5 + 0.5 * g * g)((c.clipT - c.p.darkFor) / Math.max(1e-3, 1 - c.p.darkFor)) : 1);
      if (v < 0.01) return;
      const short = c.n <= 12, zn = short ? 1 : Math.max(2, Math.round(c.p.zones));
      // which zone of this light the hit lands on (-1: none here)
      let z = -1; const k = h.index + (kind === 'snare' ? 100003 : 0);
      const pick = (i, nz) => Math.floor(hash(i * 7919 + 17) * nz);
      const fresh = (i, nz) => { const a = pick(i, nz), b = pick(i - 1, nz); return nz > 1 && a === b ? (a + 1 + Math.floor(hash(i * 31 + 5) * (nz - 1))) % nz : a; };   // never the same spot twice running
      if (c.p.mode === 'room' && inRoom(c) && c.room.kinds) {
        const counts = c.room.kinds.map(kd => kd === 'short' ? 1 : Math.max(2, Math.round(c.p.zones))), total = counts.reduce((x, y) => x + y, 0);
        const g = fresh(k, total), off = counts.slice(0, c.room.i).reduce((x, y) => x + y, 0);
        z = g >= off && g < off + counts[c.room.i] ? g - off : -1;
      } else if (c.p.mode === 'halves') z = short ? 0 : (kind === 'snare' ? 1 : 0) * 1;
      else if (c.p.mode === 'quarters') z = short ? 0 : h.index % 4;
      else z = fresh(k + (c.room ? c.room.i * 7 : 0), zn);
      if (z < 0) return;
      const parts = c.p.mode === 'halves' ? (short ? 1 : 2) : c.p.mode === 'quarters' ? (short ? 1 : 4) : zn;
      const i0 = Math.floor(z * c.n / parts), i1 = Math.max(i0 + 1, Math.floor((z + 1) * c.n / parts));
      const col = colorFor(c, (z + 0.5) / parts, h.index);
      for (let i = i0; i < i1 && i < c.n; i++) set(c.out, i, col[0] * v, col[1] * v, col[2] * v, dark ? 1 : v);
    } });

  // A buildup felt all around the room: each hit lights a few stretches somewhere in it (a small lamp is
  // a stretch of its own), never the same ones twice running, and as the build goes on more of them
  // light at once, brighter and snappier, until the drop.
  def({ id: 'buildhits', name: 'Build-up hits', cat: 'room', desc: 'For buildups: every snare (or every hit, or a beat-by-beat build) lights random stretches all around the room. As the buildup goes on, more light at once (one at the start, up to four at the end), brighter and snappier, so the energy builds toward the drop.', swatch: ['#ffffff', '#ff2fa0'],
    params: { follow: { type: 'select', def: 'snare', options: { snare: 'the snare', drums: 'kick and snare', beat: 'the beat', build: 'a build: beats, eighths, sixteenths' }, label: 'Hits on' },
      zones: { type: 'range', def: 6, min: 2, max: 12, step: 1, label: 'Stretches on a long strip' },
      most: { type: 'range', def: 4, min: 1, max: 8, step: 1, label: 'Most at once (at the end)' },
      decay: { type: 'range', def: 0.22, min: 0.05, max: 1, step: 0.01, label: 'Fade at the start (s)' },
      darkFor: { type: 'range', def: 0, min: 0, max: 1, step: 0.05, label: 'Dark between hits for the first (share)' }, color: P.color() },
    render(c) {
      clear(c.out); const m = c.music || {}, k = c.clipT, dark = k < c.p.darkFor;
      if (dark) fillAll(c.out, c.n, 0, 0, 0, 1);
      let h = null;
      if (c.p.follow === 'beat') h = { index: Math.floor(c.beat), age: frac(c.beat) * c.beatSec, strength: 1 };
      else if (c.p.follow === 'build') { const sub = k < 0.5 ? 1 : k < 0.75 ? 2 : 4, u = c.clipBeat * sub; h = { index: sub * 10007 + Math.floor(u), age: frac(u) * c.beatSec / sub, strength: 1 }; }
      else if (m.hasDrums) for (const x of [c.p.follow === 'drums' ? m.kick : null, m.snare, m.roll]) if (x && x.index >= 0 && (!h || x.age < h.age)) h = x;
      if (!h || h.index < 0) return;
      const decay = c.p.decay * (1 - 0.55 * k), v = Math.exp(-h.age / Math.max(0.03, decay)) * (0.45 + 0.55 * Math.pow(k, 1.3)) * (0.7 + 0.3 * (h.strength ?? 1));
      if (v < 0.01) return;
      const short = c.n <= 12, zn = short ? 1 : Math.max(2, Math.round(c.p.zones));
      // how many stretches this hit lights, over the whole room (or over this light, when it is alone)
      const many = Math.max(1, Math.min(Math.round(c.p.most), 1 + Math.floor(k * c.p.most * 0.999)));
      let total = zn, off = 0;
      if (c.room && c.room.n >= 2 && c.room.kinds) {
        const counts = c.room.kinds.map(kd => kd === 'short' ? 1 : Math.max(2, Math.round(c.p.zones)));
        total = counts.reduce((x, y) => x + y, 0); off = counts.slice(0, c.room.i).reduce((x, y) => x + y, 0);
      }
      const prev = new Set(), lit = new Set();
      const pickSet = (idx, into) => { let j = 0, tries = 0; while (into.size < Math.min(many, total) && tries++ < 40) { into.add(Math.floor(hash(idx * 7919 + j * 104729 + 13) * total)); j++; } };
      pickSet(h.index - 1, prev); pickSet(h.index, lit);
      // never the same spot twice running: a stretch the last hit lit moves on
      for (const z of [...lit]) if (prev.has(z) && total > many * 2) { lit.delete(z); let y = (z + 1 + Math.floor(hash(h.index * 31 + z) * (total - 1))) % total; while (lit.has(y) || prev.has(y)) y = (y + 1) % total; lit.add(y); }
      for (const g of lit) {
        const z = g - off; if (z < 0 || z >= zn) continue;
        const i0 = Math.floor(z * c.n / zn), i1 = Math.max(i0 + 1, Math.floor((z + 1) * c.n / zn)), col = colorFor(c, (z + 0.5) / zn, h.index + g);
        for (let i = i0; i < i1 && i < c.n; i++) set(c.out, i, col[0] * v, col[1] * v, col[2] * v, dark ? 1 : v);
      }
    } });

  def({ id: 'roomsplit', name: 'Room split', cat: 'room', desc: 'One side of the room in one color, the other side in the other, swapping on the beat. With one light, its two halves.', swatch: ['#8000ff', '#00ff00'],
    params: { every: P.speed(1, 'Swap every (beats)'), pump: { type: 'range', def: 0.35, min: 0, max: 1, step: 0.01, label: 'Beat pump' } },
    render(c) {
      // the two sides take colors from opposite ends of the palette (with two colors, one each)
      const k = Math.floor(c.beat / c.p.every), a = palIdx(c.pal, k), b = palIdx(c.pal, k + Math.max(1, Math.floor(c.pal.length / 2)));
      const v = (1 - c.p.pump) + c.p.pump * Math.exp(-c.phase * 4);
      for (let i = 0; i < c.n; i++) {
        const side = inRoom(c) ? c.room.side : (i < c.n / 2 ? 0 : 1), col = side ? b : a;
        set(c.out, i, col[0] * v, col[1] * v, col[2] * v);
      }
    } });
  def({ id: 'roomchase', name: 'Room chase', cat: 'room', desc: 'The light runs around the room, one lamp after the next, on the beat. With one light, a block runs along it.', swatch: ['#00ff00', '#000000'],
    params: { every: P.speed(1, 'Next light every (beats)'), floor: { type: 'range', def: 0.0, min: 0, max: 1, step: 0.01, label: 'Floor' } },
    render(c) {
      const t = c.beat / c.p.every, step = Math.floor(t), env = Math.exp(-(t - step) * 2.5), col = palIdx(c.pal, step);
      for (let i = 0; i < c.n; i++) {
        const lit = inRoom(c) ? (step % c.room.n) === c.room.i : Math.floor(i / c.n * 4) === step % 4;
        const v = lit ? Math.max(c.p.floor, env) : c.p.floor;
        set(c.out, i, col[0] * v, col[1] * v, col[2] * v, lit ? 1 : (c.p.floor > 0 ? 1 : 0));
      }
    } });
  def({ id: 'roomwave', name: 'Room wave', cat: 'room', desc: 'A slow swell rolls around the room from lamp to lamp, each half of the room in its own color. With one light, along its strip.', swatch: ['#8000ff', '#00ff00'],
    params: { speed: P.speed(8, 'Once round (beats)'), floor: { type: 'range', def: 0.1, min: 0, max: 1, step: 0.01, label: 'Floor' } },
    render(c) {
      const t = c.beat / c.p.speed;
      for (let i = 0; i < c.n; i++) {
        const x = inRoom(c) ? (c.room.i + 0.5) / c.room.n : i / Math.max(1, c.n);
        const v = c.p.floor + (1 - c.p.floor) * (0.5 + 0.5 * Math.cos(2 * Math.PI * (t - x)));
        const col = palIdx(c.pal, x < 0.5 ? 0 : 1);              // solid colors by half, never mixed into mud
        set(c.out, i, col[0] * v, col[1] * v, col[2] * v);
      }
    } });

  // ===== ACCENTS (one-shots shaped by the clip length) =====
  def({ id: 'dropblast', name: 'Drop blast', cat: 'accent', desc: 'White blast then a shockwave racing out from the center. Place it right on the drop.', swatch: ['#ffffff', '#ff2fa0'],
    params: { flash: { type: 'range', def: 0.15, min: 0.02, max: 0.5, step: 0.01, label: 'Flash (beats)' }, ring: P.size(0.12, 'Ring width'), color: P.color() },
    render(c) { clear(c.out); const b = c.clipBeat; if (b < c.p.flash) { fillAll(c.out, c.n, 1, 1, 1, 1); return; } const age = (b - c.p.flash) / Math.max(0.5, c.clipBeats - c.p.flash); if (age > 1) return; const col = colorFor(c, age, 0); for (let i = 0; i < c.n; i++) { const x = Math.abs(i / Math.max(1, c.n - 1) - 0.5) * 2; const v = Math.exp(-1 * ((x - age * 1.1) / c.p.ring) ** 2) * (1 - age); if (v > 0.01) set(c.out, i, col[0] * v, col[1] * v, col[2] * v, v); } } });

  // ----- risers: everything moves in note values, counted back from the drop ------------------
  // A buildup is heard in note values: quarter notes, then eighths, then sixteenths.
  // Lights that speed up or grow smoothly, on their own clock, look disconnected from
  // the music. So both risers work in stages laid out backwards from the end of the
  // clip (where the drop is), each a whole number of bars when the clip allows, and
  // every flash or step lands on the song's own beat grid.
  const NOTES = { '1': '1/4 notes', '2': '1/8 notes', '4': '1/16 notes', '8': '1/32 notes' };
  /** stage length in beats for k stages over the last `span` beats of the clip */
  function stageLen(span, k) {
    const raw = span / Math.max(1, k);
    return raw >= 4 ? Math.floor(raw / 4) * 4 : Math.max(1, Math.floor(raw));
  }
  /** which stage a moment is in, counted from the end: 0 is the last stage, before the drop */
  const stageFromEnd = (end, at, len) => Math.floor((end - at - 1e-6) / len);
  /** where the drop is, in clip beats: the song beat nearest the end of the clip, so a clip
   *  that is a little off the grid still changes gear on the beat */
  const dropAt = c => Math.round(c.beat - c.clipBeat + c.clipBeats) - (c.beat - c.clipBeat);

  def({ id: 'strobeRiser', name: 'Strobe riser', cat: 'accent', desc: 'A blackout strobe that steps up through note values into the drop: 1/4 notes, then 1/8, then 1/16. Each step lasts a whole number of bars when the clip allows, and every cut lands on the beat. Transparent between cuts, so put it on the layer above the buildup. End it on the drop.', swatch: ['#000000', '#ffffff'],
    params: { from: { type: 'select', def: '1', options: { '1': NOTES['1'], '2': NOTES['2'], '4': NOTES['4'] }, label: 'Starts in' },
      to: { type: 'select', def: '4', options: { '2': NOTES['2'], '4': NOTES['4'], '8': NOTES['8'] }, label: 'Ends in' },
      strobeFrom: { type: 'range', def: 0.3, min: 0, max: 1, step: 0.01, label: 'Starts at' },
      duty: { type: 'range', def: 0.5, min: 0.1, max: 0.9, step: 0.05, label: 'Dark fraction' } },
    render(c) { clear(c.out);
      const from = Number(c.p.from) || 1, to = Math.max(from, Number(c.p.to) || 4);
      const k = Math.round(Math.log2(to / from)) + 1;                            // 1/4 -> 1/16 is three stages
      const len = stageLen(c.clipBeats * (1 - c.p.strobeFrom), k);
      const idx = stageFromEnd(dropAt(c), c.clipBeat, len);
      if (idx >= k || idx < 0 || c.clipBeat >= c.clipBeats) return;              // not started yet, or past the drop
      const rate = to / 2 ** idx;                                                  // cuts per beat
      if (frac(c.beat * rate) >= 1 - c.p.duty) fillAll(c.out, c.n, 0, 0, 0, 1); } });

  def({ id: 'fadein', name: 'Fade in', cat: 'accent', desc: 'Darkness lifting over the length of the clip. Layer it above other effects.', swatch: ['#000000', '#888888'],
    params: {}, render(c) { fillAll(c.out, c.n, 0, 0, 0, 1 - c.clipT); } });

  def({ id: 'fadeout', name: 'Fade out', cat: 'accent', desc: 'Darkens everything below over the length of the clip.', swatch: ['#888888', '#000000'],
    params: {}, render(c) { fillAll(c.out, c.n, 0, 0, 0, c.clipT); } });

  def({ id: 'dark', name: 'Blackout', cat: 'accent', desc: 'Lights off for the length of the clip.', swatch: ['#000000', '#000000'],
    params: {}, render(c) { fillAll(c.out, c.n, 0, 0, 0, 1); } });

  def({ id: 'whiteflash', name: 'Flash', cat: 'accent', desc: 'One flash of the clip\u2019s color, decaying over the clip. White unless you pick something else.', swatch: ['#ffffff', '#ffffff'], colors: ['#ffffff'],
    params: { color: P.color() }, render(c) { const v = Math.exp(-c.clipT * 5); const col = colorFor(c, 0.5, 0); fillAll(c.out, c.n, col[0], col[1], col[2], v); } });

  def({ id: 'colorslam', name: 'Slam', cat: 'accent', desc: 'Whole strip slams to a palette color and fades back out.', swatch: ['#ff0000', '#000000'],
    params: { color: P.color() }, render(c) { const v = Math.exp(-c.clipT * 4); const col = colorFor(c, 0.5, 0); fillAll(c.out, c.n, col[0], col[1], col[2], v); } });

  def({ id: 'sweepout', name: 'Sweep out', cat: 'accent', desc: 'A bright bar sweeps from the center to both ends once, over the clip.', swatch: ['#ffffff', '#00c8ff'],
    params: { size: P.size(0.15, 'Width'), color: P.color() },
    render(c) { clear(c.out); const col = colorFor(c, c.clipT, 0); for (let i = 0; i < c.n; i++) { const x = Math.abs(i / Math.max(1, c.n - 1) - 0.5) * 2; const v = Math.exp(-1 * ((x - c.clipT) / c.p.size) ** 2); if (v > 0.01) set(c.out, i, col[0] * v, col[1] * v, col[2] * v, v); } } });

  const byId = Object.fromEntries(E.map(e => [e.id, e]));
  const CATS = { signature: 'Follows this song', room: 'Around the room', slow: 'Slow & smooth', medium: 'Groove', fast: 'Fast & intense', accent: 'Accents (one-shot)' };

  function defaults(effectId) {
    const e = byId[effectId]; const p = {};
    for (const [k, d] of Object.entries(e.params)) p[k] = d.def;
    return p;
  }

  // ---------- compositing ----------------------------------------------------------
  const BLENDS = { normal: 'Normal', add: 'Add', screen: 'Screen', multiply: 'Multiply', max: 'Lighten' };
  function composite(dst, src, n, opacity, blend) {
    for (let i = 0; i < n; i++) {
      const j = i * 4; const a = clamp(src[j + 3]) * opacity; if (a <= 0) continue;
      for (let ch = 0; ch < 3; ch++) {
        const d = dst[j + ch], s = clamp(src[j + ch]); let r;
        switch (blend) {
          case 'add': r = d + s; break;
          case 'screen': r = 1 - (1 - d) * (1 - s); break;
          case 'multiply': r = d * s; break;
          case 'max': r = Math.max(d, s); break;
          default: r = s;
        }
        dst[j + ch] = clamp(d + (r - d) * a);
      }
    }
  }

  return { list: E, byId, CATS, PALETTES, BEAT_OPTS, BLENDS, defaults, composite, hex2rgb, hsv, palAt, hash, solid, solidPalette };
})();
