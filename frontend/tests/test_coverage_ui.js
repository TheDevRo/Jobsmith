// Requirement coverage UI: the N/M badge, the requirements list (met/unmet markers, evidence, escaping),
// the "I don't have this" action (POSTs the text, redraws as a known gap), the job card/detail wiring,
// and the "Best coverage" sort option. Same jsdom style as test_triage_ui.js.
const fs = require("fs");
const path = require("path");
const { JSDOM, VirtualConsole } = require("jsdom");

const ROOT = path.join(__dirname, "..");
const html = fs.readFileSync(path.join(ROOT, "index.html"), "utf8");
const dom = new JSDOM(html, { runScripts: "outside-only", url: "http://localhost:8888/", virtualConsole: new VirtualConsole() });
const { window } = dom;
const doc = window.document;

const calls = [];
let reply = null;
window.api = (url, opts = {}) => { calls.push([url, opts.method || "GET", opts.body]); return reply ? Promise.resolve(reply) : Promise.reject(new Error("boom")); };
window.toast = () => {};
window.escapeHtml = (s) => String(s == null ? "" : s).replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;").replace(/"/g, "&quot;");
window.safeParseJSON = (s, f) => { try { return typeof s === "string" ? JSON.parse(s) : (s == null ? f : s); } catch (e) { return f; } };
// Helpers from other scripts that the job detail markup calls; their output is irrelevant here.
for (const fn of ["timeAgo", "qualityBadge", "appliedBadge", "renderSalarySection", "renderHeatRing", "renderHeatChip", "renderJobActions", "safeId"]) window[fn] = () => "";
window.eval(fs.readFileSync(path.join(ROOT, "js", "jobs.js"), "utf8"));

const cov = {
  requirements: [
    { text: "5+ years of Python", kind: "must", met: true, evidence: "Data Engineer at Acme" },
    { text: "Bachelor's <degree>", kind: "must", met: false, evidence: null },
    { text: "Airflow experience", kind: "nice", met: false, evidence: null, dismissed: true },
  ],
  covered: 1, total: 3, missing_must: 1,
};
const job = { id: "j1", title: "Engineer", coverage: JSON.stringify(cov) };
const checks = [];
const sortOpt = [...doc.getElementById("filter-sort").options].find(o => o.value === "best_coverage-desc");

(async () => {
  const badge = window.renderCoverageBadge(job);
  checks.push(["badge shows covered/total", />1\/3</.test(badge)]);
  checks.push(["badge flags missing must-haves", /cov-gap/.test(badge) && /1 must-have missing/.test(badge)]);
  checks.push(["no coverage -> no badge", window.renderCoverageBadge({ id: "x" }) === "" && window.renderCoverageBadge({ coverage: "garbage" }) === ""
    && window.renderCoverageBadge({ coverage: null }) === ""]);

  const section = window.renderCoverageSection(job);
  checks.push(["section summary", /1 of 3 covered/.test(section)]);
  checks.push(["met row has the evidence", /cov-req met[\s\S]*Data Engineer at Acme/.test(section)]);
  checks.push(["unmet text is escaped", section.includes("Bachelor's &lt;degree&gt;") && !section.includes("<degree>")]);
  checks.push(["one dismiss action (the undismissed unmet gap)", (section.match(/class="cov-dismiss"/g) || []).length === 1]);
  checks.push(["dismissed gap reads as a known gap", /Known gap/.test(section)]);
  checks.push(["no coverage -> no section", window.renderCoverageSection({ id: "x" }) === ""]);

  doc.body.innerHTML = section;
  window._currentJobs = { j1: job };
  reply = { coverage: { ...cov, requirements: cov.requirements.map(r => ({ ...r, dismissed: r.met ? undefined : true })) } };
  await window.dismissRequirement(doc.querySelector(".cov-dismiss"));
  checks.push(["dismiss POSTs the requirement text",
    calls.length === 1 && calls[0][0] === "/api/jobs/j1/coverage/dismiss" && calls[0][1] === "POST"
    && JSON.parse(calls[0][2]).text === "Bachelor's <degree>"]);
  checks.push(["list is redrawn without the action", doc.querySelectorAll(".cov-dismiss").length === 0 && doc.querySelectorAll(".cov-req.dismissed").length === 2]);
  checks.push(["cached job is updated", JSON.parse(window._currentJobs.j1.coverage).requirements[1].dismissed === true]);

doc.body.innerHTML = window.renderCoverageSection({ id: "j2", coverage: JSON.stringify(cov) });
  reply = null;  // server error: nothing changes
  await window.dismissRequirement(doc.querySelector(".cov-dismiss"));
  checks.push(["a failed dismiss leaves the action in place", doc.querySelectorAll(".cov-dismiss").length === 1]);

  checks.push(["detail pane includes the requirements", /cov-section/.test(window.buildJobDetailHtml({ ...job, source: "x", tags: "[]" }))]);
  checks.push(["Best Coverage sort option exists", !!sortOpt && /Coverage/i.test(sortOpt.textContent)]);

  let fail = 0;
  for (const [name, ok] of checks) { console.log((ok ? "PASS" : "FAIL") + "  " + name); if (!ok) fail++; }
  process.exit(fail ? 1 : 0);
})();
