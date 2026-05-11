/* ============================================================================
 * NEXTPLAY Enterprise — /org/* JS
 * ----------------------------------------------------------------------------
 * - Patches window.fetch to add X-Requested-With on /org/api/* (CSRF helper).
 *   Mirrors the /api/* patch in auth.js but for the org session surface.
 * - Sidebar mobile toggle.
 * - Header user-menu dropdown.
 * - Toast helper (lightweight).
 * No build step — vanilla ES5+ that runs in every modern browser.
 * ========================================================================= */

(function () {
  "use strict";

  // -------------------------------------------------------------------------
  // 1. fetch CSRF patch for /org/api/*
  // -------------------------------------------------------------------------
  // The CSRFMiddleware accepts EITHER X-Requested-With:XMLHttpRequest OR a
  // JSON content-type. Setting the header here keeps form-encoded requests
  // (e.g., logo upload as multipart) safe from CSRF rejection too.
  var origFetch = window.fetch.bind(window);
  window.fetch = function (input, init) {
    init = init || {};
    var url = typeof input === "string" ? input : (input && input.url) || "";
    if (typeof url === "string" && url.indexOf("/org/api/") !== -1) {
      var headers = new Headers(init.headers || (input && input.headers) || {});
      if (!headers.has("X-Requested-With")) {
        headers.set("X-Requested-With", "XMLHttpRequest");
      }
      init.headers = headers;
      if (typeof init.credentials === "undefined") {
        init.credentials = "same-origin";
      }
    }
    return origFetch(input, init);
  };

  // -------------------------------------------------------------------------
  // 2. Sidebar mobile toggle
  // -------------------------------------------------------------------------
  function initSidebar() {
    var toggle = document.querySelector(".org-sidebar-toggle");
    var sidebar = document.querySelector(".org-sidebar");
    var backdrop = document.querySelector(".org-sidebar-backdrop");
    if (!toggle || !sidebar) return;

    function open() {
      sidebar.classList.add("is-open");
      if (backdrop) backdrop.classList.add("is-visible");
      toggle.setAttribute("aria-expanded", "true");
    }
    function close() {
      sidebar.classList.remove("is-open");
      if (backdrop) backdrop.classList.remove("is-visible");
      toggle.setAttribute("aria-expanded", "false");
    }

    toggle.addEventListener("click", function () {
      if (sidebar.classList.contains("is-open")) close();
      else open();
    });
    if (backdrop) backdrop.addEventListener("click", close);
    document.addEventListener("keydown", function (e) {
      if (e.key === "Escape") close();
    });
  }

  // -------------------------------------------------------------------------
  // 3. Header user-menu dropdown
  // -------------------------------------------------------------------------
  function initUserMenu() {
    var btn = document.querySelector("[data-org-user-menu]");
    var menu = document.querySelector("[data-org-user-menu-list]");
    if (!btn || !menu) return;

    function toggle() {
      var open = menu.classList.toggle("is-open");
      btn.setAttribute("aria-expanded", String(open));
    }
    function close() {
      menu.classList.remove("is-open");
      btn.setAttribute("aria-expanded", "false");
    }
    btn.addEventListener("click", function (e) {
      e.stopPropagation();
      toggle();
    });
    document.addEventListener("click", function (e) {
      if (!menu.contains(e.target) && e.target !== btn) close();
    });
    document.addEventListener("keydown", function (e) {
      if (e.key === "Escape") close();
    });
  }

  // -------------------------------------------------------------------------
  // 4. Coming-soon toast
  // -------------------------------------------------------------------------
  function initComingSoonClicks() {
    document.addEventListener("click", function (e) {
      var target = e.target;
      while (target && target !== document.body) {
        if (target.classList && target.classList.contains("is-disabled") &&
            target.classList.contains("org-nav-item")) {
          e.preventDefault();
          showToast("בקרוב — הפיצ'ר עדיין בפיתוח");
          return;
        }
        target = target.parentNode;
      }
    });
  }

  // -------------------------------------------------------------------------
  // 5. Toast helper — exposed as window.OrgToast
  // -------------------------------------------------------------------------
  function ensureToastRoot() {
    var root = document.querySelector(".org-toast-root");
    if (!root) {
      root = document.createElement("div");
      root.className = "org-toast-root";
      root.style.cssText = [
        "position:fixed",
        "inset-block-end:24px",
        "inset-inline-end:24px",
        "z-index:1000",
        "display:flex",
        "flex-direction:column",
        "gap:8px",
      ].join(";");
      document.body.appendChild(root);
    }
    return root;
  }

  function showToast(message, kind) {
    kind = kind || "info";
    var root = ensureToastRoot();
    var t = document.createElement("div");
    t.className = "org-toast";
    var bg = "#1F2937";
    if (kind === "success") bg = "#10B981";
    else if (kind === "danger") bg = "#EF4444";
    else if (kind === "warning") bg = "#F59E0B";
    t.style.cssText = [
      "background:" + bg,
      "color:#FFFFFF",
      "padding:12px 16px",
      "border-radius:8px",
      "font-size:14px",
      "box-shadow:0 10px 15px -3px rgba(0,0,0,0.1)",
      "min-width:240px",
      "max-width:360px",
      "opacity:0",
      "transform:translateY(10px)",
      "transition:opacity 200ms ease, transform 200ms ease",
    ].join(";");
    t.textContent = message;
    root.appendChild(t);
    requestAnimationFrame(function () {
      t.style.opacity = "1";
      t.style.transform = "translateY(0)";
    });
    setTimeout(function () {
      t.style.opacity = "0";
      t.style.transform = "translateY(10px)";
      setTimeout(function () { if (t.parentNode) t.parentNode.removeChild(t); }, 200);
    }, 2400);
  }

  window.OrgToast = { show: showToast };

  // -------------------------------------------------------------------------
  // Init on DOMContentLoaded
  // -------------------------------------------------------------------------
  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", boot);
  } else {
    boot();
  }
  function boot() {
    initSidebar();
    initUserMenu();
    initComingSoonClicks();
  }
})();
