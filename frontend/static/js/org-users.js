/* /org/users page — members + invites management via /org/api/users/*.
 *
 * Security: every user-controlled value lands via Node.textContent or
 * setAttribute; only SVG icons are inserted via cloned DOMParser nodes.
 */

(function () {
  "use strict";

  var ctx = window.ORG_CTX || { role: "viewer", region_id: null, branch_id: null, user_id: null };
  var isAdmin = ctx.role === "org_admin";
  var isPM = ctx.role === "program_manager";
  var isRM = ctx.role === "region_manager";
  var canInvite = isAdmin || isPM || isRM;
  var canManageInvites = isAdmin || isPM || isRM;

  var ROLE_LABELS = {
    org_admin: "מנכ\"ל",
    program_manager: "מנהל תוכנית",
    region_manager: "מנהל מחוז",
    branch_manager: "מנהל סניף",  // legacy — kept so old rows still render
    coach: "מאמן",
    viewer: "צופה",
  };
  var STATUS_LABELS = {
    active: "פעיל",
    suspended: "מושעה",
    removed: "הוסר",
    pending: "ממתין",
    cancelled: "בוטל",
  };
  // Invitable roles per actor — mirrors ROLE_INVITES_TREE in
  // src/services/org_user_service.py. branch_manager is intentionally
  // excluded from invite UI (legacy; we don't issue new ones).
  var INVITABLE_ROLES_ADMIN = ["org_admin", "program_manager", "region_manager", "coach", "viewer"];
  var INVITABLE_ROLES_PM = ["region_manager", "coach", "viewer"];
  var INVITABLE_ROLES_RM = ["coach", "viewer"];

  // --- DOM refs ---
  var $membersRows = document.querySelector("[data-members-rows]");
  var $invitesRows = document.querySelector("[data-invites-rows]");
  var $inviteModal = document.getElementById("invite-modal");
  var $inviteForm = document.getElementById("invite-form");
  var $inviteError = $inviteModal.querySelector("[data-error]");
  var $inviteSuccessModal = document.getElementById("invite-success-modal");
  var $inviteSuccessCode = $inviteSuccessModal
    ? $inviteSuccessModal.querySelector("[data-success-code]") : null;
  var $inviteSuccessLink = $inviteSuccessModal
    ? $inviteSuccessModal.querySelector("[data-success-link]") : null;
  var $memberModal = document.getElementById("member-modal");           // null for non-admin
  var $memberForm = $memberModal ? document.getElementById("member-form") : null;
  var $memberError = $memberModal ? $memberModal.querySelector("[data-error]") : null;
  var $confirmModal = document.getElementById("confirm-modal");
  var $confirmTitle = $confirmModal.querySelector("[data-confirm-title]");
  var $confirmMsg = $confirmModal.querySelector("[data-confirm-message]");
  var $confirmError = $confirmModal.querySelector("[data-confirm-error]");

  // --- State ---
  var regionsById = {};
  var branchesById = {};
  var programsById = {};
  var teamNamesByCoachUserId = {};  // user_id -> [team_name, ...]  (for coach search)
  // Phase 14 — full teams list, used by the invite-team picker when the
  // selected role is "coach". Only unassigned teams (user_id == null) are
  // shown — the server rejects pre-assigned ones with 409 anyway.
  var allTeams = [];
  var allMembers = [];              // last-fetched (after server-side region filter)
  var allInvites = [];
  var pendingConfirm = null; // { onConfirm: () => Promise }
  var searchDebounce = null;

  // --- SVG icon templates (constant) ---
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
  var SVG_RESEND_TPL = parseSvg(
    '<svg xmlns="' + SVG_NS + '" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.75" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">' +
    '<path d="M16.023 9.348h4.992v-.001M2.985 19.644v-4.992m0 0h4.992m-4.993 0l3.181 3.183a8.25 8.25 0 0013.803-3.7M4.031 9.865a8.25 8.25 0 0113.803-3.7l3.181 3.182m0-4.991v4.99"/>' +
    "</svg>"
  );

  // --- Generic helpers ---
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

  // --- Boot: load reference data + tables ---
  async function loadReferenceData() {
    var [regions, branches, programs, teams] = await Promise.all([
      api("GET", "/org/api/regions").catch(function () { return { regions: [] }; }),
      api("GET", "/org/api/branches").catch(function () { return { branches: [] }; }),
      api("GET", "/org/api/programs").catch(function () { return { programs: [] }; }),
      // Teams power the "search by team name" path. Scoped on the server,
      // so region_manager / branch_manager naturally see only their slice.
      api("GET", "/org/api/teams").catch(function () { return { teams: [] }; }),
    ]);
    regionsById = {};
    (regions.regions || []).forEach(function (r) { regionsById[r.id] = r; });
    branchesById = {};
    (branches.branches || []).forEach(function (b) { branchesById[b.id] = b; });
    programsById = {};
    (programs.programs || []).forEach(function (p) { programsById[p.id] = p; });
    teamNamesByCoachUserId = {};
    (teams.teams || []).forEach(function (t) {
      if (t.user_id == null || !t.team_name) return;
      if (!teamNamesByCoachUserId[t.user_id]) teamNamesByCoachUserId[t.user_id] = [];
      teamNamesByCoachUserId[t.user_id].push(t.team_name);
    });
    allTeams = (teams.teams || []).slice();
    populateSelectors();
  }

  function inviteRolesAllowed() {
    if (isAdmin) return INVITABLE_ROLES_ADMIN;
    if (isPM) return INVITABLE_ROLES_PM;
    if (isRM) return INVITABLE_ROLES_RM;
    return [];
  }

  function populateSelectors() {
    var roleItems = inviteRolesAllowed().map(function (r) { return { value: r, label: ROLE_LABELS[r] }; });
    fillSelect(document.getElementById("invite-role"), roleItems, "value", "label", false);
    if (document.getElementById("member-role")) {
      var allRoles = INVITABLE_ROLES_ADMIN.map(function (r) { return { value: r, label: ROLE_LABELS[r] }; });
      fillSelect(document.getElementById("member-role"), allRoles, "value", "label", false);
    }

    var programList = Object.values(programsById).sort(function (a, b) { return a.name.localeCompare(b.name); });
    var inviteProgram = document.getElementById("invite-program");
    if (inviteProgram) {
      fillSelect(inviteProgram, programList, "id", "name", true, "— ללא תוכנית —");
      // PM is locked to their own program — pre-fill + disable.
      if (isPM && programList.length === 1) {
        inviteProgram.value = String(programList[0].id);
        inviteProgram.disabled = true;
      }
    }

    var regionList = Object.values(regionsById).sort(function (a, b) { return a.name.localeCompare(b.name); });
    var inviteRegion = document.getElementById("invite-region");
    fillSelect(inviteRegion, regionList, "id", "name", true, "— ללא מחוז —");
    if (isRM && ctx.region_id) {
      inviteRegion.value = String(ctx.region_id);
      inviteRegion.disabled = true;
    }
    if (document.getElementById("member-region")) {
      fillSelect(document.getElementById("member-region"), regionList, "id", "name", true, "— ללא מחוז —");
    }
    // The top-of-page list filter (org_admin only — server-side enforced).
    var $regionFilter = document.getElementById("users-region-filter");
    if ($regionFilter) {
      fillSelect($regionFilter, regionList, "id", "name", true, "כל המחוזות");
    }

    var branchList = Object.values(branchesById).sort(function (a, b) { return a.name.localeCompare(b.name); });
    var inviteBranch = document.getElementById("invite-branch");
    if (inviteBranch) {
      fillSelect(inviteBranch, branchList, "id", "name", true, "— ללא סניף —");
    }
    if (document.getElementById("member-branch")) {
      fillSelect(document.getElementById("member-branch"), branchList, "id", "name", true, "— ללא סניף —");
    }

    // Initial visibility — role might be pre-selected on render.
    updateScopeVisibility();
  }

  function updateScopeVisibility() {
    var roleEl = document.getElementById("invite-role");
    if (!roleEl) return;
    var role = roleEl.value;
    var $programGroup = document.querySelector("[data-program-group]");
    var $regionGroup = document.querySelector("[data-region-group]");
    var $branchGroup = document.querySelector("[data-branch-group]");
    var $teamGroup = document.querySelector("[data-team-group]");
    var $programSel = document.getElementById("invite-program");
    var $regionSel = document.getElementById("invite-region");
    var $branchSel = document.getElementById("invite-branch");
    var $teamSel = document.getElementById("invite-team");

    var showProgram = role === "program_manager";
    var showRegion = role === "region_manager" || role === "coach";
    // Branch is legacy — never shown for new invites.
    var showBranch = false;
    // Phase 14 — coach invites can pre-assign a team.
    var showTeam = role === "coach";

    if ($programGroup) $programGroup.style.display = showProgram ? "" : "none";
    if ($regionGroup) $regionGroup.style.display = showRegion ? "" : "none";
    if ($branchGroup) $branchGroup.style.display = showBranch ? "" : "none";
    if ($teamGroup) $teamGroup.style.display = showTeam ? "" : "none";

    // CRITICAL: hidden <select>s still submit their last value via FormData.
    // Clear any stale value when the field is no longer applicable, so the
    // server-side scope validator (_validate_scope) doesn't reject the
    // payload over a residual region_id / branch_id from a previous role.
    if (!showProgram && $programSel && !$programSel.disabled) $programSel.value = "";
    if (!showRegion && $regionSel && !$regionSel.disabled) $regionSel.value = "";
    if (!showBranch && $branchSel && !$branchSel.disabled) $branchSel.value = "";
    if (!showTeam && $teamSel) $teamSel.value = "";

    // When team picker is visible, refresh its options now (so the list
    // reflects the current program/region selection if those just changed).
    if (showTeam) refreshInviteTeamOptions();
  }

  // Populate the invite-team dropdown with unassigned teams in scope.
  // For PM/RM the scope is clamped server-side already (teams.user_id IS NULL
  // is the only client-side filter); for org_admin we also narrow by the
  // chosen program_id + region_id when those fields have values.
  function refreshInviteTeamOptions() {
    var $teamSel = document.getElementById("invite-team");
    if (!$teamSel) return;
    var pid = (document.getElementById("invite-program") || {}).value || "";
    var rid = (document.getElementById("invite-region") || {}).value || "";

    var filtered = allTeams.filter(function (t) {
      if (t.user_id != null) return false;           // already has a coach
      if (pid && String(t.program_id) !== pid) return false;
      if (rid && String(t.region_id) !== rid) return false;
      return true;
    }).sort(function (a, b) {
      return (a.team_name || "").localeCompare(b.team_name || "");
    });

    var prev = $teamSel.value;
    fillSelect($teamSel, filtered, "id", "team_name", true, "— בחר קבוצה —");
    var stillValid = filtered.some(function (t) { return String(t.id) === prev; });
    $teamSel.value = stillValid ? prev : "";
  }

  function scopeText(row) {
    var parts = [];
    if (row.region_id && regionsById[row.region_id]) parts.push(regionsById[row.region_id].name);
    if (row.branch_id && branchesById[row.branch_id]) parts.push(branchesById[row.branch_id].name);
    return parts.length ? parts.join(" · ") : "—";
  }

  function avatar(displayName, email) {
    var initial = (displayName || email || "?").slice(0, 1).toUpperCase();
    return el("span", { className: "org-avatar", text: initial });
  }

  // --- Members table ---
  async function loadMembers() {
    try {
      var url = "/org/api/users";
      var $regionFilter = document.getElementById("users-region-filter");
      if ($regionFilter && $regionFilter.value) {
        url += "?region_id=" + encodeURIComponent($regionFilter.value);
      }
      var data = await api("GET", url);
      allMembers = data.members || [];
      applyFilters();
    } catch (e) {
      renderMembersError(e.message);
    }
  }

  // Wire the region filter (only present for org_admin) to refresh members
  // on change. Populated in populateSelectors() with the regions list.
  document.addEventListener("change", function (e) {
    if (e.target && e.target.id === "users-region-filter") loadMembers();
  });

  // Free-text search across name, email, role label, region, branch, team names.
  // Debounced 120ms so the rendering doesn't churn while typing.
  document.addEventListener("input", function (e) {
    if (!e.target || e.target.id !== "users-search") return;
    if (searchDebounce) clearTimeout(searchDebounce);
    searchDebounce = setTimeout(applyFilters, 120);
  });

  function getSearchTerm() {
    var $s = document.getElementById("users-search");
    return $s ? $s.value.trim().toLowerCase() : "";
  }

  function memberMatchesSearch(m, q) {
    if (!q) return true;
    // Cheap path: short-circuit on any field hit.
    if ((m.display_name || "").toLowerCase().indexOf(q) !== -1) return true;
    if ((m.email || "").toLowerCase().indexOf(q) !== -1) return true;
    if ((ROLE_LABELS[m.role] || m.role || "").toLowerCase().indexOf(q) !== -1) return true;
    if (m.region_id && regionsById[m.region_id]
        && regionsById[m.region_id].name.toLowerCase().indexOf(q) !== -1) return true;
    if (m.branch_id && branchesById[m.branch_id]
        && branchesById[m.branch_id].name.toLowerCase().indexOf(q) !== -1) return true;
    var teamNames = teamNamesByCoachUserId[m.user_id];
    if (teamNames) {
      for (var i = 0; i < teamNames.length; i++) {
        if (teamNames[i].toLowerCase().indexOf(q) !== -1) return true;
      }
    }
    return false;
  }

  function inviteMatchesSearch(inv, q) {
    if (!q) return true;
    if ((inv.email || "").toLowerCase().indexOf(q) !== -1) return true;
    if ((ROLE_LABELS[inv.role] || inv.role || "").toLowerCase().indexOf(q) !== -1) return true;
    if (inv.region_id && regionsById[inv.region_id]
        && regionsById[inv.region_id].name.toLowerCase().indexOf(q) !== -1) return true;
    if (inv.branch_id && branchesById[inv.branch_id]
        && branchesById[inv.branch_id].name.toLowerCase().indexOf(q) !== -1) return true;
    return false;
  }

  function updateCount(filteredCount, totalCount) {
    var $c = document.querySelector("[data-users-count]");
    if (!$c) return;
    if (filteredCount === totalCount) {
      $c.textContent = totalCount + " חברים";
    } else {
      $c.textContent = filteredCount + " מתוך " + totalCount;
    }
  }

  function applyFilters() {
    var q = getSearchTerm();
    var filtered = allMembers.filter(function (m) { return memberMatchesSearch(m, q); });
    renderMembers(filtered);
    updateCount(filtered.length, allMembers.length);
    var filteredInvites = allInvites.filter(function (inv) { return inviteMatchesSearch(inv, q); });
    renderInvites(filteredInvites);
  }

  function renderMembersError(msg) {
    var colspan = isAdmin ? 5 : 4;
    $membersRows.replaceChildren(
      el("tr", null, [el("td", { className: "org-table-empty", text: msg, attrs: { colspan: String(colspan) } })])
    );
  }

  function renderMembers(members) {
    if (!members.length) {
      var q = getSearchTerm();
      renderMembersError(q ? "לא נמצאו תוצאות עבור \"" + q + "\"." : "אין חברים פעילים.");
      return;
    }
    $membersRows.replaceChildren.apply(
      $membersRows,
      members.map(function (m) {
        var nameCell = el("td", null, [
          el("div", { className: "org-flex org-items-center org-gap-3" }, [
            avatar(m.display_name, m.email),
            el("div", null, [
              el("div", { text: m.display_name || m.email, attrs: { style: "font-weight: 500;" } }),
              el("div", { className: "org-text-sm org-text-muted", text: m.display_name ? m.email : "", attrs: { dir: "ltr" } }),
            ]),
          ]),
        ]);
        var roleCell = el("td", null, [pill(ROLE_LABELS[m.role] || m.role)]);
        var scopeCell = el("td", null, [
          el("span", {
            className: scopeText(m) === "—" ? "org-pill org-pill--muted" : "org-pill",
            text: scopeText(m),
          }),
        ]);
        var statusCell = el("td", null, [pill(STATUS_LABELS[m.status] || m.status)]);
        var cells = [nameCell, roleCell, scopeCell, statusCell];
        if (isAdmin) {
          var actions = [
            iconBtn(SVG_EDIT_TPL, {
              type: "button",
              "data-edit-member": String(m.membership_id),
              title: "עריכה",
              "aria-label": "עריכה",
            }, false),
          ];
          // Block self-removal in the UI; the backend rejects it too.
          if (m.user_id !== ctx.user_id) {
            actions.push(iconBtn(SVG_TRASH_TPL, {
              type: "button",
              "data-remove-member": String(m.membership_id),
              "data-name": m.display_name || m.email,
              title: "הסרה",
              "aria-label": "הסרה",
            }, true));
          }
          cells.push(el("td", { className: "org-table-actions" }, actions));
        }
        return el("tr", null, cells);
      })
    );
  }

  // --- Pending invites table ---
  async function loadInvites() {
    try {
      var data = await api("GET", "/org/api/users/invites/pending");
      allInvites = data.invites || [];
      applyFilters();
    } catch (e) {
      renderInvitesError(e.message);
    }
  }

  function renderInvitesError(msg) {
    var colspan = canManageInvites ? 5 : 4;
    $invitesRows.replaceChildren(
      el("tr", null, [el("td", { className: "org-table-empty", text: msg, attrs: { colspan: String(colspan) } })])
    );
  }

  function formatCode(code) {
    // 8-char codes render as XXXX-XXXX for legibility.
    if (!code) return "—";
    if (code.length === 8) return code.slice(0, 4) + "-" + code.slice(4);
    return code;
  }

  function makeCodeCell(rawCode) {
    if (!rawCode) {
      return el("td", { className: "org-text-sm org-text-muted", text: "—" });
    }
    var pretty = formatCode(rawCode);
    var codeSpan = el("span", {
      text: pretty,
      attrs: {
        dir: "ltr",
        style: "font-family:Consolas,Menlo,monospace; letter-spacing:1px; font-weight:600;",
      },
    });
    var copyBtn = el("button", {
      attrs: {
        type: "button",
        title: "העתק קוד",
        "aria-label": "העתק קוד",
        "data-copy-code": rawCode,
        style: "margin-inline-start:8px; cursor:pointer; background:transparent; border:1px solid var(--org-gray-200); border-radius:4px; padding:2px 8px; font-size:12px;",
      },
      text: "העתק",
    });
    return el("td", null, [codeSpan, copyBtn]);
  }

  function renderInvites(invites) {
    if (!invites.length) {
      var q = getSearchTerm();
      // When the search is active and the full list isn't empty, this is a
      // "no matches" state rather than "no invites" — message accordingly.
      if (q && allInvites.length > 0) {
        renderInvitesError("אין התאמות לחיפוש.");
      } else {
        renderInvitesError("אין הזמנות ממתינות.");
      }
      return;
    }
    $invitesRows.replaceChildren.apply(
      $invitesRows,
      invites.map(function (i) {
        var emailCell = el("td", null, [el("span", { text: i.email, attrs: { dir: "ltr" } })]);
        var roleCell = el("td", null, [pill(ROLE_LABELS[i.role] || i.role)]);
        var codeCell = makeCodeCell(i.short_code);
        var dateCell = el("td", { className: "org-text-sm org-text-muted",
          text: i.created_at ? new Date(i.created_at).toLocaleDateString("he-IL") : "—" });
        var cells = [emailCell, roleCell, codeCell, dateCell];
        if (canManageInvites) {
          cells.push(el("td", { className: "org-table-actions" }, [
            iconBtn(SVG_RESEND_TPL, {
              type: "button",
              "data-resend-invite": String(i.id),
              "data-email": i.email,
              title: "שלח שוב",
              "aria-label": "שלח שוב",
            }, false),
            iconBtn(SVG_TRASH_TPL, {
              type: "button",
              "data-cancel-invite": String(i.id),
              "data-email": i.email,
              title: "ביטול",
              "aria-label": "ביטול",
            }, true),
          ]));
        }
        return el("tr", null, cells);
      })
    );
  }

  // --- Invite modal ---
  function openInvite() {
    $inviteForm.reset();
    hideError($inviteError);
    if (isRM && ctx.region_id) {
      document.getElementById("invite-region").value = String(ctx.region_id);
    }
    // Re-apply scope-field visibility (form.reset() restored defaults).
    updateScopeVisibility();
    openModal($inviteModal);
    setTimeout(function () { $inviteForm.elements.email.focus(); }, 50);
  }

  function openInviteSuccess(rawCode) {
    if (!$inviteSuccessModal) return;
    var pretty = formatCode(rawCode);
    if ($inviteSuccessCode) $inviteSuccessCode.textContent = pretty;
    // Phase 13 — root-level /join alias is the canonical URL for sharing.
    // Legacy /org/join 301-redirects to /join, so old shared codes still work.
    var joinUrl = window.location.origin + "/join?code=" + encodeURIComponent(rawCode);
    if ($inviteSuccessLink) $inviteSuccessLink.value = joinUrl;
    // Stash raw code on the modal so the per-button copy handlers can read it.
    $inviteSuccessModal.dataset.rawCode = rawCode;
    $inviteSuccessModal.dataset.joinUrl = joinUrl;
    openModal($inviteSuccessModal);
  }

  function scrollToInvitesTable() {
    var $section = document.getElementById("invites-table");
    if (!$section) return;
    $section.scrollIntoView({ behavior: "smooth", block: "start" });
  }

  // Show/hide program + region fields when the role changes. Phase 14 —
  // when program/region change while the team picker is visible (coach role),
  // re-filter the team list so org_admin's narrow-by-program/region works.
  var $inviteRoleSel = document.getElementById("invite-role");
  if ($inviteRoleSel) {
    $inviteRoleSel.addEventListener("change", updateScopeVisibility);
  }
  var _inviteProgSel = document.getElementById("invite-program");
  var _inviteRegSel = document.getElementById("invite-region");
  if (_inviteProgSel) _inviteProgSel.addEventListener("change", refreshInviteTeamOptions);
  if (_inviteRegSel) _inviteRegSel.addEventListener("change", refreshInviteTeamOptions);

  $inviteForm.addEventListener("submit", async function (ev) {
    ev.preventDefault();
    hideError($inviteError);
    var fd = new FormData($inviteForm);
    var role = fd.get("role");
    // Per-role scope policy — mirror _validate_scope() on the server so
    // residual values from prior selections don't leak into the payload.
    var allowProgram = role === "program_manager";
    var allowRegion = role === "region_manager" || role === "coach";
    var allowTeam = role === "coach";
    var payload = {
      email: (fd.get("email") || "").trim(),
      role: role,
      program_id: (allowProgram && fd.get("program_id"))
        ? parseInt(fd.get("program_id"), 10) : null,
      region_id: (allowRegion && fd.get("region_id"))
        ? parseInt(fd.get("region_id"), 10) : null,
      branch_id: null,  // legacy — never assigned on new invites
      team_id: (allowTeam && fd.get("team_id"))
        ? parseInt(fd.get("team_id"), 10) : null,
    };
    try {
      var result = await api("POST", "/org/api/users/invite", payload);
      var code = result && result.short_code;
      closeModal($inviteModal);
      if (code) {
        // Open the dedicated success modal — stays until the inviter
        // dismisses it, so the code + URL stay visible for copy/share.
        openInviteSuccess(code);
      } else {
        // No code path (legacy / edge case) — fall back to a brief toast.
        window.OrgToast && window.OrgToast.show("ההזמנה נשלחה", "success");
      }
      loadInvites();
    } catch (e) {
      showError($inviteError, e.message);
    }
  });

  // --- Edit member modal (admin only) ---
  async function openEditMember(membershipId) {
    if (!$memberModal) return;
    try {
      var data = await api("GET", "/org/api/users");
      var member = (data.members || []).find(function (m) { return String(m.membership_id) === String(membershipId); });
      if (!member) throw { message: "החבר לא נמצא" };
      $memberForm.elements.membership_id.value = String(member.membership_id);
      $memberModal.querySelector("[data-member-email]").textContent =
        (member.display_name ? member.display_name + " · " : "") + member.email;
      $memberForm.elements.role.value = member.role;
      $memberForm.elements.region_id.value = member.region_id != null ? String(member.region_id) : "";
      $memberForm.elements.branch_id.value = member.branch_id != null ? String(member.branch_id) : "";
      hideError($memberError);
      openModal($memberModal);
    } catch (e) {
      window.OrgToast && window.OrgToast.show(e.message, "danger");
    }
  }

  if ($memberForm) {
    $memberForm.addEventListener("submit", async function (ev) {
      ev.preventDefault();
      hideError($memberError);
      var id = $memberForm.elements.membership_id.value;
      var regionVal = $memberForm.elements.region_id.value;
      var branchVal = $memberForm.elements.branch_id.value;
      var payload = {
        role: $memberForm.elements.role.value,
        region_id: regionVal === "" ? null : parseInt(regionVal, 10),
        branch_id: branchVal === "" ? null : parseInt(branchVal, 10),
      };
      try {
        await api("PATCH", "/org/api/users/" + id, payload);
        window.OrgToast && window.OrgToast.show("הפרטים נשמרו", "success");
        closeModal($memberModal);
        loadMembers();
      } catch (e) {
        showError($memberError, e.message);
      }
    });
  }

  // --- Confirm modal (remove member / cancel invite) ---
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

  function confirmRemoveMember(membershipId, name) {
    openConfirm("הסרת חבר", "האם להסיר את " + name + " מהארגון?", async function () {
      await api("DELETE", "/org/api/users/" + membershipId);
      window.OrgToast && window.OrgToast.show("החבר הוסר", "success");
      loadMembers();
    });
  }

  async function resendInviteAction(inviteId, email) {
    try {
      await api("POST", "/org/api/users/invites/" + inviteId + "/resend");
      window.OrgToast && window.OrgToast.show("ההזמנה נשלחה שוב ל-" + email, "success");
      loadInvites();
    } catch (e) {
      window.OrgToast && window.OrgToast.show(e.message, "danger");
    }
  }

  function confirmCancelInvite(inviteId, email) {
    openConfirm("ביטול הזמנה", "האם לבטל את ההזמנה ל-" + email + "?", async function () {
      await api("DELETE", "/org/api/users/invites/" + inviteId);
      window.OrgToast && window.OrgToast.show("ההזמנה בוטלה", "success");
      loadInvites();
    });
  }

  function copyToClipboard(value, successMsg) {
    (navigator.clipboard ? navigator.clipboard.writeText(value) : Promise.reject())
      .then(function () { window.OrgToast && window.OrgToast.show(successMsg + " — " + value, "success"); })
      .catch(function () { window.OrgToast && window.OrgToast.show("העתקה נכשלה. ערך: " + value, "warning"); });
  }

  // --- Event delegation ---
  document.addEventListener("click", function (e) {
    var t = e.target.closest(
      "[data-action], [data-edit-member], [data-remove-member], [data-resend-invite], [data-cancel-invite], [data-copy-code], [data-copy-success-code], [data-copy-success-link]"
    );
    if (!t) return;
    if (t.dataset.action === "open-invite") return openInvite();
    if (t.dataset.action === "close-invite") return closeModal($inviteModal);
    if (t.dataset.action === "close-invite-success" && $inviteSuccessModal) {
      closeModal($inviteSuccessModal);
      // Right after dismiss, take the inviter to the pending invites table
      // so they see the persistent record they can re-copy from later.
      setTimeout(scrollToInvitesTable, 200);
      return;
    }
    if (t.dataset.action === "close-member" && $memberModal) return closeModal($memberModal);
    if (t.dataset.action === "close-confirm") return closeModal($confirmModal);
    if (t.dataset.action === "do-confirm") return runConfirm();
    if (t.dataset.editMember) return openEditMember(t.dataset.editMember);
    if (t.dataset.removeMember) return confirmRemoveMember(t.dataset.removeMember, t.dataset.name);
    if (t.dataset.resendInvite) return resendInviteAction(t.dataset.resendInvite, t.dataset.email);
    if (t.dataset.cancelInvite) return confirmCancelInvite(t.dataset.cancelInvite, t.dataset.email);
    if (t.dataset.copyCode) {
      copyToClipboard(formatCode(t.dataset.copyCode), "הקוד הועתק");
      return;
    }
    if (t.hasAttribute("data-copy-success-code") && $inviteSuccessModal) {
      var raw = $inviteSuccessModal.dataset.rawCode || "";
      copyToClipboard(formatCode(raw), "הקוד הועתק");
      return;
    }
    if (t.hasAttribute("data-copy-success-link") && $inviteSuccessModal) {
      var url = $inviteSuccessModal.dataset.joinUrl || "";
      copyToClipboard(url, "הלינק הועתק");
      return;
    }
  });

  document.addEventListener("keydown", function (e) {
    if (e.key !== "Escape") return;
    closeModal($inviteModal);
    if ($inviteSuccessModal) closeModal($inviteSuccessModal);
    if ($memberModal) closeModal($memberModal);
    closeModal($confirmModal);
  });

  [$inviteModal, $inviteSuccessModal, $memberModal, $confirmModal].forEach(function (m) {
    if (!m) return;
    m.addEventListener("click", function (e) { if (e.target === m) closeModal(m); });
  });

  // --- Boot ---
  async function boot() {
    await loadReferenceData();
    await Promise.all([loadMembers(), loadInvites()]);
  }
  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", boot);
  } else {
    boot();
  }
})();
