/* ══════════ Welcome Spotlight ═════════════════════════════════════
   First-time-ever entry: dim the whole UI, push Daisy front-and-
   centre, and have her deliver the welcome speech personally.

   Triggers ONCE per coach (keyed by user_id so a different account
   on the same browser starts fresh). Persists via localStorage
   `np_welcomed_<user_id>` so logouts/logins by the same coach
   don't re-trigger it.

   Reads:
     <meta name="np-user-id">       int — required for keying
     <meta name="np-user-name">     str — used in the greeting

   Depends on:
     guide-widget.js → window.NicoWidget.init() must run first so
     the toggle button + panel exist in the DOM.
   ═════════════════════════════════════════════════════════════════ */
(function () {
  function $meta(name) {
    var m = document.querySelector('meta[name="' + name + '"]');
    return m ? m.content : '';
  }

  function _alreadyWelcomed(userId) {
    if (!userId) return true; // anonymous → never trigger
    return localStorage.getItem('np_welcomed_' + userId) === '1';
  }

  function _markWelcomed(userId) {
    if (userId) localStorage.setItem('np_welcomed_' + userId, '1');
  }

  function _buildSpotlight() {
    if (document.getElementById('npWelcomeSpotlight')) return;

    var overlay = document.createElement('div');
    overlay.id = 'npWelcomeSpotlight';
    overlay.className = 'np-welcome-overlay';
    overlay.setAttribute('aria-hidden', 'true');
    document.body.appendChild(overlay);
  }

  function _clearChildren(el) {
    while (el.firstChild) el.removeChild(el.firstChild);
  }

  function _typewriter(el, text, speed) {
    return new Promise(function (resolve) {
      var i = 0;
      function step() {
        if (i >= text.length) { resolve(); return; }
        el.appendChild(document.createTextNode(text.charAt(i++)));
        setTimeout(step, speed);
      }
      step();
    });
  }

  async function _runIntro(coachName) {
    if (!window.NicoWidget) return;
    var panel = document.getElementById('nicoPanel');
    var toggle = document.getElementById('nicoToggle');
    if (toggle && panel && !panel.classList.contains('open')) {
      toggle.click();
    }

    var msgsEl = document.querySelector('#nicoPanel .nico-messages');
    if (!msgsEl) return;

    // Clear the default welcome and stream the personal greeting.
    _clearChildren(msgsEl);

    var greeting = document.createElement('div');
    greeting.className = 'nico-msg assistant';
    greeting.dir = 'ltr';
    msgsEl.appendChild(greeting);

    var p1 = document.createElement('p');
    greeting.appendChild(p1);

    var headline = (coachName ? 'Hey ' + coachName : 'Hey coach') + ' — welcome to NEXTPLAY!';
    await _typewriter(p1, headline, 18);

    var p2 = document.createElement('p');
    greeting.appendChild(p2);
    await _typewriter(p2,
      "I'm Daisy. The 5-person AI coaching staff you just met (Brad, Jack, Dr. Nexus, Ed, Duncan) is about to change how you prepare for every practice and every game.",
      12
    );

    var p3 = document.createElement('p');
    greeting.appendChild(p3);
    await _typewriter(p3,
      "But first — they need a team to coach. Hit 'Add Team' to drop in your roster, league, and play style. Everything unlocks the moment you do. Ready?",
      12
    );

    msgsEl.scrollTop = msgsEl.scrollHeight;
  }

  function _start(userId, coachName) {
    _buildSpotlight();
    var overlay = document.getElementById('npWelcomeSpotlight');
    if (overlay) overlay.classList.add('open');
    document.body.classList.add('np-welcome-active');

    setTimeout(function () { _runIntro(coachName); }, 400);

    function dismiss() {
      overlay.classList.remove('open');
      document.body.classList.remove('np-welcome-active');
      _markWelcomed(userId);
      overlay.removeEventListener('click', dismiss);
      document.removeEventListener('keydown', escDismiss);
    }
    function escDismiss(e) { if (e.key === 'Escape') dismiss(); }
    overlay.addEventListener('click', dismiss);
    document.addEventListener('keydown', escDismiss);
  }

  document.addEventListener('DOMContentLoaded', function () {
    var userId = $meta('np-user-id');
    if (!userId) return;
    if (_alreadyWelcomed(userId)) return;
    var coachName = $meta('np-user-name') || '';
    var first = (coachName.split(/\s+/)[0] || '').trim();
    if (first) first = first.charAt(0).toUpperCase() + first.slice(1);

    // Wait a tick so guide-widget.js mounts before we trigger it.
    setTimeout(function () { _start(userId, first); }, 200);
  });
})();
