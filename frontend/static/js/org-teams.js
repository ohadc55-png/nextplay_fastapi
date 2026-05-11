/* /org/teams page — team CRUD + coach assignment via /org/api/teams/*.
 *
 * Security: every user-controlled value lands via Node.textContent or
 * setAttribute. Only SVG icon templates use innerHTML, and they are
 * constant strings parsed once via DOMParser, then cloned per use.
 */

(function () {
  "use strict";

  var ctx = window.ORG_CTX || { role: "viewer", region_id: null, branch_id: null, user_id: null };
  var canManage = ["org_admin", "region_manager", "branch_manager"].indexOf(ctx.role) >= 0;
  var canDelete = ctx.role === "org_admin";

  // --- DOM refs ---
  var $rows = document.querySelector("[data-teams-rows]");
  var $regionFilter = document.getElementById("team-region-filter");
  var $branchFilter = document.getElementById("team-branch-filter");
  var $teamModal = document.getElementById("team-modal");
  var $teamForm = document.getElementById("team-form");
  var $teamError = $teamModal.querySelector("[data-error]");
  var $branchSelect = document.getElementById("team-branch");
  var $regionSelect = document.getElementById("team-region");  // modal-level region filter
  var $coachModal = document.getElementById("coach-modal");
  var $coachForm = document.getElementById("coach-form");
  var $coachError = $coachModal.querySelector("[data-error]");
  var $coachUserSelect = document.getElementById("coach-user");
  var $coachTeamName = $coachModal.querySelector("[data-coach-team-name]");
  var $confirmModal = document.getElementById("confirm-modal");
  var $confirmTitle = $confirmModal.querySelector("[data-confirm-title]");
  var $confirmMsg = $confirmModal.querySelector("[data-confirm-message]");
  var $confirmError = $confirmModal.querySelector("[data-confirm-error]");

  // --- State ---
  var branchesById = {};   // all branches in org (id → branch)
  var regionsById = {};
  var membersById = {};
  var pendingConfirm = null;

  // --- Static SVG icon templates (constant) ---
  var SVG_NS = "http://www.w3.org/2000/svg";
  function parseSvg(s) { return new DOMParser().parseFromString(s, "image/svg+xml").documentElement; }
  var SVG_EDIT_TPL = parseSvg(
    '<svg xmlns="' + SVG_NS + '" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.75" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">' +
    '<path d="M16.862 4.487l1.687-1.688a1.875 1.875 0 112.652 2.652L10.582 16.07a4.5 4.5 0 01-1.897 1.13l-2.685.8.8-2.685a4.5 4.5 0 011.13-1.897L16.863 4.487zm0 0L19.5 7.125M18 14v4.75A2.25 2.25 0 0115.75 21H5.25A2.25 2.25 0 013 18.75V8.25A2.25 2.25 0 015.25 6H10"/>' +
    "</svg>"
  );
  var SVG_TRASH_TPL = parseSvg(
    '<svg xmlns="' + SVG_NS + '" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.75" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">' +
    '<path d="M14.74 9l-.346 9m-4.788 0L9.26 9m9.968-3.21c.342.052.682.107 1.022.166m-1.022-.165L18.16 19.673a2.25 2.25 0 01-2.244 2.077H8.084a2.25 2.25 0 01-2.244-2.077L4.772 5.79m14.456 0a48.108 48.108 0 00-3.478-.397m-12 .562c.34-.059.68-.114 1.022-.165m0 0a48.11 48.11 0 013.478-.397m7.5 0v-.916c0-1.18-.91-2.164-2.09-2.201a51.964 51.964 0 00-3.32 0c-1.18.037-2.09 1.022-2.09 2.201v.916m7.5 0a48.667 48.667 0 00-7.5 0"/>' +
    "</svg>"
  );
  var SVG_COACH_TPL = parseSvg(
    '<svg xmlns="' + SVG_NS + '" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.75" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">' +
    '<path d="M15 19.128a9.38 9.38 0 002.625.372 9.337 9.337 0 004.121-.952 4.125 4.125 0 00-7.533-2.493M15 19.128v-.003c0-1.113-.285-2.16-.786-3.07M15 19.128v.106A12.318 12.318 0 018.624 21c-2.331 0-4.512-.645-6.374-1.766l-.001-.109a6.375 6.375 0 0111.964-3.07M12 6.375a3.375 3.375 0 11-6.75 0 3.375 3.375 0 016.75 0zm8.25 2.25a2.625 2.625 0 11-5.25 0 2.625 2.625 0 015.25 0z"/>' +
    "</svg>"
  );

  // --- Helpers ---
  function openModal(m) { m.classList.add("is-open"); m.setAttribute("aria-hidden", "false"); }
  function closeModal(m) { m.classList.remove("is-open"); m.setAttribute("aria-hidden", "true"); }
  function showError(el, text) { el.textContent = text; el.classList.remove("org-hidden"); }
  function hideError(el) { el.textContent = ""; el.classList.add("org-hidden"); }

  async function api(method, url, body) {
    var init = { method: method, headers: { "Accept": "application/json" } };
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
      throw { status: r.status, code: (data && data.code) || null, message: msg };
    }
    return data;
  }

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

  function iconBtn(svgTpl, attrs, isDanger) {
    var b = document.createElement("button");
    b.className = "org-btn-icon" + (isDanger ? " is-danger" : "");
    Object.keys(attrs).forEach(function (k) { b.setAttribute(k, attrs[k]); });
    b.appendChild(svgTpl.cloneNode(true));
    return b;
  }

  function pill(text, kind) {
    return el("span", { className: "org-pill" + (kind ? " " + kind : ""), text: text });
  }

  function fillSelect(sel, items, valueKey, labelKey, includeBlank, blankLabel) {
    while (sel.options.length > 0) sel.remove(0);
    if (includeBlank) {
      var blank = document.createElement("option");
      blank.value = "";
      blank.textContent = blankLabel || "— ללא —";
      sel.appendChild(blank);
    }
    items.forEach(function (it) {
      var o = document.createElement("option");
      o.value = String(it[valueKey]);
      o.textContent = it[labelKey];
      sel.appendChild(o);
    });
  }

  // --- Boot: reference data ---
  async function loadReferenceData() {
    var [regions, branches, members] = await Promise.all([
      api("GET", "/org/api/regions").catch(function () { return { regions: [] }; }),
      api("GET", "/org/api/branches").catch(function () { return { branches: [] }; }),
      api("GET", "/org/api/users").catch(function () { return { members: [] }; }),
    ]);
    regionsById = {};
    (regions.regions || []).forEach(function (r) { regionsById[r.id] = r; });
    branchesById = {};
    (branches.branches || []).forEach(function (b) { branchesById[b.id] = b; });
    membersById = {};
    (members.members || []).forEach(function (m) { membersById[m.user_id] = m; });
    populateSelectors();
  }

  function refreshBranchFilter() {
    // Narrow the branch dropdown to those in the selected region.
    var rid = $regionFilter ? $regionFilter.value : "";
    var branchList = Object.values(branchesById)
      .filter(function (b) {
        return !rid || String(b.region_id) === String(rid);
      })
      .sort(function (a, b) { return a.name.localeCompare(b.name); });
    var prev = $branchFilter.value;
    fillSelect($branchFilter, branchList, "id", "name", true, "כל הסניפים");
    // Preserve selection if still valid; otherwise reset.
    var stillValid = branchList.some(function (b) { return String(b.id) === prev; });
    $branchFilter.value = stillValid ? prev : "";
  }

  function populateSelectors() {
    var regionList = Object.values(regionsById).sort(function (a, b) {
      return a.name.localeCompare(b.name);
    });
    if ($regionFilter) {
      fillSelect($regionFilter, regionList, "id", "name", true, "כל המחוזות");
    }
    // Modal region selector — same list (subject to role pinning below).
    fillSelect($regionSelect, regionList, "id", "name", true, "— ללא מחוז —");

    // Modal branch selector starts with the full list; the region-modal
    // change handler narrows it as the user picks.
    refreshModalBranchOptions("");
    // The page-level FILTER branch dropdown narrows per the page region filter.
    refreshBranchFilter();

    // Region manager: pin the modal region selector to their region.
    if (ctx.role === "region_manager" && ctx.region_id) {
      $regionSelect.value = String(ctx.region_id);
      $regionSelect.disabled = true;
      refreshModalBranchOptions(String(ctx.region_id));
    }

    // Branch manager: pin filter + modal selectors to their branch & region.
    if (ctx.role === "branch_manager" && ctx.branch_id) {
      $branchFilter.value = String(ctx.branch_id);
      $branchFilter.disabled = true;
      if (ctx.region_id) {
        $regionSelect.value = String(ctx.region_id);
        $regionSelect.disabled = true;
        refreshModalBranchOptions(String(ctx.region_id));
      }
      $branchSelect.value = String(ctx.branch_id);
      $branchSelect.disabled = true;
    }
  }

  // Rebuild the modal branch <select> filtered to the given region (or all
  // branches if regionId is empty). Preserves the current selection if it's
  // still valid.
  function refreshModalBranchOptions(regionId) {
    var prev = $branchSelect.value;
    var list = Object.values(branchesById)
      .filter(function (b) {
        return !regionId || String(b.region_id) === String(regionId);
      })
      .sort(function (a, b) { return a.name.localeCompare(b.name); });
    fillSelect($branchSelect, list, "id", "name", true, "— ללא סניף —");
    var stillValid = list.some(function (b) { return String(b.id) === prev; });
    $branchSelect.value = stillValid ? prev : "";
  }

  function populateCoachSelector(currentUserId) {
    // Coaches eligible to lead a team: anyone with org membership.
    var memberList = Object.values(membersById)
      .filter(function (m) { return m.status === "active"; })
      .sort(function (a, b) {
        return (a.display_name || a.email).localeCompare(b.display_name || b.email);
      });
    fillSelect($coachUserSelect, memberList.map(function (m) {
      return { value: String(m.user_id), label: (m.display_name || m.email) + " · " + m.role };
    }), "value", "label", true, "— בחר מאמן —");
    if (currentUserId != null) {
      $coachUserSelect.value = String(currentUserId);
    }
  }

  // --- Teams list ---
  async function loadTeams() {
    var url = "/org/api/teams";
    var params = [];
    var bid = $branchFilter.value;
    var rid = $regionFilter ? $regionFilter.value : "";
    // Prefer branch over region; the backend filters teams by branch_id
    // OR region_id (the team's branch's region).
    if (bid) {
      params.push("branch_id=" + encodeURIComponent(bid));
    } else if (rid) {
      params.push("region_id=" + encodeURIComponent(rid));
    }
    if (params.length) url += "?" + params.join("&");
    try {
      var data = await api("GET", url);
      renderRows(data.teams || []);
    } catch (e) {
      setEmpty(e.message);
    }
  }

  function setEmpty(msg) {
    var colspan = canManage ? 5 : 4;
    $rows.replaceChildren(
      el("tr", null, [el("td", { className: "org-table-empty", text: msg, attrs: { colspan: String(colspan) } })])
    );
  }

  function renderRows(teams) {
    if (!teams.length) {
      setEmpty(canManage ? 'אין קבוצות עדיין. לחץ "קבוצה חדשה" כדי להתחיל.' : "אין קבוצות.");
      return;
    }
    $rows.replaceChildren.apply(
      $rows,
      teams.map(function (t) {
        var nameCell = el("td", null, [
          el("strong", { text: t.team_name }),
        ]);
        var leagueText = [t.league, t.division].filter(Boolean).join(" · ") || "—";
        var leagueCell = el("td", null, [
          el("span", { className: leagueText === "—" ? "org-pill org-pill--muted" : "org-pill", text: leagueText }),
        ]);
        var branchName = t.branch_id != null && branchesById[t.branch_id]
          ? branchesById[t.branch_id].name : "—";
        var branchCell = el("td", null, [
          el("span", { className: branchName === "—" ? "org-pill org-pill--muted" : "org-pill", text: branchName }),
        ]);

        // Coach cell with avatar + name.
        var coachCell;
        if (t.user_id && membersById[t.user_id]) {
          var coach = membersById[t.user_id];
          var initial = (coach.display_name || coach.email || "?").slice(0, 1).toUpperCase();
          coachCell = el("td", null, [
            el("div", { className: "org-flex org-items-center org-gap-3" }, [
              el("span", { className: "org-avatar", text: initial }),
              el("span", { text: coach.display_name || coach.email }),
            ]),
          ]);
        } else {
          coachCell = el("td", null, [
            el("span", { className: "org-pill org-pill--muted", text: "ללא מאמן" }),
          ]);
        }

        var cells = [nameCell, leagueCell, branchCell, coachCell];
        if (canManage) {
          var actions = [
            iconBtn(SVG_COACH_TPL, {
              type: "button",
              "data-assign-coach": String(t.id),
              "data-team-name": t.team_name,
              "data-current-coach": t.user_id != null ? String(t.user_id) : "",
              title: "שיוך מאמן",
              "aria-label": "שיוך מאמן",
            }, false),
            iconBtn(SVG_EDIT_TPL, {
              type: "button",
              "data-edit-team": String(t.id),
              title: "עריכה",
              "aria-label": "עריכה",
            }, false),
          ];
          if (canDelete) {
            actions.push(iconBtn(SVG_TRASH_TPL, {
              type: "button",
              "data-delete-team": String(t.id),
              "data-team-name": t.team_name,
              title: "מחיקה",
              "aria-label": "מחיקה",
            }, true));
          }
          cells.push(el("td", { className: "org-table-actions" }, actions));
        }
        return el("tr", null, cells);
      })
    );
  }

  // --- Team create / edit ---
  function openNewTeam() {
    $teamForm.reset();
    $teamForm.elements.id.value = "";
    // form.reset() wiped the dropdowns — restore role-based pinning + rebuild
    // the branch options to match the chosen region.
    if (ctx.role === "region_manager" && ctx.region_id) {
      $regionSelect.value = String(ctx.region_id);
      refreshModalBranchOptions(String(ctx.region_id));
    } else if (ctx.role === "branch_manager" && ctx.branch_id) {
      if (ctx.region_id) {
        $regionSelect.value = String(ctx.region_id);
        refreshModalBranchOptions(String(ctx.region_id));
      }
      $branchSelect.value = String(ctx.branch_id);
    } else {
      $regionSelect.value = "";
      refreshModalBranchOptions("");
    }
    document.getElementById("team-modal-title").textContent = "קבוצה חדשה";
    hideError($teamError);
    openModal($teamModal);
    setTimeout(function () { $teamForm.elements.team_name.focus(); }, 50);
  }

  async function openEditTeam(teamId) {
    try {
      var t = await api("GET", "/org/api/teams/" + teamId);
      $teamForm.elements.id.value = String(t.id);
      $teamForm.elements.team_name.value = t.team_name;
      $teamForm.elements.league.value = t.league || "";
      $teamForm.elements.division.value = t.division || "";
      // Derive region from the team's branch (if any), narrow the branch
      // list, then set the branch.
      var derivedRegion = "";
      if (t.branch_id != null && branchesById[t.branch_id]) {
        derivedRegion = String(branchesById[t.branch_id].region_id || "");
      }
      $regionSelect.value = derivedRegion;
      refreshModalBranchOptions(derivedRegion);
      $branchSelect.value = t.branch_id != null ? String(t.branch_id) : "";
      document.getElementById("team-modal-title").textContent = "עריכת קבוצה";
      hideError($teamError);
      openModal($teamModal);
    } catch (e) {
      window.OrgToast && window.OrgToast.show(e.message, "danger");
    }
  }

  // Modal region change → narrow the branch list. Disabled state (for RM/BM
  // pinning) prevents the user from ever firing this anyway.
  $regionSelect.addEventListener("change", function () {
    refreshModalBranchOptions($regionSelect.value);
  });

  $teamForm.addEventListener("submit", async function (ev) {
    ev.preventDefault();
    hideError($teamError);
    var id = $teamForm.elements.id.value;
    var branchVal = $branchSelect.value;
    var payload = {
      team_name: $teamForm.elements.team_name.value.trim(),
      league: $teamForm.elements.league.value.trim() || null,
      division: $teamForm.elements.division.value.trim() || null,
      branch_id: branchVal === "" ? null : parseInt(branchVal, 10),
    };
    try {
      if (id) {
        await api("PATCH", "/org/api/teams/" + id, payload);
        window.OrgToast && window.OrgToast.show("הקבוצה עודכנה", "success");
      } else {
        await api("POST", "/org/api/teams", payload);
        window.OrgToast && window.OrgToast.show("הקבוצה נוצרה", "success");
      }
      closeModal($teamModal);
      loadTeams();
    } catch (e) {
      showError($teamError, e.message);
    }
  });

  // --- Coach assign ---
  function openAssignCoach(teamId, teamName, currentCoachId) {
    $coachForm.reset();
    $coachForm.elements.team_id.value = String(teamId);
    $coachTeamName.textContent = teamName;
    populateCoachSelector(currentCoachId || null);
    hideError($coachError);
    openModal($coachModal);
  }

  $coachForm.addEventListener("submit", async function (ev) {
    ev.preventDefault();
    hideError($coachError);
    var teamId = $coachForm.elements.team_id.value;
    var userId = $coachUserSelect.value;
    if (!userId) {
      showError($coachError, "בחר מאמן.");
      return;
    }
    try {
      await api("POST", "/org/api/teams/" + teamId + "/coaches", { user_id: parseInt(userId, 10) });
      window.OrgToast && window.OrgToast.show("המאמן שויך לקבוצה", "success");
      closeModal($coachModal);
      loadTeams();
    } catch (e) {
      showError($coachError, e.message);
    }
  });

  // --- Confirm (delete team / unassign coach) ---
  function openConfirm(title, message, onConfirm) {
    $confirmTitle.textContent = title;
    $confirmMsg.textContent = message;
    hideError($confirmError);
    pendingConfirm = { onConfirm: onConfirm };
    openModal($confirmModal);
  }

  async function runConfirm() {
    if (!pendingConfirm) return;
    try {
      await pendingConfirm.onConfirm();
      closeModal($confirmModal);
      pendingConfirm = null;
    } catch (e) {
      showError($confirmError, e.message);
    }
  }

  function confirmDeleteTeam(teamId, teamName) {
    openConfirm("מחיקת קבוצה", "האם למחוק את " + teamName + "?", async function () {
      await api("DELETE", "/org/api/teams/" + teamId);
      window.OrgToast && window.OrgToast.show("הקבוצה נמחקה", "success");
      loadTeams();
    });
  }

  // --- Delegation ---
  document.addEventListener("click", function (e) {
    var t = e.target.closest(
      "[data-action], [data-edit-team], [data-delete-team], [data-assign-coach]"
    );
    if (!t) return;
    if (t.dataset.action === "open-new-team") return openNewTeam();
    if (t.dataset.action === "close-team") return closeModal($teamModal);
    if (t.dataset.action === "close-coach") return closeModal($coachModal);
    if (t.dataset.action === "close-confirm") return closeModal($confirmModal);
    if (t.dataset.action === "do-confirm") return runConfirm();
    if (t.dataset.editTeam) return openEditTeam(t.dataset.editTeam);
    if (t.dataset.deleteTeam) return confirmDeleteTeam(t.dataset.deleteTeam, t.dataset.teamName);
    if (t.dataset.assignCoach) {
      return openAssignCoach(
        t.dataset.assignCoach,
        t.dataset.teamName,
        t.dataset.currentCoach ? parseInt(t.dataset.currentCoach, 10) : null
      );
    }
  });

  document.addEventListener("keydown", function (e) {
    if (e.key !== "Escape") return;
    closeModal($teamModal);
    closeModal($coachModal);
    closeModal($confirmModal);
  });

  [$teamModal, $coachModal, $confirmModal].forEach(function (m) {
    m.addEventListener("click", function (e) { if (e.target === m) closeModal(m); });
  });

  $branchFilter.addEventListener("change", loadTeams);
  if ($regionFilter) {
    $regionFilter.addEventListener("change", function () {
      refreshBranchFilter();
      loadTeams();
    });
  }

  // Boot.
  async function boot() {
    await loadReferenceData();
    await loadTeams();
  }
  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", boot);
  } else {
    boot();
  }
})();
