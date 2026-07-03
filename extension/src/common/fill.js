// fill.js — registers window.__jobsmithFillAndHighlight in the content-script
// isolated world. The side panel injects this file once, then calls the
// function with a payload via a second scripting.executeScript({func,args})
// (which runs in the same isolated world and can see the global).
//
// Payload item shape:
//   {
//     field_id:   string,
//     selector:   string,        // CSS selector (from snapshot._selector)
//     name:       string,        // input name attr (used for radio groups)
//     value:      string,
//     action:     "fill"|"select"|"check"|"upload"|"skip",
//     field_type: "text"|"textarea"|"select"|"checkbox"|"radio"|"email"|"tel"|"url"|"number"|"file",
//     confidence: number,
//     source:     string,
//     options?:   string[],      // for select / radio
//   }
//
// Returns: { results: [{field_id, status, message?}], highlighted: number }
// status: "filled" | "low_confidence" | "skipped" | "not_found" | "failed"

window.__jobsmithFillAndHighlight = async function jobsmithFillAndHighlight(items, opts) {
  opts = opts || {};
  const LOW_CONF = 0.60;
  const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

  // Drive a react-select / Ashby / Workday-style combobox: open the
  // popup, type the value to filter, then click the matching option.
  // Returns { ok, message? }.
  async function fillCombobox(input, value) {
    const want = (value || "").trim();
    if (!want) return { ok: false, message: "empty value" };

    input.focus();
    input.dispatchEvent(new MouseEvent("mousedown", { bubbles: true }));
    input.dispatchEvent(new MouseEvent("mouseup", { bubbles: true }));
    input.click();

    // Type the value so the combobox filters its list. Empty out first
    // in case React holds prior state.
    nativeSet(input, "");
    fireInputEvents(input, "");
    await sleep(20);
    nativeSet(input, want);
    fireInputEvents(input, want);

    // Wait for options to render in the DOM portal.
    let opts2 = [];
    for (let i = 0; i < 30; i++) {
      await sleep(50);
      opts2 = Array.from(document.querySelectorAll('[role="option"]'))
        .filter((o) => {
          const r = o.getBoundingClientRect();
          return r.width > 0 && r.height > 0;
        });
      if (opts2.length) break;
    }
    if (!opts2.length) return { ok: false, message: "no options rendered" };

    const lower = want.toLowerCase();
    let target =
      opts2.find((o) => (o.textContent || "").trim().toLowerCase() === lower) ||
      opts2.find((o) => (o.textContent || "").trim().toLowerCase().startsWith(lower)) ||
      opts2.find((o) => (o.textContent || "").trim().toLowerCase().includes(lower));
    if (!target) return { ok: false, message: `no option matches "${want.slice(0, 40)}"` };

    target.dispatchEvent(new MouseEvent("mousedown", { bubbles: true }));
    target.dispatchEvent(new MouseEvent("mouseup", { bubbles: true }));
    target.click();
    await sleep(30);
    return { ok: true };
  }

  function nativeSet(el, value) {
    const proto =
      el instanceof HTMLTextAreaElement ? HTMLTextAreaElement.prototype :
      el instanceof HTMLSelectElement   ? HTMLSelectElement.prototype :
      HTMLInputElement.prototype;
    // React tracks the last value it knows about on el._valueTracker.
    // Seeding the tracker with the current value forces React to treat the
    // upcoming setter as a real user change, so onChange fires and the new
    // value sticks instead of snapping back on next render.
    const tracker = el._valueTracker;
    if (tracker && typeof tracker.setValue === "function") {
      try { tracker.setValue(el.value || ""); } catch (_) { /* ignore */ }
    }
    const desc = Object.getOwnPropertyDescriptor(proto, "value");
    if (desc && desc.set) desc.set.call(el, value);
    else el.value = value;
  }

  // Explicitly dispatch focus/blur events. We can't rely on el.focus()/
  // el.blur() because an injected content script often runs while the page
  // tab is NOT the focused document (the side panel holds focus), so those
  // calls silently no-op and validators that fire on blur never run.
  function fireFocus(el) {
    el.dispatchEvent(new FocusEvent("focusin", { bubbles: true }));
    el.dispatchEvent(new FocusEvent("focus",   { bubbles: false }));
  }
  function fireBlur(el) {
    el.dispatchEvent(new FocusEvent("focusout", { bubbles: true }));
    el.dispatchEvent(new FocusEvent("blur",     { bubbles: false }));
  }

  // Fire the event sequence a real keystroke produces. Plain Event("input")
  // is not enough for frameworks (Angular value accessor, Vue, Lit) and
  // masked-input libs that read InputEvent.inputType/data or listen for key
  // events. `value` is optional (combobox calls pass it to drive filtering).
  function fireInputEvents(el, value) {
    const v = value == null ? "" : String(value);
    const last = v.length ? v[v.length - 1] : "";
    try { el.dispatchEvent(new KeyboardEvent("keydown", { bubbles: true, key: last })); } catch (_) {}
    try {
      el.dispatchEvent(new InputEvent("beforeinput", { bubbles: true, cancelable: true, inputType: "insertText", data: v }));
    } catch (_) {}
    try {
      el.dispatchEvent(new InputEvent("input", { bubbles: true, inputType: "insertText", data: v }));
    } catch (_) {
      el.dispatchEvent(new Event("input", { bubbles: true }));
    }
    try { el.dispatchEvent(new KeyboardEvent("keyup", { bubbles: true, key: last })); } catch (_) {}
    el.dispatchEvent(new Event("change", { bubbles: true }));
  }

  function deepQuerySelector(sel) {
    // querySelector across light DOM + open shadow roots.
    if (!sel) return null;
    try {
      const direct = document.querySelector(sel);
      if (direct) return direct;
    } catch (_) { return null; }
    const stack = [document.documentElement];
    while (stack.length) {
      const node = stack.pop();
      let descendants = null;
      try { descendants = node.querySelectorAll("*"); } catch (_) {}
      if (!descendants) continue;
      for (const d of descendants) {
        if (d.shadowRoot) {
          try {
            const hit = d.shadowRoot.querySelector(sel);
            if (hit) return hit;
          } catch (_) {}
          stack.push(d.shadowRoot);
        }
      }
    }
    return null;
  }

  function findElement(item) {
    if (item.selector) {
      const el = deepQuerySelector(item.selector);
      if (el) return el;
    }
    if (item.name) {
      const byName = deepQuerySelector(
        `input[name="${CSS.escape(item.name)}"], textarea[name="${CSS.escape(item.name)}"], select[name="${CSS.escape(item.name)}"]`
      );
      if (byName) return byName;
    }
    return null;
  }

  function pickSelectOption(selectEl, wantedValue) {
    const want = (wantedValue || "").trim().toLowerCase();
    if (!want) return null;
    for (const opt of selectEl.options) {
      const candidates = [opt.value, opt.textContent].map(s => (s || "").trim().toLowerCase());
      if (candidates.includes(want)) return opt;
    }
    // Substring fallback
    for (const opt of selectEl.options) {
      const text = (opt.textContent || "").trim().toLowerCase();
      if (text && (text.includes(want) || want.includes(text))) return opt;
    }
    return null;
  }

  function pickRadioInGroup(name, wantedValue) {
    const group = document.querySelectorAll(`input[type="radio"][name="${CSS.escape(name)}"]`);
    const want = (wantedValue || "").trim().toLowerCase();
    if (!group.length || !want) return null;
    for (const r of group) {
      const lab = labelTextFor(r) || r.value;
      if ((lab || "").trim().toLowerCase() === want) return r;
    }
    for (const r of group) {
      const lab = labelTextFor(r) || r.value;
      const t = (lab || "").trim().toLowerCase();
      if (t && (t.includes(want) || want.includes(t))) return r;
    }
    return null;
  }

  function labelTextFor(el) {
    if (el.id) {
      const lab = document.querySelector(`label[for="${CSS.escape(el.id)}"]`);
      if (lab) return lab.textContent.trim();
    }
    let p = el.parentElement;
    while (p && p.tagName !== "LABEL") p = p.parentElement;
    return p ? p.textContent.trim() : "";
  }

  function isTruthyAnswer(s) {
    return /^(y(es)?|true|1|on)$/i.test((s || "").trim());
  }

  // ---- Apply fills -----------------------------------------------------
  const results = [];

  for (const item of items || []) {
    if (item.action === "skip" || !item.value) {
      results.push({ field_id: item.field_id, status: "skipped" });
      continue;
    }

    const el = findElement(item);
    if (!el) {
      results.push({ field_id: item.field_id, status: "not_found" });
      continue;
    }

    try {
      if (item.field_type === "file" || item.action === "upload") {
        if (!item.file_bytes || !item.file_name) {
          results.push({ field_id: item.field_id, status: "skipped", message: "no file bytes (load job first)" });
          continue;
        }
        try {
          const bytes = item.file_bytes instanceof Uint8Array
            ? item.file_bytes
            : new Uint8Array(item.file_bytes);
          const file = new File([bytes], item.file_name, { type: item.file_mime || "application/octet-stream" });
          const dt = new DataTransfer();
          dt.items.add(file);
          // Find the real <input type=file>: may be hidden, with a styled dropzone wrapper.
          const input = (el.tagName === "INPUT" && el.type === "file")
            ? el
            : (el.querySelector && el.querySelector('input[type="file"]')) || el;
          input.files = dt.files;
          input.dispatchEvent(new Event("input", { bubbles: true }));
          input.dispatchEvent(new Event("change", { bubbles: true }));
          // Best-effort: some dropzones (Greenhouse, Lever) listen for drop on a wrapper.
          const dropTarget = input.closest('[data-test-id*="drop"], .dropzone, [class*="drop"]') || input.parentElement;
          if (dropTarget && dropTarget !== input) {
            try {
              const dropEvt = new DragEvent("drop", { bubbles: true, cancelable: true, dataTransfer: dt });
              dropTarget.dispatchEvent(dropEvt);
            } catch (_) { /* DragEvent constructor may not accept dataTransfer in all browsers */ }
          }
          if (!input.files || input.files.length === 0) {
            results.push({ field_id: item.field_id, status: "failed", message: "input.files did not accept assignment" });
            continue;
          }
          results.push({ field_id: item.field_id, status: "filled", message: `attached ${item.file_name}` });
        } catch (e) {
          results.push({ field_id: item.field_id, status: "failed", message: `upload: ${e.message || e}` });
        }
        continue;
      }

      if (item._combobox || (item.field_type === "select" && el.tagName !== "SELECT")) {
        const out = await fillCombobox(el, item.value);
        if (!out.ok) { results.push({ field_id: item.field_id, status: "failed", message: out.message }); continue; }
      } else if (item.field_type === "select") {
        const opt = pickSelectOption(el, item.value);
        if (!opt) { results.push({ field_id: item.field_id, status: "failed", message: "no matching option" }); continue; }
        el.value = opt.value;
        fireInputEvents(el);
      } else if (item.field_type === "radio") {
        const target = pickRadioInGroup(item.name || el.name, item.value);
        if (!target) { results.push({ field_id: item.field_id, status: "failed", message: "no matching radio" }); continue; }
        target.click();
      } else if (item.field_type === "checkbox") {
        const want = isTruthyAnswer(item.value);
        if (el.checked !== want) el.click();
      } else {
        el.focus();
        fireFocus(el);
        nativeSet(el, item.value);
        fireInputEvents(el, item.value);
        fireBlur(el);
        el.blur();
        // Verify-after-fill: if React snapped the value back, surface it.
        const actual = (el.value || "");
        const wanted = (item.value || "");
        if (actual !== wanted && !(wanted && actual.includes(wanted))) {
          results.push({
            field_id: item.field_id,
            status: "failed",
            message: actual ? `reverted to "${actual.slice(0, 40)}"` : "value reverted",
          });
          continue;
        }
      }

      const lowConf = typeof item.confidence === "number" && item.confidence < LOW_CONF;
      results.push({ field_id: item.field_id, status: lowConf ? "low_confidence" : "filled" });
    } catch (e) {
      results.push({ field_id: item.field_id, status: "failed", message: String(e && e.message || e) });
    }
  }

  // ---- Highlight overlay ----------------------------------------------
  const styleId = "__jobsmith-hl__";
  const prev = document.getElementById(styleId);
  if (prev) prev.remove();

  if (opts.clearOnly) {
    return { results, highlighted: 0 };
  }

  const colors = {
    filled:          "#a6e3a1",
    low_confidence:  "#f9e2af",
    skipped:         "#6c7086",
    not_found:       "#f38ba8",
    failed:          "#f38ba8",
  };

  const byId = Object.fromEntries((items || []).map(i => [i.field_id, i]));
  const rules = [];
  for (const r of results) {
    const it = byId[r.field_id];
    if (!it || !it.selector) continue;
    // Required + skipped/not-filled gets a loud red so the user notices.
    let color = colors[r.status] || "#6c7086";
    if (it.required && (r.status === "skipped" || r.status === "not_found")) {
      color = "#f38ba8";
    }
    const width = (it.required && r.status !== "filled") ? 3 : 2;
    rules.push(`${it.selector} { outline: ${width}px solid ${color} !important; outline-offset: 2px !important; }`);
  }
  if (rules.length) {
    const style = document.createElement("style");
    style.id = styleId;
    style.textContent = rules.join("\n");
    (document.head || document.documentElement).appendChild(style);
  }

  return { results, highlighted: rules.length };
};

// Final expression must be structured-clonable for Firefox's executeScript.
true;
