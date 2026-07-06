// dropcatch.js — makes dragging the Resume/Cover-Letter tiles out of the
// panel onto the page actually attach the file.
//
// Browsers don't reliably carry a programmatically-created File across a
// drag from an extension document into a web page (Firefox delivers an
// empty drop), so the drag is treated as a gesture only: while a tile drag
// is in flight the panel arms this catcher in every frame; on drop we
// intercept the event before the page sees it, pick the file input nearest
// the cursor, stamp it, and message the panel — which then attaches the
// real bytes through the same code path autofill uses.
//
// Runs in the content-script isolated world; idempotent.

(function () {
  if (window.__jobsmithArmDropCatch) return;

  const STYLE_ID = "__jobsmith-dropcatch-style__";
  let armed = null;   // { kind, timer }
  let fidCounter = 0;

  function fileInputs() {
    return Array.from(document.querySelectorAll('input[type="file"]'));
  }

  function wrapperFor(input) {
    return input.closest('[data-test-id*="drop"], .dropzone, [class*="drop"], label')
      || input.parentElement || input;
  }

  function highlight(on) {
    const prev = document.getElementById(STYLE_ID);
    if (prev) prev.remove();
    if (!on) return;
    fileInputs().forEach((inp) => {
      try { wrapperFor(inp).setAttribute("data-jobsmith-dropzone", "1"); } catch (_) {}
    });
    const style = document.createElement("style");
    style.id = STYLE_ID;
    style.textContent =
      '[data-jobsmith-dropzone="1"] { outline: 2px dashed #8b7cff !important; outline-offset: 3px !important; }';
    (document.head || document.documentElement).appendChild(style);
  }

  function onDragOver(e) {
    if (!armed) return;
    e.preventDefault();  // allow dropping anywhere while our drag is armed
    if (e.dataTransfer) e.dataTransfer.dropEffect = "copy";
  }

  // What the input is *for*, from its own attributes, its label, and its
  // wrapper's visible text.
  function haystackFor(input) {
    let labelText = "";
    try {
      if (input.id) {
        const l = document.querySelector('label[for="' + CSS.escape(input.id) + '"]');
        if (l) labelText = l.textContent || "";
      }
    } catch (_) {}
    const w = wrapperFor(input);
    return [
      input.id || "", input.name || "", input.getAttribute("aria-label") || "",
      labelText, (w.textContent || "").slice(0, 200),
    ].join(" ").toLowerCase();
  }

  const KIND_RE = {
    resume: /resume|\bcv\b|curriculum/,
    cover_letter: /cover/,
  };
  const CLASH_RE = {
    resume: /cover/,
    cover_letter: /resume|\bcv\b|curriculum/,
  };

  function pickInput(x, y, kind) {
    const inputs = fileInputs();
    if (!inputs.length) return null;
    if (inputs.length === 1) return inputs[0];
    // Kind is a hard tier, distance only breaks ties: dragging a resume must
    // never land in the cover-letter input just because its (possibly
    // hidden, zero-rect) sibling sat closer to the cursor.
    //   1. inputs labeled for this kind
    //   2. inputs not labeled for the OTHER kind
    //   3. anything
    const wantRe = KIND_RE[kind];
    const clashRe = CLASH_RE[kind];
    const hays = inputs.map(haystackFor);
    let pool = inputs.filter((_, i) => wantRe && wantRe.test(hays[i]));
    if (!pool.length) pool = inputs.filter((_, i) => !(clashRe && clashRe.test(hays[i])));
    if (!pool.length) pool = inputs;
    let best = null, bestD = Infinity;
    for (const inp of pool) {
      const r = wrapperFor(inp).getBoundingClientRect();
      const cx = Math.max(r.left, Math.min(x, r.right));
      const cy = Math.max(r.top, Math.min(y, r.bottom));
      const d = Math.hypot(x - cx, y - cy);
      if (d < bestD) { bestD = d; best = inp; }
    }
    return best;
  }

  // Result of the last intercepted drop. The PANEL pulls this on dragend via
  // executeScript — a push via runtime.sendMessage is not reliably delivered
  // to an extension page iframed inside a web page (Firefox), and pulling
  // also removes the drop-vs-dragend ordering race by design.
  let dropResult = null;

  function onDrop(e) {
    if (!armed) return;
    const kind = armed.kind;
    // Best effort to keep the page's own dropzone handler (which would see
    // an empty file list) out of the way. preventDefault is shared across
    // script worlds; stop*Propagation may not silence main-world listeners.
    e.preventDefault();
    if (e.stopImmediatePropagation) e.stopImmediatePropagation();
    e.stopPropagation();
    disarm();
    const input = pickInput(e.clientX || 0, e.clientY || 0, kind);
    if (!input) {
      dropResult = { kind, ok: false, reason: "no file input on this page" };
      return;
    }
    let fid = input.getAttribute("data-jobsmith-fid");
    if (!fid) {
      fid = "dropzone_" + (++fidCounter) + "_" + Math.floor(Math.random() * 1e6);
      try { input.setAttribute("data-jobsmith-fid", fid); } catch (_) {}
    }
    dropResult = { kind, ok: true, fid };
  }

  function disarm() {
    if (!armed) return;
    clearTimeout(armed.timer);
    armed = null;
    highlight(false);
    document.removeEventListener("dragover", onDragOver, true);
    document.removeEventListener("drop", onDrop, true);
  }

  window.__jobsmithArmDropCatch = function (kind) {
    disarm();
    dropResult = null;
    armed = { kind, timer: setTimeout(disarm, 60000) };  // safety net
    document.addEventListener("dragover", onDragOver, true);
    document.addEventListener("drop", onDrop, true);
    highlight(true);
  };

  window.__jobsmithDisarmDropCatch = disarm;

  // Return-and-clear the recorded drop, so a result is consumed exactly once.
  window.__jobsmithTakeDropResult = function () {
    const r = dropResult;
    dropResult = null;
    return r;
  };
})();

// Final expression must be structured-clonable for Firefox's executeScript.
true;
