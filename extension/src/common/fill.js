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
//     field_type: "text"|"textarea"|"select"|"checkbox"|"radio"|"email"|"tel"|"url"|"number"|"file"|"date"|"password",
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

  // ---- Option matching ---------------------------------------------------
  // Scored matching instead of greedy substring, so "No" never grabs
  // "Not applicable" and state abbreviations still find their full names.

  const OPT_THRESHOLD = 55;

  const US_STATES = {
    al: "alabama", ak: "alaska", az: "arizona", ar: "arkansas", ca: "california",
    co: "colorado", ct: "connecticut", de: "delaware", fl: "florida", ga: "georgia",
    hi: "hawaii", id: "idaho", il: "illinois", in: "indiana", ia: "iowa",
    ks: "kansas", ky: "kentucky", la: "louisiana", me: "maine", md: "maryland",
    ma: "massachusetts", mi: "michigan", mn: "minnesota", ms: "mississippi",
    mo: "missouri", mt: "montana", ne: "nebraska", nv: "nevada", nh: "new hampshire",
    nj: "new jersey", nm: "new mexico", ny: "new york", nc: "north carolina",
    nd: "north dakota", oh: "ohio", ok: "oklahoma", or: "oregon", pa: "pennsylvania",
    ri: "rhode island", sc: "south carolina", sd: "south dakota", tn: "tennessee",
    tx: "texas", ut: "utah", vt: "vermont", va: "virginia", wa: "washington",
    wv: "west virginia", wi: "wisconsin", wy: "wyoming", dc: "district of columbia",
  };
  const US_STATES_REV = Object.fromEntries(Object.entries(US_STATES).map(([k, v]) => [v, k]));
  const COUNTRY_ALIASES = {
    "united states": ["usa", "us", "united states of america", "america"],
    "united kingdom": ["uk", "great britain", "england"],
  };
  // Degree strings ("BS Computer Science") → education-level dropdown buckets.
  const DEGREE_LEVELS = [
    [/\b(ph\.?d|doctor)/, ["phd", "doctorate", "doctoral degree"]],
    [/\bmba\b/, ["mba", "master's degree", "masters"]],
    [/\b(ms|msc|ma|master)\b/, ["master's degree", "masters", "master"]],
    [/\b(bs|bsc|ba|bachelor)\b/, ["bachelor's degree", "bachelors", "bachelor"]],
    [/\b(associate|aa|aas)\b/, ["associate's degree", "associate degree", "associate"]],
  ];

  function normText(s) {
    return (s || "").toLowerCase()
      .replace(/[^a-z0-9+#.\s]/g, " ")
      .replace(/\s+/g, " ")
      .trim();
  }
  function tokensOf(s) { return normText(s).split(" ").filter(Boolean); }

  function isPlaceholderOption(text) {
    const t = normText(text);
    return !t || /^(select|choose|please|pick|none selected)\b/.test(t);
  }

  function candidatesFor(want) {
    const w = normText(want);
    const out = [want];
    if (US_STATES[w]) out.push(US_STATES[w]);
    else if (US_STATES_REV[w]) out.push(US_STATES_REV[w]);
    for (const [canonical, aliases] of Object.entries(COUNTRY_ALIASES)) {
      if (w === canonical) out.push(...aliases);
      else if (aliases.includes(w)) out.push(canonical);
    }
    for (const [re, expansions] of DEGREE_LEVELS) {
      if (re.test(w)) { out.push(...expansions); break; }
    }
    return out;
  }

  function optionScore(want, text) {
    const w = normText(want), t = normText(text);
    if (!w || !t) return 0;
    if (w === t) return 100;
    const wt = tokensOf(w), tt = tokensOf(t);
    const wset = new Set(wt), tset = new Set(tt);
    if (w === "yes" || w === "no") {
      if (w === "yes") {
        if (/^y(es)?\b/.test(t)) return 90;
        return tset.has("yes") ? 80 : 0;
      }
      if (/^n(o)?\b/.test(t)) return 90;
      return (tset.has("no") || tset.has("not") || tset.has("none") || tset.has("never")) ? 75 : 0;
    }
    const wIn = wt.every((x) => tset.has(x));
    const tIn = tt.every((x) => wset.has(x));
    if (wIn && tIn) return 95;
    if (wIn) return Math.max(60, 88 - (tt.length - wt.length));  // prefer shorter options
    if (tIn) return Math.max(60, 80 - (wt.length - tt.length));
    let inter = 0;
    for (const x of wset) if (tset.has(x)) inter++;
    if (!inter) return 0;
    const uni = new Set([...wset, ...tset]).size;
    const sub = (t.includes(w) || w.includes(t)) ? 25 : 0;
    return Math.round(40 * (inter / uni)) + sub;
  }

  function scoreAgainst(want, text) {
    let best = 0;
    for (const cand of candidatesFor(want)) {
      const s = optionScore(cand, text);
      if (s > best) best = s;
    }
    return best;
  }

  // ---- Combobox driving --------------------------------------------------
  // Handles react-select / Ashby / Greenhouse typeaheads (typeable inputs)
  // AND Workday-style button widgets (open + click, no typing). Retries with
  // progressively shorter queries because async option lists (Workday
  // location lookups) often return nothing for a full-string query.
  // Multi-select widgets (aria-multiselectable) get the value split on
  // separators and each part selected in turn.
  async function fillCombobox(el, value) {
    const want = (value || "").trim();
    if (!want) return { ok: false, message: "empty value" };
    const canType = el.tagName === "INPUT" || el.tagName === "TEXTAREA";

    function openWidget() {
      try { el.focus(); } catch (_) {}
      fireFocus(el);
      el.dispatchEvent(new MouseEvent("mousedown", { bubbles: true }));
      el.dispatchEvent(new MouseEvent("mouseup", { bubbles: true }));
      el.click();
    }

    function visibleOptions() {
      const root = el.getRootNode ? el.getRootNode() : document;
      const scopes = [];
      // The listbox the widget declares it controls is the highest-signal scope.
      const ids = (
        (el.getAttribute("aria-controls") || "") + " " + (el.getAttribute("aria-owns") || "")
      ).split(/\s+/).filter(Boolean);
      for (const id of ids) {
        let scope = null;
        try { scope = (root.getElementById && root.getElementById(id)) || document.getElementById(id); } catch (_) {}
        if (scope) scopes.push(scope);
      }
      scopes.push(root);
      if (root !== document) scopes.push(document);  // portals render into <body>
      const tiers = ['[role="option"]', '[role="listbox"] li', 'li[class*="option"], div[class*="option"]'];
      for (const sel of tiers) {
        const seen = new Set();
        const out = [];
        for (const scope of scopes) {
          let cands = [];
          try { cands = scope.querySelectorAll(sel); } catch (_) {}
          for (const o of cands) {
            if (seen.has(o)) continue;
            seen.add(o);
            const r = o.getBoundingClientRect();
            if (r.width > 0 && r.height > 0) out.push(o);
          }
        }
        if (out.length) return out;
      }
      return [];
    }

    async function pollOptions(ms) {
      const deadline = Date.now() + ms;
      let found = [];
      while (Date.now() < deadline) {
        await sleep(75);
        found = visibleOptions();
        if (found.length) break;
      }
      return found;
    }

    function typeQuery(q) {
      nativeSet(el, "");
      fireInputEvents(el, "");
      nativeSet(el, q);
      fireInputEvents(el, q);
    }

    function pickFrom(rendered, wanted) {
      let best = null, bestScore = 0;
      for (const o of rendered) {
        const text = (o.textContent || "").trim();
        if (isPlaceholderOption(text)) continue;
        const s = scoreAgainst(wanted, text);
        if (s > bestScore || (s === bestScore && best && text.length < (best.textContent || "").trim().length)) {
          best = o; bestScore = s;
        }
      }
      return bestScore >= OPT_THRESHOLD ? best : null;
    }

    function clickOption(target) {
      try { target.scrollIntoView({ block: "nearest" }); } catch (_) {}
      target.dispatchEvent(new MouseEvent("mousedown", { bubbles: true }));
      target.dispatchEvent(new MouseEvent("mouseup", { bubbles: true }));
      target.click();
    }

    function isMultiselectable() {
      if ((el.getAttribute("aria-multiselectable") || "").toLowerCase() === "true") return true;
      const root = el.getRootNode ? el.getRootNode() : document;
      const ids = (
        (el.getAttribute("aria-controls") || "") + " " + (el.getAttribute("aria-owns") || "")
      ).split(/\s+/).filter(Boolean);
      for (const id of ids) {
        let scope = null;
        try { scope = (root.getElementById && root.getElementById(id)) || document.getElementById(id); } catch (_) {}
        if (scope && (scope.getAttribute("aria-multiselectable") || "").toLowerCase() === "true") return true;
      }
      const lb = document.querySelector('[role="listbox"][aria-multiselectable="true"]');
      return !!lb;
    }

    // Select a single value via the query ladder. Returns
    // { ok, message?, unverified? }.
    async function selectOne(wanted) {
      const queries = [];
      if (canType) {
        queries.push(wanted);
        const firstWord = wanted.split(/[\s,]+/)[0];
        if (firstWord && firstWord.length >= 2 && firstWord.toLowerCase() !== wanted.toLowerCase()) queries.push(firstWord);
        if (wanted.length > 4) queries.push(wanted.slice(0, 3));
      } else {
        queries.push(null);  // button widget — just open and read the list
      }

      let sawOptions = false;
      for (let i = 0; i < queries.length; i++) {
        if (queries[i] !== null) typeQuery(queries[i]);
        const rendered = await pollOptions(i === 0 ? 2000 : 1500);
        if (!rendered.length) continue;
        sawOptions = true;
        const target = pickFrom(rendered, wanted);
        if (target) {
          const pickedText = (target.textContent || "").trim();
          clickOption(target);
          await sleep(80);
          if (!canType) {
            // Button widgets show the selection as their own text — verify
            // the click actually took, and flag for review if we can't tell.
            const shown = normText(el.textContent || el.value || "");
            const pickedN = normText(pickedText);
            if (shown && pickedN && !(shown.includes(pickedN) || pickedN.includes(shown))) {
              return { ok: true, unverified: true, message: `clicked "${pickedText.slice(0, 40)}" — verify selection` };
            }
          }
          return { ok: true };
        }
      }

      if (!canType) {
        return { ok: false, message: sawOptions ? `no option matches "${wanted.slice(0, 40)}"` : "no options rendered" };
      }

      // Last resort: retype the full value and commit with Enter — many
      // comboboxes accept the highlighted entry or free text.
      typeQuery(wanted);
      await sleep(150);
      const target = pickFrom(visibleOptions(), wanted);
      if (target) {
        clickOption(target);
        await sleep(80);
        return { ok: true };
      }
      el.dispatchEvent(new KeyboardEvent("keydown", { bubbles: true, key: "Enter", keyCode: 13 }));
      el.dispatchEvent(new KeyboardEvent("keyup", { bubbles: true, key: "Enter", keyCode: 13 }));
      await sleep(80);
      if ((el.value || "").trim()) {
        return { ok: true, unverified: true, message: "typed value — verify selection" };
      }
      return { ok: false, message: sawOptions ? `no option matches "${wanted.slice(0, 40)}"` : "no options rendered" };
    }

    openWidget();
    await sleep(50);

    const parts = isMultiselectable()
      ? want.split(/[;,]/).map((s) => s.trim()).filter(Boolean)
      : [want];

    let unverified = null;
    const failed = [];
    for (let i = 0; i < parts.length; i++) {
      if (i > 0) { openWidget(); await sleep(60); }  // some multiselects close after each pick
      const res = await selectOne(parts[i]);
      if (!res.ok) failed.push(parts[i]);
      else if (res.unverified) unverified = res.message;
    }

    if (failed.length === parts.length) {
      return { ok: false, message: `no option matches "${failed[0].slice(0, 40)}"` };
    }
    if (failed.length) {
      return { ok: true, message: `selected ${parts.length - failed.length}/${parts.length} — missing: ${failed.join(", ").slice(0, 60)}` };
    }
    if (unverified) {
      return { ok: true, message: unverified };
    }
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

  // Re-apply the data-jobsmith-fid stamp to an element located by a fallback,
  // so later steps (retry re-acquisition, the highlight overlay) can rely on
  // item.selector again even though the SPA re-render dropped the original.
  function restamp(el, item) {
    if (el && item.field_id) {
      try { el.setAttribute("data-jobsmith-fid", item.field_id); } catch (_) {}
    }
    return el;
  }

  function findElement(item) {
    // 1. The data-jobsmith-fid stamp snapshot.js applied — stable across most
    //    re-renders, and cheap to resolve.
    if (item.selector) {
      const el = deepQuerySelector(item.selector);
      if (el) return el;
    }
    // 2. The element's own id/name selector (snapshot's _human_selector). A
    //    React/Vue re-render frequently drops the injected data-jobsmith-fid
    //    while keeping the id/name — the common case for bespoke name/email
    //    widgets that made findElement return null before. Re-stamp on a hit.
    if (item.human_selector) {
      const el = deepQuerySelector(item.human_selector);
      if (el) return restamp(el, item);
    }
    // 3. Any element still carrying this name= (custom components with no id).
    if (item.name) {
      const byName = deepQuerySelector(
        `input[name="${CSS.escape(item.name)}"], textarea[name="${CSS.escape(item.name)}"], select[name="${CSS.escape(item.name)}"], [name="${CSS.escape(item.name)}"]`
      );
      if (byName) return restamp(byName, item);
    }
    return null;
  }

  function pickSelectOption(selectEl, wantedValue) {
    const want = (wantedValue || "").trim();
    if (!want) return null;
    const wantN = normText(want);
    for (const opt of selectEl.options) {
      if (normText(opt.value) === wantN || normText(opt.textContent) === wantN) return opt;
    }
    let best = null, bestScore = 0;
    for (const opt of selectEl.options) {
      if (isPlaceholderOption(opt.textContent)) continue;
      const s = Math.max(scoreAgainst(want, opt.textContent), scoreAgainst(want, opt.value));
      const optLen = (opt.textContent || "").trim().length;
      if (s > bestScore || (s === bestScore && best && optLen < (best.textContent || "").trim().length)) {
        best = opt; bestScore = s;
      }
    }
    return bestScore >= OPT_THRESHOLD ? best : null;
  }

  function pickRadioInGroup(anchorEl, name, wantedValue) {
    const want = (wantedValue || "").trim();
    if (!want) return null;
    const root = (anchorEl && anchorEl.getRootNode) ? anchorEl.getRootNode() : document;
    let group = [];
    if (name) {
      try { group = Array.from(root.querySelectorAll(`input[type="radio"][name="${CSS.escape(name)}"]`)); } catch (_) {}
    }
    if (!group.length && anchorEl) group = [anchorEl];  // unnamed standalone radio
    let best = null, bestScore = 0;
    for (const r of group) {
      const lab = labelTextFor(r) || r.value;
      const s = Math.max(scoreAgainst(want, lab), scoreAgainst(want, r.value));
      if (s > bestScore) { best = r; bestScore = s; }
    }
    return bestScore >= OPT_THRESHOLD ? best : null;
  }

  function labelTextFor(el) {
    const root = (el.getRootNode ? el.getRootNode() : document);
    if (el.id && root.querySelector) {
      const lab = root.querySelector(`label[for="${CSS.escape(el.id)}"]`);
      if (lab) return lab.textContent.trim();
    }
    if (el.getAttribute && el.getAttribute("aria-label")) return el.getAttribute("aria-label").trim();
    let p = el.parentElement;
    while (p && p.tagName !== "LABEL") p = p.parentElement;
    return p ? p.textContent.trim() : "";
  }

  function isTruthyAnswer(s) {
    return /^(y(es)?|true|1|on)$/i.test((s || "").trim());
  }

  // <input type=date> needs YYYY-MM-DD regardless of what the profile says.
  function normalizeDateValue(el, value) {
    const t = (el.type || "").toLowerCase();
    if (t !== "date" && t !== "month") return value;
    const v = (value || "").trim();
    let d = null;
    if (/^(immediate(ly)?|asap|now|today)$/i.test(v)) d = new Date();
    else {
      const parsed = new Date(v);
      if (!isNaN(parsed.getTime())) d = parsed;
    }
    if (!d) return value;
    const pad = (n) => String(n).padStart(2, "0");
    return t === "month"
      ? `${d.getFullYear()}-${pad(d.getMonth() + 1)}`
      : `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}`;
  }

  // Set a text/email/number/textarea/date/tel/url value and confirm it survives
  // the page's next render. Controlled inputs (react-hook-form, Formik, masked
  // fields) sometimes accept the synthetic input event, then snap the DOM value
  // back to empty on the following render — so a synchronous read reports a
  // "filled" that isn't real (the classic "detected but not filled"). We set,
  // wait a tick, re-read, and retry on a revert (up to 2 retries), re-acquiring
  // the element each round in case the framework replaced the node. Verification
  // happens BEFORE blur, so a validation-driven clear is observed, not hidden.
  // Returns { ok, actual }.
  async function setTextFieldWithRetry(item, firstEl, value) {
    let el = firstEl;
    let actual = "";
    for (let attempt = 0; attempt < 3; attempt++) {
      if (!el || !el.isConnected) el = findElement(item) || el;
      if (!el) break;
      try { el.focus(); } catch (_) {}
      fireFocus(el);
      nativeSet(el, value);
      fireInputEvents(el, value);
      await sleep(120);  // let a controlled re-render land before trusting it
      actual = (el.value || "");
      if (actual === value || (value && actual.includes(value))) {
        fireBlur(el);
        try { el.blur(); } catch (_) {}
        return { ok: true, actual };
      }
      // Reverted — the node may have been replaced; re-acquire and retry.
      el = findElement(item) || el;
    }
    return { ok: false, actual };
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
          const assign = () => {
            input.files = dt.files;
            input.dispatchEvent(new Event("input", { bubbles: true }));
            input.dispatchEvent(new Event("change", { bubbles: true }));
          };
          assign();
          // Best-effort: some dropzones (Greenhouse, Lever) listen for drop on a wrapper.
          const dropTarget = input.closest('[data-test-id*="drop"], .dropzone, [class*="drop"]') || input.parentElement;
          if (dropTarget && dropTarget !== input) {
            try {
              const dropEvt = new DragEvent("drop", { bubbles: true, cancelable: true, dataTransfer: dt });
              dropTarget.dispatchEvent(dropEvt);
            } catch (_) { /* DragEvent constructor may not accept dataTransfer in all browsers */ }
          }
          // Some uploaders clear the input while starting their own async
          // handling — one 250ms retry was too shallow for the hardened ones
          // (Workday, some Greenhouse). Poll for up to ~1.5s, re-assigning
          // each round, and only give up if the input still holds nothing.
          const deadline = Date.now() + 1500;
          while ((!input.files || input.files.length === 0) && Date.now() < deadline) {
            await sleep(150);
            if (!input.files || input.files.length === 0) assign();
          }
          if (!input.files || input.files.length === 0) {
            results.push({
              field_id: item.field_id,
              status: "failed",
              // The panel reads this as its cue to arm dropcatch (the drag
              // path works where a scripted assignment doesn't).
              message: "this uploader rejects scripted files — drag the tile onto the upload box",
            });
            continue;
          }
          const targetDesc = input.id ? `#${input.id}` : (input.name ? `[name=${input.name}]` : "file input");
          results.push({ field_id: item.field_id, status: "filled", message: `attached ${item.file_name} → ${targetDesc}` });
        } catch (e) {
          results.push({ field_id: item.field_id, status: "failed", message: `upload: ${e.message || e}` });
        }
        continue;
      }

      if (item._combobox || (item.field_type === "select" && el.tagName !== "SELECT")) {
        const out = await fillCombobox(el, item.value);
        if (!out.ok) { results.push({ field_id: item.field_id, status: "failed", message: out.message }); continue; }
        if (out.message) { results.push({ field_id: item.field_id, status: "low_confidence", message: out.message }); continue; }
        await sleep(60);  // let dependent fields (country → state) cascade
      } else if (item.field_type === "select") {
        const opt = pickSelectOption(el, item.value);
        if (!opt) { results.push({ field_id: item.field_id, status: "failed", message: "no matching option" }); continue; }
        nativeSet(el, opt.value);
        fireInputEvents(el);
        if (el.value !== opt.value) {
          results.push({ field_id: item.field_id, status: "failed", message: "select did not accept value" });
          continue;
        }
        await sleep(60);  // dependent dropdowns repopulate after change
      } else if (item.field_type === "radio") {
        const target = pickRadioInGroup(el, item.name || el.name, item.value);
        if (!target) { results.push({ field_id: item.field_id, status: "failed", message: "no matching radio" }); continue; }
        target.click();
        if (!target.checked) {
          // Custom widgets sometimes intercept the click — force it through.
          target.checked = true;
          fireInputEvents(target);
        }
        if (!target.checked) {
          results.push({ field_id: item.field_id, status: "failed", message: "radio did not select" });
          continue;
        }
        await sleep(40);
      } else if (item.field_type === "checkbox") {
        const want = isTruthyAnswer(item.value);
        if (el.checked !== want) el.click();
        if (el.checked !== want) {
          el.checked = want;
          fireInputEvents(el);
        }
        if (el.checked !== want) {
          results.push({ field_id: item.field_id, status: "failed", message: "checkbox did not toggle" });
          continue;
        }
      } else if (el.isContentEditable || (el.getAttribute && ["", "true"].includes(el.getAttribute("contenteditable")))) {
        fireFocus(el);
        el.textContent = item.value;
        fireInputEvents(el, item.value);
        fireBlur(el);
        if ((el.textContent || "").trim() !== (item.value || "").trim()) {
          results.push({ field_id: item.field_id, status: "failed", message: "editor did not accept text" });
          continue;
        }
      } else {
        const value = normalizeDateValue(el, item.value);
        // Verify-and-retry: set, wait, re-read, retry on a controlled-input
        // revert. Only reports ok if the value survives the post-delay re-read.
        const res = await setTextFieldWithRetry(item, el, value);
        if (!res.ok) {
          results.push({
            field_id: item.field_id,
            status: "failed",
            message: res.actual ? `reverted to "${res.actual.slice(0, 40)}"` : "value reverted",
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
