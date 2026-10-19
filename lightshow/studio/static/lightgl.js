/* Light Show Studio: lights drawn on the GPU (WebGL), glow and all.
 *
 * Every light (a string light's bulb, a light pole's segment, a strip segment in the preview) is one
 * quad; the fragment shader draws its body (a circle, or a rectangle with rounded corners) and its glow
 * as a falloff from the body's edge, so a full-screen 4K view of a hundred glowing lights costs the GPU
 * next to nothing and the main thread only fills one small buffer a frame. (Canvas 2D drew the glow
 * with a shadow blur per light and lagged at 4K.)
 *
 *   const gl = LightGL(canvas);          // null where WebGL is not available
 *   gl.draw(lights, cssWidth, cssHeight) // lights: [{ x, y, w, h, shape: 'dot'|'rect', r, g, b,
 *                                        //   radius?, glow? }] in CSS pixels, colors 0..255
 */
const LightGL = (() => {
  const VS = `
    attribute vec2 a_pos; attribute vec2 a_local; attribute vec2 a_half; attribute vec3 a_color; attribute vec2 a_shape;
    uniform vec2 u_res;
    varying vec2 v_local; varying vec2 v_half; varying vec3 v_color; varying vec2 v_shape;
    void main() {
      v_local = a_local; v_half = a_half; v_color = a_color; v_shape = a_shape;
      vec2 c = a_pos / u_res * 2.0 - 1.0;
      gl_Position = vec4(c.x, -c.y, 0.0, 1.0);
    }`;
  // v_shape.x: corner radius in px (a dot: radius = half size); v_shape.y: glow reach in px
  const FS = `
    precision mediump float;
    varying vec2 v_local; varying vec2 v_half; varying vec3 v_color; varying vec2 v_shape;
    void main() {
      float rad = min(v_shape.x, min(v_half.x, v_half.y));
      vec2 q = abs(v_local) - v_half + rad;
      float d = length(max(q, 0.0)) + min(max(q.x, q.y), 0.0) - rad;   // distance outside the body (px)
      float lum = max(v_color.r, max(v_color.g, v_color.b));
      float body = clamp(0.5 - d, 0.0, 1.0);                             // anti-aliased edge
      vec3 col = v_color;                                                // black where it is dark, as the real lights (the user)
      float glow = lum * 0.55 * exp(-max(d, 0.0) / max(v_shape.y, 0.001)) * (1.0 - body);
      float a = body + glow;
      if (a < 0.003) discard;
      gl_FragColor = vec4(col * body + v_color * glow, a);               // premultiplied
    }`;

  function compile(gl, type, src) {
    const s = gl.createShader(type);
    gl.shaderSource(s, src); gl.compileShader(s);
    if (!gl.getShaderParameter(s, gl.COMPILE_STATUS)) throw new Error(gl.getShaderInfoLog(s));
    return s;
  }

  return function LightGL(canvas) {
    const gl = canvas.getContext('webgl', { premultipliedAlpha: true, alpha: true, antialias: false, preserveDrawingBuffer: false });
    if (!gl) return null;
    const prog = gl.createProgram();
    gl.attachShader(prog, compile(gl, gl.VERTEX_SHADER, VS));
    gl.attachShader(prog, compile(gl, gl.FRAGMENT_SHADER, FS));
    gl.linkProgram(prog);
    if (!gl.getProgramParameter(prog, gl.LINK_STATUS)) throw new Error(gl.getProgramInfoLog(prog));
    gl.useProgram(prog);
    const loc = n => gl.getAttribLocation(prog, n), uRes = gl.getUniformLocation(prog, 'u_res');
    const attrs = [['a_pos', 2], ['a_local', 2], ['a_half', 2], ['a_color', 3], ['a_shape', 2]];
    const STRIDE = attrs.reduce((n, a) => n + a[1], 0);
    const buf = gl.createBuffer();
    gl.bindBuffer(gl.ARRAY_BUFFER, buf);
    let off = 0;
    for (const [name, n] of attrs) {
      const l = loc(name);
      gl.enableVertexAttribArray(l);
      gl.vertexAttribPointer(l, n, gl.FLOAT, false, STRIDE * 4, off * 4);
      off += n;
    }
    gl.enable(gl.BLEND);
    gl.blendFunc(gl.ONE, gl.ONE_MINUS_SRC_ALPHA);
    let data = new Float32Array(0);
    const CORNERS = [[-1, -1], [1, -1], [-1, 1], [-1, 1], [1, -1], [1, 1]];

    function draw(lights, cssW, cssH) {
      const dpr = window.devicePixelRatio || 1;
      const W = Math.max(1, Math.floor(cssW * dpr)), H = Math.max(1, Math.floor(cssH * dpr));
      if (canvas.width !== W || canvas.height !== H) { canvas.width = W; canvas.height = H; }
      gl.viewport(0, 0, W, H);
      gl.clearColor(0, 0, 0, 0); gl.clear(gl.COLOR_BUFFER_BIT);
      const need = lights.length * 6 * STRIDE;
      if (data.length < need) data = new Float32Array(need * 2);
      let k = 0;
      for (const L of lights) {
        const hx = L.w / 2 * dpr, hy = L.h / 2 * dpr, cx = L.x * dpr, cy = L.y * dpr;
        const glow = (L.glow ?? Math.max(4, Math.min(L.w, L.h) * 0.9)) * dpr;
        const rad = (L.shape === 'dot' ? Math.min(L.w, L.h) / 2 : (L.radius ?? 2)) * dpr;
        const ex = hx + glow * 3, ey = hy + glow * 3;               // the quad reaches as far as the glow
        for (const [sx, sy] of CORNERS) {
          data[k++] = cx + sx * ex; data[k++] = cy + sy * ey;
          data[k++] = sx * ex; data[k++] = sy * ey;
          data[k++] = hx; data[k++] = hy;
          data[k++] = L.r / 255; data[k++] = L.g / 255; data[k++] = L.b / 255;
          data[k++] = rad; data[k++] = glow;
        }
      }
      gl.useProgram(prog);
      gl.uniform2f(uRes, W, H);
      gl.bindBuffer(gl.ARRAY_BUFFER, buf);
      gl.bufferData(gl.ARRAY_BUFFER, data.subarray(0, k), gl.DYNAMIC_DRAW);
      gl.drawArrays(gl.TRIANGLES, 0, lights.length * 6);
    }
    return { draw };
  };
})();
