/* /org/regions page — list/create/update/delete via /org/api/regions/*.
 * Vanilla JS; depends on org.js (loaded by base_org.html) for the fetch
 * CSRF patch + global toast helper window.OrgToast.
 *
 * Security: all user-controlled values land via Node.textContent or
 * setAttribute. Action-icon SVGs are parsed once from constant strings at
 * module load with DOMParser, then cloned for each row — no innerHTML.
 */

(function () {
  "use strict";

  var ctx = window.ORG_CTX || { role: "viewer", region_id: null };
  var canManage = ctx.role === "org_admin";

  var $rows = document.querySelector("#regions-table [data-rows]");
  var $modal = document.getElementById("region-modal");
  var $deleteModal = document.getElementById("region-delete-modal");
  var $form = document.getElementById("region-form");
  var $error = $modal.querySelector("[data-error]");
  var $deleteName = $deleteModal.querySelector("[data-delete-name]");
  var $deleteError = $deleteModal.querySelector("[data-delete-error]");

  var pendingDeleteId = null;

  var SVG_NS = "http://www.w3.org/2000/svg";
  function parseSvg(svg) {
    return new DOMParser().parseFromString(svg, "image/svg+xml").documentElement;
  }
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
    try { data = await r.json(); } catch (_e) { /* empty body */ }
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

  function setEmpty(message) {
    var colspan = canManage ? 7 : 6;
    $rows.replaceChildren(
      el("tr", null, [el("td", {
        className: "org-table-empty",
        text: message,
        attrs: { colspan: String(colspan) },
      })])
    );
  }

  function renderRows(regions) {
    if (!regions.length) {
      var msg = "אין אזורים עדיין." + (canManage ? ' לחץ על "אזור חדש" כדי להתחיל.' : "");
      setEmpty(msg);
      return;
    }
    $rows.replaceChildren.apply(
      $rows,
      regions.map(function (r) {
        var nameCell = el("td", null, [el("strong", { text: r.name })]);
        var managerCell = el("td", null, [
          el("span", {
            className: r.manager_name ? "org-pill" : "org-pill org-pill--muted",
            text: r.manager_name || "—",
          }),
        ]);
        var branchCell = el("td", null, [
          el("span", { className: "org-pill", text: String(r.branch_count != null ? r.branch_count : 0) }),
        ]);
        var teamCell = el("td", null, [
          el("span", { className: "org-pill", text: String(r.team_count != null ? r.team_count : 0) }),
        ]);
        var coachCell = el("td", null, [
          el("span", { className: "org-pill", text: String(r.coach_count != null ? r.coach_count : 0) }),
        ]);
        var playerCell = el("td", null, [
          el("span", { className: "org-pill", text: String(r.player_count != null ? r.player_count : 0) }),
        ]);
        var cells = [nameCell, managerCell, branchCell, teamCell, coachCell, playerCell];
        if (canManage) {
          var editBtn = iconBtn(SVG_EDIT_TPL, {
            type: "button",
            "data-edit": String(r.id),
            title: "עריכה",
            "aria-label": "עריכה",
          }, false);
          var delBtn = iconBtn(SVG_TRASH_TPL, {
            type: "button",
            "data-delete": String(r.id),
            "data-delete-label": r.name,
            title: "מחיקה",
            "aria-label": "מחיקה",
          }, true);
          cells.push(el("td", { className: "org-table-actions" }, [editBtn, delBtn]));
        }
        return el("tr", null, cells);
      })
    );
  }

  async function loadRegions() {
    try {
      var data = await api("GET", "/org/api/regions");
      renderRows(data.regions || []);
    } catch (e) {
      setEmpty(e.message);
    }
  }

  function openNew() {
    $form.reset();
    $form.elements.id.value = "";
    document.getElementById("region-modal-title").textContent = "אזור חדש";
    hideError($error);
    openModal($modal);
    setTimeout(function () { $form.elements.name.focus(); }, 50);
  }

  async function openEdit(id) {
    try {
      var r = await api("GET", "/org/api/regions/" + id);
      $form.elements.id.value = String(r.id);
      $form.elements.name.value = r.name;
      document.getElementById("region-modal-title").textContent = "עריכת אזור";
      hideError($error);
      openModal($modal);
    } catch (e) {
      window.OrgToast && window.OrgToast.show(e.message, "danger");
    }
  }

  $form.addEventListener("submit", async function (ev) {
    ev.preventDefault();
    hideError($error);
    var id = $form.elements.id.value;
    var payload = { name: $form.elements.name.value.trim() };
    try {
      if (id) {
        await api("PATCH", "/org/api/regions/" + id, payload);
        window.OrgToast && window.OrgToast.show("האזור עודכן", "success");
      } else {
        await api("POST", "/org/api/regions", payload);
        window.OrgToast && window.OrgToast.show("האזור נוצר", "success");
      }
      closeModal($modal);
      loadRegions();
    } catch (e) {
      showError($error, e.message);
    }
  });

  function openDelete(id, name) {
    pendingDeleteId = id;
    $deleteName.textContent = name;
    hideError($deleteError);
    openModal($deleteModal);
  }

  async function confirmDelete() {
    if (!pendingDeleteId) return;
    try {
      await api("DELETE", "/org/api/regions/" + pendingDeleteId);
      closeModal($deleteModal);
      window.OrgToast && window.OrgToast.show("האזור נמחק", "success");
      pendingDeleteId = null;
      loadRegions();
    } catch (e) {
      showError($deleteError, e.message);
    }
  }

  document.addEventListener("click", function (e) {
    var t = e.target.closest("[data-action], [data-edit], [data-delete]");
    if (!t) return;
    if (t.dataset.action === "open-new-region") return openNew();
    if (t.dataset.action === "close-modal") return closeModal($modal);
    if (t.dataset.action === "close-delete") return closeModal($deleteModal);
    if (t.dataset.action === "confirm-delete") return confirmDelete();
    if (t.dataset.edit) return openEdit(t.dataset.edit);
    if (t.dataset.delete) return openDelete(t.dataset.delete, t.dataset.deleteLabel);
  });

  document.addEventListener("keydown", function (e) {
    if (e.key === "Escape") {
      closeModal($modal);
      closeModal($deleteModal);
    }
  });

  [$modal, $deleteModal].forEach(function (m) {
    m.addEventListener("click", function (e) { if (e.target === m) closeModal(m); });
  });

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", loadRegions);
  } else {
    loadRegions();
  }
})();
