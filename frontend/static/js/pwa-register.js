/* ═══════════════ NEXTPLAY PWA registration ═══════════════
 * Lightweight wrapper around navigator.serviceWorker.register('/sw.js').
 * Loaded UNCONDITIONALLY by base.html (logged in or out) so:
 *   - Public landing pages can show "Install app" prompts.
 *   - Logged-in pages get the SW too (upload-client.js's own register call
 *     is then idempotent — returns the same registration).
 *
 * Also captures the `beforeinstallprompt` event for desktop/Android install
 * triggers — exposed as window.NextPlayPWA.promptInstall().
 *
 * Does NOT touch upload behavior, push permissions, or anything user-facing.
 * Push subscribe is a separate, opt-in flow (push-subscribe.js).
 * ═════════════════════════════════════════════════════════ */
(function () {
  'use strict';

  function isSecureOrLocal() {
    return (
      window.isSecureContext ||
      location.hostname === 'localhost' ||
      location.hostname === '127.0.0.1'
    );
  }

  if (!('serviceWorker' in navigator) || !isSecureOrLocal()) {
    return; // PWA needs HTTPS/localhost. Old browsers fall back to plain web.
  }

  // Migrate users who had the old /upload-sw.js as their SW. The new /sw.js
  // takes its place; without unregister, the browser can keep two workers at
  // the same scope and the old one may intercept fetches.
  async function migrateLegacySW() {
    try {
      var regs = await navigator.serviceWorker.getRegistrations();
      for (var i = 0; i < regs.length; i++) {
        var reg = regs[i];
        var url =
          (reg.active && reg.active.scriptURL) ||
          (reg.waiting && reg.waiting.scriptURL) ||
          (reg.installing && reg.installing.scriptURL) ||
          '';
        if (url.indexOf('/upload-sw.js') !== -1 && url.indexOf('/sw.js') === -1) {
          await reg.unregister();
        }
      }
    } catch (e) {
      /* non-fatal */
    }
  }

  async function register() {
    try {
      await migrateLegacySW();
      var reg = await navigator.serviceWorker.register('/sw.js', {
        scope: '/',
        updateViaCache: 'none',
      });
      window.NextPlayPWA = window.NextPlayPWA || {};
      window.NextPlayPWA.registration = reg;
    } catch (e) {
      console.warn('[PWA] SW register failed', e);
    }
  }

  // Defer to after first paint so we don't compete with critical render.
  if (document.readyState === 'complete') {
    register();
  } else {
    window.addEventListener('load', register);
  }

  // Capture install prompt for later trigger by UI (e.g., a button).
  var _deferredInstall = null;
  window.addEventListener('beforeinstallprompt', function (e) {
    e.preventDefault();
    _deferredInstall = e;
    window.NextPlayPWA = window.NextPlayPWA || {};
    window.NextPlayPWA.canInstall = true;
    // Fire a custom event so UI can show an Install button.
    window.dispatchEvent(new CustomEvent('nextplay:installable'));
  });

  window.addEventListener('appinstalled', function () {
    _deferredInstall = null;
    window.NextPlayPWA = window.NextPlayPWA || {};
    window.NextPlayPWA.canInstall = false;
    window.dispatchEvent(new CustomEvent('nextplay:installed'));
  });

  window.NextPlayPWA = Object.assign(window.NextPlayPWA || {}, {
    promptInstall: async function () {
      if (!_deferredInstall) return { outcome: 'unavailable' };
      _deferredInstall.prompt();
      var choice = await _deferredInstall.userChoice;
      _deferredInstall = null;
      window.NextPlayPWA.canInstall = false;
      return choice;
    },
    isStandalone: function () {
      return (
        window.matchMedia('(display-mode: standalone)').matches ||
        window.navigator.standalone === true
      );
    },
  });
})();
