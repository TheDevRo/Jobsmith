// overlay.js — mounts the Jobsmith panel as an in-page docked iframe.
//
// Firefox can't open its native sidebar without a real user click (and that
// status doesn't propagate through messages), so on Apply Assist the
// background injects this file into the application tab and calls
// window.__jobsmithMountOverlay(panelUrl) with the extension's
// sidepanel.html?tabId=<id>&overlay=1 URL. The panel then auto-appears
// docked to the right edge of the page — same UX as the old isolated-mode
// sidebar (drag-resize, collapse tab, close button, SPA re-mount).
//
// Runs in the content-script isolated world; idempotent per page.

(function () {
  const ext = (typeof browser !== "undefined") ? browser : chrome;
  const HOST_ID = "__jobsmith-assist__";
  const WIDTH_KEY = "__jobsmith_panel_width__";
  const COLLAPSED_W = "30px";

  function sp(el, prop, val) { el.style.setProperty(prop, val, "important"); }

  window.__jobsmithMountOverlay = function mountOverlay(panelUrl) {
    if (document.getElementById(HOST_ID)) return;
    if (!document.body) {
      document.addEventListener("DOMContentLoaded", () => mountOverlay(panelUrl), { once: true });
      return;
    }

    let width = "400px";
    try { width = localStorage.getItem(WIDTH_KEY) || width; } catch (_) {}
    let collapsed = false;

    // --- Container (fixed, right edge, above everything) ---
    const host = document.createElement("div");
    host.id = HOST_ID;
    sp(host, "position", "fixed");
    sp(host, "top", "0");
    sp(host, "right", "0");
    sp(host, "height", "100vh");
    sp(host, "width", width);
    sp(host, "z-index", "2147483647");
    sp(host, "display", "flex");
    sp(host, "flex-direction", "row");
    sp(host, "margin", "0");
    sp(host, "padding", "0");
    sp(host, "background", "transparent");
    sp(host, "box-shadow", "-6px 0 24px rgba(0,0,0,0.35)");
    sp(host, "transition", "width 160ms ease");

    // --- Rail: drag-resize area + collapse / close buttons ---
    const rail = document.createElement("div");
    sp(rail, "width", "14px");
    sp(rail, "flex", "0 0 14px");
    sp(rail, "cursor", "ew-resize");
    sp(rail, "background", "#181828");
    sp(rail, "border-right", "1px solid #2a2a3e");
    sp(rail, "display", "flex");
    sp(rail, "flex-direction", "column");
    sp(rail, "align-items", "center");
    sp(rail, "gap", "6px");
    sp(rail, "padding", "8px 0");

    function railBtn(text, title) {
      const b = document.createElement("div");
      b.textContent = text;
      b.title = title;
      sp(b, "cursor", "pointer");
      sp(b, "color", "#8b8fa8");
      sp(b, "font", "12px/1 -apple-system, sans-serif");
      sp(b, "user-select", "none");
      sp(b, "padding", "2px 0");
      b.addEventListener("mouseenter", () => sp(b, "color", "#e4e6f1"));
      b.addEventListener("mouseleave", () => sp(b, "color", "#8b8fa8"));
      return b;
    }
    const collapseBtn = railBtn("▶", "Collapse panel");
    const closeBtn = railBtn("✕", "Close panel for this application");
    rail.appendChild(collapseBtn);
    rail.appendChild(closeBtn);

    // --- Panel iframe (extension page — full API access inside) ---
    const iframe = document.createElement("iframe");
    iframe.src = panelUrl;
    iframe.allow = "clipboard-write";
    sp(iframe, "flex", "1 1 auto");
    sp(iframe, "height", "100%");
    sp(iframe, "border", "none");
    sp(iframe, "margin", "0");
    sp(iframe, "padding", "0");
    sp(iframe, "background", "#0f0f1a");

    host.appendChild(rail);
    host.appendChild(iframe);
    document.body.appendChild(host);

    // --- Collapse / expand ---
    collapseBtn.addEventListener("click", (e) => {
      e.stopPropagation();
      collapsed = !collapsed;
      if (collapsed) {
        sp(host, "width", COLLAPSED_W);
        sp(iframe, "display", "none");
        collapseBtn.textContent = "◀";
        collapseBtn.title = "Expand panel";
      } else {
        sp(host, "width", width);
        sp(iframe, "display", "block");
        collapseBtn.textContent = "▶";
        collapseBtn.title = "Collapse panel";
      }
    });

    // --- Close: tear down and tell the background to stop re-injecting ---
    closeBtn.addEventListener("click", (e) => {
      e.stopPropagation();
      observer.disconnect();
      host.remove();
      try { ext.runtime.sendMessage({ type: "assist-overlay-closed" }); } catch (_) {}
    });

    // --- Drag-resize from the rail ---
    let dragging = false, startX = 0, startW = parseInt(width, 10) || 400;
    rail.addEventListener("mousedown", (e) => {
      if (collapsed || e.target === collapseBtn || e.target === closeBtn) return;
      dragging = true;
      startX = e.clientX;
      startW = parseInt(host.style.getPropertyValue("width"), 10) || startW;
      sp(document.body, "user-select", "none");
      e.preventDefault();
    });
    document.addEventListener("mousemove", (e) => {
      if (!dragging) return;
      const w = Math.max(300, Math.min(640, startW + (startX - e.clientX)));
      width = w + "px";
      sp(host, "width", width);
    });
    document.addEventListener("mouseup", () => {
      if (!dragging) return;
      dragging = false;
      document.body.style.removeProperty("user-select");
      try { localStorage.setItem(WIDTH_KEY, width); } catch (_) {}
    });

    // --- Re-mount if an SPA re-render removes the host ---
    const observer = new MutationObserver(() => {
      if (!document.getElementById(HOST_ID)) {
        observer.disconnect();
        mountOverlay(panelUrl);
      }
    });
    observer.observe(document.body, { childList: true });
  };
})();

// Final expression must be structured-clonable for Firefox's executeScript.
true;
