/* /org/document-templates list page — Phase 2.2.
 *
 * Lists, uploads, soft-deletes templates via /org/api/document-templates/*.
 * Security: every user-controlled value lands via textContent or setAttribute.
 * Only SVG icons (constant) are inserted via cloned DOMParser nodes.
 */

(function () {
  "use strict";

  var ctx = window.ORG_CTX || { role: "viewer", region_id: null, branch_id: null, user_id: null };
  var canManage = ["org_admin", "region_manager"].indexOf(ctx.role) >= 0;
  var canDelete = ctx.role === "org_admin";

  // Phase 13 — tenant URL prefix (e.g. "/org" or "/shaar-shivyon"). Pulled
  // from the shell-rendered `window.__ORG_ACTIVE__` so the same JS works
  // under both URL layouts.
  var URL_PREFIX = (window.__ORG_ACTIVE__ && window.__ORG_ACTIVE__.url_prefix) || "/org";

  var SVG_SEND_TPL_STR =
    '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.75" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">' +
    '<path d="M6 12L3.269 3.125A59.769 59.769 0 0121.485 12 59.768 59.768 0 013.27 20.875L5.999 12zm0 0h7.5"/>' +
    "</svg>";

  var SVG_EYE_TPL_STR =
    '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.75" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">' +
    '<path d="M2.036 12.322a1.012 1.012 0 010-.639C3.423 7.51 7.36 4.5 12 4.5c4.638 0 8.573 3.007 9.963 7.178.07.207.07.431 0 .639C20.577 16.49 16.64 19.5 12 19.5c-4.638 0-8.573-3.007-9.963-7.178z"/>' +
    '<path d="M15 12a3 3 0 11-6 0 3 3 0 016 0z"/>' +
    "</svg>";

  var SVG_CHECK_EMPTY_TPL_STR =
    '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.75" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">' +
    '<circle cx="12" cy="12" r="9"/>' +
    "</svg>";

  var SVG_CHECK_DONE_TPL_STR =
    '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="currentColor" aria-hidden="true">' +
    '<path d="M12 2a10 10 0 100 20 10 10 0 000-20zm-1 14.6l-4.3-4.3 1.4-1.4L11 13.8l5.9-5.9 1.4 1.4L11 16.6z"/>' +
    "</svg>";

  var CATEGORY_LABELS = {
    PARTICIPATION: "השתתפות",
    TOURNAMENT: "טורניר",
    SIZING: "מידות",
    HEALTH: "בריאות",
    PERMISSION: "אישור",
    OTHER: "אחר",
  };

  // --- DOM refs ---
  var $rows = document.querySelector("[data-templates-rows]");
  var $categoryFilter = document.getElementById("template-category-filter");
  var $includeInactive = document.getElementById("template-include-inactive");
  var $templateModal = document.getElementById("template-modal");
  var $templateForm = document.getElementById("template-form");
  var $templateError = $templateModal.querySelector("[data-error]");
  var $confirmModal = document.getElementById("confirm-modal");
  var $confirmName = $confirmModal.querySelector("[data-confirm-name]");
  var $confirmError = $confirmModal.querySelector("[data-confirm-error]");

  // --- State ---
  var pendingDelete = null; // { id, name }
  var regions = [];
  var branches = [];
  var teams = [];
  var refDataLoaded = false;
  var sendCountDebounce = null;

  // --- SVG icon templates (constant) ---
  var SVG_NS = "http://www.w3.org/2000/svg";
  function parseSvg(s) { return new DOMParser().parseFromString(s, "image/svg+xml").documentElement; }
  var SVG_EDIT_TPL = parseSvg(
    '<svg xmlns="' + SVG_NS + '" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.75" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">' +
    '<path d="M16.862 4.487l1.687-1.688a1.875 1.875 0 112.652 2.652L10.582 16.07a4.5 4.5 0 01-1.897 1.13l-2.685.8.8-2.685a4.5 4.5 0 011.13-1.897L16.863 4.487zm0 0L19.5 7.125M18 14v4.75A2.25 2.25 0 0115.75 21H5.25A2.25 2.25 0 013 18.75V8.25A2.25 2.25 0 015.25 6H10"/>' +
    "</svg>"
  );
  var SVG_SEND_TPL = parseSvg(SVG_SEND_TPL_STR);
  var SVG_EYE_TPL = parseSvg(SVG_EYE_TPL_STR);
  var SVG_CHECK_EMPTY_TPL = parseSvg(SVG_CHECK_EMPTY_TPL_STR);
  var SVG_CHECK_DONE_TPL = parseSvg(SVG_CHECK_DONE_TPL_STR);
  var SVG_TRASH_TPL = parseSvg(
    '<svg xmlns="' + SVG_NS + '" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.75" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">' +
    '<path d="M14.74 9l-.346 9m-4.788 0L9.26 9m9.968-3.21c.342.052.682.107 1.022.166m-1.022-.165L18.16 19.673a2.25 2.25 0 01-2.244 2.077H8.084a2.25 2.25 0 01-2.244-2.077L4.772 5.79m14.456 0a48.108 48.108 0 00-3.478-.397m-12 .562c.34-.059.68-.114 1.022-.165m0 0a48.11 48.11 0 013.478-.397m7.5 0v-.916c0-1.18-.91-2.164-2.09-2.201a51.964 51.964 0 00-3.32 0c-1.18.037-2.09 1.022-2.09 2.201v.916m7.5 0a48.667 48.667 0 00-7.5 0"/>' +
    "</svg>"
  );

  // --- Helpers ---
  function openModal(m) { m.classList.add("is-open"); m.setAttribute("aria-hidden", "false"); }
  function closeModal(m) { m.classList.remove("is-open"); m.setAttribute("aria-hidden", "true"); }
  function showError(el, text) { el.textContent = text; el.classList.remove("org-hidden"); }
  function hideError(el) { el.textContent = ""; el.classList.add("org-hidden"); }

  async function api(method, url, body) {
    var init = { method: method, headers: { "Accept": "application/json", "X-Requested-With": "XMLHttpRequest" } };
    if (body !== undefined && !(body instanceof FormData)) {
      init.headers["Content-Type"] = "application/json";
      init.body = JSON.stringify(body);
    } else if (body instanceof FormData) {
      init.body = body;  // browser sets multipart Content-Type with boundary
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

  function formatBytes(n) {
    if (!n && n !== 0) return "—";
    var kb = n / 1024;
    if (kb < 1024) return kb.toFixed(0) + " KB";
    return (kb / 1024).toFixed(2) + " MB";
  }

  // --- Load + render ---
  async function loadTemplates() {
    var qs = [];
    if ($categoryFilter && $categoryFilter.value) qs.push("category=" + encodeURIComponent($categoryFilter.value));
    if ($includeInactive && $includeInactive.checked) qs.push("include_inactive=true");
    var url = "/org/api/document-templates" + (qs.length ? "?" + qs.join("&") : "");
    try {
      var data = await api("GET", url);
      renderRows(data.templates || []);
    } catch (e) {
      setEmpty(e.message);
    }
  }

  function setEmpty(msg) {
    var colspan = canManage ? 7 : 6;
    $rows.replaceChildren(
      el("tr", null, [el("td", { className: "org-table-empty", text: msg, attrs: { colspan: String(colspan) } })])
    );
  }

  function renderRows(templates) {
    if (!templates.length) {
      setEmpty(canManage ? 'אין מסמכים. לחץ "מסמך חדש" כדי להעלות.' : "אין מסמכים.");
      return;
    }
    $rows.replaceChildren.apply(
      $rows,
      templates.map(function (t) {
        var isDone = !!t.is_completed;

        // Name cell with a click-to-toggle check button (managers only).
        var nameInner = [];
        if (canManage) {
          var doneBtn = document.createElement("button");
          doneBtn.type = "button";
          doneBtn.className = "org-done-toggle" + (isDone ? " is-done" : "");
          doneBtn.setAttribute("data-toggle-completed", String(t.id));
          doneBtn.setAttribute("data-target-state", isDone ? "false" : "true");
          doneBtn.setAttribute("data-name", t.name);
          doneBtn.title = isDone ? "החזר למסמכים פעילים" : "סמן כהסתיים";
          doneBtn.setAttribute("aria-label", doneBtn.title);
          doneBtn.appendChild((isDone ? SVG_CHECK_DONE_TPL : SVG_CHECK_EMPTY_TPL).cloneNode(true));
          nameInner.push(doneBtn);
        }
        nameInner.push(el("strong", { text: t.name }));

        var nameWrapper = el("div", { className: "org-flex org-items-center org-gap-2" }, nameInner);
        var nameCell = el("td", null, [nameWrapper]);
        if (t.description) {
          nameCell.appendChild(el("div", { className: "org-text-sm org-text-muted", text: t.description }));
        }

        var categoryCell = el("td", null, [pill(CATEGORY_LABELS[t.category] || t.category)]);
        var fileCell = el("td", null, [
          el("span", { className: "org-pill", text: t.uploaded_file_type }),
          el("span", { className: "org-text-sm org-text-muted", text: " " + formatBytes(t.uploaded_file_size) }),
        ]);
        var signatureCell = el("td", null, [
          pill(t.requires_signature ? "כן" : "לא", t.requires_signature ? "" : "org-pill--muted"),
        ]);
        var fieldCount = (t.form_fields ? t.form_fields.length : 0) + (t.signature_zones ? t.signature_zones.length : 0);
        var fieldsCell = el("td", null, [
          el("span", { className: fieldCount ? "org-pill" : "org-pill org-pill--muted", text: String(fieldCount) }),
        ]);

        // Status pill — "הסתיים" wins over "פעיל/לא פעיל" when applicable.
        var statusLabel = isDone ? "הסתיים" : (t.is_active ? "פעיל" : "לא פעיל");
        var statusKind = isDone ? "org-pill--ok" : (t.is_active ? "" : "org-pill--muted");
        var statusCell = el("td", null, [pill(statusLabel, statusKind)]);

        var cells = [nameCell, categoryCell, fileCell, signatureCell, fieldsCell, statusCell];

        if (canManage) {
          var actions = [];
          // Deliveries visibility link — always available, even for completed templates.
          var dlvLink = document.createElement("a");
          dlvLink.className = "org-btn-icon";
          dlvLink.href = URL_PREFIX + "/document-templates/" + t.id + "/deliveries";
          dlvLink.title = "צפייה באישורים";
          dlvLink.setAttribute("aria-label", "צפייה באישורים");
          dlvLink.appendChild(SVG_EYE_TPL.cloneNode(true));
          actions.push(dlvLink);

          // Completed templates: only view is allowed. Send/edit/delete hidden.
          if (!isDone) {
            if (t.uploaded_file_type === "PDF") {
              var editLink = document.createElement("a");
              editLink.className = "org-btn-icon";
              editLink.href = URL_PREFIX + "/document-templates/" + t.id + "/edit";
              editLink.title = "סימון שדות";
              editLink.setAttribute("aria-label", "סימון שדות");
              editLink.appendChild(SVG_EDIT_TPL.cloneNode(true));
              actions.push(editLink);
              if (t.is_active) {
                actions.push(iconBtn(SVG_SEND_TPL, {
                  type: "button",
                  "data-send-template": String(t.id),
                  "data-name": t.name,
                  title: "שלח להורים",
                  "aria-label": "שלח להורים",
                }, false));
              }
            }
            if (canDelete && t.is_active) {
              actions.push(iconBtn(SVG_TRASH_TPL, {
                type: "button",
                "data-delete-template": String(t.id),
                "data-name": t.name,
                title: "ביטול תבנית",
                "aria-label": "ביטול תבנית",
              }, true));
            }
          }
          cells.push(el("td", { className: "org-table-actions" }, actions));
        }
        var row = el("tr", null, cells);
        if (isDone) row.classList.add("is-completed");
        return row;
      })
    );
  }

  // --- New template (upload) ---
  function openNew() {
    $templateForm.reset();
    document.getElementById("template-requires-signature").checked = true;
    hideError($templateError);
    openModal($templateModal);
    setTimeout(function () { $templateForm.elements.name.focus(); }, 50);
  }

  $templateForm.addEventListener("submit", async function (ev) {
    ev.preventDefault();
    hideError($templateError);
    var file = document.getElementById("template-file").files[0];
    if (!file) {
      showError($templateError, "בחר קובץ.");
      return;
    }
    if (file.size > 10 * 1024 * 1024) {
      showError($templateError, "הקובץ גדול מ-10MB.");
      return;
    }

    var fd = new FormData();
    fd.append("name", document.getElementById("template-name").value.trim());
    var desc = document.getElementById("template-description").value.trim();
    if (desc) fd.append("description", desc);
    fd.append("category", document.getElementById("template-category").value);
    fd.append("requires_signature", document.getElementById("template-requires-signature").checked ? "true" : "false");
    fd.append("file", file);

    try {
      var created = await api("POST", "/org/api/document-templates", fd);
      window.OrgToast && window.OrgToast.show("המסמך הועלה", "success");
      closeModal($templateModal);
      // PDFs go straight to the field-marking editor; DOCX stays on list.
      if (created.uploaded_file_type === "PDF") {
        window.location.href = URL_PREFIX + "/document-templates/" + created.id + "/edit";
      } else {
        loadTemplates();
      }
    } catch (e) {
      showError($templateError, e.message);
    }
  });

  // --- Delete (soft) ---
  function confirmDelete(id, name) {
    $confirmName.textContent = name;
    hideError($confirmError);
    pendingDelete = { id: id, name: name };
    openModal($confirmModal);
  }

  async function doDelete() {
    if (!pendingDelete) return;
    try {
      await api("DELETE", "/org/api/document-templates/" + pendingDelete.id);
      window.OrgToast && window.OrgToast.show("התבנית בוטלה", "success");
      closeModal($confirmModal);
      pendingDelete = null;
      loadTemplates();
    } catch (e) {
      showError($confirmError, e.message);
    }
  }

  // --- Delegation ---
  // ---- Send-campaign modal ----
  var $sendModal = document.getElementById("send-modal");
  var $sendForm = document.getElementById("send-form");
  var $sendError = $sendModal && $sendModal.querySelector("[data-error]");
  var $sendCount = $sendModal && $sendModal.querySelector("[data-send-count]");
  var $sendCountHint = $sendModal && $sendModal.querySelector("[data-send-count-hint]");
  var $sendSubmit = $sendModal && $sendModal.querySelector("[data-send-submit]");

  async function ensureRefData() {
    if (refDataLoaded) return;
    try {
      var [r, b, t] = await Promise.all([
        api("GET", "/org/api/regions").catch(function () { return { regions: [] }; }),
        api("GET", "/org/api/branches").catch(function () { return { branches: [] }; }),
        api("GET", "/org/api/teams").catch(function () { return { teams: [] }; }),
      ]);
      regions = r.regions || [];
      branches = b.branches || [];
      teams = t.teams || [];
      fillSelect("send-region", regions, "— בחר מחוז —");
      fillSelect("send-branch", branches, "— בחר סניף —");
      fillSelect("send-team", teams, "— בחר קבוצה —", "team_name");
      refDataLoaded = true;
    } catch (_e) { /* swallow — modal still usable with all-org */ }
  }

  function fillSelect(id, items, blank, labelKey) {
    var sel = document.getElementById(id);
    if (!sel) return;
    while (sel.options.length > 0) sel.remove(0);
    var opt = document.createElement("option");
    opt.value = "";
    opt.textContent = blank;
    sel.appendChild(opt);
    var key = labelKey || "name";
    items.slice().sort(function (a, b) { return (a[key] || "").localeCompare(b[key] || ""); })
      .forEach(function (it) {
        var o = document.createElement("option");
        o.value = String(it.id);
        o.textContent = it[key];
        sel.appendChild(o);
      });
  }

  async function openSend(templateId, templateName) {
    if (!$sendModal) return;
    await ensureRefData();
    $sendForm.reset();
    $sendForm.elements.template_id.value = String(templateId);
    $sendForm.elements.title.value = templateName + " — " +
      new Date().toLocaleDateString("he-IL");
    // Reset the cascading dropdowns to their full lists.
    refreshBranchOptions("");
    refreshTeamOptions("", "");
    hideError($sendError);
    openModal($sendModal);
    refreshCount();
  }

  // Cascading: branch list narrowed to selected region (or all if region empty).
  function refreshBranchOptions(regionId) {
    var prev = document.getElementById("send-branch").value;
    var list = branches.filter(function (b) {
      return !regionId || String(b.region_id) === String(regionId);
    });
    fillSelectInline("send-branch", list, "כל הסניפים", "name");
    var stillValid = list.some(function (b) { return String(b.id) === prev; });
    document.getElementById("send-branch").value = stillValid ? prev : "";
  }

  // Cascading: team list narrowed to selected branch, or to teams in branches
  // of the selected region, or all.
  function refreshTeamOptions(regionId, branchId) {
    var prev = document.getElementById("send-team").value;
    var list = teams.filter(function (t) {
      if (branchId) return String(t.branch_id) === String(branchId);
      if (regionId) {
        // Find branches in the region, then teams in those branches.
        var branchIds = branches
          .filter(function (b) { return String(b.region_id) === String(regionId); })
          .map(function (b) { return String(b.id); });
        return branchIds.indexOf(String(t.branch_id)) !== -1;
      }
      return true;
    });
    fillSelectInline("send-team", list, "כל הקבוצות", "team_name");
    var stillValid = list.some(function (t) { return String(t.id) === prev; });
    document.getElementById("send-team").value = stillValid ? prev : "";
  }

  function fillSelectInline(id, items, blank, labelKey) {
    var sel = document.getElementById(id);
    if (!sel) return;
    while (sel.options.length > 0) sel.remove(0);
    var opt = document.createElement("option");
    opt.value = "";
    opt.textContent = blank;
    sel.appendChild(opt);
    items.slice().sort(function (a, b) { return (a[labelKey] || "").localeCompare(b[labelKey] || ""); })
      .forEach(function (it) {
        var o = document.createElement("option");
        o.value = String(it.id);
        o.textContent = it[labelKey];
        sel.appendChild(o);
      });
  }

  // Build recipient_filter from the lowest-selected dropdown.
  // team picked → team. else branch → branch. else region → region. else all.
  function currentFilter() {
    var rid = parseInt(document.getElementById("send-region").value, 10) || null;
    var bid = parseInt(document.getElementById("send-branch").value, 10) || null;
    var tid = parseInt(document.getElementById("send-team").value, 10) || null;
    if (tid) return { type: "team", team_ids: [tid] };
    if (bid) return { type: "branch", branch_id: bid };
    if (rid) return { type: "region", region_id: rid };
    return { type: "all" };
  }

  async function refreshCount() {
    if (!$sendCount) return;
    var rf = currentFilter();
    try {
      var data = await api("POST", "/org/api/document-campaigns/preview-recipients",
        { recipient_filter: rf });
      $sendCount.textContent = String(data.count);
      var scopeLabel = (
        rf.type === "team" ? "בקבוצה שנבחרה" :
        rf.type === "branch" ? "בסניף שנבחר" :
        rf.type === "region" ? "במחוז שנבחר" : "בארגון"
      );
      $sendCountHint.textContent = data.count
        ? "המסמך יישלח להורים " + scopeLabel + " ב-SMS / אימייל."
        : "אין נמענים תואמים — נסה לרענן את הפילטר.";
      $sendSubmit.disabled = data.count === 0;
    } catch (_e) {
      $sendCount.textContent = "—";
      $sendCountHint.textContent = "תצוגת ספירה נכשלה.";
    }
  }

  if ($sendModal) {
    // Cascading: a change to region narrows branch + team. A change to
    // branch narrows team. A change to team just refreshes the count.
    $sendModal.addEventListener("change", function (e) {
      if (e.target.id === "send-region") {
        refreshBranchOptions(e.target.value);
        refreshTeamOptions(e.target.value, "");
      } else if (e.target.id === "send-branch") {
        refreshTeamOptions(document.getElementById("send-region").value, e.target.value);
      }
      if (sendCountDebounce) clearTimeout(sendCountDebounce);
      sendCountDebounce = setTimeout(refreshCount, 120);
    });

    $sendForm.addEventListener("submit", async function (ev) {
      ev.preventDefault();
      hideError($sendError);
      var channels = [];
      if ($sendForm.elements.ch_sms.checked) channels.push("sms");
      if ($sendForm.elements.ch_email.checked) channels.push("email");
      if (!channels.length) {
        showError($sendError, "בחר לפחות ערוץ אחד.");
        return;
      }
      var payload = {
        template_id: parseInt($sendForm.elements.template_id.value, 10),
        title: $sendForm.elements.title.value.trim(),
        recipient_filter: currentFilter(),
        delivery_channels: channels,
      };
      $sendSubmit.disabled = true;
      try {
        var camp = await api("POST", "/org/api/document-campaigns", payload);
        window.OrgToast && window.OrgToast.show(
          "הקמפיין נוצר — נשלח ל-" + camp.total_recipients + " הורים", "success"
        );
        closeModal($sendModal);
      } catch (e) {
        showError($sendError, e.message);
        $sendSubmit.disabled = false;
      }
    });
  }

  async function toggleCompleted(id, targetState, name) {
    try {
      await api("POST", "/org/api/document-templates/" + id + "/completion",
        { is_completed: targetState });
      window.OrgToast && window.OrgToast.show(
        targetState ? "המסמך סומן כהסתיים" : "המסמך הוחזר לפעיל",
        "success"
      );
      loadTemplates();
    } catch (e) {
      alert(e.message || "שגיאה בעדכון סטטוס המסמך");
    }
  }

  document.addEventListener("click", function (e) {
    var t = e.target.closest("[data-action], [data-delete-template], [data-send-template], [data-toggle-completed]");
    if (!t) return;
    if (t.dataset.action === "open-new-template") return openNew();
    if (t.dataset.action === "close-template") return closeModal($templateModal);
    if (t.dataset.action === "close-confirm") return closeModal($confirmModal);
    if (t.dataset.action === "close-send" && $sendModal) return closeModal($sendModal);
    if (t.dataset.action === "do-confirm") return doDelete();
    if (t.dataset.deleteTemplate) return confirmDelete(parseInt(t.dataset.deleteTemplate, 10), t.dataset.name);
    if (t.dataset.sendTemplate) return openSend(parseInt(t.dataset.sendTemplate, 10), t.dataset.name);
    if (t.dataset.toggleCompleted) {
      var target = t.dataset.targetState === "true";
      return toggleCompleted(parseInt(t.dataset.toggleCompleted, 10), target, t.dataset.name);
    }
  });

  document.addEventListener("keydown", function (e) {
    if (e.key !== "Escape") return;
    closeModal($templateModal);
    closeModal($confirmModal);
    if ($sendModal) closeModal($sendModal);
  });

  [$templateModal, $confirmModal, $sendModal].forEach(function (m) {
    if (!m) return;
    m.addEventListener("click", function (e) { if (e.target === m) closeModal(m); });
  });

  $categoryFilter.addEventListener("change", loadTemplates);
  $includeInactive.addEventListener("change", loadTemplates);

  // Boot.
  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", loadTemplates);
  } else {
    loadTemplates();
  }
})();
