/* /org/calendar — month grid calendar for practice sessions.
 *
 * Hydrates from GET /org/api/practice/range. Writes via POST/PATCH/DELETE
 * on /org/api/practice. Coach role is read-only (server enforces too).
 *
 * Security: every label lands via textContent / setAttribute.
 */
(function () {
  "use strict";

  var ctx = window.ORG_CAL || { role: "viewer" };
  var WRITE_ROLES = ["org_admin", "program_manager", "region_manager"];
  var canWrite = WRITE_ROLES.indexOf(ctx.role) >= 0;

  var DOW_LABELS = ["א'", "ב'", "ג'", "ד'", "ה'", "ו'", "ש'"];   // א = Sunday in IL
  var MONTH_HE = [
    "ינואר","פברואר","מרץ","אפריל","מאי","יוני",
    "יולי","אוגוסט","ספטמבר","אוקטובר","נובמבר","דצמבר",
  ];
  var KIND_LABELS = {
    practice: "אימון",
    game:     "משחק",
    meeting:  "פגישה",
    other:    "אחר",
  };

  // 12-color palette for the per-role chip coloring. Background + text
  // pairs are deliberately picked for readable contrast in light mode.
  var COLOR_PALETTE = [
    { bg: "#dbeafe", fg: "#1e3a8a" },   // blue
    { bg: "#dcfce7", fg: "#14532d" },   // green
    { bg: "#fef3c7", fg: "#78350f" },   // amber
    { bg: "#fce7f3", fg: "#831843" },   // pink
    { bg: "#ede9fe", fg: "#4c1d95" },   // violet
    { bg: "#cffafe", fg: "#155e75" },   // cyan
    { bg: "#fee2e2", fg: "#7f1d1d" },   // red
    { bg: "#e0e7ff", fg: "#312e81" },   // indigo
    { bg: "#fef9c3", fg: "#713f12" },   // yellow
    { bg: "#d1fae5", fg: "#064e3b" },   // emerald
    { bg: "#ffedd5", fg: "#7c2d12" },   // orange
    { bg: "#e2e8f0", fg: "#1e293b" },   // slate (fallback for "no key")
  ];

  function colorForKey(key) {
    if (key == null || key === "") return COLOR_PALETTE[COLOR_PALETTE.length - 1];
    // Deterministic hash so the same id always maps to the same color.
    var s = String(key);
    var h = 0;
    for (var i = 0; i < s.length; i++) {
      h = ((h << 5) - h) + s.charCodeAt(i);
      h |= 0;
    }
    var idx = Math.abs(h) % (COLOR_PALETTE.length - 1);
    return COLOR_PALETTE[idx];
  }

  // The "color key" depends on the viewer's role so the calendar surfaces
  // the most useful slicing automatically. Coaches get a single color
  // (their world is already one team).
  function colorKeyForEvent(s) {
    if (ctx.role === "org_admin") return s.program_id;
    if (ctx.role === "program_manager") return s.region_id;
    if (ctx.role === "region_manager") return s.team_id;
    return null;
  }

  var $grid = document.getElementById("org-cal-grid");
  var $title = document.querySelector("[data-cal-title]");
  var $actionsHost = document.querySelector("[data-actions-host]");
  var $legend = document.querySelector("[data-cal-legend]");
  var $legendLabel = document.querySelector("[data-legend-label]");
  var $dayDetail = document.getElementById("org-day-detail");
  var $dayDetailTitle = $dayDetail && $dayDetail.querySelector("[data-day-detail-title]");
  var $dayDetailBody = $dayDetail && $dayDetail.querySelector("[data-day-detail-body]");

  // Cap chips per day in the grid — anything past this collapses into a
  // "ועוד N" affordance that opens the day-detail panel below.
  var MAX_CHIPS_PER_DAY = 3;
  var $eventModal = document.getElementById("event-modal");
  var $eventForm = document.getElementById("event-form");
  var $eventError = $eventModal.querySelector("[data-error]");
  var $programSel = document.getElementById("event-program");
  var $regionSel = document.getElementById("event-region");
  var $teamSel = document.getElementById("event-team");
  var $teamCountHint = $eventModal.querySelector("[data-team-count]");
  var $recurring = document.getElementById("event-recurring");
  var $recurrenceEnd = document.getElementById("event-recurrence-end");

  // Roster of programs / regions / teams for the cascade dropdowns.
  // Loaded once on boot; cascades narrow the visible options.
  var allPrograms = [];   // [{id, name}]
  var allRegions = [];    // [{id, name, program_id}]
  var allTeams = [];      // [{id, team_name, region_name, region_id, program_id}]

  var $viewModal = document.getElementById("event-view-modal");
  var $viewTitle = $viewModal.querySelector("[data-view-title]");
  var $viewTeam = $viewModal.querySelector("[data-view-team]");
  var $viewWhen = $viewModal.querySelector("[data-view-when]");
  var $viewDuration = $viewModal.querySelector("[data-view-duration]");
  var $viewLocation = $viewModal.querySelector("[data-view-location]");
  var $viewError = $viewModal.querySelector("[data-view-error]");
  var $viewActions = $viewModal.querySelector("[data-view-actions]");
  var $deleteSeriesBtn = $viewModal.querySelector('[data-action="delete-series"]');

  if (canWrite && $actionsHost) $actionsHost.hidden = false;
  if (canWrite && $viewActions) $viewActions.hidden = false;

  var state = {
    year: new Date().getFullYear(),
    month: new Date().getMonth(),   // 0..11
    sessions: [],                   // current month's loaded sessions
    teamsById: {},
    currentViewSession: null,
    expandedDay: null,              // YYYY-MM-DD of the day shown in the detail panel
  };

  function el(tag, opts, children) {
    var n = document.createElement(tag);
    if (opts) {
      if (opts.className) n.className = opts.className;
      if (opts.text != null) n.textContent = opts.text;
      if (opts.attrs) Object.keys(opts.attrs).forEach(function (k) {
        n.setAttribute(k, opts.attrs[k]);
      });
    }
    if (children) children.forEach(function (c) { n.appendChild(c); });
    return n;
  }
  function pad(n) { return n < 10 ? "0" + n : String(n); }
  function ymd(d) { return d.getFullYear() + "-" + pad(d.getMonth() + 1) + "-" + pad(d.getDate()); }

  function openModal(m) { m.classList.add("is-open"); m.setAttribute("aria-hidden", "false"); }
  function closeModal(m) { m.classList.remove("is-open"); m.setAttribute("aria-hidden", "true"); }
  function showError(el2, t) { el2.textContent = t; el2.classList.remove("org-hidden"); }
  function hideError(el2) { el2.textContent = ""; el2.classList.add("org-hidden"); }

  // --- Custom confirm modal — replaces window.confirm so every prompt
  // stays in the platform's design system. Returns Promise<bool>.
  var $confirmModal = document.getElementById("cal-confirm-modal");
  var $confirmTitle = $confirmModal && $confirmModal.querySelector("[data-confirm-title]");
  var $confirmMessage = $confirmModal && $confirmModal.querySelector("[data-confirm-message]");
  var $confirmOkBtn = $confirmModal && $confirmModal.querySelector("[data-confirm-ok-btn]");
  var _confirmResolver = null;

  function customConfirm(title, message, opts) {
    if (!$confirmModal) return Promise.resolve(window.confirm(message));  // fallback
    opts = opts || {};
    $confirmTitle.textContent = title || "אישור";
    $confirmMessage.textContent = message || "";
    $confirmOkBtn.textContent = opts.okText || "אישור";
    // Destructive variant — red OK button.
    $confirmOkBtn.className = "org-btn " + (opts.destructive ? "org-btn--danger" : "org-btn--primary");
    openModal($confirmModal);
    // Focus the OK button so Enter immediately confirms.
    setTimeout(function () { $confirmOkBtn.focus(); }, 50);
    return new Promise(function (resolve) {
      _confirmResolver = resolve;
    });
  }

  function resolveConfirm(answer) {
    if (_confirmResolver) {
      var r = _confirmResolver;
      _confirmResolver = null;
      r(answer);
    }
    closeModal($confirmModal);
  }

  async function api(method, url, body) {
    var init = {
      method: method,
      headers: { "Accept": "application/json", "X-Requested-With": "XMLHttpRequest" },
      credentials: "same-origin",
    };
    if (body !== undefined) {
      init.headers["Content-Type"] = "application/json";
      init.body = JSON.stringify(body);
    }
    var r = await fetch(url, init);
    if (r.status === 204) return null;
    var data = null;
    try { data = await r.json(); } catch (_e) {}
    if (!r.ok) {
      var msg = (data && (data.detail || data.message)) || ("שגיאה " + r.status);
      throw new Error(msg);
    }
    return data;
  }

  // --- Grid rendering ---
  function monthBounds(year, month) {
    // First day of the month + last day. We pad by ±6 so the grid shows
    // a full 6-week window (max needed for any month).
    var first = new Date(year, month, 1);
    var firstDow = first.getDay();                       // 0..6 (Sun..Sat)
    var gridStart = new Date(year, month, 1 - firstDow);
    var gridEnd = new Date(gridStart);
    gridEnd.setDate(gridStart.getDate() + 42);           // exclusive
    return { gridStart: gridStart, gridEnd: gridEnd };
  }

  function bucketSessionsByDay(sessions) {
    var map = {};
    sessions.forEach(function (s) {
      if (!s.scheduled_at) return;
      var d = new Date(s.scheduled_at);
      if (isNaN(d.getTime())) return;
      var key = ymd(d);
      (map[key] || (map[key] = [])).push(s);
    });
    return map;
  }

  // Collapse a day's events into groups. Two keying paths:
  //   1. Primary — explicit series_id (set by the backend for any bulk
  //      create after Phase 12.1). All rows with the same series_id +
  //      scheduled_at collapse to one chip.
  //   2. Fallback — legacy rows from before series_id existed. We group
  //      by (kind + scheduled_at + title) as a heuristic. 108 untagged
  //      "U12 גילה" rows at 18:00 still collapse to one chip.
  // Singletons that ended up in a "group" bucket get downgraded to "solo"
  // so the chip stays informative ("18:00 · team_name" instead of "(1)").
  function groupDayEvents(dayEvents) {
    var groups = [];
    var byKey = {};
    dayEvents.forEach(function (s) {
      var tms = new Date(s.scheduled_at).getTime();
      var key = null;
      if (s.series_id) {
        key = "S:" + s.series_id + "@" + tms;
      } else if (s.title) {
        // Legacy bulk → group by exact title + kind + time. Safe because
        // a single coach is unlikely to manually create dozens of rows
        // with the exact same title at the exact same minute.
        key = "T:" + (s.kind || "") + "@" + tms + "/" + s.title;
      }
      if (!key) {
        groups.push({
          kind: "solo",
          first: s,
          time: new Date(s.scheduled_at),
          sessions: [s],
        });
        return;
      }
      var g = byKey[key];
      if (!g) {
        g = {
          kind: "group",
          first: s,
          series_id: s.series_id || null,
          scope_label: s.scope_label || null,
          scope_count: s.scope_count || null,
          time: new Date(s.scheduled_at),
          sessions: [],
        };
        byKey[key] = g;
        groups.push(g);
      }
      g.sessions.push(s);
    });
    // Downgrade singletons to solo so they don't render as "(1)".
    groups.forEach(function (g) {
      if (g.kind === "group" && g.sessions.length === 1) {
        g.kind = "solo";
      }
    });
    groups.sort(function (a, b) { return a.time - b.time; });
    return groups;
  }

  function renderLegend(sessions) {
    if (!$legend) return;
    var role = ctx.role;
    var byKey = {};
    var label = "";
    if (role === "org_admin") {
      label = "צבע לפי תוכנית:";
      sessions.forEach(function (s) {
        var k = s.program_id;
        if (k != null && !byKey[k]) {
          byKey[k] = { key: k, name: programNameById(s.program_id) || "ללא תוכנית" };
        }
      });
    } else if (role === "program_manager") {
      label = "צבע לפי מחוז:";
      sessions.forEach(function (s) {
        var k = s.region_id;
        if (k != null && !byKey[k]) {
          byKey[k] = { key: k, name: regionNameById(s.region_id) || "ללא מחוז" };
        }
      });
    } else if (role === "region_manager") {
      label = "צבע לפי קבוצה:";
      sessions.forEach(function (s) {
        var k = s.team_id;
        if (k != null && !byKey[k]) {
          byKey[k] = { key: k, name: s.team_name || "—" };
        }
      });
    } else {
      $legend.hidden = true;
      return;
    }

    var entries = Object.keys(byKey).map(function (k) { return byKey[k]; });
    if (!entries.length) {
      $legend.hidden = true;
      return;
    }
    entries.sort(function (a, b) { return (a.name || "").localeCompare(b.name || ""); });

    // Rebuild — keep the label node, append fresh chips.
    while ($legend.children.length > 1) $legend.removeChild($legend.lastChild);
    if ($legendLabel) $legendLabel.textContent = label;
    entries.forEach(function (e) {
      var c = colorForKey(e.key);
      $legend.appendChild(el("span", {
        text: e.name,
        attrs: {
          style: "padding:2px 10px; border-radius:999px; font-size:12px; font-weight:600; "
            + "background:" + c.bg + "; color:" + c.fg + ";",
        },
      }));
    });
    $legend.hidden = false;
  }

  function programNameById(id) {
    if (id == null) return null;
    for (var i = 0; i < allPrograms.length; i++) {
      if (allPrograms[i].id === id) return allPrograms[i].name;
    }
    return null;
  }

  function regionNameById(id) {
    if (id == null) return null;
    for (var i = 0; i < allRegions.length; i++) {
      if (allRegions[i].id === id) return allRegions[i].name;
    }
    return null;
  }

  function renderGrid() {
    $title.textContent = MONTH_HE[state.month] + " " + state.year;
    var bounds = monthBounds(state.year, state.month);
    var byDay = bucketSessionsByDay(state.sessions);
    var todayKey = ymd(new Date());

    $grid.replaceChildren();
    DOW_LABELS.forEach(function (lbl) {
      $grid.appendChild(el("div", { className: "org-cal-dow", text: lbl }));
    });

    var cur = new Date(bounds.gridStart);
    for (var i = 0; i < 42; i++) {
      var key = ymd(cur);
      var isOther = cur.getMonth() !== state.month;
      var isExpanded = state.expandedDay === key;
      var cell = el("div", {
        className: "org-cal-cell" + (isOther ? " is-other-month" : "")
                 + (key === todayKey ? " is-today" : "")
                 + (isExpanded ? " is-expanded" : ""),
        attrs: { "data-date": key },
      });
      cell.appendChild(el("div", { className: "day-num", text: String(cur.getDate()) }));

      var groups = groupDayEvents(byDay[key] || []);
      var visible = groups.slice(0, MAX_CHIPS_PER_DAY);
      var overflow = groups.length - visible.length;

      visible.forEach(function (g) {
        var hh = pad(g.time.getHours()) + ":" + pad(g.time.getMinutes());
        var kindLabel = KIND_LABELS[g.first.kind] || KIND_LABELS.practice;
        var title = g.first.title;
        var label;
        var tooltip;
        if (g.kind === "group") {
          // Grouped chip — keep it short. Prefer the user-typed title, fall
          // back to "{kind} {scope}" when the inviter didn't bother to name
          // the event. The team count goes in the tooltip + day-detail panel.
          var headline = title || (kindLabel + " · " + (g.scope_label || "אירוע"));
          label = hh + " · " + headline;
          tooltip = (title ? title + " · " : "")
                  + (g.scope_label ? "אירוע ל" + g.scope_label + " · " : "")
                  + g.sessions.length + " קבוצות";
        } else {
          // Solo chip — title (if any) wins over kindLabel; team name still
          // shows so the coach knows whose event it is.
          var soloHead = title || kindLabel;
          label = hh + " · " + soloHead + " · " + (g.first.team_name || "—");
          tooltip = title || kindLabel;
          if (g.first.location) tooltip += " · " + g.first.location;
        }
        // Color uses the first session of the group — all rows in a series
        // share the same scope so the color is deterministic per group.
        var color = colorForKey(colorKeyForEvent(g.first));
        cell.appendChild(el("div", {
          className: "org-cal-event",
          text: label,
          attrs: {
            "data-event-id": String(g.first.id),
            "data-series-id": g.series_id || "",
            "data-event-date": key,
            title: tooltip,
            style: "background:" + color.bg + "; color:" + color.fg + ";",
          },
        }));
      });
      if (overflow > 0) {
        cell.appendChild(el("button", {
          className: "org-cal-more",
          text: "+ " + overflow + " נוספים",
          attrs: {
            type: "button",
            "data-day-expand": key,
          },
        }));
      }
      $grid.appendChild(cell);
      cur.setDate(cur.getDate() + 1);
    }
  }

  function renderDayDetail() {
    if (!$dayDetail) return;
    if (!state.expandedDay) {
      $dayDetail.hidden = true;
      return;
    }
    var byDay = bucketSessionsByDay(state.sessions);
    var dayEvents = byDay[state.expandedDay] || [];
    if (!dayEvents.length) {
      $dayDetail.hidden = true;
      return;
    }
    var groups = groupDayEvents(dayEvents);

    // Pretty header — "13.5.2026 (יום ה')"
    var d = new Date(state.expandedDay + "T00:00:00");
    $dayDetailTitle.textContent = "אירועי " + d.toLocaleDateString("he-IL", {
      weekday: "long", day: "numeric", month: "long", year: "numeric",
    });

    $dayDetailBody.replaceChildren();
    groups.forEach(function (g) {
      var hh = pad(g.time.getHours()) + ":" + pad(g.time.getMinutes());
      var kindLabel = KIND_LABELS[g.first.kind] || KIND_LABELS.practice;
      var title = g.first.title;

      var head = el("div", { className: "group-head" });
      head.appendChild(el("span", { className: "group-time", text: hh }));

      // Heading style — user-typed title gets top billing; the scope+count
      // description sits underneath as the meta line so a day-detail row
      // reads exactly like the user briefed:
      //   "טורניר אביב"                       ← title (or kindLabel fallback)
      //   "מחוז מרכז עם 38 קבוצות משתתפות"   ← scope_label + N (for groups)
      var headline = title || kindLabel;
      if (g.kind === "solo") {
        headline += " · " + (g.first.team_name || "—");
      }
      head.appendChild(el("span", { className: "group-title", text: headline }));

      var metaBits = [];
      if (g.kind === "group") {
        var n = g.sessions.length;
        var scope = g.scope_label || "מספר קבוצות";
        metaBits.push(scope + " עם " + n + " קבוצות משתתפות");
      }
      if (g.first.location) metaBits.push(g.first.location);
      if (metaBits.length) {
        head.appendChild(el("span", { className: "group-meta", text: metaBits.join(" · ") }));
      }

      var block = el("div", { className: "group" });
      block.appendChild(head);

      // For groups: list teams ONLY when small (<=8). Larger events show
      // just the count — per the user's brief, no need to flood the panel
      // with names when 100 teams are invited.
      if (g.kind === "group" && g.sessions.length <= 8) {
        var ul = el("ul", { className: "group-teams" });
        g.sessions.forEach(function (s) {
          var li = el("li", { text: s.team_name || "—" });
          li.style.cursor = "pointer";
          li.dataset.eventId = String(s.id);
          ul.appendChild(li);
        });
        block.appendChild(ul);
      }

      $dayDetailBody.appendChild(block);
    });
    $dayDetail.hidden = false;
  }

  async function loadMonth() {
    try {
      var bounds = monthBounds(state.year, state.month);
      var url = "/org/api/practice/range?start=" + ymd(bounds.gridStart)
              + "&end=" + ymd(bounds.gridEnd);
      var data = await api("GET", url);
      state.sessions = data.sessions || [];
      renderLegend(state.sessions);
      renderGrid();
      renderDayDetail();
    } catch (e) {
      $grid.replaceChildren(el("div", { className: "org-empty", text: e.message }));
    }
  }

  function fillSelectOptions(sel, items, blankLabel, valueKey, labelFn) {
    while (sel.options.length > 0) sel.remove(0);
    var blank = document.createElement("option");
    blank.value = ""; blank.textContent = blankLabel;
    sel.appendChild(blank);
    items.forEach(function (it) {
      var o = document.createElement("option");
      o.value = String(it[valueKey]);
      o.textContent = labelFn(it);
      sel.appendChild(o);
    });
  }

  function applyCascade() {
    // Phase 15.9 — programs and regions are INDEPENDENT axes (Phase 12
    // removed `region.program_id`; regions can host teams from any
    // program). So picking a program does NOT narrow the region list;
    // it only filters teams. Region narrows teams. Both together narrow
    // teams further.
    var pid = $programSel.value;
    var rid = $regionSel.value;

    // Regions list: filter ONLY if the region itself has program_id set
    // (legacy program-pinned regions); otherwise show all regions in the
    // org. This preserves behavior for orgs that still use pinned regions
    // while fixing Sha'ar Shivyon (program_id=NULL everywhere).
    var regions = pid
      ? allRegions.filter(function (r) {
          return r.program_id == null || String(r.program_id) === pid;
        })
      : allRegions.slice();
    regions.sort(function (a, b) { return (a.name || "").localeCompare(b.name || ""); });

    var prevRegion = $regionSel.value;
    fillSelectOptions($regionSel, regions, "— כל המחוזות —", "id", function (r) { return r.name; });
    var regionStillValid = regions.some(function (r) { return String(r.id) === prevRegion; });
    $regionSel.value = regionStillValid ? prevRegion : "";
    rid = $regionSel.value;

    var teams = allTeams.filter(function (t) {
      if (pid && String(t.program_id) !== pid) return false;
      if (rid && String(t.region_id) !== rid) return false;
      return true;
    });
    teams.sort(function (a, b) { return (a.team_name || "").localeCompare(b.team_name || ""); });

    var prevTeam = $teamSel.value;
    // Cache the matching ids on the dropdown so the submit handler can
    // resolve "all teams" to a concrete list without re-running the cascade.
    $teamSel.dataset.allIds = teams.map(function (t) { return t.id; }).join(",");

    fillSelectOptions($teamSel, teams, "— בחר קבוצה —", "id", function (t) {
      var bits = [t.team_name];
      if (!rid && t.region_name) bits.push(t.region_name);   // surface scope when filter is open
      return bits.join(" · ");
    });
    // Add a "select all matching" option at the top of the list when
    // the cascade narrows to 2+ teams. The value "__ALL__" is intercepted
    // by the submit handler and expanded to team_ids before POST.
    if (teams.length > 1) {
      var allOpt = document.createElement("option");
      allOpt.value = "__ALL__";
      allOpt.textContent = "— כל הקבוצות בסינון (" + teams.length + ") —";
      // Insert just after the blank.
      $teamSel.insertBefore(allOpt, $teamSel.options[1] || null);
    }
    var teamStillValid = (prevTeam === "__ALL__" && teams.length > 1)
      || teams.some(function (t) { return String(t.id) === prevTeam; });
    $teamSel.value = teamStillValid ? prevTeam : "";
    if ($teamCountHint) {
      $teamCountHint.textContent = teams.length
        ? teams.length + " קבוצות תואמות"
        : "אין קבוצות תואמות לסינון";
    }
  }

  async function loadTeamsForModal() {
    try {
      // Cascade order: programs, regions, teams. Each fetch is filtered
      // server-side by the active membership scope, so a PM only ever
      // gets back their own program / its regions / its teams.
      var [progResp, regResp, teamResp] = await Promise.all([
        api("GET", "/org/api/programs"),
        api("GET", "/org/api/regions"),
        api("GET", "/org/api/teams"),
      ]);
      allPrograms = (progResp.programs || []).slice();
      allRegions = (regResp.regions || []).slice();
      allTeams = (teamResp.teams || []).slice();

      // Sort programs by name.
      allPrograms.sort(function (a, b) { return (a.name || "").localeCompare(b.name || ""); });

      // Populate program dropdown.
      fillSelectOptions(
        $programSel, allPrograms,
        "— כל התוכניות —", "id", function (p) { return p.name; }
      );

      applyCascade();

      // Wire cascade changes once. (Multiple boots shouldn't double-bind
      // since loadTeamsForModal runs at most once.)
      $programSel.addEventListener("change", applyCascade);
      $regionSel.addEventListener("change", applyCascade);
    } catch (_e) { /* leave dropdowns empty on failure */ }
  }

  // --- Open create modal ---
  function openCreate(prefillDate) {
    if (!canWrite) return;
    $eventForm.reset();
    document.getElementById("event-kind").value = "practice";
    syncTitleRequirement();
    $programSel.value = "";
    $regionSel.value = "";
    applyCascade();   // resets region/team dropdowns to full list
    $recurring.checked = false;
    $recurrenceEnd.style.display = "none";
    if (prefillDate) {
      document.getElementById("event-date").value = prefillDate;
    } else {
      document.getElementById("event-date").value = ymd(new Date());
    }
    document.getElementById("event-time").value = "18:00";
    document.getElementById("event-duration").value = "90";
    hideError($eventError);
    openModal($eventModal);
    setTimeout(function () { $programSel.focus(); }, 50);
  }

  $recurring.addEventListener("change", function () {
    $recurrenceEnd.style.display = $recurring.checked ? "" : "none";
  });

  // When the user picks "אחר", surface the title field as required + relabel.
  var $kindSel = document.getElementById("event-kind");
  var $titleInput = document.getElementById("event-title");
  var $titleLabel = $eventModal.querySelector("[data-title-label]");
  var $titleHelp = $eventModal.querySelector("[data-title-help]");

  function syncTitleRequirement() {
    var isOther = $kindSel && $kindSel.value === "other";
    if (isOther) {
      $titleLabel.textContent = "שם האירוע *";
      $titleInput.required = true;
      $titleInput.placeholder = "לדוגמה: הצגת הורים, יום גיבוש, סיור";
      if ($titleHelp) $titleHelp.hidden = false;
    } else {
      $titleLabel.textContent = "כותרת";
      $titleInput.required = false;
      $titleInput.placeholder = "";
      if ($titleHelp) $titleHelp.hidden = true;
    }
  }
  if ($kindSel) $kindSel.addEventListener("change", syncTitleRequirement);

  $eventForm.addEventListener("submit", async function (ev) {
    ev.preventDefault();
    hideError($eventError);
    var fd = new FormData($eventForm);
    var teamId = fd.get("team_id");
    var date = fd.get("date");
    var time = fd.get("time");
    if (!teamId || !date || !time) {
      showError($eventError, "חובה למלא קבוצה, תאריך ושעה");
      return;
    }
    var rawTitle = (fd.get("title") || "").trim();
    if ((fd.get("kind") || "") === "other" && !rawTitle) {
      showError($eventError, "כאשר הסוג 'אחר' — חובה למלא שם אירוע");
      return;
    }
    var scheduledAt = date + "T" + time + ":00";
    var payload = {
      title: (fd.get("title") || "").trim() || null,
      scheduled_at: scheduledAt,
      duration_minutes: fd.get("duration_minutes")
        ? parseInt(fd.get("duration_minutes"), 10) : null,
      location: (fd.get("location") || "").trim() || null,
      kind: fd.get("kind") || "practice",
    };

    if (teamId === "__ALL__") {
      var raw = $teamSel.dataset.allIds || "";
      var idsArr = raw.split(",")
        .filter(function (x) { return x !== ""; })
        .map(function (x) { return parseInt(x, 10); });
      if (!idsArr.length) {
        showError($eventError, "אין קבוצות לבחירה. נקה את הסינון או בחר קבוצה אחת.");
        return;
      }
      // Derive a human-readable scope label so 100 rows collapse into a
      // single chip on the calendar ("אירוע מחוז צפון" / "אירוע תוכנית סל-טק").
      // Pick the most specific scope the user actually narrowed to.
      var scopeLabel;
      var pidStr = $programSel.value;
      var ridStr = $regionSel.value;
      if (ridStr) {
        scopeLabel = "מחוז " + (regionNameById(parseInt(ridStr, 10)) || "—");
      } else if (pidStr) {
        scopeLabel = "תוכנית " + (programNameById(parseInt(pidStr, 10)) || "—");
      } else {
        scopeLabel = "כל הקבוצות";
      }

      var title = "יצירת אירוע למספר קבוצות";
      var msg = "האירוע ייווצר עבור " + idsArr.length + " קבוצות (" + scopeLabel + "). להמשיך?";
      if (fd.get("recurring") === "on") {
        msg = "האירוע יוכפל לפי כמות הקבוצות (" + idsArr.length + ") "
            + "וכמות השבועות. ייתכן ועשרות אירועים ייווצרו. להמשיך?";
      }
      var ok = await customConfirm(title, msg, { okText: "צור אירועים" });
      if (!ok) return;
      payload.team_ids = idsArr;
      payload.scope_label = scopeLabel;
    } else {
      payload.team_id = parseInt(teamId, 10);
    }
    if (fd.get("recurring") === "on") {
      payload.recurrence = "weekly";
      var until = fd.get("recurrence_until");
      if (!until) {
        showError($eventError, "בחר תאריך סיום לחזרה");
        return;
      }
      payload.recurrence_until = until;
    }
    try {
      var data = await api("POST", "/org/api/practice", payload);
      var msg;
      if (data.team_count > 1 && data.occurrence_count > 1) {
        msg = "נוצרו " + data.count + " אירועים ("
          + data.team_count + " קבוצות × " + data.occurrence_count + " מועדים)";
      } else if (data.count > 1) {
        msg = "נוצרו " + data.count + " אירועים";
      } else {
        msg = "האירוע נוצר";
      }
      window.OrgToast && window.OrgToast.show(msg, "success");
      closeModal($eventModal);
      loadMonth();
    } catch (e) {
      showError($eventError, e.message);
    }
  });

  // --- View / delete ---
  function findSession(id) {
    for (var i = 0; i < state.sessions.length; i++) {
      if (String(state.sessions[i].id) === String(id)) return state.sessions[i];
    }
    return null;
  }

  function openView(s) {
    state.currentViewSession = s;
    var kindLabel = KIND_LABELS[s.kind] || KIND_LABELS.practice;
    $viewTitle.textContent = (s.title || kindLabel) + " · " + kindLabel;
    $viewTeam.textContent = s.team_name || "—";
    var d = new Date(s.scheduled_at);
    $viewWhen.textContent = isNaN(d.getTime()) ? "—"
      : d.toLocaleString("he-IL", { dateStyle: "short", timeStyle: "short" });
    $viewDuration.textContent = s.duration_minutes != null
      ? s.duration_minutes + " דק׳" : "—";
    $viewLocation.textContent = s.location || "—";
    hideError($viewError);
    // "Delete series" appears only when this row is part of a series —
    // /range now surfaces `series_id` from attributes_json so the button
    // can decide visibility without a second probe.
    $deleteSeriesBtn.hidden = !s.series_id;
    openModal($viewModal);
  }

  async function deleteCurrent(series) {
    if (!state.currentViewSession) return;
    var s = state.currentViewSession;
    // Build a clear confirm prompt — destructive variant uses the red OK.
    var title = series ? "מחיקת סדרת אירועים" : "מחיקת אירוע";
    var msg = series
      ? "פעולה זו תמחק את האירוע הנוכחי וכל המופעים העתידיים שלו בסדרה. "
        + "אירועים עבר יישמרו. להמשיך?"
      : "האם למחוק את האירוע \"" + (s.title || KIND_LABELS[s.kind] || "אירוע") + "\"? "
        + "ניתן לבטל רק על-ידי יצירה מחדש.";
    // Hide the view modal momentarily so the confirm sits cleanly on top.
    closeModal($viewModal);
    var ok = await customConfirm(title, msg, {
      okText: series ? "מחק סדרה" : "מחק אירוע",
      destructive: true,
    });
    if (!ok) {
      // Re-open the view so the user can take another action.
      openModal($viewModal);
      return;
    }
    try {
      var url = "/org/api/practice/" + s.id + (series ? "?series=true" : "");
      var data = await api("DELETE", url);
      var doneMsg = data.deleted > 1 ? ("נמחקו " + data.deleted + " אירועים") : "האירוע נמחק";
      window.OrgToast && window.OrgToast.show(doneMsg, "success");
      state.currentViewSession = null;
      loadMonth();
    } catch (e) {
      showError($viewError, e.message);
      openModal($viewModal);   // restore view modal so error is visible
    }
  }

  function expandDay(dateKey) {
    state.expandedDay = dateKey;
    renderGrid();
    renderDayDetail();
    if ($dayDetail && !$dayDetail.hidden) {
      $dayDetail.scrollIntoView({ behavior: "smooth", block: "start" });
    }
  }

  function collapseDay() {
    state.expandedDay = null;
    renderGrid();
    if ($dayDetail) $dayDetail.hidden = true;
  }

  // --- Event delegation ---
  document.addEventListener("click", function (e) {
    var t = e.target.closest(
      "[data-action], [data-event-id], [data-day-expand], [data-date]"
    );
    if (!t) return;

    if (t.dataset.action === "prev-month") {
      state.month -= 1;
      if (state.month < 0) { state.month = 11; state.year -= 1; }
      return loadMonth();
    }
    if (t.dataset.action === "next-month") {
      state.month += 1;
      if (state.month > 11) { state.month = 0; state.year += 1; }
      return loadMonth();
    }
    if (t.dataset.action === "today") {
      var now = new Date();
      state.year = now.getFullYear();
      state.month = now.getMonth();
      return loadMonth();
    }
    if (t.dataset.action === "new-event") return openCreate(null);
    if (t.dataset.action === "close-event") return closeModal($eventModal);
    if (t.dataset.action === "close-view") return closeModal($viewModal);
    if (t.dataset.action === "delete-event") return deleteCurrent(false);
    if (t.dataset.action === "delete-series") return deleteCurrent(true);
    if (t.dataset.action === "confirm-ok") return resolveConfirm(true);
    if (t.dataset.action === "confirm-cancel") return resolveConfirm(false);
    if (t.dataset.action === "close-day-detail") return collapseDay();

    // "+ N נוספים" → expand the day-detail panel below.
    if (t.dataset.dayExpand) {
      e.stopPropagation();
      expandDay(t.dataset.dayExpand);
      return;
    }

    // Event chip → view modal (takes precedence over the parent cell click).
    if (t.dataset.eventId) {
      e.stopPropagation();
      var s = findSession(t.dataset.eventId);
      if (s) openView(s);
      return;
    }
    // Day cell click — branch behavior:
    //   - Days with ANY events: expand the day-detail panel below the grid.
    //   - Empty days:           open create modal (writers only).
    if (t.dataset.date) {
      var byDay = bucketSessionsByDay(state.sessions);
      var dayGroups = groupDayEvents(byDay[t.dataset.date] || []);
      if (dayGroups.length > 0) {
        expandDay(t.dataset.date);
        return;
      }
      if (canWrite) openCreate(t.dataset.date);
    }
  });

  document.addEventListener("keydown", function (e) {
    if (e.key === "Escape") {
      closeModal($eventModal);
      closeModal($viewModal);
      if ($confirmModal && $confirmModal.classList.contains("is-open")) {
        resolveConfirm(false);   // Esc → cancel
      }
    }
    // Enter while confirm is open + the OK button focused → confirm.
    if (e.key === "Enter" && $confirmModal
        && $confirmModal.classList.contains("is-open")
        && document.activeElement === $confirmOkBtn) {
      e.preventDefault();
      resolveConfirm(true);
    }
  });
  [$eventModal, $viewModal].forEach(function (m) {
    m.addEventListener("click", function (e) { if (e.target === m) closeModal(m); });
  });
  // Backdrop click on confirm modal → cancel.
  if ($confirmModal) {
    $confirmModal.addEventListener("click", function (e) {
      if (e.target === $confirmModal) resolveConfirm(false);
    });
  }

  // --- Boot ---
  (async function () {
    if (canWrite) await loadTeamsForModal();
    await loadMonth();
  })();
})();
