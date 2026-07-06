// Functional test of the fill.js combobox driver in jsdom: typeahead,
// button widgets, short-query retries, verification, and multi-select.
const { loadDom, evalScript, report } = require("./helpers");

const html = `<!DOCTYPE html><html><body>
  <!-- typeable react-select style -->
  <input id="cb1" role="combobox" aria-autocomplete="list" data-jobsmith-fid="cb1">
  <div id="menu1"></div>

  <!-- Workday-style button widget: options appear on click, no typing -->
  <button id="cb2" aria-haspopup="listbox" data-jobsmith-fid="cb2">Select One</button>
  <div id="menu2"></div>

  <!-- async lookup that only answers short queries (Workday location search) -->
  <input id="cb3" role="combobox" aria-autocomplete="list" data-jobsmith-fid="cb3">
  <div id="menu3"></div>

  <!-- button widget that never updates its text (verification must flag it) -->
  <button id="cb4" aria-haspopup="listbox" data-jobsmith-fid="cb4">Select One</button>
  <div id="menu4"></div>

  <!-- multi-select skills picker -->
  <input id="cb5" role="combobox" aria-multiselectable="true" data-jobsmith-fid="cb5">
  <div id="menu5"></div>
</body></html>`;

const dom = loadDom(html);
const { window } = dom;
const doc = window.document;
evalScript(window, "common/fill.js");

const picked = {};

function renderOptions(menuId, labels, pickKey) {
  const menu = doc.getElementById(menuId);
  menu.innerHTML = "";
  for (const label of labels) {
    const o = doc.createElement("div");
    o.setAttribute("role", "option");
    o.textContent = label;
    o.addEventListener("click", () => { picked[pickKey] = label; menu.innerHTML = ""; });
    menu.appendChild(o);
  }
}

// cb1: filters a static list as you type
const CB1_OPTS = ["Alabama", "Texas", "Tennessee"];
doc.getElementById("cb1").addEventListener("input", (e) => {
  const q = (e.target.value || "").toLowerCase();
  renderOptions("menu1", CB1_OPTS.filter(o => o.toLowerCase().includes(q)), "cb1");
});

// cb2: button widget opens full list on click; selection updates button text
doc.getElementById("cb2").addEventListener("click", () => {
  renderOptions("menu2", ["Mobile", "Home", "Work"], "cb2");
  for (const o of doc.getElementById("menu2").querySelectorAll('[role="option"]')) {
    o.addEventListener("click", () => { doc.getElementById("cb2").textContent = o.textContent; });
  }
});

// cb3: only answers queries of <= 3 chars (async lookup quirk)
doc.getElementById("cb3").addEventListener("input", (e) => {
  const q = (e.target.value || "").toLowerCase();
  if (!q || q.length > 3) { doc.getElementById("menu3").innerHTML = ""; return; }
  renderOptions("menu3", ["Austin, TX, United States"], "cb3");
});

// cb4: button widget whose text never changes (selection can't be verified)
doc.getElementById("cb4").addEventListener("click", () => {
  renderOptions("menu4", ["Alpha", "Beta"], "cb4");
});

// cb5: multi-select — options stay available across picks
const cb5picked = [];
doc.getElementById("cb5").addEventListener("input", (e) => {
  const q = (e.target.value || "").toLowerCase();
  const all = ["Python", "React", "Go"].filter(o => !q || o.toLowerCase().includes(q));
  const menu = doc.getElementById("menu5");
  menu.innerHTML = "";
  for (const label of all) {
    const o = doc.createElement("div");
    o.setAttribute("role", "option");
    o.textContent = label;
    o.addEventListener("click", () => cb5picked.push(label));
    menu.appendChild(o);
  }
});

const items = [
  { field_id: "cb1", selector: '[data-jobsmith-fid="cb1"]', value: "Texas", action: "select", field_type: "select", confidence: 0.95, _combobox: true },
  { field_id: "cb2", selector: '[data-jobsmith-fid="cb2"]', value: "Mobile", action: "select", field_type: "select", confidence: 0.95, _combobox: true },
  { field_id: "cb3", selector: '[data-jobsmith-fid="cb3"]', value: "Austin, TX, United States", action: "select", field_type: "select", confidence: 0.95, _combobox: true },
  { field_id: "cb4", selector: '[data-jobsmith-fid="cb4"]', value: "Alpha", action: "select", field_type: "select", confidence: 0.95, _combobox: true },
  { field_id: "cb5", selector: '[data-jobsmith-fid="cb5"]', value: "Python, React", action: "select", field_type: "select", confidence: 0.95, _combobox: true },
];

(async () => {
  const out = await window.__jobsmithFillAndHighlight(items, {});
  const byId = Object.fromEntries(out.results.map(r => [r.field_id, r]));

  const fail = report([
    ["typeahead picks Texas", picked.cb1 === "Texas" && byId.cb1.status === "filled"],
    ["button widget picks Mobile (verified)", picked.cb2 === "Mobile" && byId.cb2.status === "filled"],
    ["short-query retry finds Austin", picked.cb3 === "Austin, TX, United States" && byId.cb3.status === "filled"],
    ["unverifiable widget flagged low-conf", picked.cb4 === "Alpha" && byId.cb4.status === "low_confidence"],
    ["multi-select picks both skills", cb5picked.includes("Python") && cb5picked.includes("React") && byId.cb5.status === "filled"],
  ]);

  if (fail) {
    console.log("picked:", picked, "results:", JSON.stringify(out.results));
    process.exit(1);
  }
  console.log("\ncombobox: all checks passed");
})();
