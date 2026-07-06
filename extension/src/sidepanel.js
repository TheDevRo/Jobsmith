// sidepanel.js — scan + autofill the active tab.

const $ = (id) => document.getElementById(id);
const ext = (typeof browser !== "undefined") ? browser : chrome;

let lastMapping = null;     // { tab, url, descriptors, values, deep } — cached from most recent scan
let currentJob = null;      // { id, title, company, url }
let cachedFiles = { resume: null, cover: null };  // pre-fetched File objects
let autoScanOn = true;      // gates background scan/poll; synced from config
let autoFillOn = false;     // fill right after each auto-scan; synced from config
let lastAutoFillKey = "";   // tabId|url guard so auto-fill fires once per page

function setStatus(msg, cls = "") {
  const s = $("status");
  s.textContent = msg;
  s.className = cls;
}

let toastTimer = null;
function showToast(msg) {
  const t = $("toast");
  if (!t) return;
  t.innerHTML = `<span class="tick">✓</span>${""}`;
  t.appendChild(document.createTextNode(msg));
  t.classList.add("show");
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => t.classList.remove("show"), 1600);
}

async function copyToClipboard(text) {
  try {
    await navigator.clipboard.writeText(text);
    return true;
  } catch (_) {
    // Fallback for contexts where the async clipboard API is unavailable.
    try {
      const ta = document.createElement("textarea");
      ta.value = text;
      ta.style.position = "fixed";
      ta.style.opacity = "0";
      document.body.appendChild(ta);
      ta.select();
      const ok = document.execCommand("copy");
      ta.remove();
      return ok;
    } catch (e) {
      return false;
    }
  }
}

// When mounted as an in-page docked panel (Firefox Assist overlay), the
// panel is pinned to its host tab via ?tabId= — "active tab in the current
// window" would be wrong whenever the host tab isn't focused.
const PINNED_TAB_ID = (() => {
  const v = new URLSearchParams(location.search).get("tabId");
  const n = v ? parseInt(v, 10) : NaN;
  return Number.isFinite(n) ? n : null;
})();

// ---------------------------------------------------------------------------
// Privileged API access.
//
// Chrome grants extension pages full APIs even when iframed inside a web
// page. Firefox does NOT: the docked overlay gets content-script-level
// privileges only — ext.tabs / ext.scripting are undefined there. Route
// those calls through the background script (always fully privileged) via
// a small RPC over runtime.sendMessage, which IS available in the iframe.
// (?forceRpc=1 exercises the RPC path in any browser, for tests.)
// ---------------------------------------------------------------------------

const HAS_DIRECT_APIS =
  !new URLSearchParams(location.search).has("forceRpc") &&
  !!(ext.tabs && ext.tabs.query && ext.scripting && ext.scripting.executeScript);

function bgRpc(method, ...args) {
  return new Promise((resolve, reject) => {
    const handle = (resp) => {
      if (!resp) {
        const le = ext.runtime.lastError;
        reject(new Error(le ? le.message : "no response from background"));
      } else if (!resp.ok) {
        reject(new Error(resp.error));
      } else {
        resolve(resp.result);
      }
    };
    try {
      const p = ext.runtime.sendMessage({ type: "jobsmith-rpc", method, args });
      if (p && typeof p.then === "function") p.then(handle, reject);
    } catch (e) {
      reject(e);
    }
  });
}

function tabsQuery(info) {
  if (HAS_DIRECT_APIS) return new Promise((r) => ext.tabs.query(info, r));
  return bgRpc("tabs.query", info);
}

function tabsGet(tabId) {
  if (HAS_DIRECT_APIS) {
    return new Promise((resolve) => {
      try {
        ext.tabs.get(tabId, (t) => resolve(ext.runtime.lastError ? null : t));
      } catch (_) { resolve(null); }
    });
  }
  return bgRpc("tabs.get", tabId).catch(() => null);
}

function execFiles(target, files) {
  if (HAS_DIRECT_APIS) return ext.scripting.executeScript({ target, files });
  return bgRpc("scripting.executeScript", { target, files });
}

// Call a well-known window.__jobsmith* function in the page's isolated world
// with JSON args (functions can't cross the RPC boundary, names can).
function execCall(target, fnName, fnArgs) {
  if (HAS_DIRECT_APIS) {
    return ext.scripting.executeScript({
      target,
      func: (name, a) => { const f = window[name]; return f ? f.apply(null, a) : null; },
      args: [fnName, fnArgs || []],
    });
  }
  return bgRpc("scripting.callInPage", { target, fnName, fnArgs: fnArgs || [] });
}

async function activeTab() {
  if (PINNED_TAB_ID != null) {
    const tab = await tabsGet(PINNED_TAB_ID);
    if (tab) return tab;
    // Pinned tab was closed — fall through to normal behavior.
  }
  const tabs = await tabsQuery({ active: true, currentWindow: true });
  return tabs && tabs[0];
}

async function snapshotActiveTab() {
  const tab = await activeTab();
  if (!tab || !tab.id) throw new Error("No active tab");
  // Deep mode: inject into every frame so embedded ATS forms (Greenhouse,
  // Lever, Ashby) are scanned. Off by default — heavy pages have many
  // third-party iframes that slow injection. Toggle in popup settings.
  const { deepScan } = await Jobsmith.jobsmithGetConfig();

  async function grab(allFrames) {
    const target = allFrames ? { tabId: tab.id, allFrames: true } : { tabId: tab.id };
    const results = await execFiles(target, ["common/snapshot.js"]);
    if (!results || !results.length) {
      throw new Error("Snapshot returned nothing (page may block scripts)");
    }
    const merged = { url: tab.url || "", fields: [] };
    for (const r of results) {
      const snap = r && r.result;
      if (!snap || !Array.isArray(snap.fields)) continue;
      if (!merged.url && snap.url) merged.url = snap.url;
      const fid = typeof r.frameId === "number" ? r.frameId : 0;
      for (const f of snap.fields) {
        // Disambiguate field_ids across frames (the DOM `data-jobsmith-fid` stamp
        // is scoped to its own frame, so the original selector still works
        // when we route the fill back to that frameId).
        const taggedId = fid === 0 ? f.field_id : `f${fid}__${f.field_id}`;
        merged.fields.push({ ...f, field_id: taggedId, _frameId: fid });
      }
    }
    return merged;
  }

  let deep = deepScan;
  let merged = await grab(deepScan);
  // Auto-fallback: company career pages often embed the real ATS form
  // (Greenhouse/Lever) in an iframe. If the top frame has next to no
  // fields, rescan all frames even with deep-scan off.
  if (!deep && merged.fields.length < 3) {
    try {
      const deepMerged = await grab(true);
      if (deepMerged.fields.length > merged.fields.length) {
        merged = deepMerged;
        deep = true;
      }
    } catch (_) { /* keep the shallow result */ }
  }
  return { tab, snapshot: merged, deep };
}

async function injectFillRuntime(tabId, deep) {
  const target = deep ? { tabId, allFrames: true } : { tabId };
  await execFiles(target, ["common/fill.js"]);
}

async function runFill(tabId, items, { clearOnly = false } = {}) {
  // Items can target multiple frames; group by _frameId and route each
  // batch to the right frame in parallel.
  const groups = new Map();
  for (const it of items) {
    const fid = typeof it._frameId === "number" ? it._frameId : 0;
    if (!groups.has(fid)) groups.set(fid, []);
    groups.get(fid).push(it);
  }
  const perFrame = await Promise.all(
    Array.from(groups, async ([fid, group]) => {
      try {
        const res = await execCall(
          { tabId, frameIds: [fid] },
          "__jobsmithFillAndHighlight",
          [group, { clearOnly }]
        );
        return res && res[0] && res[0].result;
      } catch (e) {
        console.warn(`fill in frame ${fid} failed:`, e);
        return null;
      }
    })
  );
  const combined = { results: [], highlighted: 0 };
  for (const out of perFrame) {
    if (!out) continue;
    if (Array.isArray(out.results)) combined.results.push(...out.results);
    if (typeof out.highlighted === "number") combined.highlighted += out.highlighted;
  }
  return combined;
}

async function buildFillItems(descriptors, values) {
  const descById = Object.fromEntries(descriptors.map(d => [d.field_id, d]));
  // Pre-read upload bytes once (structured-clonable Uint8Array per item).
  const fileBytesCache = {};
  async function bytesFor(kind) {
    if (fileBytesCache[kind] !== undefined) return fileBytesCache[kind];
    const f = kind === "cover_letter" ? cachedFiles.cover : cachedFiles.resume;
    if (!f) { fileBytesCache[kind] = null; return null; }
    const buf = await f.arrayBuffer();
    // Plain Array (not Uint8Array) so it survives JSON serialization in
    // scripting.executeScript args. fill.js wraps it back into a Uint8Array.
    fileBytesCache[kind] = {
      bytes: Array.from(new Uint8Array(buf)),
      name: f.name,
      mime: f.type || "application/octet-stream",
    };
    return fileBytesCache[kind];
  }
  const items = [];
  for (const v of values) {
    const d = descById[v.field_id] || {};
    const item = {
      field_id:   v.field_id,
      selector:   d._selector || "",
      name:       d.name || "",
      value:      v.value || "",
      action:     v.action || "fill",
      field_type: d.field_type || "text",
      confidence: typeof v.confidence === "number" ? v.confidence : 1.0,
      source:     v.source || "",
      options:    d.options || null,
      required:   !!d.required,
      _frameId:   typeof d._frameId === "number" ? d._frameId : 0,
      _combobox:  !!d._combobox,
    };
    if (item.action === "upload") {
      const blob = await bytesFor(item.value);
      if (blob) {
        item.file_bytes = blob.bytes;
        item.file_name = blob.name;
        item.file_mime = blob.mime;
      }
    }
    items.push(item);
  }
  return items;
}

function renderFields(values, descriptors, fillResults) {
  const root = $("fields");
  root.innerHTML = "";
  if (!values.length) {
    root.innerHTML = '<div class="empty">No fields detected on this page.</div>';
    return;
  }
  const descById   = Object.fromEntries(descriptors.map(d => [d.field_id, d]));
  const statusById = Object.fromEntries((fillResults || []).map(r => [r.field_id, r]));

  for (const v of values) {
    const d = descById[v.field_id] || {};
    const fr = statusById[v.field_id];
    const el = document.createElement("div");
    el.className = `field src-${v.source || "skip"}`;
    const label = d.label || d.name || v.field_id;
    const valDisplay = v.action === "skip"
      ? "(skipped)"
      : (v.value || "(empty)");
    const statusLine = fr
      ? `<span class="status-${fr.status}">${fr.status}${fr.message ? ` — ${fr.message}` : ""}</span>`
      : "";

    el.innerHTML = `
      <div class="label"></div>
      <div class="meta"></div>
      <div class="value"></div>
      <div class="meta">${statusLine}</div>`;
    el.querySelector(".label").textContent = label;
    el.querySelector(".meta").textContent =
      `${d.field_type || "text"} • ${v.source} • conf ${v.confidence.toFixed(2)}` +
      (d.required ? " • required" : "");
    el.querySelector(".value").textContent = valDisplay;

    // Click anywhere on the card to copy its value to the clipboard. Only
    // for fields that actually carry a value (not skipped / empty).
    const copyVal = v.action === "skip" ? "" : (v.value || "");
    if (copyVal) {
      el.classList.add("copyable");
      el.title = "Click to copy";
      el.addEventListener("click", async () => {
        const ok = await copyToClipboard(copyVal);
        if (ok) {
          el.classList.add("copied");
          setTimeout(() => el.classList.remove("copied"), 1200);
          const preview = copyVal.length > 40 ? copyVal.slice(0, 40) + "…" : copyVal;
          showToast(`Copied: ${preview}`);
        } else {
          showToast("Copy failed");
        }
      });
    }
    root.appendChild(el);
  }
}

async function doScan() {
  $("scan").disabled = true;
  setStatus("Scanning page…");
  try {
    const { tab, snapshot, deep } = await snapshotActiveTab();
    if (!snapshot.fields.length) {
      setStatus("No visible form fields on this page.", "warn");
      renderFields([], []);
      lastMapping = null;
      return null;
    }
    setStatus(`Found ${snapshot.fields.length} field(s). Asking backend to map…`);
    const resp = await Jobsmith.jobsmithFetch("/api/ext/scan", {
      method: "POST",
      body: {
        url: snapshot.url,
        job_id: currentJob ? currentJob.id : null,
        fields: snapshot.fields,
      },
    });
    setStatus(`Mapped ${resp.count} field(s).`, "ok");
    lastMapping = { tab, url: snapshot.url, descriptors: snapshot.fields, values: resp.fields, deep };
    renderFields(resp.fields, snapshot.fields, null);
    return lastMapping;
  } catch (e) {
    console.error(e);
    if (e.status === 401) setStatus("Token rejected. Open Settings and update it.", "err");
    else setStatus(`Error: ${e.message}`, "err");
    return null;
  } finally {
    $("scan").disabled = false;
  }
}

async function doAutofill() {
  $("autofill").disabled = true;
  setStatus("Preparing autofill…");
  try {
    let mapping = lastMapping;
    if (!mapping || !mapping.tab) {
      mapping = await doScan();
      if (!mapping) return;
    }
    setStatus("Filling fields…");
    const items = await buildFillItems(mapping.descriptors, mapping.values);
    await injectFillRuntime(mapping.tab.id, mapping.deep);
    const out = await runFill(mapping.tab.id, items);
    if (!out) throw new Error("Fill script returned nothing");

    const counts = out.results.reduce((acc, r) => {
      acc[r.status] = (acc[r.status] || 0) + 1;
      return acc;
    }, {});
    const summary =
      `Filled ${counts.filled || 0}` +
      (counts.low_confidence ? ` (+${counts.low_confidence} low-conf)` : "") +
      (counts.skipped ? `, skipped ${counts.skipped}` : "") +
      (counts.failed ? `, failed ${counts.failed}` : "") +
      (counts.not_found ? `, missing ${counts.not_found}` : "");
    setStatus(summary, counts.failed || counts.not_found ? "warn" : "ok");
    renderFields(mapping.values, mapping.descriptors, out.results);
  } catch (e) {
    console.error(e);
    setStatus(`Autofill error: ${e.message}`, "err");
  } finally {
    $("autofill").disabled = false;
  }
}

async function doClearHighlights() {
  try {
    const tab = await activeTab();
    if (!tab || !tab.id) return;
    // Clear in every frame directly — runFill() groups by item frame and
    // executes nothing for an empty item list.
    await injectFillRuntime(tab.id, true);
    await execCall({ tabId: tab.id, allFrames: true }, "__jobsmithFillAndHighlight", [[], { clearOnly: true }]);
    setStatus("Highlights cleared.");
  } catch (e) {
    setStatus(`Clear error: ${e.message}`, "err");
  }
}

// ---- Job loading + drag tiles ------------------------------------------

function setTileState(tile, { label, hint, file, downloadBtn }) {
  tile.querySelector(".name").textContent = label;
  tile.querySelector(".hint").textContent = hint;
  if (file) {
    tile.classList.remove("disabled");
    tile.setAttribute("draggable", "true");
    tile._jobsmithFile = file;
    if (downloadBtn) { downloadBtn.disabled = false; downloadBtn._jobsmithFile = file; }
  } else {
    tile.classList.add("disabled");
    tile.setAttribute("draggable", "false");
    tile._jobsmithFile = null;
    if (downloadBtn) { downloadBtn.disabled = true; downloadBtn._jobsmithFile = null; }
  }
}

function downloadFile(file) {
  if (!file) return;
  const url = URL.createObjectURL(file);
  const a = document.createElement("a");
  a.href = url;
  a.download = file.name;
  document.body.appendChild(a);
  a.click();
  a.remove();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
}

// ---- Drag-to-attach ------------------------------------------------------
// Browsers don't reliably deliver a programmatic File dragged from an
// extension page into a web page (Firefox drops an empty payload — the file
// "disappears"). So the drag is a gesture only: while it's in flight we arm
// common/dropcatch.js in the page, which intercepts the drop, picks the file
// input nearest the cursor, and messages back — then we attach the real
// bytes through the same runFill upload path autofill uses.

let dropCatchTabId = null;

async function armDropCatch(kind) {
  try {
    const tab = await activeTab();
    if (!tab || !tab.id) return;
    dropCatchTabId = tab.id;
    const target = { tabId: tab.id, allFrames: true };  // ATS forms live in iframes too
    await execFiles(target, ["common/dropcatch.js"]);
    await execCall(target, "__jobsmithArmDropCatch", [kind]);
    setStatus("Drop it on the highlighted upload area…");
  } catch (e) {
    // Surface it — a silent console line here cost a debugging round-trip.
    setStatus(`Drag setup failed: ${e.message}`, "err");
  }
}

async function disarmDropCatch() {
  if (dropCatchTabId == null) return;
  const tabId = dropCatchTabId;
  dropCatchTabId = null;
  try {
    await execCall({ tabId, allFrames: true }, "__jobsmithDisarmDropCatch", []);
  } catch (_) { /* tab gone — nothing to disarm */ }
}

// Pull the recorded drop result out of every frame of the tab. The page
// records it in dropcatch.js when the drop lands (drop always fires before
// dragend, so by the time we collect, the result is there). Pulling via
// executeScript avoids runtime messaging, whose delivery to an extension
// page iframed in a web page is unreliable in Firefox.
async function collectDropResult(tabId) {
  const results = await execCall({ tabId, allFrames: true }, "__jobsmithTakeDropResult", []);
  for (const r of results || []) {
    if (r && r.result) {
      return { ...r.result, frameId: typeof r.frameId === "number" ? r.frameId : 0 };
    }
  }
  return null;
}

async function handleFileDrop(tabId, drop) {
  if (!drop.ok) {
    setStatus(`Drop failed: ${drop.reason || "no file input found"}`, "err");
    return;
  }
  const file = drop.kind === "cover_letter" ? cachedFiles.cover : cachedFiles.resume;
  if (!file) {
    setStatus("No file loaded — load a job first.", "warn");
    return;
  }
  setStatus(`Attaching ${file.name}…`);
  try {
    const buf = await file.arrayBuffer();
    const item = {
      field_id: drop.fid,
      selector: `[data-jobsmith-fid="${CSS.escape(drop.fid)}"]`,
      value: drop.kind,
      action: "upload",
      field_type: "file",
      confidence: 1,
      source: "drop",
      _frameId: drop.frameId,
      file_bytes: Array.from(new Uint8Array(buf)),
      file_name: file.name,
      file_mime: file.type || "application/octet-stream",
    };
    await injectFillRuntime(tabId, true);
    const out = await runFill(tabId, [item]);
    const r = out && out.results && out.results[0];
    if (r && r.status === "filled") {
      setStatus(`Attached ${file.name}.`, "ok");
      showToast(`Attached ${file.name}`);
    } else {
      setStatus(`Attach failed: ${(r && r.message) || "unknown error"}`, "err");
    }
  } catch (e) {
    setStatus(`Attach error: ${e.message}`, "err");
  }
}

function attachDragHandlers(tile, kind) {
  tile.addEventListener("dragstart", (e) => {
    const file = tile._jobsmithFile;
    if (!file) { e.preventDefault(); return; }
    e.dataTransfer.effectAllowed = "copy";
    try {
      e.dataTransfer.items.add(file);
    } catch (_) {
      // Older Firefox path: setData fallback (rarely needed)
      e.dataTransfer.setData("application/octet-stream", file.name);
    }
    // Hint for sites that listen for DownloadURL drops
    const mime = file.type || "application/octet-stream";
    e.dataTransfer.setData("DownloadURL", `${mime}:${file.name}:about:blank`);
    armDropCatch(kind);
  });
  tile.addEventListener("dragend", async () => {
    // drop fired before dragend (if it fired at all) — pull the recorded
    // result out of the page and attach.
    const tabId = dropCatchTabId;
    if (tabId == null) return;
    try {
      const drop = await collectDropResult(tabId);
      if (drop) {
        await handleFileDrop(tabId, drop);
      } else {
        setStatus("Drag cancelled — drop onto the upload area of the form.", "warn");
      }
    } catch (e) {
      setStatus(`Drop check failed: ${e.message}`, "err");
    } finally {
      disarmDropCatch();
    }
  });
}

async function loadJob(jobId) {
  if (!jobId) {
    setStatus("Enter a job ID.", "warn");
    return;
  }
  setStatus(`Loading job ${jobId}…`);
  $("jobContext").innerHTML = "";
  cachedFiles = { resume: null, cover: null };
  setTileState($("resumeTile"),  { label: "Resume",       hint: "loading…", file: null, downloadBtn: $("resumeDownload") });
  setTileState($("coverTile"),   { label: "Cover Letter", hint: "loading…", file: null, downloadBtn: $("coverDownload") });

  try {
    currentJob = await Jobsmith.jobsmithFetch(`/api/ext/job/${encodeURIComponent(jobId)}`);
  } catch (e) {
    if (e.status === 404) setStatus(`Job ${jobId} not found.`, "err");
    else setStatus(`Load error: ${e.message}`, "err");
    setTileState($("resumeTile"), { label: "Resume",       hint: "load a job", file: null, downloadBtn: $("resumeDownload") });
    setTileState($("coverTile"),  { label: "Cover Letter", hint: "load a job", file: null, downloadBtn: $("coverDownload") });
    return;
  }

  ext.storage.local.set({ lastJobId: jobId });

  const ctx = $("jobContext");
  ctx.innerHTML = "";
  const title = `${currentJob.title || "(no title)"} at ${currentJob.company || "(no company)"}`;
  if (currentJob.url) {
    const a = document.createElement("a");
    a.href = currentJob.url; a.target = "_blank"; a.textContent = title;
    ctx.appendChild(a);
  } else {
    ctx.textContent = title;
  }

  const results = await Promise.allSettled([
    Jobsmith.jobsmithFetchFile(`/api/ext/resume/${encodeURIComponent(jobId)}`,       `${jobId}_resume.docx`),
    Jobsmith.jobsmithFetchFile(`/api/ext/cover-letter/${encodeURIComponent(jobId)}`, `${jobId}_cover_letter.docx`),
  ]);

  if (results[0].status === "fulfilled") {
    cachedFiles.resume = results[0].value;
    setTileState($("resumeTile"), { label: "Resume.docx", hint: "drag or download", file: cachedFiles.resume, downloadBtn: $("resumeDownload") });
  } else {
    setTileState($("resumeTile"), { label: "Resume", hint: "not tailored yet", file: null, downloadBtn: $("resumeDownload") });
  }
  if (results[1].status === "fulfilled") {
    cachedFiles.cover = results[1].value;
    setTileState($("coverTile"), { label: "CoverLetter.docx", hint: "drag or download", file: cachedFiles.cover, downloadBtn: $("coverDownload") });
  } else {
    setTileState($("coverTile"), { label: "Cover Letter", hint: "not tailored yet", file: null, downloadBtn: $("coverDownload") });
  }

  const ok = results.filter(r => r.status === "fulfilled").length;
  setStatus(`Loaded ${ok} of 2 files. Drag the tile onto the ATS file input.`, ok ? "ok" : "warn");
}

$("loadJob").addEventListener("click", () => loadJob($("jobId").value.trim()));
$("jobId").addEventListener("keydown", (e) => {
  if (e.key === "Enter") loadJob($("jobId").value.trim());
});
attachDragHandlers($("resumeTile"), "resume");
attachDragHandlers($("coverTile"), "cover_letter");

$("resumeDownload").addEventListener("click", () => downloadFile($("resumeDownload")._jobsmithFile));
$("coverDownload").addEventListener("click",  () => downloadFile($("coverDownload")._jobsmithFile));

// Restore last-used job ID
ext.storage.local.get(["lastJobId"], (out) => {
  if (out && out.lastJobId) $("jobId").value = out.lastJobId;
});

$("scan").addEventListener("click", doScan);
$("autofill").addEventListener("click", doAutofill);
$("clear").addEventListener("click", doClearHighlights);

// Auto-scan on/off switch. Persists to config and gates all background
// scanning/polling. Turning it on kicks an immediate scan so the panel
// catches up with the current tab.
$("autoScanToggle").addEventListener("change", async (e) => {
  autoScanOn = e.target.checked;
  await Jobsmith.jobsmithSetConfig({ autoScan: autoScanOn });
  if (autoScanOn) {
    setStatus("Auto-scan on.", "ok");
    checkActiveJobHint(true);
    autoScanIfReady();
  } else {
    setStatus("Auto-scan off — use the buttons manually.");
  }
});

// Auto-fill on/off switch. Requires auto-scan to be useful, but is stored
// independently so it survives auto-scan being toggled.
$("autoFillToggle").addEventListener("change", async (e) => {
  autoFillOn = e.target.checked;
  await Jobsmith.jobsmithSetConfig({ autoFill: autoFillOn });
  if (autoFillOn) {
    lastAutoFillKey = "";  // allow an immediate fill of the current page
    setStatus("Auto-fill on — pages fill right after scanning.", "ok");
    autoScanIfReady();
  } else {
    setStatus("Auto-fill off.");
  }
});

// Keep the toggles in sync if changed elsewhere (e.g. another panel window).
if (ext.storage && ext.storage.onChanged) {
  ext.storage.onChanged.addListener((changes, area) => {
    if (area !== "local") return;
    if (changes.autoScan) {
      autoScanOn = changes.autoScan.newValue !== false;
      $("autoScanToggle").checked = autoScanOn;
    }
    if (changes.autoFill) {
      autoFillOn = changes.autoFill.newValue === true;
      $("autoFillToggle").checked = autoFillOn;
    }
  });
}

$("markApplied").addEventListener("click", doMarkApplied);

async function doMarkApplied() {
  if (!currentJob || !currentJob.id) {
    setStatus("Load a job first.", "warn");
    return;
  }
  if (!confirm("Mark this job as manually applied?")) return;
  setStatus("Marking as applied…");
  try {
    const resp = await Jobsmith.jobsmithFetch(`/api/ext/job/${encodeURIComponent(currentJob.id)}/mark-applied`, {
      method: "POST",
    });
    const n = resp && resp.applications_updated ? resp.applications_updated : 0;
    setStatus(
      n > 0
        ? `Marked job ${currentJob.id} applied (removed ${n} from review queue).`
        : `Marked job ${currentJob.id} applied.`,
      "ok",
    );
  } catch (e) {
    if (e.status === 404) setStatus(`Job ${currentJob.id} not found.`, "err");
    else setStatus(`Error: ${e.message}`, "err");
  }
}

async function checkActiveJobHint(silent = false) {
  if (!autoScanOn) return;
  // Auto-bind to whichever job the user most recently clicked "Open Job URL"
  // on in the Jobsmith UI. Best-effort; never throws to the caller.
  try {
    const { backendUrl } = await Jobsmith.jobsmithGetConfig();
    const resp = await fetch(backendUrl + "/api/extension/active-job");
    if (!resp.ok) return;
    const data = await resp.json();
    const jobId = data && data.job_id ? String(data.job_id) : "";
    if (!jobId) return;
    if (currentJob && String(currentJob.id) === jobId) return; // already bound
    $("jobId").value = jobId;
    await loadJob(jobId);
    if (!silent) setStatus("Auto-bound from Jobsmith click.", "ok");
  } catch (e) {
    console.debug("active-job hint check failed:", e);
  }
}

async function autoScanIfReady() {
  if (!autoScanOn) return;
  try {
    const tab = await activeTab();
    if (!tab || !tab.id || !tab.url) return;
    if (/^(chrome|about|file|edge|moz-extension|chrome-extension):/.test(tab.url)) return;
    if (lastMapping && lastMapping.tab && lastMapping.tab.id === tab.id && lastMapping.url === tab.url) return;
    setStatus("Auto-scanning…");
    const mapping = await doScan();
    if (mapping && mapping.values && mapping.values.length) {
      // Hands-off mode: fill right away, but only once per tab+URL so a
      // re-scan of the same page (focus changes, polling) can't re-fill
      // fields the user has since corrected.
      if (autoFillOn) {
        const key = `${mapping.tab.id}|${mapping.url}`;
        if (key !== lastAutoFillKey) {
          lastAutoFillKey = key;
          setStatus(`${mapping.values.length} fields mapped — auto-filling…`);
          await doAutofill();
          return;
        }
      }
      setStatus(`${mapping.values.length} fields ready — click Autofill.`, "ok");
    }
  } catch (e) {
    console.debug("autoScanIfReady failed:", e);
  }
}

window.addEventListener("focus", () => {
  checkActiveJobHint(false);
  autoScanIfReady();
});

if (ext.tabs && ext.tabs.onUpdated) {
  ext.tabs.onUpdated.addListener((tabId, changeInfo, tab) => {
    if (changeInfo.status !== "complete") return;
    if (PINNED_TAB_ID != null) {
      if (tabId !== PINNED_TAB_ID) return;  // pinned mode: track only the host tab
    } else if (!tab || !tab.active) {
      return;
    }
    checkActiveJobHint(true);
    autoScanIfReady();
  });
}

// Fire when the user switches tabs — onUpdated alone misses this.
if (ext.tabs && ext.tabs.onActivated) {
  ext.tabs.onActivated.addListener(() => {
    checkActiveJobHint(true);
    autoScanIfReady();
  });
}

// SPA / pushState navigations don't always trip tabs.onUpdated(complete);
// webNavigation.onCommitted catches them.
if (ext.webNavigation && ext.webNavigation.onCommitted) {
  ext.webNavigation.onCommitted.addListener(async (details) => {
    if (details.frameId !== 0) return;
    try {
      const tab = await activeTab();
      if (!tab || tab.id !== details.tabId) return;
      autoScanIfReady();
    } catch { /* ignore */ }
  });
}

// Belt-and-suspenders: pick up backend-signaled job hints (Apply Assist /
// Open Job URL clicks in the Jobsmith UI) even if no tab event fires. Cheap
// JSON GET; paused while the panel isn't visible.
setInterval(() => {
  if (document.visibilityState !== "visible") return;
  checkActiveJobHint(true);
}, 3000);

(async function init() {
  // Reflect the saved auto-scan preference before any background work runs.
  try {
    const cfg = await Jobsmith.jobsmithGetConfig();
    autoScanOn = cfg.autoScan;
    autoFillOn = cfg.autoFill;
    $("autoScanToggle").checked = autoScanOn;
    $("autoFillToggle").checked = autoFillOn;
  } catch { /* defaults: scan on, fill off */ }

  try {
    await Jobsmith.jobsmithHealth();
  } catch {
    setStatus("Backend not reachable. Open Settings.", "err");
    return;
  }
  try {
    await Jobsmith.jobsmithFetch("/api/ext/profile");
    setStatus("Ready. Click Scan, then Autofill.");
  } catch (e) {
    setStatus(e.status === 401 ? "Token missing or invalid — open Settings." : `Error: ${e.message}`, "err");
    return;
  }
  checkActiveJobHint(true);
  autoScanIfReady();
})();
