// snapshot.js — returns a list of FieldDescriptor-shaped objects describing
// visible form fields on the current page. Mirrors the structure produced by
// backend/auto_apply/browser_controller.py::_SNAPSHOT_JS so the mapper can
// consume it unchanged.
//
// SINGLE SOURCE: this is a verbatim copy of extension/src/common/snapshot.js,
// bundled here so the in-app Apply browser (App/Screens/ApplyBrowserView.swift)
// can inject it into a WKWebView via evaluateJavaScript — the IIFE's final
// expression `{ url, fields }` is returned directly. Keep in sync with the
// extension original when either changes.

(function jobsmithSnapshot() {
  function isVisible(el) {
    if (!el || el.disabled) return false;
    // File inputs are almost always visually hidden behind a styled
    // dropzone/button — keep them no matter what.
    if (el.tagName === "INPUT" && (el.type || "").toLowerCase() === "file") return true;
    const style = window.getComputedStyle(el);
    if (style.display === "none" || style.visibility === "hidden" || style.opacity === "0") return false;
    const rect = el.getBoundingClientRect();
    return rect.width > 0 && rect.height > 0;
  }

  function isComboboxInput(el) {
    if (el.tagName !== "INPUT") return false;
    const role = (el.getAttribute("role") || "").toLowerCase();
    const haspop = (el.getAttribute("aria-haspopup") || "").toLowerCase();
    const autocomp = (el.getAttribute("aria-autocomplete") || "").toLowerCase();
    return role === "combobox"
      || haspop === "listbox" || haspop === "menu" || haspop === "true"
      || autocomp === "list" || autocomp === "both";
  }

  // Workday / custom-widget dropdowns: a button or div acting as a select.
  // Not an <input>, so fill.js can't type into it — it opens the popup and
  // clicks the matching [role=option] instead.
  function isComboboxWidget(el) {
    if (el.tagName === "INPUT" || el.tagName === "SELECT" || el.tagName === "TEXTAREA") return false;
    const role = (el.getAttribute("role") || "").toLowerCase();
    const haspop = (el.getAttribute("aria-haspopup") || "").toLowerCase();
    return role === "combobox"
      || (el.tagName === "BUTTON" && haspop === "listbox");
  }

  function isEditableTextbox(el) {
    const ce = el.getAttribute ? el.getAttribute("contenteditable") : null;
    if (!el.isContentEditable && ce !== "" && ce !== "true") return false;
    const role = (el.getAttribute("role") || "").toLowerCase();
    return role === "textbox" || el.getAttribute("aria-multiline") === "true";
  }

  function labelFor(el) {
    // 1. <label for="id">
    if (el.id) {
      const lab = document.querySelector(`label[for="${CSS.escape(el.id)}"]`);
      if (lab && lab.textContent) return lab.textContent.trim();
    }
    // 2. Parent <label>
    let parent = el.parentElement;
    while (parent && parent.tagName !== "LABEL" && parent.tagName !== "FORM") parent = parent.parentElement;
    if (parent && parent.tagName === "LABEL") return parent.textContent.trim();
    // 3. aria-label / aria-labelledby
    if (el.getAttribute("aria-label")) return el.getAttribute("aria-label").trim();
    const labelledBy = el.getAttribute("aria-labelledby");
    if (labelledBy) {
      let text = "";
      for (const refId of labelledBy.split(/\s+/)) {
        const ref = document.getElementById(refId);
        if (ref) text += " " + ref.textContent;
      }
      if (text.trim()) return text.trim();
    }
    // 4. Previous sibling text
    let sib = el.previousElementSibling;
    while (sib) {
      const t = (sib.textContent || "").trim();
      if (t && t.length < 200) return t;
      sib = sib.previousElementSibling;
    }
    return "";
  }

  // The question a radio/checkbox group (or ambiguous field) belongs to:
  // fieldset legend, or role=group/radiogroup aria label. Sent as
  // extra_context so backend matching sees the real question, not just "Yes".
  function groupContext(el) {
    try {
      const fs = el.closest && el.closest("fieldset");
      if (fs) {
        const lg = fs.querySelector("legend");
        if (lg && lg.textContent.trim()) return lg.textContent.trim().slice(0, 300);
      }
      const grp = el.closest && el.closest('[role="group"], [role="radiogroup"]');
      if (grp) {
        const al = grp.getAttribute("aria-label");
        if (al && al.trim()) return al.trim().slice(0, 300);
        const lb = grp.getAttribute("aria-labelledby");
        if (lb) {
          const ref = document.getElementById(lb.split(/\s+/)[0]);
          if (ref && ref.textContent.trim()) return ref.textContent.trim().slice(0, 300);
        }
      }
    } catch (_) { /* closest() unsupported on this node */ }
    return "";
  }

  function humanSelectorFor(el) {
    if (el.id) return `#${CSS.escape(el.id)}`;
    if (el.name) return `${el.tagName.toLowerCase()}[name="${CSS.escape(el.name)}"]`;
    return "";
  }

  const FIELD_QUERY = 'input, select, textarea, [role="combobox"], button[aria-haspopup="listbox"], [contenteditable="true"], [contenteditable=""]';

  function collectAllFormEls(root) {
    // Walk light DOM + open shadow roots, collecting form fields from every
    // reachable subtree. querySelectorAll dedupes within one scope.
    const out = [];
    const stack = [root];
    while (stack.length) {
      const node = stack.pop();
      if (!node) continue;
      let kids = null;
      try { kids = node.querySelectorAll(FIELD_QUERY); } catch (_) {}
      if (kids) for (const k of kids) out.push(k);
      let descendants = null;
      try { descendants = node.querySelectorAll("*"); } catch (_) {}
      if (descendants) {
        for (const d of descendants) {
          if (d.shadowRoot) stack.push(d.shadowRoot);
        }
      }
    }
    return out;
  }

  function fieldType(el) {
    if (el.tagName === "TEXTAREA") return "textarea";
    if (el.tagName === "SELECT") return "select";
    // React-select / Ashby / Workday combobox widgets render a text input
    // (or button/div) with role=combobox + aria-haspopup. Map these to
    // "select" so the backend treats them as a constrained choice and
    // fill.js drives the popup option list.
    if (isComboboxInput(el) || isComboboxWidget(el)) return "select";
    if (isEditableTextbox(el)) return "textarea";
    const t = (el.type || "text").toLowerCase();
    if (["checkbox", "radio", "file", "email", "tel", "url", "number", "date", "password"].includes(t)) return t;
    if (t === "datetime-local" || t === "month" || t === "week" || t === "time") return "date";
    return "text";
  }

  function optionsFor(el) {
    if (el.tagName === "SELECT") {
      return Array.from(el.options).map(o => o.textContent.trim()).filter(Boolean);
    }
    if (el.type === "radio" && el.name) {
      const group = document.querySelectorAll(`input[type="radio"][name="${CSS.escape(el.name)}"]`);
      return Array.from(group).map(r => labelFor(r) || r.value).filter(Boolean);
    }
    return null;
  }

  const seenRadioNames = new Set();
  const seenFids = new Set();
  const fields = [];
  const els = collectAllFormEls(document.documentElement);

  els.forEach((el, idx) => {
    const isFormTag = ["INPUT", "SELECT", "TEXTAREA"].includes(el.tagName);
    if (!isFormTag && !isComboboxWidget(el) && !isEditableTextbox(el)) return;
    // A combobox wrapper div usually contains the real <input> (react-select);
    // skip the wrapper so the widget isn't captured twice.
    if (!isFormTag && el.querySelector && el.querySelector("input, select, textarea")) return;
    if (!isVisible(el)) return;
    if (el.tagName === "INPUT" && ["hidden", "submit", "button", "reset", "image"].includes((el.type || "").toLowerCase())) return;
    if (el.type === "radio" && el.name) {
      // Dedupe by group name — but only for named radios; unnamed radios are
      // standalone fields and must not swallow each other.
      if (seenRadioNames.has(el.name)) return;
      seenRadioNames.add(el.name);
    }
    let fid = el.id || el.name || `field_${idx}`;
    // Same-name checkbox groups (name="days[]") must not collide.
    if (seenFids.has(fid)) fid = `${fid}_${idx}`;
    seenFids.add(fid);
    // Stamp the element so fill.js can locate it later via a stable selector
    // that survives React/Vue re-renders and auto-generated id churn.
    try { el.setAttribute("data-jobsmith-fid", fid); } catch (_) {}
    const label = labelFor(el);
    fields.push({
      field_id: fid,
      label: label,
      placeholder: el.placeholder || el.getAttribute("data-placeholder") || "",
      field_type: fieldType(el),
      name: el.name || "",
      options: optionsFor(el),
      required: el.required || el.getAttribute("aria-required") === "true",
      extra_context: groupContext(el),
      autocomplete: el.getAttribute("autocomplete") || "",
      _selector: `[data-jobsmith-fid="${CSS.escape(fid)}"]`,
      _human_selector: humanSelectorFor(el),
      _combobox: isComboboxInput(el) || isComboboxWidget(el),
      _editable: isEditableTextbox(el),
    });
  });

  return { url: window.location.href, fields };
})();
