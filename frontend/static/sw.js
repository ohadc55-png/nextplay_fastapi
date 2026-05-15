/* ══════════════════════════════════════════════════════════════════════════
 *  NEXTPLAY main Service Worker
 *  Scope: /  (root)
 *
 *  Composes three responsibilities into ONE worker (browsers only allow one
 *  SW per scope):
 *    1. Background uploads — loaded via importScripts so the existing logic
 *       at /upload-sw.js is unchanged. install/activate/message handlers
 *       there fire normally; we just add more handlers below.
 *    2. Offline cache — network-first for HTML, cache-first for static
 *       assets. Lets coaches pull up the app without connection (read-only).
 *    3. Push notifications — handles 'push' + 'notificationclick' events.
 *
 *  Cache versioning: bump SW_VERSION whenever cache strategy changes so old
 *  caches get evicted on activate. The browser already revalidates the SW
 *  itself thanks to updateViaCache: 'none' in the registration call.
 * ════════════════════════════════════════════════════════════════════════ */

const SW_VERSION = 'pwa-v1';
const STATIC_CACHE = `nextplay-static-${SW_VERSION}`;
const PAGE_CACHE = `nextplay-pages-${SW_VERSION}`;

// Compose: load existing upload SW logic so background uploads keep working.
// All its addEventListener handlers attach to `self` and fire in addition
// to the ones we register below.
importScripts('/upload-sw.js');

/* ─── install: pre-cache app shell ──────────────────────────────────────── */
const APP_SHELL = [
  '/static/manifest.json',
  '/static/img/icons/icon-192.png',
  '/static/img/icons/icon-512.png',
  '/static/img/icons/apple-touch-icon-180.png',
  '/static/img/logo_transparent.png',
];

self.addEventListener('install', function (event) {
  event.waitUntil(
    caches.open(STATIC_CACHE).then(function (cache) {
      // Cache the shell, but don't fail install if any one resource is missing.
      return Promise.all(
        APP_SHELL.map(function (url) {
          return cache.add(url).catch(function (e) {
            console.warn('[SW] precache miss', url, e && e.message);
          });
        })
      );
    })
  );
});

/* ─── activate: evict old caches from previous SW versions ─────────────── */
self.addEventListener('activate', function (event) {
  event.waitUntil(
    caches.keys().then(function (keys) {
      return Promise.all(
        keys
          .filter(function (k) {
            return (
              (k.startsWith('nextplay-static-') && k !== STATIC_CACHE) ||
              (k.startsWith('nextplay-pages-') && k !== PAGE_CACHE)
            );
          })
          .map(function (k) {
            return caches.delete(k);
          })
      );
    })
  );
});

/* ─── fetch: routing strategy ──────────────────────────────────────────────
   /api/*           → network-only (never cache user data, never serve stale)
   /admin/*         → network-only (admin = Ohad-only, web-only)
   /static/*        → cache-first  (assets are content-versioned via ?v=)
   GET HTML pages   → network-first with cache fallback (offline read-only)
   anything else    → pass-through (let the browser decide)
   non-GET methods  → pass-through (cache only handles GET)
─────────────────────────────────────────────────────────────────────────── */
self.addEventListener('fetch', function (event) {
  const req = event.request;

  // SW only handles GET. POST/PUT/DELETE pass through; if offline, the browser
  // will return a network error — the page's JS shows the "offline" toast.
  if (req.method !== 'GET') return;

  const url = new URL(req.url);

  // Cross-origin (Google fonts, Resend, S3, etc.): pass through.
  if (url.origin !== self.location.origin) return;

  // Never cache API or admin — they contain live user/operational data.
  if (url.pathname.startsWith('/api/') || url.pathname.startsWith('/admin/')) {
    return; // pass through to network
  }

  // Media files (videos, audio) — pass through to the network. Browsers
  // request these with HTTP Range headers and receive 206 Partial Content
  // responses, which the Cache API's put() cannot store (throws TypeError).
  // Letting the network handle them directly preserves <video> seeking +
  // streaming and avoids breaking the page with cache errors.
  if (
    url.pathname.startsWith('/static/media/') ||
    /\.(mp4|webm|mov|m4v|ogv|mp3|m4a|ogg|wav)$/i.test(url.pathname)
  ) {
    return; // pass through to network
  }

  // Static assets: cache-first. They're cache-busted via ?v= so a new deploy
  // mints new URLs and the old entries become unreachable + auto-evicted.
  if (url.pathname.startsWith('/static/')) {
    event.respondWith(cacheFirst(req, STATIC_CACHE));
    return;
  }

  // Everything else GET = HTML page navigation. Network-first so coaches
  // see fresh content when online; falls back to cached copy when offline.
  if (req.mode === 'navigate' || (req.headers.get('accept') || '').includes('text/html')) {
    event.respondWith(networkFirstPage(req));
    return;
  }
});

async function cacheFirst(req, cacheName) {
  const cache = await caches.open(cacheName);
  const cached = await cache.match(req);
  if (cached) {
    // Refresh in background — eventual consistency is fine for static assets.
    fetch(req)
      .then(function (resp) {
        if (isCacheable(resp)) cache.put(req, resp.clone()).catch(function () {});
      })
      .catch(function () {});
    return cached;
  }
  try {
    const resp = await fetch(req);
    if (isCacheable(resp)) cache.put(req, resp.clone()).catch(function () {});
    return resp;
  } catch (e) {
    return new Response('', { status: 504, statusText: 'offline' });
  }
}

// Only cache full successful responses. 206 Partial Content (Range requests
// for media) and opaque/error responses throw on Cache.put().
function isCacheable(resp) {
  return resp && resp.ok && resp.status === 200 && resp.type === 'basic';
}

async function networkFirstPage(req) {
  const cache = await caches.open(PAGE_CACHE);
  try {
    const resp = await fetch(req);
    if (resp && resp.ok) cache.put(req, resp.clone());
    return resp;
  } catch (e) {
    const cached = await cache.match(req);
    if (cached) return cached;
    // No cache hit AND offline. Return a tiny inline shell so the user sees
    // SOMETHING instead of the browser's "no internet" Chrome page.
    return new Response(
      '<!DOCTYPE html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">' +
      '<title>NEXTPLAY — offline</title><style>body{margin:0;padding:48px 24px;background:#0a0e14;color:#f0f3f6;font-family:-apple-system,BlinkMacSystemFont,Segoe UI,Roboto,Helvetica,Arial,sans-serif;text-align:center}h1{color:#ff6b35;font-size:24px;margin:0 0 12px}p{color:#9ba4b0;line-height:1.6;max-width:420px;margin:0 auto 24px}a{color:#ff6b35;text-decoration:none;font-weight:600;display:inline-block;border:1px solid #ff6b35;padding:10px 24px;border-radius:8px}</style></head>' +
      '<body><h1>You\'re offline</h1><p>NEXTPLAY needs an internet connection to load this page. Your saved roster + chat history are still available — try opening one of those.</p><a href="/" onclick="location.reload();return false">Try again</a></body></html>',
      { status: 503, headers: { 'Content-Type': 'text/html; charset=utf-8' } }
    );
  }
}

/* ─── push: server-sent notification ────────────────────────────────────── */
self.addEventListener('push', function (event) {
  let data = {};
  try {
    data = event.data ? event.data.json() : {};
  } catch (e) {
    data = { title: 'NEXTPLAY', body: event.data ? event.data.text() : '' };
  }

  const title = data.title || 'NEXTPLAY';
  const options = {
    body: data.body || '',
    icon: data.icon || '/static/img/icons/icon-192.png',
    badge: data.badge || '/static/img/icons/icon-192.png',
    tag: data.tag || 'nextplay',
    renotify: data.renotify !== false,
    data: { url: data.url || '/', kind: data.kind || 'engagement' },
    requireInteraction: data.requireInteraction === true,
  };
  event.waitUntil(self.registration.showNotification(title, options));
});

/* ─── notificationclick: focus existing tab or open deep link ──────────── */
self.addEventListener('notificationclick', function (event) {
  event.notification.close();
  const targetUrl = (event.notification.data && event.notification.data.url) || '/';

  event.waitUntil(
    (async function () {
      const all = await self.clients.matchAll({ type: 'window', includeUncontrolled: true });
      // Prefer a tab already on the target URL (or origin), focus it.
      for (const client of all) {
        try {
          const u = new URL(client.url);
          if (u.origin === self.location.origin) {
            await client.focus();
            // Navigate the focused tab to the target if it's a different path.
            if (u.pathname + u.search !== targetUrl) {
              if ('navigate' in client) {
                return client.navigate(targetUrl);
              }
            }
            return;
          }
        } catch (e) {
          /* malformed URL — skip */
        }
      }
      // No existing tab — open new.
      if (self.clients.openWindow) {
        return self.clients.openWindow(targetUrl);
      }
    })()
  );
});
