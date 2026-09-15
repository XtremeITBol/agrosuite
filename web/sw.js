/* Service worker de AgroSuite.
 *
 * Tres responsabilidades:
 *  1. Cachear el esqueleto de la app para que /campo abra sin señal.
 *  2. Guardar el catálogo (lotes y especies) para poder completar el
 *     formulario en un lote sin cobertura.
 *  3. Vaciar la cola de observaciones cuando vuelve la conexión, incluso si
 *     no hay ninguna pestaña abierta (Background Sync).
 *
 * Requiere contexto seguro: sobre http:// en una IP de LAN el navegador ni
 * siquiera registra este archivo. Por eso el servidor sirve HTTPS con una CA
 * local.
 */
'use strict';

importScripts('/idb-queue.js');

var VERSION = 'v3';
var SHELL_CACHE = 'agrosuite-shell-' + VERSION;
var DATA_CACHE = 'agrosuite-data-' + VERSION;

var SHELL = [
  '/campo',
  '/campo.html',
  '/idb-queue.js',
  '/manifest.webmanifest',
  '/icons/icon-192.png',
  '/icons/icon-512.png'
];

self.addEventListener('install', function (e) {
  e.waitUntil(
    caches.open(SHELL_CACHE)
      // addAll falla entero si un recurso falla; se cachea de a uno para que
      // un 404 aislado no impida instalar el service worker.
      .then(function (c) {
        return Promise.all(SHELL.map(function (u) {
          return c.add(u).catch(function () { return null; });
        }));
      })
      .then(function () { return self.skipWaiting(); })
  );
});

self.addEventListener('activate', function (e) {
  e.waitUntil(
    caches.keys().then(function (keys) {
      return Promise.all(keys.map(function (k) {
        if (k !== SHELL_CACHE && k !== DATA_CACHE) return caches.delete(k);
      }));
    }).then(function () { return self.clients.claim(); })
  );
});

function networkFirst(req, cacheName) {
  return fetch(req).then(function (res) {
    if (res && res.ok) {
      var copy = res.clone();
      caches.open(cacheName).then(function (c) { c.put(req, copy); });
    }
    return res;
  }).catch(function () {
    return caches.match(req).then(function (hit) {
      return hit || new Response(
        JSON.stringify({ error: 'Sin conexión y sin copia en caché.', offline: true }),
        { status: 503, headers: { 'Content-Type': 'application/json' } });
    });
  });
}

function cacheFirst(req, cacheName) {
  return caches.match(req).then(function (hit) {
    if (hit) return hit;
    return fetch(req).then(function (res) {
      if (res && res.ok) {
        var copy = res.clone();
        caches.open(cacheName).then(function (c) { c.put(req, copy); });
      }
      return res;
    });
  });
}

self.addEventListener('fetch', function (e) {
  var req = e.request;
  if (req.method !== 'GET') return;                 // los POST nunca se cachean
  var url = new URL(req.url);
  if (url.origin !== self.location.origin) return;

  // Navegación: intentar red, caer a la copia de /campo.
  if (req.mode === 'navigate') {
    e.respondWith(
      fetch(req).catch(function () {
        return caches.match('/campo').then(function (hit) {
          return hit || caches.match('/campo.html');
        });
      })
    );
    return;
  }

  // El catálogo debe estar disponible sin señal: red primero, caché si falla.
  if (url.pathname === '/api/field/catalog') {
    e.respondWith(networkFirst(req, DATA_CACHE));
    return;
  }

  // El resto de la API es estado vivo: sin caché, para no mostrar datos viejos
  // como si fueran actuales.
  if (url.pathname.indexOf('/api/') === 0) return;

  e.respondWith(cacheFirst(req, SHELL_CACHE));
});

/* Background Sync: el navegador dispara esto solo, al recuperar conectividad. */
self.addEventListener('sync', function (e) {
  if (e.tag === 'sync-observations') {
    e.waitUntil(
      self.AgroQueue.flushQueue().then(function (r) {
        return self.clients.matchAll({ includeUncontrolled: true }).then(function (cs) {
          cs.forEach(function (c) { c.postMessage({ type: 'sync-result', result: r }); });
        });
      })
    );
  }
});

/* Sincronización a pedido desde la página (botón "Sincronizar ahora"), y
   respaldo para navegadores sin Background Sync. */
self.addEventListener('message', function (e) {
  if (e.data && e.data.type === 'flush') {
    e.waitUntil(
      self.AgroQueue.flushQueue().then(function (r) {
        if (e.source) e.source.postMessage({ type: 'sync-result', result: r });
      })
    );
  }
});
