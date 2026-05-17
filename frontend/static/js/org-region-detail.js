/* /org/regions/{id} — region drill-down page.
 *
 * Hydrates from a single endpoint /org/api/regions/{id}/detail which returns
 * region info + parent program + teams + members + today's practices.
 * Security: every value lands via Node.textContent.
 */
(function () {
  "use strict";

  var ctx = window.ORG_REGION || { region_id: null };
  var regionId = ctx.region_id;
  if (!regionId) return;

  // Phase 13 — tenant URL prefix (legacy "/org" or slug-prefixed).
  var URL_PREFIX = (window.__ORG_ACTIVE__ && window.__ORG_ACTIVE__.url_prefix) || "/org";

  var $title = document.querySelector("[data-region-title]");
  var $program = document.querySelector("[data-region-program]");
  var $tiles = document.querySelector("[data-region-tiles]");
  var $teams = document.querySelector("[data-region-teams]");
  var $members = document.querySelector("[data-region-members]");
  var $practices = document.querySelector("[data-region-practices]");

  var ROLE_LABELS = {
    org_admin: "מנכ\"ל",
    program_manager: "מנהל תוכנית",
    region_manager: "מנהל מחוז",
    branch_manager: "מנהל סניף",
    coach: "מאמן",
    viewer: "צופה",
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

  async function api(url) {
    var r = await fetch(url, {
      method: "GET",
      headers: { "Accept": "application/json", "X-Requested-With": "XMLHttpRequest" },
      credentials: "same-origin",
    });
    if (r.status === 404) throw new Error("המחוז לא נמצא");
    if (!r.ok) {
      var d = null;
      try { d = await r.json(); } catch (_) {}
      throw new Error((d && d.detail) || ("שגיאה " + r.status));
    }
    return r.json();
  }

  function fmtTime(iso) {
    if (!iso) return "—";
    var d = new Date(iso);
    if (isNaN(d.getTime())) return "—";
    return d.toLocaleTimeString("he-IL", { hour: "2-digit", minute: "2-digit" });
  }

  function renderHeader(data) {
    $title.textContent = data.region.name;
    if (data.program && data.program.name) {
      $program.textContent = "תוכנית · " + data.program.name;
    } else {
      $program.textContent = "ללא תוכנית משויכת";
    }
    var crumb = document.getElementById("crumb-region-name");
    if (crumb) crumb.textContent = data.region.name || "—";
    document.title = (data.region.name || "מחוז") + " — NEXTPLAY Enterprise";
  }

  function renderTiles(data) {
    var counts = [
      { label: "קבוצות במחוז", value: data.teams.length },
      { label: "צוות במחוז", value: data.members.length },
      { label: "אימוני היום", value: data.practices_today.length },
    ];
    $tiles.replaceChildren.apply($tiles, counts.map(function (c) {
      return el("div", { className: "org-stat" }, [
        el("div", { className: "org-stat-label", text: c.label }),
        el("div", { className: "org-stat-value", text: String(c.value) }),
      ]);
    }));
  }

  function renderTeams(teams) {
    if (!teams.length) {
      $teams.replaceChildren(
        el("tr", null, [el("td", { className: "org-table-empty", text: "אין קבוצות במחוז.", attrs: { colspan: "3" } })])
      );
      return;
    }
    $teams.replaceChildren.apply($teams, teams.map(function (t) {
      var href = URL_PREFIX + "/teams/" + t.id;
      var nameLink = el("a", {
        text: t.team_name || "—",
        attrs: { href: href, style: "color: inherit; text-decoration: none; font-weight: 600;" },
      });
      var nameCell = el("td", null, [nameLink]);
      var leagueParts = [t.league, t.division].filter(Boolean).join(" · ");
      var leagueCell = el("td", { className: "org-text-sm org-text-muted", text: leagueParts || "—" });
      var coachCell = el("td", { className: "org-text-sm",
        text: t.coach_display_name || "ללא מאמן" });
      var tr = el("tr", null, [nameCell, leagueCell, coachCell]);
      tr.style.cursor = "pointer";
      tr.addEventListener("click", function (e) {
        if (e.target.closest("a")) return;
        window.location.href = href;
      });
      return tr;
    }));
  }

  function renderMembers(members) {
    if (!members.length) {
      $members.replaceChildren(
        el("tr", null, [el("td", { className: "org-table-empty", text: "אין צוות משויך למחוז.", attrs: { colspan: "3" } })])
      );
      return;
    }
    $members.replaceChildren.apply($members, members.map(function (m) {
      return el("tr", null, [
        el("td", null, [el("strong", { text: m.display_name || "—" })]),
        el("td", { className: "org-text-sm", text: m.email || "—", attrs: { dir: "ltr" } }),
        el("td", null, [
          el("span", {
            className: "org-pill",
            text: ROLE_LABELS[m.role] || m.role,
          }),
        ]),
      ]);
    }));
  }

  function renderPractices(practices) {
    if (!practices.length) {
      $practices.replaceChildren(
        el("p", { className: "org-text-sm org-text-muted", text: "אין אימונים מתוכננים להיום." })
      );
      return;
    }
    var list = el("ul", { className: "org-activity-list", attrs: { style: "list-style: none; padding: 0; margin: 0;" } });
    practices.forEach(function (p) {
      var time = el("span", { className: "org-text-sm org-text-muted", text: fmtTime(p.scheduled_at) });
      var team = el("div", { text: p.team_name || "—", attrs: { style: "font-weight: 600;" } });
      var meta = el("div", {
        className: "org-text-sm org-text-muted",
        text: [p.title, p.location].filter(Boolean).join(" · ") || "",
      });
      list.appendChild(el("li", {
        attrs: {
          style: "display: flex; align-items: flex-start; gap: var(--org-space-3); "
            + "padding: var(--org-space-3) 0; "
            + "border-block-end: 1px solid var(--org-gray-100);",
        },
      }, [
        el("div", { attrs: { style: "flex: 1;" } }, [team, meta]),
        time,
      ]));
    });
    $practices.replaceChildren(list);
  }

  async function boot() {
    try {
      var data = await api("/org/api/regions/" + regionId + "/detail");
      renderHeader(data);
      renderTiles(data);
      renderTeams(data.teams);
      renderMembers(data.members);
      renderPractices(data.practices_today);
    } catch (e) {
      $title.textContent = e.message;
      $tiles.replaceChildren(el("div", { className: "org-empty", text: e.message }));
    }
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", boot);
  } else {
    boot();
  }
})();
