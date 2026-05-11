/* /org/dashboard — role-aware tiles + activity feed.
 *
 * Hydrates from two JSON endpoints:
 *   GET /org/api/dashboard/role-stats    → tile list (labels + hrefs)
 *   GET /org/api/dashboard/recent-activity → audit ribbon
 *
 * Security: every value lands via Node.textContent or setAttribute.
 */

(function () {
  "use strict";

  var ctx = window.ORG_CTX || { role: "viewer", user_id: null };
  var $tiles = document.querySelector("[data-tiles]");
  var $activity = document.querySelector("[data-activity]");
  var $subtitle = document.querySelector("[data-role-subtitle]");

  var ROLE_SUBTITLES = {
    org_admin: "סקירה כללית של הארגון. הכל בידיים שלך.",
    region_manager: "התצוגה הזו מסוננת למחוז שלך.",
    branch_manager: "התצוגה הזו מסוננת לסניף שלך.",
    coach: "הקבוצות והשחקנים שלך, וקיצור לאפליקציית המאמן.",
    viewer: "תצוגת צופה — קריאה בלבד.",
  };

  function el(tag, opts, children) {
    var n = document.createElement(tag);
    if (opts) {
      if (opts.className) n.className = opts.className;
      if (opts.text != null) n.textContent = opts.text;
      if (opts.attrs) Object.keys(opts.attrs).forEach(function (k) { n.setAttribute(k, opts.attrs[k]); });
    }
    if (children) children.forEach(function (c) { n.appendChild(c); });
    return n;
  }

  async function api(method, url) {
    var r = await fetch(url, { method: method, headers: { "Accept": "application/json" } });
    if (!r.ok) {
      var data = null;
      try { data = await r.json(); } catch (_e) {}
      throw new Error((data && data.detail) || ("שגיאה " + r.status));
    }
    return r.json();
  }

  function renderSubtitle() {
    $subtitle.textContent = ROLE_SUBTITLES[ctx.role] || "ברוך הבא.";
  }

  function renderTile(t) {
    var inner = el("div", null, [
      el("div", { className: "org-stat-label", text: t.label }),
      el("div", { className: "org-stat-value", text: String(t.value) }),
    ]);
    if (t.href) {
      var a = el("a", {
        className: "org-stat",
        attrs: { href: t.href, style: "text-decoration: none; color: inherit; display: block;" },
      }, [inner]);
      return a;
    }
    return el("div", { className: "org-stat" }, [inner]);
  }

  async function loadTiles() {
    try {
      var data = await api("GET", "/org/api/dashboard/role-stats");
      var tiles = data.tiles || [];
      if (!tiles.length) {
        $tiles.replaceChildren(
          el("div", { className: "org-empty" }, [
            el("div", { className: "org-empty-title", text: "אין נתונים להצגה" }),
            el("div", { className: "org-empty-body", text: data.warning || "אם זה נראה לא נכון, פנה למנהל הארגון." }),
          ])
        );
        return;
      }
      $tiles.replaceChildren.apply($tiles, tiles.map(renderTile));
    } catch (e) {
      $tiles.replaceChildren(
        el("div", { className: "org-empty", text: e.message })
      );
    }
  }

  function formatTimestamp(iso) {
    if (!iso) return "—";
    var d = new Date(iso);
    if (isNaN(d.getTime())) return "—";
    // Render relative for recent events, then fall back to date.
    var diffMs = Date.now() - d.getTime();
    var mins = Math.floor(diffMs / 60000);
    if (mins < 1) return "עכשיו";
    if (mins < 60) return "לפני " + mins + " דקות";
    var hours = Math.floor(mins / 60);
    if (hours < 24) return "לפני " + hours + " שעות";
    return d.toLocaleDateString("he-IL");
  }

  function renderActivity(events) {
    if (!events.length) {
      $activity.replaceChildren(
        el("p", { className: "org-text-sm org-text-muted", text: "אין פעילות אחרונה." })
      );
      return;
    }
    var list = el("ul", { className: "org-activity-list", attrs: { style: "list-style: none; padding: 0; margin: 0;" } });
    events.forEach(function (ev) {
      var when = el("span", { className: "org-text-sm org-text-muted", text: formatTimestamp(ev.created_at) });
      var who = el("span", { text: (ev.actor_email || "—").split("@")[0] });
      var what = el("span", {
        className: "org-pill",
        text: ev.action_label,
        attrs: { style: "margin-inline-start: var(--org-space-2);" },
      });
      var row = el("li", {
        attrs: {
          style: "display: flex; align-items: center; gap: var(--org-space-3); "
            + "padding: var(--org-space-3) 0; "
            + "border-block-end: 1px solid var(--org-gray-100);",
        },
      }, [
        el("div", { attrs: { style: "flex: 1;" } }, [who, what]),
        when,
      ]);
      list.appendChild(row);
    });
    $activity.replaceChildren(list);
  }

  async function loadActivity() {
    try {
      var data = await api("GET", "/org/api/dashboard/recent-activity?limit=20");
      renderActivity(data.events || []);
    } catch (e) {
      $activity.replaceChildren(
        el("p", { className: "org-text-sm org-text-muted", text: e.message })
      );
    }
  }

  async function boot() {
    renderSubtitle();
    await Promise.all([loadTiles(), loadActivity()]);
  }
  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", boot);
  } else {
    boot();
  }
})();
