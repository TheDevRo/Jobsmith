// snapshot.js — runs as a content script via chrome.scripting.executeScript.
// Returns a list of FieldDescriptor-shaped objects describing visible form fields
// on the current page. Mirrors the structure produced by
// backend/auto_apply/browser_controller.py::_SNAPSHOT_JS so the backend's
// map_fields_to_values can consume it unchanged.

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
      const ref = document.getElementById(labelledBy);
      if (ref) return ref.textContent.trim();
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

  function humanSelectorFor(el) {
    if (el.id) return `#${CSS.escape(el.id)}`;
    if (el.name) return `${el.tagName.toLowerCase()}[name="${CSS.escape(el.name)}"]`;
    return "";
  }

  function collectAllFormEls(root) {
    // Walk light DOM + open shadow roots, collecting input/select/textarea
    // from every reachable subtree.
    const out = [];
    const stack = [root];
    while (stack.length) {
      const node = stack.pop();
      if (!node) continue;
      let kids = null;
      try { kids = node.querySelectorAll("input, select, textarea"); } catch (_) {}
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
    // with role=combobox + aria-haspopup. Map these to "select" so the
    // backend treats them as a constrained choice and fill.js drives the
    // popup option list.
    if (isComboboxInput(el)) return "select";
    const t = (el.type || "text").toLowerCase();
    if (["checkbox", "radio", "file", "email", "tel", "url", "number"].includes(t)) return t;
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
  const fields = [];
  const els = collectAllFormEls(document.documentElement);

  els.forEach((el, idx) => {
    if (!isVisible(el)) return;
    if (el.type === "hidden" || el.type === "submit" || el.type === "button") return;
    if (el.type === "radio") {
      if (seenRadioNames.has(el.name)) return;
      seenRadioNames.add(el.name);
    }
    const fid = el.id || el.name || `field_${idx}`;
    // Stamp the element so fill.js can locate it later via a stable selector
    // that survives React/Vue re-renders and auto-generated id churn.
    try { el.setAttribute("data-jobsmith-fid", fid); } catch (_) {}
    fields.push({
      field_id: fid,
      label: labelFor(el),
      placeholder: el.placeholder || "",
      field_type: fieldType(el),
      name: el.name || "",
      options: optionsFor(el),
      required: el.required || el.getAttribute("aria-required") === "true",
      extra_context: "",
      _selector: `[data-jobsmith-fid="${CSS.escape(fid)}"]`,
      _human_selector: humanSelectorFor(el),
      _autocomplete: el.getAttribute("autocomplete") || "",
      _combobox: isComboboxInput(el),
    });
  });

  return { url: window.location.href, fields };
})();
