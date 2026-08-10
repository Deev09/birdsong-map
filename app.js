/*
 * Birdsong Map — map-first, sound-first.
 *
 * The map is the instrument. Sweeping across it plays the county's signature
 * bird; opening a county fans its birds out as a halo you can sweep through.
 * Every clip is a self-hosted ~40 KB MP3 decoded into an AudioBuffer, so a
 * sound starts on the same frame as the hover — streaming from Commons could
 * never do that.
 */

const MONTHS = ['', 'Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec'];
const FULL = ['', 'January', 'February', 'March', 'April', 'May', 'June', 'July',
  'August', 'September', 'October', 'November', 'December'];

// Keep in sync with the --shape custom properties in style.css.
const SHAPE = {
  whistle: '#ffd66b', trill: '#7ddc8a', chatter: '#ff9a52', hoot: '#6ea8ff',
  honk: '#4fd3c4', buzz: '#c08bff', screech: '#ff6b7a',
};
const SHAPE_ORDER = ['whistle', 'trill', 'chatter', 'hoot', 'honk', 'buzz', 'screech'];

const SPEC_T = 56, SPEC_F = 28;
const RING_N = 12;

let SPECIES = {}, REGIONS = {}, SPECS = {}, GEO = null;
let proj = null, centroids = {};

const state = {
  month: new Date().getMonth() + 1,
  gid: null,
  shape: null,
  hoverSound: true,
  yearTimer: null,
  lastTapped: null,   // touch: first tap hears, second tap opens details
};

/**
 * Touch devices never fire pointerenter, so a hover-driven map is silent on a
 * phone: tapping a county opened its halo and played nothing at all. Checked
 * live rather than assumed — a real tap emits pointerdown/pointerup/click and
 * nothing else. Query this per interaction rather than caching it, because
 * hybrid laptops gain and lose a mouse at runtime.
 */
const canHover = () => window.matchMedia('(hover: hover)').matches;

// ------------------------------------------------------------------ audio

// A decoded 6 s / 48 kHz mono clip is float32 PCM: 1.1 MiB resident, a 27x
// expansion over the 42 KB file. Caching every decode would reach ~224 MiB
// across the 204 clips and take the tab down for exactly the users who explore
// most. So keep every *compressed* buffer (8 MB total, cheap) and hold only a
// small window of decoded ones.
const MAX_DECODED = 24;   // ~26 MiB

const AudioKit = {
  ctx: null,
  raw: new Map(),        // url -> ArrayBuffer (compressed, keep all)
  buffers: new Map(),    // url -> AudioBuffer (decoded, LRU-capped)
  inflight: new Map(),
  src: null,
  current: null,
  seq: 0,
  ready: false,

  init() {
    if (!this.ctx) {
      this.ctx = new (window.AudioContext || window.webkitAudioContext)();
      this.gain = this.ctx.createGain();
      this.gain.gain.value = 0.9;
      this.gain.connect(this.ctx.destination);
      this.ctx.onstatechange = () => {
        this.ready = this.ctx.state === 'running';
        document.body.classList.toggle('muted', !this.ready);
      };
    }
    if (this.ctx.state === 'suspended') this.ctx.resume();
    this.ready = this.ctx.state === 'running';
    return this.ready;
  },

  fetchRaw(url) {
    if (this.raw.has(url)) return Promise.resolve(this.raw.get(url));
    if (this.inflight.has(url)) return this.inflight.get(url);
    const p = fetch(url)
      .then(r => (r.ok ? r.arrayBuffer() : null))
      .then(b => { if (b) this.raw.set(url, b); this.inflight.delete(url); return b; })
      .catch(() => { this.inflight.delete(url); return null; });
    this.inflight.set(url, p);
    return p;
  },

  async decoded(url) {
    const hit = this.buffers.get(url);
    if (hit) {                       // refresh LRU position
      this.buffers.delete(url);
      this.buffers.set(url, hit);
      return hit;
    }
    const raw = await this.fetchRaw(url);
    if (!raw || !this.ctx) return null;
    let buf;
    try {
      // decodeAudioData detaches its input, so hand it a copy and keep ours.
      buf = await this.ctx.decodeAudioData(raw.slice(0));
    } catch (e) {
      return null;
    }
    this.buffers.set(url, buf);
    while (this.buffers.size > MAX_DECODED) {
      this.buffers.delete(this.buffers.keys().next().value);
    }
    return buf;
  },

  stop() {
    if (this.src) {
      try { this.src.stop(); } catch (e) { /* already ended */ }
      this.src = null;
    }
    if (this.current) {
      markSounding(this.current, false);
      this.current = null;
    }
  },

  async play(key) {
    const sp = SPECIES[key];
    if (!sp || !sp.clip) return;
    if (!this.init()) return;        // suspended: stay silent AND stay honest

    // A slow fetch for a county the pointer already left must not stop the
    // cached one playing now.
    const tok = ++this.seq;
    const buf = await this.decoded(sp.clip);
    if (!buf || tok !== this.seq || !this.ready) return;

    this.stop();
    const s = this.ctx.createBufferSource();
    s.buffer = buf;
    s.connect(this.gain);
    s.start();
    this.src = s;
    this.current = key;
    markSounding(key, true);   // only ever after a real start()
    s.onended = () => {
      if (this.src === s) { this.src = null; markSounding(key, false); this.current = null; }
    };
  },

  // Warm what the pointer is about to touch — compressed only, so prefetching a
  // whole county costs ~500 KB of memory rather than ~13 MiB of decoded PCM.
  prefetch(keys) {
    keys.slice(0, 14).forEach(k => {
      const sp = SPECIES[k];
      if (sp && sp.clip) this.fetchRaw(sp.clip);
    });
  },
};

/**
 * What a tap on a bird means.
 *
 * With a mouse, hover already played it, so a click means "tell me more".
 * With a finger there was no hover, so the first tap has to be the sound —
 * opening a full-screen sheet over the map on every tap would bury the one
 * thing people came for. Tap again on the same bird to open the details.
 */
function tapBird(key, el, sp) {
  if (canHover()) { openSheet(key); return; }
  if (state.lastTapped === key) { state.lastTapped = null; openSheet(key); return; }
  state.lastTapped = key;
  AudioKit.play(key);
  if (el) showPeek(el, key, sp.name, (sp.sound && sp.sound.tags.join(' · ')) || '');
}

function markSounding(key, on) {
  for (const el of document.querySelectorAll(`[data-key="${key}"]`)) {
    el.classList.toggle('sounding', on);
  }
}

// ------------------------------------------------------------------ geometry

function eachRing(features, fn) {
  for (const f of features) {
    const g = f.geometry;
    const polys = g.type === 'Polygon' ? [g.coordinates] : g.coordinates;
    for (const poly of polys) for (const ring of poly) fn(ring, f);
  }
}

function makeProjection(features, width) {
  let minX = 180, maxX = -180, minY = 90, maxY = -90;
  eachRing(features, ring => {
    for (const [x, y] of ring) {
      if (x < minX) minX = x; if (x > maxX) maxX = x;
      if (y < minY) minY = y; if (y > maxY) maxY = y;
    }
  });
  const k = Math.cos(((minY + maxY) / 2) * Math.PI / 180);
  const scale = width / ((maxX - minX) * k);
  return {
    width,
    height: (maxY - minY) * scale,
    to: ([x, y]) => [(x - minX) * k * scale, (maxY - y) * scale],
  };
}

function pathFor(feature) {
  const g = feature.geometry;
  const polys = g.type === 'Polygon' ? [g.coordinates] : g.coordinates;
  let d = '';
  for (const poly of polys) {
    for (const ring of poly) {
      d += ring.map((pt, i) => {
        const [x, y] = proj.to(pt);
        return (i ? 'L' : 'M') + x.toFixed(1) + ' ' + y.toFixed(1);
      }).join('') + 'Z';
    }
  }
  return d;
}

function centroidOf(feature) {
  let sx = 0, sy = 0, n = 0;
  eachRing([feature], ring => {
    for (const pt of ring) { const [x, y] = proj.to(pt); sx += x; sy += y; n++; }
  });
  return [sx / n, sy / n];
}

// ------------------------------------------------------------------ spectrogram

const rgbCache = {};
function rgb(hex) {
  if (!rgbCache[hex]) {
    rgbCache[hex] = [1, 3, 5].map(i => parseInt(hex.slice(i, i + 2), 16));
  }
  return rgbCache[hex];
}

/**
 * Paint the stored 28x56 log-frequency spectrogram, tinted with the bird's
 * sound-shape colour. The point is that the *shape* is recognisable: a trill is
 * evenly spaced vertical ticks, a whistle a smooth horizontal arc, a hoot a
 * low fat blob. That is the bridge between "what I heard" and "which bird".
 */
function drawSpec(canvas, key) {
  const g = canvas.getContext('2d');
  const w = canvas.width, h = canvas.height;
  g.clearRect(0, 0, w, h);
  const str = SPECS[key];
  if (!str) return;

  const sp = SPECIES[key];
  const tag = (sp.sound && sp.sound.tags[0]) || 'whistle';
  const [r, gg, b] = rgb(SHAPE[tag] || '#ffd66b');

  const cw = w / SPEC_T, ch = h / SPEC_F;
  for (let f = 0; f < SPEC_F; f++) {
    for (let t = 0; t < SPEC_T; t++) {
      const v = +str[f * SPEC_T + t];
      if (!v) continue;
      const a = v / 9;
      // ease toward white at the top end so peaks read as "hot"
      const m = Math.pow(a, 1.6);
      g.fillStyle = `rgba(${Math.round(r + (255 - r) * m * .7)},`
        + `${Math.round(gg + (255 - gg) * m * .7)},`
        + `${Math.round(b + (255 - b) * m * .7)},${(0.12 + a * 0.88).toFixed(3)})`;
      g.fillRect(t * cw, (SPEC_F - 1 - f) * ch, Math.ceil(cw), Math.ceil(ch));
    }
  }
}

// ------------------------------------------------------------------ data helpers

function listFor(gid, month) {
  const r = REGIONS[gid];
  if (!r) return [];
  return (r.months[String(month)] || []).filter(([k]) => SPECIES[k]);
}

function matchesShape(key) {
  if (!state.shape) return true;
  const s = SPECIES[key].sound;
  return !!(s && s.tags.includes(state.shape));
}

function audible(list) {
  return list.filter(([k]) => SPECIES[k].clip);
}

/** Birds shown for the current county+month, after any sound filter. */
function currentList(gid) {
  const l = audible(listFor(gid, state.month));
  const f = l.filter(([k]) => matchesShape(k));
  return state.shape ? f : l;
}

/** The county's sound mix as a proportion vector over SHAPE_ORDER. */
function shapeVector(gid) {
  const list = audible(listFor(gid, state.month)).slice(0, 20);
  const w = {};
  let total = 0;
  for (const [k, n] of list) {
    const s = SPECIES[k].sound;
    if (!s) continue;
    for (const t of s.tags) { w[t] = (w[t] || 0) + n; total += n; }
  }
  if (!total) return null;
  return SHAPE_ORDER.map(t => (w[t] || 0) / total);
}

/**
 * Which sound is over-represented here, compared with the whole state.
 *
 * Two earlier attempts failed for opposite reasons. Colouring by the single
 * dominant tag gave two colours, because the rhythm tag sorts first and almost
 * every county is trill- or chatter-led. Blending the full mix gave *one*
 * colour, because neighbouring counties share most of their species and all
 * average out to the same brown.
 *
 * Counties genuinely do not differ much in absolute terms -- so show the
 * difference instead. Subtracting the state mean turns a near-uniform field
 * into a real signal: "unusually trill-heavy for Iowa in August".
 */
function anomalyMap() {
  const vecs = {};
  const mean = new Array(SHAPE_ORDER.length).fill(0);
  let n = 0;
  for (const gid in REGIONS) {
    const v = shapeVector(gid);
    if (!v) continue;
    vecs[gid] = v;
    for (let i = 0; i < v.length; i++) mean[i] += v[i];
    n++;
  }
  if (!n) return {};
  for (let i = 0; i < mean.length; i++) mean[i] /= n;

  const out = {};
  let peak = 1e-6;
  for (const gid in vecs) {
    const v = vecs[gid];
    let bi = 0, bv = -Infinity;
    for (let i = 0; i < v.length; i++) {
      const d = v[i] - mean[i];
      if (d > bv) { bv = d; bi = i; }
    }
    out[gid] = { tag: SHAPE_ORDER[bi], dev: bv };
    if (bv > peak) peak = bv;
  }
  for (const gid in out) out[gid].norm = Math.min(1, out[gid].dev / peak);
  return out;
}

/** With a filter on, how much of the county's activity is that sound shape. */
function shapeShare(gid, shape) {
  const list = audible(listFor(gid, state.month)).slice(0, 20);
  let hit = 0, total = 0;
  for (const [k, n] of list) {
    const s = SPECIES[k].sound;
    if (!s) continue;
    total += n;
    if (s.tags.includes(shape)) hit += n;
  }
  return total ? hit / total : 0;
}

// ------------------------------------------------------------------ map

function drawMap() {
  const svg = document.getElementById('map');
  svg.innerHTML = '';
  proj = makeProjection(GEO.features, 1000);
  svg.setAttribute('viewBox', `0 0 ${proj.width} ${Math.round(proj.height)}`);

  const ns = 'http://www.w3.org/2000/svg';
  for (const f of GEO.features) {
    const gid = f.properties.gid;
    centroids[gid] = centroidOf(f);

    const p = document.createElementNS(ns, 'path');
    p.setAttribute('d', pathFor(f));
    p.dataset.gid = gid;
    p.setAttribute('tabindex', '0');
    p.setAttribute('role', 'button');
    p.setAttribute('aria-label', f.properties.name + ' County');

    const t = document.createElementNS(ns, 'title');
    t.textContent = f.properties.name + ' County';
    p.appendChild(t);

    p.addEventListener('pointerenter', () => hoverCounty(gid, p));
    p.addEventListener('click', () => {
      select(gid);
      // On touch there was no hover to play the county's bird, so the tap has
      // to do both jobs — otherwise the map is mute.
      if (!canHover()) hoverCounty(gid, p);
    });
    p.addEventListener('focus', () => hoverCounty(gid, p));
    p.addEventListener('keydown', e => {
      if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); select(gid); }
    });
    svg.appendChild(p);
  }
  paint();
}

/** Recolour every county for the current month + filter. */
function paint() {
  const anom = state.shape ? null : anomalyMap();
  for (const p of document.querySelectorAll('#map path')) {
    const gid = p.dataset.gid;
    let fill = '#10151c';
    if (state.shape) {
      const a = Math.min(1, shapeShare(gid, state.shape) * 2.6);
      fill = `color-mix(in srgb, ${SHAPE[state.shape]} ${(a * 88).toFixed(0)}%, #10151c)`;
    } else if (anom[gid]) {
      const { tag, norm } = anom[gid];
      const pct = (22 + norm * 62).toFixed(0);   // keep weak counties visible
      fill = `color-mix(in srgb, ${SHAPE[tag]} ${pct}%, #10151c)`;
    }
    p.style.setProperty('--f', fill);
    p.classList.toggle('sel', gid === state.gid);
  }
  drawLegend();
}

function drawLegend() {
  const el = document.getElementById('legend');
  el.innerHTML = state.shape
    ? `<b style="--c:${SHAPE[state.shape]}">more ${state.shape} &rarr;</b>`
    : `<span style="opacity:.75">sound over-represented vs. state average:</span>`
      + SHAPE_ORDER.map(t => `<b style="--c:${SHAPE[t]}">${t}</b>`).join('');
}

let hoverTimer = null;
function hoverCounty(gid, pathEl) {
  const list = currentList(gid);
  AudioKit.prefetch(list.map(([k]) => k));

  const top = list[0];
  const name = REGIONS[gid].name;
  if (!top) {
    showPeek(pathEl, null, `${name} County`, state.shape ? `no ${state.shape} here` : 'no recordings');
    return;
  }
  const sp = SPECIES[top[0]];
  showPeek(pathEl, top[0], sp.name, `${name} County · ${(sp.sound && sp.sound.tags.join(' · ')) || ''}`);

  if (!state.hoverSound) return;
  // Small delay so dragging the pointer across the state doesn't machine-gun.
  clearTimeout(hoverTimer);
  hoverTimer = setTimeout(() => AudioKit.play(top[0]), 90);
}

function showPeek(anchorEl, key, name, sub) {
  const peek = document.getElementById('peek');
  const stage = document.getElementById('stage');
  const b = anchorEl.getBoundingClientRect();
  const s = stage.getBoundingClientRect();
  peek.hidden = false;
  peek.style.left = (b.left + b.width / 2 - s.left) + 'px';
  peek.style.top = (b.top - s.top) + 'px';
  document.getElementById('peekName').textContent = name;
  document.getElementById('peekSub').textContent = sub;
  const c = document.getElementById('peekSpec');
  c.style.display = key ? '' : 'none';
  if (key) drawSpec(c, key);
}

function hidePeek() {
  document.getElementById('peek').hidden = true;
  clearTimeout(hoverTimer);
}

// ------------------------------------------------------------------ ring

function drawRing() {
  const ring = document.getElementById('ring');
  ring.innerHTML = '';
  if (!state.gid) return;

  const svg = document.getElementById('map');
  const box = svg.getBoundingClientRect();
  const scale = box.width / proj.width;
  const [cx0, cy0] = centroids[state.gid];

  // Shrink the halo on narrow screens rather than letting it swamp the map --
  // but never below the 44 px touch minimum, and fewer pips instead. The ring
  // radius is derived from this, so it has to be the single source of truth or
  // the pips overlap.
  const pip = box.width >= 560 ? 52 : (canHover() ? 38 : 46);
  const n = Math.min(box.width < 560 ? (canHover() ? 8 : 6) : RING_N,
                     currentList(state.gid).length);
  const list = currentList(state.gid).slice(0, n);
  if (!list.length) return;

  // Radius must be large enough that n pips fit around the circumference
  // without overlapping. This is in screen pixels -- scaling it by the map
  // scale (as an earlier version did) collapses the ring at small sizes.
  const R = Math.max((list.length * (pip + 8)) / (2 * Math.PI), pip * 1.5);
  const cx = cx0 * scale, cy = cy0 * scale;

  ring.style.setProperty('--pip', pip + 'px');
  // Warm every clip in the halo now, so sweeping it is 0 ms per bird.
  AudioKit.prefetch(list.map(([k]) => k));
  const ns = 'http://www.w3.org/2000/svg';
  const spokes = document.createElementNS(ns, 'svg');
  spokes.setAttribute('class', 'spoke');
  spokes.style.cssText = `left:0;top:0;width:100%;height:100%;overflow:visible`;
  ring.appendChild(spokes);

  list.forEach(([key], i) => {
    const a = (i / list.length) * Math.PI * 2 - Math.PI / 2;
    const x = cx + Math.cos(a) * R;
    const y = cy + Math.sin(a) * R;

    const line = document.createElementNS(ns, 'line');
    line.setAttribute('x1', cx); line.setAttribute('y1', cy);
    line.setAttribute('x2', x); line.setAttribute('y2', y);
    line.setAttribute('stroke', 'rgba(255,180,84,.22)');
    line.setAttribute('stroke-width', '1');
    spokes.appendChild(line);

    const sp = SPECIES[key];
    const pip = document.createElement('button');
    pip.className = 'pip';
    pip.dataset.key = key;
    pip.style.left = x + 'px';
    pip.style.top = y + 'px';
    pip.style.animationDelay = (i * 22) + 'ms';
    pip.title = sp.name;
    pip.setAttribute('aria-label', `${sp.name} — play call`);
    pip.innerHTML = sp.photo
      ? `<img src="${sp.photo.thumb}" alt="" loading="lazy">`
      : '<span class="n">🐦</span>';

    pip.addEventListener('pointerenter', () => {
      showPeek(pip, key, sp.name, (sp.sound && sp.sound.tags.join(' · ')) || '');
      if (state.hoverSound) AudioKit.play(key);
    });
    pip.addEventListener('focus', () => {
      showPeek(pip, key, sp.name, (sp.sound && sp.sound.tags.join(' · ')) || '');
      if (state.hoverSound) AudioKit.play(key);
    });
    pip.addEventListener('click', () => tapBird(key, pip, sp));
    ring.appendChild(pip);
  });
}

// ------------------------------------------------------------------ rail

function tagHtml(sp) {
  return ((sp.sound && sp.sound.tags) || [])
    .map(t => `<span class="tag" data-t="${t}">${t}</span>`).join('');
}

function drawRail() {
  const rail = document.getElementById('rail');
  rail.innerHTML = '';
  if (!state.gid) return;

  const list = currentList(state.gid);
  const name = REGIONS[state.gid].name;
  document.getElementById('place').textContent = name + ' County';
  const one = list.length === 1;
  document.getElementById('sub').textContent = state.shape
    ? `${list.length} bird${one ? '' : 's'} here in ${FULL[state.month]} with a ${state.shape}`
    : `${list.length} bird${one ? '' : 's'} you could hear in ${FULL[state.month]} · most reported first`;

  if (!list.length) {
    rail.innerHTML = `<p style="color:var(--muted);font-size:13px">Nothing matches — try another sound or month.</p>`;
    return;
  }

  const frag = document.createDocumentFragment();
  for (const [key] of list) {
    const sp = SPECIES[key];
    const b = document.createElement('button');
    b.className = 'bird';
    b.dataset.key = key;
    b.innerHTML =
      `<figure>${sp.photo ? `<img src="${sp.photo.thumb}" alt="" loading="lazy">` : ''}</figure>
       <div class="cap"><div class="cn"></div><div class="cs">${tagHtml(sp)}</div></div>`;
    b.querySelector('.cn').textContent = sp.name;
    b.addEventListener('pointerenter', () => { if (state.hoverSound) AudioKit.play(key); });
    // Keyboard focus is the hover equivalent, so it obeys the same toggle.
    b.addEventListener('focus', () => { if (state.hoverSound) AudioKit.play(key); });
    b.addEventListener('click', () => tapBird(key, b, sp));
    frag.appendChild(b);
  }
  rail.appendChild(frag);
}

// ------------------------------------------------------------------ sheet

/**
 * Acoustic nearest neighbours, in z-scored feature space.
 *
 * Hand-picked divisors (centroid/1, pulse/6, spread/900) silently stop meaning
 * anything the moment a feature's scale changes -- after the analysis band was
 * narrowed they put a cormorant next to a Chipping Sparrow. Standardising each
 * feature against the corpus makes the weighting explicit and scale-free:
 * every axis contributes one standard deviation per unit of distance.
 */
const FEATS = [
  s => Math.log(s.centroid + 50),   // pitch, perceptually log
  s => s.flatness == null ? 0.2 : s.flatness,   // tonal vs noisy
  s => Math.log(s.spread + 50),     // pure vs broad
  s => Math.sqrt(s.pulse || 0),     // rhythm; sqrt keeps fast trills from dominating
];

let zStats = null;
function featureStats() {
  if (zStats) return zStats;
  const rows = Object.values(SPECIES).filter(s => s.sound && s.clip).map(s => s.sound);
  zStats = FEATS.map((f, i) => {
    const v = rows.map(f);
    const m = v.reduce((a, b) => a + b, 0) / v.length;
    const sd = Math.sqrt(v.reduce((a, b) => a + (b - m) ** 2, 0) / v.length) || 1;
    return { m, sd };
  });
  return zStats;
}

function similar(key, n = 3) {
  const a = SPECIES[key].sound;
  if (!a) return [];
  const st = featureStats();
  const z = s => FEATS.map((f, i) => (f(s) - st[i].m) / st[i].sd);
  const va = z(a);
  return Object.keys(SPECIES)
    .filter(k => k !== key && SPECIES[k].sound && SPECIES[k].clip)
    .map(k => {
      const vb = z(SPECIES[k].sound);
      let d = 0;
      for (let i = 0; i < va.length; i++) d += (va[i] - vb[i]) ** 2;
      return [k, d];
    })
    .sort((x, y) => x[1] - y[1])
    .slice(0, n)
    .map(x => x[0]);
}

function openSheet(key) {
  const sp = SPECIES[key];
  const sheet = document.getElementById('sheet');
  sheet.hidden = false;
  state.sheetKey = key;

  const img = document.getElementById('sheetImg');
  if (sp.photo) { img.src = sp.photo.url; img.style.display = ''; } else { img.style.display = 'none'; }
  document.getElementById('sheetName').textContent = sp.name;
  document.getElementById('sheetSci').textContent = sp.sci;
  document.getElementById('sheetTags').innerHTML = tagHtml(sp);
  document.getElementById('sheetBlurb').textContent = sp.blurb || '';
  drawSpec(document.getElementById('sheetSpec'), key);

  const alike = document.getElementById('alike');
  alike.innerHTML = '';
  for (const k of similar(key)) {
    const o = SPECIES[k];
    const b = document.createElement('button');
    b.dataset.key = k;
    b.innerHTML = `${o.photo ? `<img src="${o.photo.thumb}" alt="">` : ''}<span></span>`;
    b.querySelector('span').textContent = o.name;
    b.addEventListener('pointerenter', () => { if (state.hoverSound) AudioKit.play(k); });
    // Click plays rather than navigating. Comparing is the whole point of this
    // row — jumping to a new sheet threw away the bird you were comparing against.
    b.addEventListener('click', () => AudioKit.play(k));
    alike.appendChild(b);
  }

  const bits = [];
  if (sp.photo) bits.push(`Photo: ${sp.photo.by} (${sp.photo.license.toUpperCase()})`);
  if (sp.audio) {
    bits.push(`Audio: <a href="${sp.audio.page}" target="_blank" rel="noopener">`
      + `${sp.audio.by || 'Wikimedia Commons'}</a> (${sp.audio.license}), trimmed &amp; normalised`);
  }
  if (sp.wiki) bits.push(`Text: <a href="${sp.wiki}" target="_blank" rel="noopener">Wikipedia</a> CC BY-SA 4.0`);
  document.getElementById('sheetAttrib').innerHTML = bits.join(' · ');

  AudioKit.play(key);
}

function closeSheet() {
  document.getElementById('sheet').hidden = true;
  AudioKit.stop();
}

// ------------------------------------------------------------------ control

function select(gid) {
  state.gid = gid;
  paint();
  drawRing();
  drawRail();
}

function setMonth(m) {
  state.month = m;
  for (const b of document.querySelectorAll('.mbtn')) b.classList.toggle('on', +b.dataset.m === m);
  paint();
  drawRing();
  drawRail();
}

function setShape(s) {
  state.shape = s;
  for (const c of document.querySelectorAll('.chip')) c.classList.toggle('on', c.dataset.shape === s);
  document.getElementById('tag').textContent = s
    ? `Showing where you'd hear a ${s}. Sweep the map.`
    : 'Sweep the map. Every county sounds different.';
  paint();
  drawRing();
  drawRail();
}

function toggleYear() {
  const btn = document.getElementById('yearplay');
  if (state.yearTimer) {
    clearInterval(state.yearTimer);
    state.yearTimer = null;
    btn.classList.remove('on');
    btn.textContent = '▶';
    return;
  }
  btn.classList.add('on');
  btn.textContent = '❚❚';
  state.yearTimer = setInterval(() => setMonth(state.month % 12 + 1), 900);
}

// ------------------------------------------------------------------ boot

async function boot() {
  const [geo, species, regions, specs] = await Promise.all([
    fetch('data/counties.geojson').then(r => r.json()),
    fetch('data/species.json').then(r => r.json()),
    fetch('data/regions.json').then(r => r.json()),
    fetch('data/spectrograms.json').then(r => r.json()).catch(() => ({})),
  ]);
  GEO = geo; SPECIES = species; REGIONS = regions; SPECS = specs;

  drawMap();

  const mwrap = document.getElementById('months');
  for (let m = 1; m <= 12; m++) {
    const b = document.createElement('button');
    b.className = 'mbtn' + (m === state.month ? ' on' : '');
    b.dataset.m = m;
    b.textContent = MONTHS[m];
    b.addEventListener('click', () => setMonth(m));
    mwrap.appendChild(b);
  }

  for (const c of document.querySelectorAll('.chip')) {
    c.addEventListener('click', () => setShape(state.shape === c.dataset.shape ? null : c.dataset.shape));
  }

  document.getElementById('hoverSound').addEventListener('change', e => {
    state.hoverSound = e.target.checked;
    if (!state.hoverSound) AudioKit.stop();
  });

  document.getElementById('yearplay').addEventListener('click', toggleYear);
  document.getElementById('sheetX').addEventListener('click', closeSheet);
  document.getElementById('sheetPlay').addEventListener('click', () => {
    if (state.sheetKey) AudioKit.play(state.sheetKey);
  });
  document.getElementById('sheet').addEventListener('click', e => {
    if (e.target.id === 'sheet') closeSheet();
  });
  document.addEventListener('keydown', e => {
    if (e.key === 'Escape') { closeSheet(); hidePeek(); }
    // Space replays the open bird — the natural key for "again".
    if ((e.key === ' ' || e.key === 'Spacebar') && !document.getElementById('sheet').hidden) {
      e.preventDefault();
      if (state.sheetKey) AudioKit.play(state.sheetKey);
    }
  });

  // The gate is the ONLY place audio gets unlocked, so the first sound a user
  // triggers is guaranteed to be audible. Hovering is not an activation
  // gesture, so without this the map is silent until an unrelated click.
  const gate = document.getElementById('gate');
  const openGate = () => {
    AudioKit.init();
    gate.classList.add('gone');
    setTimeout(() => { gate.hidden = true; }, 400);
  };
  document.getElementById('gateGo').addEventListener('click', openGate);
  gate.addEventListener('click', e => { if (e.target === gate) openGate(); });

  // Respect a checkbox the browser restored from a previous session.
  state.hoverSound = document.getElementById('hoverSound').checked;

  document.getElementById('stage').addEventListener('pointerleave', () => { hidePeek(); });
  window.addEventListener('resize', () => drawRing());

  // Belt and braces: if the gate is dismissed some other way, any real gesture
  // still unlocks. init() is idempotent.
  const unlock = () => AudioKit.init();
  window.addEventListener('pointerdown', unlock, { once: true });
  window.addEventListener('keydown', unlock, { once: true });

  const polk = Object.keys(REGIONS).find(g => REGIONS[g].name === 'Polk');
  select(polk);
}

boot();
