// Functional test of src/common/fill.js in jsdom.
const { loadDom, evalScript, report } = require("./helpers");

const html = `<!DOCTYPE html><html><body>
  <form>
    <label for="fname">First Name</label><input id="fname" data-jobsmith-fid="fname" type="text">

    <label for="state">State</label>
    <select id="state" data-jobsmith-fid="state">
      <option value="">Select...</option>
      <option value="CA">California</option>
      <option value="TX">Texas</option>
      <option value="NY">New York</option>
    </select>

    <fieldset>
      <legend>Will you require sponsorship?</legend>
      <label><input type="radio" name="sponsor" value="1" data-jobsmith-fid="sponsor"> Yes</label>
      <label><input type="radio" name="sponsor" value="0"> No</label>
      <label><input type="radio" name="sponsor" value="2"> Not applicable</label>
    </fieldset>

    <label><input type="checkbox" id="agree" data-jobsmith-fid="agree"> I agree to the terms</label>

    <label for="start">Start Date</label><input type="date" id="start" data-jobsmith-fid="start">

    <select id="degree" data-jobsmith-fid="degree">
      <option value="">Please select</option>
      <option value="hs">High School</option>
      <option value="bach">Bachelor's Degree</option>
      <option value="mast">Master's Degree</option>
    </select>

    <label for="vet">Veteran Status</label>
    <select id="vet" data-jobsmith-fid="vet">
      <option value="">Select one</option>
      <option value="v1">I am a protected veteran</option>
      <option value="v2">I am not a protected veteran</option>
      <option value="v3">I don't wish to answer</option>
    </select>

    <input type="file" id="resume" data-jobsmith-fid="resume">
    <input type="file" id="cover" data-jobsmith-fid="cover">
  </form>
</body></html>`;

const dom = loadDom(html);
const { window } = dom;

// jsdom has no DataTransfer, and HTMLInputElement.files is getter-only.
// Both are stand-ins for the browser behavior fill.js relies on.
window.DataTransfer = function DataTransfer() {
  const files = [];
  this.items = { add: (f) => files.push(f) };
  Object.defineProperty(this, "files", { get: () => files });
};

// A file input that only accepts an assignment on the Nth try — the hardened
// ATS uploaders (Workday, some Greenhouse) clear it while their own async
// handler runs. acceptOnAttempt = Infinity → it never accepts.
function hostileFileInput(id, acceptOnAttempt) {
  const el = window.document.getElementById(id);
  const state = { attempts: 0, stored: null };
  Object.defineProperty(el, "files", {
    configurable: true,
    get: () => state.stored,
    set: (v) => {
      state.attempts++;
      if (state.attempts >= acceptOnAttempt) state.stored = v;
    },
  });
  return state;
}
const resumeInput = hostileFileInput("resume", 3);       // accepts on the 3rd assignment
const coverInput = hostileFileInput("cover", Infinity);  // never accepts

evalScript(window, "common/fill.js");

const items = [
  { field_id: "fname", selector: '[data-jobsmith-fid="fname"]', value: "Jane", action: "fill", field_type: "text", confidence: 0.95 },
  { field_id: "state", selector: '[data-jobsmith-fid="state"]', value: "TX", action: "select", field_type: "select", confidence: 0.95 },
  { field_id: "sponsor", selector: '[data-jobsmith-fid="sponsor"]', name: "sponsor", value: "No", action: "select", field_type: "radio", confidence: 0.95 },
  { field_id: "agree", selector: '[data-jobsmith-fid="agree"]', value: "Yes", action: "check", field_type: "checkbox", confidence: 0.55 },
  { field_id: "start", selector: '[data-jobsmith-fid="start"]', value: "Immediately", action: "fill", field_type: "date", confidence: 0.95 },
  { field_id: "degree", selector: '[data-jobsmith-fid="degree"]', value: "BS Computer Science", action: "select", field_type: "select", confidence: 0.95 },
  { field_id: "vet", selector: '[data-jobsmith-fid="vet"]', value: "I am not a veteran", action: "select", field_type: "select", confidence: 0.95 },
  { field_id: "missing", selector: '[data-jobsmith-fid="nope"]', value: "x", action: "fill", field_type: "text", confidence: 0.95 },
  { field_id: "skipme", selector: '[data-jobsmith-fid="fname"]', value: "", action: "skip", field_type: "text", confidence: 0 },
  { field_id: "resume", selector: '[data-jobsmith-fid="resume"]', value: "resume", action: "upload", field_type: "file", confidence: 1,
    file_bytes: [1, 2, 3], file_name: "Resume.docx", file_mime: "application/octet-stream" },
  { field_id: "cover", selector: '[data-jobsmith-fid="cover"]', value: "cover_letter", action: "upload", field_type: "file", confidence: 1,
    file_bytes: [4, 5, 6], file_name: "CoverLetter.docx", file_mime: "application/octet-stream" },
];

(async () => {
  const out = await window.__jobsmithFillAndHighlight(items, {});
  const doc = window.document;
  const byId = Object.fromEntries(out.results.map(r => [r.field_id, r]));

  const fail = report([
    ["text fill", doc.getElementById("fname").value === "Jane" && byId.fname.status === "filled"],
    ["select via state abbrev", doc.getElementById("state").value === "TX" && byId.state.status === "filled"],
    ["radio No (not 'Not applicable')", doc.querySelector('input[name="sponsor"][value="0"]').checked && !doc.querySelector('input[name="sponsor"][value="2"]').checked && byId.sponsor.status === "filled"],
    ["checkbox low-conf", doc.getElementById("agree").checked && byId.agree.status === "low_confidence"],
    ["date normalized", /^\d{4}-\d{2}-\d{2}$/.test(doc.getElementById("start").value) && byId.start.status === "filled"],
    ["degree bucket", doc.getElementById("degree").value === "bach" && byId.degree.status === "filled"],
    ["veteran sentence", doc.getElementById("vet").value === "v2" && byId.vet.status === "filled"],
    ["missing → not_found", byId.missing.status === "not_found"],
    ["skip honored", byId.skipme.status === "skipped"],
    ["highlights applied", out.highlighted > 0],

    // Upload retry poll (REL-11): keep re-assigning while the uploader clears
    // the input, up to ~1.5s.
    ["upload retried until accepted", byId.resume.status === "filled" && resumeInput.attempts >= 3],
    ["upload attached the right file", resumeInput.stored && resumeInput.stored.length === 1 && resumeInput.stored[0].name === "Resume.docx"],
    ["upload that never sticks → failed", byId.cover.status === "failed"],
    ["failed upload points at the drag path", /drag the tile/i.test(byId.cover.message || "")],
    ["failed upload retried several times", coverInput.attempts >= 5],
  ]);

  if (fail) {
    console.log("\nraw results:", JSON.stringify(out.results, null, 1));
    process.exit(1);
  }
  console.log("\nfill.js: all checks passed");
})();
