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
  </form>
</body></html>`;

const dom = loadDom(html);
const { window } = dom;
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
  ]);

  if (fail) {
    console.log("\nraw results:", JSON.stringify(out.results, null, 1));
    process.exit(1);
  }
  console.log("\nfill.js: all checks passed");
})();
