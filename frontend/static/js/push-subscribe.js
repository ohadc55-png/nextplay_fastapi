/* ═══════════════ NEXTPLAY push subscription helper ═══════════════
 * Wraps Notification + PushManager + the VAPID handshake.
 * Loaded by base.html for ALL logged-in users so any page can
 * trigger subscribe (settings page is the primary caller today).
 *
 * Public API on window.NextPlayPush:
 *   - status()           → Promise<{supported, permission, subscribed, configured}>
 *   - subscribe()        → Promise<{ok, error?}>
 *   - unsubscribe()      → Promise<{ok}>
 *   - sendTestPush()     → Promise<{status, sent_count, reason?}>
 *
 * The status object tells the settings UI which state to render:
 *   supported=false   → hide the whole section ("Your browser doesn't support push")
 *   configured=false  → "Push not yet configured by NEXTPLAY" (VAPID keys missing on server)
 *   permission=denied → "You denied permission — undo it in browser settings"
 *   subscribed=true   → green badge + Send Test button + Disable button
 *   else              → orange Enable button
 * ════════════════════════════════════════════════════════════════════ */
(function () {
  'use strict';

  function isSecureOrLocal() {
    return (
      window.isSecureContext ||
      location.hostname === 'localhost' ||
      location.hostname === '127.0.0.1'
    );
  }

  function supportsPush() {
    return (
      'serviceWorker' in navigator &&
      'PushManager' in window &&
      'Notification' in window &&
      isSecureOrLocal()
    );
  }

  // VAPID public keys are URL-base64-encoded; PushManager wants a Uint8Array.
  function urlBase64ToUint8Array(b64) {
    var pad = '='.repeat((4 - (b64.length % 4)) % 4);
    var base64 = (b64 + pad).replace(/-/g, '+').replace(/_/g, '/');
    var raw = atob(base64);
    var arr = new Uint8Array(raw.length);
    for (var i = 0; i < raw.length; i++) arr[i] = raw.charCodeAt(i);
    return arr;
  }

  async function getVapidKey() {
    var resp = await fetch('/api/push/vapid-key', { credentials: 'same-origin' });
    if (!resp.ok) return { key: '', configured: false };
    return await resp.json();
  }

  async function getRegistration() {
    // Wait for the SW that pwa-register.js / upload-client.js installed.
    if (navigator.serviceWorker.controller) {
      return await navigator.serviceWorker.ready;
    }
    // ServiceWorker hasn't taken control yet — find the registration explicitly.
    var regs = await navigator.serviceWorker.getRegistrations();
    for (var i = 0; i < regs.length; i++) {
      var r = regs[i];
      var url = (r.active && r.active.scriptURL) || '';
      if (url.indexOf('/sw.js') !== -1) return r;
    }
    return null;
  }

  async function status() {
    if (!supportsPush()) {
      return { supported: false, permission: 'unsupported', subscribed: false, configured: false };
    }
    var vapid = await getVapidKey();
    var reg = await getRegistration();
    var sub = reg ? await reg.pushManager.getSubscription() : null;
    return {
      supported: true,
      permission: Notification.permission, // 'default' | 'granted' | 'denied'
      subscribed: !!sub,
      configured: !!vapid.configured,
      vapidKey: vapid.key,
    };
  }

  async function subscribe() {
    if (!supportsPush()) return { ok: false, error: 'Browser does not support push.' };

    var vapid = await getVapidKey();
    if (!vapid.configured || !vapid.key) {
      return { ok: false, error: 'Push not yet configured on the server. Try again later.' };
    }

    // Ask permission. If user previously denied, this returns 'denied' immediately
    // — they must clear it from browser settings, we can't re-prompt.
    var perm = await Notification.requestPermission();
    if (perm !== 'granted') {
      return { ok: false, error: 'Permission ' + perm };
    }

    var reg = await getRegistration();
    if (!reg) return { ok: false, error: 'Service worker not ready yet — refresh and try again.' };

    // Reuse existing subscription if present (same keys), otherwise create one.
    var sub = await reg.pushManager.getSubscription();
    if (!sub) {
      try {
        sub = await reg.pushManager.subscribe({
          userVisibleOnly: true,
          applicationServerKey: urlBase64ToUint8Array(vapid.key),
        });
      } catch (e) {
        return { ok: false, error: 'Subscribe failed: ' + (e && e.message) };
      }
    }

    var json = sub.toJSON ? sub.toJSON() : JSON.parse(JSON.stringify(sub));
    var tz = '';
    try {
      tz = Intl.DateTimeFormat().resolvedOptions().timeZone || '';
    } catch (e) {}

    // Persist to backend.
    var resp = await fetch('/api/push/subscribe', {
      method: 'POST',
      credentials: 'same-origin',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        endpoint: json.endpoint,
        keys: json.keys || {},
        timezone: tz,
      }),
    });
    if (!resp.ok) {
      var msg = '';
      try {
        var err = await resp.json();
        msg = err.error || ('HTTP ' + resp.status);
      } catch (e) {
        msg = 'HTTP ' + resp.status;
      }
      return { ok: false, error: msg };
    }
    return { ok: true };
  }

  async function unsubscribe() {
    var reg = await getRegistration();
    var endpoint = '';
    if (reg) {
      var sub = await reg.pushManager.getSubscription();
      if (sub) {
        endpoint = sub.endpoint;
        try { await sub.unsubscribe(); } catch (e) { /* ignore */ }
      }
    }
    await fetch('/api/push/unsubscribe', {
      method: 'POST',
      credentials: 'same-origin',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ endpoint: endpoint }),
    });
    return { ok: true };
  }

  async function sendTestPush() {
    var resp = await fetch('/api/push/test', {
      method: 'POST',
      credentials: 'same-origin',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({}),
    });
    return await resp.json();
  }

  async function savePreferences(prefs) {
    var resp = await fetch('/api/push/preferences', {
      method: 'POST',
      credentials: 'same-origin',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(prefs || {}),
    });
    return await resp.json();
  }

  async function loadPreferences() {
    var resp = await fetch('/api/push/preferences', { credentials: 'same-origin' });
    if (!resp.ok) return null;
    return await resp.json();
  }

  window.NextPlayPush = {
    supported: supportsPush,
    status: status,
    subscribe: subscribe,
    unsubscribe: unsubscribe,
    sendTestPush: sendTestPush,
    savePreferences: savePreferences,
    loadPreferences: loadPreferences,
  };
})();
