/* Cola de monitoreo offline sobre IndexedDB.
 *
 * Este archivo lo cargan DOS contextos: la página (campo.html) y el service
 * worker (sw.js, vía importScripts). Por eso no toca el DOM ni asume `window`:
 * el Background Sync del navegador despierta al service worker cuando vuelve
 * la señal, sin que haya ninguna pestaña abierta, y desde ahí se vacía la cola.
 *
 * La idempotencia vive en el `client_uuid` que se genera acá: el servidor
 * tiene un índice único sobre esa columna, así que reenviar la cola entera
 * nunca duplica una observación.
 */
(function (root) {
  'use strict';

  var DB_NAME = 'agrosuite', DB_VERSION = 1;
  var QUEUE = 'queue', META = 'meta';

  function openDB() {
    return new Promise(function (resolve, reject) {
      var req = indexedDB.open(DB_NAME, DB_VERSION);
      req.onupgradeneeded = function (e) {
        var db = e.target.result;
        if (!db.objectStoreNames.contains(QUEUE)) {
          db.createObjectStore(QUEUE, { keyPath: 'client_uuid' });
        }
        if (!db.objectStoreNames.contains(META)) {
          db.createObjectStore(META, { keyPath: 'key' });
        }
      };
      req.onsuccess = function () { resolve(req.result); };
      req.onerror = function () { reject(req.error); };
    });
  }

  function tx(store, mode, fn) {
    return openDB().then(function (db) {
      return new Promise(function (resolve, reject) {
        var t = db.transaction(store, mode);
        var out = fn(t.objectStore(store));
        t.oncomplete = function () { resolve(out && out.result !== undefined ? out.result : out); };
        t.onerror = function () { reject(t.error); };
        t.onabort = function () { reject(t.error); };
      });
    });
  }

  function uuid() {
    if (root.crypto && root.crypto.randomUUID) return root.crypto.randomUUID();
    // Respaldo para WebView viejos: aleatoriedad criptográfica, formato v4.
    var b = new Uint8Array(16);
    (root.crypto || {}).getRandomValues
      ? root.crypto.getRandomValues(b)
      : b.forEach(function (_, i) { b[i] = Math.floor(Math.random() * 256); });
    b[6] = (b[6] & 0x0f) | 0x40; b[8] = (b[8] & 0x3f) | 0x80;
    var h = [].map.call(b, function (x) { return ('0' + x.toString(16)).slice(-2); }).join('');
    return [h.slice(0,8), h.slice(8,12), h.slice(12,16), h.slice(16,20), h.slice(20)].join('-');
  }

  function enqueue(obs) {
    var row = Object.assign({}, obs);
    if (!row.client_uuid) row.client_uuid = uuid();
    row.queued_at = new Date().toISOString();
    row.attempts = 0;
    return tx(QUEUE, 'readwrite', function (s) { s.put(row); }).then(function () { return row; });
  }

  function listQueue() {
    return tx(QUEUE, 'readonly', function (s) { return s.getAll(); });
  }

  function removeMany(uuids) {
    if (!uuids.length) return Promise.resolve(0);
    return tx(QUEUE, 'readwrite', function (s) {
      uuids.forEach(function (u) { s.delete(u); });
    }).then(function () { return uuids.length; });
  }

  function bumpAttempts(uuids) {
    return tx(QUEUE, 'readwrite', function (s) {
      uuids.forEach(function (u) {
        var g = s.get(u);
        g.onsuccess = function () {
          var row = g.result;
          if (row) { row.attempts = (row.attempts || 0) + 1; s.put(row); }
        };
      });
    });
  }

  function setMeta(key, value) {
    return tx(META, 'readwrite', function (s) { s.put({ key: key, value: value }); });
  }

  function getMeta(key) {
    return tx(META, 'readonly', function (s) { return s.get(key); })
      .then(function (r) { return r ? r.value : null; });
  }

  /* Vacía la cola contra el servidor.
   * Devuelve {sent, accepted, duplicated, rejected, offline}.
   * Los rechazos permanentes (HTTP 400 por dato inválido) se sacan de la cola:
   * reintentar para siempre un registro corrupto la dejaría trabada. */
  function flushQueue() {
    return listQueue().then(function (rows) {
      if (!rows.length) return { sent: 0, accepted: 0, duplicated: 0, rejected: 0 };
      var payload = rows.map(function (r) {
        var o = Object.assign({}, r);
        delete o.queued_at; delete o.attempts;
        return o;
      });
      return fetch('/api/field/observations', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ observations: payload })
      }).then(function (res) {
        if (!res.ok && res.status !== 207) throw new Error('HTTP ' + res.status);
        return res.json();
      }).then(function (data) {
        var done = (data.accepted || []).map(function (a) { return a.client_uuid; })
          .concat((data.duplicated || []).map(function (d) { return d.client_uuid; }));
        var bad = (data.rejected || [])
          .map(function (r) { return r.client_uuid; })
          .filter(Boolean);
        return removeMany(done.concat(bad)).then(function () {
          return setMeta('last_sync', new Date().toISOString());
        }).then(function () {
          return {
            sent: rows.length,
            accepted: (data.accepted || []).length,
            duplicated: (data.duplicated || []).length,
            rejected: (data.rejected || []).length,
            detail: data.rejected || []
          };
        });
      }).catch(function (err) {
        // Sin red: la cola queda intacta para el próximo intento.
        return bumpAttempts(rows.map(function (r) { return r.client_uuid; }))
          .then(function () {
            return { sent: 0, accepted: 0, duplicated: 0, rejected: 0,
                     offline: true, error: String(err && err.message || err) };
          });
      });
    });
  }

  root.AgroQueue = {
    enqueue: enqueue, listQueue: listQueue, flushQueue: flushQueue,
    removeMany: removeMany, setMeta: setMeta, getMeta: getMeta, uuid: uuid
  };
})(typeof self !== 'undefined' ? self : this);
