const CACHE = 'reseller-v2';
const STATIC = ['/', '/static/index.html', '/static/manifest.json'];

self.addEventListener('install', e => {
    e.waitUntil(caches.open(CACHE).then(c => c.addAll(STATIC)));
    self.skipWaiting();
});

self.addEventListener('activate', e => {
    e.waitUntil(caches.keys().then(keys =>
          Promise.all(keys.filter(k => k !== CACHE).map(k => caches.delete(k)))
                                     ));
    self.clients.claim();
});

self.addEventListener('fetch', e => {
    if (e.request.url.includes('/scan')) {
          // Always network for API calls
      return;
    }
    e.respondWith(
          caches.match(e.request).then(cached => cached || fetch(e.request))
        );
});
