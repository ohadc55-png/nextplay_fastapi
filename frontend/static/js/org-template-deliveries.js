/* /org/document-templates/{id}/deliveries — Phase 2.5 visibility page.
 *
 * Loads stats + per-recipient rows. Filter by region/branch/team/status with
 * cascading dropdowns. CSV export client-side from the loaded rows.
 */

(function () {
  "use strict";

  var templateId = window.ORG_TEMPLATE_ID;
  if (!templateId) return;

  var ctx = window.ORG_CTX || {};
  var STATUS_LABELS = {
    NOT_OPENED: "לא נפתח",
    OPENED: "נפתח",
    FILLED: "מולא",
    SIGNED: "נחתם",
    EXPIRED: "פג תוקף",
    DECLINED: "נדחה",
  };
  var STATUS_KIND = {
    SIGNED: "org-pill--ok",
    NOT_OPENED: "org-pill--muted",
    EXPIRED: "org-pill--err",
    DECLINED: "org-pill--err",
    OPENED: "org-pill--warn",
    FILLED: "org-pill--warn",
  };

  var $rows = document.querySelector("[data-deliveries-rows]");
  var $name = document.querySelector("[data-template-name]");
  var $total = document.querySelector("[data-stat-total]");
  var $signed = document.querySelector("[data-stat-signed]");
  var $pending = document.querySelector("[data-stat-pending]");
  var $failed = document.querySelector("[data-stat-failed]");
  var $bar = document.querySelector("[data-progress-bar]");
  var $region = document.getElementById("dlv-region");
  var $branch = document.getElementById("dlv-branch");
  var $team = document.getElementById("dlv-team");
  var $status = document.getElementById("dlv-status");

  var regions = [];
  var branches = [];
  var teams = [];
  var lastRows = [];

  async function api(method, url) {
    var r = await fetch(url, { method: method, headers: { "Accept": "application/json", "X-Requested-With": "XMLHttpRequest" } });
    if (!r.ok) {
      var msg = "שגיאה " + r.status;
      try { var d = await r.json(); if (d.detail) msg = d.detail; } catch (_e) {}
      throw new Error(msg);
    }
    return r.json();
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
  function fmt(iso) {
    if (!iso) return "—";
    try {
      var d = new Date(iso);
      return d.toLocaleDateString("he-IL") + " " + d.toLocaleTimeString("he-IL", { hour: "2-digit", minute: "2-digit" });
    } catch (_e) { return iso; }
  }

  function fillSelect(sel, items, blank, labelKey) {
    var prev = sel.value;
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
    sel.value = prev;
  }

  function refreshBranchOptions() {
    var rid = $region.value;
    var list = branches.filter(function (b) {
      return !rid || String(b.region_id) === String(rid);
    });
    fillSelect($branch, list, "כל הסניפים", "name");
  }

  function refreshTeamOptions() {
    var rid = $region.value;
    var bid = $branch.value;
    var list = teams.filter(function (t) {
      if (bid) return String(t.branch_id) === String(bid);
      if (rid) {
        var branchIds = branches
          .filter(function (b) { return String(b.region_id) === String(rid); })
          .map(function (b) { return String(b.id); });
        return branchIds.indexOf(String(t.branch_id)) !== -1;
      }
      return true;
    });
    fillSelect($team, list, "כל הקבוצות", "team_name");
  }

  async function loadRefData() {
    try {
      var rrs = await Promise.all([
        api("GET", "/org/api/regions").catch(function () { return { regions: [] }; }),
        api("GET", "/org/api/branches").catch(function () { return { branches: [] }; }),
        api("GET", "/org/api/teams").catch(function () { return { teams: [] }; }),
      ]);
      regions = rrs[0].regions || [];
      branches = rrs[1].branches || [];
      teams = rrs[2].teams || [];
      fillSelect($region, regions, "כל המחוזות", "name");
      refreshBranchOptions();
      refreshTeamOptions();
    } catch (_e) {}
  }

  async function load() {
    var qs = [];
    if ($region.value) qs.push("region_id=" + $region.value);
    if ($branch.value) qs.push("branch_id=" + $branch.value);
    if ($team.value) qs.push("team_id=" + $team.value);
    if ($status.value) qs.push("status=" + $status.value);
    var url = "/org/api/document-templates/" + templateId + "/deliveries"
      + (qs.length ? "?" + qs.join("&") : "");
    try {
      var data = await api("GET", url);
      if (data.template) $name.textContent = data.template.name;
      renderStats(data.stats || {});
      lastRows = data.deliveries || [];
      renderRows(lastRows);
    } catch (e) {
      $rows.replaceChildren(el("tr", null, [
        el("td", { className: "org-table-empty", text: e.message, attrs: { colspan: "8" } })
      ]));
    }
  }

  function renderStats(s) {
    var total = s.total || 0;
    var signed = s.signed || 0;
    var pending = (s.not_opened || 0) + (s.opened || 0);
    var failed = (s.expired || 0) + (s.declined || 0) + (s.delivery_failed || 0);
    $total.textContent = String(total);
    $signed.textContent = String(signed);
    $pending.textContent = String(pending);
    $failed.textContent = String(failed);
    var pct = total > 0 ? Math.round((signed / total) * 100) : 0;
    $bar.style.width = pct + "%";
  }

  function renderRows(rows) {
    if (!rows.length) {
      $rows.replaceChildren(el("tr", null, [
        el("td", { className: "org-table-empty", text: "אין נמענים תואמים לפילטר.", attrs: { colspan: "8" } })
      ]));
      return;
    }
    $rows.replaceChildren.apply(
      $rows,
      rows.map(function (r) {
        var player = el("td", { text: "—" });
        // We don't load player names individually here — use recipient_name as fallback.
        player.textContent = r.recipient_name || "—";

        var parent = el("td", null);
        parent.appendChild(el("div", { text: r.recipient_name || "—" }));
        if (r.recipient_email) {
          parent.appendChild(el("div", { className: "org-text-sm org-text-muted", text: r.recipient_email }));
        }

        var ch = el("td", { className: "org-text-sm", text: r.channel_used || "—" });
        var sent = el("td", { className: "org-text-sm org-text-muted", text: fmt(r.sent_at) });

        var statusCell = el("td", null, [pill(
          STATUS_LABELS[r.document_status] || r.document_status,
          STATUS_KIND[r.document_status] || ""
        )]);
        if (r.delivery_status === "FAILED") {
          statusCell.appendChild(el("div", { className: "org-text-sm org-text-muted", text: "שליחה נכשלה" }));
        }

        var signed = el("td", { className: "org-text-sm", text: fmt(r.signed_at) });
        var expires = el("td", { className: "org-text-sm org-text-muted", text: fmt(r.expires_at) });

        var pdfCell = el("td", null);
        if (r.final_pdf_url && r.document_status === "SIGNED") {
          var a = document.createElement("a");
          a.href = "/sign/download?key=" + encodeURIComponent(r.final_pdf_url);
          a.textContent = "PDF";
          a.className = "org-btn org-btn--ghost org-btn--sm";
          a.target = "_blank";
          pdfCell.appendChild(a);
        }

        return el("tr", null, [player, parent, ch, sent, statusCell, signed, expires, pdfCell]);
      })
    );
  }

  function exportCsv() {
    if (!lastRows.length) return;
    var headers = ["שחקן", "הורה", "אימייל", "ערוץ", "סטטוס", "נשלח", "נחתם", "פג תוקף"];
    var lines = [headers.join(",")];
    lastRows.forEach(function (r) {
      var row = [
        r.recipient_name || "",
        r.recipient_name || "",
        r.recipient_email || "",
        r.channel_used || "",
        STATUS_LABELS[r.document_status] || r.document_status,
        r.sent_at || "",
        r.signed_at || "",
        r.expires_at || "",
      ].map(function (v) { return '"' + String(v).replace(/"/g, '""') + '"'; });
      lines.push(row.join(","));
    });
    var blob = new Blob(["﻿" + lines.join("\n")], { type: "text/csv;charset=utf-8" });
    var url = URL.createObjectURL(blob);
    var a = document.createElement("a");
    a.href = url;
    a.download = "deliveries_template_" + templateId + ".csv";
    document.body.appendChild(a);
    a.click();
    a.remove();
    URL.revokeObjectURL(url);
  }

  $region.addEventListener("change", function () {
    refreshBranchOptions();
    refreshTeamOptions();
    load();
  });
  $branch.addEventListener("change", function () {
    refreshTeamOptions();
    load();
  });
  $team.addEventListener("change", load);
  $status.addEventListener("change", load);

  document.addEventListener("click", function (ev) {
    var t = ev.target.closest("[data-action='export-csv']");
    if (t) exportCsv();
  });

  (async function () {
    await loadRefData();
    load();
  })();
})();
