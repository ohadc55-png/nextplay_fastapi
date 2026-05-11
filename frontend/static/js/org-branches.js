/* /org/branches page — list/create/update/delete via /org/api/branches/*.
 * Loads region list once into the modal selector + region filter.
 *
 * Security: every user-controlled value lands via Node.textContent or
 * setAttribute. Action-icon SVGs are parsed once from constant strings at
 * module load with DOMParser, then cloned for each row — no innerHTML.
 */

(function () {
  "use strict";

  var ctx = window.ORG_CTX || { role: "viewer", region_id: null, branch_id: null };
  var canCreate = ctx.role === "org_admin" || ctx.role === "region_manager";
  var canDelete = ctx.role === "org_admin";
  var canEdit = canCreate;

  var $rows = document.querySelector("#branches-table [data-rows]");
  var $modal = document.getElementById("branch-modal");
  var $deleteModal = document.getElementById("branch-delete-modal");
  var $form = document.getElementById("branch-form");
  var $error = $modal.querySelector("[data-error]");
  var $regionSelect = $form.elements.region_id;
  var $regionFilter = document.getElementById("region-filter");
  var $deleteName = $deleteModal.querySelector("[data-delete-name]");
  var $deleteError = $deleteModal.querySelector("[data-delete-error]");

  var pendingDeleteId = null;
  var regionsById = {};

  // Static SVG icons — parsed ONCE from constant strings, then cloned per row.
  var SVG_NS = "http://www.w3.org/2000/svg";
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

  function parseSvg(svg) {
    return new DOMParser().parseFromString(svg, "image/svg+xml").documentElement;
  }

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
    var colspan = canEdit ? 5 : 4;
    $rows.replaceChildren(
      el("tr", null, [el("td", {
        className: "org-table-empty",
        text: message,
        attrs: { colspan: String(colspan) },
      })])
    );
  }

  async function loadRegions() {
    try {
      var data = await api("GET", "/org/api/regions");
      regionsById = {};
      (data.regions || []).forEach(function (r) { regionsById[r.id] = r; });
      populateRegionSelectors(data.regions || []);
    } catch (_e) {
      regionsById = {};
    }
  }

  function populateRegionSelectors(regions) {
    function populate(sel) {
      while (sel.options.length > 1) sel.remove(1);
      regions.forEach(function (r) {
        var o = document.createElement("option");
        o.value = String(r.id);
        o.textContent = r.name;
        sel.appendChild(o);
      });
    }
    populate($regionFilter);
    populate($regionSelect);
  }

  async function loadBranches() {
    var url = "/org/api/branches";
    var rid = $regionFilter.value;
    if (rid) url += "?region_id=" + encodeURIComponent(rid);
    try {
      var data = await api("GET", url);
      renderRows(data.branches || []);
    } catch (e) {
      setEmpty(e.message);
    }
  }

  function renderRows(branches) {
    if (!branches.length) {
      var msg = "אין סניפים עדיין." + (canCreate ? ' לחץ על "סניף חדש" כדי להתחיל.' : "");
      setEmpty(msg);
      return;
    }
    $rows.replaceChildren.apply(
      $rows,
      branches.map(function (b) {
        // Whole row links to the detail page; the strong tag picks up the
        // pointer cursor via CSS .org-row-link.
        var nameLink = el("a", {
          attrs: {
            href: "/org/branches/" + b.id,
            style: "color: inherit; text-decoration: none; display: block;",
          },
        }, [el("strong", { text: b.name })]);
        var nameCell = el("td", null, [nameLink]);
        var regionName = b.region_id != null && regionsById[b.region_id]
          ? regionsById[b.region_id].name
          : "—";
        var regionCell = el("td", null, [
          el("span", {
            className: regionName === "—" ? "org-pill org-pill--muted" : "org-pill",
            text: regionName,
          }),
        ]);
        var teamCountCell = el("td", null, [
          el("span", {
            className: "org-pill",
            text: String(b.team_count != null ? b.team_count : 0),
          }),
        ]);
        var playerCountCell = el("td", null, [
          el("span", {
            className: "org-pill",
            text: String(b.player_count != null ? b.player_count : 0),
          }),
        ]);
        var cells = [nameCell, regionCell, teamCountCell, playerCountCell];
        if (canEdit) {
          var actions = [
            iconBtn(SVG_EDIT_TPL, {
              type: "button",
              "data-edit": String(b.id),
              title: "עריכה",
              "aria-label": "עריכה",
            }, false),
          ];
          if (canDelete) {
            actions.push(iconBtn(SVG_TRASH_TPL, {
              type: "button",
              "data-delete": String(b.id),
              "data-delete-label": b.name,
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

  // --- create / edit ---
  function openNew() {
    $form.reset();
    $form.elements.id.value = "";
    if (ctx.role === "region_manager" && ctx.region_id) {
      $regionSelect.value = String(ctx.region_id);
      $regionSelect.disabled = true;
    } else {
      $regionSelect.disabled = false;
    }
    document.getElementById("branch-modal-title").textContent = "סניף חדש";
    hideError($error);
    openModal($modal);
    setTimeout(function () { $form.elements.name.focus(); }, 50);
  }

  async function openEdit(id) {
    try {
      var b = await api("GET", "/org/api/branches/" + id);
      $form.elements.id.value = String(b.id);
      $form.elements.name.value = b.name;
      $regionSelect.value = b.region_id != null ? String(b.region_id) : "";
      $regionSelect.disabled = (ctx.role === "region_manager");
      document.getElementById("branch-modal-title").textContent = "עריכת סניף";
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
    var regionValue = $regionSelect.value;
    var payload = {
      name: $form.elements.name.value.trim(),
      region_id: regionValue === "" ? null : parseInt(regionValue, 10),
    };
    try {
      if (id) {
        await api("PATCH", "/org/api/branches/" + id, payload);
        window.OrgToast && window.OrgToast.show("הסניף עודכן", "success");
      } else {
        await api("POST", "/org/api/branches", payload);
        window.OrgToast && window.OrgToast.show("הסניף נוצר", "success");
      }
      closeModal($modal);
      loadBranches();
    } catch (e) {
      showError($error, e.message);
    }
  });

  // --- delete ---
  function openDelete(id, name) {
    pendingDeleteId = id;
    $deleteName.textContent = name;
    hideError($deleteError);
    openModal($deleteModal);
  }

  async function confirmDelete() {
    if (!pendingDeleteId) return;
    try {
      await api("DELETE", "/org/api/branches/" + pendingDeleteId);
      closeModal($deleteModal);
      window.OrgToast && window.OrgToast.show("הסניף נמחק", "success");
      pendingDeleteId = null;
      loadBranches();
    } catch (e) {
      showError($deleteError, e.message);
    }
  }

  // --- delegation ---
  document.addEventListener("click", function (e) {
    var t = e.target.closest("[data-action], [data-edit], [data-delete]");
    if (!t) return;
    if (t.dataset.action === "open-new-branch") return openNew();
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

  $regionFilter.addEventListener("change", loadBranches);

  async function boot() {
    await loadRegions();
    await loadBranches();
  }
  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", boot);
  } else {
    boot();
  }
})();
