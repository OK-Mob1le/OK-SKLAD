/* OK-Mobile · service worker: делает приложение установимым и открывающимся без сети */
const VER = 'okm-v3';
const SHELL = ['./', 'index.html', 'manifest.webmanifest', 'icons/icon-192.png', 'icons/icon-512.png', 'icons/apple-touch-icon.png'];

self.addEventListener('install', e => {
  e.waitUntil(caches.open(VER).then(c => c.addAll(SHELL)).then(() => self.skipWaiting()));
});

self.addEventListener('activate', e => {
  e.waitUntil(
    caches.keys()
      .then(keys => Promise.all(keys.filter(k => k !== VER).map(k => caches.delete(k))))
      .then(() => self.clients.claim())
  );
});

self.addEventListener('fetch', e => {
  const req = e.request;
  if (req.method !== 'GET') return;                              // POST (Telegram API и т.п.) не трогаем
  const url = new URL(req.url);
  if (url.origin !== location.origin) return;                    // Telegram, Firebase, шрифты — напрямую

  if (req.mode === 'navigate') {                                 // страница: сначала сеть (свежая версия), при отсутствии сети — кэш
    e.respondWith(
      fetch(req)
        .then(res => {
          if (res.ok) { const copy = res.clone(); caches.open(VER).then(c => c.put('index.html', copy)); }
          return res;
        })
        .catch(() => caches.match('index.html').then(r => r || caches.match('./')))
    );
    return;
  }

  e.respondWith(                                                 // остальное: из кэша, иначе сеть
    caches.match(req).then(hit => hit || fetch(req).then(res => {
      if (res.ok) { const copy = res.clone(); caches.open(VER).then(c => c.put(req, copy)); }
      return res;
    }))
  );
});
