/**
 * Auth — auto-refresh + CSRF header injection.
 * Included in base.html for all authenticated pages.
 */
(function() {
    // Auto-refresh every 25 minutes (token expires at 30)
    var REFRESH_INTERVAL = 25 * 60 * 1000;

    async function refreshToken() {
        for (var attempt = 0; attempt < 3; attempt++) {
            try {
                var res = await fetch('/api/auth/refresh', { method: 'POST' });
                if (res.ok) return;
                if (res.status === 401) {
                    window.location.href = '/login';
                    return;
                }
            } catch (e) { /* network error — retry */ }
            await new Promise(function(r) { setTimeout(r, 2000 * (attempt + 1)); });
        }
        window.location.href = '/login';
    }

    setInterval(refreshToken, REFRESH_INTERVAL);

    // Patch global fetch to automatically add X-Requested-With header (CSRF protection)
    var _origFetch = window.fetch;
    window.fetch = function(url, opts) {
        opts = opts || {};
        // Only add to same-origin API requests
        if (typeof url === 'string' && url.startsWith('/api/')) {
            opts.headers = opts.headers || {};
            if (opts.headers instanceof Headers) {
                if (!opts.headers.has('X-Requested-With')) {
                    opts.headers.set('X-Requested-With', 'XMLHttpRequest');
                }
            } else {
                opts.headers['X-Requested-With'] = opts.headers['X-Requested-With'] || 'XMLHttpRequest';
            }
        }
        return _origFetch.call(this, url, opts);
    };
})();
