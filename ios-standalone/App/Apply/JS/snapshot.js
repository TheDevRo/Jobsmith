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

  // The hidden native <select> that a Fabric-style (BambooHR) custom dropdown
  // backs: a visible toggle button/div plus a display:none / off-screen
  // <select> rendered inside the same wrapper. Returns it whether or not it
  // carries real options (React often fills those in later), else null.
  function hiddenNativeSelectFor(el) {
    const wrap = el.parentElement;
    // Only a tight widget wrapper counts — never a whole <form>/<fieldset>, or
    // an unrelated hidden <select> elsewhere on the page would match. The
    // backing select must be a sibling of the toggle or a direct child of the
    // wrapper.
    if (!wrap || wrap.tagName === "FORM" || wrap.tagName === "FIELDSET") return null;
    for (const sib of wrap.children) {
      if (sib.tagName === "SELECT" && sib !== el && !isVisible(sib)) return sib;
      if (sib !== el) {
        for (const gk of sib.children) {
          if (gk.tagName === "SELECT" && !isVisible(gk)) return gk;
        }
      }
    }
    return null;
  }

  // Page chrome that hosts popup toggles which are NOT form fields (nav menus,
  // account buttons, share menus). aria-haspopup="true" alone is too weak a
  // signal inside these regions.
  function inChromeRegion(el) {
    try {
      return !!(el.closest && el.closest(
        'nav, header, footer, [role="navigation"], [role="menubar"], [role="banner"], [role="menu"], [role="toolbar"]'));
    } catch (_) { return false; }
  }

  // Workday / custom-widget dropdowns: a button or div acting as a select.
  // Not an <input>, so fill.js can't type into it — it opens the popup and
  // clicks the matching [role=option] instead. Matches role=combobox, a
  // button/div toggle with aria-haspopup="listbox", or a toggle backed by a
  // hidden native <select>. A bare aria-haspopup="true" toggle (BambooHR's
  // Fabric uses it) additionally needs a label or a backing select and must
  // sit outside page chrome — otherwise every nav/share menu button on the
  // page would be captured as a select field.
  function isComboboxWidget(el) {
    if (el.tagName === "INPUT" || el.tagName === "SELECT" || el.tagName === "TEXTAREA") return false;
    const role = (el.getAttribute("role") || "").toLowerCase();
    const haspop = (el.getAttribute("aria-haspopup") || "").toLowerCase();
    if (role === "combobox") return true;
    const isToggle = el.tagName === "BUTTON" || el.tagName === "DIV";
    if (isToggle && haspop === "listbox") return true;
    if (isToggle && hiddenNativeSelectFor(el)) return true;
    if (isToggle && haspop === "true" && !inChromeRegion(el) && labelFor(el)) return true;
    return false;
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
    // 4. Previous sibling text — but only text that reads like a label. The
    // first non-empty sibling decides: helper prose (multi-sentence, long)
    // between fields must not become the next field's label.
    let sib = el.previousElementSibling;
    while (sib) {
      const t = (sib.textContent || "").trim();
      if (t) {
        if ((sib.tagName === "LABEL" || sib.tagName === "LEGEND") && t.length < 200) return t;
        if (t.length < 120 && !/[.!?]\s+\S/.test(t)) return t;
        return "";
      }
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

  // Placeholder option text ("–Select–", "Choose…") that isn't a real choice.
  function isSelectPlaceholder(text) {
    return /^[\s–—-]*(select|choose|please|pick)\b/i.test((text || "").trim());
  }

  // Anti-bot honeypot: a field the form expects to stay empty. BambooHR silently
  // rejects the whole submission if one is filled, so it must never be emitted.
  // Conservative on purpose (name/label/placeholder signatures + far-offscreen
  // positioning) so real fields are never dropped.
  function isHoneypot(el) {
    const idName = ((el.name || "") + " " + (el.id || "")).toLowerCase();
    if (/hpcsaf|honeypot|leaveblank/i.test(idName)) return true;
    const lbl = (labelFor(el) || "");
    const ph = (el.placeholder || el.getAttribute("data-placeholder") || "");
    if (/leave\s+(this\s+)?(field\s+)?blank/i.test(lbl) ||
        /leave\s+(this\s+)?(field\s+)?blank/i.test(ph)) return true;
    // Off-screen via absolute positioning (BambooHR hides its trap this way, not
    // display:none, so it slips past isVisible). Document-relative coordinates,
    // NOT viewport-relative: on a scrolled page every field above the fold has a
    // negative viewport rect, and those are real fields.
    try {
      const r = el.getBoundingClientRect();
      const absLeft = r.left + (window.pageXOffset || 0);
      const absTop = r.top + (window.pageYOffset || 0);
      if (absLeft + r.width < 0 || absLeft <= -9999 || absTop <= -9999) return true;
    } catch (_) {}
    let node = el;
    for (let hops = 0; node && hops < 5; hops++, node = node.parentElement) {
      try {
        const st = window.getComputedStyle(node);
        if (st.position === "absolute" || st.position === "fixed") {
          const left = parseFloat(st.left);
          if (!isNaN(left) && left <= -9999) return true;
          const top = parseFloat(st.top);
          if (!isNaN(top) && top <= -9999) return true;
        }
      } catch (_) {}
    }
    return false;
  }

  // Native radio/checkbox that a design system hides (opacity:0 / sr-only /
  // 1px) behind a visible styled proxy + label. isVisible fails on the input
  // itself, so without this the Yes/No screening questions (work auth,
  // sponsorship) are never detected. Only inputs with a real visible label or
  // wrapping control qualify — honeypots are excluded separately (isHoneypot).
  function hasVisibleProxy(el) {
    if (el.tagName !== "INPUT") return false;
    const t = (el.type || "").toLowerCase();
    if (t !== "radio" && t !== "checkbox") return false;
    if (el.id) {
      let lab = null;
      try { lab = document.querySelector(`label[for="${CSS.escape(el.id)}"]`); } catch (_) {}
      if (lab && isVisible(lab)) return true;
    }
    let p = el.parentElement;
    for (let hops = 0; p && hops < 3; hops++, p = p.parentElement) {
      const role = (p.getAttribute && (p.getAttribute("role") || "").toLowerCase()) || "";
      if ((p.tagName === "LABEL" || role === "radio" || role === "checkbox") && isVisible(p)) return true;
    }
    return false;
  }

  // BambooHR & many design systems mark required fields with a leading or
  // trailing "*" in the label, not the native required attribute. Only edge
  // asterisks count — a "*" mid-label is usually a footnote reference, and
  // treating it as required floods the panel with false "required, not
  // filled" rows.
  function hasRequiredMarker(text) {
    return /^\s*\*|\*\s*$/.test(text || "");
  }

  // Workday renders one date as separate spinbutton inputs
  // (data-automation-id="dateSectionMonth-input" / Day / Year) that carry no
  // label of their own. Name the segment so the mapper sees
  // "Start Date — Month" instead of an anonymous text input.
  function workdayDateSegment(el) {
    const aid = (el.getAttribute && (el.getAttribute("data-automation-id") || "")) || "";
    const m = aid.match(/dateSection(Month|Day|Year)/i);
    return m ? m[1] : "";
  }

  // The owning field's label for a widget whose inputs sit a few wrappers
  // below it (Workday date segments). First labelled ancestor wins; the walk
  // stops at broad containers whose first <label> belongs to some other field.
  function ancestorLabelText(el) {
    let anc = el.parentElement;
    for (let hops = 0; anc && hops < 6; hops++, anc = anc.parentElement) {
      if (anc.tagName === "FORM" || anc.tagName === "BODY") break;
      let lab = null;
      try { lab = anc.querySelector("label"); } catch (_) {}
      if (lab && lab.textContent.trim()) return lab.textContent.trim().slice(0, 200);
    }
    return "";
  }

  const FIELD_QUERY = 'input, select, textarea, [role="combobox"], button[aria-haspopup="listbox"], button[aria-haspopup="true"], [aria-haspopup="true"], [contenteditable="true"], [contenteditable=""]';

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
    // Fabric custom select: the visible options are rendered by React into a
    // popup, but the backing hidden <select> sometimes carries the real list.
    // Use it when it has >1 real option; otherwise leave null so fill.js opens
    // the popup and reads the rendered options.
    if (isComboboxWidget(el)) {
      const nativeSel = hiddenNativeSelectFor(el);
      if (nativeSel) {
        const opts = Array.from(nativeSel.options)
          .map(o => o.textContent.trim())
          .filter(t => t && !isSelectPlaceholder(t));
        if (opts.length > 1) return opts;
      }
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
    // Anti-bot honeypots (offscreen / "leave blank" / hpcsaf) must never be
    // emitted — filling one makes BambooHR silently reject the submission.
    if (isHoneypot(el)) return;
    // Native radio/checkbox hidden behind a visible styled proxy still counts.
    if (!isVisible(el) && !hasVisibleProxy(el)) return;
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
    let label = labelFor(el);
    const dateSeg = workdayDateSegment(el);
    if (dateSeg) {
      const owner = ancestorLabelText(el) || label;
      label = owner ? owner + " — " + dateSeg : dateSeg;
    }
    fields.push({
      field_id: fid,
      label: label,
      placeholder: el.placeholder || el.getAttribute("data-placeholder") || "",
      field_type: fieldType(el),
      name: el.name || "",
      options: optionsFor(el),
      required: el.required || el.getAttribute("aria-required") === "true" || hasRequiredMarker(label),
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
