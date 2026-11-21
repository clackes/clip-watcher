/* ClipVision Companion service worker.
   This app only works while its local server runs, so the worker is deliberately conservative:
   - navigations go to the network first (so the page's one-time token is always fresh), with a small
     offline card as the fallback when the server is down;
   - versioned static assets (…?v=<mtime>), fonts, icons and the manifest are cache-first: the URL
     changes whenever the file does, so the cache never serves a stale script;
   - the API, media, captions, profile images and the event stream are never touched — always network. */
const VERSION = 'cv-1';
const SHELL = 'cv-shell-' + VERSION;
const ASSETS = 'cv-assets-' + VERSION;

const OFFLINE_URL = '/__offline';
const OFFLINE_HTML = `<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
<title>ClipVision Companion — offline</title>
<style>:root{color-scheme:dark}html,body{height:100%}body{margin:0;display:grid;place-items:center;
background:#11111c;color:#edf5ff;font:400 16px/1.5 system-ui,sans-serif;text-align:center;padding:24px}
.card{max-width:360px}.mark{width:72px;height:72px;border-radius:20px;margin:0 auto 20px;
background:linear-gradient(135deg,#609fee,#ff9d9a 66%,#ab4bda);display:grid;place-items:center}
.mark svg{width:34px;height:34px;fill:#fff}h1{font-size:1.25rem;margin:0 0 8px}
p{color:rgb(237 245 255/.6);margin:0 0 20px}button{font:inherit;color:#fff;border:0;cursor:pointer;
padding:12px 22px;border-radius:14px;background:linear-gradient(135deg,#609fee,#ff9d9a 66%,#ab4bda)}</style></head>
<body><div class="card"><div class="mark"><svg viewBox="0 0 24 24"><path d="M7 4.5v15l13-7.5z"/></svg></div>
<h1>The companion server isn't reachable</h1>
<p>ClipVision Companion talks to the bot running on this machine. Start it, then try again.</p>
<button onclick="location.reload()">Try again</button></div></body></html>`;

function isApi(url) {
  return (
    url.pathname.startsWith('/api/') ||
    url.pathname.startsWith('/media/') ||
    url.pathname.startsWith('/captions/') ||
    url.pathname.startsWith('/profile/') ||
    url.pathname.startsWith('/connect/image/') ||
    url.pathname.endsWith('/events')
  );
}

function isAsset(url) {
  return (
    url.pathname.startsWith('/static/') ||
    url.pathname.startsWith('/fonts/') ||
    url.pathname === '/manifest.webmanifest'
  );
}

self.addEventListener('install', e => {
  e.waitUntil(
    caches
      .open(SHELL)
      .then(c => c.put(OFFLINE_URL, new Response(OFFLINE_HTML, { headers: { 'Content-Type': 'text/html; charset=utf-8' } })))
      .then(() => self.skipWaiting())
  );
});

self.addEventListener('activate', e => {
  e.waitUntil(
    caches
      .keys()
      .then(keys => Promise.all(keys.filter(k => k !== SHELL && k !== ASSETS).map(k => caches.delete(k))))
      .then(() => self.clients.claim())
  );
});

self.addEventListener('fetch', e => {
  const req = e.request;
  if (req.method !== 'GET') return;
  const url = new URL(req.url);
  if (url.origin !== self.location.origin) return;
  if (isApi(url)) return; // always the network, never the worker

  if (req.mode === 'navigate') {
    e.respondWith(fetch(req).catch(() => caches.match(OFFLINE_URL)));
    return;
  }

  if (isAsset(url)) {
    e.respondWith(
      caches.open(ASSETS).then(async cache => {
        const hit = await cache.match(req);
        if (hit) return hit;
        const res = await fetch(req);
        if (res && res.ok && res.type === 'basic') cache.put(req, res.clone());
        return res;
      })
    );
  }
});
