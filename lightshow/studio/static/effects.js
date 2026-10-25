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
  /** A color's hue, 0..1; null for a gray (saturation under minSat) or black. */
  function hueOf(hex, minSat = 0.2) {
    const [r, g, b] = hex2rgb(hex), mx = Math.max(r, g, b), d = mx - Math.min(r, g, b);
    if (!mx || !d || d / mx < minSat) return null;
    const h = mx === r ? ((g - b) / d) % 6 : mx === g ? (b - r) / d + 2 : (r - g) / d + 4;
    return ((h / 6) + 1) % 1;
  }
  /** The pure color (full saturation and value) of a hue 0..1, as hex. */
  const hueHex = h => '#' + hsv(((h % 1) + 1) % 1, 1, 1).map(x => Math.round(x * 255).toString(16).padStart(2, '0')).join('');
  function hsv(h, s, v) {
    h = frac(h); const i = Math.floor(h * 6), f = h * 6 - i;
    const p = v * (1 - s), q = v * (1 - s * f), t = v * (1 - s * (1 - f));
    switch (i % 6) {
      case 0: return [v, t, p]; case 1: return [q, v, p]; case 2: return [p, v, t];
      case 3: return [p, q, v]; case 4: return [t, p, v]; default: return [v, p, q];
    }
  }
  /** gradient sample through the palette, x wraps around */
  /** A color part way along a palette (x 0..1 goes round it once). Between two colors near each other on the
   *  color wheel (up to 150 degrees apart) the blend goes round the wheel, so every step on the way is a pure
   *  bright color (red to green through yellow, not a dim olive); between two far apart (violet and lime, red
   *  and cyan) each holds most of its span and the change is a quicker crossfade, since their RGB middle is a
   *  muddy grey. White or grey in the palette: a plain blend (a tint). */
  function rgbHue(c) {
    const mx = Math.max(c[0], c[1], c[2]), d = mx - Math.min(c[0], c[1], c[2]);
    if (!mx || d / mx < 0.2) return null;
    const h = mx === c[0] ? ((c[1] - c[2]) / d) % 6 : mx === c[1] ? (c[2] - c[0]) / d + 2 : (c[0] - c[1]) / d + 4;
    return ((h / 6) + 1) % 1;
  }
  function palAt(pal, x) {
    const m = pal.length; if (m === 1) return pal[0];
    x = frac(x) * m; const i = Math.floor(x); let f = x - i; const a = pal[i % m], b = pal[(i + 1) % m];
    const ha = rgbHue(a), hb = rgbHue(b);
    if (ha !== null && hb !== null) {
      const d = hb - ha - Math.round(hb - ha);                       // the short way round, -0.5..0.5
      if (Math.abs(d) <= 150 / 360) {
        const va = Math.max(a[0], a[1], a[2]), vb = Math.max(b[0], b[1], b[2]);
        const sa = (va - Math.min(a[0], a[1], a[2])) / va, sb = (vb - Math.min(b[0], b[1], b[2])) / vb;
        return hsv(ha + d * f, lerp(sa, sb, f), lerp(va, vb, f));
      }
      f = clamp((f - 0.5) / 0.4 + 0.5); f = f * f * (3 - 2 * f);    // far apart: a crossfade over the middle 40%
    }
    return [lerp(a[0], b[0], f), lerp(a[1], b[1], f), lerp(a[2], b[2], f)];
  }
  const palIdx = (pal, i) => pal[((Math.floor(i) % pal.length) + pal.length) % pal.length];
  function hash(k) { k = (k ^ 0x9e3779b9) >>> 0; k = Math.imul(k ^ (k >>> 16), 0x85ebca6b) >>> 0; k = Math.imul(k ^ (k >>> 13), 0xc2b2ae35) >>> 0; return ((k ^ (k >>> 16)) >>> 0) / 4294967296; }
  /** How many hits so far in this clip, of these instruments together, hits within 40 ms of each other one hit (a
   *  kick and a bass note landing together are one moment: counted as two, a two-color swap stood still). */
  function hitMoments(c, streams) {
    const m = c.music || {}; if (!m.hitsBetween) return 0;
    const ts = streams.flatMap(st => m.hitsBetween(st, c.clipT0 - 0.02, c.t + 1e-6)).sort((x, y) => x - y);
    let k = 0, last = -1e9; for (const t of ts) { if (t - last > 0.04) k++; last = t; } return k;
  }
  function set(out, i, r, g, b, a = 1) { const j = i * 4; out[j] = r; out[j + 1] = g; out[j + 2] = b; out[j + 3] = a; }
  function add(out, i, r, g, b, a = 1) { const j = i * 4; out[j] += r; out[j + 1] += g; out[j + 2] += b; out[j + 3] = Math.max(out[j + 3], a); }
  function fillAll(out, n, r, g, b, a = 1) { for (let i = 0; i < n; i++) set(out, i, r, g, b, a); }
  function clear(out) { out.fill(0); }
  /** direction helpers: x in 0..1 along strip -> 0..1 in effect space */
  function dirX(c, x) {
    switch (c.p.direction) {
      case 'rev': return 1 - x;
      case 'out': return away(c, x);                 // the starting point = 0, the far end = 1
      case 'in': return 1 - away(c, x);              // the ends = 0, the starting point = 1
      default: return x;
    }
  }
  /** How far x is from where the effect starts (its "Starts from": the middle unless set), 0..1, 1 at the
   *  farther end: from the middle it opens both ways, from an end it runs the whole strip one way. */
  function away(c, x) { const o = c.p.origin ?? 0.5; return Math.abs(x - o) / Math.max(o, 1 - o, 1e-3); }
  const DIRS = { fwd: 'forward', rev: 'reverse', out: 'center out', in: 'ends in' };
  /** How a sweep travels from its start to its end (u 0..1 of the way, in time -> 0..1 of the way, in
   *  space): steady, smooth, a rush through the middle, hanging in the middle, landing in bounces, ramping
   *  up or down. */
  const EASES = { linear: 'steady', smooth: 'smooth', rush: 'rush through the middle', drift: 'hang in the middle', bounce: 'bounce at the end', accel: 'ramp up', decel: 'ramp down' };
  function ease(c, u) {
    u = clamp(u);
    switch (c.p.ease) {
      case 'smooth': return 0.5 - 0.5 * Math.cos(u * Math.PI);
      case 'rush': return u < 0.5 ? 4 * u * u * u : 1 - Math.pow(-2 * u + 2, 3) / 2;
      case 'drift': return 0.5 + 4 * Math.pow(u - 0.5, 3);
      case 'bounce': { const n1 = 7.5625, d1 = 2.75;
        if (u < 1 / d1) return n1 * u * u;
        if (u < 2 / d1) { u -= 1.5 / d1; return n1 * u * u + 0.75; }
        if (u < 2.5 / d1) { u -= 2.25 / d1; return n1 * u * u + 0.9375; }
        u -= 2.625 / d1; return n1 * u * u + 0.984375; }
      case 'accel': return u * u;
      case 'decel': return 1 - (1 - u) * (1 - u);
      default: return u;
    }
  }

  // common param definitions
  // Where an effect plays, once the house is mapped (app.js houseDraw): on each light as ever, or on the house as
  // one, across it, round its middle, from its middle out, or each wall its own side of a cube of colors.
  // Across the house goes the way of its Direction (0 left to right, 90 front to back, 45 a diagonal...); round the
  // middle starts there and turns the way of its Spin. Auto-build picks them from the song's own fingerprint.
  const SPACES = { strip: 'each light on its own', across: 'across the house', around: 'round the middle', out: 'from the middle out', sides: 'each wall its own (a cube)' };
  const P = {
    space: (def, ...ways) => ({ type: 'select', def, options: Object.fromEntries(['strip', ...ways].map(k => [k, SPACES[k]])), label: 'Plays' }),
    angle: () => ({ type: 'range', def: 0, min: 0, max: 345, step: 15, label: 'Direction (degrees: 0 left to right, 90 front to back)' }),
    spin: () => ({ type: 'select', def: 'cw', options: { cw: 'clockwise', ccw: 'counter-clockwise' }, label: 'Spin' }),
    speed: (def = 4, label = 'Cycle (beats)') => ({ type: 'beats', def, label }),
    intensity: (def = 1) => ({ type: 'range', def, min: 0, max: 1, step: 0.01, label: 'Intensity' }),
    size: (def = 0.3, label = 'Size') => ({ type: 'range', def, min: 0.02, max: 1, step: 0.01, label }),
    direction: (def = 'fwd') => ({ type: 'select', def, options: DIRS, label: 'Direction' }),
    color: () => ({ type: 'select', def: 'cycle', options: { cycle: 'palette cycle', gradient: 'palette gradient', first: 'first color', second: 'second color' }, label: 'Color' }),
    decay: (def = 0.5) => ({ type: 'range', def, min: 0.05, max: 1, step: 0.01, label: 'Decay' }),
    ease: (def = 'linear') => ({ type: 'select', def, options: EASES, label: 'Motion' }),
    origin: () => ({ type: 'range', def: 0.5, min: 0, max: 1, step: 0.05, label: 'Starts from (0 left, 1 right)' }),
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
  // effects that open out from a point (or come in to it): where that point is can be set, the middle by default
  const FROM_A_POINT = new Set(['wobble', 'deep808', 'heartbeat', 'ripple', 'hookstrike', 'dropblast', 'sweepout']);
  const def = (e) => {
    e.params = e.params || {};
    if ((FROM_A_POINT.has(e.id) || e.params.direction) && !e.params.origin) e.params.origin = P.origin();
    E.push(e);
  };

  // ===== SLOW =====
  def({ id: 'solid', name: 'Solid color', cat: 'slow', desc: 'One palette color, optionally cycling per N beats.', swatch: ['#ff2fa0', '#ff2fa0'],
    params: { space: P.space('strip', 'sides'), speed: P.speed(16, 'Change every (beats)'), color: P.color(), intensity: P.intensity() },
    render(c) { const k = Math.floor(c.beat / c.p.speed); const col = colorFor(c, 0.5, k); fillAll(c.out, c.n, col[0] * c.p.intensity, col[1] * c.p.intensity, col[2] * c.p.intensity); } });

  def({ id: 'gradient', name: 'Gradient sweep', cat: 'slow', desc: 'The palette gradient scrolls slowly along the strip.', swatch: ['#ff4e00', '#7a1fa2'],
    params: { space: P.space('across', 'across', 'around', 'out', 'sides'), angle: P.angle(), spin: P.spin(), speed: P.speed(16, 'One length per (beats)'), direction: P.direction(), stretch: { type: 'range', def: 1, min: 0.25, max: 4, step: 0.05, label: 'Stretch' }, intensity: P.intensity() },
    render(c) { const s = c.beat / c.p.speed; for (let i = 0; i < c.n; i++) { const x = dirX(c, i / Math.max(1, c.n - 1)); const col = palAt(c.pal, x / c.p.stretch - s); set(c.out, i, col[0] * c.p.intensity, col[1] * c.p.intensity, col[2] * c.p.intensity); } } });

  def({ id: 'aurora', name: 'Aurora', cat: 'slow', desc: 'Slow drifting ribbons of palette color, like northern lights.', swatch: ['#00ffa0', '#3c00c8'],
    params: { space: P.space('across', 'across', 'around', 'sides'), angle: P.angle(), spin: P.spin(), speed: P.speed(16), intensity: P.intensity(0.9), depth: { type: 'range', def: 0.5, min: 0, max: 1, step: 0.01, label: 'Depth' } },
    render(c) { const ph = c.beat / c.p.speed * 2 * Math.PI; for (let i = 0; i < c.n; i++) { const x = i / Math.max(1, c.n - 1); const w = 0.5 + 0.5 * Math.sin(x * 6.3 + ph) * Math.sin(x * 2.1 - ph * 0.7); const col = palAt(c.pal, x * 0.6 + 0.15 * Math.sin(ph * 0.5 + x * 3)); const v = c.p.intensity * (1 - c.p.depth + c.p.depth * w); set(c.out, i, col[0] * v, col[1] * v, col[2] * v); } } });

  def({ id: 'breathe', name: 'Breathe', cat: 'slow', desc: 'The whole strip swells and fades once per cycle.', swatch: ['#3d8bff', '#0a1a40'],
    params: { speed: P.speed(4), color: P.color(), floor: { type: 'range', def: 0.02, min: 0, max: 1, step: 0.01, label: 'Floor' }, intensity: P.intensity() },
    render(c) { const ph = frac(c.beat / c.p.speed); const v = c.p.floor + (1 - c.p.floor) * (0.5 - 0.5 * Math.cos(ph * 2 * Math.PI)); const col = colorFor(c, ph, Math.floor(c.beat / c.p.speed)); fillAll(c.out, c.n, col[0] * v * c.p.intensity, col[1] * v * c.p.intensity, col[2] * v * c.p.intensity); } });

  def({ id: 'colorwash', name: 'Color wash', cat: 'slow', desc: 'Crossfades smoothly from palette color to color.', swatch: ['#ff6ec7', '#06d6a0'],
    params: { space: P.space('strip', 'sides'), speed: P.speed(8, 'Per color (beats)'), intensity: P.intensity() },
    render(c) { const x = c.beat / c.p.speed; const col = palAt(c.pal, x / c.pal.length); fillAll(c.out, c.n, col[0] * c.p.intensity, col[1] * c.p.intensity, col[2] * c.p.intensity); } });

  def({ id: 'wave', name: 'Wave', cat: 'slow', desc: 'A brightness wave rolls along the strip in the palette gradient.', swatch: ['#0028ff', '#00c8ff'],
    params: { space: P.space('across', 'across', 'around', 'out', 'sides'), angle: P.angle(), spin: P.spin(), speed: P.speed(4), direction: P.direction(), waves: { type: 'range', def: 1.5, min: 0.25, max: 6, step: 0.25, label: 'Waves' }, floor: { type: 'range', def: 0.0, min: 0, max: 1, step: 0.01, label: 'Floor' } },
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
  def({ id: 'spotlight', name: 'Vocal spotlight', cat: 'slow', desc: 'A soft, steady warm glow in the middle of every strip, like a spotlight on a singer alone on stage. Shows place it where the voice is nearly alone; it holds still rather than following the voice.', swatch: ['#ffd9a0', '#000000'],
    params: { space: P.space('out', 'out'), width: P.size(0.35, 'Width'), tint: { type: 'select', def: 'warm', options: { warm: 'warm white', white: 'white', palette: 'the palette' }, label: 'Color' } },
    render(c) {
      clear(c.out);                                                   // (steady: a spotlight does not pulse with the voice)
      const w = Math.max(0.08, c.p.width), col = c.p.tint === 'palette' ? c.pal[0] : c.p.tint === 'white' ? [1, 1, 1] : [1, 0.78, 0.5];
      for (let i = 0; i < c.n; i++) { const x = c.n <= 1 ? 0 : (i / (c.n - 1)) - 0.5, g = Math.exp(-((x / w) ** 2) * 2.2); if (g > 0.01) set(c.out, i, col[0] * g, col[1] * g, col[2] * g, g); }
    } });

  def({ id: 'wobble', name: 'Wobble', cat: 'fast', desc: 'Pulses at the bass wobble\'s own speed (found on the separated bass track), not the beat\'s: the light opens out from the middle and closes again with every wobble.', swatch: ['#7c3aed', '#000000'],
    params: { per: { type: 'select', def: '2', options: { '1.5': '1.5 a beat', '2': '2 a beat', '3': '3 a beat (triplets)', '4': '4 a beat', '6': '6 a beat', '8': '8 a beat' }, label: 'Wobbles (when not found)' }, color: P.color() },
    render(c) {
      clear(c.out); const m = c.music || {}, per = (m.wobble && m.wobble.per) || parseFloat(c.p.per) || 2;
      const u = c.beat * per, env = Math.pow(0.5 - 0.5 * Math.cos(2 * Math.PI * u), 1.4) * (0.45 + 0.55 * clamp((m.bassLevel ?? 0.7) * 1.2));
      const col = colorFor(c, 0.5, Math.floor(u)), half = 0.12 + 0.38 * env;
      for (let i = 0; i < c.n; i++) { const x = away(c, (i + 0.5) / c.n) / 2; if (x <= half) { const v = env * (1 - 0.5 * x / Math.max(1e-3, half)); set(c.out, i, col[0] * v, col[1] * v, col[2] * v, v); } }
    } });

  def({ id: 'deep808', name: '808 glow', cat: 'medium', desc: 'On every 808 (a bass hit that holds, low and long) the whole room sinks into one deep color that dies away as slowly as the 808 does.', swatch: ['#7a0000', '#000000'],
    params: { color: P.color(), depth: { type: 'range', def: 0.75, min: 0.2, max: 1, step: 0.01, label: 'Brightness' } },
    render(c) {
      clear(c.out); const h = (c.music || {}).b808; if (!h || h.index < 0) return;
      const hold = Math.max(0.4, h.hold || 0.8), v = c.p.depth * (h.age < 0.04 ? h.age / 0.04 : Math.exp(-(h.age - 0.04) / (hold * 0.9))) * (0.6 + 0.4 * h.strength);
      if (v < 0.01) return; const col = colorFor(c, 0.5, h.index);
      for (let i = 0; i < c.n; i++) { const x = away(c, (i + 0.5) / c.n), g = v * (1 - 0.35 * x); set(c.out, i, col[0] * g, col[1] * g, col[2] * g, g); }
    } });

  def({ id: 'ticks', name: 'Hat ticks', cat: 'fast', desc: 'Tiny white ticks racing along the strip through a trap hi-hat roll, as fast as the roll.', swatch: ['#ffffff', '#000000'],
    params: { space: P.space('around', 'across', 'around', 'sides'), angle: P.angle(), spin: P.spin(), rate: { type: 'range', def: 6, min: 2, max: 12, step: 1, label: 'Ticks a beat (when not found)' }, travel: { type: 'range', def: 0.5, min: 0.1, max: 2, step: 0.05, label: 'Crossing (beats)' } },
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
      for (let i = 0; i < c.n; i++) { const x = away(c, (i + 0.5) / c.n), w = g * (1 - 0.5 * x); set(c.out, i, col[0] * w, col[1] * w, col[2] * w, w); }
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
    params: { space: P.space('out', 'out', 'across', 'sides'), angle: P.angle(), follow: { type: 'select', def: 'drums', options: { drums: 'kick and snare', kick: 'kick only', beat: 'every beat' }, label: 'Follows' },
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
    params: { space: P.space('around', 'across', 'around', 'sides'), angle: P.angle(), spin: P.spin(), speed: P.speed(1, 'Beats per block'), size: P.size(0.12, 'Block size'), direction: P.direction(), gap: { type: 'range', def: 0.6, min: 0, max: 0.9, step: 0.01, label: 'Gap' } },
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
    params: { space: P.space('across', 'across', 'around', 'sides'), angle: P.angle(), spin: P.spin(), speed: P.speed(2, 'One length per (beats)'), tail: P.size(0.3, 'Tail length'), color: P.color(), overlay: P.bool('Transparent background', true), ease: P.ease() },
    render(c) { clear(c.out); if (!c.p.overlay) fillAll(c.out, c.n, 0, 0, 0, 1); const u = c.beat / c.p.speed; const leg = Math.floor(u); const ph = ease(c, frac(u)); const fwd = leg % 2 === 0; const head = fwd ? ph : 1 - ph; const col = colorFor(c, head, leg); for (let i = 0; i < c.n; i++) { const x = i / Math.max(1, c.n - 1); const behind = fwd ? head - x : x - head; let v = behind >= 0 ? Math.exp(-behind / (c.p.tail * 0.5)) : 0; v = Math.max(v, Math.exp(-1 * ((x - head) / 0.03) ** 2)); if (v > 0.01) add(c.out, i, col[0] * v, col[1] * v, col[2] * v, v); } } });

  def({ id: 'alternate', name: 'Alternate', cat: 'medium', desc: 'Odd and even segments swap palette colors every beat.', swatch: ['#ff0000', '#0040ff'],
    params: { every: P.speed(1, 'Swap every (beats)'), size: { type: 'range', def: 1, min: 1, max: 8, step: 1, label: 'Segment width' }, intensity: P.intensity() },
    render(c) {
      // every swap the odd and even segments trade colors; every other swap the pair moves on along the palette.
      // (Moving the pair on every swap as well undid the swap with two colors: Walk Away's gold and purple stood still.)
      const k = Math.floor(c.beat / c.p.every), m = Math.floor(k / 2), a = palIdx(c.pal, 2 * m), b = palIdx(c.pal, 2 * m + 1);
      for (let i = 0; i < c.n; i++) { const col = (Math.floor(i / c.p.size) + k) % 2 === 0 ? a : b; set(c.out, i, col[0] * c.p.intensity, col[1] * c.p.intensity, col[2] * c.p.intensity); }
    } });

  def({ id: 'wipe', name: 'Wipe', cat: 'medium', desc: 'Fills the strip with the next palette color every cycle.', swatch: ['#00ffd0', '#8a2be2'],
    params: { speed: P.speed(4, 'One wipe per (beats)'), direction: P.direction(), soft: { type: 'range', def: 0.05, min: 0, max: 0.5, step: 0.01, label: 'Soft edge' } },
    render(c) { const u = c.beat / c.p.speed; const k = Math.floor(u), ph = frac(u); const a = palIdx(c.pal, k - 1), b = palIdx(c.pal, k); for (let i = 0; i < c.n; i++) { const x = dirX(c, i / Math.max(1, c.n - 1)); const m = clamp((ph * (1 + c.p.soft) - x) / (c.p.soft + 0.001)); set(c.out, i, lerp(a[0], b[0], m), lerp(a[1], b[1], m), lerp(a[2], b[2], m)); } } });

  def({ id: 'meter', name: 'Energy meter', cat: 'medium', desc: 'The song’s loudness fills the strip like a VU meter. Starts at: the loudness that is still empty (raise it in a loud part, so the meter bounces with every hit instead of standing full).', swatch: ['#00a000', '#ff0000'],
    params: { direction: P.direction(), color: P.color(), floor: { type: 'range', def: 0.05, min: 0, max: 1, step: 0.01, label: 'Floor' }, gain: { type: 'range', def: 1, min: 0.5, max: 2, step: 0.05, label: 'Gain' },
      low: { type: 'range', def: 0, min: 0, max: 0.9, step: 0.01, label: 'Starts at' } },
    render(c) { const lvl = clamp((c.energy - (c.p.low || 0)) / (1 - (c.p.low || 0)) * c.p.gain); for (let i = 0; i < c.n; i++) { const x = dirX(c, (i + 0.5) / c.n); const on = x < lvl; const col = c.p.color === 'cycle' ? palAt(c.pal, x) : colorFor(c, x, 0); const v = on ? 1 : c.p.floor; set(c.out, i, col[0] * v, col[1] * v, col[2] * v); } } });

  def({ id: 'spectrum', name: 'Spectrum', cat: 'medium', desc: 'A live spectrum analyser: bass at one end, treble at the other.', swatch: ['#ff0000', '#0000ff'],
    params: { direction: P.direction(), color: P.color(), floor: { type: 'range', def: 0.05, min: 0, max: 1, step: 0.01, label: 'Floor' } },
    render(c) { const nb = c.bands.length; for (let i = 0; i < c.n; i++) { const x = dirX(c, (i + 0.5) / c.n); const fb = x * (nb - 1); const j = Math.floor(fb); const e = lerp(c.bands[j], c.bands[Math.min(nb - 1, j + 1)], fb - j); const col = c.p.color === 'cycle' ? palAt(c.pal, x) : colorFor(c, x, 0); const v = c.p.floor + (1 - c.p.floor) * e * e; set(c.out, i, col[0] * v, col[1] * v, col[2] * v); } } });

  def({ id: 'scanner', name: 'Scanner', cat: 'medium', desc: 'A bright bar sweeps back and forth with a soft tail.', swatch: ['#ff0000', '#300000'],
    params: { space: P.space('across', 'across', 'around', 'sides'), angle: P.angle(), spin: P.spin(), speed: P.speed(2, 'Sweep (beats)'), size: P.size(0.12, 'Width'), color: P.color(), floor: { type: 'range', def: 0.0, min: 0, max: 1, step: 0.01, label: 'Floor' }, ease: P.ease('smooth') },
    render(c) { const u = c.beat / c.p.speed, e = ease(c, frac(u)); const pos = Math.floor(u) % 2 === 0 ? e : 1 - e; const col = colorFor(c, pos, Math.floor(u / 2)); for (let i = 0; i < c.n; i++) { const x = i / Math.max(1, c.n - 1); const v = c.p.floor + Math.exp(-1 * ((x - pos) / (c.p.size * 0.5)) ** 2); set(c.out, i, col[0] * v, col[1] * v, col[2] * v); } } });

  def({ id: 'blocks', name: 'Random blocks', cat: 'medium', desc: 'Every beat, random segments jump to random palette colors.', swatch: ['#ffd166', '#118ab2'],
    params: { every: P.speed(1, 'Change every (beats)'), size: { type: 'range', def: 3, min: 1, max: 12, step: 1, label: 'Block width' }, density: { type: 'range', def: 0.4, min: 0.1, max: 1, step: 0.01, label: 'Density' } },
    render(c) { const k = Math.floor(c.beat / c.p.every); for (let i = 0; i < c.n; i++) { const b = Math.floor(i / c.p.size); const on = hash(k * 131 + b * 17) < c.p.density; const col = palIdx(c.pal, Math.floor(hash(k * 977 + b * 31) * c.pal.length)); set(c.out, i, on ? col[0] : 0, on ? col[1] : 0, on ? col[2] : 0); } } });

  def({ id: 'battle', name: 'Color battle', cat: 'medium', desc: 'Two colors push against each other from both ends; the front line surges on every beat.', swatch: ['#ff0000', '#0040ff'],
    params: { space: P.space('across', 'across', 'sides'), angle: P.angle(), speed: P.speed(8, 'Battle length (beats)'), surge: { type: 'range', def: 0.15, min: 0, max: 0.5, step: 0.01, label: 'Beat surge' }, soft: { type: 'range', def: 0.08, min: 0, max: 0.5, step: 0.01, label: 'Front softness' } },
    render(c) { const a = c.pal[0], b = c.pal[1 % c.pal.length]; const u = c.beat / c.p.speed; const base = 0.5 + 0.35 * Math.sin(u * 2 * Math.PI); const side = Math.floor(c.beat) % 2 === 0 ? 1 : -1; const front = clamp(base + side * c.p.surge * c.kick, 0.02, 0.98); for (let i = 0; i < c.n; i++) { const x = i / Math.max(1, c.n - 1); const m = clamp((x - front) / (c.p.soft + 0.001) + 0.5); const glow = 1 + 0.6 * Math.exp(-1 * ((x - front) / 0.05) ** 2); set(c.out, i, lerp(a[0], b[0], m) * glow, lerp(a[1], b[1], m) * glow, lerp(a[2], b[2], m) * glow); } } });

  def({ id: 'ripple', name: 'Ripples', cat: 'medium', desc: 'Rings expand from their starting point (the middle unless set) on every beat over a transparent background.', swatch: ['#ffffff', '#00c8ff'],
    params: { space: P.space('out', 'out', 'across', 'around', 'sides'), angle: P.angle(), spin: P.spin(), every: P.speed(1, 'Every (beats)'), travel: P.speed(1, 'Travel time (beats)'), width: P.size(0.1, 'Ring width'), color: P.color() },
    render(c) { clear(c.out); const cur = Math.floor(c.beat / c.p.every); const alive = Math.ceil(c.p.travel / c.p.every) + 1; for (let k = cur; k > cur - alive && k >= 0; k--) { const age = (c.beat - k * c.p.every) / c.p.travel; if (age < 0 || age > 1) continue; const r = age; const col = colorFor(c, age, k); const fade = 1 - 0.55 * age; for (let i = 0; i < c.n; i++) { const x = away(c, i / Math.max(1, c.n - 1)); const v = Math.exp(-1 * ((x - r) / (c.p.width)) ** 2) * fade; if (v > 0.01) add(c.out, i, col[0] * v, col[1] * v, col[2] * v, v); } } } });

  def({ id: 'sparkle', name: 'Sparkles', cat: 'medium', desc: 'Sharp sparkles over a transparent background, denser when the music is louder.', swatch: ['#ffffff', '#000000'],
    params: { density: { type: 'range', def: 0.15, min: 0.01, max: 1, step: 0.01, label: 'Density' }, reactive: P.bool('Follow loudness', true), color: P.color(), life: { type: 'range', def: 0.12, min: 0.03, max: 0.5, step: 0.01, label: 'Life (beats)' } },
    render(c) { clear(c.out); const d = c.p.density * (c.p.reactive ? 0.3 + c.energy : 1); const slot = Math.floor(c.beat / c.p.life); for (let i = 0; i < c.n; i++) { for (let s = slot; s > slot - 2; s--) { if (hash(s * 7919 + i * 131) < d) { const ph = (c.beat - s * c.p.life) / c.p.life; const v = clamp(1 - ph * 0.5); const col = colorFor(c, i / c.n, s + i); add(c.out, i, col[0] * v, col[1] * v, col[2] * v, v); } } } } });

  def({ id: 'dualcomet', name: 'Dual comets', cat: 'medium', desc: 'Two comets in two colors launched from opposite ends, crossing in the middle.', swatch: ['#ff0000', '#0040ff'],
    params: { space: P.space('across', 'across', 'around', 'sides'), angle: P.angle(), spin: P.spin(), speed: P.speed(2, 'Crossing time (beats)'), tail: P.size(0.35, 'Tail length'), overlay: P.bool('Transparent background', true), ease: P.ease() },
    render(c) { clear(c.out); if (!c.p.overlay) fillAll(c.out, c.n, 0, 0, 0, 1); const u = c.beat / c.p.speed; const leg = Math.floor(u), ph = ease(c, frac(u)); const a = c.pal[0], b = c.pal[1 % c.pal.length]; const headA = leg % 2 === 0 ? ph : 1 - ph; const dirA = leg % 2 === 0 ? 1 : -1; for (let i = 0; i < c.n; i++) { const x = i / Math.max(1, c.n - 1); for (const [head, dir, col] of [[headA, dirA, a], [1 - headA, -dirA, b]]) { const behind = (head - x) * dir; let v = behind >= 0 ? Math.exp(-behind / (c.p.tail * 0.5)) : 0; v = Math.max(v, Math.exp(-1 * ((x - head) / 0.03) ** 2)); if (v > 0.01) add(c.out, i, col[0] * v, col[1] * v, col[2] * v, v); } } } });

  // ===== SPARSE (mostly dark) =====
  def({ id: 'meteors', name: 'Meteors', cat: 'medium', desc: 'Mostly dark. Now and then a meteor streaks in from a random spot with a fading tail.', swatch: ['#ffffff', '#101020'],
    params: { space: P.space('around', 'across', 'around', 'sides'), angle: P.angle(), spin: P.spin(), every: P.speed(1, 'Chance every (beats)'), chance: { type: 'range', def: 0.5, min: 0.05, max: 1, step: 0.05, label: 'Chance' }, travel: P.speed(2, 'Travel time (beats)'), tail: P.size(0.25, 'Tail length'), color: P.color() },
    render(c) { clear(c.out); const cur = Math.floor(c.beat / c.p.every); const alive = Math.ceil(c.p.travel / c.p.every) + 1;
      for (let k = cur; k > cur - alive && k >= 0; k--) { if (hash(k * 7919) > c.p.chance) continue; const age = (c.beat - k * c.p.every) / c.p.travel; if (age < 0 || age > 1) continue;
        // travel the whole strip, entering from one edge and leaving by the other: a head
        // that starts mid-strip and flies off leaves only a faint smear behind it
        const dir = hash(k * 31) < 0.5 ? 1 : -1;
        const head = dir > 0 ? -0.2 + age * 1.4 : 1.2 - age * 1.4;
        const col = colorFor(c, hash(k * 53), k); const fade = Math.sin(Math.PI * Math.min(1, age * 1.15)) ** 0.4;
        for (let i = 0; i < c.n; i++) { const x = i / Math.max(1, c.n - 1); const behind = (head - x) * dir; let v = behind >= 0 ? Math.exp(-behind / (c.p.tail * 0.5)) : 0; v = Math.max(v, Math.exp(-1 * ((x - head) / 0.025) ** 2)) * fade; if (v > 0.01) add(c.out, i, col[0] * v, col[1] * v, col[2] * v, v); } } } });

  def({ id: 'meteorfall', name: 'Meteor fall', cat: 'room', desc: 'For the meteor tubes (the kitchen): on every hit a meteor falls down a few tubes picked at random (or shoots up them), a bright head with a fading tail; with "Grows", more of them and quicker as the clip goes on (a buildup). On a strip it falls along the strip.', swatch: ['#ffffff', '#5cc8ff'],
    params: { follow: { type: 'select', def: 'drums', options: { drums: 'kick and snare', kick: 'the kick', snare: 'the snare', beat: 'the beat', build: 'a build: beats, eighths, sixteenths' }, label: 'Falls on' },
      many: { type: 'range', def: 2, min: 1, max: 8, step: 1, label: 'Tubes a hit' },
      fall: { type: 'range', def: 0.5, min: 0.15, max: 3, step: 0.05, label: 'Fall time (s)' },
      tail: { type: 'range', def: 4, min: 0, max: 10, step: 1, label: 'Tail (LEDs)' },
      grows: P.bool('Grows (more and quicker toward the end)', false),
      way: { type: 'select', def: 'down', options: { down: 'falling', up: 'shooting up', split: 'falling on the kick, shooting up on the snare' }, label: 'Way' }, color: P.color() },
    render(c) {
      clear(c.out);
      const tb = c.tubes || { count: 1, per: c.n }, per = tb.per, count = tb.count, m = c.music || {}, k = c.clipT;
      const fall = c.p.fall * (c.p.grows ? 1 - 0.45 * k : 1), tail = Math.round(c.p.tail);
      const many = Math.max(1, Math.min(count, c.p.grows ? Math.round(c.p.many * (0.3 + 0.7 * k)) : Math.round(c.p.many)));
      // the hits whose meteors are still falling: [time, an id that picks their tubes]
      const hits = [];
      const steps = (sub, beat0) => { const b = c.clipBeat * sub, last = Math.floor(b);
        for (let q = last; q >= 0 && (b - q) * c.beatSec / sub <= fall; q--) hits.push([c.t - (b - q) * c.beatSec / sub, sub * 100003 + q + beat0]); };
      if (c.p.follow === 'build') steps(k < 0.5 ? 1 : k < 0.75 ? 2 : 4, Math.round(c.clipT0 * 10));
      else if (c.p.follow === 'beat' || !m.hasDrums || !m.hitsBetween) steps(1, Math.round(c.clipT0 * 10));
      else for (const st of c.p.follow === 'drums' ? ['kick', 'snare', 'roll'] : c.p.follow === 'kick' ? ['kick'] : ['snare', 'roll'])
        for (const th of m.hitsBetween(st, c.t - fall, c.t + 1e-6)) if (th >= c.clipT0 - 0.02) hits.push([th, Math.round(th * 1000), st]);
      for (const [th, id, st] of hits) {
        const prog = (c.t - th) / fall; if (prog < 0 || prog > 1) continue;
        const up = c.p.way === 'up' || (c.p.way === 'split' && st !== undefined && st !== 'kick');   // (shooting up from the bottom)
        const head = prog * (per + tail + 1) - 1;                      // enters at the top, leaves past the bottom
        const used = new Set();
        for (let j = 0; used.size < many && j < many * 6; j++) used.add(Math.floor(hash(id * 7919 + j * 104729 + 7) * count));
        let w = 0;
        for (const tube of used) {
          const col = colorFor(c, tube / Math.max(1, count - 1), id + w++), hot = [0.55 + 0.45 * col[0], 0.55 + 0.45 * col[1], 0.55 + 0.45 * col[2]];
          for (let led = 0; led < per; led++) {
            const d = head - led; if (d < -0.5 || d > tail + 1) continue;
            const v = d < 0.5 ? 1 - Math.abs(d) : Math.max(0, 1 - (d - 0.5) / (tail + 0.5));
            const cc = d < 0.75 ? hot : col, i = tube * per + (up ? per - 1 - led : led);
            if (v > 0.01) add(c.out, i, cc[0] * v, cc[1] * v, cc[2] * v, v);
          }
        }
      }
    } });

  // ===== THE HOUSE =====
  // Effects that play across the house as one space, from where each segment is on the floor plan (the party app's
  // house map; c.where from app.js spaceOf: an [x, y] in feet for each segment, null where one is not placed, and the
  // house: its size, its middle, its rooms). A light not on the map gets stand-in spots along the house, so these
  // play everywhere.
  const spots = c => (c.where && c.where.pos) || null;
  const houseOf = c => (c.where && c.where.house) || { w: 40, d: 14, c: [20, 7], R: 21, rooms: [] };
  const dist = (p, q) => Math.hypot(p[0] - q[0], p[1] - q[1]);
  const smooth = x => x * x * (3 - 2 * x);
  function inPoly(p, poly) { let k = false; for (let i = 0, j = poly.length - 1; i < poly.length; j = i++) { const [xi, yi] = poly[i], [xj, yj] = poly[j]; if ((yi > p[1]) !== (yj > p[1]) && p[0] < (xj - xi) * (p[1] - yi) / (yj - yi) + xi) k = !k; } return k; }
  /** Which end of the house a spot belongs to: -1 the leftmost room, 1 the rightmost, between them the rooms in
   *  the middle (by how far across they are); with no rooms, the left or right half. */
  function teamOf(p, H) {
    const rs = H.rooms || []; if (rs.length < 2) return p[0] < H.c[0] ? -1 : 1;
    const k = rs.findIndex(r => inPoly(p, r.poly));
    if (k === 0) return -1; if (k === rs.length - 1) return 1;
    if (k < 0) return p[0] <= rs[0].x1 ? -1 : p[0] >= rs[rs.length - 1].x0 ? 1 : 0;
    return clamp((p[0] - rs[0].x1) / Math.max(1, rs[rs.length - 1].x0 - rs[0].x1)) * 2 - 1;
  }

  def({ id: 'shockwave', name: 'Shockwave', cat: 'accent', desc: 'The house map: a white wave bursts out from the middle of the house (or a wall) and races out to every wall, the clip\'s color following it; for a drop\'s first beat.', swatch: ['#ffffff', '#7c3aed'],
    params: { from: { type: 'select', def: 'middle', options: { middle: 'the middle of the house', left: 'the left wall', right: 'the right wall' }, label: 'From' },
      speed: { type: 'range', def: 60, min: 15, max: 150, step: 5, label: 'Speed (ft/s)' }, width: { type: 'range', def: 3, min: 1, max: 10, step: 0.5, label: 'Wave (ft)' } },
    render(c) {
      clear(c.out); const P = spots(c); if (!P) return; const H = houseOf(c);
      const o = c.p.from === 'left' ? [0, H.c[1]] : c.p.from === 'right' ? [H.w, H.c[1]] : H.c;
      const r = (c.t - c.clipT0) * c.p.speed, col = c.pal[0];
      for (let i = 0; i < c.n; i++) {
        const p = P[i]; if (!p) continue;
        const u = r - dist(p, o); if (u < -c.p.width * 2) continue;
        const front = Math.exp(-((u / c.p.width) ** 2)), trail = u > 0 ? 0.85 * Math.exp(-u / c.p.speed / 0.35) : 0, v = Math.max(front, trail);
        if (v < 0.01) continue;
        set(c.out, i, Math.min(1, front + col[0] * trail), Math.min(1, front + col[1] * trail), Math.min(1, front + col[2] * trail), v);
      }
    } });

  def({ id: 'closein', name: 'Closing in', cat: 'room', desc: 'The house map, for a build: every snare (every beat without drums) lights stretches near the far walls first, then closer and closer to the middle, all of it meeting there as the build ends.', swatch: ['#f59e0b', '#000000'],
    params: { decay: { type: 'range', def: 0.3, min: 0.1, max: 1, step: 0.05, label: 'Fade (s)' }, color: P.color() },
    render(c) {
      clear(c.out); const P = spots(c); if (!P) return; const H = houseOf(c), m = c.music || {};
      const snares = m.hasDrums && m.hitsBetween && m.hitsBetween('snare', c.clipT0, c.clipT1).length >= 2;
      const hits = snares ? m.hitsBetween('snare', c.t - c.p.decay, c.t + 1e-6) : [];
      if (!snares) for (let q = Math.floor(c.clipBeat); q >= 0 && (c.clipBeat - q) * c.beatSec <= c.p.decay; q--) hits.push(c.t - (c.clipBeat - q) * c.beatSec);
      for (const th of hits) {
        const age = c.t - th; if (age < 0 || age > c.p.decay) continue;
        const k = clamp((th - c.clipT0) / Math.max(0.1, c.clipT1 - c.clipT0)), target = 1 - k, v = Math.pow(1 - age / c.p.decay, 1.5), id = Math.round(th * 1000);
        const col = colorFor(c, k, id);
        for (let i = 0; i < c.n; i++) {
          const p = P[i]; if (!p) continue;
          const dn = dist(p, H.c) / H.R; if (Math.abs(dn - target) > 0.16) continue;
          const sector = Math.floor((Math.atan2(p[1] - H.c[1], p[0] - H.c[0]) + Math.PI) * 2.5);
          if (hash(id * 31 + Math.floor(dn * 14) * 7 + sector * 101) > 0.7) continue;     // stretches, not the whole ring
          add(c.out, i, col[0] * v, col[1] * v, col[2] * v, v);
        }
      }
    } });

  def({ id: 'rollthunder', name: 'Rolling thunder', cat: 'accent', desc: 'The house map: lightning strikes somewhere in the house and the flash rolls out from there, the far end a moment later, flickering as it goes. Once a clip, or a new strike every bar.', swatch: ['#e0ecff', '#000000'],
    params: { every: { type: 'select', def: 'once', options: { once: 'once', bar: 'every bar' }, label: 'Strikes' }, speed: { type: 'range', def: 35, min: 10, max: 120, step: 5, label: 'Rolls at (ft/s)' } },
    render(c) {
      clear(c.out); const P = spots(c); if (!P) return; const H = houseOf(c);
      const bar = c.p.every === 'bar', k = bar ? Math.floor(c.clipBeat / 4) : 0, t0 = c.clipT0 + k * 4 * c.beatSec;
      const seed = Math.round(c.clipT0 * 10) * 7 + k, o = [hash(seed) * H.w, hash(seed + 1) * H.d];
      for (let i = 0; i < c.n; i++) {
        const p = P[i]; if (!p) continue;
        const a = c.t - t0 - dist(p, o) / c.p.speed; if (a < 0) continue;
        const v = a < 0.05 ? 1 : a < 0.09 ? 0.12 : a < 0.16 ? 0.9 : Math.exp(-(a - 0.16) / 0.22); if (v < 0.01) continue;
        set(c.out, i, 0.88 * v, 0.93 * v, v, v);
      }
    } });

  def({ id: 'pingpong', name: 'House ping-pong', cat: 'fast', desc: 'The house map: the kick lights the left of the house in the first color, the snare the right in the second, trading across the middle.', swatch: ['#22d3ee', '#ec4899'],
    params: { decay: { type: 'range', def: 0.22, min: 0.08, max: 0.6, step: 0.02, label: 'Fade (s)' } },
    render(c) {
      clear(c.out); const P = spots(c); if (!P) return; const H = houseOf(c), m = c.music || {};
      const env = h => h && h.index >= 0 ? Math.exp(-h.age / c.p.decay) * (0.6 + 0.4 * (h.strength ?? 1)) : 0;
      const k = env(m.kick), s = env(m.snare), a = c.pal[0], b = c.pal[1 % c.pal.length];
      for (let i = 0; i < c.n; i++) {
        const p = P[i]; if (!p) continue;
        const left = p[0] < H.c[0], v = left ? k : s, col = left ? a : b; if (v < 0.01) continue;
        set(c.out, i, col[0] * v, col[1] * v, col[2] * v, v);
      }
    } });

  def({ id: 'roombattle', name: 'Room battle', cat: 'room', desc: 'The house map: the rooms at the two ends of the house each take a color and trade the lead phrase by phrase (the one in the lead full, the other low; the rooms between blend the two), meeting in one color for the clip\'s last beats.', swatch: ['#7c3aed', '#f59e0b'],
    params: { phrase: P.speed(8, 'Lead changes every (beats)'), low: { type: 'range', def: 0.25, min: 0, max: 0.8, step: 0.05, label: 'The other side' },
      merge: { type: 'range', def: 0, min: 0, max: 16, step: 1, label: 'Meet for the last (beats)' } },
    render(c) {
      clear(c.out); const P = spots(c); if (!P) return; const H = houseOf(c);
      const ph = c.clipBeat / c.p.phrase, n = Math.floor(ph), lead = n % 2 === 0 ? -1 : 1;
      const swing = n === 0 ? 1 : smooth(clamp(frac(ph) * c.p.phrase));            // (a beat's swing into the new lead)
      const merged = c.p.merge > 0 && c.clipBeats - c.clipBeat <= c.p.merge, A = c.pal[0], B = c.pal[1 % c.pal.length];
      for (let i = 0; i < c.n; i++) {
        const p = P[i]; if (!p) continue;
        if (merged) { set(c.out, i, A[0], A[1], A[2], 1); continue; }
        const u = (teamOf(p, H) + 1) / 2, col = [lerp(A[0], B[0], u), lerp(A[1], B[1], u), lerp(A[2], B[2], u)];
        const now = lead < 0 ? 1 - u : u, before = lead < 0 ? u : 1 - u, on = c.p.low + (1 - c.p.low) * lerp(before, now, swing);
        set(c.out, i, col[0] * on, col[1] * on, col[2] * on, 1);
      }
    } });

  def({ id: 'housescan', name: 'House scanner', cat: 'medium', desc: 'The house map: a band of light sweeps the house from one end to the other and back, one way each bar.', swatch: ['#ffffff', '#000000'],
    params: { speed: P.speed(4, 'One way (beats)'), width: { type: 'range', def: 3, min: 1, max: 12, step: 0.5, label: 'Band (ft)' }, color: P.color() },
    render(c) {
      clear(c.out); const P = spots(c); if (!P) return; const H = houseOf(c);
      const ph = c.clipBeat / c.p.speed, k = Math.floor(ph), u = frac(ph), x = (k % 2 ? 1 - u : u) * (H.w + 2 * c.p.width) - c.p.width, col = colorFor(c, u, k);
      for (let i = 0; i < c.n; i++) {
        const p = P[i]; if (!p) continue;
        const v = Math.exp(-(((p[0] - x) / c.p.width) ** 2)); if (v < 0.01) continue;
        set(c.out, i, col[0] * v, col[1] * v, col[2] * v, v);
      }
    } });

  def({ id: 'housechase', name: 'House chase', cat: 'medium', desc: 'The house map: a comet runs round the whole house, every light one path around the middle, its tail behind it.', swatch: ['#ec4899', '#000000'],
    params: { speed: P.speed(8, 'Once round (beats)'), tail: { type: 'range', def: 0.25, min: 0.05, max: 0.6, step: 0.01, label: 'Tail (of the way round)' }, color: P.color() },
    render(c) {
      clear(c.out); const P = spots(c); if (!P) return; const H = houseOf(c);
      const head = frac(c.clipBeat / c.p.speed), col = colorFor(c, head, Math.floor(c.clipBeat / c.p.speed));
      for (let i = 0; i < c.n; i++) {
        const p = P[i]; if (!p) continue;
        const a = frac(Math.atan2(p[1] - H.c[1], p[0] - H.c[0]) / (2 * Math.PI) + 1), back = frac(head - a);
        if (back > c.p.tail) continue;
        const v = Math.pow(1 - back / c.p.tail, 2), hot = back < 0.015 ? 1 : 0;      // the head white-hot
        set(c.out, i, Math.min(1, col[0] * v + hot), Math.min(1, col[1] * v + hot), Math.min(1, col[2] * v + hot), v);
      }
    } });

  def({ id: 'lighthouse', name: 'Lighthouse', cat: 'slow', desc: 'The house map: a soft beam turns slowly around the middle of the house over a low glow; for calm parts (it only ever swells).', swatch: ['#93c5fd', '#0b1020'],
    params: { speed: P.speed(32, 'Once round (beats)'), width: { type: 'range', def: 70, min: 20, max: 180, step: 5, label: 'Beam (degrees)' }, floor: { type: 'range', def: 0.15, min: 0, max: 0.6, step: 0.05, label: 'Glow' } },
    render(c) {
      clear(c.out); const P = spots(c); if (!P) return; const H = houseOf(c), col = c.pal[0], beam = frac(c.beat / c.p.speed), half = c.p.width / 720;
      for (let i = 0; i < c.n; i++) {
        const p = P[i]; if (!p) continue;
        const a = frac(Math.atan2(p[1] - H.c[1], p[0] - H.c[0]) / (2 * Math.PI) + 1), d = Math.abs(frac(a - beam + 0.5) - 0.5);
        const s = d < half ? 0.5 + 0.5 * Math.cos(Math.PI * d / half) : 0, v = c.p.floor + (1 - c.p.floor) * s;
        set(c.out, i, col[0] * v, col[1] * v, col[2] * v, 1);
      }
    } });

  def({ id: 'housegradient', name: 'House palette', cat: 'slow', desc: 'The house map: the palette laid across the whole house end to end, drifting slowly, so every light is part of one picture.', swatch: ['#7c3aed', '#22d3ee'],
    params: { speed: P.speed(64, 'Drifts round (beats)'), spread: { type: 'range', def: 1, min: 0.25, max: 3, step: 0.25, label: 'Colors across the house' }, intensity: P.intensity() },
    render(c) {
      clear(c.out); const P = spots(c); if (!P) return; const H = houseOf(c), drift = c.beat / c.p.speed, k = c.p.intensity;
      for (let i = 0; i < c.n; i++) {
        const p = P[i]; if (!p) continue;
        const col = palAt(c.pal, p[0] / H.w * c.p.spread + drift); set(c.out, i, col[0] * k, col[1] * k, col[2] * k, 1);
      }
    } });

  def({ id: 'tubeform', name: 'Tube shape', cat: 'room', desc: 'For the meteor tubes (the kitchen): how each tube carries the show\'s color down its LEDs while the clip plays: the whole tube, an orb floating in the middle (fading to both ends), orbs drifting slowly up and down, or a glow from the top. Draws nothing itself.', swatch: ['#5cc8ff', '#000000'],
    params: { shape: { type: 'select', def: 'orb', options: { full: 'the whole tube', orb: 'a floating orb', drift: 'orbs drifting', top: 'a glow from the top' }, label: 'Shape' } },
    render(c) { clear(c.out); } });

  def({ id: 'pulsar', name: 'Pulsar', cat: 'medium', desc: 'Dark strip; on every beat one or two points flare up somewhere and fade.', swatch: ['#8ad8ff', '#000010'],
    params: { every: P.speed(1, 'Flare every (beats)'), count: P.count(1, 4), size: P.size(0.08, 'Flare size'), decay: P.decay(0.6), color: P.color() },
    render(c) { clear(c.out); const k = Math.floor(c.beat / c.p.every); const ph = (c.beat - k * c.p.every) / c.p.every; const env = Math.exp(-ph / (c.p.decay * 0.6 + 0.02));
      for (let m = 0; m < c.p.count; m++) { const x0 = 0.05 + 0.9 * hash(k * 101 + m * 17); const col = colorFor(c, x0, k + m); for (let i = 0; i < c.n; i++) { const x = i / Math.max(1, c.n - 1); const v = Math.exp(-1 * ((x - x0) / c.p.size) ** 2) * env; if (v > 0.01) add(c.out, i, col[0] * v, col[1] * v, col[2] * v, v); } } } });

  def({ id: 'orbit', name: 'Orbit', cat: 'medium', desc: 'Two or three bright points bounce end to end at different speeds over darkness, crossing and separating.', swatch: ['#ff2fa0', '#000000'],
    params: { space: P.space('around', 'across', 'around', 'sides'), angle: P.angle(), spin: P.spin(), speed: P.speed(4, 'Slowest lap (beats)'), count: P.count(2, 4), tail: P.size(0.12, 'Tail length'), color: P.color() },
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
        else v = Math.exp(-1 * (((away(c, x) - travel) / c.p.size) ** 2)) * env;
        if (v > 0.01) add(c.out, i, col[0] * v, col[1] * v, col[2] * v, v);
      } } });

  def({ id: 'leadtrace', name: 'Lead trace', cat: 'signature', desc: 'A streak for every note of the wide lead instrument \u2014 a saw synth, horn line or riff. Its place on the strip follows the note played; its color is the clip\u2019s (or the note\u2019s, with Color by note).', swatch: ['#00ffd0', '#8a2be2'],
    params: { space: P.space('across', 'across', 'around', 'sides'), angle: P.angle(), spin: P.spin(), tail: P.size(0.25, 'Tail length'), decay: P.decay(0.4), byNote: P.bool('Color by note', false), spread: { type: 'range', def: 0.8, min: 0, max: 1, step: 0.05, label: 'Spread by pitch' } },
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
    params: { space: P.space('out', 'out', 'across', 'sides'), angle: P.angle(), color: P.color(), gain: { type: 'range', def: 1.2, min: 0.4, max: 3, step: 0.1, label: 'Sensitivity' }, floor: { type: 'range', def: 0, min: 0, max: 0.4, step: 0.01, label: 'Floor' }, direction: P.direction('out') },
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
    params: { space: P.space('out', 'out', 'across', 'sides'), angle: P.angle(), byNote: P.bool('Color by note', false), decay: P.decay(0.5), floor: { type: 'range', def: 0.06, min: 0, max: 0.5, step: 0.01, label: 'Floor' }, direction: P.direction('out') },
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

  def({ id: 'drumhits', name: 'Drum hits', cat: 'medium', desc: 'The whole light blooms on the drums: the first color on every kick, the second on every snare, and fades between. Made for small lamps, which play the drums while the long strips move. Split: with two or more small lamps (light poles in the corners), the first half of them in the room order take the kick and the rest the snare, a call and response across the room; Together: every lamp on every hit.', swatch: ['#8000ff', '#00ff00'],
    params: { follow: { type: 'select', def: 'drums', options: { drums: 'kick and snare', kick: 'kick only', snare: 'snare only' }, label: 'Follows' }, decay: P.decay(0.35),
      floor: { type: 'range', def: 0.0, min: 0, max: 1, step: 0.01, label: 'Floor' },
      split: { type: 'select', def: 'together', options: { together: 'every lamp on every hit', split: 'kick one side, snare the other' }, label: 'Lamps' } },
    render(c) {
      clear(c.out); const m = c.music || {}, tau = c.p.decay * 0.4 + 0.03;
      let ek, es;
      if (m.hasDrums) {
        const hit = h => (h && h.index >= 0 ? Math.exp(-h.age / tau) * (0.6 + 0.4 * h.strength) : 0);
        ek = c.p.follow === 'snare' ? 0 : hit(m.kick); es = c.p.follow === 'kick' ? 0 : hit(m.snare);
      } else { ek = Math.exp(-c.phase * c.beatSec / tau); es = 0; }      // no drums known: on the beat
      if (c.p.split === 'split' && es !== 0 && c.room && c.room.kinds) {   // this lamp's side among the small lamps
        const shorts = c.room.kinds.map((k, i) => (k === 'short' ? i : -1)).filter(i => i >= 0), at = shorts.indexOf(c.room.i);
        if (shorts.length >= 2 && at >= 0) { if (at < shorts.length / 2) es = 0; else ek = 0; }
      }
      const col = ek >= es ? c.pal[0] : c.pal[1 % c.pal.length], v = Math.max(c.p.floor, ek, es);
      fillAll(c.out, c.n, col[0] * v, col[1] * v, col[2] * v, v);
    } });

  def({ id: 'drumstrobe', name: 'Drum strobe', cat: 'fast', desc: 'A hard flash on every real kick (or snare) hit and nothing in between, so it is exactly as busy as the drummer. With Grows on it starts at half brightness and is full on the drop.', swatch: ['#ffffff', '#000000'],
    params: { follow: { type: 'select', def: 'kick', options: { kick: 'the kick', snare: 'the snare', both: 'kick and snare' }, label: 'Flashes on' },
      length: { type: 'range', def: 0.05, min: 0.02, max: 0.25, step: 0.01, label: 'Flash length (s)' }, grows: P.bool('Grows toward the end', false), white: P.bool('White', false), color: P.color() },
    render(c) {
      clear(c.out); const m = c.music || {}; if (!m.hasDrums) return;
      let h = null;
      for (const x of [c.p.follow !== 'snare' ? m.kick : null, c.p.follow !== 'kick' ? m.snare : null, c.p.follow !== 'kick' ? m.roll : null]) if (x && x.index >= 0 && (!h || x.age < h.age)) h = x;
      if (!h) return;
      // the flash holds for its length, but never past 60% of the way to the next hit: a slow snare gets a long
      // solid white (the lights see it), a sixteenth-note roll still strobes rather than blurring into white
      let len = c.p.length;
      if (m.hitsBetween) {
        const at = c.t - h.age, streams = c.p.follow === 'kick' ? ['kick'] : c.p.follow === 'snare' ? ['snare', 'roll'] : ['kick', 'snare', 'roll'];
        let next = Infinity;
        // (a hit within 40 ms is the same one, doubled or flammed: not the next; and no flash under 40 ms)
        for (const st of streams) { const n = m.hitsBetween(st, at + 0.04, at + 1)[0]; if (n !== undefined && n < next) next = n; }
        if (next < Infinity) len = Math.min(len, Math.max(0.04, 0.6 * (next - at)));
      }
      if (h.age > len) return;
      // growing: from half brightness, steadily to full on the drop (from faint, the first flashes were lost)
      const v = c.p.grows ? (0.5 + 0.5 * c.clipT) * (0.85 + 0.15 * h.strength) : 0.6 + 0.4 * h.strength;
      const col = c.p.white ? [1, 1, 1] : colorFor(c, 0.5, h.index);
      // (fully opaque: its brightness as its alpha too was squared on the lights, a half-bright flash a quarter)
      fillAll(c.out, c.n, col[0] * v, col[1] * v, col[2] * v, 1);
    } });

  def({ id: 'growlhits', name: 'Growl hits', cat: 'fast', desc: 'For a dubstep or rave drop: every growl and saw stab (not the drums) slices a stretch or two of the strips in its color, somewhere new each time, held long enough for the lights to show it and then gone. Made for the overhead strings while the lamps keep the drums.', swatch: ['#ff2fa0', '#000000'],
    params: { slices: { type: 'range', def: 2, min: 1, max: 4, step: 1, label: 'Stretches per stab' }, width: { type: 'range', def: 0.18, min: 0.06, max: 0.5, step: 0.01, label: 'Stretch size' },
      decay: P.decay(0.16), color: { ...P.color(), def: 'second' } },
    render(c) {
      clear(c.out); const h = (c.music || {}).growl; if (!h || h.index < 0) return;
      // full for 0.07 s (a frame or so of a light updating 15 times a second), then gone quickly
      const v = (h.age < 0.07 ? 1 : Math.exp(-(h.age - 0.07) / Math.max(0.03, c.p.decay))) * (0.75 + 0.25 * h.strength);
      if (v < 0.01) return;
      const col = colorFor(c, hash(h.index * 13 + 1), h.index), hot = Math.max(0, 1 - h.age / 0.05) * 0.5;   // (a white-hot start)
      const rgb = [0, 1, 2].map(k => col[k] + (1 - col[k]) * hot), room = c.room ? c.room.i : 0;
      const lit = (i, j) => { const a = hash(h.index * 7919 + j * 104729 + room * 31 + 3), x = (i + 0.5) / c.n; return Math.abs(x - a) <= c.p.width / 2; };
      for (let i = 0; i < c.n; i++) {
        let on = c.n <= 12;                                                      // (a lamp: the whole of it)
        for (let j = 0; !on && j < Math.round(c.p.slices); j++) on = lit(i, j);
        if (on) set(c.out, i, rgb[0], rgb[1], rgb[2], v);
      }
    } });

  def({ id: 'blackout', name: 'Blackout strobe', cat: 'fast', desc: 'Cuts whatever is below to black in a rhythm. Put it on top of a base effect.', swatch: ['#000000', '#444444'],
    params: { every: { type: 'beats', def: 0.5, label: 'Cut every (beats)' }, duty: { type: 'range', def: 0.5, min: 0.05, max: 0.95, step: 0.01, label: 'Dark length' } },
    render(c) { clear(c.out); const u = c.beat / c.p.every; if (frac(u) > 1 - c.p.duty) fillAll(c.out, c.n, 0, 0, 0, 1); } });

  def({ id: 'hyperchase', name: 'Hyper chase', cat: 'fast', desc: 'Tiny blocks race along the strip, several per beat.', swatch: ['#00ffd0', '#ff2fa0'],
    params: { space: P.space('around', 'across', 'around', 'sides'), angle: P.angle(), spin: P.spin(), speed: { type: 'beats', def: 0.25, label: 'Beats per block' }, size: P.size(0.08, 'Block size'), direction: P.direction() },
    render(c) { const blocks = Math.max(2, Math.round(1 / c.p.size)); const s = c.beat / c.p.speed; for (let i = 0; i < c.n; i++) { const x = dirX(c, i / c.n); const u = x * blocks - s; const k = Math.floor(u); const on = frac(u) > 0.7; const col = palIdx(c.pal, k); set(c.out, i, on ? col[0] : 0, on ? col[1] : 0, on ? col[2] : 0); } } });

  def({ id: 'lightning', name: 'Lightning', cat: 'fast', desc: 'Lightning striking the poles and the hanging tubes, which already look like bolts: strike after strike, 2 to 5 of them at a time, picked at random, now and then one that lights the whole house. Every flash held long enough for the lights to show it. Timed in seconds, not beats, so it works on a sound effect too.', swatch: ['#ffffff', '#8ad8ff'],
    params: { rate: { type: 'range', def: 0.5, min: 0.05, max: 1, step: 0.01, label: 'How often' }, color: P.color() },
    render(c) {
      // A strike may start every 0.1 s (rate: the chance it does; 0.5 is about four a second, 1 eight). The poles and
      // the 20 tubes are what it strikes (the user: they look like lightning already): each strike 2 to 5 of them, picked
      // at random, the whole pole or tube; one in seven lights the whole house, the strings too. Each holds 0.1-0.14 s:
      // flashes under a frame were never seen (the lights update as few as 15 times a second; the user: at least 0.1 to
      // 0.2 s). The clip opens with one that lights the whole house (a short "thunder" moment always gets its strike).
      // With no pole or tube playing (testing on one strip), strikes land on a spot of the house instead, as before.
      clear(c.out); const SLOT = 0.1, P = spots(c), H = houseOf(c);
      if (c.t - c.clipT0 < 0.12) { const col = colorFor(c, 0.5, 0); fillAll(c.out, c.n, col[0], col[1], col[2], 1); return; }
      const kinds = c.kinds || [], T = kinds.includes('tubes') ? 20 : 0, U = T + kinds.filter(k => k === 'short').length;
      const now = Math.floor(c.t / SLOT);
      for (let k = now; k >= now - 2; k--) {                                  // (a strike still holding from the slots before)
        if (k < 0 || hash(k * 31) > c.p.rate * 0.8) continue;
        const hold = 0.1 + 0.04 * hash(k * 31 + 5), u = c.t - k * SLOT; if (u < 0 || u >= hold) continue;
        // only the newest strike shows (2 to 5 at a time, the user): the loop stops at the first still holding; the
        // one before it has had its 0.1 s by then
        const big = hash(k * 31 + 9) < 1 / 7, col = colorFor(c, hash(k * 7 + 3), k);
        if (big) { fillAll(c.out, c.n, col[0], col[1], col[2], 1); break; }
        if (U >= 2) {
          // the poles and tubes struck: 2-5 of them (tubes 0-19, then the poles in the order the lights stand)
          const m = Math.min(U, 2 + Math.floor(hash(k * 31 + 11) * 4)), hit = new Set();
          for (let j = 0; hit.size < m; j++) { let x = Math.floor(hash(k * 97 + j * 13) * U); while (hit.has(x)) x = (x + 1) % U; hit.add(x); }
          if (c.kind === 'tubes') { for (let i = 0; i < c.n; i++) if (hit.has(Math.min(19, Math.floor(i * 20 / c.n)))) set(c.out, i, col[0], col[1], col[2], 1); }
          else if (c.kind === 'short' && hit.has(T + (c.pole || 0))) fillAll(c.out, c.n, col[0], col[1], col[2], 1);
          break;
        }
        const o = [hash(k * 7 + 1) * H.w, hash(k * 7 + 2) * H.d], r = 4 + 3 * hash(k * 7 + 4);
        const room = c.room ? c.room.i : 0, a = hash(k * 7 + 1 + room * 13), w = 0.25 + 0.35 * hash(k * 7 + 2 + room * 13);
        for (let i = 0; i < c.n; i++) {
          const p = P && P[i];
          const hit = p ? Math.hypot(p[0] - o[0], p[1] - o[1]) <= r : c.n <= 12 ? hash(k * 7 + 5 + room * 13) < 0.4 : Math.abs((i + 0.5) / c.n - a) <= w / 2;
          if (hit) set(c.out, i, col[0], col[1], col[2], 1);
        }
        break;
      }
    } });

  def({ id: 'chaos', name: 'Chaos', cat: 'fast', desc: 'The clip\u2019s colors race along the strip, dense and fast, jumping direction on every beat. Starts out as a rainbow.', swatch: ['#ff0000', '#00ff00'], palette: 'Rainbow',
    params: { speed: P.speed(4, 'Cycle (beats)'), stretch: { type: 'range', def: 2, min: 0.5, max: 6, step: 0.25, label: 'Repeats' } },
    render(c) { const k = Math.floor(c.beat); const dir = hash(k * 13) < 0.5 ? 1 : -1; const s = frac(c.beat / c.p.speed) * dir + hash(k * 17); for (let i = 0; i < c.n; i++) { const x = i / c.n; const col = palAt(c.pal, x * c.p.stretch + s); set(c.out, i, col[0], col[1], col[2]); } } });

  def({ id: 'flash', name: 'Beat flash', cat: 'fast', desc: 'Whole strip flashes a palette color on every beat and decays.', swatch: ['#ff2fa0', '#000000'],
    params: { every: P.speed(1, 'Every (beats)'), decay: P.decay(0.3), color: P.color(), overlay: P.bool('Transparent when dark', true) },
    render(c) { clear(c.out); const u = c.beat / c.p.every; const ph = frac(u) * c.p.every; const v = Math.exp(-ph / (c.p.decay * 0.4 + 0.02)); const col = colorFor(c, 0.5, Math.floor(u)); fillAll(c.out, c.n, col[0] * v, col[1] * v, col[2] * v, c.p.overlay ? v : 1); } });

  def({ id: 'scramble', name: 'Scramble', cat: 'fast', desc: 'Random slices of the strip jump to random colors several times a beat.', swatch: ['#c6ff00', '#ff00c8'],
    params: { rate: { type: 'range', def: 4, min: 1, max: 16, step: 1, label: 'Changes per beat' }, density: { type: 'range', def: 0.5, min: 0.05, max: 1, step: 0.01, label: 'Density' }, overlay: P.bool('Transparent background', true) },
    render(c) { clear(c.out); if (!c.p.overlay) fillAll(c.out, c.n, 0, 0, 0, 1); const slot = Math.floor(c.beat * c.p.rate); const nseg = 6; for (let s = 0; s < nseg; s++) { if (hash(slot * 101 + s * 7) > c.p.density) continue; const a = Math.floor(hash(slot * 53 + s) * c.n), len = 1 + Math.floor(hash(slot * 59 + s) * c.n / 4); const col = palIdx(c.pal, Math.floor(hash(slot * 61 + s) * c.pal.length)); for (let i = a; i < Math.min(c.n, a + len); i++) set(c.out, i, col[0], col[1], col[2], 1); } } });

  def({ id: 'halves', name: 'Halves', cat: 'fast', desc: 'Left and right halves alternate in two palette colors, swapping on every real drum hit (kick or snare), so it lands with the drummer however the beat goes; on the beat when the drums are not known.', swatch: ['#ff0000', '#0040ff'],
    params: { space: P.space('across', 'across', 'sides'), angle: P.angle(), follow: { type: 'select', def: 'drums', options: { drums: 'the drums (kick and snare)', beat: 'the beat' }, label: 'Swaps on' },
      every: P.speed(1, 'Swap every (beats, or hits)'), dim: { type: 'range', def: 0.0, min: 0, max: 1, step: 0.01, label: 'Off-side level' } },
    render(c) {
      // on the drums: the hits so far (kicks and snares both count), so each one swaps the sides; on a
      // song with the kick on every beat this is the beat, on one without it lands where the drums do
      const m = c.music || {}, age = h => (h && h.index >= 0 ? h.age : 99);   // (a stretch with no drums for 1.5 s: the beat)
      const drums = c.p.follow !== 'beat' && m.hasDrums && Math.min(age(m.kick), age(m.snare)) < 1.5;
      // (each side its own color of the pair, the pair moving on every other swap: moving it on with every swap,
      // with two colors only the first ever lit)
      const k = drums ? Math.floor(hitMoments(c, ['kick', 'snare']) / Math.max(1, Math.round(c.p.every))) : Math.floor(c.beat / c.p.every), pr = Math.floor(k / 2), a = palIdx(c.pal, 2 * pr), b = palIdx(c.pal, 2 * pr + 1); for (let i = 0; i < c.n; i++) { const left = i < c.n / 2; const on = left === (k % 2 === 0); const col = left ? a : b; const v = on ? 1 : c.p.dim; set(c.out, i, col[0] * v, col[1] * v, col[2] * v); } } });

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
      // the snare gone quiet for a beat and a half (or none found in this build): the beat keeps it going (I'm
      // Good's dark build sat 2.4 s in total black; hYperSAD's last build, no snare in it, only its dim wash)
      if (c.p.follow !== 'beat' && c.p.follow !== 'build' && (!h || h.index < 0 || h.age > 1.5 * c.beatSec)) h ={ index: 50021 + Math.floor(c.beat), age: frac(c.beat) * c.beatSec, strength: 0.8 };
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
        // its brightness counted once: over the dark, the color that bright; over the rest, the full color laid on
        // that much (as both, a build's first hits came out at a fifth, not half)
        for (let i = i0; i < i1 && i < c.n; i++) if (dark) set(c.out, i, col[0] * v, col[1] * v, col[2] * v, 1); else set(c.out, i, col[0], col[1], col[2], v);
      }
    } });

  def({ id: 'roomsplit', name: 'Room split', cat: 'room', desc: 'One side of the room in one color, the other side in the other, swapping on the beat. With one light, its two halves.', swatch: ['#8000ff', '#00ff00'],
    params: { space: P.space('across', 'across', 'sides'), angle: P.angle(), every: P.speed(1, 'Swap every (beats)'), pump: { type: 'range', def: 0.35, min: 0, max: 1, step: 0.01, label: 'Beat pump' } },
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
    params: { space: P.space('around', 'across', 'around', 'sides'), angle: P.angle(), spin: P.spin(), every: P.speed(1, 'Next light every (beats)'), floor: { type: 'range', def: 0.0, min: 0, max: 1, step: 0.01, label: 'Floor' } },
    render(c) {
      const t = c.beat / c.p.every, step = Math.floor(t), env = Math.exp(-(t - step) * 2.5), col = palIdx(c.pal, step);
      for (let i = 0; i < c.n; i++) {
        const lit = inRoom(c) ? (step % c.room.n) === c.room.i : Math.floor(i / c.n * 4) === step % 4;
        const v = lit ? Math.max(c.p.floor, env) : c.p.floor;
        set(c.out, i, col[0] * v, col[1] * v, col[2] * v, lit ? 1 : (c.p.floor > 0 ? 1 : 0));
      }
    } });
  def({ id: 'roomsweep', name: 'Room sweep', cat: 'room', desc: 'A bright band crosses the room: the left light pole, then along the overhead strips, then the right pole (the small lamps split into left and right by the light order, the long strips between). With one light, it crosses the strip.', swatch: ['#ffffff', '#000000'],
    params: { space: P.space('across', 'across', 'around', 'sides'), angle: P.angle(), spin: P.spin(), every: P.speed(2, 'One crossing every (beats)'), width: { type: 'range', def: 0.6, min: 0.1, max: 2, step: 0.05, label: 'Band width (lights)' }, color: P.color(), ease: P.ease() },
    render(c) {
      clear(c.out);
      const u = ease(c, frac(c.clipBeat / c.p.every));                   // how far across the room this crossing is
      const { slot, m, small } = roomLine(c);
      const w = c.p.width, pos = u * (m + w) - w / 2, col = colorFor(c, u, 0);
      for (let i = 0; i < c.n; i++) {
        const x = small ? slot + 0.5 : slot + (c.n > 1 ? i / (c.n - 1) : 0.5);   // a pole lights whole; a strip, along it
        const v = Math.exp(-(((x - pos) / (w * 0.5)) ** 2));
        if (v > 0.01) set(c.out, i, col[0] * v, col[1] * v, col[2] * v, v);
      }
    } });
  def({ id: 'roomscan', name: 'Room scanner', cat: 'room', desc: 'One slow bar scans back and forth across both rooms: the left light pole, along the overhead strips, the right pole, and back. With one light, across its strip.', swatch: ['#ff0000', '#300000'],
    params: { space: P.space('across', 'across', 'around', 'sides'), angle: P.angle(), spin: P.spin(), speed: P.speed(16, 'One crossing (beats)'), width: { type: 'range', def: 0.6, min: 0.1, max: 2, step: 0.05, label: 'Bar width (lights)' }, color: P.color(),
      floor: { type: 'range', def: 0, min: 0, max: 1, step: 0.01, label: 'Floor' }, ease: P.ease('smooth') },
    render(c) {
      const { slot, m, small } = roomLine(c);
      const u = c.beat / c.p.speed, e = ease(c, frac(u)), pos = (Math.floor(u) % 2 === 0 ? e : 1 - e) * m, w = c.p.width, col = colorFor(c, pos / m, Math.floor(u / 2));
      for (let i = 0; i < c.n; i++) {
        const x = small ? slot + 0.5 : slot + (c.n > 1 ? i / (c.n - 1) : 0.5);   // a pole lights whole; a strip, along it
        const v = c.p.floor + Math.exp(-(((x - pos) / (w * 0.5)) ** 2));
        set(c.out, i, col[0] * v, col[1] * v, col[2] * v, Math.min(1, v));
      }
    } });
  /** This light's place on the line across the rooms: the left lamps (a light pole), the long strips, the
   *  right lamps; m places in all, a strip spanning [slot, slot + 1]. One light alone: the whole line. */
  function roomLine(c) {
    if (!(inRoom(c) && c.room.kinds)) return { slot: 0, m: 1, small: false };
    const k = c.room.kinds, shorts = k.map((x, i) => (x === 'short' ? i : -1)).filter(i => i >= 0);
    const longs = k.map((x, i) => (x !== 'short' ? i : -1)).filter(i => i >= 0), half = Math.ceil(shorts.length / 2);
    const line = [...shorts.slice(0, half), ...longs, ...shorts.slice(half)];
    return { slot: Math.max(0, line.indexOf(c.room.i)), m: line.length, small: k[c.room.i] === 'short' };
  }
  def({ id: 'roomwave', name: 'Room wave', cat: 'room', desc: 'A slow swell rolls around the room from lamp to lamp, each half of the room in its own color. With one light, along its strip.', swatch: ['#8000ff', '#00ff00'],
    params: { space: P.space('around', 'across', 'around', 'sides'), angle: P.angle(), spin: P.spin(), speed: P.speed(8, 'Once round (beats)'), floor: { type: 'range', def: 0.1, min: 0, max: 1, step: 0.01, label: 'Floor' } },
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
  def({ id: 'dropblast', name: 'Drop blast', cat: 'accent', desc: 'White blast then a shockwave racing out from its starting point (the middle unless set). Place it right on the drop.', swatch: ['#ffffff', '#ff2fa0'],
    params: { space: P.space('out', 'out', 'across', 'sides'), angle: P.angle(), flash: { type: 'range', def: 0.15, min: 0.02, max: 0.5, step: 0.01, label: 'Flash (beats)' }, ring: P.size(0.12, 'Ring width'), color: P.color() },
    render(c) { clear(c.out); const b = c.clipBeat; if (b < c.p.flash) { fillAll(c.out, c.n, 1, 1, 1, 1); return; } const age = (b - c.p.flash) / Math.max(0.5, c.clipBeats - c.p.flash); if (age > 1) return; const col = colorFor(c, age, 0); for (let i = 0; i < c.n; i++) { const x = away(c, i / Math.max(1, c.n - 1)); const v = Math.exp(-1 * ((x - age * 1.1) / c.p.ring) ** 2) * (1 - age); if (v > 0.01) set(c.out, i, col[0] * v, col[1] * v, col[2] * v, v); } } });

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

  def({ id: 'colorslam', name: 'Slam', cat: 'accent', desc: 'Whole strip slams to a palette color and fades back out (after holding it, if set).', swatch: ['#ff0000', '#000000'],
    params: { color: P.color(), hold: { type: 'range', def: 0, min: 0, max: 0.9, step: 0.05, label: 'Hold (share of the clip)' } },
    render(c) { const h = c.p.hold || 0, v = c.clipT < h ? 1 : Math.exp(-(c.clipT - h) / (1 - h) * 4); const col = colorFor(c, 0.5, 0); fillAll(c.out, c.n, col[0], col[1], col[2], v); } });

  def({ id: 'glitch', name: 'Glitch', cat: 'accent', desc: 'A burst of digital glitch over the clip: blocks of the strip jump on and off in one solid color, now and then white, black between, covering what is under it.', swatch: ['#ff0040', '#000000'],
    params: { rate: { type: 'range', def: 0.125, min: 0.03, max: 0.5, step: 0.01, label: 'Jumps every (beats)' }, density: { type: 'range', def: 0.45, min: 0.1, max: 0.9, step: 0.01, label: 'Lit' },
      blocks: { type: 'range', def: 6, min: 2, max: 16, step: 1, label: 'Blocks on a strip' }, white: P.bool('Flecks of white', true) },
    render(c) {
      const k = Math.floor(c.clipBeat / c.p.rate), col = palIdx(c.pal, 0), room = c.room ? c.room.i * 991 : 0, nb = c.n < 10 ? 1 : Math.round(c.p.blocks);
      for (let i = 0; i < c.n; i++) {
        const b = Math.floor(i / c.n * nb), on = hash(k * 131 + b * 17 + room) < c.p.density, w = c.p.white && hash(k * 7 + b * 3 + room + 5) < 0.18;
        if (on) set(c.out, i, w ? 1 : col[0], w ? 1 : col[1], w ? 1 : col[2], 1); else set(c.out, i, 0, 0, 0, 1);
      } } });
  def({ id: 'drumgate', name: 'Drum gate', cat: 'fast', desc: 'Chops the whole show with the drums: black between the hits, the lights there for a moment on each kick and snare. Put it on top of a heavy drop (dubstep).', swatch: ['#000000', '#ffffff'],
    params: { hold: { type: 'range', def: 0.12, min: 0.04, max: 0.4, step: 0.01, label: 'Open after a hit (s)' } },
    render(c) {
      const m = c.music || {};
      if (!m.hasDrums) { clear(c.out); return; }
      let age = 99; for (const h of [m.kick, m.snare, m.roll]) if (h && h.index >= 0) age = Math.min(age, h.age);
      const open = age < 5 ? Math.exp(-age / c.p.hold) : 0;
      fillAll(c.out, c.n, 0, 0, 0, 1 - open);
    } });
  def({ id: 'hitswap', name: 'Hit swap', cat: 'fast', desc: 'The whole light in one pure, solid color that flips to the next palette color on every drum hit (kick or snare), a little brighter on the hit itself; on the beat when the drums are not known. Every light the same color: the room flips with the drums. Made for a hardcore drop.', swatch: ['#ff0000', '#0000ff'],
    params: { space: P.space('strip', 'sides'), follow: { type: 'select', def: 'hits', options: { hits: 'the drums and the bass', drums: 'the drums (kick and snare)', beat: 'the beat' }, label: 'Flips on' },
      punch: { type: 'range', def: 0.15, min: 0, max: 0.5, step: 0.01, label: 'Brighter on the hit' } },
    render(c) {
      // (with the bass too: a half-time dubstep drop has only two or three drum hits a second, and its bass
      // is what drives it: the room flips with the wobbles)
      const m = c.music || {}, age = h => (h && h.index >= 0 ? h.age : 99), withBass = c.p.follow === 'hits';
      const last = Math.min(age(m.kick), age(m.snare), withBass ? age(m.basshit) : 99);
      const drums = c.p.follow !== 'beat' && m.hasDrums && last < 1.5;
      const k = drums ? hitMoments(c, withBass ? ['kick', 'snare', 'basshit'] : ['kick', 'snare']) : Math.floor(c.beat);
      const col = palIdx(c.pal, k), v = (1 - c.p.punch) + c.p.punch * (drums ? Math.exp(-last / 0.12) : 0);
      fillAll(c.out, c.n, col[0] * v, col[1] * v, col[2] * v, 1);
    } });
  def({ id: 'fillflood', name: 'Fill flood', cat: 'accent', desc: 'For a drum fill: the whole room in one solid color, flashing white on every hit of the fill, covering what is under it.', swatch: ['#ff0000', '#ffffff'],
    params: { flash: { type: 'range', def: 0.06, min: 0.02, max: 0.2, step: 0.01, label: 'Flash (s)' } },
    render(c) {
      const m = c.music || {}, col = palIdx(c.pal, 0);
      let age = 99; for (const h of [m.kick, m.snare, m.roll]) if (h && h.index >= 0) age = Math.min(age, h.age);
      const w = age < c.p.flash ? 1 - age / c.p.flash : 0;
      fillAll(c.out, c.n, col[0] + (1 - col[0]) * w, col[1] + (1 - col[1]) * w, col[2] + (1 - col[2]) * w, 1);
    } });
  def({ id: 'sweepout', name: 'Sweep out', cat: 'accent', desc: 'A bright bar sweeps out from its starting point (the middle unless set) to the ends once, over the clip.', swatch: ['#ffffff', '#00c8ff'],
    params: { space: P.space('out', 'out', 'across', 'sides'), angle: P.angle(), size: P.size(0.15, 'Width'), color: P.color(), ease: P.ease() },
    render(c) { clear(c.out); const at = ease(c, c.clipT), col = colorFor(c, c.clipT, 0); for (let i = 0; i < c.n; i++) { const x = away(c, i / Math.max(1, c.n - 1)); const v = Math.exp(-1 * ((x - at) / c.p.size) ** 2); if (v > 0.01) set(c.out, i, col[0] * v, col[1] * v, col[2] * v, v); } } });

  const byId = Object.fromEntries(E.map(e => [e.id, e]));
  const CATS = { signature: 'Follows this song', room: 'Around the room', slow: 'Slow & smooth', medium: 'Groove', fast: 'Fast & intense', accent: 'Accents (one-shot)' };

  function defaults(effectId) {
    const e = byId[effectId]; const p = {};
    for (const [k, d] of Object.entries(e.params)) p[k] = d.def;
    return p;
  }

  // ---------- compositing ----------------------------------------------------------
  const BLENDS = { normal: 'Normal', add: 'Add', screen: 'Screen', multiply: 'Multiply', max: 'Lighten', cut: 'Cut (black where it lights)' };
  function composite(dst, src, n, opacity, blend) {
    for (let i = 0; i < n; i++) {
      const j = i * 4; const a = clamp(src[j + 3]) * opacity; if (a <= 0) continue;
      if (blend === 'cut') {                                  // darkness where the layer lights: a black swipe
        // (the layer's own light, not its light times its alpha: a band's fade counted twice left its middle
        // only dim; and a little more, so the core of a swipe is fully black)
        const m = clamp(1.6 * Math.max(src[j], src[j + 1], src[j + 2])) * opacity;
        for (let ch = 0; ch < 3; ch++) dst[j + ch] *= 1 - m;
        continue;
      }
      // Add, as light does, but true to the colors: a layer in a color like the one under it adds (brighter),
      // one in a color far from it (orange hits on a teal bed) takes the place where it is bright, mostly,
      // rather than summing to a pale near-white (over a whole drop, the room washed out to lavender).
      let keep = 1;
      if (blend === 'add') {
        const d0 = dst[j], d1 = dst[j + 1], d2 = dst[j + 2], s0 = clamp(src[j]), s1 = clamp(src[j + 1]), s2 = clamp(src[j + 2]);
        const dd = Math.hypot(d0, d1, d2), ss = Math.hypot(s0, s1, s2);
        if (dd > 0.02 && ss > 0.02) keep = 1 - Math.max(s0, s1, s2) * (1 - (d0 * s0 + d1 * s1 + d2 * s2) / (dd * ss));
      }
      for (let ch = 0; ch < 3; ch++) {
        const d = dst[j + ch], s = clamp(src[j + ch]); let r;
        switch (blend) {
          case 'add': r = d * keep + s; break;
          case 'screen': r = 1 - (1 - d) * (1 - s); break;
          case 'multiply': r = d * s; break;
          case 'max': r = Math.max(d, s); break;
          default: r = s;
        }
        dst[j + ch] = clamp(d + (r - d) * a);
      }
    }
  }

  return { list: E, byId, CATS, PALETTES, BEAT_OPTS, BLENDS, defaults, composite, hex2rgb, hueOf, hueHex, hsv, hash, solid, solidPalette };
})();
