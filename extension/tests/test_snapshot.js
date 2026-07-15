// Functional test of src/common/snapshot.js in jsdom.
const { loadDom, evalScript, report } = require("./helpers");

const html = `<!DOCTYPE html><html><body>
  <form>
    <label for="fn">First Name</label>
    <input id="fn" type="text" autocomplete="given-name" required>

    <input type="hidden" name="csrf" value="x">
    <input type="submit" value="Go">

    <fieldset>
      <legend>Are you authorized to work in the US?</legend>
      <label><input type="radio" name="auth" value="y"> Yes</label>
      <label><input type="radio" name="auth" value="n"> No</label>
    </fieldset>

    <fieldset>
      <legend>Which days are you available?</legend>
      <label><input type="checkbox" name="days[]" value="mon"> Monday</label>
      <label><input type="checkbox" name="days[]" value="tue"> Tuesday</label>
    </fieldset>

    <!-- Workday-style button dropdown -->
    <label for="wd">Phone Device Type</label>
    <button id="wd" aria-haspopup="listbox">Select One</button>

    <!-- react-select style: wrapper div role=combobox containing real input -->
    <div role="combobox" aria-expanded="false">
      <input id="rs" role="combobox" aria-autocomplete="list" aria-label="State">
    </div>

    <!-- contenteditable rich editor -->
    <div id="editor" contenteditable="true" role="textbox" aria-multiline="true" aria-label="Cover Letter"></div>

    <!-- plain contenteditable without textbox role must be ignored -->
    <div contenteditable="true" id="not-a-field">styling widget</div>

    <label for="pw">Password</label><input id="pw" type="password">
    <label for="dt">Start Date</label><input id="dt" type="date">
    <input type="file" id="resume" style="display:none">

    <!-- Fabric (BambooHR) custom select: visible button aria-haspopup=true
         backed by a HIDDEN native <select> sibling that carries the options. -->
    <label for="fabric-state">State *</label>
    <div class="fab-select-wrap">
      <button id="fabric-state" type="button" aria-haspopup="true">–Select–</button>
      <select name="state.value" style="display:none" aria-hidden="true">
        <option value="">–Select–</option>
        <option value="CA">California</option>
        <option value="TX">Texas</option>
      </select>
    </div>

    <!-- Honeypots: name signature, offscreen, and "leave blank" label -->
    <label for="hp1">Please leave this field blank</label>
    <input id="hp1" name="nickname_hpcsaf" type="text">
    <label for="hp2">Nickname</label>
    <input id="hp2" name="nickname2" type="text" style="position:absolute;left:-9999px">
    <label for="hp3">Confirm you are human — leave this blank</label>
    <input id="hp3" name="confirm_human" type="text">

    <!-- Required marked only by a trailing "*" (BambooHR has no native attr) -->
    <label for="lastname">Last Name *</label><input id="lastname" type="text">
    <!-- Unmarked field must NOT be flagged required -->
    <label for="linkedin">LinkedIn URL</label><input id="linkedin" type="url">

    <!-- Visually-hidden radio group behind a visible styled proxy+label -->
    <fieldset>
      <legend>Are you legally authorized to work?</legend>
      <label class="proxy"><input type="radio" name="workauth" value="yes" style="opacity:0;position:absolute;width:1px;height:1px"> Yes</label>
      <label class="proxy"><input type="radio" name="workauth" value="no" style="opacity:0;position:absolute;width:1px;height:1px"> No</label>
    </fieldset>
  </form>
</body></html>`;

const dom = loadDom(html);
const snap = evalScript(dom.window, "common/snapshot.js");
const byId = Object.fromEntries(snap.fields.map(f => [f.field_id, f]));
const ids = snap.fields.map(f => f.field_id);

const fail = report([
  ["first name captured with autocomplete", byId.fn && byId.fn.autocomplete === "given-name" && byId.fn.required === true],
  ["hidden/submit excluded", !ids.some(i => i === "csrf")],
  ["radio group deduped to one field", snap.fields.filter(f => f.name === "auth").length === 1],
  ["radio options collected", (byId.auth.options || []).join(",") === "Yes,No"],
  ["radio legend in extra_context", /authorized to work/i.test(byId.auth.extra_context)],
  ["checkbox group NOT deduped", snap.fields.filter(f => f.name === "days[]").length === 2],
  ["checkbox fids unique", new Set(ids).size === ids.length],
  ["checkbox legend context", snap.fields.filter(f => f.name === "days[]").every(f => /days are you available/i.test(f.extra_context))],
  ["workday button combobox captured as select", byId.wd && byId.wd.field_type === "select" && byId.wd._combobox === true],
  ["react-select wrapper skipped, inner input kept", byId.rs && byId.rs.field_type === "select" && snap.fields.filter(f => f.label === "State").length === 1],
  ["contenteditable textbox captured", byId.editor && byId.editor.field_type === "textarea" && byId.editor.label === "Cover Letter"],
  ["non-textbox contenteditable ignored", !byId["not-a-field"]],
  ["password type kept", byId.pw && byId.pw.field_type === "password"],
  ["date type kept", byId.dt && byId.dt.field_type === "date"],
  ["hidden file input kept", byId.resume && byId.resume.field_type === "file"],

  // Fabric custom select (visible aria-haspopup="true" button + hidden native
  // <select>) captured as a fillable select with options from the hidden list.
  ["fabric custom select captured", byId["fabric-state"] && byId["fabric-state"].field_type === "select" && byId["fabric-state"]._combobox === true],
  ["fabric options pulled from hidden <select>", byId["fabric-state"] && (byId["fabric-state"].options || []).join(",") === "California,Texas"],
  ["hidden native <select> not emitted on its own", !snap.fields.some(f => f.name === "state.value")],

  // Honeypots dropped by name / offscreen / "leave blank" label.
  ["honeypot by name (hpcsaf) skipped", !byId.hp1],
  ["honeypot offscreen skipped", !byId.hp2],
  ["honeypot by 'leave blank' label skipped", !byId.hp3],

  // Required inferred from the "*" marker (no native required attr).
  ["required inferred from * marker", byId.lastname && byId.lastname.required === true],
  ["fabric select * marks it required", byId["fabric-state"] && byId["fabric-state"].required === true],
  ["unmarked field not required", byId.linkedin && byId.linkedin.required === false],

  // Visually-hidden radio group behind a visible proxy is detected.
  ["hidden radio group detected", snap.fields.filter(f => f.name === "workauth").length === 1],
  ["hidden radio options collected", byId.workauth && (byId.workauth.options || []).join(",") === "Yes,No"],
]);

if (fail) {
  console.log(JSON.stringify(snap.fields, null, 1));
  process.exit(1);
}
console.log("\nsnapshot.js: all checks passed");
