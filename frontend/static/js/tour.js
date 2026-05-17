/* ══════════ NextPlay Guided Tour System ══════════ */
var NpTour = (function() {

  var AVATAR_URL = '/static/img/agents/daisy_chain.png';
  var GUIDE_NAME = 'Daisy Chain';
  var GUIDE_ROLE = 'Platform Guide';

  var CONFIGS = {
    home: [
      { target: '.agents-grid', title: 'Your AI Coaching Staff', text: 'Meet your five AI specialists. Each one handles a different aspect of coaching — from scouting opponents to designing practice plans. Click any card to start a chat.', position: 'bottom' },
      { target: '.hero-stats', title: 'Team Overview', text: 'A quick snapshot of your team — players, files, sessions, and AI staff. This updates automatically as you build your team.', position: 'bottom' },
      { target: '#sidebarTeamSection', title: 'Team Switcher', text: 'Your active team is shown here. If you coach multiple teams, click to switch between them.', position: 'right' }
    ],
    chat: [
      { target: '#agentButtons', title: 'Your Coaching Staff', text: 'Meet your AI coaching team. Each specialist handles a different area — scouting, analytics, tactics, and training. Tap any agent to talk directly, or let the GM route your question automatically.', position: 'bottom' },
      { target: '#chatInput', title: 'Ask Anything', text: 'Type your question here. You can ask about game strategy, player stats, practice drills — anything coaching-related. The right expert will answer.', position: 'top' },
      { target: '.btn-attach', title: 'Upload Files', text: 'Attach game footage screenshots, stat spreadsheets, or scouting reports. The AI will analyze them and give you actionable insights.', position: 'top' },
      { target: '#chatMessages', title: 'Your Conversations', text: 'All responses appear here. You can rate answers with thumbs up/down to help improve the AI. Start a new session anytime with the button above.', position: 'bottom' }
    ],
    'team-setup': [
      { target: '.page-header', title: 'Team Setup', text: 'This is where you configure your team identity and manage your roster. The AI staff uses this data to give you personalized advice.', position: 'bottom' },
      { target: '.card-header', title: 'Your Roster', text: 'Add players with their positions, physical attributes, strengths, and weaknesses. The more detail you provide, the better the AI advice gets.', position: 'bottom' },
      { target: '#rosterList', title: 'Your Roster', text: 'Click any player to open their profile and adjust their skill metrics. Or describe each player to Brad in chat — he\'ll fill the metrics for you automatically.', position: 'bottom' }
    ],
    'data-upload': [
      { target: '#dropZone', title: 'Upload Zone', text: 'Drag and drop files here, or click to browse. Supports CSV, PDF, TXT, JSON, and XLSX files up to 200MB.', position: 'bottom' },
      { target: '#filesList', title: 'Your Files', text: 'All uploaded files appear here. The AI coaching staff can access these files during chat to analyze stats, read scouting reports, and more.', position: 'bottom' }
    ],
    scouting: [
      { target: '#videoGrid', title: 'Video Library', text: 'Your uploaded videos appear here. Click any video to open it in the analysis view with telestration tools.', position: 'bottom' },
      { target: '#quotaBar', title: 'Storage Usage', text: 'Track your video storage usage. Pro users get 10 GB with options to expand.', position: 'bottom' }
    ],
    'scouting-editor': [
      { target: '#videoContainer', title: 'Video Player', text: 'Your video plays here. Use the timeline below to scrub through footage. You can adjust playback speed and zoom in on specific areas of the court.', position: 'bottom' },
      { target: '#toolsSidebar', title: 'Drawing Tools', text: 'Annotate directly on the video — draw arrows, circles, and add text to highlight plays, movements, or mistakes. Your annotations are saved on the timeline.', position: 'right' },
      { target: '#scissorsBtn', title: 'Create Clips', text: 'This is your most powerful tool. Click here to mark in/out points and cut clips from the full video. Tag each clip by play type — pick & roll, fast break, defense, and more.', position: 'right' },
      { target: '.tags-bar', title: 'Quick Tags', text: 'Tag each clip by play type — pick and roll, fast break, defense, and more. Tags help you filter and organize clips for quick review later.', position: 'bottom' },
      { target: '#clipTimelineStrip', title: 'Your Clips', text: 'All your clips appear here as a visual timeline. Click any clip to jump to it, rate it, or add notes.', position: 'bottom' },
      { target: '.timeline-action-bar', title: 'Export & Share', text: 'Use Compile Video to combine clips into a highlight reel — perfect for pre-game scouting or post-game review. Use Share Clips to generate a link you can send to players, assistants, or parents.', position: 'bottom' }
    ],
    plays: [
      { target: '.pc-toolbar', title: 'Play Controls', text: 'Create new plays, switch between edit and play modes, and save or share your diagrams.', position: 'bottom' },
      { target: '.pc-canvas-wrap', title: 'Court Canvas', text: 'Draw your plays on the court. Drag players to position them, then add actions like passes, screens, and cuts.', position: 'right' },
      { target: '.pc-left-toolbar', title: 'Action Palette', text: 'Choose actions to add — passes, dribbles, screens, cuts, and shots. Each one gets visualized on the court diagram.', position: 'right' }
    ],
    notebook: [
      { target: '.nb-new-btn', title: 'Create Entry', text: 'Start a new notebook entry — practice plans, game summaries, player notes, attendance, and more.', position: 'bottom' },
      { target: '.nb-filters', title: 'Filter & Search', text: 'Filter entries by type and date, or search for specific content. Keep your coaching journal organized.', position: 'bottom' },
      { target: '.nb-timeline', title: 'Entry Timeline', text: 'Your entries appear here in chronological order. Click any entry to view, edit, or delete it.', position: 'bottom' }
    ],
    history: [
      { target: '#searchInput', title: 'Search Conversations', text: 'Search through all your past chats with the coaching staff. Find that play suggestion or stat analysis from last week.', position: 'bottom' },
      { target: '.sessions-list', title: 'Chat Sessions', text: 'Each session is a conversation thread. Click to expand and review the full exchange.', position: 'bottom' }
    ],
    settings: [
      { target: '#prefLanguage', title: 'Language & Style', text: 'Choose your preferred language and how detailed you want the AI responses to be.', position: 'bottom' },
      { target: '#focusAreas', title: 'Coaching Focus', text: 'Tell the AI what areas matter most — defense, transition, player development, etc. The staff will prioritize these.', position: 'bottom' },
      { target: '#customNotes', title: 'Custom Instructions', text: 'Add special notes for the AI, like "Always suggest drills for a small gym" or "Focus on youth development."', position: 'bottom' },
      { target: '#saveBtn', title: 'Save Settings', text: 'Remember to save after making changes. Your preferences will immediately affect how the AI responds.', position: 'bottom' }
    ],
    'player-profile': [
      { target: '.player-header, .page-header', title: 'Player Profile', text: 'View and edit this player\'s name, number, photo, and basic info here.', position: 'bottom' },
      { target: '.metrics-grid, .player-metrics-card', title: 'Skill Metrics in 6 Categories', text: 'Drag any slider to rate the player from 1 to 10. Or describe the player in chat with Brad — he\'ll fill these for you automatically.', position: 'top' },
      { target: '#playerRadar, .radar-chart, canvas', title: 'Visual Profile', text: 'The radar chart updates live as you adjust metrics. Quick way to spot strengths and gaps.', position: 'left' }
    ]
  };

  var PAGE_MAP = {
    '/': 'home',
    '/chat': 'chat',
    '/team-setup': 'team-setup',
    '/data-upload': 'data-upload',
    '/scouting': 'scouting',
    '/plays': 'plays',
    '/notebook': 'notebook',
    '/history': 'history',
    '/settings': 'settings'
  };

  var _overlay, _highlight, _tooltip, _currentPage, _tourIdx;

  function _userScope() {
    var m = document.querySelector('meta[name="np-user-id"]');
    return (m && m.content) ? 'u' + m.content + '_' : '';
  }

  function _getState(page) {
    return parseInt(localStorage.getItem('np_tour_' + _userScope() + page) || '0', 10);
  }

  function _setState(page, val) {
    localStorage.setItem('np_tour_' + _userScope() + page, val);
  }

  function _detectPage() {
    var path = window.location.pathname.replace(/\/+$/, '') || '/';
    if (PAGE_MAP[path]) return PAGE_MAP[path];
    // Dynamic routes
    if (path.indexOf('/player/') === 0) return 'player-profile';
    return null;
  }

  function _createUI() {
    _overlay = document.createElement('div');
    _overlay.className = 'tour-overlay';
    document.body.appendChild(_overlay);

    _highlight = document.createElement('div');
    _highlight.className = 'tour-highlight';
    document.body.appendChild(_highlight);

    _tooltip = document.createElement('div');
    _tooltip.className = 'tour-tooltip';
    document.body.appendChild(_tooltip);
  }

  function _showStep(idx) {
    var steps = CONFIGS[_currentPage];
    if (!steps || idx >= steps.length) { _endTour(); return; }
    _tourIdx = idx;
    var step = steps[idx];
    var el = document.querySelector(step.target);
    if (!el) { _showStep(idx + 1); return; }

    var rect = el.getBoundingClientRect();
    var pad = 8;

    _highlight.style.top = (rect.top - pad + window.scrollY) + 'px';
    _highlight.style.left = (rect.left - pad) + 'px';
    _highlight.style.width = (rect.width + pad * 2) + 'px';
    _highlight.style.height = (rect.height + pad * 2) + 'px';

    _tooltip.style.opacity = '0';
    _tooltip.textContent = '';

    var arrow = document.createElement('div');
    arrow.className = 'tour-arrow tour-arrow-' + step.position;

    var header = document.createElement('div');
    header.className = 'tour-header';

    var avatar = document.createElement('img');
    avatar.src = AVATAR_URL;
    avatar.className = 'tour-avatar';
    avatar.onerror = function() {
      var fb = document.createElement('div');
      fb.className = 'tour-avatar-fallback';
      fb.textContent = 'D';
      this.replaceWith(fb);
    };

    var headerText = document.createElement('div');
    headerText.className = 'tour-header-text';

    var title = document.createElement('div');
    title.className = 'tour-title';
    title.textContent = step.title;

    var badge = document.createElement('div');
    badge.className = 'tour-badge';
    badge.textContent = GUIDE_NAME + ' \u2022 ' + GUIDE_ROLE;

    headerText.appendChild(title);
    headerText.appendChild(badge);
    header.appendChild(avatar);
    header.appendChild(headerText);

    var body = document.createElement('div');
    body.className = 'tour-body';
    body.textContent = step.text;

    var footer = document.createElement('div');
    footer.className = 'tour-footer';

    var dots = document.createElement('div');
    dots.className = 'tour-dots';
    for (var d = 0; d < steps.length; d++) {
      var dot = document.createElement('span');
      dot.className = 'tour-dot' + (d === idx ? ' active' : '');
      dots.appendChild(dot);
    }

    var btns = document.createElement('div');
    btns.className = 'tour-btns';

    var skipBtn = document.createElement('button');
    skipBtn.className = 'tour-btn-skip';
    skipBtn.textContent = 'Skip Tour';
    skipBtn.onclick = function() { _endTour(); };

    var nextBtn = document.createElement('button');
    nextBtn.className = 'tour-btn-next';
    nextBtn.textContent = idx === steps.length - 1 ? 'Got it!' : 'Next';
    nextBtn.onclick = function() { _showStep(idx + 1); };

    btns.appendChild(skipBtn);
    btns.appendChild(nextBtn);
    footer.appendChild(dots);
    footer.appendChild(btns);

    _tooltip.appendChild(arrow);
    _tooltip.appendChild(header);
    _tooltip.appendChild(body);
    _tooltip.appendChild(footer);

    var tw = 340;
    var ttop, tleft;
    var pos = step.position;

    // Measure actual tooltip height after content is added
    _tooltip.style.width = tw + 'px';
    _tooltip.style.visibility = 'hidden';
    _tooltip.style.opacity = '0';
    _tooltip.style.display = 'block';
    var th = _tooltip.offsetHeight || 200;
    _tooltip.style.visibility = '';
    _tooltip.style.display = '';

    // Auto-flip if not enough space
    if (pos === 'bottom' && rect.bottom + pad + 14 + th > window.innerHeight) {
      pos = 'top';
    } else if (pos === 'top' && rect.top - pad - 14 - th < 0) {
      pos = 'bottom';
    }

    // Update arrow to match actual position
    var arrowEl = _tooltip.querySelector('.tour-arrow');
    if (arrowEl) {
      arrowEl.className = 'tour-arrow tour-arrow-' + pos;
    }

    if (pos === 'bottom') {
      ttop = rect.bottom + pad + 14;
      tleft = rect.left + rect.width / 2 - tw / 2;
    } else if (pos === 'right') {
      ttop = rect.top + rect.height / 2 - 80;
      tleft = rect.right + pad + 14;
    } else {
      ttop = rect.top - pad - 14 - th;
      tleft = rect.left + rect.width / 2 - tw / 2;
    }

    if (tleft < 12) tleft = 12;
    if (tleft + tw > window.innerWidth - 12) tleft = window.innerWidth - tw - 12;

    // Clamp vertically to stay in viewport
    if (ttop < 12) ttop = 12;
    if (ttop + th > window.innerHeight - 12) ttop = window.innerHeight - th - 12;

    _tooltip.style.width = tw + 'px';
    _tooltip.style.left = tleft + 'px';
    _tooltip.style.top = ttop + 'px';
    _tooltip.style.bottom = 'auto';

    requestAnimationFrame(function() { _tooltip.style.opacity = '1'; });
    el.scrollIntoView({ behavior: 'smooth', block: 'nearest' });
  }

  function _endTour() {
    if (_overlay) _overlay.remove();
    if (_highlight) _highlight.remove();
    if (_tooltip) _tooltip.remove();
    _overlay = _highlight = _tooltip = null;
    // State is incremented in init() before the tour starts, so abandonment
    // (closing tab mid-tour) still counts as one of the two allowed appearances.
  }

  function _startTour() {
    _createUI();
    _showStep(0);
  }

  function _showRepeatPrompt() {
    var bar = document.createElement('div');
    bar.className = 'tour-repeat-bar';

    var av = document.createElement('img');
    av.src = AVATAR_URL;
    av.className = 'tour-repeat-avatar';
    av.onerror = function() { this.style.display = 'none'; };

    var txt = document.createElement('span');
    txt.className = 'tour-repeat-text';
    txt.textContent = 'Need another walkthrough?';

    var yesBtn = document.createElement('button');
    yesBtn.className = 'tour-repeat-yes';
    yesBtn.textContent = 'Show me';
    yesBtn.onclick = function() {
      bar.remove();
      _startTour();
    };

    var noBtn = document.createElement('button');
    noBtn.className = 'tour-repeat-no';
    noBtn.textContent = 'I\'m good';
    noBtn.onclick = function() {
      bar.remove();
      _setState(_currentPage, 2);
    };

    bar.appendChild(av);
    bar.appendChild(txt);
    bar.appendChild(yesBtn);
    bar.appendChild(noBtn);
    document.body.appendChild(bar);
  }

  function init(pageName) {
    _currentPage = pageName || _detectPage();
    if (!_currentPage || !CONFIGS[_currentPage]) return;

    // First-visit-ever defers to welcome-spotlight: the personal welcome
    // overlay is delivering the introduction this load, so don't ambush
    // the coach with the tour bubble at the same time. State is NOT bumped
    // — the tour shows in full on the next visit (state=0 → full tour,
    // state=1 → "want another walkthrough?" prompt, just shifted by one
    // visit). The np_welcomed_<uid> flag is set by welcome-spotlight.js
    // when the coach dismisses the overlay.
    var uidMeta = document.querySelector('meta[name="np-user-id"]');
    var uid = uidMeta ? uidMeta.content : '';
    if (uid && localStorage.getItem('np_welcomed_' + uid) !== '1') return;

    var state = _getState(_currentPage);

    // Hard cap: Daisy's onboarding tour appears at most 2 times per page.
    // After the user has seen the full tour once + the "want to see again?"
    // prompt once, she stops appearing entirely on this page. Persisted in
    // localStorage so it survives logouts/logins — only initial onboarding,
    // never on every reconnection.
    if (state >= 2) return;

    // Increment IMMEDIATELY (before showing any UI). This counts the visit
    // even if the coach closes the tab mid-tour, so the per-page cap is
    // enforced reliably.
    _setState(_currentPage, state + 1);

    var delay = _currentPage === 'plays' ? 1500 : 800;

    if (state === 0) {
      setTimeout(function() {
        var firstTarget = CONFIGS[_currentPage][0];
        if (firstTarget && document.querySelector(firstTarget.target)) {
          _startTour();
        } else {
          var obs = new MutationObserver(function(_, o) {
            if (document.querySelector(firstTarget.target)) {
              o.disconnect();
              _startTour();
            }
          });
          obs.observe(document.body, { childList: true, subtree: true });
          setTimeout(function() { obs.disconnect(); }, 5000);
        }
      }, delay);
    } else if (state === 1) {
      setTimeout(_showRepeatPrompt, 1200);
    }
  }

  return { init: init, start: _startTour };
})();

document.addEventListener('DOMContentLoaded', function() {
  // One-time reset for existing users when tour content changes.
  // Bump TOUR_VERSION to force all users to see the tour fresh again.
  var TOUR_VERSION = '3';
  if (localStorage.getItem('np_tour_version') !== TOUR_VERSION) {
    Object.keys(localStorage).filter(function(k){ return k.startsWith('np_tour_'); })
          .forEach(function(k){ localStorage.removeItem(k); });
    localStorage.setItem('np_tour_version', TOUR_VERSION);
  }
  NpTour.init();
});
