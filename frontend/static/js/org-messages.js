/* /org/messages — Phase 2.5 messaging module.
 *
 * Tabs: All / Drafts / Sending / Sent. Inline modal supports both save-as-draft
 * and send-now. Drafts can be re-opened and edited. Cascading region→branch→team
 * filter shares the lowest-selected-wins semantics with /org/document-templates.
 *
 * Security: user content lands via textContent only.
 */

(function () {
  "use strict";

  var ctx = window.ORG_CTX || { role: "viewer", region_id: null, branch_id: null, user_id: null };
  var canManage = ["org_admin", "region_manager", "branch_manager"].indexOf(ctx.role) >= 0;

  var STATUS_LABELS = {
    DRAFT: "טיוטה",
    SCHEDULED: "מתוזמן",
    SENDING: "בתהליך",
    SENT: "נשלח",
    CANCELLED: "בוטל",
  };

  // --- DOM refs ---
  var $rows = document.querySelector("[data-messages-rows]");
  var $tabs = document.querySelectorAll(".org-tab[data-tab]");
  var $modal = document.getElementById("message-modal");
  var $form = document.getElementById("message-form");
  var $error = $modal.querySelector("[data-error]");
  var $confirm = document.getElementById("confirm-msg-modal");
  var $confirmName = $confirm.querySelector("[data-confirm-msg-name]");
  var $confirmError = $confirm.querySelector("[data-confirm-msg-error]");
  var $count = $modal.querySelector("[data-msg-count]");
  var $countHint = $modal.querySelector("[data-msg-count-hint]");
  var $submit = $modal.querySelector("[data-msg-submit]");
  var $modalTitle = document.getElementById("message-modal-title");

  // --- State ---
  var activeTab = "all";
  var regions = [];
  var branches = [];
  var teams = [];
  var refDataLoaded = false;
  var countDebounce = null;
  var pendingDelete = null;
  var editingMessage = null; // when reopening a DRAFT

  // --- Helpers ---
  function openModal(m) { m.classList.add("is-open"); m.setAttribute("aria-hidden", "false"); }
  function closeModal(m) { m.classList.remove("is-open"); m.setAttribute("aria-hidden", "true"); }
  function showError(el, t) { el.textContent = t; el.classList.remove("org-hidden"); }
  function hideError(el) { el.textContent = ""; el.classList.add("org-hidden"); }

  async function api(method, url, body) {
    var init = { method: method, headers: { "Accept": "application/json", "X-Requested-With": "XMLHttpRequest" } };
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

  function pill(text, kind) {
    return el("span", { className: "org-pill" + (kind ? " " + kind : ""), text: text });
  }

  function formatDate(iso) {
    if (!iso) return "—";
    try {
      var d = new Date(iso);
      return d.toLocaleDateString("he-IL") + " " + d.toLocaleTimeString("he-IL", { hour: "2-digit", minute: "2-digit" });
    } catch (_e) { return iso; }
  }

  // --- Load + render ---
  async function load() {
    var url = "/org/api/messages";
    if (activeTab !== "all") {
      url += "?status_filter=" + encodeURIComponent(activeTab);
    }
    try {
      var data = await api("GET", url);
      renderRows(data.messages || []);
    } catch (e) {
      setEmpty(e.message);
    }
  }

  function setEmpty(msg) {
    var colspan = canManage ? 6 : 5;
    $rows.replaceChildren(
      el("tr", null, [el("td", { className: "org-table-empty", text: msg, attrs: { colspan: String(colspan) } })])
    );
  }

  function renderRows(messages) {
    if (!messages.length) {
      setEmpty(canManage ? "אין הודעות. לחץ \"הודעה חדשה\" כדי להתחיל." : "אין הודעות.");
      return;
    }
    $rows.replaceChildren.apply(
      $rows,
      messages.map(function (m) {
        var subject = el("td", null, [el("strong", { text: m.subject })]);
        if (m.body) {
          var preview = m.body.length > 70 ? m.body.slice(0, 70) + "…" : m.body;
          subject.appendChild(el("div", { className: "org-text-sm org-text-muted", text: preview }));
        }
        var statusKind = m.status === "DRAFT" ? "org-pill--muted"
          : (m.status === "SENT" ? "org-pill--ok"
            : (m.status === "SCHEDULED" ? "org-pill--warn" : ""));
        var statusCellInner = [pill(STATUS_LABELS[m.status] || m.status, statusKind)];
        if (m.status === "SCHEDULED" && m.scheduled_at) {
          statusCellInner.push(el("div", {
            className: "org-text-sm org-text-muted",
            text: formatDate(m.scheduled_at),
          }));
        }
        var status = el("td", null, statusCellInner);

        var channels = el("td", { className: "org-text-sm", text: (m.delivery_channels || []).map(function (c) { return c === "sms" ? "SMS" : "אימייל"; }).join(" · ") || "—" });

        var recip = el("td", null);
        if (m.status === "DRAFT") {
          recip.textContent = "—";
        } else {
          var line = String(m.total_delivered || 0) + " / " + String(m.total_recipients || 0);
          recip.appendChild(el("strong", { text: line }));
          if (m.total_failed) {
            recip.appendChild(el("div", {
              className: "org-text-sm org-text-muted",
              text: String(m.total_failed) + " נכשלו",
            }));
          }
        }

        var sentAt = el("td", { className: "org-text-sm org-text-muted", text: formatDate(m.sent_at) });

        var cells = [subject, status, channels, recip, sentAt];

        if (canManage) {
          var actions = [];
          if (m.status === "DRAFT") {
            var editBtn = document.createElement("button");
            editBtn.type = "button";
            editBtn.className = "org-btn org-btn--ghost org-btn--sm";
            editBtn.textContent = "ערוך";
            editBtn.setAttribute("data-edit-message", String(m.id));
            actions.push(editBtn);

            var sendNow = document.createElement("button");
            sendNow.type = "button";
            sendNow.className = "org-btn org-btn--primary org-btn--sm";
            sendNow.textContent = "שלח";
            sendNow.setAttribute("data-send-draft", String(m.id));
            actions.push(sendNow);

            var del = document.createElement("button");
            del.type = "button";
            del.className = "org-btn org-btn--danger org-btn--sm";
            del.textContent = "מחק";
            del.setAttribute("data-delete-message", String(m.id));
            del.setAttribute("data-name", m.subject);
            actions.push(del);
          }
          cells.push(el("td", { className: "org-table-actions" }, actions));
        }

        return el("tr", null, cells);
      })
    );
  }

  // --- Tabs ---
  $tabs.forEach(function (t) {
    t.addEventListener("click", function () {
      $tabs.forEach(function (x) { x.classList.remove("is-active"); });
      t.classList.add("is-active");
      activeTab = t.getAttribute("data-tab") || "all";
      load();
    });
  });

  // --- Cascading dropdowns ---
  async function ensureRefData() {
    if (refDataLoaded) return;
    try {
      var rrs = await Promise.all([
        api("GET", "/org/api/regions").catch(function () { return { regions: [] }; }),
        api("GET", "/org/api/branches").catch(function () { return { branches: [] }; }),
        api("GET", "/org/api/teams").catch(function () { return { teams: [] }; }),
      ]);
      regions = rrs[0].regions || [];
      branches = rrs[1].branches || [];
      teams = rrs[2].teams || [];
      fillSelectInline("msg-region", regions, "כל המחוזות", "name");
      refreshBranchOptions("");
      refreshTeamOptions("", "");
      refDataLoaded = true;
    } catch (_e) {}
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

  function refreshBranchOptions(regionId) {
    var prev = document.getElementById("msg-branch").value;
    var list = branches.filter(function (b) {
      return !regionId || String(b.region_id) === String(regionId);
    });
    fillSelectInline("msg-branch", list, "כל הסניפים", "name");
    var stillValid = list.some(function (b) { return String(b.id) === prev; });
    document.getElementById("msg-branch").value = stillValid ? prev : "";
  }

  function refreshTeamOptions(regionId, branchId) {
    var prev = document.getElementById("msg-team").value;
    var list = teams.filter(function (t) {
      if (branchId) return String(t.branch_id) === String(branchId);
      if (regionId) {
        var branchIds = branches
          .filter(function (b) { return String(b.region_id) === String(regionId); })
          .map(function (b) { return String(b.id); });
        return branchIds.indexOf(String(t.branch_id)) !== -1;
      }
      return true;
    });
    fillSelectInline("msg-team", list, "כל הקבוצות", "team_name");
    var stillValid = list.some(function (t) { return String(t.id) === prev; });
    document.getElementById("msg-team").value = stillValid ? prev : "";
  }

  function currentFilter() {
    var rid = parseInt(document.getElementById("msg-region").value, 10) || null;
    var bid = parseInt(document.getElementById("msg-branch").value, 10) || null;
    var tid = parseInt(document.getElementById("msg-team").value, 10) || null;
    if (tid) return { type: "team", team_ids: [tid] };
    if (bid) return { type: "branch", branch_id: bid };
    if (rid) return { type: "region", region_id: rid };
    return { type: "all" };
  }

  function currentChannels() {
    var sms = $form.elements.ch_sms && $form.elements.ch_sms.checked;
    var email = $form.elements.ch_email && $form.elements.ch_email.checked;
    var ch = [];
    if (sms) ch.push("sms");
    if (email) ch.push("email");
    return ch;
  }

  async function refreshCount() {
    if (!$count) return;
    var rf = currentFilter();
    try {
      var data = await api("POST", "/org/api/messages/preview-recipients",
        { recipient_filter: rf });
      $count.textContent = String(data.count);
      var label = (
        rf.type === "team" ? "בקבוצה שנבחרה" :
        rf.type === "branch" ? "בסניף שנבחר" :
        rf.type === "region" ? "במחוז שנבחר" : "בארגון"
      );
      $countHint.textContent = data.count
        ? "ההודעה תישלח להורים " + label + "."
        : "אין נמענים תואמים.";
      $submit.disabled = data.count === 0;
    } catch (e) {
      $count.textContent = "—";
      $countHint.textContent = e.message || "";
      $submit.disabled = true;
    }
  }

  function scheduleCount() {
    if (countDebounce) clearTimeout(countDebounce);
    countDebounce = setTimeout(refreshCount, 250);
  }

  // --- Modal open ---
  async function openNew() {
    editingMessage = null;
    await ensureRefData();
    $form.reset();
    $form.elements.message_id.value = "";
    $form.elements.ch_sms.checked = true;
    $form.elements.ch_email.checked = true;
    // Reset schedule fields explicitly — $form.reset() should handle it
    // but the `hidden` attribute isn't a form value, so toggle it.
    var sched = document.getElementById("msg-schedule-toggle");
    var schedFields = document.getElementById("msg-schedule-fields");
    if (sched) sched.checked = false;
    if (schedFields) schedFields.hidden = true;
    refreshBranchOptions("");
    refreshTeamOptions("", "");
    $modalTitle.textContent = "הודעה חדשה";
    hideError($error);
    openModal($modal);
    refreshCount();
    setTimeout(function () { $form.elements.subject.focus(); }, 50);
  }

  async function openEdit(id) {
    await ensureRefData();
    try {
      var msg = await api("GET", "/org/api/messages/" + id);
      editingMessage = msg;
      $form.reset();
      $form.elements.message_id.value = String(msg.id);
      $form.elements.subject.value = msg.subject || "";
      $form.elements.body.value = msg.body || "";
      var channels = msg.delivery_channels || [];
      $form.elements.ch_sms.checked = channels.indexOf("sms") >= 0;
      $form.elements.ch_email.checked = channels.indexOf("email") >= 0;
      var rf = msg.recipient_filter || { type: "all" };
      document.getElementById("msg-region").value = rf.region_id ? String(rf.region_id) : "";
      refreshBranchOptions(rf.region_id || "");
      document.getElementById("msg-branch").value = rf.branch_id ? String(rf.branch_id) : "";
      refreshTeamOptions(rf.region_id || "", rf.branch_id || "");
      document.getElementById("msg-team").value = (rf.team_ids && rf.team_ids[0]) ? String(rf.team_ids[0]) : "";
      $modalTitle.textContent = "עריכת טיוטה";
      hideError($error);
      openModal($modal);
      refreshCount();
    } catch (e) {
      alert(e.message || "שגיאה בטעינת ההודעה");
    }
  }

  function buildBody(saveAsDraft) {
    var subject = ($form.elements.subject.value || "").trim();
    var body = $form.elements.body.value || "";
    var rf = currentFilter();
    var channels = currentChannels();
    var out = {
      subject: subject,
      body: body,
      recipient_filter: rf,
      delivery_channels: channels,
      save_as_draft: !!saveAsDraft,
    };
    // Schedule field — only attach if the user enabled it.
    var scheduleToggle = document.getElementById("msg-schedule-toggle");
    var scheduleAt = document.getElementById("msg-schedule-at");
    if (!saveAsDraft && scheduleToggle && scheduleToggle.checked && scheduleAt && scheduleAt.value) {
      // datetime-local is local time without timezone; convert to ISO
      // string so the server parses it consistently.
      var dt = new Date(scheduleAt.value);
      out.scheduled_at = dt.toISOString();
    }
    return out;
  }

  // Toggle the schedule fields when the checkbox flips.
  document.addEventListener("change", function (ev) {
    if (ev.target && ev.target.id === "msg-schedule-toggle") {
      var fields = document.getElementById("msg-schedule-fields");
      if (fields) fields.hidden = !ev.target.checked;
    }
  });

  $form.addEventListener("submit", async function (ev) {
    ev.preventDefault();
    hideError($error);
    var payload = buildBody(false);
    if (!payload.delivery_channels.length) {
      showError($error, "יש לבחור לפחות ערוץ אחד");
      return;
    }
    try {
      if (editingMessage && editingMessage.id) {
        // Edit existing draft + send: PATCH + POST /send
        await api("PATCH", "/org/api/messages/" + editingMessage.id, {
          subject: payload.subject,
          body: payload.body,
          recipient_filter: payload.recipient_filter,
          delivery_channels: payload.delivery_channels,
        });
        await api("POST", "/org/api/messages/" + editingMessage.id + "/send", {});
      } else {
        await api("POST", "/org/api/messages", payload);
      }
      closeModal($modal);
      load();
    } catch (e) {
      showError($error, e.message);
    }
  });

  $modal.addEventListener("click", async function (ev) {
    var actionEl = ev.target.closest("[data-action]");
    if (!actionEl) return;
    var action = actionEl.getAttribute("data-action");
    if (action === "close-message") {
      closeModal($modal);
    } else if (action === "save-draft") {
      ev.preventDefault();
      hideError($error);
      var payload = buildBody(true);
      try {
        if (editingMessage && editingMessage.id) {
          await api("PATCH", "/org/api/messages/" + editingMessage.id, {
            subject: payload.subject,
            body: payload.body,
            recipient_filter: payload.recipient_filter,
            delivery_channels: payload.delivery_channels,
          });
        } else {
          await api("POST", "/org/api/messages", payload);
        }
        closeModal($modal);
        load();
      } catch (e) {
        showError($error, e.message);
      }
    }
  });

  // Click-to-insert placeholder chips.
  $modal.addEventListener("click", function (ev) {
    var chip = ev.target.closest("[data-insert-placeholder]");
    if (!chip) return;
    ev.preventDefault();
    var token = "{{" + chip.getAttribute("data-insert-placeholder") + "}}";
    var ta = document.getElementById("msg-body");
    if (!ta) return;
    var start = ta.selectionStart != null ? ta.selectionStart : ta.value.length;
    var end = ta.selectionEnd != null ? ta.selectionEnd : ta.value.length;
    ta.value = ta.value.slice(0, start) + token + ta.value.slice(end);
    var newPos = start + token.length;
    ta.focus();
    ta.setSelectionRange(newPos, newPos);
  });

  // Change events for cascading + count refresh.
  $modal.addEventListener("change", function (ev) {
    if (!ev.target) return;
    if (ev.target.id === "msg-region") {
      refreshBranchOptions(ev.target.value);
      refreshTeamOptions(ev.target.value, "");
    } else if (ev.target.id === "msg-branch") {
      refreshTeamOptions(document.getElementById("msg-region").value, ev.target.value);
    }
    scheduleCount();
  });

  // --- Row actions ---
  document.addEventListener("click", function (ev) {
    var t = ev.target.closest("[data-edit-message], [data-send-draft], [data-delete-message]");
    if (!t) return;
    var id = parseInt(t.getAttribute("data-edit-message") || t.getAttribute("data-send-draft") || t.getAttribute("data-delete-message"), 10);
    if (!id) return;
    if (t.hasAttribute("data-edit-message")) {
      openEdit(id);
    } else if (t.hasAttribute("data-send-draft")) {
      sendDraft(id);
    } else if (t.hasAttribute("data-delete-message")) {
      pendingDelete = { id: id, name: t.getAttribute("data-name") || "טיוטה" };
      $confirmName.textContent = pendingDelete.name;
      hideError($confirmError);
      openModal($confirm);
    }
  });

  async function sendDraft(id) {
    try {
      await api("POST", "/org/api/messages/" + id + "/send", {});
      load();
    } catch (e) {
      alert(e.message || "שגיאה בשליחה");
    }
  }

  $confirm.addEventListener("click", async function (ev) {
    var btn = ev.target.closest("[data-action]");
    if (!btn) return;
    var action = btn.getAttribute("data-action");
    if (action === "close-confirm-msg") {
      closeModal($confirm);
      pendingDelete = null;
    } else if (action === "do-confirm-msg" && pendingDelete) {
      try {
        await api("DELETE", "/org/api/messages/" + pendingDelete.id);
        closeModal($confirm);
        pendingDelete = null;
        load();
      } catch (e) {
        showError($confirmError, e.message);
      }
    }
  });

  // --- New button ---
  document.addEventListener("click", function (ev) {
    var t = ev.target.closest("[data-action='open-new-message']");
    if (t) openNew();
  });

  // --- Boot ---
  load();
})();
