/**
 * NextPlay — Video Hub / Scouting
 * Upload, playback, clipping, tagging, telestrator, timeline.
 *
 * TABLE OF CONTENTS
 * ─────────────────
 * 1. VIDEO PLAYER & INIT ........... State, YouTube shim, init, upload, grid
 * 2. CLIPPING & TIMELINE ........... Timeline markers, annotation track, clips sidebar
 * 3. ANNOTATIONS & TELESTRATOR ..... Keyboard shortcuts, scissors, I/O points, drawing tools
 * 4. EXPORT & COMPILATION .......... Compile modal, card creators, rendering, export
 * 5. PLAYLISTS ..................... Playlist CRUD, batch operations
 * 6. COMPARISON MODE ............... Side-by-side video comparison, zoom
 */

/* ═══════════════════════════════════════════════════════════════════════════
   §1  VIDEO PLAYER & INIT
   State, YouTube shim, config, upload, video grid, filters
   ═══════════════════════════════════════════════════════════════════════════ */

/* ═══ State ═══════════════════════════════════════════════ */
let _videos = [];
let _currentVideo = null;
let _clips = [];
let _annotations = [];
let _vjsPlayer = null;
let _uploadConfig = null; // { provider: 's3', bucket, region }
let _pendingFile = null;
let _currentFilter = '';
let _clipRating = null;
let _uploadSource = 'file'; // --- NEW: Hybrid Video Architecture --- 'file' or 'url'
let _uploadInProgress = false; // Prevent navigation during upload

// --- NEW: Hybrid Video Architecture — YouTube ID extraction + Player Shim ---
function _extractYouTubeId(url) {
  const m = url.match(/(?:youtube\.com\/watch\?v=|youtu\.be\/|youtube\.com\/embed\/|youtube\.com\/shorts\/)([a-zA-Z0-9_-]{11})/);
  return m ? m[1] : null;
}

function _getEmbedUrl(url) {
  if (!url || typeof url !== 'string') return null;
  var ytId = _extractYouTubeId(url);
  if (ytId) return 'https://www.youtube.com/embed/' + ytId;
  var vimeoMatch = url.match(/vimeo\.com\/(\d+)/);
  if (vimeoMatch) return 'https://player.vimeo.com/video/' + vimeoMatch[1];
  return null;
}

/**
 * YouTubePlayerShim — wraps the YT IFrame API with a Video.js-compatible interface
 * so all existing tools (telestrator, clipping, tagging) work without changes.
 */
class YouTubePlayerShim {
  constructor(containerEl, videoId, onReady) {
    this._listeners = {};
    this._duration = 0;
    this._disposed = false;
    this._rafId = null;

    // Create a div for the YT player inside the container
    const ytDiv = document.createElement('div');
    ytDiv.id = 'ytPlayerHost';
    containerEl.appendChild(ytDiv);

    this._ytPlayer = new YT.Player('ytPlayerHost', {
      videoId: videoId,
      playerVars: { rel: 0, modestbranding: 1, iv_load_policy: 3, controls: 1, playsinline: 1, enablejsapi: 1 },
      events: {
        onReady: (e) => {
          this._duration = e.target.getDuration();
          this._startTimeLoop();
          if (onReady) onReady();
          this._fire('loadedmetadata');
        },
        onStateChange: (e) => {
          this._duration = this._ytPlayer.getDuration();
          if (e.data === YT.PlayerState.PLAYING) { this._fire('play'); this._fire('ratechange'); }
          if (e.data === YT.PlayerState.PAUSED) this._fire('pause');
          if (e.data === YT.PlayerState.ENDED) this._fire('ended');
        },
      },
    });
  }

  // --- Video.js compatible API ---
  currentTime(t) {
    if (t !== undefined) { this._ytPlayer.seekTo(t, true); this._fire('seeked'); return; }
    return this._ytPlayer.getCurrentTime ? this._ytPlayer.getCurrentTime() : 0;
  }
  duration() { return this._duration || (this._ytPlayer.getDuration ? this._ytPlayer.getDuration() : 0); }
  play() { try { this._ytPlayer.playVideo(); } catch(e) {} }
  pause() {
    try {
      this._ytPlayer.pauseVideo();
      // Force-mute as safety net — some YT embeds leak audio after pauseVideo()
      const self = this;
      setTimeout(() => {
        try {
          if (self._ytPlayer.getPlayerState() !== YT.PlayerState.PAUSED &&
              self._ytPlayer.getPlayerState() !== YT.PlayerState.ENDED) {
            self._ytPlayer.pauseVideo();
          }
        } catch(e) {}
      }, 100);
    } catch(e) {}
  }
  paused() {
    try {
      const state = this._ytPlayer.getPlayerState();
      return state !== YT.PlayerState.PLAYING && state !== YT.PlayerState.BUFFERING;
    } catch(e) { return true; }
  }
  playbackRate(r) {
    if (r !== undefined) { this._ytPlayer.setPlaybackRate(r); this._fire('ratechange'); return; }
    return this._ytPlayer.getPlaybackRate ? this._ytPlayer.getPlaybackRate() : 1;
  }
  muted(v) {
    if (v !== undefined) { v ? this._ytPlayer.mute() : this._ytPlayer.unMute(); return; }
    return this._ytPlayer.isMuted ? this._ytPlayer.isMuted() : false;
  }
  on(event, cb) {
    if (!this._listeners[event]) this._listeners[event] = [];
    this._listeners[event].push(cb);
  }
  off(event, cb) {
    if (!this._listeners[event]) return;
    this._listeners[event] = this._listeners[event].filter(fn => fn !== cb);
  }
  dispose() {
    this._disposed = true;
    if (this._rafId) cancelAnimationFrame(this._rafId);
    try { this._ytPlayer.destroy(); } catch (e) {}
    this._listeners = {};
  }
  // Stubs for Video.js-specific features (no-op for YouTube)
  controlBar = { addChild() {}, getChild() { return null; } };
  tech() { return { el_: null }; }

  // --- Internal ---
  _fire(event) { (this._listeners[event] || []).forEach(cb => { try { cb(); } catch(e) {} }); }
  _startTimeLoop() {
    const tick = () => {
      if (this._disposed) return;
      this._fire('timeupdate');
      this._rafId = requestAnimationFrame(tick);
    };
    this._rafId = requestAnimationFrame(tick);
  }
}

// YouTube IFrame API readiness
let _ytApiReady = false;
if (window.YT && window.YT.Player) { _ytApiReady = true; }
window.onYouTubeIframeAPIReady = () => { _ytApiReady = true; };

// 6-second timeout: if iframe_api / widgetapi.js is blocked (ad-blocker,
// network, CSP), reject so the caller can fall back to a plain iframe
// embed instead of leaving the player container black forever.
function _waitForYTApi(timeoutMs = 6000) {
  return new Promise((resolve, reject) => {
    if (_ytApiReady) return resolve();
    const start = Date.now();
    const check = setInterval(() => {
      if (_ytApiReady || (window.YT && window.YT.Player)) {
        clearInterval(check);
        _ytApiReady = true;
        resolve();
      } else if (Date.now() - start > timeoutMs) {
        clearInterval(check);
        reject(new Error('YouTube IFrame API failed to load'));
      }
    }, 100);
  });
}
// --- END NEW ---

/* ═══ Phase 1 State ═══════════════════════════════════════ */
let _clipInPoint = null;   // float seconds — set with I key
let _clipOutPoint = null;  // float seconds — set with O key
let _clipPreviewActive = false;
const PLAYBACK_RATES = [0.25, 0.5, 1, 1.5, 2];
const FRAME_DURATION = 1 / 30; // 30fps default

/* ═══ ACTION TYPES ════════════════════════════════════════ */
const ACTION_TYPES = [
  { value: 'pick_and_roll', get label() { return t('scouting.action.pick_and_roll'); } },
  { value: 'isolation', get label() { return t('scouting.action.isolation'); } },
  { value: 'fast_break', get label() { return t('scouting.action.fast_break'); } },
  { value: 'defense', get label() { return t('scouting.action.defense'); } },
  { value: 'transition', get label() { return t('scouting.action.transition'); } },
  { value: 'three_pointer', get label() { return t('scouting.action.three_pointer'); } },
  { value: 'post_up', get label() { return t('scouting.action.post_up'); } },
  { value: 'screen', get label() { return t('scouting.action.screen'); } },
  { value: 'turnover', get label() { return t('scouting.action.turnover'); } },
  { value: 'rebound', get label() { return t('scouting.action.rebound'); } },
  { value: 'free_throw', get label() { return t('scouting.action.free_throw'); } },
  { value: 'out_of_bounds', get label() { return t('scouting.action.out_of_bounds'); } },
  { value: 'other', get label() { return t('scouting.action.other'); } },
];

/* ═══ Init ════════════════════════════════════════════════ */
// ── Upload navigation guard ─────────────────────────────────
// Intercept sidebar links during upload to show styled warning
let _pendingNavUrl = null;

window.addEventListener('beforeunload', (e) => {
  // When SW owns the upload, navigation is safe — the upload continues in background.
  if (_uploadInProgress && !(window.NextPlayUpload && window.NextPlayUpload.hasSWSupport())) {
    e.preventDefault();
    e.returnValue = '';
    return '';
  }
});

function _initUploadNavGuard() {
  // Intercept all sidebar navigation links
  document.querySelectorAll('.sidebar-nav a[href], .sidebar-brand a[href]').forEach(link => {
    link.addEventListener('click', (e) => {
      if (!_uploadInProgress) return; // allow navigation
      // SW-backed uploads: let user navigate freely
      if (window.NextPlayUpload && window.NextPlayUpload.hasSWSupport()) return;
      e.preventDefault();
      _pendingNavUrl = link.getAttribute('href');
      _showUploadWarningModal();
    });
  });
}

function _showUploadWarningModal() {
  // Remove existing if any
  const existing = document.getElementById('uploadNavWarning');
  if (existing) existing.remove();

  const overlay = document.createElement('div');
  overlay.id = 'uploadNavWarning';
  overlay.style.cssText = 'position:fixed;top:0;left:0;right:0;bottom:0;background:rgba(0,0,0,0.6);z-index:99999;display:flex;align-items:center;justify-content:center;';
  overlay.innerHTML = `
    <div style="background:var(--surface, #161d28);border:1px solid var(--border, rgba(255,255,255,0.08));
        border-radius:16px;padding:32px;max-width:420px;width:90%;text-align:center;
        box-shadow:0 20px 60px rgba(0,0,0,0.5);">
      <svg width="48" height="48" viewBox="0 0 24 24" fill="none" stroke="var(--accent, #f48c25)"
          stroke-width="2" stroke-linecap="round" stroke-linejoin="round" style="margin-bottom:16px;">
        <path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/><polyline points="17 8 12 3 7 8"/>
        <line x1="12" y1="3" x2="12" y2="15"/>
      </svg>
      <h3 style="color:#fff;font-size:18px;font-weight:700;margin:0 0 8px;">Upload in Progress</h3>
      <p style="color:#9ba4b0;font-size:14px;line-height:1.5;margin:0 0 24px;">
        A video is currently being uploaded.<br>Leaving this page will cancel the upload.
      </p>
      <div style="display:flex;gap:12px;justify-content:center;">
        <button onclick="document.getElementById('uploadNavWarning').remove()"
            style="padding:10px 24px;border-radius:10px;font-size:14px;font-weight:600;cursor:pointer;
            background:var(--accent, #f48c25);color:#fff;border:none;">
          Stay on Page
        </button>
        <button onclick="_confirmLeave()"
            style="padding:10px 24px;border-radius:10px;font-size:14px;font-weight:600;cursor:pointer;
            background:rgba(255,255,255,0.06);color:#fff;border:1px solid rgba(255,255,255,0.1);">
          Leave Anyway
        </button>
      </div>
    </div>`;
  document.body.appendChild(overlay);
  overlay.addEventListener('click', (e) => {
    if (e.target === overlay) overlay.remove();
  });
}

function _confirmLeave() {
  _uploadInProgress = false;
  _uploadAborted = true;
  _activeUploadXHRs.forEach(xhr => xhr.abort());
  _activeUploadXHRs = [];
  document.getElementById('uploadNavWarning')?.remove();
  if (_pendingNavUrl) window.location.href = _pendingNavUrl;
}

document.addEventListener('DOMContentLoaded', async () => {
  await loadUploadConfig();
  loadVideos();
  setupUploadZone();
  setupFilters();
  setupSearch();
  populateClipActionSelect();
  _initUploadNavGuard();
  // Show/hide custom type description field
  const uploadTypeEl = document.getElementById('uploadType');
  if (uploadTypeEl) {
    uploadTypeEl.addEventListener('change', () => {
      const grp = document.getElementById('uploadCustomTypeGroup');
      if (grp) grp.style.display = uploadTypeEl.value === 'other' ? '' : 'none';
    });
  }
});

async function loadUploadConfig() {
  try {
    const res = await fetch('/api/scouting/upload-config');
    if (res.ok) _uploadConfig = await res.json();
  } catch (e) { /* upload not configured */ }
}

function populateClipActionSelect() {
  const sel = document.getElementById('clipAction');
  sel.innerHTML = ACTION_TYPES.map(a => `<option value="${a.value}">${a.label}</option>`).join('');
}

/* ═══ Video Grid ══════════════════════════════════════════ */
async function loadVideos() {
  try {
    let url = '/api/scouting/videos';
    const params = [];
    if (_currentFilter) params.push(`video_type=${_currentFilter}`);
    const search = document.getElementById('videoSearch')?.value;
    if (search) params.push(`search=${encodeURIComponent(search)}`);
    if (params.length) url += '?' + params.join('&');

    const res = await API.get(url);
    _videos = res.data || [];
    renderVideoGrid();
    loadQuota();
  } catch (e) {
    console.error('Load videos error:', e);
    document.getElementById('videoGrid').innerHTML = `<p style="color:var(--text-muted);text-align:center;grid-column:1/-1;">${t('scouting.grid.load_failed')}</p>`;
  }
}

function _expiryBadge(v) {
  if (v.keep_forever) return `<span class="video-card-badge video-card-permanent">${t('scouting.badge.permanent')}</span>`;
  if (!v.expires_at) return '';
  const exp = new Date(v.expires_at.endsWith('Z') ? v.expires_at : v.expires_at + 'Z');
  const now = new Date();
  const diffH = Math.max(0, (exp - now) / 3600000);
  if (diffH <= 0) return `<span class="video-card-badge video-card-expiry-urgent">${t('scouting.badge.expired')}</span>`;
  if (diffH <= 48) return `<span class="video-card-badge video-card-expiry-urgent">${t('scouting.badge.hours_left', { count: Math.ceil(diffH) })}</span>`;
  const days = Math.ceil(diffH / 24);
  return `<span class="video-card-badge video-card-expiry">${t('scouting.badge.days_left', { count: days })}</span>`;
}

function renderVideoGrid() {
  const grid = document.getElementById('videoGrid');
  if (!_videos.length) {
    grid.innerHTML = `<div style="grid-column:1/-1;text-align:center;padding:var(--sp-8);color:var(--text-muted);">
      <span class="material-symbols-outlined" style="font-size:3rem;display:block;margin-bottom:var(--sp-2);">videocam_off</span>
      ${t('scouting.grid.empty')}</div>`;
    return;
  }
  grid.innerHTML = _videos.map(v => {
    const thumb = v.thumbnail_url
      ? `<img class="video-card-thumb" src="${v.thumbnail_url}" alt="${v.title}" loading="lazy">`
      : `<div class="video-card-thumb-placeholder"><span class="material-symbols-outlined">videocam</span></div>`;
    const expiryBadge = _expiryBadge(v);
    const type = v.video_type === 'other' && v.custom_type_note ? v.custom_type_note : v.video_type.replace('_', ' ');
    // Format duration
    let durStr = '';
    if (v.duration_seconds) {
      const m = Math.floor(v.duration_seconds / 60);
      const s = Math.floor(v.duration_seconds % 60);
      durStr = `${m}:${s.toString().padStart(2, '0')}`;
    }
    // Format date
    let dateStr = '';
    if (v.created_at) {
      const d = new Date(v.created_at.endsWith('Z') ? v.created_at : v.created_at + 'Z');
      dateStr = d.toLocaleDateString('he-IL');
    }
    return `<div class="video-card" onclick="openVideo(${v.id})">
      <div class="video-card-thumb-wrap">
        ${thumb}
        ${durStr ? `<span class="video-card-duration">${durStr}</span>` : ''}
      </div>
      <div class="video-card-body">
        <div class="video-card-title">${esc(v.title)}</div>
        <div class="video-card-info">
          ${dateStr ? `<span><span class="material-symbols-outlined">calendar_today</span>${dateStr}</span>` : ''}
          ${v.opponent ? `<span><span class="material-symbols-outlined">groups</span>vs ${esc(v.opponent)}</span>` : ''}
          <span><span class="material-symbols-outlined">movie</span>${v.clip_count} ${t('scouting.grid.clips_label')}</span>
        </div>
        <div class="video-card-meta">
          <span class="video-card-badge">${type}</span>
          ${v.source_type === 'external' ? '<span class="video-card-badge video-card-source-external">External</span>' : ''}
          ${expiryBadge}
        </div>
      </div>
    </div>`;
  }).join('');
}

async function loadQuota() {
  try {
    const res = await API.get('/api/scouting/quota', { silent: true });
    const d = res.data;
    window._quotaData = d;
    const usedGB = (d.storage_used_bytes / (1024*1024*1024)).toFixed(2);
    const limitGB = d.storage_limit_gb || (d.storage_limit_bytes / (1024*1024*1024)).toFixed(0);
    const pct = Math.min(100, (d.storage_used_bytes / d.storage_limit_bytes) * 100);
    const barColor = pct >= 90 ? '#ef4444' : pct >= 50 ? '#f59e0b' : '#22c55e';
    document.getElementById('quotaBar').innerHTML = `<div style="display:flex;align-items:center;gap:var(--sp-2);"><span style="font-weight:600;color:var(--text);white-space:nowrap;">${t('scouting.quota.storage', { used: usedGB, limit: limitGB })}</span><div class="quota-fill"><div class="quota-fill-inner" style="width:${pct}%;background:${barColor}"></div></div></div>`;
  } catch (e) {}
}

/* ═══ Storage Upgrade ═════════════════════════════════════ */

function renderUpgradeTiers() {
  const container = document.getElementById('upgradeTiers');
  if (!container) return;
  const extra = (window._quotaData && window._quotaData.extra_storage_gb) || 0;
  const totalGB = 10 + extra;

  // Tier options based on current extra storage
  const tierMap = {
    0:  [
      { gb: 5,  label: '+5 GB (15 GB total)',  price: '$3/mo' },
      { gb: 10, label: '+10 GB (20 GB total)', price: '$5.50/mo' },
      { gb: 20, label: '+20 GB (30 GB total)', price: '$10/mo' },
    ],
    5:  [
      { gb: 10, label: 'Upgrade to +10 GB (20 GB total)', price: '+$2.50/mo' },
      { gb: 20, label: 'Upgrade to +20 GB (30 GB total)', price: '+$7/mo' },
    ],
    10: [
      { gb: 20, label: 'Upgrade to +20 GB (30 GB total)', price: '+$4.50/mo' },
    ],
  };

  const tiers = tierMap[extra];
  if (!tiers) {
    container.innerHTML = `<p style="color:var(--text-muted);text-align:center;padding:var(--sp-4) 0;">${t('scouting.upgrade.max_reached')}</p>`;
    return;
  }

  container.innerHTML = `
    <p style="color:var(--text-muted);font-size:0.85rem;margin-bottom:var(--sp-3);">
      Current plan: ${totalGB} GB total${extra > 0 ? ' (' + extra + ' GB extra)' : ''}
    </p>
    ${tiers.map(tier => `
      <div style="display:flex;align-items:center;justify-content:space-between;padding:var(--sp-3) var(--sp-4);background:var(--bg-input);border:1px solid var(--border);border-radius:var(--radius-sm);margin-bottom:var(--sp-2);">
        <div>
          <span style="color:var(--text);font-weight:600;">${tier.label}</span>
          <span style="color:var(--text-muted);margin-left:var(--sp-2);">— ${tier.price}</span>
        </div>
        <button class="btn btn-primary" style="padding:8px 16px;min-height:36px;font-size:0.85rem;" onclick="initiateUpgrade(${tier.gb})">
          Select Plan
        </button>
      </div>
    `).join('')}
    <p style="color:var(--text-muted);font-size:0.8rem;margin-top:var(--sp-3);text-align:center;">
      Or delete older videos to free space.
    </p>
  `;
}

function initiateUpgrade(targetTierGB) {
  console.log(`[NextPlay] Upgrade requested: +${targetTierGB}GB tier`);
  // TODO: Integrate Stripe checkout here
  Toast.info(t('scouting.upgrade.coming_soon'));
  closeModal('upgradeModal');
}

/* ═══ Filters & Search ════════════════════════════════════ */
function setupFilters() {
  document.querySelectorAll('#typeFilters .filter-btn').forEach(btn => {
    btn.addEventListener('click', () => {
      document.querySelectorAll('#typeFilters .filter-btn').forEach(b => b.classList.remove('active'));
      btn.classList.add('active');
      _currentFilter = btn.dataset.type;
      loadVideos();
    });
  });
}

function setupSearch() {
  let timer;
  document.getElementById('videoSearch')?.addEventListener('input', () => {
    clearTimeout(timer);
    timer = setTimeout(loadVideos, 400);
  });
}

/* ═══ Upload ══════════════════════════════════════════════ */
let _uploadAborted = false;
let _activeUploadXHRs = [];

function setupUploadZone() {
  const zone = document.getElementById('uploadZone');
  const input = document.getElementById('videoFileInput');

  zone.addEventListener('click', () => input.click());
  zone.addEventListener('dragover', (e) => { e.preventDefault(); zone.classList.add('dragover'); });
  zone.addEventListener('dragleave', () => zone.classList.remove('dragover'));
  zone.addEventListener('drop', (e) => {
    e.preventDefault();
    zone.classList.remove('dragover');
    if (e.dataTransfer.files.length) handleFileSelect(e.dataTransfer.files[0]);
  });
  input.addEventListener('change', () => { if (input.files[0]) handleFileSelect(input.files[0]); });
}

function handleFileSelect(file) {
  const ext = '.' + file.name.split('.').pop().toLowerCase();
  if (!['.mp4', '.mov', '.webm'].includes(ext)) {
    Toast.error(t('scouting.upload.invalid_type'));
    return;
  }
  if (file.size > 5 * 1024 * 1024 * 1024) {
    Toast.error(t('scouting.upload.too_large'));
    return;
  }
  _pendingFile = file;
  document.getElementById('uploadTitle').value = file.name.replace(/\.[^/.]+$/, '');
  document.getElementById('uploadSubmitBtn').disabled = false;
  document.getElementById('uploadZone').innerHTML = `
    <span class="material-symbols-outlined">check_circle</span>
    <p>${esc(file.name)}</p>
    <p style="font-size:0.75rem;color:var(--text-muted);">${(file.size / (1024*1024)).toFixed(1)} MB</p>`;
}

function cancelUploadModal() {
  _uploadAborted = true;
  _activeUploadXHRs.forEach(xhr => xhr.abort());
  _activeUploadXHRs = [];
  if (window.NextPlayUpload && window.NextPlayUpload.hasSWSupport()) {
    window.NextPlayUpload.cancelAll();
  }
  closeModal('uploadModal');
}

// --- NEW: Hybrid Video Architecture ---
function switchUploadSource(source) {
  _uploadSource = source;
  document.querySelectorAll('#uploadSourceTabs .upload-tab').forEach(btn => {
    btn.classList.toggle('active', btn.dataset.source === source);
  });
  const isFile = source === 'file';
  document.getElementById('uploadZone').style.display = isFile ? '' : 'none';
  document.getElementById('uploadUrlSection').style.display = isFile ? 'none' : '';
  document.getElementById('uploadProgress').classList.remove('active');
  if (isFile) {
    document.getElementById('uploadSubmitBtn').disabled = !_pendingFile;
    document.getElementById('uploadSubmitBtn').textContent = 'Upload';
  } else {
    const url = (document.getElementById('uploadVideoUrl').value || '').trim();
    document.getElementById('uploadSubmitBtn').disabled = !url;
    document.getElementById('uploadSubmitBtn').textContent = 'Add Video';
    _pendingFile = null;
  }
}
// --- END NEW ---

function openUploadModal() {
  _pendingFile = null;
  _uploadAborted = false;
  // --- NEW: Hybrid Video Architecture — reset to file tab ---
  _uploadSource = 'file';
  switchUploadSource('file');
  const urlInput = document.getElementById('uploadVideoUrl');
  if (urlInput) urlInput.value = '';
  // --- END NEW ---
  document.getElementById('uploadSubmitBtn').disabled = true;
  document.getElementById('uploadProgress').classList.remove('active');
  document.getElementById('uploadZone').innerHTML = `
    <span class="material-symbols-outlined">cloud_upload</span>
    <p>${t('scouting.upload.drag_or_click')}</p>
    <p style="font-size:0.75rem;color:var(--text-muted);">${t('scouting.upload.formats')}</p>`;
  document.getElementById('uploadTitle').value = '';
  document.getElementById('uploadOpponent').value = '';
  document.getElementById('uploadDate').value = '';
  openModal('uploadModal');
}

async function submitUpload() {
  // --- NEW: Hybrid Video Architecture — handle external URL ---
  if (_uploadSource === 'url') {
    const url = (document.getElementById('uploadVideoUrl').value || '').trim();
    if (!url) { Toast.error('Please enter a video URL'); return; }
    const btn = document.getElementById('uploadSubmitBtn');
    btn.disabled = true;
    btn.textContent = 'Adding...';
    try {
      await API.post('/api/scouting/videos/external', {
        url,
        title: document.getElementById('uploadTitle').value || url,
        video_type: document.getElementById('uploadType').value,
        opponent: document.getElementById('uploadOpponent').value || null,
        game_date: document.getElementById('uploadDate').value || null,
      });
      Toast.success('Video added successfully');
      closeModal('uploadModal');
      loadVideos();
    } catch (e) {
      Toast.error(e.message || 'Failed to add video');
    } finally {
      btn.disabled = false;
      btn.textContent = 'Add Video';
    }
    return;
  }
  // --- END NEW ---

  if (!_pendingFile || !_uploadConfig) {
    Toast.error('Upload not configured');
    return;
  }

  _uploadAborted = false;

  // Check quota first (before closing modal)
  try {
    const quotaRes = await fetch('/api/scouting/quota');
    const quota = await quotaRes.json();
    if (quota.storage_used_bytes + _pendingFile.size > quota.storage_limit_bytes) {
      Toast.error('Storage limit reached');
      renderUpgradeTiers();
      openModal('upgradeModal');
      return;
    }
  } catch (e) { /* quota check failed, proceed */ }

  // Capture form values before closing modal
  const file = _pendingFile;
  const title = document.getElementById('uploadTitle').value || file.name;
  const type = document.getElementById('uploadType').value;
  const customType = type === 'other' ? (document.getElementById('uploadCustomType').value || null) : null;
  const opponent = document.getElementById('uploadOpponent').value || null;
  const gameDate = document.getElementById('uploadDate').value || null;
  const keepForever = document.getElementById('uploadKeepForever').checked;

  // ── SW path: hand off to Service Worker, survives page navigation ──
  // Wait for SW init to complete (up to ~5s) before deciding
  if (window.NextPlayUpload && typeof window.NextPlayUpload.ready === 'function') {
    try {
      await Promise.race([
        window.NextPlayUpload.ready(),
        new Promise(function(r) { setTimeout(r, 5000); })
      ]);
    } catch(e) {}
  }
  console.log('[submitUpload] SW mode:', window.NextPlayUpload && window.NextPlayUpload.getMode && window.NextPlayUpload.getMode());
  if (window.NextPlayUpload && window.NextPlayUpload.hasSWSupport() && _uploadConfig.provider === 's3') {
    try {
      await window.NextPlayUpload.enqueue(file, {
        title, type, customType, opponent, gameDate, keepForever
      });
      closeModal('uploadModal');
      Toast.info('Upload started — you can navigate freely');
      return;
    } catch (e) {
      console.warn('[Upload] SW enqueue failed, falling back to in-page upload', e);
      // fall through to legacy path below
    }
  }

  // ── Legacy path (fallback when SW unsupported or S3 disabled) ──
  // Close modal immediately — upload continues in background
  closeModal('uploadModal');
  _uploadInProgress = true;
  Toast.info('Uploading video — please stay on this page');

  // Show floating progress banner
  const banner = document.getElementById('bgUploadBanner');
  const bgFill = document.getElementById('bgUploadFill');
  const bgPct = document.getElementById('bgUploadPct');
  const bgStatus = document.getElementById('bgUploadStatus');
  const bgTitle = document.getElementById('bgUploadTitle');

  if (banner) {
    banner.style.display = 'block';
    if (bgTitle) bgTitle.textContent = 'Uploading: ' + (title.length > 30 ? title.slice(0, 30) + '...' : title);
    if (bgFill) bgFill.style.width = '0%';
    if (bgPct) bgPct.textContent = '0%';
    if (bgStatus) bgStatus.textContent = 'Preparing...';
  }

  const updateProgress = (pct, statusMsg) => {
    if (bgFill) bgFill.style.width = pct + '%';
    if (bgPct) bgPct.textContent = pct + '%';
    if (statusMsg && bgStatus) bgStatus.textContent = statusMsg;
  };

  try {
    let uploadKey;

    if (_uploadConfig.provider === 'local') {
      // ── Local upload (dev mode — no S3) ──
      uploadKey = await uploadLocalFile(file, updateProgress);
    } else {
      // ── S3 upload ──
      const presignData = await API.post('/api/scouting/s3/presign-upload', {
        file_name: file.name,
        file_size: file.size,
        content_type: file.type || 'video/mp4'
      });
      const presign = presignData.data || presignData;
      if (!presign.key) throw new Error('Failed to get upload URL');
      uploadKey = presign.key;

      if (presign.mode === 'single') {
        await uploadToS3Single(presign.url, file, updateProgress);
      } else {
        await uploadToS3Multipart(presign, file, updateProgress);
      }
    }

    if (_uploadAborted) { banner.style.display = 'none'; return; }

    // Refresh auth token before register — long uploads may have expired it
    updateProgress(100, 'Saving...');
    try {
      await fetch('/api/auth/refresh', { method: 'POST', credentials: 'include' });
    } catch (e) { /* best-effort refresh */ }

    // Register with backend
    await API.post('/api/scouting/videos', {
      s3_key: uploadKey,
      original_name: file.name,
      file_size: file.size,
      title, video_type: type, custom_type_note: customType,
      opponent, game_date: gameDate, keep_forever: keepForever,
    });

    // Success — update banner briefly then hide
    if (bgPct) bgPct.textContent = '';
    if (bgStatus) bgStatus.textContent = 'Upload complete!';
    if (bgFill) { bgFill.style.width = '100%'; bgFill.style.background = '#22c55e'; }
    if (bgTitle) bgTitle.textContent = 'Done: ' + (title.length > 30 ? title.slice(0, 30) + '...' : title);
    _uploadInProgress = false;
    Toast.success(t('scouting.upload.success'));
    loadVideos();

    setTimeout(() => {
      if (banner) banner.style.display = 'none';
      if (bgFill) bgFill.style.background = 'var(--accent, #f48c25)';
    }, 4000);

  } catch (e) {
    console.error('[Upload] failed', e);
    _uploadInProgress = false;
    if (!_uploadAborted) {
      const errMsg = (e && e.message) ? e.message : t('scouting.upload.failed');
      if (bgStatus) bgStatus.textContent = errMsg.length > 60 ? errMsg.slice(0, 60) + '...' : errMsg;
      if (bgFill) bgFill.style.background = '#ef4444';
      if (bgPct) bgPct.textContent = '';
      if (bgTitle) bgTitle.textContent = 'Upload failed';
      Toast.error(errMsg);
      // Keep the error visible longer so user can read it
      setTimeout(() => {
        if (banner) banner.style.display = 'none';
        if (bgFill) bgFill.style.background = 'var(--accent, #f48c25)';
      }, 15000);
    } else {
      if (banner) banner.style.display = 'none';
    }
  }
}

/**
 * Local file upload — sends file directly to server via multipart form.
 * Used when S3 is not configured (local development).
 */
async function uploadLocalFile(file, onProgress) {
  return new Promise((resolve, reject) => {
    const formData = new FormData();
    formData.append('file', file);
    const xhr = new XMLHttpRequest();
    xhr.upload.onprogress = (e) => {
      if (e.lengthComputable) {
        const pct = Math.round(e.loaded / e.total * 100);
        onProgress(pct, `Uploading... ${pct}%`);
      }
    };
    xhr.onload = () => {
      if (xhr.status >= 200 && xhr.status < 300) {
        try {
          const data = JSON.parse(xhr.responseText);
          resolve(data.key);
        } catch { reject(new Error('Invalid response')); }
      } else {
        try {
          const err = JSON.parse(xhr.responseText);
          reject(new Error(err.error || 'Upload failed'));
        } catch { reject(new Error(`Upload failed (${xhr.status})`)); }
      }
    };
    xhr.onerror = () => reject(new Error('Network error'));
    xhr.open('POST', '/api/scouting/local/upload');
    xhr.setRequestHeader('X-Requested-With', 'XMLHttpRequest');
    xhr.send(formData);
  });
}

/**
 * Single-request S3 upload via presigned PUT URL.
 */
async function uploadToS3Single(url, file, onProgress) {
  return new Promise((resolve, reject) => {
    const xhr = new XMLHttpRequest();
    xhr.upload.onprogress = (e) => {
      if (e.lengthComputable) onProgress(Math.round(e.loaded / e.total * 100));
    };
    xhr.onload = () => {
      if (xhr.status >= 200 && xhr.status < 300) resolve();
      else reject(new Error('Upload failed: ' + xhr.status));
    };
    xhr.onerror = () => reject(new Error('Network error'));
    xhr.open('PUT', url);
    xhr.setRequestHeader('Content-Type', file.type || 'video/mp4');
    xhr.timeout = 600000; // 10 min
    xhr.ontimeout = () => reject(new Error('Upload timed out'));
    xhr.send(file);
  });
}

/**
 * Multipart S3 upload — splits file into parts, uploads each to a presigned URL,
 * then completes the multipart upload via the backend.
 */
async function uploadToS3Multipart(presign, file, onProgress) {
  const { urls, part_size, key, upload_id } = presign;
  const CONCURRENCY = 4;
  const completedParts = [];
  const partProgress = {};
  let queueIndex = 0;
  _activeUploadXHRs = [];

  function reportProgress() {
    const uploaded = Object.values(partProgress).reduce((a, b) => a + b, 0);
    const pct = Math.round((uploaded / file.size) * 100);
    const done = completedParts.length;
    onProgress(Math.min(pct, 99), `Uploading... (${done}/${urls.length} parts done)`);
  }

  async function worker() {
    while (queueIndex < urls.length) {
      if (_uploadAborted) throw new Error(t('scouting.upload.cancelled'));
      const idx = queueIndex++;
      const { part_number, url } = urls[idx];
      const start = (part_number - 1) * part_size;
      const end = Math.min(start + part_size, file.size);
      const chunk = file.slice(start, end);
      partProgress[part_number] = 0;

      const etag = await uploadPartWithRetry(url, chunk, part_number, urls.length, 3, (loaded) => {
        partProgress[part_number] = loaded;
        reportProgress();
      });

      completedParts.push({ part_number, etag });
      partProgress[part_number] = end - start;
      reportProgress();
    }
  }

  const workers = Array.from(
    { length: Math.min(CONCURRENCY, urls.length) },
    () => worker()
  );

  try {
    await Promise.all(workers);
  } catch (err) {
    _activeUploadXHRs.forEach(xhr => xhr.abort());
    _activeUploadXHRs = [];
    throw err;
  }

  // Complete multipart — CHECK the response
  onProgress(99, 'Finalizing...');
  _activeUploadXHRs = [];
  const completeRes = await fetch('/api/scouting/s3/complete-multipart', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({ key, upload_id, parts: completedParts })
  });
  if (!completeRes.ok) {
    const ct = completeRes.headers.get('content-type') || '';
    let msg = 'Failed to finalize upload (HTTP ' + completeRes.status + ')';
    if (ct.includes('application/json')) {
      try {
        const data = await completeRes.json();
        if (data && data.error) msg = data.error;
      } catch (e) {}
    }
    throw new Error(msg);
  }
}

/**
 * Upload a single part to S3 with retry and exponential backoff.
 */
async function uploadPartWithRetry(url, chunk, partNum, totalParts, maxRetries, onProgress) {
  for (let attempt = 0; attempt < maxRetries; attempt++) {
    try {
      return await new Promise((resolve, reject) => {
        const xhr = new XMLHttpRequest();
        _activeUploadXHRs.push(xhr);
        xhr.upload.onprogress = (e) => {
          if (e.lengthComputable) onProgress(e.loaded);
        };
        const cleanup = () => {
          const idx = _activeUploadXHRs.indexOf(xhr);
          if (idx > -1) _activeUploadXHRs.splice(idx, 1);
        };
        xhr.onload = () => {
          cleanup();
          if (xhr.status >= 200 && xhr.status < 300) {
            const etag = xhr.getResponseHeader('ETag');
            resolve(etag);
          } else {
            reject(new Error('Part upload failed: ' + xhr.status));
          }
        };
        xhr.onerror = () => { cleanup(); reject(new Error('Network error')); };
        xhr.open('PUT', url);
        xhr.timeout = 600000; // 10 min per part (100MB chunks)
        xhr.ontimeout = () => { cleanup(); reject(new Error('Part upload timed out')); };
        xhr.send(chunk);
      });
    } catch (err) {
      if (attempt === maxRetries - 1) throw err;
      await new Promise(r => setTimeout(r, 1000 * Math.pow(2, attempt))); // exponential backoff
    }
  }
}

/* ═══ Analysis View ═══════════════════════════════════════ */
async function openVideo(videoId) {
  try {
    const res = await API.get(`/api/scouting/videos/${videoId}`);
    _currentVideo = res.data;
    _clips = res.data.clips || [];
    _annotations = res.data.annotations || [];
    _compileTimeline = []; // reset before loading saved cards

    document.getElementById('gridView').style.display = 'none';
    document.getElementById('analysisView').classList.add('active');
    document.getElementById('analysisTitle').textContent = _currentVideo.title;

    _updateExpiryUI();

    // Load saved compile cards for this video
    var _loadedCards = [];
    try {
      const cardsRes = await API.get(`/api/scouting/compile-cards?video_id=${videoId}`);
      _loadedCards = cardsRes.data || [];
    } catch (e) { console.warn('[Compile Cards] load failed:', e); }

    // Build initial timeline: clips + cards placed according to after_clip_id
    _compileTimeline = _clips.map(function(c) { return { type: 'clip', clip: c }; });
    _loadedCards.forEach(function(c) {
      var cfg = c.config || {};
      var item = { type: c.card_type, config: cfg, dbId: c.id };
      var afterId = cfg.after_clip_id;
      if (afterId == null) {
        // Insert at start (before all clips but after other start cards)
        var insertAt = 0;
        while (insertAt < _compileTimeline.length && _compileTimeline[insertAt].type !== 'clip') insertAt++;
        _compileTimeline.splice(insertAt, 0, item);
      } else {
        // Find clip with matching id, insert after it
        var clipIdx = -1;
        for (var k = 0; k < _compileTimeline.length; k++) {
          if (_compileTimeline[k].type === 'clip' && _compileTimeline[k].clip.id === afterId) {
            clipIdx = k;
            break;
          }
        }
        if (clipIdx >= 0) {
          // Insert after this clip (and any cards immediately after it)
          var insertAfter = clipIdx + 1;
          while (insertAfter < _compileTimeline.length && _compileTimeline[insertAfter].type !== 'clip') insertAfter++;
          _compileTimeline.splice(insertAfter, 0, item);
        } else {
          // Clip not found (deleted) — append at end
          _compileTimeline.push(item);
        }
      }
    });

    initVideoPlayer();
    renderClipsSidebar();
    renderTimelineMarkers(); renderAnnotationTrack();
    loadPlaylists();

    // Trigger Daisy's video editor tour on first visit
    if (typeof NpTour !== 'undefined') {
      setTimeout(function() { NpTour.init('scouting-editor'); }, 1200);
    }
    // Populate comparison video picker
    const compSel = document.getElementById('compVideoSelect');
    if (compSel) {
      compSel.innerHTML = `<option value="">${t('scouting.comparison.choose_video')}</option>` +
        _videos.filter(v => v.id !== _currentVideo.id).map(v => `<option value="${v.id}">${esc(v.title)}</option>`).join('');
    }
  } catch (e) {
    Toast.error(t('scouting.analysis.load_failed'));
  }
}

function backToGrid() {
  document.getElementById('analysisView').classList.remove('active');
  document.getElementById('gridView').style.display = '';
  if (_vjsPlayer) {
    _vjsPlayer.pause();
  }
  // Cleanup comparison mode if active
  if (_comparisonMode) toggleComparisonMode();
  // Reset zoom
  telestrator.resetZoom();
  _currentVideo = null;
  telestrator.annotations = [];
}

function _showNoVideoMessage() {
  var c = document.getElementById('videoPlayerContainer');
  if (!c) return;
  c.textContent = '';
  var d = document.createElement('div');
  d.style.cssText = 'display:flex;align-items:center;justify-content:center;height:300px;color:var(--text-muted);flex-direction:column;gap:var(--sp-2);';
  var icon = document.createElement('span');
  icon.className = 'material-symbols-outlined';
  icon.style.fontSize = '48px';
  icon.textContent = 'videocam_off';
  var p = document.createElement('p');
  p.textContent = 'Video media file is not available';
  d.appendChild(icon);
  d.appendChild(p);
  c.appendChild(d);
}

function initVideoPlayer() {
  // --- NEW: Hybrid Video Architecture — clean up previous player or iframe ---
  const container = document.getElementById('videoContainer');
  if (_vjsPlayer) {
    _vjsPlayer.dispose();
    _vjsPlayer = null;
  }
  // Always rebuild the container (handles both Video.js disposal and iframe cleanup)
  container.innerHTML = '';
  container.classList.remove('yt-mode');
  container.removeAttribute('style');
  // --- END NEW ---
  const video = document.createElement('video');
  video.id = 'scoutingPlayer';
  video.className = 'video-js vjs-default-skin';
  video.setAttribute('playsinline', '');
  video.setAttribute('crossorigin', 'anonymous');
  container.appendChild(video);
  const canvas = document.createElement('canvas');
  canvas.id = 'telestratorCanvas';
  canvas.className = 'telestrator-canvas';
  container.appendChild(canvas);
  // --- END NEW ---

  // --- NEW: Hybrid Video Architecture — YouTube via IFrame API with shim ---
  if (_currentVideo.source_type === 'external' && _currentVideo.external_url) {
    const ytId = _extractYouTubeId(_currentVideo.external_url);
    if (ytId) {
      // Set up container: YouTube player + canvas overlay (remove unused <video> element)
      container.classList.add('yt-mode');
      const unusedVideo = container.querySelector('video');
      if (unusedVideo) unusedVideo.remove();

      _waitForYTApi().then(() => {
        _vjsPlayer = new YouTubePlayerShim(container, ytId, () => {
          // Init telestrator after player is ready
          const videoEl = container.querySelector('iframe');
          const canvasEl = container.querySelector('.telestrator-canvas');
          if (videoEl && canvasEl) telestrator.init(videoEl, canvasEl);
          _vjsPlayer.on('timeupdate', onTimeUpdate);
          _vjsPlayer.on('loadedmetadata', () => {
            const dur = _vjsPlayer.duration();
            document.getElementById('timelineDuration').textContent = fmtTime(isFinite(dur) ? dur : _currentVideo?.duration_seconds);
            renderAnnotationTrack();
          });
          _vjsPlayer.on('durationchange', () => {
            const dur = _vjsPlayer.duration();
            document.getElementById('timelineDuration').textContent = fmtTime(isFinite(dur) ? dur : _currentVideo?.duration_seconds);
          });
        });
      }).catch((err) => {
        // YT IFrame API failed to load — render a plain embedded iframe so
        // the coach can at least watch the video. Telestrator/clipping
        // integration won't work, but the video is visible and playable.
        console.warn('[scouting] YT API unavailable, falling back to plain iframe:', err.message);
        const iframe = document.createElement('iframe');
        iframe.src = 'https://www.youtube.com/embed/' + ytId;
        iframe.allow = 'autoplay; encrypted-media; picture-in-picture; fullscreen';
        iframe.allowFullscreen = true;
        iframe.setAttribute('frameborder', '0');
        container.appendChild(iframe);
      });
      return;
    }
  }

  const sources = [];
  if (_currentVideo.source_type === 'external' && _currentVideo.external_url) {
    const url = _currentVideo.external_url;
    if (url.includes('.m3u8')) {
      sources.push({ src: url, type: 'application/x-mpegURL' });
    } else {
      sources.push({ src: '/api/scouting/video-proxy/' + _currentVideo.id, type: 'video/mp4' });
    }
  }
  if (_currentVideo.source_type === 's3') {
    if (_currentVideo.s3_url) {
      sources.push({ src: _currentVideo.s3_url, type: 'video/mp4' });
    } else {
      // Fallback: route through the same-origin proxy when the backend
      // didn't pre-bake a presigned URL (e.g. S3 not configured locally,
      // expired credentials, presign error). Mirrors v1 behaviour for
      // safety + keeps canvas annotations same-origin friendly.
      sources.push({ src: '/api/scouting/video-proxy/' + _currentVideo.id, type: 'video/mp4' });
    }
  }

  if (!sources.length) {
    _showNoVideoMessage();
    return;
  }

  _vjsPlayer = videojs('scoutingPlayer', {
    controls: true,
    playbackRates: [0.25, 0.5, 1, 1.5, 2],
    fluid: true,
    sources: sources,
    html5: {
      vhs: { overrideNative: false },
      nativeAudioTracks: true,
      nativeVideoTracks: true,
    },
  });

  // Force native <video> element to sync on pause/play (fixes HLS audio leak)
  _vjsPlayer.on('pause', () => {
    const el = _vjsPlayer.tech({ IWillNotUseThisInPlugins: true })?.el_;
    if (el && !el.paused) el.pause();
  });
  _vjsPlayer.on('play', () => {
    const el = _vjsPlayer.tech({ IWillNotUseThisInPlugins: true })?.el_;
    if (el && el.paused) el.play();
  });

  // ── Skip buttons in Video.js control bar (next to play) ──
  const VjsButton = videojs.getComponent('Button');
  class SkipBackBtn extends VjsButton {
    constructor(player, options) { super(player, options); this.controlText(t('scouting.shortcuts.back_5s')); }
    buildCSSClass() { return 'vjs-skip-btn vjs-skip-back ' + super.buildCSSClass(); }
    handleClick() { skipTime(-5); }
  }
  class SkipFwdBtn extends VjsButton {
    constructor(player, options) { super(player, options); this.controlText(t('scouting.shortcuts.forward_5s')); }
    buildCSSClass() { return 'vjs-skip-btn vjs-skip-fwd ' + super.buildCSSClass(); }
    handleClick() { skipTime(5); }
  }
  // Frame-by-frame buttons (Phase 1.2)
  class FrameBackBtn extends VjsButton {
    constructor(player, options) { super(player, options); this.controlText(t('scouting.shortcuts.prev_frame')); }
    buildCSSClass() { return 'vjs-skip-btn vjs-frame-back ' + super.buildCSSClass(); }
    handleClick() { this.player().pause(); stepFrame(-1); }
  }
  class FrameFwdBtn extends VjsButton {
    constructor(player, options) { super(player, options); this.controlText(t('scouting.shortcuts.next_frame')); }
    buildCSSClass() { return 'vjs-skip-btn vjs-frame-fwd ' + super.buildCSSClass(); }
    handleClick() { this.player().pause(); stepFrame(1); }
  }
  _vjsPlayer.controlBar.addChild(new FrameBackBtn(_vjsPlayer), {}, 1);
  _vjsPlayer.controlBar.addChild(new SkipBackBtn(_vjsPlayer), {}, 2);
  _vjsPlayer.controlBar.addChild(new SkipFwdBtn(_vjsPlayer), {}, 3);
  _vjsPlayer.controlBar.addChild(new FrameFwdBtn(_vjsPlayer), {}, 4);

  // ── Override fullscreen to use videoWithTools (includes tools sidebar) ──
  const fsToggle = _vjsPlayer.controlBar.getChild('fullscreenToggle');
  if (fsToggle) {
    fsToggle.off('click');
    fsToggle.on('click', () => {
      const wrapper = document.getElementById('videoWithTools');
      if (document.fullscreenElement) {
        document.exitFullscreen();
      } else {
        wrapper.requestFullscreen().then(() => {
          if (screen.orientation && screen.orientation.lock) {
            screen.orientation.lock('landscape').catch(() => {});
          }
        });
      }
    });
  }
  document.addEventListener('fullscreenchange', () => {
    const wrapper = document.getElementById('videoWithTools');
    wrapper.classList.toggle('is-fullscreen', !!document.fullscreenElement);
    if (!document.fullscreenElement && screen.orientation && screen.orientation.unlock) {
      screen.orientation.unlock();
    }
  });

  // Speed badge (Phase 1.3)
  _vjsPlayer.on('ratechange', () => {
    const rate = _vjsPlayer.playbackRate();
    const badge = document.getElementById('speedBadge');
    if (!badge) return;
    badge.textContent = rate + 'x';
    badge.classList.add('visible');
    if (rate === 1) {
      setTimeout(() => badge.classList.remove('visible'), 800);
    } else {
      // Keep visible while not 1x, but refresh the show timer
      clearTimeout(badge._hideTimer);
      badge._hideTimer = null;
    }
  });

  _vjsPlayer.on('timeupdate', onTimeUpdate);
  _vjsPlayer.on('loadedmetadata', () => {
    const dur = _vjsPlayer.duration();
    document.getElementById('timelineDuration').textContent = fmtTime(isFinite(dur) ? dur : _currentVideo?.duration_seconds);
    telestrator.init(document.getElementById('scoutingPlayer'), document.getElementById('telestratorCanvas'));
    renderAnnotationTrack();

    // Fix for MediaRecorder WebM files with missing duration metadata:
    // Seek to a huge time to force browser to compute real duration, then seek back
    if (!isFinite(dur) || dur === 0) {
      var videoEl = _vjsPlayer.tech({ IWillNotUseThisInPlugins: true })?.el_;
      if (videoEl) {
        var onSeekedFix = function() {
          videoEl.removeEventListener('seeked', onSeekedFix);
          videoEl.currentTime = 0;
        };
        videoEl.addEventListener('seeked', onSeekedFix);
        try { videoEl.currentTime = 1e101; } catch(e) {}
      }
    }
  });
  _vjsPlayer.on('durationchange', () => {
    const dur = _vjsPlayer.duration();
    document.getElementById('timelineDuration').textContent = fmtTime(isFinite(dur) ? dur : _currentVideo?.duration_seconds);
  });
}

function onTimeUpdate() {
  if (!_vjsPlayer) return;
  const t = _vjsPlayer.currentTime();
  const rawD = _vjsPlayer.duration();
  const d = isFinite(rawD) && rawD > 0 ? rawD : (_currentVideo?.duration_seconds || 1);
  document.getElementById('timelineProgress').style.width = (t / d * 100) + '%';
  document.getElementById('timelineCurrent').textContent = fmtTime(t);

  // Render telestrator annotations
  telestrator.renderFrame(t);
}

/* ═══════════════════════════════════════════════════════════════════════════
   §2  CLIPPING & TIMELINE
   Timeline markers, annotation track, clips sidebar, clip timeline strip
   ═══════════════════════════════════════════════════════════════════════════ */

/* ═══ Timeline ════════════════════════════════════════════ */
function renderTimelineMarkers() {
  // Remove old markers
  document.querySelectorAll('.timeline-marker').forEach(m => m.remove());
  document.querySelectorAll('.timeline-io-marker').forEach(m => m.remove());
  const bar = document.getElementById('timelineBar');
  const rawDur = _vjsPlayer?.duration() || _currentVideo?.duration_seconds || 1;
  const duration = isFinite(rawDur) && rawDur > 0 ? rawDur : (_currentVideo?.duration_seconds || 1);

  // Clip markers (LTR: left = 0:00)
  _clips.forEach(c => {
    const pct = (c.start_time / duration) * 100;
    const marker = document.createElement('div');
    marker.className = 'timeline-marker ' + (c.rating === 'positive' ? 'positive' : c.rating === 'negative' ? 'negative' : 'neutral');
    marker.style.left = `calc(${pct}% - 2px)`;
    marker.title = `${c.action_type.replace(/_/g, ' ')} (${fmtTime(c.start_time)})`;
    marker.onclick = (e) => { e.stopPropagation(); if (_vjsPlayer) _vjsPlayer.currentTime(c.start_time); };
    bar.appendChild(marker);
  });

  // I/O point markers
  if (_clipInPoint !== null) {
    const pct = (_clipInPoint / duration) * 100;
    const m = document.createElement('div');
    m.className = 'timeline-io-marker io-in';
    m.style.left = `${pct}%`;
    m.title = `In: ${fmtTime(_clipInPoint)}`;
    bar.appendChild(m);
  }
  if (_clipOutPoint !== null) {
    const pct = (_clipOutPoint / duration) * 100;
    const m = document.createElement('div');
    m.className = 'timeline-io-marker io-out';
    m.style.left = `${pct}%`;
    m.title = `Out: ${fmtTime(_clipOutPoint)}`;
    bar.appendChild(m);
  }
  // Highlight region between I/O
  if (_clipInPoint !== null && _clipOutPoint !== null && _clipOutPoint > _clipInPoint) {
    const leftPct = (_clipInPoint / duration) * 100;
    const widthPct = ((_clipOutPoint - _clipInPoint) / duration) * 100;
    const region = document.createElement('div');
    region.className = 'timeline-io-region';
    region.style.left = leftPct + '%';
    region.style.width = widthPct + '%';
    bar.appendChild(region);
  }
}

function seekTimeline(e) {
  if (!_vjsPlayer) return;
  const bar = document.getElementById('timelineBar');
  const rect = bar.getBoundingClientRect();
  const pct = (e.clientX - rect.left) / rect.width; // LTR: left side is 0:00
  const dur = _vjsPlayer.duration();
  if (!isFinite(dur) || dur <= 0) return;
  _vjsPlayer.currentTime(pct * dur);
}

function skipTime(seconds) {
  if (!_vjsPlayer) return;
  const t = _vjsPlayer.currentTime() + seconds;
  _vjsPlayer.currentTime(Math.max(0, Math.min(t, _vjsPlayer.duration())));
}

/* ═══ Annotation Track (resizable / draggable bars) ══════ */
const ANN_COLORS = {
  freehand: '#ef4444', arrow: '#60A5FA',
  text: '#FBBF24',
};
const ANN_LABELS = {
  freehand: '✏️', arrow: '➡️', text: '📝',
};
let _selectedAnnIdx = -1;

const ANN_TYPE_NAMES = {
  get freehand() { return t('scouting.ann.type.freehand'); },
  get arrow() { return t('scouting.ann.type.arrow'); },
  get text() { return t('scouting.ann.type.text'); },
};


function renderAnnotationTrack() {
  const track = document.getElementById('annotationTrack');
  if (!track) return;
  track.innerHTML = '';
  const rawDur = _vjsPlayer?.duration() || _currentVideo?.duration_seconds || 1;
  const duration = isFinite(rawDur) && rawDur > 0 ? rawDur : (_currentVideo?.duration_seconds || 1);
  const anns = telestrator.annotations?.length ? telestrator.annotations : _annotations;
  // Always keep visible (CSS shows empty state placeholder)
  track.style.display = 'block';
  if (!anns || !anns.length) { return; }

  // Group by annotation type
  const groups = {};
  anns.forEach((ann, idx) => {
    const type = ann.annotation_type || 'other';
    if (!groups[type]) groups[type] = [];
    groups[type].push({ ann, idx });
  });

  // Render a row per type
  for (const type of Object.keys(groups)) {
    const color = ANN_COLORS[type] || '#f48c25';
    const label = ANN_LABELS[type] || '•';
    const typeName = ANN_TYPE_NAMES[type] || type;

    const row = document.createElement('div');
    row.className = 'ann-track-row';

    const rowLabel = document.createElement('div');
    rowLabel.className = 'ann-track-label';
    rowLabel.style.color = color;
    rowLabel.textContent = typeName;
    row.appendChild(rowLabel);

    const rowBars = document.createElement('div');
    rowBars.className = 'ann-track-bars';
    row.appendChild(rowBars);

    for (const { ann, idx } of groups[type]) {
      const leftPct = (ann.timestamp / duration) * 100;
      const widthPct = (ann.duration / duration) * 100;
      const selected = idx === _selectedAnnIdx;

      const bar = document.createElement('div');
      bar.className = 'ann-bar' + (selected ? ' selected' : '');
      bar.style.left = leftPct + '%';
      bar.style.width = Math.max(widthPct, 0.8) + '%';
      bar.style.background = color;
      bar.title = `${typeName} — ${fmtTime(ann.timestamp)} → ${fmtTime(ann.timestamp + ann.duration)}`;
      bar.dataset.annIdx = idx;
      // Build annotation bar DOM safely (no innerHTML with user data)
      const handleL = document.createElement('div');
      handleL.className = 'ann-handle ann-handle-left';
      const labelSpan = document.createElement('span');
      labelSpan.className = 'ann-bar-label';
      labelSpan.textContent = label;
      const delBtn = document.createElement('button');
      delBtn.className = 'ann-delete-btn';
      delBtn.title = 'Delete';
      delBtn.textContent = '\u00d7';
      const handleR = document.createElement('div');
      handleR.className = 'ann-handle ann-handle-right';
      bar.appendChild(handleL);
      bar.appendChild(labelSpan);
      bar.appendChild(delBtn);
      bar.appendChild(handleR);

      // Click = select + seek
      bar.addEventListener('click', (e) => {
        if (e.target.classList.contains('ann-handle') || e.target.classList.contains('ann-delete-btn')) return;
        _selectedAnnIdx = idx;
        if (_vjsPlayer) _vjsPlayer.currentTime(ann.timestamp);
        renderAnnotationTrack();
      });

      // Delete button
      bar.querySelector('.ann-delete-btn').addEventListener('click', (e) => {
        e.stopPropagation();
        deleteAnnotation(idx);
      });

      // Drag handles (resize)
      bar.querySelector('.ann-handle-left').addEventListener('mousedown', (e) => startAnnDrag(e, ann, idx, 'left', duration));
      bar.querySelector('.ann-handle-right').addEventListener('mousedown', (e) => startAnnDrag(e, ann, idx, 'right', duration));

      // Middle drag (move freely)
      bar.addEventListener('mousedown', (e) => {
        if (e.target.classList.contains('ann-handle') || e.target.classList.contains('ann-delete-btn')) return;
        startAnnDrag(e, ann, idx, 'move', duration);
      });

      rowBars.appendChild(bar);
    }

    track.appendChild(row);
  }
}

function startAnnDrag(e, ann, idx, mode, videoDuration) {
  e.preventDefault();
  e.stopPropagation();
  _selectedAnnIdx = idx;

  const barEl = e.target.closest('.ann-bar');
  if (!barEl) return;
  const barsContainer = barEl.parentElement;
  const trackRect = barsContainer.getBoundingClientRect();
  const origTimestamp = ann.timestamp;
  const origDuration = ann.duration;
  const startX = e.clientX;
  let moved = false;

  // Highlight selected
  document.querySelectorAll('.ann-bar').forEach(b => b.classList.remove('selected'));
  barEl.classList.add('selected');

  function onMove(ev) {
    moved = true;
    const x = ev.clientX - trackRect.left; // LTR: measure from left
    const timePct = Math.max(0, Math.min(1, x / trackRect.width));
    const timePos = timePct * videoDuration;

    if (mode === 'left') {
      const end = origTimestamp + origDuration;
      const newStart = Math.min(timePos, end - 0.3);
      ann.timestamp = Math.max(0, newStart);
      ann.duration = end - ann.timestamp;
    } else if (mode === 'right') {
      const newEnd = Math.max(timePos, ann.timestamp + 0.3);
      ann.duration = Math.min(newEnd - ann.timestamp, videoDuration - ann.timestamp);
    } else {
      const deltaX = ev.clientX - startX; // LTR: normal delta
      const deltaPct = deltaX / trackRect.width;
      const deltaTime = deltaPct * videoDuration;
      let newStart = origTimestamp + deltaTime;
      newStart = Math.max(0, Math.min(newStart, videoDuration - origDuration));
      ann.timestamp = newStart;
    }

    // Update bar CSS directly (no DOM rebuild during drag)
    const leftPct = (ann.timestamp / videoDuration) * 100;
    const widthPct = (ann.duration / videoDuration) * 100;
    barEl.style.left = leftPct + '%';
    barEl.style.width = Math.max(widthPct, 0.8) + '%';
  }

  async function onUp() {
    document.removeEventListener('mousemove', onMove);
    document.removeEventListener('mouseup', onUp);
    renderAnnotationTrack(); // full rebuild on release
    if (!moved) return;
    try {
      await API.put(`/api/scouting/annotations/${ann.id}`, {
        timestamp: Math.round(ann.timestamp * 100) / 100,
        duration: Math.round(ann.duration * 100) / 100,
      });
    } catch (err) {
      ann.timestamp = origTimestamp;
      ann.duration = origDuration;
      renderAnnotationTrack();
    }
  }

  document.addEventListener('mousemove', onMove);
  document.addEventListener('mouseup', onUp);
}

async function deleteAnnotation(idx) {
  const anns = telestrator.annotations?.length ? telestrator.annotations : _annotations;
  if (!anns || idx < 0 || idx >= anns.length) return;
  const ann = anns[idx];
  try {
    await API.del(`/api/scouting/annotations/${ann.id}`);
    telestrator.annotations = telestrator.annotations.filter(a => a.id !== ann.id);
    _annotations = _annotations.filter(a => a.id !== ann.id);
    _selectedAnnIdx = -1;
    renderAnnotationTrack();
    telestrator.renderFrame(_vjsPlayer ? _vjsPlayer.currentTime() : 0);
    Toast.success(t('scouting.ann.deleted'));
  } catch (e) { Toast.error(t('scouting.ann.delete_failed')); }
}

/* ═══════════════════════════════════════════════════════════════════════════
   §3  ANNOTATIONS & TELESTRATOR
   Keyboard shortcuts, scissors tool, I/O points, annotation copy/paste,
   clip preview, quick tag, drawing toolbar helpers
   ═══════════════════════════════════════════════════════════════════════════ */

/* ═══ Keyboard Shortcuts System (Phase 1.1) ═══════════════ */
document.addEventListener('keydown', (e) => {
  // Guard: skip if user is typing in input fields
  const tag = e.target.tagName;
  if (tag === 'INPUT' || tag === 'TEXTAREA' || tag === 'SELECT') return;
  if (e.target.isContentEditable) return;
  // Guard: skip if a modal is open
  if (document.querySelector('.modal-overlay.active')) return;
  // Guard: only work when analysis view is active (video loaded)
  const inAnalysis = document.getElementById('analysisView')?.classList.contains('active');

  const key = e.key;
  const ctrl = e.ctrlKey || e.metaKey;
  const shift = e.shiftKey;

  // ? — Toggle keyboard shortcuts overlay (works everywhere)
  if (key === '?' || (shift && key === '/')) {
    e.preventDefault();
    toggleShortcutsOverlay();
    return;
  }

  if (!inAnalysis || !_vjsPlayer) return;

  // ── Playback ──
  if (key === ' ') {
    e.preventDefault();
    _vjsPlayer.paused() ? _vjsPlayer.play() : _vjsPlayer.pause();
    return;
  }

  // Arrow left/right: ±5s when playing, ±1 frame when paused
  if (key === 'ArrowLeft' && !ctrl) {
    e.preventDefault();
    if (shift) { skipTime(-1); }
    else if (_vjsPlayer.paused()) { stepFrame(-1); }
    else { skipTime(-5); }
    return;
  }
  if (key === 'ArrowRight' && !ctrl) {
    e.preventDefault();
    if (shift) { skipTime(1); }
    else if (_vjsPlayer.paused()) { stepFrame(1); }
    else { skipTime(5); }
    return;
  }

  // , and . — frame step (always)
  if (key === ',') { e.preventDefault(); _vjsPlayer.pause(); stepFrame(-1); return; }
  if (key === '.') { e.preventDefault(); _vjsPlayer.pause(); stepFrame(1); return; }

  // [ and ] — slower/faster playback rate
  if (key === '[') { e.preventDefault(); cyclePlaybackRate(-1); return; }
  if (key === ']') { e.preventDefault(); cyclePlaybackRate(1); return; }

  // 1-5 — direct playback rate
  if (!ctrl && !shift && key >= '1' && key <= '5') {
    e.preventDefault();
    const rate = PLAYBACK_RATES[parseInt(key) - 1];
    _vjsPlayer.playbackRate(rate);
    return;
  }

  // ── Drawing Tools ──
  if (!ctrl && !shift) {
    const toolMap = { d: 'freehand', a: 'arrow', t: 'text' };
    if (toolMap[key.toLowerCase()]) {
      e.preventDefault();
      const toolBtn = document.querySelector(`.tool-btn[data-tool="${toolMap[key.toLowerCase()]}"]`);
      if (toolBtn) setDrawTool(toolBtn);
      return;
    }
  }

  // Escape — deselect tool / deselect annotation
  if (key === 'Escape') {
    if (telestrator.tool) {
      const activeBtn = document.querySelector('.tool-btn.active');
      if (activeBtn) setDrawTool(activeBtn); // toggle off
    } else {
      _selectedAnnIdx = -1;
      renderAnnotationTrack();
    }
    return;
  }

  // Ctrl+Z — undo annotation
  if (ctrl && key === 'z') {
    e.preventDefault();
    telestrator.undo();
    return;
  }

  // Delete — delete selected annotation
  if (key === 'Delete' && _selectedAnnIdx >= 0) {
    deleteAnnotation(_selectedAnnIdx);
    return;
  }

  // I — set clip In-point
  if (key === 'i' && !ctrl && !shift) {
    e.preventDefault();
    setClipInPoint();
    return;
  }

  // O — set clip Out-point
  if (key === 'o' && !ctrl && !shift) {
    e.preventDefault();
    setClipOutPoint();
    return;
  }

  // Ctrl+C — copy selected annotation(s)
  if (ctrl && key === 'c' && _selectedAnnIdx >= 0) {
    e.preventDefault();
    copyAnnotations();
    return;
  }

  // Ctrl+V — paste annotations
  if (ctrl && key === 'v' && _copiedAnnotations.length) {
    e.preventDefault();
    pasteAnnotations();
    return;
  }

  // + / = — zoom in, - — zoom out, 0 — reset zoom
  if (key === '+' || key === '=') { e.preventDefault(); zoomIn(); return; }
  if (key === '-') { e.preventDefault(); zoomOut(); return; }
  if (key === '0' && !ctrl) { e.preventDefault(); resetZoom(); return; }
});

/* ── Frame-by-frame stepping ── */
function stepFrame(direction) {
  if (!_vjsPlayer) return;
  const t = _vjsPlayer.currentTime() + (direction * FRAME_DURATION);
  _vjsPlayer.currentTime(Math.max(0, Math.min(t, _vjsPlayer.duration())));
}

/* ── Playback rate cycling ── */
function cyclePlaybackRate(direction) {
  if (!_vjsPlayer) return;
  const current = _vjsPlayer.playbackRate();
  let idx = PLAYBACK_RATES.indexOf(current);
  if (idx === -1) idx = PLAYBACK_RATES.findIndex(r => r >= current) || 2;
  idx = Math.max(0, Math.min(PLAYBACK_RATES.length - 1, idx + direction));
  _vjsPlayer.playbackRate(PLAYBACK_RATES[idx]);
}

/* ── Shortcuts overlay toggle ── */
var _shortcutsRelease = null;
function toggleShortcutsOverlay() {
  const overlay = document.getElementById('shortcutsOverlay');
  if (!overlay) return;
  const isOpen = overlay.classList.toggle('active');
  if (isOpen && typeof trapFocus === 'function') {
    _shortcutsRelease = trapFocus(overlay);
  } else if (!isOpen && _shortcutsRelease) {
    _shortcutsRelease();
    _shortcutsRelease = null;
  }
}

/* ═══ Scissors Clip Tool ═══════════════════════════════════ */
function scissorsCut() {
  if (!_vjsPlayer) return;
  const btn = document.getElementById('scissorsBtn');
  const label = document.getElementById('scissorsLabel');

  if (_clipInPoint === null) {
    // First click — set IN point
    setClipInPoint();
    btn.classList.add('scissors-active');
    label.textContent = 'End Clip';
  } else {
    // Second click — set OUT point, pause video, and open clip modal
    setClipOutPoint();
    if (_vjsPlayer) _vjsPlayer.pause();
    if (_clipOutPoint > _clipInPoint) {
      createClipFromIO();
    } else {
      Toast.error('End point must be after start point');
    }
    btn.classList.remove('scissors-active');
    label.textContent = 'Clip';
  }
}

/* ═══ Clip In/Out Points (Phase 1.6) ═══════════════════════ */
function setClipInPoint() {
  if (!_vjsPlayer) return;
  _clipInPoint = _vjsPlayer.currentTime();
  Toast.success(`${t('scouting.io.in')}: ${fmtTime(_clipInPoint)}`);
  _updateIOBar();
  renderTimelineMarkers();
}

function setClipOutPoint() {
  if (!_vjsPlayer) return;
  _clipOutPoint = _vjsPlayer.currentTime();
  Toast.success(`${t('scouting.io.out')}: ${fmtTime(_clipOutPoint)}`);
  _updateIOBar();
  renderTimelineMarkers();
}

function clearIOPoints() {
  _clipInPoint = null;
  _clipOutPoint = null;
  // Reset scissors button
  const sb = document.getElementById('scissorsBtn');
  const sl = document.getElementById('scissorsLabel');
  if (sb) sb.classList.remove('scissors-active');
  if (sl) sl.textContent = 'Clip';
  _updateIOBar();
  renderTimelineMarkers();
}

function _updateIOBar() {
  const bar = document.getElementById('clipIOBar');
  if (!bar) return;
  if (_clipInPoint !== null || _clipOutPoint !== null) {
    const inText = _clipInPoint !== null ? fmtTime(_clipInPoint) : '--:--';
    const outText = _clipOutPoint !== null ? fmtTime(_clipOutPoint) : '--:--';
    const canCreate = _clipInPoint !== null && _clipOutPoint !== null && _clipOutPoint > _clipInPoint;
    bar.innerHTML = `
      <span class="io-label">${t('scouting.io.in')} <strong>${inText}</strong></span>
      <span class="io-separator">→</span>
      <span class="io-label">${t('scouting.io.out')} <strong>${outText}</strong></span>
      ${canCreate ? `<span class="io-duration">${Math.round(_clipOutPoint - _clipInPoint)}s</span>` : ''}
      ${canCreate ? `<button class="btn btn-primary btn-sm" onclick="createClipFromIO()">${t('scouting.io.create_clip')}</button>` : ''}
      <button class="btn-icon io-clear" onclick="clearIOPoints()" title="Clear"><span class="material-symbols-outlined">close</span></button>`;
    bar.style.display = 'flex';
  } else {
    bar.style.display = 'none';
  }
}

function createClipFromIO() {
  if (_clipInPoint === null || _clipOutPoint === null) return;
  if (!_vjsPlayer) return;

  document.getElementById('clipStart').value = fmtTime(_clipInPoint);
  document.getElementById('clipEnd').value = fmtTime(_clipOutPoint);
  document.getElementById('clipAction').value = 'other';
  document.getElementById('clipNotes').value = '';
  var customTagInput = document.getElementById('clipCustomTag');
  if (customTagInput) { customTagInput.value = ''; }
  var customTagGroup = document.getElementById('customTagGroup');
  if (customTagGroup) { customTagGroup.style.display = ''; }
  _clipRating = null;
  document.getElementById('ratingPos').classList.remove('active');
  document.getElementById('ratingNeg').classList.remove('active');

  _updateClipDuration();
  openModal('clipModal');
}

/* ═══ Annotation Copy/Paste (Phase 2.3 prep) ══════════════ */
let _copiedAnnotations = [];

function copyAnnotations() {
  const anns = telestrator.annotations?.length ? telestrator.annotations : _annotations;
  if (_selectedAnnIdx >= 0 && _selectedAnnIdx < anns.length) {
    const ann = anns[_selectedAnnIdx];
    _copiedAnnotations = [JSON.parse(JSON.stringify(ann))];
    Toast.info(t('scouting.ann.copied'));
  }
}

async function pasteAnnotations() {
  if (!_copiedAnnotations.length || !_currentVideo || !_vjsPlayer) return;
  const currentTime = _vjsPlayer.currentTime();
  for (const ann of _copiedAnnotations) {
    try {
      const res = await API.post(`/api/scouting/videos/${_currentVideo.id}/annotations`, {
        annotation_type: ann.annotation_type,
        timestamp: currentTime,
        duration: ann.duration,
        stroke_data: ann.stroke_data,
        color: ann.color,
        stroke_width: ann.stroke_width,
        text_content: ann.text_content || null,
      });
      telestrator.annotations.push(res.data);
    } catch (e) { console.error('Paste error:', e); }
  }
  renderAnnotationTrack();
  telestrator.renderFrame(currentTime);
  Toast.success(t('scouting.ann.pasted'));
}

/* ═══ Clip Trim Preview (Phase 1.7) ═══════════════════════ */
function previewClip() {
  if (!_vjsPlayer) return;
  const start = parseTimeInput(document.getElementById('clipStart').value);
  const end = parseTimeInput(document.getElementById('clipEnd').value);
  if (end <= start) { Toast.error(t('scouting.clips.invalid_range')); return; }

  const previewBtn = document.getElementById('clipPreviewBtn');
  if (_clipPreviewActive) {
    // Stop preview
    _clipPreviewActive = false;
    _vjsPlayer.pause();
    if (previewBtn) previewBtn.textContent = t('scouting.clip.preview');
    return;
  }

  _clipPreviewActive = true;
  if (previewBtn) previewBtn.textContent = t('scouting.clip.stop');
  _vjsPlayer.currentTime(start);
  _vjsPlayer.play();

  const onUpdate = () => {
    if (!_clipPreviewActive) { _vjsPlayer.off('timeupdate', onUpdate); return; }
    if (_vjsPlayer.currentTime() >= end) {
      _vjsPlayer.currentTime(start); // loop
    }
  };
  _vjsPlayer.on('timeupdate', onUpdate);
}

function stopClipPreview() {
  _clipPreviewActive = false;
  const previewBtn = document.getElementById('clipPreviewBtn');
  if (previewBtn) previewBtn.textContent = t('scouting.clip.preview');
}

/* ═══ Clips Sidebar ═══════════════════════════════════════ */
function renderClipsSidebar() {
  const el = document.getElementById('clipsList');
  document.getElementById('clipCount').textContent = `(${_clips.length})`;

  if (_clipSidebarTab === 'playlists') {
    renderPlaylistsSidebar();
    renderClipTimeline();
    return;
  }

  // Filter chips
  const chipBox = document.getElementById('clipFilterChips');
  const compileBtn = document.getElementById('compileClipsBtn');
  const shareClipsBtn = document.getElementById('shareClipsBtn');
  if (_clips.length >= 2) {
    const types = [...new Set(_clips.map(c => c.action_type))];
    if (types.length > 1) {
      chipBox.style.display = '';
      chipBox.innerHTML = `<button class="clip-filter-chip${!_clipFilterType ? ' active' : ''}" onclick="setClipFilter('')">${t('scouting.compile.filter_all')}</button>` +
        types.map(ty => {
          const label = ACTION_TYPES.find(a => a.value === ty)?.label || ty;
          return `<button class="clip-filter-chip${_clipFilterType === ty ? ' active' : ''}" onclick="setClipFilter('${ty}')">${esc(label)}</button>`;
        }).join('');
    } else { chipBox.style.display = 'none'; }
    compileBtn.style.display = '';
    if (shareClipsBtn) shareClipsBtn.style.display = '';
  } else {
    chipBox.style.display = 'none';
    if (compileBtn) compileBtn.style.display = 'none';
    if (shareClipsBtn) shareClipsBtn.style.display = 'none';
  }

  if (!_clips.length) {
    el.innerHTML = `<p style="color:var(--text-muted);text-align:center;font-size:0.82rem;padding:var(--sp-4);">${t('scouting.clips.empty')}</p>`;
    renderClipTimeline();
    return;
  }

  const filtered = _clipFilterType ? _clips.filter(c => c.action_type === _clipFilterType) : _clips;

  el.innerHTML = filtered.map(c => {
    const action = ACTION_TYPES.find(a => a.value === c.action_type)?.label || c.action_type;
    const rating = c.rating === 'positive' ? '👍' : c.rating === 'negative' ? '👎' : '';
    const selected = _selectedClipIds.has(c.id) ? ' batch-selected' : '';
    return `<div class="clip-card${selected}" onclick="jumpToClip(${c.id})" data-clip-id="${c.id}">
      <div class="clip-card-header">
        <input type="checkbox" class="clip-batch-check" ${_selectedClipIds.has(c.id) ? 'checked' : ''} onclick="toggleClipSelection(${c.id}, event)">
        <span class="clip-card-action">${esc(action)} <span class="clip-card-rating">${rating}</span></span>
        <button class="btn-icon clip-share-btn" title="Share" onclick="event.stopPropagation();shareClip(${c.id})"><span class="material-symbols-outlined" style="font-size:1rem;">share</span></button>
        <button class="btn-icon" style="font-size:0.7rem;" onclick="event.stopPropagation();deleteClip(${c.id})"><span class="material-symbols-outlined" style="font-size:1rem;">delete</span></button>
      </div>
      <div class="clip-card-time">${fmtTime(c.start_time)} — ${fmtTime(c.end_time)}</div>
      <div class="clip-watch-count">${t('scouting.clips.watched', { count: c.watch_count || 0 })}</div>
    </div>`;
  }).join('');

  renderClipTimeline();
}

function setClipFilter(type) {
  _clipFilterType = type;
  renderClipsSidebar();
}

/* ═══ Clip Timeline Strip (Clipchamp-style) ═══════════════ */

var _tlDragIndex = null; // timeline drag state

// Persist card positions (which clip they come after) by updating their config
function persistCardPositions() {
  if (!_currentVideo || !_currentVideo.id) return;
  // For each card, find the clip that comes immediately before it in _compileTimeline
  // Store after_clip_id in the card's config (or null = start of timeline)
  _compileTimeline.forEach(function(item, idx) {
    if (item.type === 'clip') return;
    // Find nearest clip before this card
    var afterClipId = null;
    for (var j = idx - 1; j >= 0; j--) {
      if (_compileTimeline[j].type === 'clip') {
        afterClipId = _compileTimeline[j].clip.id;
        break;
      }
    }
    // Only update if changed
    if (item.config.after_clip_id !== afterClipId) {
      item.config.after_clip_id = afterClipId;
      if (item.dbId) {
        API.put('/api/scouting/compile-cards/' + item.dbId, {
          config: item.config
        }).catch(function(e) { console.warn('[Compile Cards] position update failed:', e); });
      }
    }
  });
}

function renderClipTimeline() {
  var strip = document.getElementById('clipTimelineStrip');
  var filtersEl = document.getElementById('timelineFilters');
  var compileBtn = document.getElementById('timelineCompileBtn');
  if (!strip) return;

  // Build timeline: only rebuild if clips changed (preserve drag order)
  var hasClips = _compileTimeline && _compileTimeline.some(function(item) { return item.type === 'clip'; });
  if (!hasClips || !_compileTimeline.length) {
    var existingCards = _compileTimeline ? _compileTimeline.filter(function(item) { return item.type !== 'clip'; }) : [];
    _compileTimeline = existingCards.concat(_clips.map(function(c) { return { type: 'clip', clip: c }; }));
  } else {
    // Sync: add new clips not yet in timeline, remove deleted clips
    var timelineClipIds = {};
    _compileTimeline.forEach(function(item) { if (item.type === 'clip') timelineClipIds[item.clip.id] = true; });
    _clips.forEach(function(c) {
      if (!timelineClipIds[c.id]) _compileTimeline.push({ type: 'clip', clip: c });
    });
    var currentClipIds = {};
    _clips.forEach(function(c) { currentClipIds[c.id] = true; });
    _compileTimeline = _compileTimeline.filter(function(item) {
      return item.type !== 'clip' || currentClipIds[item.clip.id];
    });
  }

  strip.textContent = '';

  if (_compileTimeline.length === 0) {
    var empty = document.createElement('div');
    empty.className = 'clip-timeline-empty';
    empty.textContent = 'No clips yet. Use the Clip tool to create clips.';
    strip.appendChild(empty);
    if (compileBtn) compileBtn.disabled = true;
    return;
  }

  var clipCount = _compileTimeline.filter(function(i) { return i.type === 'clip'; }).length;
  if (compileBtn) compileBtn.disabled = clipCount < 2;

  // Render all timeline items
  _compileTimeline.forEach(function(item, idx) {
    var thumb = document.createElement('div');
    thumb.className = 'clip-thumb' + (item.type !== 'clip' ? ' card-item' : '');
    thumb.setAttribute('draggable', 'true');
    thumb.setAttribute('data-tl-index', idx);

    // Drag events
    thumb.addEventListener('dragstart', function(e) {
      _tlDragIndex = idx;
      thumb.classList.add('dragging');
      e.dataTransfer.effectAllowed = 'move';
    });
    thumb.addEventListener('dragend', function() {
      thumb.classList.remove('dragging');
      _tlDragIndex = null;
      document.querySelectorAll('.clip-thumb.drag-over').forEach(function(el) { el.classList.remove('drag-over'); });
    });
    thumb.addEventListener('dragover', function(e) {
      e.preventDefault();
      e.dataTransfer.dropEffect = 'move';
      thumb.classList.add('drag-over');
    });
    thumb.addEventListener('dragleave', function() {
      thumb.classList.remove('drag-over');
    });
    thumb.addEventListener('drop', function(e) {
      e.preventDefault();
      thumb.classList.remove('drag-over');
      if (_tlDragIndex === null || _tlDragIndex === idx) return;
      // Move item from _tlDragIndex to idx
      var moved = _compileTimeline.splice(_tlDragIndex, 1)[0];
      _compileTimeline.splice(idx, 0, moved);
      _tlDragIndex = null;
      persistCardPositions();
      renderClipTimeline();
    });

    if (item.type === 'clip') {
      var clip = item.clip;
      thumb.setAttribute('data-clip-id', clip.id);
      thumb.onclick = function(e) { if (!e.defaultPrevented) showClipDetail(clip, e, thumb); };

      // Thumbnail canvas
      var canvas = document.createElement('canvas');
      canvas.className = 'clip-thumb-canvas';
      canvas.width = 160;
      canvas.height = 90;
      thumb.appendChild(canvas);
      generateClipThumbnail(clip, canvas);

      // Action type badge
      var badge = document.createElement('div');
      badge.className = 'clip-thumb-badge';
      badge.textContent = (clip.action_type || 'other').replace(/_/g, ' ');
      thumb.appendChild(badge);

      // Info
      var info = document.createElement('div');
      info.className = 'clip-thumb-info';
      var ctag = document.createElement('span');
      ctag.className = 'clip-thumb-tag';
      ctag.textContent = (clip.action_type || 'other').replace(/_/g, ' ');
      info.appendChild(ctag);
      var timeSpan = document.createElement('span');
      timeSpan.className = 'clip-thumb-time';
      timeSpan.textContent = fmtTime(clip.start_time) + ' \u2014 ' + fmtTime(clip.end_time);
      info.appendChild(timeSpan);
      thumb.appendChild(info);

    } else {
      // Card item (game_intro or player_card)
      var imgDiv = document.createElement('div');
      imgDiv.className = 'clip-thumb-img';
      imgDiv.style.cssText = 'display:flex;align-items:center;justify-content:center;height:64px;';
      var icon = document.createElement('span');
      icon.className = 'material-symbols-outlined';
      icon.style.fontSize = '1.5rem';
      icon.textContent = item.type === 'game_intro' ? 'slideshow' : 'person';
      imgDiv.appendChild(icon);
      thumb.appendChild(imgDiv);

      var info = document.createElement('div');
      info.className = 'clip-thumb-info';
      var cardTag = document.createElement('span');
      cardTag.className = 'clip-thumb-tag';
      cardTag.textContent = item.type === 'game_intro' ? 'Game Intro' : (item.config && item.config.name ? item.config.name : 'Player Card');
      info.appendChild(cardTag);
      var dur = document.createElement('span');
      dur.className = 'clip-thumb-time';
      dur.textContent = '5 seconds';
      info.appendChild(dur);
      thumb.appendChild(info);

      // Remove button for cards
      var removeBtn = document.createElement('button');
      removeBtn.className = 'tl-remove';
      removeBtn.style.cssText = 'position:absolute;top:2px;right:2px;background:rgba(0,0,0,0.6);border:none;color:#fff;border-radius:50%;width:18px;height:18px;font-size:0.65rem;cursor:pointer;display:flex;align-items:center;justify-content:center;';
      removeBtn.textContent = '\u2715';
      removeBtn.onclick = function(e) {
        e.stopPropagation();
        e.preventDefault();
        var removed = _compileTimeline.splice(idx, 1)[0];
        renderClipTimeline();
        // Delete from DB
        if (removed && removed.dbId) {
          API.del('/api/scouting/compile-cards/' + removed.dbId).catch(function(err) {
            console.warn('[Compile Cards] delete failed:', err);
          });
        }
      };
      thumb.appendChild(removeBtn);
    }

    strip.appendChild(thumb);
  });

  // Render filter chips
  if (filtersEl) {
    filtersEl.textContent = '';
    var types = [];
    _clips.forEach(function(c) {
      if (types.indexOf(c.action_type) === -1) types.push(c.action_type);
    });
    if (types.length > 1) {
      var allChip = document.createElement('button');
      allChip.className = 'timeline-filter-chip active';
      allChip.textContent = 'All';
      allChip.onclick = function() { filterTimeline(''); };
      filtersEl.appendChild(allChip);
      types.forEach(function(ty) {
        var chip = document.createElement('button');
        chip.className = 'timeline-filter-chip';
        chip.textContent = (ty || 'other').replace(/_/g, ' ');
        chip.onclick = function() { filterTimeline(ty); };
        filtersEl.appendChild(chip);
      });
    }
  }
}

function generateClipThumbnail(clip, canvas) {
  // Try to capture a frame from the video at the clip's start time
  if (!_vjsPlayer) return;
  try {
    var videoEl = _vjsPlayer.tech ? _vjsPlayer.tech({ IWillNotUseThisInPlugins: true }) : null;
    videoEl = videoEl ? videoEl.el_ : null;
    if (!videoEl || videoEl.readyState < 2) {
      // Video not ready — draw placeholder
      var ctx = canvas.getContext('2d');
      ctx.fillStyle = '#1a2332';
      ctx.fillRect(0, 0, canvas.width, canvas.height);
      ctx.fillStyle = '#5a6372';
      ctx.font = '10px sans-serif';
      ctx.textAlign = 'center';
      ctx.fillText(fmtTime(clip.start_time), canvas.width / 2, canvas.height / 2 + 4);
      return;
    }
    // Draw current video frame as thumbnail (approximate — uses current frame)
    var ctx = canvas.getContext('2d');
    ctx.drawImage(videoEl, 0, 0, canvas.width, canvas.height);
  } catch (e) {
    var ctx2 = canvas.getContext('2d');
    ctx2.fillStyle = '#1a2332';
    ctx2.fillRect(0, 0, canvas.width, canvas.height);
  }
}

function showClipDetail(clip, event, thumbEl) {
  event.stopPropagation();
  var popup = document.getElementById('clipDetailPopup');
  if (!popup) return;

  // Remove active from all thumbs
  document.querySelectorAll('.clip-thumb.active').forEach(function(t) { t.classList.remove('active'); });
  thumbEl.classList.add('active');

  // Build detail content
  popup.textContent = '';

  var header = document.createElement('div');
  header.className = 'clip-detail-header';
  var tagEl = document.createElement('span');
  tagEl.className = 'clip-detail-tag';
  tagEl.textContent = (clip.action_type || 'other').replace(/_/g, ' ');
  header.appendChild(tagEl);
  var timeEl = document.createElement('span');
  timeEl.textContent = fmtTime(clip.start_time) + ' \u2014 ' + fmtTime(clip.end_time);
  timeEl.style.color = 'var(--text-muted)';
  header.appendChild(timeEl);
  if (clip.rating) {
    var ratingEl = document.createElement('span');
    ratingEl.textContent = clip.rating === 'positive' ? '\uD83D\uDC4D' : '\uD83D\uDC4E';
    header.appendChild(ratingEl);
  }
  popup.appendChild(header);

  if (clip.notes) {
    var notesEl = document.createElement('div');
    notesEl.style.cssText = 'color:var(--text-muted);font-size:0.78rem;margin-bottom:4px;';
    notesEl.textContent = clip.notes;
    popup.appendChild(notesEl);
  }

  var actions = document.createElement('div');
  actions.className = 'clip-detail-actions';

  var playBtn = document.createElement('button');
  playBtn.textContent = '\u25B6 Play';
  playBtn.onclick = function() { if (_vjsPlayer) { _vjsPlayer.currentTime(clip.start_time); _vjsPlayer.play(); } hideClipDetail(); };
  actions.appendChild(playBtn);

  var shareBtn = document.createElement('button');
  shareBtn.textContent = '\uD83D\uDCE4 Share';
  shareBtn.onclick = function() { shareClip(clip.id); hideClipDetail(); };
  actions.appendChild(shareBtn);

  var delBtn = document.createElement('button');
  delBtn.className = 'danger';
  delBtn.textContent = '\uD83D\uDDD1\uFE0F Delete';
  delBtn.onclick = function() { deleteClip(clip.id); hideClipDetail(); };
  actions.appendChild(delBtn);

  popup.appendChild(actions);

  // Position popup above the clicked thumbnail
  var thumbRect = thumbEl.getBoundingClientRect();
  popup.style.display = 'block';
  popup.style.position = 'fixed';
  popup.style.bottom = (window.innerHeight - thumbRect.top + 8) + 'px';
  popup.style.left = (thumbRect.left + thumbRect.width / 2) + 'px';
  popup.style.transform = 'translateX(-50%)';
}

function hideClipDetail() {
  var popup = document.getElementById('clipDetailPopup');
  if (popup) popup.style.display = 'none';
  document.querySelectorAll('.clip-thumb.active').forEach(function(t) { t.classList.remove('active'); });
}

function filterTimeline(actionType) {
  // Highlight the active filter chip
  document.querySelectorAll('.timeline-filter-chip').forEach(function(chip) {
    chip.classList.toggle('active', actionType === '' ? chip.textContent === 'All' : chip.textContent.replace(/ /g, '_') === actionType);
  });
  // Show/hide clips in timeline
  document.querySelectorAll('.clip-thumb[data-clip-id]').forEach(function(thumb) {
    if (!actionType) { thumb.style.display = ''; return; }
    var clipId = parseInt(thumb.getAttribute('data-clip-id'));
    var clip = _clips.find(function(c) { return c.id === clipId; });
    thumb.style.display = (clip && clip.action_type === actionType) ? '' : 'none';
  });
}

// Close detail popup when clicking outside
document.addEventListener('click', function(e) {
  if (!e.target.closest('.clip-thumb') && !e.target.closest('.clip-detail-popup')) {
    hideClipDetail();
  }
});

/* ═══════════════════════════════════════════════════════════════════════════
   §4  EXPORT & COMPILATION
   Compile modal, game intro cards, player cards, canvas rendering,
   S3 upload, clip export with burn-in annotations
   ═══════════════════════════════════════════════════════════════════════════ */

/* ═══ Clip Compilation ═══════════════════════════════════ */
let _compiling = false;
let _compiledNewVideoId = null;
let _compileOriginalVideoId = null;
let _compileTimeline = []; // Array of {type:'clip'|'game_intro'|'player_card', ...data}
let _compileSelectedIds = new Set();
let _draggedTimelineIndex = null;

// Temp state for card creators
let _introTeamLogoDataUrl = null;
let _introOpponentLogoDataUrl = null;
let _pcPhotoDataUrl = null;
let _pcTeamLogoDataUrl = null;

function openCompileModal() {
  // --- Hybrid Video Architecture — block compile for YouTube videos ---
  if (_currentVideo?.source_type === 'external') {
    Toast.error('Compile Video is not available for external/YouTube videos. Upload a video file to use this feature.');
    return;
  }
  if (_clips.length < 2) { Toast.error(t('scouting.compile.min_clips')); return; }
  // Default title
  document.getElementById('compileTitle').value = `${t('scouting.compile.btn')} — ${_currentVideo?.title || ''}`.trim();
  // Populate filter dropdown with available action_types
  const filterSel = document.getElementById('compileFilter');
  const types = [...new Set(_clips.map(c => c.action_type))];
  filterSel.innerHTML = `<option value="">${t('scouting.compile.filter_all')}</option>` +
    types.map(ty => {
      const label = ACTION_TYPES.find(a => a.value === ty)?.label || ty;
      return `<option value="${ty}">${esc(label)}</option>`;
    }).join('');
  // Reset state
  document.getElementById('compileSortBy').value = 'chrono';
  document.getElementById('compileFilter').value = '';
  document.getElementById('compileProgress').style.display = 'none';
  document.getElementById('compileGenerateBtn').disabled = false;
  // Use the existing timeline order from the bottom strip — preserves user's drag-drop reordering
  _compileSelectedIds = new Set(_clips.map(c => c.id));
  // Sync: ensure all current clips are in the timeline (add new ones, remove deleted ones)
  var timelineClipIds = {};
  _compileTimeline.forEach(function(item) { if (item.type === 'clip') timelineClipIds[item.clip.id] = true; });
  _clips.forEach(function(c) {
    if (!timelineClipIds[c.id]) _compileTimeline.push({ type: 'clip', clip: c });
  });
  var currentClipIds = {};
  _clips.forEach(function(c) { currentClipIds[c.id] = true; });
  _compileTimeline = _compileTimeline.filter(function(item) {
    return item.type !== 'clip' || currentClipIds[item.clip.id];
  });
  renderCompileTimeline();
  openModal('compileModal');
}

function _sortCompileClips(clips, sortBy) {
  const sorted = [...clips];
  if (sortBy === 'action') {
    sorted.sort((a, b) => a.action_type.localeCompare(b.action_type) || a.start_time - b.start_time);
  } else if (sortBy === 'rating') {
    const order = { positive: 0, negative: 2 };
    sorted.sort((a, b) => (order[a.rating] ?? 1) - (order[b.rating] ?? 1) || a.start_time - b.start_time);
  } else {
    sorted.sort((a, b) => a.start_time - b.start_time);
  }
  return sorted;
}

function renderCompileTimeline() {
  const sortBy = document.getElementById('compileSortBy') ? document.getElementById('compileSortBy').value : 'chrono';
  const filterType = document.getElementById('compileFilter') ? document.getElementById('compileFilter').value : '';

  // PRESERVE user's drag order from the bottom timeline strip
  // Only apply sort if user explicitly changed sort dropdown (sortBy !== 'chrono' === 'manual')
  // For now, ALWAYS preserve the existing order set by the bottom timeline
  // Apply filter without rebuilding the order
  // (sort/filter dropdowns are kept for backward compat but don't reorder unless user uses them)

  const el = document.getElementById('compileTimeline');
  if (!el) return;
  el.textContent = ''; // Clear

  _compileTimeline.forEach(function(item, idx) {
    const li = document.createElement('li');
    li.className = 'compile-timeline-item';
    li.setAttribute('draggable', 'true');
    li.dataset.idx = idx;

    // Drag handle
    const handle = document.createElement('span');
    handle.className = 'drag-handle';
    handle.textContent = '\u2630';
    li.appendChild(handle);

    // Type icon
    const icon = document.createElement('span');
    icon.className = 'tl-icon';
    if (item.type === 'clip') {
      icon.classList.add('clip');
      icon.textContent = '\uD83C\uDFAC';
    } else if (item.type === 'game_intro') {
      icon.classList.add('intro');
      icon.textContent = '\uD83C\uDFC0';
    } else if (item.type === 'player_card') {
      icon.classList.add('player');
      icon.textContent = '\uD83D\uDC64';
    }
    li.appendChild(icon);

    // Info
    const info = document.createElement('span');
    info.className = 'tl-info';
    if (item.type === 'clip') {
      const action = ACTION_TYPES.find(a => a.value === item.clip.action_type)?.label || item.clip.action_type;
      const rating = item.clip.rating === 'positive' ? ' \uD83D\uDC4D' : item.clip.rating === 'negative' ? ' \uD83D\uDC4E' : '';
      const strong = document.createElement('strong');
      strong.textContent = fmtTime(item.clip.start_time) + '\u2013' + fmtTime(item.clip.end_time);
      info.appendChild(strong);
      info.appendChild(document.createTextNode(' ' + action + rating));
    } else if (item.type === 'game_intro') {
      const strong = document.createElement('strong');
      strong.textContent = 'Game Intro';
      info.appendChild(strong);
      info.appendChild(document.createTextNode(' ' + (item.config.team_name || '') + ' vs ' + (item.config.opponent_name || '')));
    } else if (item.type === 'player_card') {
      const strong = document.createElement('strong');
      strong.textContent = 'Player Card';
      info.appendChild(strong);
      info.appendChild(document.createTextNode(' ' + (item.config.name || '') + ' \u2014 ' + (item.config.position || '')));
    }
    li.appendChild(info);

    // Remove button
    const removeBtn = document.createElement('button');
    removeBtn.className = 'tl-remove';
    removeBtn.textContent = '\u2715';
    removeBtn.addEventListener('click', function(e) {
      e.stopPropagation();
      _compileTimeline.splice(idx, 1);
      if (item.type === 'clip') {
        _compileSelectedIds.delete(item.clip.id);
      }
      renderCompileTimeline();
    });
    li.appendChild(removeBtn);

    // Drag events
    li.addEventListener('dragstart', function(e) {
      _draggedTimelineIndex = idx;
      li.classList.add('dragging');
      e.dataTransfer.effectAllowed = 'move';
      e.dataTransfer.setData('text/plain', idx.toString());
    });
    li.addEventListener('dragend', function() {
      li.classList.remove('dragging');
      _draggedTimelineIndex = null;
      el.querySelectorAll('.compile-timeline-item').forEach(function(item) {
        item.classList.remove('drag-over');
      });
    });
    li.addEventListener('dragover', function(e) {
      e.preventDefault();
      e.dataTransfer.dropEffect = 'move';
      li.classList.add('drag-over');
    });
    li.addEventListener('dragleave', function() {
      li.classList.remove('drag-over');
    });
    li.addEventListener('drop', function(e) {
      e.preventDefault();
      li.classList.remove('drag-over');
      if (_draggedTimelineIndex === null || _draggedTimelineIndex === idx) return;
      var moved = _compileTimeline.splice(_draggedTimelineIndex, 1)[0];
      _compileTimeline.splice(idx, 0, moved);
      _draggedTimelineIndex = null;
      renderCompileTimeline();
    });

    el.appendChild(li);
  });

  // Summary
  const totalDur = _compileTimeline.reduce(function(s, item) {
    if (item.type === 'clip') return s + (item.clip.end_time - item.clip.start_time);
    return s + 5;
  }, 0);
  const clipCount = _compileTimeline.filter(function(item) { return item.type === 'clip'; }).length;
  const cardCount = _compileTimeline.length - clipCount;
  var summaryParts = clipCount + ' clips';
  if (cardCount > 0) summaryParts += ', ' + cardCount + ' card' + (cardCount > 1 ? 's' : '');
  summaryParts += ', ' + fmtTime(totalDur);
  document.getElementById('compileSummary').textContent = summaryParts;
}

function compileToggleClip(clipId, checked) {
  if (checked) {
    _compileSelectedIds.add(clipId);
    var clip = _clips.find(function(c) { return c.id === clipId; });
    if (clip && !_compileTimeline.find(function(item) { return item.type === 'clip' && item.clip.id === clipId; })) {
      _compileTimeline.push({ type: 'clip', clip: clip });
    }
  } else {
    _compileSelectedIds.delete(clipId);
    _compileTimeline = _compileTimeline.filter(function(item) { return !(item.type === 'clip' && item.clip.id === clipId); });
  }
  renderCompileTimeline();
}

function compileToggleAll(selectAll) {
  const filterType = document.getElementById('compileFilter').value;
  const clips = filterType ? _clips.filter(c => c.action_type === filterType) : _clips;
  if (selectAll) {
    clips.forEach(function(c) {
      _compileSelectedIds.add(c.id);
      if (!_compileTimeline.find(function(item) { return item.type === 'clip' && item.clip.id === c.id; })) {
        _compileTimeline.push({ type: 'clip', clip: c });
      }
    });
  } else {
    var idsToRemove = new Set(clips.map(function(c) { return c.id; }));
    clips.forEach(function(c) { _compileSelectedIds.delete(c.id); });
    _compileTimeline = _compileTimeline.filter(function(item) { return !(item.type === 'clip' && idsToRemove.has(item.clip.id)); });
  }
  renderCompileTimeline();
}

/* ═══ Game Intro Card Creator ═══════════════════════════ */
function openGameIntroCreator() {
  _introTeamLogoDataUrl = null;
  _introOpponentLogoDataUrl = null;
  document.getElementById('introTeamName').value = '';
  document.getElementById('introOpponentName').value = '';
  document.getElementById('introLeague').value = '';
  document.getElementById('introGameDate').value = '';
  document.getElementById('introTintColor').value = '#1a237e';
  document.getElementById('introTintOpacity').value = 35;
  document.getElementById('introTintOpacityLabel').textContent = '35%';
  document.getElementById('introCoachNotes').value = '';
  document.getElementById('introTeamLogo').value = '';
  document.getElementById('introOpponentLogo').value = '';
  document.getElementById('introTeamLogoPreview').textContent = '';
  var teamIcon = document.createElement('span');
  teamIcon.className = 'material-symbols-outlined';
  teamIcon.style.cssText = 'color:var(--text-muted);font-size:1.2rem;';
  teamIcon.textContent = 'add_photo_alternate';
  document.getElementById('introTeamLogoPreview').appendChild(teamIcon);
  document.getElementById('introOpponentLogoPreview').textContent = '';
  var oppIcon = teamIcon.cloneNode(true);
  document.getElementById('introOpponentLogoPreview').appendChild(oppIcon);
  previewGameIntro();
  openModal('gameIntroCreatorModal');
}

function onIntroLogoChange(which, input) {
  if (!input.files || !input.files[0]) return;
  var reader = new FileReader();
  reader.onload = function(e) {
    var dataUrl = e.target.result;
    var previewEl;
    if (which === 'team') {
      _introTeamLogoDataUrl = dataUrl;
      previewEl = document.getElementById('introTeamLogoPreview');
    } else {
      _introOpponentLogoDataUrl = dataUrl;
      previewEl = document.getElementById('introOpponentLogoPreview');
    }
    previewEl.textContent = '';
    var img = document.createElement('img');
    img.src = dataUrl;
    img.alt = which === 'team' ? 'Team logo' : 'Opponent logo';
    previewEl.appendChild(img);
    previewGameIntro();
  };
  reader.readAsDataURL(input.files[0]);
}

var _arenaBgImg = null;
var _arenaBgLoaded = false;
(function() {
  _arenaBgImg = new Image();
  _arenaBgImg.onload = function() {
    _arenaBgLoaded = true;
    if (document.getElementById('gameIntroPreviewCanvas')) previewGameIntro();
  };
  _arenaBgImg.src = '/static/img/arena_bg.jpg';
})();

function previewGameIntro() {
  var canvas = document.getElementById('gameIntroPreviewCanvas');
  if (!canvas) return;
  var ctx = canvas.getContext('2d');
  var w = canvas.width, h = canvas.height;
  var tintColor = document.getElementById('introTintColor').value || '#1a237e';
  var tintOpacity = (parseInt(document.getElementById('introTintOpacity').value, 10) || 35) / 100;
  var teamName = document.getElementById('introTeamName').value || 'Team';
  var opponentName = document.getElementById('introOpponentName').value || 'Opponent';
  var league = document.getElementById('introLeague').value || '';
  var gameDate = document.getElementById('introGameDate').value || '';
  var coachNotes = document.getElementById('introCoachNotes').value || '';

  // Dark base
  ctx.fillStyle = '#0a0a12';
  ctx.fillRect(0, 0, w, h);

  // Arena background at 35% max exposure
  if (_arenaBgLoaded && _arenaBgImg) {
    ctx.save();
    ctx.globalAlpha = 0.35;
    var imgRatio = _arenaBgImg.width / _arenaBgImg.height;
    var canvasRatio = w / h;
    var sx = 0, sy = 0, sw = _arenaBgImg.width, sh = _arenaBgImg.height;
    if (imgRatio > canvasRatio) {
      sw = _arenaBgImg.height * canvasRatio;
      sx = (_arenaBgImg.width - sw) / 2;
    } else {
      sh = _arenaBgImg.width / canvasRatio;
      sy = (_arenaBgImg.height - sh) / 2;
    }
    ctx.drawImage(_arenaBgImg, sx, sy, sw, sh, 0, 0, w, h);
    ctx.restore();
  }

  // Glass tint overlay
  ctx.save();
  ctx.fillStyle = tintColor;
  ctx.globalAlpha = tintOpacity;
  ctx.fillRect(0, 0, w, h);
  ctx.restore();

  // Frosted glass highlight strip (subtle)
  ctx.save();
  var grad = ctx.createLinearGradient(0, 0, 0, h);
  grad.addColorStop(0, 'rgba(255,255,255,0.08)');
  grad.addColorStop(0.3, 'rgba(255,255,255,0.02)');
  grad.addColorStop(0.7, 'rgba(0,0,0,0.05)');
  grad.addColorStop(1, 'rgba(0,0,0,0.15)');
  ctx.fillStyle = grad;
  ctx.fillRect(0, 0, w, h);
  ctx.restore();

  // Logos area
  var logoSize = Math.round(h * 0.28);
  var centerY = h * 0.34;

  // Draw logos if available
  function drawLogo(dataUrl, cx, cy) {
    // Glow behind logo
    ctx.save();
    ctx.shadowColor = 'rgba(0,0,0,0.5)';
    ctx.shadowBlur = 18;
    ctx.fillStyle = 'rgba(255,255,255,0.06)';
    ctx.beginPath();
    ctx.arc(cx, cy, logoSize / 2 + 4, 0, Math.PI * 2);
    ctx.fill();
    ctx.restore();

    if (!dataUrl) {
      ctx.fillStyle = 'rgba(255,255,255,0.1)';
      ctx.beginPath();
      ctx.arc(cx, cy, logoSize / 2, 0, Math.PI * 2);
      ctx.fill();
      ctx.strokeStyle = 'rgba(255,255,255,0.15)';
      ctx.lineWidth = 2;
      ctx.stroke();
      return;
    }
    var img = new Image();
    img.onload = function() {
      ctx.save();
      ctx.beginPath();
      ctx.arc(cx, cy, logoSize / 2, 0, Math.PI * 2);
      ctx.clip();
      ctx.drawImage(img, cx - logoSize / 2, cy - logoSize / 2, logoSize, logoSize);
      ctx.restore();
      // Ring around logo
      ctx.strokeStyle = 'rgba(255,255,255,0.25)';
      ctx.lineWidth = 2;
      ctx.beginPath();
      ctx.arc(cx, cy, logoSize / 2, 0, Math.PI * 2);
      ctx.stroke();
    };
    img.src = dataUrl;
  }

  drawLogo(_introTeamLogoDataUrl, w * 0.25, centerY);
  drawLogo(_introOpponentLogoDataUrl, w * 0.75, centerY);

  // VS text with shadow
  ctx.save();
  ctx.shadowColor = 'rgba(0,0,0,0.6)';
  ctx.shadowBlur = 12;
  ctx.fillStyle = '#ffffff';
  ctx.font = 'bold ' + Math.round(h * 0.15) + 'px "Space Grotesk", sans-serif';
  ctx.textAlign = 'center';
  ctx.textBaseline = 'middle';
  ctx.fillText('VS', w / 2, centerY);
  ctx.restore();

  // Team names below logos
  ctx.save();
  ctx.shadowColor = 'rgba(0,0,0,0.5)';
  ctx.shadowBlur = 6;
  ctx.font = 'bold ' + Math.round(h * 0.065) + 'px "Space Grotesk", sans-serif';
  ctx.fillStyle = '#ffffff';
  ctx.textAlign = 'center';
  ctx.textBaseline = 'middle';
  ctx.fillText(teamName, w * 0.25, centerY + logoSize / 2 + h * 0.08);
  ctx.fillText(opponentName, w * 0.75, centerY + logoSize / 2 + h * 0.08);
  ctx.restore();

  // Coach notes (below team names, above bottom bar)
  if (coachNotes.trim()) {
    ctx.save();
    ctx.shadowColor = 'rgba(0,0,0,0.5)';
    ctx.shadowBlur = 4;
    var notesFontSize = Math.round(h * 0.038);
    ctx.font = '500 ' + notesFontSize + 'px "Space Grotesk", sans-serif';
    ctx.fillStyle = 'rgba(255,255,255,0.85)';
    ctx.textAlign = 'center';
    ctx.textBaseline = 'middle';
    var notesY = h * 0.72;
    var maxNotesW = w * 0.75;
    var lines = coachNotes.split('\n');
    var wrappedLines = [];
    lines.forEach(function(line) {
      var words = line.split(' ');
      var current = '';
      words.forEach(function(word) {
        var test = current ? current + ' ' + word : word;
        if (ctx.measureText(test).width > maxNotesW) {
          if (current) wrappedLines.push(current);
          current = word;
        } else {
          current = test;
        }
      });
      if (current) wrappedLines.push(current);
    });
    var lineH = notesFontSize * 1.35;
    var startY = notesY - ((wrappedLines.length - 1) * lineH) / 2;
    // Subtle background pill for notes
    if (wrappedLines.length > 0) {
      var pillH = wrappedLines.length * lineH + 10;
      var pillW = maxNotesW + 24;
      ctx.fillStyle = 'rgba(0,0,0,0.25)';
      _roundRect(ctx, (w - pillW) / 2, startY - lineH / 2 - 5, pillW, pillH, 8);
      ctx.fill();
      ctx.fillStyle = 'rgba(255,255,255,0.85)';
    }
    wrappedLines.forEach(function(l, idx) {
      ctx.fillText(l, w / 2, startY + idx * lineH);
    });
    ctx.restore();
  }

  // Bottom bar: League + Date
  ctx.save();
  ctx.fillStyle = 'rgba(0,0,0,0.35)';
  ctx.fillRect(0, h * 0.88, w, h * 0.12);
  ctx.shadowColor = 'rgba(0,0,0,0.4)';
  ctx.shadowBlur = 4;
  ctx.font = '600 ' + Math.round(h * 0.042) + 'px "Space Grotesk", sans-serif';
  ctx.fillStyle = 'rgba(255,255,255,0.7)';
  ctx.textAlign = 'center';
  ctx.textBaseline = 'middle';
  var bottomText = [league, gameDate].filter(Boolean).join('  \u2022  ');
  ctx.fillText(bottomText, w / 2, h * 0.94);
  ctx.restore();
}

function _roundRect(ctx, x, y, w, h, r) {
  ctx.beginPath();
  ctx.moveTo(x + r, y);
  ctx.lineTo(x + w - r, y);
  ctx.quadraticCurveTo(x + w, y, x + w, y + r);
  ctx.lineTo(x + w, y + h - r);
  ctx.quadraticCurveTo(x + w, y + h, x + w - r, y + h);
  ctx.lineTo(x + r, y + h);
  ctx.quadraticCurveTo(x, y + h, x, y + h - r);
  ctx.lineTo(x, y + r);
  ctx.quadraticCurveTo(x, y, x + r, y);
  ctx.closePath();
}

function saveGameIntroToTimeline() {
  var config = {
    team_name: document.getElementById('introTeamName').value || 'Team',
    opponent_name: document.getElementById('introOpponentName').value || 'Opponent',
    team_logo_data: _introTeamLogoDataUrl,
    opponent_logo_data: _introOpponentLogoDataUrl,
    league: document.getElementById('introLeague').value || '',
    game_date: document.getElementById('introGameDate').value || '',
    tint_color: document.getElementById('introTintColor').value || '#1a237e',
    tint_opacity: parseInt(document.getElementById('introTintOpacity').value, 10) || 35,
    coach_notes: document.getElementById('introCoachNotes').value || ''
  };
  // Insert at the beginning of timeline
  config.after_clip_id = null;
  var item = { type: 'game_intro', config: config };
  _compileTimeline.unshift(item);
  closeModal('gameIntroCreatorModal');
  if (typeof renderCompileTimeline === 'function') renderCompileTimeline();
  renderClipTimeline();
  Toast.success('Game intro added to timeline');
  // Persist to DB
  if (_currentVideo && _currentVideo.id) {
    API.post('/api/scouting/compile-cards', {
      card_type: 'game_intro',
      config: config,
      video_id: _currentVideo.id
    }).then(function(r) {
      if (r && r.data && r.data.id) item.dbId = r.data.id;
    }).catch(function(e) { console.warn('[Compile Cards] save failed:', e); });
  }
}

/* ═══ Player Card Creator ═══════════════════════════════ */
let _pcMode = 'roster'; // 'roster' or 'scouting'

function openPlayerCardCreator() {
  _pcPhotoDataUrl = null;
  _pcTeamLogoDataUrl = null;
  _pcMode = 'roster';
  document.getElementById('pcModeRoster').classList.add('active');
  document.getElementById('pcModeScouting').classList.remove('active');
  document.getElementById('pcRosterSection').style.display = '';
  document.getElementById('pcScoutingSection').style.display = 'none';
  document.getElementById('pcNotes').value = '';
  document.getElementById('pcTeamColor').value = '#c62828';

  // Clear scouting fields
  document.getElementById('pcScoutName').value = '';
  document.getElementById('pcScoutNumber').value = '';
  document.getElementById('pcScoutPosition').value = 'PG';
  document.getElementById('pcScoutHand').value = 'R';
  document.getElementById('pcScoutTeam').value = '';
  document.getElementById('pcPhotoPreview').textContent = '';
  var photoIcon = document.createElement('span');
  photoIcon.className = 'material-symbols-outlined';
  photoIcon.style.cssText = 'color:var(--text-muted);font-size:1.2rem;';
  photoIcon.textContent = 'person';
  document.getElementById('pcPhotoPreview').appendChild(photoIcon);
  document.getElementById('pcTeamLogoPreview').textContent = '';
  var logoIcon = document.createElement('span');
  logoIcon.className = 'material-symbols-outlined';
  logoIcon.style.cssText = 'color:var(--text-muted);font-size:1.2rem;';
  logoIcon.textContent = 'add_photo_alternate';
  document.getElementById('pcTeamLogoPreview').appendChild(logoIcon);

  loadRosterPlayersForSelect();
  previewPlayerCard();
  openModal('playerCardCreatorModal');
}

function togglePlayerCardMode(mode) {
  _pcMode = mode;
  document.getElementById('pcModeRoster').classList.toggle('active', mode === 'roster');
  document.getElementById('pcModeScouting').classList.toggle('active', mode === 'scouting');
  document.getElementById('pcRosterSection').style.display = mode === 'roster' ? '' : 'none';
  document.getElementById('pcScoutingSection').style.display = mode === 'scouting' ? '' : 'none';
  previewPlayerCard();
}

async function loadRosterPlayersForSelect() {
  var sel = document.getElementById('pcRosterSelect');
  sel.textContent = '';
  var defaultOpt = document.createElement('option');
  defaultOpt.value = '';
  defaultOpt.textContent = 'Choose a player...';
  sel.appendChild(defaultOpt);
  try {
    var res = await API.get('/api/players');
    var players = res.data.players || res.data || [];
    players.forEach(function(p) {
      var opt = document.createElement('option');
      opt.value = p.id;
      opt.textContent = (p.name || p.full_name || '') + (p.jersey_number ? ' #' + p.jersey_number : '');
      opt.dataset.player = JSON.stringify(p);
      sel.appendChild(opt);
    });
  } catch (e) {
    console.warn('[PlayerCard] Could not load roster:', e);
  }
}

function onRosterPlayerSelected(selectEl) {
  var opt = selectEl.options[selectEl.selectedIndex];
  if (!opt || !opt.dataset.player) { previewPlayerCard(); return; }
  try {
    var p = JSON.parse(opt.dataset.player);
    selectEl.dataset.selectedName = p.name || p.full_name || '';
    selectEl.dataset.selectedNumber = p.jersey_number || '';
    selectEl.dataset.selectedPosition = p.position || 'PG';
    selectEl.dataset.selectedHand = p.dominant_hand || 'R';
    selectEl.dataset.selectedTeam = p.team_name || '';
    selectEl.dataset.selectedPhoto = p.photo_url || p.photo_s3_key || '';
    selectEl.dataset.selectedLogo = p.team_logo_url || p.team_logo_s3_key || '';
    previewPlayerCard();
  } catch (e) {}
}

function onPlayerCardFileChange(which, input) {
  if (!input.files || !input.files[0]) return;
  var reader = new FileReader();
  reader.onload = function(e) {
    var dataUrl = e.target.result;
    var previewEl;
    if (which === 'photo') {
      _pcPhotoDataUrl = dataUrl;
      previewEl = document.getElementById('pcPhotoPreview');
    } else {
      _pcTeamLogoDataUrl = dataUrl;
      previewEl = document.getElementById('pcTeamLogoPreview');
    }
    previewEl.textContent = '';
    var img = document.createElement('img');
    img.src = dataUrl;
    img.alt = which === 'photo' ? 'Player photo' : 'Team logo';
    previewEl.appendChild(img);
    previewPlayerCard();
  };
  reader.readAsDataURL(input.files[0]);
}

function _getPlayerCardConfig() {
  var teamColor = document.getElementById('pcTeamColor').value || '#c62828';
  var notes = document.getElementById('pcNotes').value || '';
  var config;

  if (_pcMode === 'roster') {
    var sel = document.getElementById('pcRosterSelect');
    config = {
      name: sel.dataset.selectedName || 'Player',
      number: sel.dataset.selectedNumber || '',
      position: sel.dataset.selectedPosition || 'PG',
      hand: sel.dataset.selectedHand || 'R',
      team_name: sel.dataset.selectedTeam || '',
      photo_s3_key: sel.dataset.selectedPhoto || '',
      logo_s3_key: sel.dataset.selectedLogo || '',
      photo_data: null,
      logo_data: null,
      team_color: teamColor,
      notes: notes
    };
  } else {
    config = {
      name: document.getElementById('pcScoutName').value || 'Player',
      number: document.getElementById('pcScoutNumber').value || '',
      position: document.getElementById('pcScoutPosition').value || 'PG',
      hand: document.getElementById('pcScoutHand').value || 'R',
      team_name: document.getElementById('pcScoutTeam').value || '',
      photo_s3_key: '',
      logo_s3_key: '',
      photo_data: _pcPhotoDataUrl,
      logo_data: _pcTeamLogoDataUrl,
      team_color: teamColor,
      notes: notes
    };
  }
  return config;
}

function previewPlayerCard() {
  var canvas = document.getElementById('playerCardPreviewCanvas');
  if (!canvas) return;
  var ctx = canvas.getContext('2d');
  var w = canvas.width, h = canvas.height;
  var config = _getPlayerCardConfig();

  // Clear
  ctx.clearRect(0, 0, w, h);

  // Layout: LEFT 40% = photo area (team color), RIGHT 60% = info area (dark)
  var splitX = Math.round(w * 0.4);
  var slashOffset = Math.round(w * 0.04);

  _drawPlayerCardBackground(ctx, w, h, config, splitX, slashOffset);

  if (config.photo_data) {
    var photoImg = new Image();
    photoImg.onload = function() {
      _drawPlayerCardPhoto(ctx, w, h, photoImg, splitX, slashOffset);
      _drawPlayerCardOverlays(ctx, w, h, config, splitX, slashOffset);
    };
    photoImg.src = config.photo_data;
  } else {
    _drawPlayerCardOverlays(ctx, w, h, config, splitX, slashOffset);
  }
}

function _drawPlayerCardBackground(ctx, w, h, config, splitX, slashOffset) {
  // LEFT 40%: team color background with diagonal slash
  ctx.save();
  ctx.beginPath();
  ctx.moveTo(0, 0);
  ctx.lineTo(splitX + slashOffset, 0);
  ctx.lineTo(splitX - slashOffset, h);
  ctx.lineTo(0, h);
  ctx.closePath();
  ctx.fillStyle = config.team_color;
  ctx.fill();

  // Subtle darker gradient overlay on team color area for depth
  var grad = ctx.createLinearGradient(0, 0, splitX, h);
  grad.addColorStop(0, 'rgba(0,0,0,0)');
  grad.addColorStop(1, 'rgba(0,0,0,0.35)');
  ctx.fillStyle = grad;
  ctx.fill();
  ctx.restore();

  // RIGHT 60%: dark background
  ctx.save();
  ctx.beginPath();
  ctx.moveTo(splitX + slashOffset, 0);
  ctx.lineTo(w, 0);
  ctx.lineTo(w, h);
  ctx.lineTo(splitX - slashOffset, h);
  ctx.closePath();
  ctx.fillStyle = '#0d1117';
  ctx.fill();
  ctx.restore();
}

function _drawPlayerCardPhoto(ctx, w, h, photoImg, splitX, slashOffset) {
  // Photo is contained within the left area but NOT filling it completely
  // Leave margin at top/bottom/left to show team color around photo
  ctx.save();

  // Clip to left region with diagonal
  ctx.beginPath();
  ctx.moveTo(0, 0);
  ctx.lineTo(splitX + slashOffset, 0);
  ctx.lineTo(splitX - slashOffset, h);
  ctx.lineTo(0, h);
  ctx.closePath();
  ctx.clip();

  // Photo dimensions: 72% of left panel (reduced 20% from 92%), centered
  var maxPhotoH = h * 0.72;
  var maxPhotoW = splitX * 0.72;
  var photoAspect = photoImg.width / photoImg.height;
  var targetH = maxPhotoH;
  var targetW = targetH * photoAspect;
  if (targetW > maxPhotoW) {
    targetW = maxPhotoW;
    targetH = targetW / photoAspect;
  }
  // Position: horizontally centered in left panel, vertically centered
  var px = (splitX - targetW) / 2 - slashOffset * 0.3;
  var py = (h - targetH) / 2;
  ctx.drawImage(photoImg, px, py, targetW, targetH);

  ctx.restore();
}

function _drawPlayerCardOverlays(ctx, w, h, config, splitX, slashOffset) {
  var infoX = splitX + Math.round(w * 0.05);
  var infoW = w - infoX - 16;

  // Giant faded jersey number in background of info area
  if (config.number) {
    ctx.save();
    ctx.font = 'bold ' + Math.round(h * 0.6) + 'px "Space Grotesk", sans-serif';
    ctx.fillStyle = 'rgba(255,255,255,0.04)';
    ctx.textAlign = 'right';
    ctx.textBaseline = 'middle';
    ctx.fillText(config.number, w - 12, h * 0.5);
    ctx.restore();
  }

  // Player name (large, bold, white)
  ctx.font = 'bold ' + Math.round(h * 0.11) + 'px "Space Grotesk", sans-serif';
  ctx.fillStyle = '#ffffff';
  ctx.textAlign = 'left';
  ctx.textBaseline = 'top';
  var nameY = h * 0.11;
  ctx.fillText(config.name, infoX, nameY);

  // Team-color accent underline bar
  var underlineY = nameY + Math.round(h * 0.14);
  ctx.fillStyle = config.team_color;
  ctx.fillRect(infoX, underlineY, Math.round(infoW * 0.45), 3);

  // Position + Hand
  var detailY = underlineY + Math.round(h * 0.055);
  ctx.font = '600 ' + Math.round(h * 0.05) + 'px "Space Grotesk", sans-serif';
  ctx.fillStyle = 'rgba(255,255,255,0.7)';
  ctx.fillText('POSITION \u2014 ' + (config.position || 'PG'), infoX, detailY);
  ctx.fillText('HAND \u2014 ' + (config.hand === 'L' ? 'LEFT' : 'RIGHT'), infoX, detailY + Math.round(h * 0.075));

  // Thin divider
  var divY = detailY + Math.round(h * 0.18);
  ctx.fillStyle = 'rgba(255,255,255,0.1)';
  ctx.fillRect(infoX, divY, infoW, 1);

  // Scouting notes as bullet points
  var notesLines = (config.notes || '').split('\n').filter(function(l) { return l.trim(); });
  var noteY = divY + Math.round(h * 0.04);
  var noteFontSize = Math.round(h * 0.045);
  ctx.font = '400 ' + noteFontSize + 'px "Space Grotesk", sans-serif';
  for (var i = 0; i < Math.min(notesLines.length, 5); i++) {
    ctx.fillStyle = config.team_color;
    ctx.fillText('\u25CF', infoX, noteY);
    ctx.fillStyle = 'rgba(255,255,255,0.85)';
    ctx.fillText(notesLines[i].trim(), infoX + 16, noteY);
    noteY += Math.round(noteFontSize * 1.6);
  }

  // Bottom: 4px team-color accent bar across full width
  ctx.fillStyle = config.team_color;
  ctx.fillRect(0, h - 4, w, 4);

  // Team logo in top-left corner
  if (config.logo_data) {
    var logoImg = new Image();
    logoImg.onload = function() {
      ctx.save();
      ctx.globalAlpha = 0.8;
      var logoSz = Math.round(h * 0.16);
      ctx.drawImage(logoImg, 10, 10, logoSz, logoSz);
      ctx.restore();
    };
    logoImg.src = config.logo_data;
  }
}

function savePlayerCardToTimeline() {
  var config = _getPlayerCardConfig();
  if (_pcMode === 'roster') {
    var sel = document.getElementById('pcRosterSelect');
    if (!sel.value) { Toast.error('Please select a player'); return; }
  } else {
    if (!document.getElementById('pcScoutName').value.trim()) { Toast.error('Please enter a player name'); return; }
  }
  // Append at end — after_clip_id = last clip in current timeline (or null)
  var lastClipId = null;
  for (var i = _compileTimeline.length - 1; i >= 0; i--) {
    if (_compileTimeline[i].type === 'clip') { lastClipId = _compileTimeline[i].clip.id; break; }
  }
  config.after_clip_id = lastClipId;
  var item = { type: 'player_card', config: config };
  _compileTimeline.push(item);
  closeModal('playerCardCreatorModal');
  if (typeof renderCompileTimeline === 'function') renderCompileTimeline();
  renderClipTimeline();
  Toast.success('Player card added to timeline');
  // Persist to DB
  if (_currentVideo && _currentVideo.id) {
    API.post('/api/scouting/compile-cards', {
      card_type: 'player_card',
      config: config,
      video_id: _currentVideo.id
    }).then(function(r) {
      if (r && r.data && r.data.id) item.dbId = r.data.id;
    }).catch(function(e) { console.warn('[Compile Cards] save failed:', e); });
  }
}

/* ═══ Canvas Card Rendering for Compilation ═════════════ */

function loadImageFromDataUrl(dataUrl) {
  if (!dataUrl) return Promise.resolve(null);
  return new Promise(function(resolve) {
    var done = false;
    var img = new Image();
    img.onload = function() { if (!done) { done = true; resolve(img); } };
    img.onerror = function() { if (!done) { done = true; resolve(null); } };
    img.src = dataUrl;
    setTimeout(function() { if (!done) { done = true; resolve(null); } }, 3000);
  });
}

function loadImageFromS3(s3Key) {
  if (!s3Key) return Promise.resolve(null);
  return new Promise(function(resolve) {
    var done = false;
    var img = new Image();
    img.crossOrigin = 'anonymous';
    img.onload = function() { if (!done) { done = true; resolve(img); } };
    img.onerror = function() { if (!done) { done = true; resolve(null); } };
    img.src = '/api/scouting/asset/' + encodeURIComponent(s3Key);
    setTimeout(function() { if (!done) { done = true; resolve(null); } }, 3000);
  });
}

function wrapText(ctx, text, maxWidth) {
  var words = text.split(' ');
  var lines = [];
  var currentLine = '';
  for (var i = 0; i < words.length; i++) {
    var testLine = currentLine ? (currentLine + ' ' + words[i]) : words[i];
    if (ctx.measureText(testLine).width > maxWidth && currentLine) {
      lines.push(currentLine);
      currentLine = words[i];
    } else {
      currentLine = testLine;
    }
  }
  if (currentLine) lines.push(currentLine);
  return lines;
}

async function renderGameIntroFrames(ctx, vw, vh, config, recorder, progressFn, videoTrack) {
  var fps = 30;
  var totalFrames = fps * 5;
  var frameInterval = 1000 / fps;

  var teamLogo = await loadImageFromDataUrl(config.team_logo_data);
  var opponentLogo = await loadImageFromDataUrl(config.opponent_logo_data);

  // Load arena background (use preloaded if available, else load fresh)
  var arenaBg = (_arenaBgLoaded && _arenaBgImg) ? _arenaBgImg : await new Promise(function(resolve) {
    var done = false;
    var img = new Image();
    img.onload = function() { if (!done) { done = true; resolve(img); } };
    img.onerror = function() { if (!done) { done = true; resolve(null); } };
    img.src = '/static/img/arena_bg.jpg';
    setTimeout(function() { if (!done) { done = true; resolve(null); } }, 5000);
  });

  await new Promise(function(r) { setTimeout(r, 100); });

  var tintColor = config.tint_color || config.bg_color || '#1a237e';
  var tintOpacity = (config.tint_opacity != null ? config.tint_opacity : 35) / 100;
  var teamName = config.team_name || 'Team';
  var opponentName = config.opponent_name || 'Opponent';
  var league = config.league || '';
  var gameDate = config.game_date || '';
  var coachNotes = config.coach_notes || '';

  return new Promise(function(resolve) {
    var frame = 0;
    function drawFrame() {
      if (frame >= totalFrames) { resolve(); return; }

      // Dark base
      ctx.fillStyle = '#0a0a12';
      ctx.fillRect(0, 0, vw, vh);

      // Arena background at 35% exposure
      if (arenaBg) {
        ctx.save();
        ctx.globalAlpha = 0.35;
        var imgRatio = arenaBg.width / arenaBg.height;
        var canvasRatio = vw / vh;
        var sx = 0, sy = 0, sw = arenaBg.width, sh = arenaBg.height;
        if (imgRatio > canvasRatio) {
          sw = arenaBg.height * canvasRatio;
          sx = (arenaBg.width - sw) / 2;
        } else {
          sh = arenaBg.width / canvasRatio;
          sy = (arenaBg.height - sh) / 2;
        }
        ctx.drawImage(arenaBg, sx, sy, sw, sh, 0, 0, vw, vh);
        ctx.restore();
      }

      // Glass tint overlay
      ctx.save();
      ctx.fillStyle = tintColor;
      ctx.globalAlpha = tintOpacity;
      ctx.fillRect(0, 0, vw, vh);
      ctx.restore();

      // Frosted glass gradient
      ctx.save();
      var grad = ctx.createLinearGradient(0, 0, 0, vh);
      grad.addColorStop(0, 'rgba(255,255,255,0.08)');
      grad.addColorStop(0.3, 'rgba(255,255,255,0.02)');
      grad.addColorStop(0.7, 'rgba(0,0,0,0.05)');
      grad.addColorStop(1, 'rgba(0,0,0,0.15)');
      ctx.fillStyle = grad;
      ctx.fillRect(0, 0, vw, vh);
      ctx.restore();

      // Fade-in animation
      var alpha = frame < 15 ? frame / 15 : 1;
      ctx.save();
      ctx.globalAlpha = alpha;

      var logoSize = Math.round(vh * 0.32);
      var centerY = vh * 0.34;
      var logoLeftCX = vw * 0.25;
      var logoRightCX = vw * 0.75;

      // Logo glow + ring
      function drawCompileLogo(img, cx, cy) {
        ctx.save();
        ctx.shadowColor = 'rgba(0,0,0,0.5)';
        ctx.shadowBlur = 18;
        ctx.fillStyle = 'rgba(255,255,255,0.06)';
        ctx.beginPath();
        ctx.arc(cx, cy, logoSize / 2 + 4, 0, Math.PI * 2);
        ctx.fill();
        ctx.restore();

        if (img) {
          ctx.save();
          ctx.beginPath();
          ctx.arc(cx, cy, logoSize / 2, 0, Math.PI * 2);
          ctx.clip();
          ctx.drawImage(img, cx - logoSize / 2, cy - logoSize / 2, logoSize, logoSize);
          ctx.restore();
        } else {
          ctx.fillStyle = 'rgba(255,255,255,0.1)';
          ctx.beginPath();
          ctx.arc(cx, cy, logoSize / 2, 0, Math.PI * 2);
          ctx.fill();
        }
        ctx.strokeStyle = 'rgba(255,255,255,0.25)';
        ctx.lineWidth = 2;
        ctx.beginPath();
        ctx.arc(cx, cy, logoSize / 2, 0, Math.PI * 2);
        ctx.stroke();
      }

      drawCompileLogo(teamLogo, logoLeftCX, centerY);
      drawCompileLogo(opponentLogo, logoRightCX, centerY);

      // VS text with shadow
      ctx.save();
      ctx.shadowColor = 'rgba(0,0,0,0.6)';
      ctx.shadowBlur = 12;
      ctx.fillStyle = '#ffffff';
      ctx.font = 'bold ' + Math.round(vh * 0.16) + 'px "Space Grotesk", sans-serif';
      ctx.textAlign = 'center';
      ctx.textBaseline = 'middle';
      ctx.fillText('VS', vw / 2, centerY);
      ctx.restore();

      // Team names
      ctx.save();
      ctx.shadowColor = 'rgba(0,0,0,0.5)';
      ctx.shadowBlur = 6;
      ctx.font = 'bold ' + Math.round(vh * 0.065) + 'px "Space Grotesk", sans-serif';
      ctx.fillStyle = '#ffffff';
      ctx.textAlign = 'center';
      ctx.textBaseline = 'middle';
      ctx.fillText(teamName, logoLeftCX, centerY + logoSize / 2 + vh * 0.08);
      ctx.fillText(opponentName, logoRightCX, centerY + logoSize / 2 + vh * 0.08);
      ctx.restore();

      // Coach notes
      if (coachNotes.trim()) {
        ctx.save();
        ctx.shadowColor = 'rgba(0,0,0,0.5)';
        ctx.shadowBlur = 4;
        var notesFontSize = Math.round(vh * 0.038);
        ctx.font = '500 ' + notesFontSize + 'px "Space Grotesk", sans-serif';
        ctx.fillStyle = 'rgba(255,255,255,0.85)';
        ctx.textAlign = 'center';
        ctx.textBaseline = 'middle';
        var notesY = vh * 0.72;
        var maxNotesW = vw * 0.75;
        var cLines = coachNotes.split('\n');
        var wrappedLines = [];
        cLines.forEach(function(line) {
          var words = line.split(' ');
          var current = '';
          words.forEach(function(word) {
            var test = current ? current + ' ' + word : word;
            if (ctx.measureText(test).width > maxNotesW) {
              if (current) wrappedLines.push(current);
              current = word;
            } else {
              current = test;
            }
          });
          if (current) wrappedLines.push(current);
        });
        var lineH = notesFontSize * 1.35;
        var startY = notesY - ((wrappedLines.length - 1) * lineH) / 2;
        if (wrappedLines.length > 0) {
          var pillH = wrappedLines.length * lineH + 10;
          var pillW = maxNotesW + 24;
          ctx.fillStyle = 'rgba(0,0,0,0.25)';
          _roundRect(ctx, (vw - pillW) / 2, startY - lineH / 2 - 5, pillW, pillH, 8);
          ctx.fill();
          ctx.fillStyle = 'rgba(255,255,255,0.85)';
        }
        wrappedLines.forEach(function(l, idx) {
          ctx.fillText(l, vw / 2, startY + idx * lineH);
        });
        ctx.restore();
      }

      // Bottom bar
      ctx.save();
      ctx.fillStyle = 'rgba(0,0,0,0.35)';
      ctx.fillRect(0, vh * 0.88, vw, vh * 0.12);
      ctx.shadowColor = 'rgba(0,0,0,0.4)';
      ctx.shadowBlur = 4;
      ctx.font = '600 ' + Math.round(vh * 0.042) + 'px "Space Grotesk", sans-serif';
      ctx.fillStyle = 'rgba(255,255,255,0.7)';
      ctx.textAlign = 'center';
      ctx.textBaseline = 'middle';
      var bottomText = [league, gameDate].filter(Boolean).join('  \u2022  ');
      ctx.fillText(bottomText, vw / 2, vh * 0.94);
      ctx.restore();

      ctx.restore(); // end alpha

      if (videoTrack && videoTrack.requestFrame) {
        try { videoTrack.requestFrame(); } catch(e) {}
      }

      if (progressFn) progressFn(frame / totalFrames);
      frame++;
      setTimeout(drawFrame, frameInterval);
    }
    drawFrame();
  });
}

async function renderPlayerCardFrames(ctx, vw, vh, config, recorder, progressFn, videoTrack) {
  var fps = 30;
  var totalFrames = fps * 5; // 5 seconds
  var frameInterval = 1000 / fps;

  // Pre-load images
  var playerPhoto = config.photo_data
    ? await loadImageFromDataUrl(config.photo_data)
    : (config.photo_s3_key ? await loadImageFromS3(config.photo_s3_key) : null);
  var teamLogo = config.logo_data
    ? await loadImageFromDataUrl(config.logo_data)
    : (config.logo_s3_key ? await loadImageFromS3(config.logo_s3_key) : null);

  var teamColor = config.team_color || '#c62828';
  // LEFT 40% = photo area (smaller), RIGHT 60% = info area (larger)
  var splitX = Math.round(vw * 0.4);
  var slashOffset = Math.round(vw * 0.04);

  // Small stabilization delay so MediaRecorder is ready
  await new Promise(function(r) { setTimeout(r, 100); });

  return new Promise(function(resolve) {
    var frame = 0;
    function drawFrame() {
      if (frame >= totalFrames) { resolve(); return; }

      ctx.clearRect(0, 0, vw, vh);

      // Animate: slide-in during first 20 frames
      var progress = frame < 20 ? frame / 20 : 1;
      var slideOffset = Math.round((1 - progress) * 40);

      // LEFT 40%: team color background with diagonal slash
      ctx.save();
      ctx.beginPath();
      ctx.moveTo(0, 0);
      ctx.lineTo(splitX + slashOffset - slideOffset, 0);
      ctx.lineTo(splitX - slashOffset - slideOffset, vh);
      ctx.lineTo(0, vh);
      ctx.closePath();
      ctx.fillStyle = teamColor;
      ctx.fill();

      // Subtle darker gradient on team color area
      var grad = ctx.createLinearGradient(0, 0, splitX, vh);
      grad.addColorStop(0, 'rgba(0,0,0,0)');
      grad.addColorStop(1, 'rgba(0,0,0,0.35)');
      ctx.fillStyle = grad;
      ctx.fill();

      // Player photo — contained within left area, NOT filling it entirely
      if (playerPhoto) {
        ctx.save();
        ctx.beginPath();
        ctx.moveTo(0, 0);
        ctx.lineTo(splitX + slashOffset, 0);
        ctx.lineTo(splitX - slashOffset, vh);
        ctx.lineTo(0, vh);
        ctx.closePath();
        ctx.clip();

        // Photo at 72% of left panel (reduced 20%), centered
        var maxPhotoH = vh * 0.72;
        var maxPhotoW = splitX * 0.72;
        var photoAspect = playerPhoto.width / playerPhoto.height;
        var targetH = maxPhotoH;
        var targetW = targetH * photoAspect;
        if (targetW > maxPhotoW) {
          targetW = maxPhotoW;
          targetH = targetW / photoAspect;
        }
        var px = (splitX - targetW) / 2 - slashOffset * 0.3;
        var py = (vh - targetH) / 2;
        ctx.drawImage(playerPhoto, px, py, targetW, targetH);
        ctx.restore();
      }
      ctx.restore();

      // RIGHT 60%: dark background
      ctx.save();
      ctx.beginPath();
      ctx.moveTo(splitX + slashOffset, 0);
      ctx.lineTo(vw, 0);
      ctx.lineTo(vw, vh);
      ctx.lineTo(splitX - slashOffset, vh);
      ctx.closePath();
      ctx.fillStyle = '#0d1117';
      ctx.fill();
      ctx.restore();

      // Info area
      var infoX = splitX + Math.round(vw * 0.05);
      var infoW = vw - infoX - 16;

      // Giant faded jersey number
      if (config.number) {
        ctx.save();
        ctx.font = 'bold ' + Math.round(vh * 0.6) + 'px "Space Grotesk", sans-serif';
        ctx.fillStyle = 'rgba(255,255,255,0.04)';
        ctx.textAlign = 'right';
        ctx.textBaseline = 'middle';
        ctx.fillText(config.number, vw - 12, vh * 0.45);
        ctx.restore();
      }

      ctx.save();
      ctx.globalAlpha = Math.min(1, progress * 1.5);

      // Player name
      var nameFontSize = Math.round(vh * 0.095);
      ctx.font = 'bold ' + nameFontSize + 'px "Space Grotesk", sans-serif';
      ctx.fillStyle = '#ffffff';
      ctx.textAlign = 'left';
      ctx.textBaseline = 'top';
      var nameY = vh * 0.1;
      ctx.fillText(config.name || 'Player', infoX, nameY);

      // Team-color accent underline bar
      var underlineY = nameY + Math.round(vh * 0.13);
      ctx.fillStyle = teamColor;
      ctx.fillRect(infoX, underlineY, Math.round(infoW * 0.5), 4);

      // Position + Hand
      var detailFontSize = Math.round(vh * 0.048);
      var detailY = underlineY + Math.round(vh * 0.06);
      ctx.font = '600 ' + detailFontSize + 'px "Space Grotesk", sans-serif';
      ctx.fillStyle = 'rgba(255,255,255,0.7)';
      ctx.fillText('POSITION \u2014 ' + (config.position || 'PG'), infoX, detailY);
      ctx.fillText('HAND \u2014 ' + (config.hand === 'L' ? 'LEFT' : 'RIGHT'), infoX, detailY + Math.round(vh * 0.07));

      // Thin divider
      var divY = detailY + Math.round(vh * 0.16);
      ctx.fillStyle = 'rgba(255,255,255,0.1)';
      ctx.fillRect(infoX, divY, infoW, 1);

      // Scouting notes as bullet points
      var notesLines = (config.notes || '').split('\n').filter(function(l) { return l.trim(); });
      var noteY = divY + Math.round(vh * 0.04);
      var noteFontSize = Math.round(vh * 0.04);
      ctx.font = '400 ' + noteFontSize + 'px "Space Grotesk", sans-serif';
      for (var ni = 0; ni < Math.min(notesLines.length, 5); ni++) {
        ctx.fillStyle = teamColor;
        ctx.fillText('\u25CF', infoX, noteY);
        ctx.fillStyle = 'rgba(255,255,255,0.85)';
        ctx.fillText(notesLines[ni].trim(), infoX + 18, noteY);
        noteY += Math.round(noteFontSize * 1.7);
      }

      ctx.restore();

      // Bottom: 4px team-color accent bar
      ctx.fillStyle = teamColor;
      ctx.fillRect(0, vh - 4, vw, 4);

      // Team logo in top-left corner with subtle transparency
      if (teamLogo) {
        ctx.save();
        ctx.globalAlpha = 0.65;
        var logoSz = Math.round(vh * 0.16);
        ctx.drawImage(teamLogo, 14, 14, logoSz, logoSz);
        ctx.restore();
      }

      // Force canvas stream to emit this frame
      if (videoTrack && videoTrack.requestFrame) {
        try { videoTrack.requestFrame(); } catch(e) {}
      }

      if (progressFn) progressFn(frame / totalFrames);
      frame++;
      setTimeout(drawFrame, frameInterval);
    }
    drawFrame();
  });
}

// Add stabilization delay at start of player card render
async function _renderPlayerCardStabilize() {
  await new Promise(function(r) { setTimeout(r, 100); });
}

async function startCompilation() {
  if (_compiling) return;
  if (_compileTimeline.length < 1) { Toast.error(t('scouting.compile.no_clips')); return; }

  _compiling = true;
  _compileOriginalVideoId = _currentVideo?.id || null;

  // Close compile modal
  closeModal('compileModal');

  // Use the global floating banner for progress (same as upload)
  const banner = document.getElementById('bgUploadBanner');
  const bgTitle = document.getElementById('bgUploadTitle');
  const bgPct = document.getElementById('bgUploadPct');
  const bgFill = document.getElementById('bgUploadFill');
  const bgStatus = document.getElementById('bgUploadStatus');
  if (banner) { banner.style.display = 'block'; }
  if (bgTitle) { bgTitle.textContent = 'Compiling video...'; }
  if (bgPct) { bgPct.textContent = '0%'; }
  if (bgFill) { bgFill.style.width = '0%'; bgFill.style.background = 'var(--accent, #f48c25)'; }
  if (bgStatus) { bgStatus.textContent = 'Processing...'; }

  // Also update inline progress (hidden behind modal but kept for compatibility)
  const btn = document.getElementById('compileGenerateBtn');
  if (btn) btn.disabled = true;
  const progress = document.getElementById('compileProgress');
  const label = document.getElementById('compileProgressLabel');
  const fill = document.getElementById('compileProgressFill');
  progress.style.display = '';
  fill.style.width = '0%';

  // Prevent accidental tab close
  const beforeUnload = (e) => { e.preventDefault(); e.returnValue = ''; };
  window.addEventListener('beforeunload', beforeUnload);

  try {
    // Access the underlying video element
    const videoEl = _vjsPlayer.tech({ IWillNotUseThisInPlugins: true })?.el_;
    if (!videoEl) throw new Error('Cannot access video element');

    const vw = videoEl.videoWidth || 1280;
    const vh = videoEl.videoHeight || 720;
    const compCanvas = document.createElement('canvas');
    compCanvas.width = vw;
    compCanvas.height = vh;
    const compCtx = compCanvas.getContext('2d');

    // Setup MediaRecorder — get videoTrack for requestFrame() support
    const stream = compCanvas.captureStream(30);
    var _canvasVideoTrack = stream.getVideoTracks()[0];
    // Try to capture audio from video element (skip if cross-origin)
    try {
      if (videoEl.captureStream && !_currentVideo.s3_url) {
        var videoStream = videoEl.captureStream();
        var audioTrack = videoStream.getAudioTracks()[0];
        if (audioTrack) { stream.addTrack(audioTrack); console.log('[Compile] Audio track captured'); }
      }
    } catch (e) { console.log('[Compile] Audio capture skipped:', e.message); }

    var mimeType = 'video/webm';
    if (MediaRecorder.isTypeSupported('video/webm;codecs=vp9')) mimeType = 'video/webm;codecs=vp9';
    else if (MediaRecorder.isTypeSupported('video/webm;codecs=vp8')) mimeType = 'video/webm;codecs=vp8';
    console.log('[Compile] Using codec:', mimeType, 'canvas:', vw, 'x', vh, 'videoEl.readyState:', videoEl.readyState);

    const recorder = new MediaRecorder(stream, { mimeType, videoBitsPerSecond: 5000000 });
    const chunks = [];
    recorder.ondataavailable = (e) => {
      console.log('[Compile] chunk:', e.data.size, 'bytes');
      if (e.data.size > 0) chunks.push(e.data);
    };

    // Calculate total duration from timeline (clips + 5s per card)
    const totalDuration = _compileTimeline.reduce(function(s, item) {
      if (item.type === 'clip') return s + (item.clip.end_time - item.clip.start_time);
      return s + 5; // cards are always 5 seconds
    }, 0);
    let processedDuration = 0;

    // Save original playback rate
    const origRate = _vjsPlayer.playbackRate();
    _vjsPlayer.playbackRate(1);
    _vjsPlayer.muted(true); // mute during processing

    recorder.start(100); // 100ms chunks
    const showTags = document.getElementById('compileShowTags')?.checked;
    const isRTL = document.documentElement.dir === 'rtl' || document.documentElement.lang === 'he';
    const tagFontSize = Math.round(vw * 0.028); // ~36px at 1280w
    const tagPad = tagFontSize * 0.5;

    // Count clips for status display
    const totalClips = _compileTimeline.filter(function(item) { return item.type === 'clip'; }).length;
    let clipNum = 0;

    // Process each timeline item sequentially
    for (let i = 0; i < _compileTimeline.length; i++) {
      const item = _compileTimeline[i];

      if (item.type === 'game_intro') {
        if (bgStatus) bgStatus.textContent = 'Game Intro';
        label.textContent = 'Rendering Game Intro...';

        // Pause video (we're not using it) — recorder stays recording the canvas
        if (_vjsPlayer) _vjsPlayer.pause();
        if (recorder.state === 'paused') recorder.resume();

        try {
          await renderGameIntroFrames(compCtx, vw, vh, item.config, recorder, function(pct) {
            var overallPct = Math.round(((processedDuration + pct * 5) / totalDuration) * 100);
            var pctStr = Math.min(overallPct, 99) + '%';
            fill.style.width = pctStr;
            if (bgFill) bgFill.style.width = pctStr;
            if (bgPct) bgPct.textContent = pctStr;
          }, _canvasVideoTrack);
        } catch (cardErr) {
          console.error('[Compile] Game Intro render error:', cardErr);
        }
        processedDuration += 5;

      } else if (item.type === 'player_card') {
        if (bgStatus) bgStatus.textContent = 'Player Card: ' + (item.config.name || '');
        label.textContent = 'Rendering Player Card: ' + (item.config.name || '') + '...';

        if (_vjsPlayer) _vjsPlayer.pause();
        if (recorder.state === 'paused') recorder.resume();

        try {
          await renderPlayerCardFrames(compCtx, vw, vh, item.config, recorder, function(pct) {
            var overallPct = Math.round(((processedDuration + pct * 5) / totalDuration) * 100);
            var pctStr = Math.min(overallPct, 99) + '%';
            fill.style.width = pctStr;
            if (bgFill) bgFill.style.width = pctStr;
            if (bgPct) bgPct.textContent = pctStr;
          }, _canvasVideoTrack);
        } catch (cardErr) {
          console.error('[Compile] Player Card render error:', cardErr);
        }
        processedDuration += 5;

      } else if (item.type === 'clip') {
        clipNum++;
        const clip = { ...item.clip, end_time: item.clip.end_time + 0.5 };
        const clipDur = clip.end_time - clip.start_time;
        label.textContent = t('scouting.compile.processing', { current: clipNum, total: totalClips, pct: 0 });
        if (bgStatus) bgStatus.textContent = 'Clip ' + clipNum + ' of ' + totalClips;

        // Pause both recorder AND video during seek
        if (recorder.state === 'recording') recorder.pause();
        _vjsPlayer.pause();

        // Seek to clip start — wait for seeked event
        _vjsPlayer.currentTime(clip.start_time);
        await new Promise(r => {
          const onSeeked = () => { _vjsPlayer.off('seeked', onSeeked); r(); };
          _vjsPlayer.on('seeked', onSeeked);
          setTimeout(r, 3000);
        });

        // Play video and wait until it's ACTUALLY playing
        try { await _vjsPlayer.play(); } catch(e) { console.warn('[Compile] play() failed:', e); }
        await new Promise(r => {
          const onPlaying = () => { _vjsPlayer.off('playing', onPlaying); r(); };
          _vjsPlayer.on('playing', onPlaying);
          setTimeout(r, 3000);
        });

        // Wait for first real frame to be decoded
        await new Promise(r => setTimeout(r, 100));

        // NOW resume recorder — video is playing smoothly
        if (recorder.state === 'paused') recorder.resume();

        // Frame capture loop for this clip — use setTimeout for consistent 30fps timing
        var _clipStuckCounter = 0;
        var _lastCaptureTime = -1;
        var _frameInterval = 1000 / 30; // 30fps = ~33ms per frame
        await new Promise((resolve) => {
          const captureFrame = () => {
            if (!_compiling) { resolve(); return; }
            const curTime = _vjsPlayer.currentTime();
            if (curTime >= clip.end_time) {
              _vjsPlayer.pause();
              resolve();
              return;
            }
            // Detect stuck video (same time for 30+ frames = ~1 second at 30fps)
            if (Math.abs(curTime - _lastCaptureTime) < 0.01) {
              _clipStuckCounter++;
              if (_clipStuckCounter > 30) {
                console.warn('[Compile] Video stuck at', curTime, '— skipping clip');
                _vjsPlayer.pause();
                resolve();
                return;
              }
            } else {
              _clipStuckCounter = 0;
            }
            _lastCaptureTime = curTime;
            // Skip frame if video is buffering (readyState < 3 = not enough data)
            if (videoEl.readyState < 3) {
              setTimeout(captureFrame, _frameInterval);
              return;
            }
            // Draw video frame
            compCtx.drawImage(videoEl, 0, 0, vw, vh);
            // Draw annotations (scale telestrator to export canvas)
            const origW = telestrator.canvas.width;
            const origH = telestrator.canvas.height;
            telestrator.canvas.width = vw;
            telestrator.canvas.height = vh;
            telestrator.renderFrame(curTime);
            compCtx.drawImage(telestrator.canvas, 0, 0);
            telestrator.canvas.width = origW;
            telestrator.canvas.height = origH;
            // Draw action type tag overlay
            if (showTags && clip.action_type) {
              const tagLabel = ACTION_TYPES.find(a => a.value === clip.action_type)?.label || clip.action_type;
              compCtx.save();
              compCtx.direction = 'ltr';
              compCtx.font = `bold ${tagFontSize}px "Space Grotesk", sans-serif`;
              const tw = compCtx.measureText(tagLabel).width;
              const pillW = tw + tagPad * 2;
              const pillH = tagFontSize * 1.4;
              const pillX = isRTL ? (vw - pillW - tagPad) : tagPad;
              const pillY = tagPad;
              // Background pill
              compCtx.fillStyle = 'rgba(0,0,0,0.65)';
              compCtx.beginPath();
              compCtx.roundRect(pillX, pillY, pillW, pillH, tagFontSize * 0.25);
              compCtx.fill();
              // Text
              compCtx.fillStyle = '#ffffff';
              compCtx.textAlign = isRTL ? 'right' : 'left';
              compCtx.textBaseline = 'middle';
              const textX = isRTL ? (pillX + pillW - tagPad) : (pillX + tagPad);
              compCtx.fillText(tagLabel, textX, pillY + pillH / 2);
              compCtx.restore();
            }
            // Update progress (both inline and floating banner)
            const clipProgress = curTime - clip.start_time;
            const overallPct = Math.round(((processedDuration + clipProgress) / totalDuration) * 100);
            const pctStr = Math.min(overallPct, 99) + '%';
            fill.style.width = pctStr;
            label.textContent = t('scouting.compile.processing', { current: clipNum, total: totalClips, pct: Math.min(overallPct, 99) });
            if (bgFill) bgFill.style.width = pctStr;
            if (bgPct) bgPct.textContent = pctStr;
            if (bgStatus) bgStatus.textContent = 'Clip ' + clipNum + ' of ' + totalClips;
            setTimeout(captureFrame, _frameInterval);
          };
          setTimeout(captureFrame, _frameInterval);
        });

        processedDuration += clipDur;
      }
    }

    // Request final data before stopping
    if (recorder.state === 'recording') {
      recorder.requestData();
      await new Promise(r => setTimeout(r, 200));
    }

    // Stop recording
    const exportDone = new Promise(r => { recorder.onstop = r; });
    recorder.stop();
    await exportDone;

    // Stop all stream tracks to release resources
    stream.getTracks().forEach(function(track) { track.stop(); });

    _vjsPlayer.muted(false);
    _vjsPlayer.playbackRate(origRate);

    // Create blob
    const blob = new Blob(chunks, { type: mimeType });
    console.log('[Compile] blob:', blob.size, 'bytes, type:', blob.type, 'chunks:', chunks.length);
    if (blob.size < 500) throw new Error('Compilation produced empty video');
    label.textContent = t('scouting.compile.uploading');
    fill.style.width = '100%';
    if (bgTitle) bgTitle.textContent = 'Uploading compiled video...';
    if (bgFill) bgFill.style.width = '100%';

    // Upload to S3 (reuse existing upload logic)
    const s3Res = await _uploadBlobToS3(blob, (pct) => {
      label.textContent = `${t('scouting.compile.uploading')} ${pct}%`;
      if (bgPct) bgPct.textContent = pct + '%';
      if (bgStatus) bgStatus.textContent = 'Uploading...';
    });

    // Register as new video via API
    const title = document.getElementById('compileTitle').value || `Compilation — ${_currentVideo?.title || ''}`;
    const res = await API.post('/api/scouting/videos', {
      s3_key: s3Res.s3_key,
      original_name: `${title}.webm`,
      file_size: s3Res.file_size || blob.size,
      duration_seconds: totalDuration,
      title,
      video_type: 'highlight',
      keep_forever: false,
    });

    _compiledNewVideoId = res.data?.id;
    closeModal('compileModal');
    // Restore canvas
    telestrator._resizeCanvas();
    telestrator.renderFrame(_vjsPlayer ? _vjsPlayer.currentTime() : 0);

    // Update banner to show success
    if (bgTitle) bgTitle.textContent = 'Compile complete!';
    if (bgFill) { bgFill.style.width = '100%'; bgFill.style.background = '#22c55e'; }
    if (bgPct) bgPct.textContent = '';
    if (bgStatus) bgStatus.textContent = 'New video created';
    setTimeout(function() { if (banner) banner.style.display = 'none'; if (bgFill) bgFill.style.background = 'var(--accent, #f48c25)'; }, 4000);

    Toast.success('Compiled video created!');
    loadVideos();

    // Show delete-original prompt
    openModal('compileDeleteModal');

  } catch (e) {
    console.error('Compilation error:', e);
    Toast.error(t('scouting.compile.failed') + ': ' + (e.message || ''));
    _vjsPlayer.muted(false);
    // Hide banner on error
    if (bgTitle) bgTitle.textContent = 'Compile failed';
    if (bgFill) { bgFill.style.background = '#ef4444'; }
    if (bgStatus) bgStatus.textContent = e.message || '';
    setTimeout(function() { if (banner) banner.style.display = 'none'; if (bgFill) bgFill.style.background = 'var(--accent, #f48c25)'; }, 5000);
  } finally {
    _compiling = false;
    if (_vjsPlayer) {
      _vjsPlayer.pause();
      _vjsPlayer.muted(false);
      _vjsPlayer.playbackRate(1);
    }
    if (btn) btn.disabled = false;
    progress.style.display = 'none';
    window.removeEventListener('beforeunload', beforeUnload);
    telestrator._resizeCanvas();
    telestrator.renderFrame(_vjsPlayer ? _vjsPlayer.currentTime() : 0);
  }
}

async function _uploadBlobToS3(blob, onProgress) {
  const file = new File([blob], 'compilation.webm', { type: blob.type || 'video/webm' });
  const presignRes = await fetch('/api/scouting/s3/presign-upload', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({
      file_name: file.name,
      file_size: file.size,
      content_type: file.type
    })
  });
  const presign = await presignRes.json();

  if (presign.mode === 'single') {
    await uploadToS3Single(presign.url, file, onProgress);
  } else {
    await uploadToS3Multipart(presign, file, onProgress);
  }

  return { s3_key: presign.key, file_size: file.size };
}

async function compileDeleteOriginal() {
  closeModal('compileDeleteModal');
  if (_vjsPlayer) _vjsPlayer.pause();
  backToGrid();
  const origId = _compileOriginalVideoId;
  if (origId) {
    try {
      await API.del(`/api/scouting/videos/${origId}`);
      Toast.success(t('scouting.video.deleted'));
    } catch (e) {
      Toast.error(t('scouting.video.delete_failed'));
    }
  }
  // Navigate to the new compiled video
  if (_compiledNewVideoId) {
    loadVideos();
    setTimeout(() => openVideo(_compiledNewVideoId), 1000);
  } else {
    loadVideos();
  }
}

function compileKeepOriginal() {
  closeModal('compileDeleteModal');
  if (_vjsPlayer) _vjsPlayer.pause();
  // Navigate to the new compiled video
  if (_compiledNewVideoId) {
    backToGrid();
    loadVideos();
    setTimeout(() => openVideo(_compiledNewVideoId), 1000);
  } else {
    backToGrid();
    loadVideos();
  }
}

function jumpToClip(clipId) {
  const clip = _clips.find(c => c.id === clipId);
  if (clip && _vjsPlayer) {
    _vjsPlayer.currentTime(clip.start_time);
    _vjsPlayer.play();
  }
  // Highlight
  document.querySelectorAll('.clip-card').forEach(c => c.classList.remove('active'));
  document.querySelector(`[data-clip-id="${clipId}"]`)?.classList.add('active');
}

/* ═══ Quick Tag (Clip Creation) ═══════════════════════════ */
function quickTag(btn) {
  if (!_vjsPlayer) return;
  const actionType = btn.dataset.action;
  const currentTime = _vjsPlayer.currentTime();
  const duration = _vjsPlayer.duration() || currentTime + 30;
  // Use I/O points if both set, otherwise default: 5s before/after
  let start, end;
  if (_clipInPoint !== null && _clipOutPoint !== null && _clipOutPoint > _clipInPoint) {
    start = _clipInPoint;
    end = _clipOutPoint;
  } else {
    start = Math.max(0, currentTime - 5);
    end = Math.min(duration, currentTime + 5);
  }

  _vjsPlayer.pause();

  document.getElementById('clipStart').value = fmtTime(start);
  document.getElementById('clipEnd').value = fmtTime(end);
  document.getElementById('clipAction').value = actionType;
  document.getElementById('clipNotes').value = '';
  _clipRating = null;
  document.getElementById('ratingPos').classList.remove('active');
  document.getElementById('ratingNeg').classList.remove('active');

  _updateClipDuration();
  openModal('clipModal');
}

/* Parse "M:SS" or "MM:SS" or raw seconds back to float */
function parseTimeInput(str) {
  if (!str) return 0;
  str = str.trim();
  if (str.includes(':')) {
    const parts = str.split(':');
    return Math.max(0, parseInt(parts[0] || 0) * 60 + parseFloat(parts[1] || 0));
  }
  return Math.max(0, parseFloat(str) || 0);
}

/* +/- 1 second buttons */
function adjustClipTime(which, delta) {
  const el = document.getElementById(which === 'start' ? 'clipStart' : 'clipEnd');
  const duration = _vjsPlayer ? (_vjsPlayer.duration() || 9999) : 9999;
  let val = parseTimeInput(el.value) + delta;
  val = Math.max(0, Math.min(duration, val));
  el.value = fmtTime(val);
  _updateClipDuration();
}

/* When user manually edits the time input */
function onClipTimeChange() {
  const duration = _vjsPlayer ? (_vjsPlayer.duration() || 9999) : 9999;
  const startEl = document.getElementById('clipStart');
  const endEl = document.getElementById('clipEnd');
  let s = parseTimeInput(startEl.value);
  let e = parseTimeInput(endEl.value);
  s = Math.max(0, Math.min(duration, s));
  e = Math.max(s + 1, Math.min(duration, e)); // end must be > start
  startEl.value = fmtTime(s);
  endEl.value = fmtTime(e);
  _updateClipDuration();
}

function _updateClipDuration() {
  const s = parseTimeInput(document.getElementById('clipStart').value);
  const e = parseTimeInput(document.getElementById('clipEnd').value);
  const dur = Math.max(0, e - s);
  const badge = document.getElementById('clipDuration');
  if (badge) badge.textContent = dur < 60 ? `${Math.round(dur)}s` : `${fmtTime(dur)}`;
}

function setRating(rating) {
  _clipRating = _clipRating === rating ? null : rating;
  document.getElementById('ratingPos').classList.toggle('active', _clipRating === 'positive');
  document.getElementById('ratingNeg').classList.toggle('active', _clipRating === 'negative');
}

async function saveClip() {
  if (!_currentVideo) return;
  const startTime = parseTimeInput(document.getElementById('clipStart').value);
  const endTime = parseTimeInput(document.getElementById('clipEnd').value);

  try {
    var actionType = document.getElementById('clipAction').value;
    if (actionType === 'other') {
      var customTag = (document.getElementById('clipCustomTag') || {}).value;
      if (customTag && customTag.trim()) {
        actionType = customTag.trim().toLowerCase().replace(/\s+/g, '_');
      }
    }
    const res = await API.post(`/api/scouting/videos/${_currentVideo.id}/clips`, {
      start_time: startTime,
      end_time: endTime,
      action_type: actionType,
      rating: _clipRating,
      notes: document.getElementById('clipNotes').value || null,
    });
    Toast.success(t('scouting.clips.tagged'));
    closeModal('clipModal');
    clearIOPoints();

    // Reload clips
    const vRes = await API.get(`/api/scouting/videos/${_currentVideo.id}`);
    _clips = vRes.data.clips || [];
    renderClipsSidebar();
    renderTimelineMarkers(); renderAnnotationTrack();
  } catch (e) {
    Toast.error(t('scouting.clips.save_failed', { error: e.message || '' }));
  }
}

async function deleteClip(clipId) {
  if (!await NpDialog.confirm(t('scouting.clips.confirm_delete'), { title: 'Delete Clip', icon: 'delete', danger: true, okText: 'Delete' })) return;
  try {
    // Find clip time range before removing
    const clip = _clips.find(c => c.id === clipId);

    await API.del(`/api/scouting/clips/${clipId}`);
    _clips = _clips.filter(c => c.id !== clipId);

    // Delete annotations within the clip's time range
    if (clip) {
      const toRemove = telestrator.annotations.filter(a =>
        a.timestamp >= clip.start_time - 0.5 && a.timestamp <= clip.end_time + 0.5
      );
      for (const ann of toRemove) {
        try { await API.del(`/api/scouting/annotations/${ann.id}`); } catch (_) {}
      }
      telestrator.annotations = telestrator.annotations.filter(a =>
        !(a.timestamp >= clip.start_time - 0.5 && a.timestamp <= clip.end_time + 0.5)
      );
      telestrator.renderFrame(_vjsPlayer ? _vjsPlayer.currentTime() : 0);
    }

    renderClipsSidebar();
    renderTimelineMarkers(); renderAnnotationTrack();
    Toast.success(t('scouting.clips.deleted'));
  } catch (e) { Toast.error(t('scouting.clips.delete_failed')); }
}

// --- NEW: Public Clip Sharing ---
async function shareClip(clipId) {
  if (!_currentVideo) return;
  try {
    const res = await API.post(`/api/scouting/clips/${clipId}/share`, { video_id: _currentVideo.id });
    const url = res.data.url;
    await navigator.clipboard.writeText(url);
    Toast.success('Share link copied to clipboard!');
  } catch (e) {
    Toast.error('Failed to create share link: ' + (e.message || ''));
  }
}

async function shareSelectedClips() {
  if (!_currentVideo) return;

  // If timeline has cards (game_intro/player_card), share as timeline
  var hasCards = _compileTimeline.some(function(item) { return item.type !== 'clip'; });
  if (hasCards && _selectedClipIds.size === 0) {
    return shareTimeline();
  }

  var clipIds = [..._selectedClipIds];
  if (clipIds.length === 0) {
    clipIds = _clips.map(function(c) { return c.id; });
  }
  if (clipIds.length === 0) { Toast.error('No clips to share'); return; }
  try {
    let res;
    if (clipIds.length === 1) {
      res = await API.post(`/api/scouting/clips/${clipIds[0]}/share`, { video_id: _currentVideo.id });
    } else {
      res = await API.post('/api/scouting/clips/share-multi', { video_id: _currentVideo.id, clip_ids: clipIds });
    }
    await navigator.clipboard.writeText(res.data.url);
    Toast.success(`Share link for ${clipIds.length} clip${clipIds.length > 1 ? 's' : ''} copied!`);
  } catch (e) {
    Toast.error('Failed to create share link: ' + (e.message || ''));
  }
}

async function shareTimeline() {
  if (!_currentVideo || !_compileTimeline.length) {
    Toast.error('No timeline to share');
    return;
  }
  var timeline = _compileTimeline.map(function(item) {
    if (item.type === 'clip') {
      return { type: 'clip', clip_id: item.clip.id };
    }
    return { type: item.type, config: item.config };
  });
  try {
    var res = await API.post('/api/scouting/share-timeline', {
      video_id: _currentVideo.id,
      timeline: timeline
    });
    await navigator.clipboard.writeText(res.data.url);
    Toast.success('Timeline share link copied!');
  } catch (e) {
    Toast.error('Failed to share timeline: ' + (e.message || ''));
  }
}
// --- END NEW ---

/* ═══ Expiry / Keep Forever / Delete Video ════════════════ */
function _updateExpiryUI() {
  const infoEl = document.getElementById('videoExpiryInfo');
  const btn = document.getElementById('keepForeverBtn');
  if (!_currentVideo || !infoEl || !btn) return;
  if (_currentVideo.keep_forever) {
    infoEl.textContent = t('scouting.expiry.permanent');
    infoEl.className = 'video-expiry-info permanent';
    btn.innerHTML = '<span class="material-symbols-outlined" style="color:#22c55e;">all_inclusive</span>';
    btn.title = t('scouting.expiry.remove_permanent_tooltip');
  } else if (_currentVideo.expires_at) {
    const exp = new Date(_currentVideo.expires_at.endsWith('Z') ? _currentVideo.expires_at : _currentVideo.expires_at + 'Z');
    const diffH = Math.max(0, (exp - new Date()) / 3600000);
    if (diffH <= 48) {
      infoEl.textContent = t('scouting.badge.hours_left', { count: Math.ceil(diffH) });
      infoEl.className = 'video-expiry-info urgent';
    } else {
      infoEl.textContent = t('scouting.badge.days_left', { count: Math.ceil(diffH / 24) });
      infoEl.className = 'video-expiry-info';
    }
    btn.innerHTML = '<span class="material-symbols-outlined">all_inclusive</span>';
    btn.title = t('scouting.expiry.keep_forever_tooltip');
  } else {
    infoEl.textContent = '';
    btn.innerHTML = '<span class="material-symbols-outlined">all_inclusive</span>';
    btn.title = t('scouting.expiry.keep_forever_tooltip');
  }
}

async function toggleKeepForever() {
  if (!_currentVideo) return;
  const newVal = !_currentVideo.keep_forever;
  try {
    await API.put(`/api/scouting/videos/${_currentVideo.id}`, { keep_forever: newVal });
    _currentVideo.keep_forever = newVal;
    if (newVal) {
      _currentVideo.expires_at = null;
      Toast.success(t('scouting.expiry.keep_forever'));
    } else {
      // Server sets expires_at to now+14d, approximate locally
      const exp = new Date();
      exp.setDate(exp.getDate() + 14);
      _currentVideo.expires_at = exp.toISOString();
      Toast.info(t('scouting.expiry.auto_delete'));
    }
    _updateExpiryUI();
  } catch (e) { Toast.error(t('scouting.expiry.update_failed')); }
}

async function deleteCurrentVideo() {
  if (!_currentVideo) return;
  if (!await NpDialog.confirm(t('scouting.video.confirm_delete'), { title: 'Delete Video', icon: 'delete_forever', danger: true, okText: 'Delete' })) return;
  try {
    await API.del(`/api/scouting/videos/${_currentVideo.id}`);
    Toast.success(t('scouting.video.deleted'));
    backToGrid();
    loadVideos();
  } catch (e) { Toast.error(t('scouting.video.delete_failed')); }
}

/* telestrator → scouting-telestrator.js */

/* ═══ Drawing Toolbar Helpers ═════════════════════════════ */
function setDrawTool(btn) {
  const tool = btn.dataset.tool;
  document.querySelectorAll('.tool-btn[data-tool]').forEach(b => b.classList.remove('active'));

  if (telestrator.tool === tool) {
    telestrator.setTool(null);
  } else {
    btn.classList.add('active');
    telestrator.setTool(tool);
  }

  // Show stroke/color settings only for freehand & arrow
  const activeTool = telestrator.tool;
  const sliders = document.getElementById('drawSliders');
  if (sliders) sliders.style.display = (activeTool === 'freehand' || activeTool === 'arrow') ? '' : 'none';
}

function setDrawColor(color) { telestrator.setColor(color); }

function setStrokeWidth(val) {
  telestrator.strokeWidth = parseInt(val) || 3;
  const preview = document.getElementById('strokeWidthPreview');
  if (preview) { preview.style.width = val + 'px'; preview.style.height = val + 'px'; }
}
function setDrawOpacity(val) {
  telestrator.opacity = parseInt(val) / 100;
  telestrator.renderFrame(_vjsPlayer ? _vjsPlayer.currentTime() : 0);
}
function undoDraw() { telestrator.undo(); }
async function clearDrawings() {
  if (await NpDialog.confirm(t('scouting.draw.clear_confirm'), { title: 'Clear All', icon: 'ink_eraser', okText: 'Clear' })) telestrator.clearAll();
}

/* ═══════════════════════════════════════════════════════════════════════════
   §5  PLAYLISTS
   Playlist CRUD, sidebar tabs, batch clip operations
   ═══════════════════════════════════════════════════════════════════════════ */

/* ═══ Playlists (Phase 3.1) ═════════════════════════════════ */
let _playlists = [];
let _activePlaylist = null;
let _clipSidebarTab = 'clips'; // 'clips' or 'playlists'
let _clipFilterType = ''; // action_type filter for clips sidebar

async function loadPlaylists() {
  try {
    const res = await API.get('/api/scouting/playlists', { silent: true });
    _playlists = res.data || [];
    if (_clipSidebarTab === 'playlists') renderPlaylistsSidebar();
  } catch (e) { /* playlists not available yet */ }
}

function switchClipSidebarTab(tab) {
  _clipSidebarTab = tab;
  document.querySelectorAll('.clips-tab-btn').forEach(b => b.classList.toggle('active', b.dataset.tab === tab));
  if (tab === 'clips') {
    renderClipsSidebar();
  } else {
    renderPlaylistsSidebar();
  }
}

function renderPlaylistsSidebar() {
  const el = document.getElementById('clipsList');
  if (!_playlists.length) {
    el.innerHTML = `<div style="text-align:center;padding:var(--sp-4);">
      <p style="color:var(--text-muted);font-size:0.82rem;">${t('scouting.playlist.empty')}</p>
      <button class="btn btn-primary btn-sm" onclick="createPlaylistPrompt()" style="margin-top:var(--sp-2);">${t('scouting.playlist.create')}</button>
    </div>`;
    return;
  }
  el.innerHTML = _playlists.map(p => `
    <div class="clip-card playlist-card" data-playlist-id="${p.id}">
      <div class="clip-card-header">
        <span class="clip-card-action">📋 ${esc(p.name)}</span>
        <span style="color:var(--text-muted);font-size:0.72rem;">${t('scouting.playlist.clips', { count: p.item_count })}</span>
      </div>
      <div style="display:flex;gap:4px;margin-top:4px;">
        <button class="btn btn-ghost" style="font-size:0.68rem;padding:2px 6px;" onclick="event.stopPropagation(); deletePlaylist(${p.id})">Delete</button>
      </div>
    </div>
  `).join('') + `<button class="btn btn-ghost" onclick="createPlaylistPrompt()" style="width:100%;margin-top:var(--sp-2);font-size:0.78rem;">${t('scouting.playlist.new')}</button>`;
}

async function createPlaylistPrompt() {
  const name = await NpDialog.prompt(t('scouting.playlist.prompt'), { title: 'New Playlist', icon: 'playlist_add', placeholder: 'Playlist name...' });
  if (!name) return;
  try {
    await API.post('/api/scouting/playlists', { name });
    Toast.success(t('scouting.playlist.created'));
    loadPlaylists();
  } catch (e) { Toast.error(t('scouting.playlist.create_failed')); }
}

async function deletePlaylist(id) {
  if (!await NpDialog.confirm(t('scouting.playlist.confirm_delete'), { title: 'Delete Playlist', icon: 'delete', danger: true, okText: 'Delete' })) return;
  try {
    await API.del(`/api/scouting/playlists/${id}`);
    _playlists = _playlists.filter(p => p.id !== id);
    renderPlaylistsSidebar();
    Toast.success(t('scouting.playlist.deleted'));
  } catch (e) { Toast.error(t('scouting.playlist.delete_failed')); }
}

async function addClipToPlaylist(clipId) {
  if (!_playlists.length) {
    Toast.error(t('scouting.playlist.create_first'));
    return;
  }
  // Simple: add to first playlist. TODO: playlist selector
  const pl = _playlists[0];
  try {
    await API.post(`/api/scouting/playlists/${pl.id}/items`, { clip_id: clipId });
    Toast.success(t('scouting.playlist.added', { name: pl.name }));
    loadPlaylists();
  } catch (e) { Toast.error(t('scouting.playlist.add_failed')); }
}

/* ═══ Batch Clip Operations (Phase 3.3) ════════════════════ */
let _selectedClipIds = new Set();

function toggleClipSelection(clipId, e) {
  e.stopPropagation();
  if (_selectedClipIds.has(clipId)) {
    _selectedClipIds.delete(clipId);
  } else {
    _selectedClipIds.add(clipId);
  }
  _updateBatchBar();
  document.querySelector(`[data-clip-id="${clipId}"]`)?.classList.toggle('batch-selected', _selectedClipIds.has(clipId));
}

function selectAllClips() {
  _clips.forEach(c => _selectedClipIds.add(c.id));
  _updateBatchBar();
  document.querySelectorAll('.clip-card').forEach(c => c.classList.add('batch-selected'));
}

function deselectAllClips() {
  _selectedClipIds.clear();
  _updateBatchBar();
  document.querySelectorAll('.clip-card').forEach(c => c.classList.remove('batch-selected'));
}

function _updateBatchBar() {
  const bar = document.getElementById('batchBar');
  if (!bar) return;
  if (_selectedClipIds.size > 0) {
    bar.style.display = 'flex';
    bar.querySelector('.batch-count').textContent = t('scouting.batch.selected', { count: _selectedClipIds.size });
  } else {
    bar.style.display = 'none';
  }
}

async function batchDeleteClips() {
  if (!_selectedClipIds.size) return;
  if (!await NpDialog.confirm(t('scouting.batch.delete_confirm', { count: _selectedClipIds.size }), { title: 'Delete Clips', icon: 'delete_sweep', danger: true, okText: 'Delete All' })) return;
  try {
    await API.post('/api/scouting/clips/batch-delete', { clip_ids: [..._selectedClipIds] });
    _clips = _clips.filter(c => !_selectedClipIds.has(c.id));
    _selectedClipIds.clear();
    renderClipsSidebar();
    renderTimelineMarkers(); renderAnnotationTrack();
    _updateBatchBar();
    Toast.success(t('scouting.batch.deleted'));
  } catch (e) { Toast.error(t('scouting.batch.delete_failed')); }
}

async function batchRateClips(rating) {
  if (!_selectedClipIds.size) return;
  try {
    await API.post('/api/scouting/clips/batch-update', { clip_ids: [..._selectedClipIds], rating });
    _clips.forEach(c => { if (_selectedClipIds.has(c.id)) c.rating = rating; });
    renderClipsSidebar();
    Toast.success(t('scouting.batch.updated'));
  } catch (e) { Toast.error(t('scouting.batch.update_failed')); }
}

/* ═══════════════════════════════════════════════════════════════════════════
   §6  COMPARISON MODE
   Zoom controls, side-by-side comparison, clip export with annotations
   ═══════════════════════════════════════════════════════════════════════════ */

/* ═══ Phase 4.1: Zoom Controls ═════════════════════════════ */
function zoomIn() { telestrator.setZoom(telestrator.zoom + 0.5); }
function zoomOut() { telestrator.setZoom(telestrator.zoom - 0.5); }
function resetZoom() { telestrator.resetZoom(); }

function _updateZoomBadge() {
  const badge = document.getElementById('zoomBadge');
  if (!badge) return;
  if (telestrator.zoom > 1) {
    badge.textContent = `${telestrator.zoom.toFixed(1)}x`;
    badge.style.display = '';
  } else {
    badge.style.display = 'none';
  }
}

/* ═══ Phase 4.2: Side-by-Side Comparison ═══════════════════ */
let _comparisonMode = false;
let _compPlayer2 = null;

function toggleComparisonMode() {
  _comparisonMode = !_comparisonMode;
  const layout = document.querySelector('.analysis-layout');
  const compPanel = document.getElementById('comparisonPanel');
  const compBtn = document.getElementById('comparisonBtn');

  if (_comparisonMode) {
    layout?.classList.add('comparison-mode');
    if (compPanel) compPanel.style.display = '';
    if (compBtn) compBtn.classList.add('active');
    _initComparisonPlayer();
  } else {
    layout?.classList.remove('comparison-mode');
    if (compPanel) compPanel.style.display = 'none';
    if (compBtn) compBtn.classList.remove('active');
    _disposeComparisonPlayer();
  }
}

function _initComparisonPlayer() {
  const container = document.getElementById('compVideo2Container');
  if (!container) return;
  container.innerHTML = '<video id="compPlayer2" class="video-js vjs-default-skin" playsinline></video>';
  // Don't init until user picks a video
}

function loadComparisonVideo(videoId) {
  const video = _videos.find(v => v.id === parseInt(videoId));
  if (!video) return;

  if (_compPlayer2) { _compPlayer2.dispose(); _compPlayer2 = null; }

  const container = document.getElementById('compVideo2Container');
  container.innerHTML = '<video id="compPlayer2" class="video-js vjs-default-skin" playsinline></video>';

  const sources = [];
  // --- NEW: Hybrid Video Architecture — handle external URLs in comparison ---
  if (video.source_type === 'external' && video.external_url) {
    const embedUrl = _getEmbedUrl(video.external_url);
    if (embedUrl) {
      container.innerHTML = `<iframe src="${embedUrl}" style="width:100%;aspect-ratio:16/9;border:none;border-radius:var(--radius);" allowfullscreen allow="autoplay; encrypted-media; picture-in-picture"></iframe>`;
      return;
    }
    const url = video.external_url;
    if (url.includes('.m3u8')) {
      sources.push({ src: url, type: 'application/x-mpegURL' });
    } else {
      sources.push({ src: '/api/scouting/video-proxy/' + video.id, type: 'video/mp4' });
    }
  }
  // --- END NEW ---
  if (video.source_type === 's3') {
    if (video.s3_url) {
      sources.push({ src: video.s3_url, type: 'video/mp4' });
    } else {
      sources.push({ src: '/api/scouting/video-proxy/' + video.id, type: 'video/mp4' });
    }
  }

  if (!sources.length) {
    container.innerHTML = `<div style="display:flex;align-items:center;justify-content:center;height:200px;color:var(--text-muted);flex-direction:column;gap:var(--sp-2);">
      <span class="material-symbols-outlined" style="font-size:36px;">videocam_off</span>
      <p>Video media file is not available</p>
    </div>`;
    return;
  }

  _compPlayer2 = videojs('compPlayer2', {
    controls: true,
    fluid: true,
    sources: sources,
    html5: { vhs: { overrideNative: false }, nativeAudioTracks: true, nativeVideoTracks: true },
  });

  document.getElementById('compTitle2').textContent = video.title;

  // Sync controls
  const syncCheckbox = document.getElementById('compSync');
  if (syncCheckbox?.checked) _setupCompSync();
}

function _setupCompSync() {
  if (!_vjsPlayer || !_compPlayer2) return;
  _vjsPlayer.on('play', () => { if (document.getElementById('compSync')?.checked) _compPlayer2.play(); });
  _vjsPlayer.on('pause', () => { if (document.getElementById('compSync')?.checked) _compPlayer2.pause(); });
  _vjsPlayer.on('seeked', () => {
    if (document.getElementById('compSync')?.checked) _compPlayer2.currentTime(_vjsPlayer.currentTime());
  });
}

function _disposeComparisonPlayer() {
  if (_compPlayer2) { _compPlayer2.dispose(); _compPlayer2 = null; }
}

/* ═══ Phase 4.3: Clip Export (burn-in annotations) ═════════ */
let _exporting = false;

async function exportClipWithAnnotations() {
  if (_exporting) return;
  if (!_vjsPlayer || !_currentVideo) { Toast.error(t('scouting.export.no_video')); return; }

  // Use I/O points or currently playing clip
  let startTime = _clipInPoint;
  let endTime = _clipOutPoint;
  if (startTime === null || endTime === null || endTime <= startTime) {
    // Try to find the active clip
    const activeCard = document.querySelector('.clip-card.active');
    if (activeCard) {
      const clipId = parseInt(activeCard.dataset.clipId);
      const clip = _clips.find(c => c.id === clipId);
      if (clip) { startTime = clip.start_time; endTime = clip.end_time; }
    }
  }
  if (startTime === null || endTime === null || endTime <= startTime) {
    Toast.error('Set In/Out points (I/O keys) or select a clip first');
    return;
  }

  _exporting = true;
  const exportBtn = document.getElementById('exportBtn');
  const progressBar = document.getElementById('exportProgress');
  if (exportBtn) exportBtn.disabled = true;
  if (progressBar) progressBar.style.display = '';

  try {
    // Create hidden compositing canvas
    const videoEl = _vjsPlayer.tech({ IWillNotUseThisInPlugins: true })?.el_;
    if (!videoEl) throw new Error('Cannot access video element');

    const vw = videoEl.videoWidth || 1280;
    const vh = videoEl.videoHeight || 720;
    const compCanvas = document.createElement('canvas');
    compCanvas.width = vw;
    compCanvas.height = vh;
    const compCtx = compCanvas.getContext('2d');

    // Setup MediaRecorder
    const stream = compCanvas.captureStream(30);
    const recorder = new MediaRecorder(stream, {
      mimeType: MediaRecorder.isTypeSupported('video/webm;codecs=vp9') ? 'video/webm;codecs=vp9' : 'video/webm',
      videoBitsPerSecond: 5000000,
    });
    const chunks = [];
    recorder.ondataavailable = (e) => { if (e.data.size > 0) chunks.push(e.data); };

    const exportDone = new Promise((resolve) => { recorder.onstop = resolve; });

    // Seek to start and play at 1x
    _vjsPlayer.playbackRate(1);
    _vjsPlayer.currentTime(startTime);
    await new Promise(r => setTimeout(r, 300)); // wait for seek

    recorder.start();
    _vjsPlayer.play();

    const duration = endTime - startTime;

    // Frame capture loop
    const captureFrame = () => {
      if (!_exporting) { recorder.stop(); return; }
      const t = _vjsPlayer.currentTime();

      if (t >= endTime) {
        _vjsPlayer.pause();
        recorder.stop();
        return;
      }

      // Draw video frame
      compCtx.drawImage(videoEl, 0, 0, vw, vh);

      // Draw annotations at current time (scale from display canvas to export canvas)
      const origW = telestrator.canvas.width;
      const origH = telestrator.canvas.height;
      telestrator.canvas.width = vw;
      telestrator.canvas.height = vh;
      telestrator.renderFrame(t);
      compCtx.drawImage(telestrator.canvas, 0, 0);
      telestrator.canvas.width = origW;
      telestrator.canvas.height = origH;

      // Update progress
      const pct = Math.round(((t - startTime) / duration) * 100);
      if (progressBar) progressBar.querySelector('.export-progress-fill').style.width = pct + '%';

      requestAnimationFrame(captureFrame);
    };
    requestAnimationFrame(captureFrame);

    await exportDone;

    // Create download
    const blob = new Blob(chunks, { type: 'video/webm' });
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url;
    a.download = `${_currentVideo.title || 'clip'}_export.webm`;
    a.click();
    URL.revokeObjectURL(url);
    Toast.success('Export complete!');

  } catch (e) {
    console.error('Export error:', e);
    Toast.error('Export failed: ' + (e.message || ''));
  } finally {
    _exporting = false;
    _vjsPlayer.pause();
    if (exportBtn) exportBtn.disabled = false;
    if (progressBar) progressBar.style.display = 'none';
    // Restore canvas size
    telestrator._resizeCanvas();
    telestrator.renderFrame(_vjsPlayer ? _vjsPlayer.currentTime() : 0);
  }
}

function cancelExport() {
  _exporting = false;
}

/* ═══ Utilities ═══════════════════════════════════════════ */
function fmtTime(s) {
  if (!s || isNaN(s) || !isFinite(s)) return '0:00';
  const m = Math.floor(s / 60);
  const sec = Math.floor(s % 60);
  return `${m}:${sec.toString().padStart(2, '0')}`;
}

/* esc → shared-utils.js */
