// Service worker for Qwen Remote.
//
// Deliberately minimal. This app is a live view of a session in this box: the SSE
// stream, /send, /approve and /status must ALWAYS hit the network, and a cached
// answer would be a lie about what the session is doing. So the only thing cached is
// the icon set — enough to satisfy installability and to render the app shell
// quickly — and everything else passes straight through.
//
// Nothing carrying the token is ever cached: the start URL and API calls bypass the
// cache entirely, so the credential never lands in Cache Storage.

const CACHE = 'qwen-remote-v1';
const ASSETS = [
  '/icon-192.png',
  '/icon-256.png',
  '/icon-512.png',
  '/icon-maskable-512.png',
];

self.addEventListener('install', (event) => {
  event.waitUntil(
    caches.open(CACHE).then((c) => c.addAll(ASSETS)).then(() => self.skipWaiting())
  );
});

self.addEventListener('activate', (event) => {
  event.waitUntil(
    caches.keys()
      .then((keys) => Promise.all(keys.filter((k) => k !== CACHE).map((k) => caches.delete(k))))
      .then(() => self.clients.claim())
  );
});

self.addEventListener('fetch', (event) => {
  const url = new URL(event.request.url);
  const isIcon = url.origin === self.location.origin && /^\/icon-[\w-]+\.png$/.test(url.pathname);
  if (event.request.method !== 'GET' || !isIcon) {
    return;  // network, always — live session data must not be served from a cache
  }
  event.respondWith(
    caches.match(event.request).then((hit) => hit || fetch(event.request))
  );
});
