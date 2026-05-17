/* /admin/orgs/wizard — 4-step Vanilla JS wizard.
 *
 * Per-step client-side validation (HTML5 + a slug-availability preflight on
 * step 1 → step 2 transition). Final submit posts a structured JSON envelope
 * to /admin/api/orgs/wizard/commit; on 201, redirect to /admin/orgs/{id}.
 */

(function () {
  "use strict";

  var STEP_COUNT = 4;
  var currentStep = 1;

  var $form = document.getElementById("wizard-form");
  var $prev = document.getElementById("prev-btn");
  var $next = document.getElementById("next-btn");
  var $submit = document.getElementById("submit-btn");

  function $$(sel) { return document.querySelectorAll(sel); }

  function showStep(n) {
    $$('.admin-wizard-pane').forEach(function (el) {
      el.classList.toggle("hidden", parseInt(el.dataset.step, 10) !== n);
    });
    $$('.admin-wizard-step').forEach(function (el) {
      var s = parseInt(el.dataset.stepIndicator, 10);
      el.classList.toggle("is-active", s === n);
      el.classList.toggle("is-done", s < n);
    });
    $prev.disabled = (n === 1);
    $next.classList.toggle("hidden", n === STEP_COUNT);
    $submit.classList.toggle("hidden", n !== STEP_COUNT);
    currentStep = n;
  }

  function showStepError(stepEl, message) {
    var box = stepEl.querySelector("[data-step-error]");
    if (!box) return;
    if (!message) {
      box.classList.add("hidden");
      box.textContent = "";
    } else {
      box.classList.remove("hidden");
      box.textContent = message;
    }
  }

  function currentPane() { return document.querySelector('.admin-wizard-pane[data-step="' + currentStep + '"]'); }

  function validateCurrentStep() {
    var pane = currentPane();
    showStepError(pane, "");
    // Trigger native HTML5 validity for visible required fields in this pane only.
    var fields = pane.querySelectorAll("input, select, textarea");
    for (var i = 0; i < fields.length; i++) {
      var f = fields[i];
      if (!f.checkValidity()) {
        f.reportValidity();
        return false;
      }
    }
    return true;
  }

  // --- Preflight slug + subdomain on step transition (1→2 and 2→3) ---
  async function preflightIfNeeded() {
    if (currentStep !== 1 && currentStep !== 2) return true;
    var slug = ($form.elements.slug.value || "").trim().toLowerCase();
    var subdomain = ($form.elements.subdomain.value || "").trim().toLowerCase() || null;
    try {
      var r = await fetch("/admin/api/orgs/wizard/preflight", {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          "Accept": "application/json",
          "X-Requested-With": "XMLHttpRequest",
        },
        body: JSON.stringify({ slug: slug, subdomain: subdomain }),
      });
      if (!r.ok) {
        var errData = null;
        try { errData = await r.json(); } catch (_e) {}
        showStepError(currentPane(), (errData && errData.detail) || ("Preflight failed (" + r.status + ")"));
        return false;
      }
      var d = await r.json();
      if (!d.slug_available) {
        showStepError(currentPane(), "Slug '" + slug + "' is already taken.");
        return false;
      }
      if (!d.subdomain_available) {
        showStepError(currentPane(), "Subdomain '" + subdomain + "' is already taken.");
        return false;
      }
      return true;
    } catch (e) {
      showStepError(currentPane(), "Network error during preflight.");
      return false;
    }
  }

  // --- Build the structured commit payload from the form ---
  function buildCommitPayload() {
    var f = $form.elements;
    var monthlyShekel = parseInt(f.monthly_fee_shekel.value || "0", 10);
    var setupShekel = parseInt(f.setup_fee_shekel.value || "0", 10);
    var contractStart = f.contract_start.value || new Date().toISOString().slice(0, 10);

    return {
      step1: {
        name: f.name.value.trim(),
        legal_name: f.legal_name.value.trim() || null,
        tax_id: f.tax_id.value.trim() || null,
        address: f.address.value.trim() || null,
        slug: f.slug.value.trim().toLowerCase(),
      },
      step2: {
        logo_url: f.logo_url.value.trim() || null,
        primary_color: f.primary_color.value || "#2563EB",
        subdomain: (f.subdomain.value.trim().toLowerCase() || null),
      },
      step3: {
        structure_type: f.structure_type.value,
        monthly_fee_cents: monthlyShekel * 100,
        setup_fee_cents: setupShekel * 100,
        trial_days: parseInt(f.trial_days.value || "0", 10),
        contract_start: contractStart,
        status: f.status.value,
      },
      step4: {
        full_name: f.full_name.value.trim(),
        email: f.email.value.trim().toLowerCase(),
        phone: f.phone.value.trim() || null,
        role: f.role.value,
        send_invite_immediately: f.send_invite_immediately.checked,
      },
    };
  }

  // --- Submit handler ---
  $form.addEventListener("submit", async function (ev) {
    ev.preventDefault();
    if (!validateCurrentStep()) return;

    $submit.disabled = true;
    $submit.textContent = "Creating…";

    try {
      var r = await fetch("/admin/api/orgs/wizard/commit", {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          "Accept": "application/json",
          "X-Requested-With": "XMLHttpRequest",
        },
        body: JSON.stringify(buildCommitPayload()),
      });
      if (!r.ok) {
        var errData = null;
        try { errData = await r.json(); } catch (_e) {}
        var msg = (errData && (errData.detail || errData.message)) || ("Commit failed (" + r.status + ")");
        showStepError(currentPane(), msg);
        $submit.disabled = false;
        $submit.textContent = "Create organization";
        return;
      }
      var data = await r.json();
      // Success. If an invite was minted, show the credentials in a
      // confirmation overlay BEFORE redirecting — this is the only chance
      // for the admin to copy the link + code if email delivery is
      // misconfigured (or if they typed a fake address during setup).
      // No invite → straight redirect like before.
      if (data.invite_url || data.invite_short_code) {
        showInviteCredentials(data);
      } else {
        window.location.href = "/admin/orgs/" + data.org_id;
      }
    } catch (e) {
      showStepError(currentPane(), "Network error: " + (e.message || e));
      $submit.disabled = false;
      $submit.textContent = "Create organization";
    }
  });

  // --- Post-commit credentials overlay ---
  // Built with explicit DOM API (no innerHTML) so untrusted-looking server
  // strings (email / code) can never be parsed as HTML — defense against
  // any future regression that lets non-app data into these fields.
  function _el(tag, props, children) {
    var n = document.createElement(tag);
    if (props) {
      if (props.style) n.style.cssText = props.style;
      if (props.text != null) n.textContent = props.text;
      if (props.id) n.id = props.id;
      if (props.type) n.type = props.type;
      if (props.value != null) n.value = props.value;
      if (props.readOnly) n.readOnly = true;
      if (props.dataset) {
        Object.keys(props.dataset).forEach(function (k) { n.dataset[k] = props.dataset[k]; });
      }
    }
    if (children) children.forEach(function (c) { if (c) n.appendChild(c); });
    return n;
  }

  function _credentialBox(labelText, fieldId, fieldValue, accentColor) {
    var input = _el("input", {
      type: "text", id: fieldId, value: fieldValue, readOnly: true,
      style: "flex:1;background:transparent;border:none;color:" + accentColor +
        ";font-family:monospace;font-size:" + (fieldId === "inviteCodeField" ? "22px;letter-spacing:2px;font-weight:bold" : "13px") +
        ";outline:none;",
    });
    var copyBtn = _el("button", {
      type: "button", text: "Copy",
      dataset: { copy: fieldId },
      style: "background:#ff6b35;color:white;border:none;padding:6px 12px;border-radius:6px;font-size:12px;cursor:pointer;",
    });
    return _el("div", {
      style: "background:#0d1117;border:1px solid #2a3142;border-radius:8px;padding:16px;margin-bottom:12px;",
    }, [
      _el("div", { text: labelText, style: "font-size:11px;text-transform:uppercase;opacity:0.6;margin-bottom:6px;" }),
      _el("div", { style: "display:flex;gap:8px;align-items:center;" }, [input, copyBtn]),
    ]);
  }

  function showInviteCredentials(data) {
    var prettyCode = data.invite_short_code
      ? (data.invite_short_code.length >= 8
          ? data.invite_short_code.slice(0, 4) + "-" + data.invite_short_code.slice(4)
          : data.invite_short_code)
      : "";

    var overlay = _el("div", {
      style: "position:fixed;inset:0;background:rgba(0,0,0,0.75);z-index:9999;display:flex;align-items:center;justify-content:center;padding:24px;",
    });

    // Status message: if email sent, name the recipient; otherwise be loud
    // about manual delivery being the only path.
    var statusEl = _el("p", {
      style: "margin:0 0 20px 0;opacity:0.85;font-size:14px;",
    });
    if (data.ceo_invite_email_sent) {
      statusEl.appendChild(document.createTextNode("An invite email was sent to "));
      statusEl.appendChild(_el("strong", { text: data.ceo_email || "the CEO" }));
      statusEl.appendChild(document.createTextNode("."));
    } else {
      statusEl.appendChild(_el("strong", { text: "Email NOT sent." }));
      statusEl.appendChild(document.createTextNode(" Share the credentials below manually."));
    }

    var goBtn = _el("button", {
      id: "goToOrgBtn", type: "button", text: "Go to organization page →",
      style: "background:#ff6b35;color:white;border:none;padding:10px 24px;border-radius:8px;font-size:14px;font-weight:600;cursor:pointer;width:100%;",
    });

    var card = _el("div", {
      style: "background:#1a1f2e;border:1px solid #2a3142;border-radius:12px;padding:32px;max-width:560px;width:100%;color:#e6edf3;",
    }, [
      _el("h2", { text: "Organization created", style: "margin:0 0 12px 0;color:#3fb950;" }),
      statusEl,
      _credentialBox("Join Link", "inviteLinkField", data.invite_url || "", "#4da6ff"),
      _credentialBox("8-Digit Code", "inviteCodeField", prettyCode, "#ff6b35"),
      _el("p", {
        text: "Tip: you can always re-read these on the org page — they stay available until the invite is accepted.",
        style: "margin:0 0 20px 0;font-size:13px;opacity:0.7;",
      }),
      goBtn,
    ]);

    overlay.appendChild(card);
    document.body.appendChild(overlay);

    // Wire up copy buttons (Clipboard API; legacy fallback for non-HTTPS dev).
    card.querySelectorAll("[data-copy]").forEach(function (btn) {
      btn.addEventListener("click", function () {
        var input = document.getElementById(btn.dataset.copy);
        if (!input) return;
        input.select();
        var ok = false;
        if (navigator.clipboard && navigator.clipboard.writeText) {
          navigator.clipboard.writeText(input.value);
          ok = true;
        } else {
          try { ok = document.execCommand("copy"); } catch (_) {}
        }
        if (ok) {
          var orig = btn.textContent;
          btn.textContent = "Copied!";
          setTimeout(function () { btn.textContent = orig; }, 1200);
        }
      });
    });

    goBtn.addEventListener("click", function () {
      window.location.href = "/admin/orgs/" + data.org_id;
    });
  }

  // --- Next / Prev buttons ---
  $next.addEventListener("click", async function () {
    if (!validateCurrentStep()) return;
    var ok = await preflightIfNeeded();
    if (!ok) return;
    showStep(currentStep + 1);
  });
  $prev.addEventListener("click", function () {
    showStepError(currentPane(), "");
    showStep(currentStep - 1);
  });

  // Default contract_start to today.
  var $contractStart = document.querySelector('input[name="contract_start"]');
  if ($contractStart && !$contractStart.value) {
    $contractStart.value = new Date().toISOString().slice(0, 10);
  }

  showStep(1);
})();
