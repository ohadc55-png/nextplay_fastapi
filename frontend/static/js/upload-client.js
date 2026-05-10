/* ══════════ NextPlay Upload Client ══════════
 * Page-side glue for Service Worker background uploads.
 * - Registers the SW
 * - Detects support and falls back gracefully
 * - BroadcastChannel listener drives the #bgUploadBanner
 * - Exposes window.NextPlayUpload API for scouting.js
 */
(function() {
  'use strict';

  // Was '/upload-sw.js' (upload-only worker). Now we register the unified
  // /sw.js which importScripts('/upload-sw.js') so the upload engine still
  // runs PLUS PWA offline cache + push handlers live in the same worker.
  var SW_URL = '/sw.js';
  var LEGACY_SW_URL = '/upload-sw.js';
  var CHANNEL_NAME = 'nextplay-uploads';
  var BANNER_ID = 'bgUploadBanner';

  var _swMode = 'unknown'; // 'unknown' | 'active' | 'failed' | 'unsupported'
  var _swRegistration = null;
  var _channel = null;
  var _activeJobs = new Map(); // uploadId -> last progress state
  var _disabledViaQuery = false;
  var _readyResolve;
  var _readyPromise = new Promise(function(r) { _readyResolve = r; });

  try {
    var params = new URLSearchParams(location.search);
    if (params.get('nosw') === '1') _disabledViaQuery = true;
  } catch(e) {}

  function isSecureOrLocal() {
    return window.isSecureContext || location.hostname === 'localhost' || location.hostname === '127.0.0.1';
  }

  function canUseSW() {
    if (_disabledViaQuery) return false;
    if (!('serviceWorker' in navigator)) return false;
    if (!('indexedDB' in window)) return false;
    if (!('BroadcastChannel' in window)) return false;
    if (!isSecureOrLocal()) return false;
    return true;
  }

  function getActiveSW() {
    // Works regardless of whether page is controlled (use registration.active)
    if (_swRegistration && _swRegistration.active) return _swRegistration.active;
    return navigator.serviceWorker.controller;
  }

  async function pingSW(timeoutMs) {
    return new Promise(function(resolve) {
      var target = getActiveSW();
      if (!target) { resolve(false); return; }
      var mc = new MessageChannel();
      var done = false;
      var timer = setTimeout(function() {
        if (!done) { done = true; resolve(false); }
      }, timeoutMs || 2000);
      mc.port1.onmessage = function(e) {
        if (done) return;
        done = true;
        clearTimeout(timer);
        resolve(!!(e.data && e.data.type === 'PONG'));
      };
      try {
        target.postMessage({ type: 'PING' }, [mc.port2]);
      } catch (e) {
        done = true;
        clearTimeout(timer);
        resolve(false);
      }
    });
  }

  async function initSW() {
    if (!canUseSW()) { _swMode = 'unsupported'; return; }
    try {
      // Migrate users who had the old /upload-sw.js registered directly. The
      // new /sw.js takes its place — without unregistering, the browser would
      // keep two workers at the same scope (the new one wins eventually but
      // the old one can intercept fetches in the meantime).
      try {
        var existing = await navigator.serviceWorker.getRegistrations();
        for (var i = 0; i < existing.length; i++) {
          var reg = existing[i];
          var url = (reg.active && reg.active.scriptURL) || (reg.waiting && reg.waiting.scriptURL) || (reg.installing && reg.installing.scriptURL) || '';
          if (url && url.indexOf(LEGACY_SW_URL) !== -1 && url.indexOf(SW_URL) === -1) {
            await reg.unregister();
          }
        }
      } catch (e) { /* non-fatal */ }
      _swRegistration = await navigator.serviceWorker.register(SW_URL, { scope: '/', updateViaCache: 'none' });
      // Wait for active worker (handles first-install case)
      if (!_swRegistration.active) {
        await new Promise(function(r) {
          var t = setTimeout(r, 3000);
          var sw = _swRegistration.installing || _swRegistration.waiting;
          if (sw) {
            sw.addEventListener('statechange', function() {
              if (sw.state === 'activated') { clearTimeout(t); r(); }
            });
          } else {
            clearTimeout(t); r();
          }
        });
      }
      var ok = await pingSW(2500);
      _swMode = ok ? 'active' : 'failed';
      if (ok) {
        var target = getActiveSW();
        if (target) target.postMessage({ type: 'LIST_JOBS' });
      }
    } catch (e) {
      console.warn('[Upload] SW registration failed', e);
      _swMode = 'failed';
    }
  }

  function initChannel() {
    try {
      _channel = new BroadcastChannel(CHANNEL_NAME);
      _channel.addEventListener('message', onBroadcast);
    } catch (e) {
      console.warn('[Upload] BroadcastChannel init failed', e);
    }
    // Also listen to direct SW messages (for LIST_JOBS reply)
    if ('serviceWorker' in navigator) {
      navigator.serviceWorker.addEventListener('message', function(e) {
        var msg = e.data || {};
        if (msg.type === 'JOBS_LIST' && Array.isArray(msg.jobs)) {
          msg.jobs.forEach(function(j) {
            if (j.status !== 'done' && j.status !== 'aborted') {
              _activeJobs.set(j.id, j);
            }
          });
          renderBanner();
        }
      });
    }
  }

  function onBroadcast(e) {
    var msg = e.data || {};
    if (msg.type === 'PROGRESS') {
      _activeJobs.set(msg.uploadId, msg);
      renderBanner();
    } else if (msg.type === 'DONE') {
      _activeJobs.delete(msg.uploadId);
      showDoneState(msg);
    } else if (msg.type === 'ERROR') {
      _activeJobs.delete(msg.uploadId);
      showErrorState(msg);
    } else if (msg.type === 'ABORTED') {
      _activeJobs.delete(msg.uploadId);
      renderBanner();
    }
  }

  function renderBanner() {
    var banner = document.getElementById(BANNER_ID);
    if (!banner) return;
    if (_activeJobs.size === 0) {
      banner.style.display = 'none';
      return;
    }
    // Take the first active job for the compact banner
    var job = _activeJobs.values().next().value;
    banner.style.display = 'block';

    var titleEl = document.getElementById('bgUploadTitle');
    var pctEl = document.getElementById('bgUploadPct');
    var fillEl = document.getElementById('bgUploadFill');
    var statusEl = document.getElementById('bgUploadStatus');

    if (titleEl) {
      var n = _activeJobs.size;
      var label = (job.title || job.fileName || 'video');
      if (label.length > 30) label = label.slice(0, 30) + '...';
      titleEl.textContent = (n > 1 ? '[' + n + '] ' : '') + 'Uploading: ' + label;
    }
    if (pctEl) pctEl.textContent = (job.pct || 0) + '%';
    if (fillEl) {
      fillEl.style.width = (job.pct || 0) + '%';
      fillEl.style.background = 'var(--accent, #f48c25)';
    }
    if (statusEl) {
      if (job.partsTotal > 1) {
        statusEl.textContent = '(' + (job.partsDone || 0) + '/' + job.partsTotal + ' parts done)';
      } else {
        statusEl.textContent = '';
      }
    }
  }

  function showDoneState(msg) {
    var banner = document.getElementById(BANNER_ID);
    if (!banner) return;
    banner.style.display = 'block';
    var titleEl = document.getElementById('bgUploadTitle');
    var pctEl = document.getElementById('bgUploadPct');
    var fillEl = document.getElementById('bgUploadFill');
    var statusEl = document.getElementById('bgUploadStatus');
    if (titleEl) {
      var label = msg.title || 'video';
      if (label.length > 30) label = label.slice(0, 30) + '...';
      titleEl.textContent = 'Done: ' + label;
    }
    if (pctEl) pctEl.textContent = '';
    if (fillEl) { fillEl.style.width = '100%'; fillEl.style.background = '#22c55e'; }
    if (statusEl) statusEl.textContent = 'Upload complete!';
    setTimeout(function() {
      if (_activeJobs.size === 0) banner.style.display = 'none';
      if (fillEl) fillEl.style.background = 'var(--accent, #f48c25)';
    }, 4000);
    // If we're on the scouting page and loadVideos exists, refresh the grid
    if (typeof window.loadVideos === 'function') {
      try { window.loadVideos(); } catch(e) {}
    }
  }

  function showErrorState(msg) {
    var banner = document.getElementById(BANNER_ID);
    if (!banner) return;
    banner.style.display = 'block';
    var titleEl = document.getElementById('bgUploadTitle');
    var pctEl = document.getElementById('bgUploadPct');
    var fillEl = document.getElementById('bgUploadFill');
    var statusEl = document.getElementById('bgUploadStatus');
    if (titleEl) titleEl.textContent = 'Upload failed';
    if (pctEl) pctEl.textContent = '';
    if (fillEl) fillEl.style.background = '#ef4444';
    if (statusEl) statusEl.textContent = msg.error || 'Unknown error';
    setTimeout(function() {
      if (_activeJobs.size === 0) banner.style.display = 'none';
      if (fillEl) fillEl.style.background = 'var(--accent, #f48c25)';
    }, 6000);
  }

  // ── Public API ─────────────────────────────────────────────
  async function enqueue(file, meta) {
    if (_swMode !== 'active') throw new Error('Service worker not active');
    var target = getActiveSW();
    if (!target) throw new Error('No active SW');

    var uploadId = (self.crypto && self.crypto.randomUUID) ? self.crypto.randomUUID()
                   : 'u-' + Date.now() + '-' + Math.random().toString(36).slice(2, 10);

    var job = {
      id: uploadId,
      status: 'pending',
      fileName: file.name,
      fileSize: file.size,
      contentType: file.type || 'video/mp4',
      meta: meta || {},
      completedParts: [],
      partsTotal: 0,
      bytesUploaded: 0,
      createdAt: Date.now()
    };

    await self.NextPlayUploadIDB.putBlob(uploadId, file);
    await self.NextPlayUploadIDB.putJob(job);

    // Seed the banner immediately for instant feedback
    _activeJobs.set(uploadId, {
      uploadId: uploadId, pct: 0, partsDone: 0, partsTotal: 1,
      title: (meta && meta.title) || file.name, fileName: file.name
    });
    renderBanner();

    target.postMessage({ type: 'ENQUEUE', uploadId: uploadId });
    return uploadId;
  }

  function cancelAll() {
    var target = getActiveSW();
    if (!target) return;
    _activeJobs.forEach(function(_, id) {
      target.postMessage({ type: 'CANCEL', uploadId: id });
    });
    _activeJobs.clear();
    renderBanner();
  }

  function cancel(uploadId) {
    var target = getActiveSW();
    if (!target) return;
    target.postMessage({ type: 'CANCEL', uploadId: uploadId });
    _activeJobs.delete(uploadId);
    renderBanner();
  }

  window.NextPlayUpload = {
    hasSWSupport: function() { return _swMode === 'active'; },
    isSWActive: function() { return _swMode === 'active' && _activeJobs.size > 0; },
    getMode: function() { return _swMode; },
    ready: function() { return _readyPromise; },
    enqueue: enqueue,
    cancel: cancel,
    cancelAll: cancelAll,
    activeCount: function() { return _activeJobs.size; }
  };

  // Init on DOM ready
  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', boot);
  } else {
    boot();
  }

  async function boot() {
    initChannel();
    await initSW();
    console.log('[Upload] mode:', _swMode);
    _readyResolve(_swMode);
  }
})();
