// Functional test of the extension's "Prefer PDF documents" setting and of how
// fetched documents are named (src/common/api.js).
//   - preferPdf defaults to ON when unset; an explicit false turns it off
//   - jobsmithSetConfig persists it
//   - the File is named from the response's Content-Type, not from the
//     extension the caller guessed (a .docx request answered with a PDF
//     yields a .pdf), on the direct path and through the background fallback
//   - an unknown Content-Type keeps the requested name

const { loadDom, evalScript, report } = require("./helpers");

function arm({ store = {}, direct, bg }) {
  const dom = loadDom("<!DOCTYPE html><html><body></body></html>");
  const w = dom.window;
  w.Response = Response;
  w.Blob = Blob;
  w.File = File;
  w.fetch = async (url, init) => direct(url, init);
  w.chrome = {
    runtime: { lastError: null, sendMessage: (msg, cb) => cb(bg(msg)) },
    storage: {
      local: {
        get: (keys) => Promise.resolve(Object.fromEntries(
          (Array.isArray(keys) ? keys : [keys]).map((k) => [k, store[k]])
            .filter(([, v]) => v !== undefined)
        )),
        set: (v) => { Object.assign(store, v); return Promise.resolve(); },
      },
    },
  };
  evalScript(w, "common/storage.js");
  evalScript(w, "common/api.js");
  return { w, api: w.Jobsmith, store };
}

const DOCX = "application/vnd.openxmlformats-officedocument.wordprocessingml.document";
const file = (type) => async () => new Response("bytes", { status: 200, headers: { "content-type": type } });

async function main() {
  const checks = [];

  // --- preferPdf default / explicit / persisted --------------------------
  {
    const { api, store } = arm({ direct: file("x"), bg: () => null });
    checks.push(["preferPdf defaults to on", (await api.jobsmithGetConfig()).preferPdf === true]);
    await api.jobsmithSetConfig({ preferPdf: false });
    checks.push(["setConfig persists preferPdf=false", store.preferPdf === false]);
    checks.push(["preferPdf=false is read back", (await api.jobsmithGetConfig()).preferPdf === false]);
    await api.jobsmithSetConfig({ preferPdf: true });
    checks.push(["preferPdf=true is read back", (await api.jobsmithGetConfig()).preferPdf === true]);
    const { api: other } = arm({ store: { backendUrl: "http://localhost:8888" }, direct: file("x"), bg: () => null });
    checks.push(["unrelated saved settings keep preferPdf on", (await other.jobsmithGetConfig()).preferPdf === true]);
  }

  // --- File name follows the response ------------------------------------
  {
    const { api } = arm({ direct: file("application/pdf"), bg: () => null });
    const f = await api.jobsmithFetchFile("/api/ext/resume/j1?format=pdf", "j1_resume.docx");
    checks.push(["PDF response renames .docx request to .pdf", f.name === "j1_resume.pdf"]);
    checks.push(["PDF response keeps the PDF mime", f.type === "application/pdf"]);
  }
  {
    const { api } = arm({ direct: file(DOCX), bg: () => null });
    const f = await api.jobsmithFetchFile("/api/ext/cover-letter/j1?format=docx", "j1_cover_letter.pdf");
    checks.push(["DOCX response renames .pdf request to .docx", f.name === "j1_cover_letter.docx"]);
    checks.push(["DOCX response keeps the DOCX mime", f.type === DOCX]);
  }
  {
    const { api } = arm({ direct: file("application/pdf; charset=binary"), bg: () => null });
    const f = await api.jobsmithFetchFile("/api/ext/resume/j1", "j1_resume.docx");
    checks.push(["Content-Type parameters are ignored", f.name === "j1_resume.pdf" && f.type === "application/pdf"]);
  }
  {
    const { api } = arm({ direct: file("application/octet-stream"), bg: () => null });
    const f = await api.jobsmithFetchFile("/api/ext/resume/j1", "j1_resume.docx");
    checks.push(["unknown Content-Type keeps the requested name", f.name === "j1_resume.docx"]);
  }
  {
    let ref;
    ref = arm({
      direct: () => { throw new ref.w.TypeError("NetworkError"); },
      bg: () => ({ ok: true, result: { status: 200, statusText: "OK", contentType: "application/pdf",
        bodyB64: Buffer.from("pdf").toString("base64") } }),
    });
    const f = await ref.api.jobsmithFetchFile("/api/ext/resume/j1?format=pdf", "j1_resume.docx");
    checks.push(["background fallback names the file from the response", f.name === "j1_resume.pdf" && f.type === "application/pdf"]);
  }

  const fail = report(checks);
  if (fail) { console.log(`\npdf preference: ${fail} check(s) failed`); process.exit(1); }
  console.log("\npdf preference: all checks passed");
}

main();
