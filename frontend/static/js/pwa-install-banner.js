/* ═══════════════ NEXTPLAY in-page install button manager ═══════════════
 * Persistent install entry point — rendered as part of the page (NOT a
 * floating FAB), so it stays integrated with the layout and survives every
 * navigation. Auto-binds to any element with `data-pwa-install` attribute.
 *
 * Click behavior, in order of priority:
 *   1. Standalone mode (we're already INSIDE the installed app) →
 *      "✓ You're already using the app" toast. The button stays visible
 *      so coaches don't think it broke or vanished.
 *   2. beforeinstallprompt captured by pwa-register.js (Chromium browsers) →
 *      fire the native install prompt.
 *   3. iOS Safari → show a how-to modal (Apple has no JS install API).
 *   4. None of the above → show a friendly fallback modal explaining the
 *      app may already be installed (look in apps), or that this browser
 *      doesn't support PWA install (use Chrome / Edge / Safari instead).
 *
 * No innerHTML — all DOM built via createElement / createElementNS so any
 * future text additions are XSS-safe by construction.
 * ════════════════════════════════════════════════════════════════════ */
(function () {
  'use strict';

  var SVG_NS = 'http://www.w3.org/2000/svg';
  var INSTALL_ATTR = 'data-pwa-install';
  var MODAL_ID = 'nextplay-pwa-install-modal';
  var TOAST_ID = 'nextplay-pwa-install-toast';

  // ─── Environment detection ─────────────────────────────────────
  function isStandalone() {
    return (
      window.matchMedia('(display-mode: standalone)').matches ||
      window.matchMedia('(display-mode: fullscreen)').matches ||
      window.navigator.standalone === true
    );
  }

  function isIOS() {
    var ua = navigator.userAgent || '';
    return /iPad|iPhone|iPod/.test(ua) ||
           (navigator.platform === 'MacIntel' && navigator.maxTouchPoints > 1);
  }

  function isIOSSafari() {
    if (!isIOS()) return false;
    var ua = navigator.userAgent || '';
    if (/CriOS|FxiOS|EdgiOS|GSA|FBAN|FBAV|Instagram|Line\//i.test(ua)) return false;
    return /Safari/i.test(ua);
  }

  // ─── SVG helpers (no innerHTML) ────────────────────────────────
  function svg(attrs) {
    var el = document.createElementNS(SVG_NS, 'svg');
    setAttrs(el, attrs);
    return el;
  }
  function svgEl(name, attrs) {
    var el = document.createElementNS(SVG_NS, name);
    setAttrs(el, attrs);
    return el;
  }
  function setAttrs(el, attrs) {
    for (var k in attrs) {
      if (Object.prototype.hasOwnProperty.call(attrs, k)) {
        el.setAttribute(k, attrs[k]);
      }
    }
  }

  function shareIcon() {
    var s = svg({
      width: '14', height: '14', viewBox: '0 0 24 24', fill: 'none',
      stroke: 'currentColor', 'stroke-width': '2',
      'stroke-linecap': 'round', 'stroke-linejoin': 'round',
      style: 'vertical-align:middle;margin-left:4px',
    });
    s.appendChild(svgEl('path', { d: 'M4 12v8a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2v-8' }));
    s.appendChild(svgEl('polyline', { points: '16 6 12 2 8 6' }));
    s.appendChild(svgEl('line', { x1: '12', y1: '2', x2: '12', y2: '15' }));
    return s;
  }

  // ─── Toast (small bottom-center popup) ─────────────────────────
  function showToast(text) {
    var existing = document.getElementById(TOAST_ID);
    if (existing && existing.parentNode) existing.parentNode.removeChild(existing);

    var t = document.createElement('div');
    t.id = TOAST_ID;
    t.setAttribute('role', 'status');
    Object.assign(t.style, {
      position: 'fixed', left: '50%', bottom: '32px',
      transform: 'translateX(-50%)',
      background: '#151c2a',
      color: '#f0f3f6',
      border: '1px solid rgba(74,222,128,0.35)',
      borderRadius: '12px',
      padding: '14px 22px',
      fontSize: '14px', fontWeight: '600',
      fontFamily: '-apple-system,BlinkMacSystemFont,Segoe UI,Roboto,Helvetica,Arial,sans-serif',
      boxShadow: '0 10px 30px rgba(0,0,0,0.5)',
      zIndex: '99999',
      maxWidth: '420px',
      textAlign: 'center',
    });
    t.textContent = text;
    document.body.appendChild(t);
    setTimeout(function () {
      if (t && t.parentNode) t.parentNode.removeChild(t);
    }, 3500);
  }

  // ─── Modal builder ────────────────────────────────────────────
  function buildStep(num, parts) {
    var row = document.createElement('div');
    row.style.cssText = 'display:flex;gap:12px;align-items:center;margin-bottom:12px;';
    var badge = document.createElement('div');
    badge.style.cssText =
      'background:rgba(255,107,53,0.15);color:#ff6b35;width:32px;height:32px;' +
      'border-radius:8px;display:flex;align-items:center;justify-content:center;' +
      'font-weight:800;flex-shrink:0';
    badge.textContent = String(num);
    var copy = document.createElement('div');
    parts.forEach(function (p) {
      if (p == null) return;
      if (typeof p === 'string') copy.appendChild(document.createTextNode(p));
      else if (p === '_share') copy.appendChild(shareIcon());
      else { // {bold: '...'}
        var s = document.createElement('strong');
        s.style.color = '#ff6b35';
        s.textContent = p.bold;
        copy.appendChild(s);
      }
    });
    row.appendChild(badge);
    row.appendChild(copy);
    return row;
  }

  function showModal(title, sub, steps) {
    var existing = document.getElementById(MODAL_ID);
    if (existing && existing.parentNode) existing.parentNode.removeChild(existing);

    var backdrop = document.createElement('div');
    backdrop.id = MODAL_ID;
    Object.assign(backdrop.style, {
      position: 'fixed', inset: '0',
      background: 'rgba(10,14,20,0.7)',
      backdropFilter: 'blur(4px)',
      display: 'flex', alignItems: 'flex-end', justifyContent: 'center',
      zIndex: '99999', padding: '16px',
    });

    var card = document.createElement('div');
    Object.assign(card.style, {
      background: '#151c2a',
      border: '1px solid rgba(255,107,53,0.3)',
      borderRadius: '18px',
      padding: '20px 22px 24px',
      maxWidth: '420px', width: '100%',
      color: '#f0f3f6',
      fontFamily: '-apple-system,BlinkMacSystemFont,Segoe UI,Roboto,Helvetica,Arial,sans-serif',
      fontSize: '15px', lineHeight: '1.6',
      boxShadow: '0 -10px 40px rgba(0,0,0,0.6)',
      marginBottom: '24px',
    });

    var titleEl = document.createElement('div');
    titleEl.textContent = title;
    Object.assign(titleEl.style, {
      fontSize: '18px', fontWeight: '800', marginBottom: '6px', color: '#ffffff',
    });

    var subEl = document.createElement('div');
    subEl.textContent = sub;
    Object.assign(subEl.style, { color: '#9ba4b0', fontSize: '13px', marginBottom: '18px' });

    card.appendChild(titleEl);
    card.appendChild(subEl);
    (steps || []).forEach(function (s, i) {
      card.appendChild(buildStep(i + 1, s));
    });

    var closeBtn = document.createElement('button');
    closeBtn.type = 'button';
    closeBtn.textContent = 'Got it';
    Object.assign(closeBtn.style, {
      background: '#ff6b35', color: '#ffffff', border: '0',
      padding: '12px 18px', borderRadius: '10px', cursor: 'pointer',
      fontSize: '14px', fontWeight: '700', width: '100%', marginTop: '6px',
    });
    closeBtn.addEventListener('click', function () {
      var m = document.getElementById(MODAL_ID);
      if (m && m.parentNode) m.parentNode.removeChild(m);
    });
    backdrop.addEventListener('click', function (e) {
      if (e.target === backdrop) closeBtn.click();
    });
    card.appendChild(closeBtn);
    backdrop.appendChild(card);
    document.body.appendChild(backdrop);
  }

  // ─── Click handler — the brain ─────────────────────────────────
  async function handleInstallClick(triggerEl) {
    // 1. Already running INSIDE the installed app (standalone display mode).
    if (isStandalone()) {
      showToast('✓ You\'re using the installed app right now');
      return;
    }

    // 2. Native install prompt available (Chromium desktop + Android).
    if (window.NextPlayPWA && window.NextPlayPWA.canInstall) {
      try {
        if (triggerEl) triggerEl.disabled = true;
        var r = await window.NextPlayPWA.promptInstall();
        if (triggerEl) triggerEl.disabled = false;
        if (r && r.outcome === 'accepted') {
          showToast('✓ Installed! Look for NEXTPLAY in your apps.');
          markAllInstalled();
        }
        return;
      } catch (e) {
        if (triggerEl) triggerEl.disabled = false;
      }
    }

    // 3. iOS Safari — manual Add to Home Screen instructions.
    if (isIOSSafari()) {
      showModal(
        'Install NEXTPLAY on your iPhone',
        'Two taps and you have a real app icon on your home screen.',
        [
          ['Tap the ', { bold: 'Share' }, ' button at the bottom of Safari ', '_share'],
          ['Choose ', { bold: 'Add to Home Screen' }, ' from the menu'],
        ]
      );
      return;
    }

    // 4. Anything else — likely already installed (browser stops firing the
    //    install prompt once the app is on the device) OR this browser
    //    can't install PWAs at all.
    showModal(
      'Looks like you may already have it',
      'NEXTPLAY may already be installed on this device. If not, here\'s how:',
      [
        ['Look for ', { bold: 'NEXTPLAY' }, ' in your apps / home screen'],
        ['Or open this page in ', { bold: 'Chrome, Edge, or Safari (iOS 16.4+)' }, ' and try again'],
      ]
    );
  }

  // ─── Button state management ──────────────────────────────────
  function findInstallButtons() {
    return document.querySelectorAll('[' + INSTALL_ATTR + ']');
  }

  function bindButton(btn) {
    if (btn.__nextplayBound) return;
    btn.__nextplayBound = true;
    btn.addEventListener('click', function (e) {
      e.preventDefault();
      handleInstallClick(btn);
    });
    refreshButtonState(btn);
  }

  function refreshButtonState(btn) {
    // The label text lives inside the button; we update the [data-pwa-label]
    // child if present, else fall back to setting button textContent.
    var label = btn.querySelector('[data-pwa-label]') || btn;
    var standalone = isStandalone();
    var canInstall = !!(window.NextPlayPWA && window.NextPlayPWA.canInstall);

    if (standalone) {
      label.textContent = '✓ App installed';
      btn.setAttribute('aria-label', 'NEXTPLAY is installed on this device');
      btn.style.opacity = '0.85';
      return;
    }

    // For the in-page button we always show "Install App" regardless of
    // canInstall — coaches can still tap it to get instructions or to
    // discover it's already installed (per the user spec).
    var defaultLabel = btn.getAttribute('data-pwa-label-text') || 'Install App';
    label.textContent = defaultLabel;
    btn.style.opacity = '1';
    btn.disabled = false;
  }

  function markAllInstalled() {
    findInstallButtons().forEach(refreshButtonState);
  }

  function bindAll() {
    findInstallButtons().forEach(bindButton);
  }

  function init() {
    bindAll();
    // Re-bind for buttons added after initial load (single-page navigation,
    // dynamic UI, modals).
    var mo = new MutationObserver(bindAll);
    mo.observe(document.documentElement, { childList: true, subtree: true });

    // Re-evaluate state when install becomes possible / fires / completes.
    window.addEventListener('nextplay:installable', markAllInstalled);
    window.addEventListener('nextplay:installed', markAllInstalled);
    var mq = window.matchMedia('(display-mode: standalone)');
    if (mq.addEventListener) mq.addEventListener('change', markAllInstalled);
    else if (mq.addListener) mq.addListener(markAllInstalled);
  }

  // Public API for code that wants to invoke install programmatically.
  window.NextPlayPWA = window.NextPlayPWA || {};
  window.NextPlayPWA.handleInstallClick = handleInstallClick;
  window.NextPlayPWA.refreshInstallButtons = function () {
    findInstallButtons().forEach(refreshButtonState);
  };

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', init);
  } else {
    init();
  }
})();
