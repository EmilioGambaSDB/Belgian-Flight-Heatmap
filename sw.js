// Offline support: app shell cached, data fetched fresh when online.
const CACHE = 'bfh-v1';
const SHELL = ['./', 'index.html', 'style.css', 'app.js', 'manifest.webmanifest', 'icons/icon.svg',
  'vendor/leaflet/leaflet.css', 'vendor/leaflet/leaflet.js'];

self.addEventListener('install', (e) => {
  e.waitUntil(caches.open(CACHE).then((c) => c.addAll(SHELL)).then(() => self.skipWaiting()));
});
self.addEventListener('activate', (e) => {
  e.waitUntil(caches.keys().then((ks) => Promise.all(ks.filter((k) => k !== CACHE).map((k) => caches.delete(k))))
    .then(() => self.clients.claim()));
});
self.addEventListener('fetch', (e) => {
  const url = new URL(e.request.url);
  if (e.request.method !== 'GET' || url.hostname.includes('nominatim')) return;
  const sameOrigin = url.origin === location.origin;
  const isTile = url.hostname.includes('basemaps.cartocdn.com');
  if (sameOrigin) {
    // network first, fall back to cache
    e.respondWith(fetch(e.request).then((r) => {
      if (r.ok) { const copy = r.clone(); caches.open(CACHE).then((c) => c.put(e.request, copy)); }
      return r;
    }).catch(() => caches.match(e.request)));
  } else if (isTile) {
    e.respondWith(caches.match(e.request).then((hit) => hit || fetch(e.request)));
  }
});
