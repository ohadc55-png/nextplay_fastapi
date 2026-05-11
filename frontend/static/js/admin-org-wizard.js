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
      // Success — redirect to the new org's detail page.
      window.location.href = "/admin/orgs/" + data.org_id;
    } catch (e) {
      showStepError(currentPane(), "Network error: " + (e.message || e));
      $submit.disabled = false;
      $submit.textContent = "Create organization";
    }
  });

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
