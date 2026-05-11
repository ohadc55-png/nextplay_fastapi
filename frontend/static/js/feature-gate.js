/* ══════════ Feature Gate ════════════════════════════════════════════
   Blocks access to coach features (Notebook / Team Setup / Chat /
   Video Hub / Play Creator / Data Upload …) when the coach hasn't
   added a team yet. Surfaced as a centered modal so the next action
   is obvious.

   The `<meta name="np-has-team" content="true|false">` tag is set
   by base.html from the `profile` context. When false we intercept
   navigation to any link whose `href` matches a protected route AND
   any in-page button explicitly tagged `data-requires-team`.
   ═════════════════════════════════════════════════════════════════ */
(function () {
  function hasTeam() {
    var m = document.querySelector('meta[name="np-has-team"]');
    return m && m.content === 'true';
  }

  // Coach-app routes that all require an active team. Keep in sync with
  // the page handlers in src/api/pages.py — anything that ends up
  // rendering home.html when profile is null belongs here.
  var PROTECTED_PATHS = [
    '/notebook',
    '/team-setup',
    '/data-upload',
    '/scouting',
    '/scouting-video',
    '/video-hub',
    '/plays',
    '/play-creator',
    '/chat',
    '/history',
    '/notebook/',
    '/scouting/',
    '/plays/',
  ];

  function isProtectedHref(href) {
    if (!href) return false;
    try {
      var u = new URL(href, window.location.origin);
      if (u.origin !== window.location.origin) return false;
      var p = u.pathname.replace(/\/+$/, '');
      for (var i = 0; i < PROTECTED_PATHS.length; i++) {
        var pp = PROTECTED_PATHS[i].replace(/\/+$/, '');
        if (p === pp || p.indexOf(pp + '/') === 0) return true;
      }
      return false;
    } catch (e) { return false; }
  }

  function buildModal() {
    if (document.getElementById('noTeamModal')) return;

    var overlay = document.createElement('div');
    overlay.id = 'noTeamModal';
    overlay.className = 'no-team-overlay';
    overlay.setAttribute('role', 'dialog');
    overlay.setAttribute('aria-modal', 'true');
    overlay.setAttribute('aria-labelledby', 'noTeamTitle');

    var box = document.createElement('div');
    box.className = 'no-team-box';

    var icon = document.createElement('div');
    icon.className = 'no-team-icon';
    icon.textContent = '🏀';

    var h = document.createElement('h2');
    h.id = 'noTeamTitle';
    h.className = 'no-team-title';
    h.textContent = 'Add a team first';

    var p = document.createElement('p');
    p.className = 'no-team-text';
    p.textContent =
      "Your AI coaching staff needs a roster to work with. " +
      "Add your team's name, league, and a few players — then every " +
      "feature unlocks. Takes under a minute.";

    var actions = document.createElement('div');
    actions.className = 'no-team-actions';

    var primary = document.createElement('a');
    primary.className = 'btn btn-primary';
    primary.href = '/team-setup';
    primary.textContent = 'Add Team';
    primary.onclick = function () {
      // /team-setup is itself protected; the modal must NOT trigger
      // again. Mark a one-shot bypass via sessionStorage that the
      // click handler reads to let this navigation through.
      sessionStorage.setItem('np_allow_team_setup', '1');
    };

    var secondary = document.createElement('button');
    secondary.type = 'button';
    secondary.className = 'btn btn-ghost';
    secondary.textContent = 'Not now';
    secondary.onclick = closeModal;

    actions.appendChild(primary);
    actions.appendChild(secondary);

    box.appendChild(icon);
    box.appendChild(h);
    box.appendChild(p);
    box.appendChild(actions);
    overlay.appendChild(box);
    document.body.appendChild(overlay);

    overlay.addEventListener('click', function (e) {
      if (e.target === overlay) closeModal();
    });
    document.addEventListener('keydown', _escClose);
  }

  function _escClose(e) {
    if (e.key === 'Escape') closeModal();
  }

  function openModal() {
    buildModal();
    var el = document.getElementById('noTeamModal');
    if (el) el.classList.add('open');
    document.body.style.overflow = 'hidden';
  }

  function closeModal() {
    var el = document.getElementById('noTeamModal');
    if (el) el.classList.remove('open');
    document.body.style.overflow = '';
  }

  // Capture clicks before the browser navigates. Bubble phase is fine
  // because we only block in-app SPA links / sidebar links — no need
  // for capture-phase interception.
  function onClick(e) {
    if (hasTeam()) return;

    // Allow the user to actually reach the team-setup page (otherwise
    // they could never escape).
    var a = e.target.closest && e.target.closest('a[href]');
    if (a) {
      var href = a.getAttribute('href');
      if (isProtectedHref(href)) {
        if (href.indexOf('/team-setup') === 0) {
          sessionStorage.setItem('np_allow_team_setup', '1');
          return;
        }
        e.preventDefault();
        e.stopPropagation();
        openModal();
      }
      return;
    }
    // Buttons opted-in via data-requires-team="true"
    var btn = e.target.closest && e.target.closest('[data-requires-team="true"]');
    if (btn) {
      e.preventDefault();
      e.stopPropagation();
      openModal();
    }
  }

  // If we landed on a protected route directly without a team — the
  // server already redirected us to home.html, so we don't open the
  // modal automatically. The user only sees the modal when they
  // actively click something protected.

  document.addEventListener('DOMContentLoaded', function () {
    document.addEventListener('click', onClick, false);
  });

  // Expose for debugging + for the spotlight script to share state.
  window.NpFeatureGate = { hasTeam: hasTeam, open: openModal, close: closeModal };
})();
