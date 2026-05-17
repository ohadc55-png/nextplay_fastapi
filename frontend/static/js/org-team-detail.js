/* /org/teams/{id} — team drill-down + inline roster CRUD.
 *
 * Hydrates from /org/api/teams/{id}/detail. CREATE/UPDATE/DELETE on the
 * roster goes through /org/api/players/* (the same endpoints the dashboard
 * /org/api/players page uses).
 *
 * Security: every value lands via Node.textContent / setAttribute.
 */
(function () {
  "use strict";

  var ctx = window.ORG_TEAM || { team_id: null, role: "viewer" };
  var teamId = ctx.team_id;
  if (!teamId) return;

  // Anyone with org_admin/program_manager/region_manager/coach can create
  // players. The server enforces scope on its end.
  var WRITE_ROLES = ["org_admin", "program_manager", "region_manager", "coach"];
  var canWrite = WRITE_ROLES.indexOf(ctx.role) >= 0;

  // Phase 13 — tenant URL prefix (legacy "/org" or slug-prefixed).
  var URL_PREFIX = (window.__ORG_ACTIVE__ && window.__ORG_ACTIVE__.url_prefix) || "/org";

  var $title = document.querySelector("[data-team-title]");
  var $context = document.querySelector("[data-team-context]");
  var $tiles = document.querySelector("[data-team-tiles]");
  var $rosterRows = document.querySelector("[data-roster-rows]");
  var $actionsHost = document.querySelector("[data-actions-host]");
  var $actionsCol = document.querySelector("[data-actions-col]");
  var $practices = document.querySelector("[data-team-practices]");
  var $playerModal = document.getElementById("player-modal");
  var $playerForm = document.getElementById("player-form");
  var $playerError = $playerModal.querySelector("[data-error]");
  var $confirmModal = document.getElementById("confirm-modal");
  var $confirmTitle = $confirmModal.querySelector("[data-confirm-title]");
  var $confirmMsg = $confirmModal.querySelector("[data-confirm-message]");
  var $confirmError = $confirmModal.querySelector("[data-confirm-error]");

  var pendingConfirm = null;
  var rosterCache = [];

  if (canWrite) {
    if ($actionsHost) $actionsHost.hidden = false;
    if ($actionsCol) $actionsCol.hidden = false;
  }

  var SVG_NS = "http://www.w3.org/2000/svg";
  function parseSvg(s) { return new DOMParser().parseFromString(s, "image/svg+xml").documentElement; }
  var SVG_EDIT_TPL = parseSvg(
    '<svg xmlns="' + SVG_NS + '" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.75" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">' +
    '<path d="M16.862 4.487l1.687-1.688a1.875 1.875 0 112.652 2.652L10.582 16.07a4.5 4.5 0 01-1.897 1.13l-2.685.8.8-2.685a4.5 4.5 0 011.13-1.897L16.863 4.487zm0 0L19.5 7.125M18 14v4.75A2.25 2.25 0 0115.75 21H5.25A2.25 2.25 0 013 18.75V8.25A2.25 2.25 0 015.25 6H10"/>' +
    "</svg>"
  );
  var SVG_TRASH_TPL = parseSvg(
    '<svg xmlns="' + SVG_NS + '" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.75" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">' +
    '<path d="M14.74 9l-.346 9m-4.788 0L9.26 9m9.968-3.21c.342.052.682.107 1.022.166m-1.022-.165L18.16 19.673a2.25 2.25 0 01-2.244 2.077H8.084a2.25 2.25 0 01-2.244-2.077L4.772 5.79"/>' +
    "</svg>"
  );

  function openModal(m) { m.classList.add("is-open"); m.setAttribute("aria-hidden", "false"); }
  function closeModal(m) { m.classList.remove("is-open"); m.setAttribute("aria-hidden", "true"); }
  function showError(el, t) { el.textContent = t; el.classList.remove("org-hidden"); }
  function hideError(el) { el.textContent = ""; el.classList.add("org-hidden"); }

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
      throw { status: r.status, message: msg };
    }
    return data;
  }

  function fmtTime(iso) {
    if (!iso) return "—";
    var d = new Date(iso);
    return isNaN(d.getTime()) ? "—"
      : d.toLocaleTimeString("he-IL", { hour: "2-digit", minute: "2-digit" });
  }

  function renderHeader(team) {
    $title.textContent = team.team_name || "—";
    var parts = [];
    if (team.program_name) parts.push("תוכנית · " + team.program_name);
    if (team.region_name) parts.push("מחוז · " + team.region_name);
    if (team.coach_display_name) parts.push("מאמן · " + team.coach_display_name);
    $context.textContent = parts.join("  ·  ") || "ללא הקשר נוסף";
    var crumb = document.getElementById("crumb-team-name");
    if (crumb) crumb.textContent = team.team_name || "—";
    document.title = (team.team_name || "קבוצה") + " — NEXTPLAY Enterprise";
  }

  function renderTiles(data) {
    var tiles = [
      { label: "שחקנים פעילים", value: data.roster.length },
      { label: "אימוני היום", value: data.practices_today.length },
      { label: "ליגה", value: data.team.league || "—" },
    ];
    $tiles.replaceChildren.apply($tiles, tiles.map(function (t) {
      return el("div", { className: "org-stat" }, [
        el("div", { className: "org-stat-label", text: t.label }),
        el("div", { className: "org-stat-value", text: String(t.value) }),
      ]);
    }));
  }

  function renderRoster(roster) {
    if (!roster.length) {
      var col = canWrite ? 5 : 4;
      $rosterRows.replaceChildren(
        el("tr", null, [el("td", {
          className: "org-table-empty",
          text: canWrite ? 'אין שחקנים בסגל עדיין. לחץ "שחקן חדש" להוסיף.' : "אין שחקנים בסגל.",
          attrs: { colspan: String(col) },
        })])
      );
      return;
    }
    $rosterRows.replaceChildren.apply($rosterRows, roster.map(function (p) {
      var playerHref = URL_PREFIX + "/players/" + p.id;
      var numCell = el("td", null, [
        el("span", {
          className: p.number != null ? "org-pill" : "org-pill org-pill--muted",
          text: p.number != null ? String(p.number) : "—",
        }),
      ]);
      var nameLink = el("a", {
        text: p.name || "—",
        attrs: { href: playerHref, style: "color: inherit; text-decoration: none; font-weight: 600;" },
      });
      var nameCell = el("td", null, [nameLink]);
      var posCell = el("td", { text: p.position || "—" });
      var ageCell = el("td", { text: p.age != null ? String(p.age) : "—" });
      var cells = [numCell, nameCell, posCell, ageCell];
      if (canWrite) {
        cells.push(el("td", { className: "org-table-actions" }, [
          iconBtn(SVG_EDIT_TPL, {
            type: "button", "data-edit-player": String(p.id),
            title: "עריכה", "aria-label": "עריכה",
          }, false),
          iconBtn(SVG_TRASH_TPL, {
            type: "button", "data-delete-player": String(p.id), "data-player-name": p.name || "",
            title: "השבת", "aria-label": "השבת",
          }, true),
        ]));
      }
      var tr = el("tr", null, cells);
      tr.style.cursor = "pointer";
      // Whole-row click → player detail; ignore clicks on buttons / link itself.
      tr.addEventListener("click", function (e) {
        if (e.target.closest("button, a")) return;
        window.location.href = playerHref;
      });
      return tr;
    }));
  }

  function renderPractices(practices) {
    if (!practices.length) {
      $practices.replaceChildren(
        el("p", { className: "org-text-sm org-text-muted", text: "אין אימונים מתוכננים להיום." })
      );
      return;
    }
    var list = el("ul", { attrs: { style: "list-style: none; padding: 0; margin: 0;" } });
    practices.forEach(function (p) {
      list.appendChild(el("li", {
        attrs: {
          style: "display:flex; gap:12px; padding:8px 0; border-bottom:1px solid var(--org-gray-100);",
        },
      }, [
        el("span", { className: "org-text-sm org-text-muted", text: fmtTime(p.scheduled_at) }),
        el("div", { attrs: { style: "flex:1;" } }, [
          el("div", { text: p.title || "אימון", attrs: { style: "font-weight:600;" } }),
          el("div", { className: "org-text-sm org-text-muted", text: p.location || "" }),
        ]),
      ]));
    });
    $practices.replaceChildren(list);
  }

  async function loadDetail() {
    try {
      var data = await api("GET", "/org/api/teams/" + teamId + "/detail");
      rosterCache = data.roster || [];
      renderHeader(data.team || {});
      renderTiles(data);
      renderRoster(data.roster || []);
      renderPractices(data.practices_today || []);
    } catch (e) {
      $title.textContent = e.message;
      $tiles.replaceChildren(el("div", { className: "org-empty", text: e.message }));
    }
  }

  // --- Player modal ---
  function openCreatePlayer() {
    $playerForm.reset();
    $playerForm.elements.id.value = "";
    document.getElementById("player-modal-title").textContent = "שחקן חדש";
    hideError($playerError);
    openModal($playerModal);
    setTimeout(function () { $playerForm.elements.name.focus(); }, 50);
  }

  function openEditPlayer(playerId) {
    var p = rosterCache.find(function (x) { return String(x.id) === String(playerId); });
    if (!p) return;
    $playerForm.elements.id.value = String(p.id);
    $playerForm.elements.name.value = p.name || "";
    $playerForm.elements.number.value = p.number != null ? String(p.number) : "";
    $playerForm.elements.position.value = p.position || "";
    $playerForm.elements.age.value = p.age != null ? String(p.age) : "";
    document.getElementById("player-modal-title").textContent = "עריכת שחקן";
    hideError($playerError);
    openModal($playerModal);
  }

  $playerForm.addEventListener("submit", async function (ev) {
    ev.preventDefault();
    hideError($playerError);
    var fd = new FormData($playerForm);
    var id = fd.get("id");
    var payload = {
      name: (fd.get("name") || "").trim(),
      number: fd.get("number") ? parseInt(fd.get("number"), 10) : null,
      position: (fd.get("position") || "").trim() || null,
      age: fd.get("age") ? parseInt(fd.get("age"), 10) : null,
    };
    try {
      if (id) {
        await api("PATCH", "/org/api/players/" + id, payload);
        window.OrgToast && window.OrgToast.show("השחקן עודכן", "success");
      } else {
        payload.team_id = parseInt(teamId, 10);
        await api("POST", "/org/api/players", payload);
        window.OrgToast && window.OrgToast.show("השחקן נוצר", "success");
      }
      closeModal($playerModal);
      loadDetail();
    } catch (e) {
      showError($playerError, e.message);
    }
  });

  // --- Confirm (deactivate) ---
  function openConfirm(title, message, action) {
    pendingConfirm = action;
    $confirmTitle.textContent = title;
    $confirmMsg.textContent = message;
    hideError($confirmError);
    openModal($confirmModal);
  }
  async function runConfirm() {
    if (!pendingConfirm) return;
    try {
      await pendingConfirm();
      closeModal($confirmModal);
      pendingConfirm = null;
    } catch (e) {
      showError($confirmError, e.message);
    }
  }

  function deactivatePlayer(playerId, name) {
    openConfirm("השבתת שחקן", "האם להשבית את " + (name || "השחקן") + "?", async function () {
      await api("DELETE", "/org/api/players/" + playerId);
      window.OrgToast && window.OrgToast.show("השחקן הושבת", "success");
      loadDetail();
    });
  }

  // --- Event delegation ---
  document.addEventListener("click", function (e) {
    var t = e.target.closest("[data-action], [data-edit-player], [data-delete-player]");
    if (!t) return;
    if (t.dataset.action === "open-new-player") return openCreatePlayer();
    if (t.dataset.action === "close-player") return closeModal($playerModal);
    if (t.dataset.action === "close-confirm") return closeModal($confirmModal);
    if (t.dataset.action === "do-confirm") return runConfirm();
    if (t.dataset.editPlayer) return openEditPlayer(t.dataset.editPlayer);
    if (t.dataset.deletePlayer) return deactivatePlayer(t.dataset.deletePlayer, t.dataset.playerName);
  });
  document.addEventListener("keydown", function (e) {
    if (e.key === "Escape") { closeModal($playerModal); closeModal($confirmModal); }
  });
  [$playerModal, $confirmModal].forEach(function (m) {
    m.addEventListener("click", function (e) { if (e.target === m) closeModal(m); });
  });

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", loadDetail);
  } else {
    loadDetail();
  }
})();
