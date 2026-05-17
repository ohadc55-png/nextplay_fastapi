/* /org/dashboard — Phase 12 refresh.
 *
 * Hydrates from FOUR JSON endpoints in parallel:
 *   GET /org/api/dashboard/role-stats        → KPI tiles (role-aware)
 *   GET /org/api/dashboard/breakdown         → bars + donut (role-aware, both nullable)
 *   GET /org/api/dashboard/recent-activity   → audit ribbon
 *   GET /org/api/practice/today              → today's training list
 *
 * Plus one for the mini calendar at the bottom:
 *   GET /org/api/practice/range?start=…&end=…  → current month's sessions
 *
 * Security: every value lands via Node.textContent or setAttribute (no innerHTML).
 * Role gating: action buttons (data-actions-host) show for org_admin/PM/RM only.
 */

(function () {
  "use strict";

  var ctx = window.ORG_CTX || { role: "viewer", user_id: null };
  var WRITE_ROLES = ["org_admin", "program_manager", "region_manager"];
  var canWrite = WRITE_ROLES.indexOf(ctx.role) >= 0;

  var $tiles       = document.querySelector("[data-tiles]");
  var $activity    = document.querySelector("[data-activity]");
  var $practice    = document.querySelector("[data-practice-today]");
  var $subtitle    = document.querySelector("[data-role-subtitle]");
  var $actions     = document.querySelector("[data-actions-host]");
  var $barsPanel   = document.querySelector("[data-bars-panel]");
  var $barsTitle   = document.querySelector("[data-bars-title]");
  var $barsBody    = document.querySelector("[data-bars-body]");
  var $donutPanel  = document.querySelector("[data-donut-panel]");
  var $donutTitle  = document.querySelector("[data-donut-title]");
  var $donutBody   = document.querySelector("[data-donut-body]");
  var $miniCal     = document.querySelector("[data-mini-cal]");
  var $miniCalTitle= document.querySelector("[data-mini-cal-title]");

  var ROLE_SUBTITLES = {
    org_admin:       "סקירה כללית של הארגון. הכל בידיים שלך.",
    program_manager: "התצוגה הזו מסוננת לתוכנית שלך.",
    region_manager:  "התצוגה הזו מסוננת למחוז שלך.",
    coach:           "הקבוצות והשחקנים שלך, וקיצור לאפליקציית המאמן.",
    viewer:          "תצוגת צופה — קריאה בלבד.",
  };
  var MONTH_HE = [
    "ינואר","פברואר","מרץ","אפריל","מאי","יוני",
    "יולי","אוגוסט","ספטמבר","אוקטובר","נובמבר","דצמבר",
  ];
  // Donut color palette — 6 stops in the blue→cyan family so segments read
  // as one cohesive scale rather than random hues.
  var DONUT_COLORS = ["#3B82F6", "#06B6D4", "#7DD3FC", "#1E3A8A", "#67E8F9", "#0E7490"];

  // ---- DOM helpers ----
  function el(tag, opts, children) {
    var n = document.createElement(tag);
    if (opts) {
      if (opts.className) n.className = opts.className;
      if (opts.text != null) n.textContent = opts.text;
      if (opts.attrs) Object.keys(opts.attrs).forEach(function (k) {
        n.setAttribute(k, opts.attrs[k]);
      });
    }
    if (children) children.forEach(function (c) { c && n.appendChild(c); });
    return n;
  }
  function svgEl(tag, attrs) {
    var n = document.createElementNS("http://www.w3.org/2000/svg", tag);
    if (attrs) Object.keys(attrs).forEach(function (k) { n.setAttribute(k, attrs[k]); });
    return n;
  }
  function pad(n) { return n < 10 ? "0" + n : String(n); }
  function ymd(d) { return d.getFullYear() + "-" + pad(d.getMonth() + 1) + "-" + pad(d.getDate()); }

  async function api(method, url) {
    var r = await fetch(url, {
      method: method,
      headers: { "Accept": "application/json", "X-Requested-With": "XMLHttpRequest" },
      credentials: "same-origin",
    });
    if (!r.ok) {
      var data = null;
      try { data = await r.json(); } catch (_e) {}
      throw new Error((data && data.detail) || ("שגיאה " + r.status));
    }
    return r.json();
  }

  // Counter animation — counts the KPI value from 0 → target. Respects
  // prefers-reduced-motion. Numbers stay tabular so the value doesn't dance.
  function animateCounter(node, end, suffix) {
    suffix = suffix || "";
    if (window.matchMedia("(prefers-reduced-motion: reduce)").matches) {
      node.textContent = end.toLocaleString("en") + suffix;
      return;
    }
    var dur = 1100, start = performance.now();
    function tick(t) {
      var p = Math.min((t - start) / dur, 1);
      var eased = 1 - Math.pow(1 - p, 3);
      node.textContent = Math.round(end * eased).toLocaleString("en") + suffix;
      if (p < 1) requestAnimationFrame(tick);
    }
    requestAnimationFrame(tick);
  }

  // ---- Subtitle / actions toggle ----
  function renderSubtitle(programName) {
    var base = ROLE_SUBTITLES[ctx.role] || "ברוך הבא.";
    if (programName && (ctx.role === "program_manager" || ctx.role === "region_manager")) {
      base = base + " · " + programName;
    }
    $subtitle.textContent = base;
  }

  // ---- KPI tiles ----
  function renderTile(t) {
    var labelNode = el("div", { className: "org-kpi__label", text: t.label });
    var valueNode = el("div", { className: "org-kpi__value", text: "0" });

    var body = el("div", null, [labelNode, valueNode]);

    // Numeric tiles get a counter animation. Non-numeric values (e.g. "—")
    // bypass the animation and render verbatim.
    var raw = String(t.value);
    var numMatch = raw.match(/^(\d+(?:[,.]\d+)*)(\D.*)?$/);
    if (numMatch) {
      var n = parseInt(numMatch[1].replace(/[,.]/g, ""), 10);
      var suffix = numMatch[2] || "";
      requestAnimationFrame(function () { animateCounter(valueNode, n, suffix); });
    } else {
      valueNode.textContent = raw;
    }

    if (t.href) {
      return el("a", {
        className: "org-kpi",
        attrs: { href: t.href, style: "text-decoration: none; color: inherit; display: block;" },
      }, [body]);
    }
    return el("div", { className: "org-kpi" }, [body]);
  }

  async function loadTiles() {
    try {
      var data = await api("GET", "/org/api/dashboard/role-stats");
      if (data.program_name) renderSubtitle(data.program_name);
      var tiles = (data.tiles || []).slice(0, 4);    // cap at 4 for the new grid
      if (!tiles.length) {
        $tiles.replaceChildren(el("div", { className: "org-empty" }, [
          el("div", { className: "org-empty-title", text: "אין נתונים להצגה" }),
          el("div", { className: "org-empty-body", text: data.warning || "אם זה נראה לא נכון, פנה למנהל הארגון." }),
        ]));
        return;
      }
      $tiles.replaceChildren.apply($tiles, tiles.map(renderTile));
    } catch (e) {
      $tiles.replaceChildren(el("div", { className: "org-empty", text: e.message }));
    }
  }

  // ---- Activity feed ----
  function formatTimestamp(iso) {
    if (!iso) return "—";
    var d = new Date(iso);
    if (isNaN(d.getTime())) return "—";
    var diffMs = Date.now() - d.getTime();
    var mins = Math.floor(diffMs / 60000);
    if (mins < 1) return "עכשיו";
    if (mins < 60) return "לפני " + mins + " דקות";
    var hours = Math.floor(mins / 60);
    if (hours < 24) return "לפני " + hours + " שעות";
    return d.toLocaleDateString("he-IL");
  }

  // Rotate dot colors so the feed isn't a single-color stripe. Hash by
  // action so the same action type always lands on the same color.
  function dotClassFor(action) {
    if (!action) return "org-list-dot";
    var h = 0;
    for (var i = 0; i < action.length; i++) {
      h = ((h << 5) - h) + action.charCodeAt(i);
      h |= 0;
    }
    var bucket = Math.abs(h) % 3;
    if (bucket === 0) return "org-list-dot";
    if (bucket === 1) return "org-list-dot org-list-dot--cyan";
    return "org-list-dot org-list-dot--gray";
  }

  function renderActivity(events) {
    if (!events.length) {
      $activity.replaceChildren(
        el("p", { className: "org-text-sm org-text-muted", text: "אין פעילות אחרונה." })
      );
      return;
    }
    var list = el("ul", { className: "org-feed" });
    events.forEach(function (ev) {
      var dot = el("span", { className: dotClassFor(ev.action) });
      var who = (ev.actor_email || "—").split("@")[0];
      var main = el("div", { className: "org-feed__main" });
      main.appendChild(document.createTextNode(who + " · "));
      main.appendChild(el("strong", { text: ev.action_label || ev.action || "—" }));
      var body = el("div", { className: "org-feed__body" }, [main]);
      var when = el("span", { className: "org-feed__when", text: formatTimestamp(ev.created_at) });
      list.appendChild(el("li", { className: "org-feed__row" }, [dot, body, when]));
    });
    $activity.replaceChildren(list);
  }

  async function loadActivity() {
    try {
      var data = await api("GET", "/org/api/dashboard/recent-activity?limit=8");
      renderActivity(data.events || []);
    } catch (e) {
      $activity.replaceChildren(
        el("p", { className: "org-text-sm org-text-muted", text: e.message })
      );
    }
  }

  // ---- Today's practices ----
  function formatTimeOnly(iso) {
    if (!iso) return "—";
    var d = new Date(iso);
    if (isNaN(d.getTime())) return "—";
    return d.toLocaleTimeString("he-IL", { hour: "2-digit", minute: "2-digit" });
  }

  function renderPractice(sessions) {
    if (!$practice) return;
    if (!sessions.length) {
      $practice.replaceChildren(
        el("p", { className: "org-text-sm org-text-muted", text: "אין אימונים מתוכננים להיום." })
      );
      return;
    }
    var list = el("ul", { className: "org-feed" });
    sessions.slice(0, 6).forEach(function (s) {
      var team = el("div", { text: s.team_name || "—", attrs: { style: "font-weight: 600;" } });
      var metaText = [s.title, s.location].filter(Boolean).join(" · ");
      var body = el("div", { className: "org-feed__body" }, [team]);
      if (metaText) {
        body.appendChild(el("div", { className: "org-feed__sub", text: metaText }));
      }
      var pill = el("span", { className: "org-time-pill", text: formatTimeOnly(s.scheduled_at) });
      list.appendChild(el("li", { className: "org-feed__row" }, [body, pill]));
    });
    $practice.replaceChildren(list);
  }

  async function loadPractice() {
    if (!$practice) return;
    try {
      var data = await api("GET", "/org/api/practice/today");
      renderPractice(data.sessions || []);
    } catch (e) {
      $practice.replaceChildren(
        el("p", { className: "org-text-sm org-text-muted", text: e.message })
      );
    }
  }

  // ---- Charts: bars + donut ----
  function renderBars(spec) {
    if (!spec || !spec.rows || !spec.rows.length) {
      $barsPanel.hidden = true;
      return;
    }
    $barsTitle.textContent = spec.label || "התפלגות";
    var max = spec.rows.reduce(function (m, r) { return Math.max(m, r.value || 0); }, 0) || 1;
    var frag = document.createDocumentFragment();
    spec.rows.forEach(function (r) {
      var name = el("div", { className: "org-hbar__name", text: r.name || "—" });
      var pct = Math.max(2, Math.round((r.value || 0) * 100 / max));
      var fill = el("div", {
        className: "org-hbar__fill",
        attrs: { style: "width:" + pct + "%;" },
      });
      var track = el("div", { className: "org-hbar__track" }, [fill]);
      var val = el("div", { className: "org-hbar__value", text: String(r.value || 0) });
      frag.appendChild(el("div", { className: "org-hbar" }, [name, track, val]));
    });
    $barsBody.replaceChildren(frag);
    $barsPanel.hidden = false;
  }

  function renderDonut(spec) {
    if (!spec || !spec.rows || spec.rows.length < 2) {
      $donutPanel.hidden = true;
      return;
    }
    $donutTitle.textContent = spec.label || "חלוקה";

    // SVG donut — circumference = 100 (using r=15.915 trick). Each segment
    // is a circle with dasharray controlling its arc + dashoffset rotating
    // into place. All draw-on-load via stroke-dasharray keyframe.
    var ns = "http://www.w3.org/2000/svg";
    var svg = svgEl("svg", { viewBox: "0 0 42 42", width: "130", height: "130" });
    var offset = 25;   // start segment 1 at top
    spec.rows.forEach(function (r, i) {
      var pct = Math.max(0, Math.min(100, r.percent || 0));
      var seg = svgEl("circle", {
        cx: "21", cy: "21", r: "15.915",
        fill: "none",
        stroke: DONUT_COLORS[i % DONUT_COLORS.length],
        "stroke-width": "6",
        "stroke-dasharray": pct + " " + (100 - pct),
        "stroke-dashoffset": String(offset),
        transform: "rotate(-90 21 21)",
      });
      offset -= pct;
      svg.appendChild(seg);
    });
    var donut = el("div", { className: "org-donut" });
    donut.appendChild(svg);
    var center = el("div", { className: "org-donut__center" });
    var centerInner = el("div");
    centerInner.appendChild(el("div", { className: "org-donut__big", text: String(spec.total || "") }));
    centerInner.appendChild(el("div", { className: "org-donut__small", text: spec.total_label || "" }));
    center.appendChild(centerInner);
    donut.appendChild(center);

    var legend = el("div", { className: "org-donut-legend" });
    spec.rows.forEach(function (r, i) {
      var sw = el("span", {
        className: "org-donut-legend__swatch",
        attrs: { style: "background:" + DONUT_COLORS[i % DONUT_COLORS.length] + ";" },
      });
      var nameNode = el("span", { className: "org-donut-legend__name", text: r.name || "—" });
      var valNode = el("span", { className: "org-donut-legend__value", text: (r.percent || 0) + "%" });
      legend.appendChild(el("div", { className: "org-donut-legend__row" }, [sw, nameNode, valNode]));
    });

    var wrap = el("div", { className: "org-donut-wrap" }, [donut, legend]);
    $donutBody.replaceChildren(wrap);
    $donutPanel.hidden = false;
  }

  async function loadCharts() {
    try {
      var data = await api("GET", "/org/api/dashboard/breakdown");
      renderBars(data.bars);
      renderDonut(data.donut);
    } catch (_e) {
      // Charts are decorative — silently hide on any error rather than
      // showing a red banner the user can't act on.
      if ($barsPanel) $barsPanel.hidden = true;
      if ($donutPanel) $donutPanel.hidden = true;
    }
  }

  // ---- Mini calendar ----
  function monthBounds(year, month) {
    var first = new Date(year, month, 1);
    var firstDow = first.getDay();
    var gridStart = new Date(year, month, 1 - firstDow);
    var gridEnd = new Date(gridStart);
    gridEnd.setDate(gridStart.getDate() + 42);
    return { gridStart: gridStart, gridEnd: gridEnd };
  }

  function bucketByDay(sessions) {
    var map = {};
    (sessions || []).forEach(function (s) {
      if (!s.scheduled_at) return;
      var d = new Date(s.scheduled_at);
      if (isNaN(d.getTime())) return;
      var key = ymd(d);
      (map[key] || (map[key] = [])).push(s);
    });
    return map;
  }

  async function loadMiniCal() {
    if (!$miniCal) return;
    var now = new Date();
    var year = now.getFullYear(), month = now.getMonth();
    if ($miniCalTitle) $miniCalTitle.textContent = MONTH_HE[month] + " " + year;
    try {
      var b = monthBounds(year, month);
      var data = await api("GET", "/org/api/practice/range?start=" + ymd(b.gridStart) + "&end=" + ymd(b.gridEnd));
      var byDay = bucketByDay(data.sessions);
      renderMiniCal(year, month, b, byDay, ymd(now));
    } catch (_e) {
      $miniCal.replaceChildren(
        el("p", { className: "org-text-sm org-text-muted", text: "לוח שנה לא זמין כרגע." })
      );
    }
  }

  function renderMiniCal(year, month, bounds, byDay, todayKey) {
    var DOW = ["א", "ב", "ג", "ד", "ה", "ו", "ש"];
    var grid = el("div", { className: "org-mini-cal" });
    DOW.forEach(function (d) {
      grid.appendChild(el("div", { className: "org-mini-cal__dow", text: d }));
    });
    var cur = new Date(bounds.gridStart);
    for (var i = 0; i < 42; i++) {
      var key = ymd(cur);
      var isOther = cur.getMonth() !== month;
      var classes = "org-mini-cal__cell";
      if (isOther) classes += " is-other";
      if (key === todayKey) classes += " is-today";
      var cell = el("div", { className: classes });
      cell.appendChild(document.createTextNode(String(cur.getDate())));
      var events = byDay[key] || [];
      if (events.length) {
        var dots = el("div", { className: "org-mini-cal__dots" });
        // Up to 3 dots — alternate blue/cyan based on event kind so the
        // calendar visually summarizes the day's mix.
        var n = Math.min(events.length, 3);
        for (var j = 0; j < n; j++) {
          var alt = (events[j].kind === "game" || events[j].kind === "other");
          dots.appendChild(el("span", { className: "org-mini-cal__dot" + (alt ? " org-mini-cal__dot--cyan" : "") }));
        }
        cell.appendChild(dots);
      }
      grid.appendChild(cell);
      cur.setDate(cur.getDate() + 1);
    }
    $miniCal.replaceChildren(grid);
  }

  // ---- Boot ----
  async function boot() {
    renderSubtitle(null);
    if ($actions && canWrite) $actions.hidden = false;
    // Activity feed was removed from the dashboard in Phase 12 refresh —
    // loadActivity() is kept callable for any future surface that re-enables it.
    var jobs = [loadTiles(), loadPractice(), loadCharts(), loadMiniCal()];
    if ($activity) jobs.push(loadActivity());
    await Promise.all(jobs);
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", boot);
  } else {
    boot();
  }
})();
