/* Belgium Flight Heatmap: static front end. No build step, no server. */
(function () {
  'use strict';

  const EBBR = [50.9014, 4.4844];
  // Weights used for the score and for the heat colours.
  const BAND_WEIGHT = [1.0, 0.35, 0.1];   // low, mid, high
  const NIGHT_WEIGHT = 10;                 // like the +10 dB night penalty in Lden
  const SCORE_REF = 300;                   // weighted flights/day that maps to score 10
  const LAYER_NAMES = {
    modelled_year: 'Expected: typical year',
    modelled_west: 'Expected: westerly wind',
    modelled_east: 'Expected: easterly wind',
    modelled_weekend: 'Expected: weekend / night scheme',
    observed_30: 'Observed: last 30 days',
    observed_90: 'Observed: last 90 days',
    observed_365: 'Observed: last 12 months',
  };

  const $ = (s) => document.querySelector(s);
  const cache = {};
  const state = { layer: 'modelled_year', bands: [true, true, false], time: 'all' };

  // ---------------------------------------------------------------- map
  const map = L.map('map', { zoomControl: false, preferCanvas: true }).setView([50.90, 4.48], 11);
  L.control.zoom({ position: 'bottomright' }).addTo(map);
  L.tileLayer('https://{s}.basemaps.cartocdn.com/light_all/{z}/{x}/{y}{r}.png', {
    subdomains: 'abcd', maxZoom: 18,
    attribution: '© <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> © <a href="https://carto.com/attributions">CARTO</a> · flights © <a href="https://opensky-network.org">OpenSky</a>',
  }).addTo(map);
  L.control.scale({ imperial: false, position: 'bottomleft' }).addTo(map);
  L.circleMarker(EBBR, { radius: 5, color: '#14213d', weight: 2, fillOpacity: 1, fillColor: '#fff' })
    .bindTooltip('Brussels Airport').addTo(map);

  // ---------------------------------------------------------------- data
  async function loadLayer(name) {
    if (cache[name] !== undefined) return cache[name];
    try {
      const res = await fetch(`data/${name}.json`, { cache: 'no-cache' });
      if (!res.ok) throw new Error(res.status);
      const json = await res.json();
      const m = json.meta;
      const arr = new Float32Array(m.nrows * m.ncols * 6);
      for (const row of json.cells) {
        const base = (row[0] * m.ncols + row[1]) * 6;
        for (let s = 0; s < 6; s++) arr[base + s] = row[2 + s];
      }
      cache[name] = { meta: m, arr };
    } catch (e) {
      cache[name] = null;
    }
    return cache[name];
  }

  // Heat map: plain flights per day for the selected altitude bands and time of day.
  function heatWeights() {
    const w = new Float32Array(6);
    for (let b = 0; b < 3; b++) {
      const on = state.bands[b] ? 1 : 0;
      w[b] = state.time !== 'night' ? on : 0;
      w[b + 3] = state.time !== 'day' ? on : 0;
    }
    return w;
  }
  // Score: every flight counts, weighted by altitude, night x10.
  const SCORE_WEIGHTS = Float32Array.from([...BAND_WEIGHT, ...BAND_WEIGHT.map((x) => x * NIGHT_WEIGHT)]);

  function cellValue(layer, i, j, w) {
    const m = layer.meta;
    if (i < 0 || j < 0 || i >= m.nrows || j >= m.ncols) return 0;
    const base = (i * m.ncols + j) * 6;
    let v = 0;
    for (let s = 0; s < 6; s++) v += layer.arr[base + s] * w[s];
    return v;
  }

  // ---------------------------------------------------------------- colours
  const STOPS = [
    [0.00, [255, 237, 160, 0]],
    [0.12, [255, 237, 160, 110]],
    [0.35, [254, 178, 76, 170]],
    [0.60, [240, 59, 32, 195]],
    [0.82, [189, 0, 38, 210]],
    [1.00, [84, 39, 143, 225]],
  ];
  function colour(t) {
    t = Math.max(0, Math.min(1, t));
    for (let k = 1; k < STOPS.length; k++) {
      if (t <= STOPS[k][0]) {
        const [t0, c0] = STOPS[k - 1], [t1, c1] = STOPS[k];
        const f = (t - t0) / (t1 - t0);
        return c0.map((c, n) => c + (c1[n] - c) * f);
      }
    }
    return STOPS[STOPS.length - 1][1];
  }
  // 1/day -> ~0.0, 10/day -> 0.5, 100+/day -> 1.0
  const intensity = (v) => (v <= 0.05 ? 0 : Math.log10(1 + v * 9) / Math.log10(1 + 100 * 9));

  // ---------------------------------------------------------------- heat layer
  const Heat = L.GridLayer.extend({
    createTile(coords) {
      const tile = document.createElement('canvas');
      const size = this.getTileSize();
      tile.width = size.x; tile.height = size.y;
      const layer = cache[state.layer];
      if (!layer) return tile;
      const m = layer.meta;
      const nw = map.unproject(coords.scaleBy(size), coords.z);
      const se = map.unproject(coords.add([1, 1]).scaleBy(size), coords.z);
      const i0 = Math.floor((se.lat - m.lat0) / m.dlat) - 1;
      const i1 = Math.floor((nw.lat - m.lat0) / m.dlat) + 1;
      const j0 = Math.floor((nw.lng - m.lon0) / m.dlon) - 1;
      const j1 = Math.floor((se.lng - m.lon0) / m.dlon) + 1;
      if (i1 < 0 || j1 < 0 || i0 >= m.nrows || j0 >= m.ncols) return tile;
      const h = i1 - i0 + 1, w = j1 - j0 + 1;
      const src = document.createElement('canvas');
      src.width = w; src.height = h;
      const sctx = src.getContext('2d');
      const img = sctx.createImageData(w, h);
      const wts = heatWeights();
      let any = false;
      for (let i = i0; i <= i1; i++) {
        for (let j = j0; j <= j1; j++) {
          const v = cellValue(layer, i, j, wts);
          if (v <= 0.05) continue;
          any = true;
          const c = colour(intensity(v));
          const p = ((i1 - i) * w + (j - j0)) * 4;
          img.data[p] = c[0]; img.data[p + 1] = c[1]; img.data[p + 2] = c[2]; img.data[p + 3] = c[3];
        }
      }
      if (!any) return tile;
      sctx.putImageData(img, 0, 0);
      // Pixel centres of the source image sit on cell centres.
      const origin = coords.scaleBy(size);
      const tl = map.project([m.lat0 + (i1 + 0.5) * m.dlat, m.lon0 + (j0 + 0.5) * m.dlon], coords.z).subtract(origin);
      const br = map.project([m.lat0 + (i0 + 0.5) * m.dlat, m.lon0 + (j1 + 0.5) * m.dlon], coords.z).subtract(origin);
      const sx = (br.x - tl.x) / Math.max(1, w - 1), sy = (br.y - tl.y) / Math.max(1, h - 1);
      // Draw onto a padded canvas with a light blur, then crop, so tiles join without seams.
      const blur = Math.max(0, Math.min(10, sx * 0.45));
      const pad = Math.ceil(blur * 2);
      const big = document.createElement('canvas');
      big.width = size.x + 2 * pad; big.height = size.y + 2 * pad;
      const bctx = big.getContext('2d');
      bctx.imageSmoothingEnabled = true;
      bctx.imageSmoothingQuality = 'high';
      if (blur >= 1) bctx.filter = `blur(${blur.toFixed(1)}px)`;
      bctx.drawImage(src, tl.x - sx / 2 + pad, tl.y - sy / 2 + pad, w * sx, h * sy);
      tile.getContext('2d').drawImage(big, -pad, -pad);
      return tile;
    },
  });
  const heat = new Heat({ opacity: 0.85, zIndex: 400 }).addTo(map);

  async function refresh() {
    const layer = await loadLayer(state.layer);
    const info = $('#layer-info');
    if (!layer) {
      info.textContent = state.layer.startsWith('observed')
        ? 'No observed data yet. It appears after the first daily data update has run.'
        : 'This layer could not be loaded.';
    } else if (layer.meta.source === 'opensky') {
      info.textContent = `Average per day over ${layer.meta.days} days of real tracks (${layer.meta.from} to ${layer.meta.to}).`;
    } else {
      info.textContent = 'Model of expected flights per day, based on runway rules and published routes. Approximate.';
    }
    heat.redraw();
    if (marker) showCard(marker.getLatLng(), lastLabel);
  }

  // ---------------------------------------------------------------- score card
  let marker = null, lastLabel = '';

  function pointStats(layer, lat, lng) {
    const m = layer.meta;
    const fi = (lat - m.lat0) / m.dlat - 0.5, fj = (lng - m.lon0) / m.dlon - 0.5;
    const i = Math.floor(fi), j = Math.floor(fj), di = fi - i, dj = fj - j;
    const out = new Float32Array(6);
    // bilinear interpolation between the four nearest cell centres
    const corners = [[i, j, (1 - di) * (1 - dj)], [i + 1, j, di * (1 - dj)], [i, j + 1, (1 - di) * dj], [i + 1, j + 1, di * dj]];
    for (const [ci, cj, f] of corners) {
      if (ci < 0 || cj < 0 || ci >= m.nrows || cj >= m.ncols) continue;
      const base = (ci * m.ncols + cj) * 6;
      for (let s = 0; s < 6; s++) out[s] += layer.arr[base + s] * f;
    }
    return out;
  }

  function score(s) {
    let v = 0;
    for (let k = 0; k < 6; k++) v += s[k] * SCORE_WEIGHTS[k];
    return Math.min(10, (10 * Math.log10(1 + v)) / Math.log10(1 + SCORE_REF));
  }

  function verdict(sc) {
    if (sc < 2) return ['Quiet', '#2b8a3e'];
    if (sc < 4) return ['Occasional', '#5c940d'];
    if (sc < 6) return ['Noticeable', '#e67700'];
    if (sc < 8) return ['Frequent', '#d9480f'];
    return ['Severe', '#c92a2a'];
  }

  const fmt = (x) => (x < 0.05 ? '0' : x < 10 ? x.toFixed(1) : Math.round(x).toString());

  function distanceKm(a, b) {
    return map.distance(a, b) / 1000;
  }

  async function showCard(latlng, label) {
    lastLabel = label || '';
    if (marker) marker.setLatLng(latlng); else marker = L.marker(latlng).addTo(map);
    const names = Object.keys(LAYER_NAMES);
    const layers = await Promise.all(names.map(loadLayer));
    const cur = layers[names.indexOf(state.layer)];
    const body = $('#card-body');
    const title = label ? escapeHtml(label) : `${latlng.lat.toFixed(4)}, ${latlng.lng.toFixed(4)}`;
    let html = `<h2>${title}</h2><div class="muted small">${distanceKm(latlng, EBBR).toFixed(1)} km from Brussels Airport</div>`;
    if (cur) {
      const s = pointStats(cur, latlng.lat, latlng.lng);
      const sc = score(s);
      const [word, col] = verdict(sc);
      const day = s[0] + s[1] + s[2], night = s[3] + s[4] + s[5];
      html += `<div class="score"><span class="num">${sc.toFixed(1)}</span><span><span class="pill" style="background:${col}">${word}</span><br><span class="small muted">out of 10 · ${LAYER_NAMES[state.layer]}</span></span></div>`;
      html += `<table><tr><th>Flights per day overhead</th><th>Day</th><th>Night</th></tr>
        <tr><td>Low, under 600 m</td><td>${fmt(s[0])}</td><td>${fmt(s[3])}</td></tr>
        <tr><td>Medium, 600 to 1,500 m</td><td>${fmt(s[1])}</td><td>${fmt(s[4])}</td></tr>
        <tr><td>High, 1,500 to 3,000 m</td><td>${fmt(s[2])}</td><td>${fmt(s[5])}</td></tr>
        <tr><th>Total</th><th>${fmt(day)}</th><th>${fmt(night)}</th></tr></table>`;
    }
    html += `<table style="margin-top:10px"><tr><th>Scenario</th><th>Score</th><th>Flights/day</th></tr>`;
    names.forEach((n, k) => {
      const l = layers[k];
      if (!l) return;
      const s = pointStats(l, latlng.lat, latlng.lng);
      const tot = s.reduce((a, b) => a + b, 0);
      html += `<tr${n === state.layer ? ' style="font-weight:600"' : ''}><td>${LAYER_NAMES[n]}</td><td>${score(s).toFixed(1)}</td><td>${fmt(tot)}</td></tr>`;
    });
    html += `</table><p class="note">Flights counted within about 250 m, below 3,000 m. The score weights low flights most and counts night flights ten times. Compare the westerly and easterly rows: the same house can be quiet most of the year and busy when the wind turns.</p>`;
    body.innerHTML = html;
    $('#card').hidden = false;
    saveUrl();
  }

  function escapeHtml(s) {
    return s.replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
  }

  // ---------------------------------------------------------------- search
  let lastSearch = 0;
  async function search(q) {
    const wait = 1100 - (Date.now() - lastSearch);   // Nominatim: max 1 request/second
    if (wait > 0) await new Promise((r) => setTimeout(r, wait));
    lastSearch = Date.now();
    const url = 'https://nominatim.openstreetmap.org/search?' + new URLSearchParams({
      q, format: 'jsonv2', countrycodes: 'be', limit: '5', addressdetails: '0',
    });
    const res = await fetch(url, { headers: { 'Accept-Language': navigator.language || 'en' } });
    if (!res.ok) throw new Error('search failed');
    return res.json();
  }

  $('#search').addEventListener('submit', async (e) => {
    e.preventDefault();
    const q = $('#q').value.trim();
    const ul = $('#results');
    if (!q) return;
    ul.hidden = false;
    ul.innerHTML = '<li>Searching…</li>';
    try {
      const hits = await search(q);
      if (!hits.length) { ul.innerHTML = '<li>No address found in Belgium.</li>'; return; }
      ul.innerHTML = '';
      hits.forEach((h) => {
        const li = document.createElement('li');
        li.tabIndex = 0;
        li.textContent = h.display_name;
        const pick = () => {
          const ll = L.latLng(+h.lat, +h.lon);
          map.setView(ll, Math.max(map.getZoom(), 14));
          showCard(ll, h.display_name.split(',').slice(0, 3).join(','));
          ul.hidden = true;
          if (window.innerWidth <= 720) setPanel(false);
        };
        li.addEventListener('click', pick);
        li.addEventListener('keydown', (ev) => { if (ev.key === 'Enter') pick(); });
        ul.appendChild(li);
      });
    } catch (err) {
      ul.innerHTML = '<li>Address search is unavailable right now. Tap the map instead.</li>';
    }
  });

  // ---------------------------------------------------------------- controls
  $('#layer').addEventListener('change', (e) => { state.layer = e.target.value; saveUrl(); refresh(); });
  document.querySelectorAll('input[name=band]').forEach((el) =>
    el.addEventListener('change', () => { state.bands[+el.value] = el.checked; saveUrl(); refresh(); }));
  document.querySelectorAll('input[name=time]').forEach((el) =>
    el.addEventListener('change', () => { if (el.checked) { state.time = el.value; saveUrl(); refresh(); } }));
  map.on('click', (e) => showCard(e.latlng));
  $('#close-card').addEventListener('click', () => {
    $('#card').hidden = true;
    if (marker) { map.removeLayer(marker); marker = null; }
    saveUrl();
  });
  $('#about-link').addEventListener('click', (e) => { e.preventDefault(); $('#about').showModal(); });

  function setPanel(open) {
    $('#panel').classList.toggle('collapsed', !open);
    $('#toggle-panel').setAttribute('aria-expanded', String(open));
  }
  $('#toggle-panel').addEventListener('click', () => setPanel($('#panel').classList.contains('collapsed')));
  if (window.innerWidth <= 720) setPanel(false);

  // Shareable links: #layer=...&bands=110&time=all&at=lat,lng
  function saveUrl() {
    const p = new URLSearchParams({ layer: state.layer, bands: state.bands.map(Number).join(''), time: state.time });
    if (marker && !$('#card').hidden) {
      const ll = marker.getLatLng();
      p.set('at', `${ll.lat.toFixed(5)},${ll.lng.toFixed(5)}`);
    }
    history.replaceState(null, '', '#' + p.toString());
  }
  function readUrl() {
    const p = new URLSearchParams(location.hash.slice(1));
    if (p.get('layer') && LAYER_NAMES[p.get('layer')]) state.layer = p.get('layer');
    if (/^[01]{3}$/.test(p.get('bands') || '')) state.bands = p.get('bands').split('').map((c) => c === '1');
    if (['all', 'day', 'night'].includes(p.get('time'))) state.time = p.get('time');
    $('#layer').value = state.layer;
    document.querySelectorAll('input[name=band]').forEach((el) => { el.checked = state.bands[+el.value]; });
    document.querySelectorAll('input[name=time]').forEach((el) => { el.checked = el.value === state.time; });
    const at = (p.get('at') || '').split(',').map(Number);
    return at.length === 2 && at.every(isFinite) ? L.latLng(at[0], at[1]) : null;
  }
  const startAt = readUrl();

  // Disable observed options that have no data yet.
  Promise.all(['observed_30', 'observed_90', 'observed_365'].map(loadLayer)).then((ls) => {
    ls.forEach((l, k) => {
      const opt = $(`#layer option[value=observed_${[30, 90, 365][k]}]`);
      if (!l) { opt.disabled = true; opt.textContent += ' (no data yet)'; }
    });
  });

  refresh().then(() => {
    if (startAt) { map.setView(startAt, 14); showCard(startAt); }
  });

  if ('serviceWorker' in navigator) {
    window.addEventListener('load', () => navigator.serviceWorker.register('sw.js').catch(() => {}));
  }
})();
