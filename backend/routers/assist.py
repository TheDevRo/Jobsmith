"""
routers/assist.py — Applicant Assist: the extension/browser handoff flow,
the isolated-mode sidebar, tailored-file serving, and live-page
autofill/scan against the active assist browser.
"""

import asyncio
import json
import logging
import os
from pathlib import Path

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel

from .. import app_state as state
from .. import applicant_assist
from .. import database as db
from .. import extension_api

logger = logging.getLogger(__name__)

router = APIRouter()

# ---------------------------------------------------------------------------
# Applicant Assist endpoints
# ---------------------------------------------------------------------------

@router.get("/api/assist/content")
async def assist_content():
    """Return resume and cover-letter plain text for the active assist session."""
    session = applicant_assist.get_active_session()
    if session is None:
        raise HTTPException(404, "No active Applicant Assist session")
    return JSONResponse(
        {
            "resume_text": session.get("resume_text", ""),
            "cover_letter_text": session.get("cover_letter_text", ""),
        },
        headers={"Access-Control-Allow-Origin": "*"},
    )


@router.get("/assist-sidebar")
async def assist_sidebar():
    """Self-contained sidebar page served inside an iframe on external job pages."""
    cfg = state.load_config()
    port = cfg.get("server", {}).get("port", 8888)
    backend_url = f"http://localhost:{port}"
    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Applicant Assist</title>
<style>
*,*::before,*::after{{box-sizing:border-box;margin:0;padding:0}}
html,body{{height:100%;overflow:hidden;background:#1e1e2e;color:#cdd6f4;
  font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif;
  font-size:13px}}
#root{{display:flex;flex-direction:column;height:100%}}
#header{{display:flex;align-items:center;justify-content:space-between;
  padding:12px 10px;background:#313244;border-bottom:1px solid #45475a;
  flex-shrink:0}}
#title{{font-weight:600;font-size:14px;white-space:nowrap;overflow:hidden;
  text-overflow:ellipsis;color:#cdd6f4;flex:1}}
#toggle-btn{{background:none;border:none;color:#cdd6f4;cursor:pointer;
  font-size:20px;line-height:1;padding:0 4px;flex-shrink:0}}
#ctab{{display:none;padding:14px 0;font-size:11px;font-weight:600;
  color:#89b4fa;cursor:pointer;text-align:center;writing-mode:vertical-rl;
  flex:1;background:#1e1e2e;align-items:center;justify-content:center}}
#body{{flex:1;overflow-y:auto;padding:10px;display:flex;flex-direction:column;gap:12px}}
.section{{background:#313244;border-radius:8px;overflow:hidden}}
.sec-hdr{{display:block;padding:7px 10px;font-weight:700;font-size:11px;
  text-transform:uppercase;letter-spacing:0.6px;color:#89b4fa;background:#45475a}}
.sec-body{{padding:8px;display:flex;flex-direction:column;gap:6px}}
.lbl{{display:block;font-size:11px;color:#a6adc8}}
pre{{background:#181825;color:#cdd6f4;padding:8px;border-radius:4px;
  font-size:11px;font-family:'Menlo','Monaco','Consolas',monospace;
  max-height:180px;overflow-y:auto;white-space:pre-wrap;word-break:break-word;
  border:none}}
.btn-row{{display:flex;gap:6px;flex-wrap:wrap}}
.btn-copy{{display:inline-block;background:#89b4fa;color:#1e1e2e;border:none;
  border-radius:4px;padding:5px 10px;font-size:12px;font-weight:700;
  cursor:pointer;line-height:1.4}}
.btn-dl{{display:inline-block;background:#45475a;color:#cdd6f4;border:none;
  border-radius:4px;padding:5px 10px;font-size:12px;font-weight:700;
  cursor:pointer;line-height:1.4}}
#autofill-bar{{padding:8px 10px;background:#1e1e2e;border-bottom:1px solid #45475a;
  display:flex;flex-direction:column;gap:6px;flex-shrink:0}}
#btn-row{{display:flex;gap:6px}}
#btn-autofill{{flex:1;padding:8px;background:#a6e3a1;color:#1e1e2e;border:none;
  border-radius:6px;font-size:13px;font-weight:700;cursor:pointer;line-height:1.4}}
#btn-autofill:disabled{{background:#45475a;color:#6c7086;cursor:not-allowed}}
#btn-scan{{padding:8px 10px;background:#45475a;color:#cdd6f4;border:none;
  border-radius:6px;font-size:13px;font-weight:700;cursor:pointer;line-height:1.4}}
#btn-scan:disabled{{opacity:0.5;cursor:not-allowed}}
#autofill-status{{font-size:11px;color:#a6adc8;text-align:center;min-height:14px}}
#autofill-result{{display:none;background:#181825;border-radius:6px;padding:8px;font-size:11px}}
#autofill-result .af-summary{{color:#a6adc8;margin-bottom:6px}}
#autofill-result .af-list{{list-style:none;padding:0;margin:0;display:flex;flex-direction:column;gap:3px}}
#autofill-result .af-item{{padding:3px 6px;border-radius:3px;font-size:10px}}
#autofill-result .af-filled{{background:rgba(166,227,161,0.15);color:#a6e3a1}}
#autofill-result .af-low{{background:rgba(249,226,175,0.15);color:#f9e2af}}
#autofill-result .af-failed{{background:rgba(243,139,168,0.15);color:#f38ba8}}
#scan-result{{display:none;background:#181825;border-radius:6px;padding:8px;font-size:11px;max-height:200px;overflow-y:auto}}
#scan-result .sf-item{{padding:3px 6px;border-radius:3px;margin-bottom:3px;background:#313244}}
#scan-result .sf-req{{color:#f38ba8;font-weight:700}}
#status{{padding:10px;color:#a6adc8;font-size:12px;text-align:center}}
</style>
</head>
<body>
<div id="root">
  <div id="header">
    <span id="title">Your Tailored Docs</span>
    <button id="toggle-btn" title="Collapse">&#8250;</button>
  </div>
  <div id="ctab">Docs</div>
  <div id="autofill-bar">
    <div id="btn-row">
      <button id="btn-autofill" onclick="triggerAutofill()">&#9654; Autofill</button>
      <button id="btn-scan" onclick="scanForm()">&#128269; Scan</button>
    </div>
    <div id="autofill-status"></div>
    <div id="autofill-result"></div>
    <div id="scan-result"></div>
  </div>
  <div id="body">
    <div id="status">Loading...</div>
  </div>
</div>
<script>
const BACKEND = {json.dumps(backend_url)};
const NO_CONTENT = '(no content — tailor this job first in Jobsmith)';

async function load() {{
  try {{
    const r = await fetch(BACKEND + '/api/assist/content');
    if (!r.ok) throw new Error('HTTP ' + r.status);
    const data = await r.json();
    render(data.resume_text || '', data.cover_letter_text || '');
  }} catch (e) {{
    document.getElementById('status').textContent = 'Error: ' + e.message;
  }}
}}

function render(resumeText, clText) {{
  const body = document.getElementById('body');
  body.innerHTML = '';
  body.appendChild(makeSection('Resume', resumeText, 'resume'));
  body.appendChild(makeSection('Cover Letter', clText, 'cover_letter'));
}}

function makeSection(label, rawText, fileType) {{
  const sec = document.createElement('div');
  sec.className = 'section';
  const hdr = document.createElement('div');
  hdr.className = 'sec-hdr';
  hdr.textContent = label;
  const sbody = document.createElement('div');
  sbody.className = 'sec-body';
  const lbl = document.createElement('span');
  lbl.className = 'lbl';
  lbl.textContent = 'Plain Text';
  const pre = document.createElement('pre');
  pre.textContent = rawText || NO_CONTENT;
  const btnRow = document.createElement('div');
  btnRow.className = 'btn-row';
  const copyBtn = document.createElement('button');
  copyBtn.className = 'btn-copy';
  copyBtn.textContent = 'Copy All';
  copyBtn.addEventListener('click', () => copyText(rawText || NO_CONTENT));
  const dlBtn = document.createElement('button');
  dlBtn.className = 'btn-dl';
  dlBtn.textContent = '⬇ DOCX';
  dlBtn.addEventListener('click', () => downloadDocx(fileType));
  btnRow.appendChild(copyBtn);
  btnRow.appendChild(dlBtn);
  sbody.appendChild(lbl);
  sbody.appendChild(pre);
  sbody.appendChild(btnRow);
  sec.appendChild(hdr);
  sec.appendChild(sbody);
  return sec;
}}

function copyText(text) {{
  navigator.clipboard.writeText(text).catch(() => {{
    const ta = document.createElement('textarea');
    ta.value = text;
    ta.style.cssText = 'position:fixed;opacity:0;top:0;left:0';
    document.body.appendChild(ta);
    ta.select();
    document.execCommand('copy');
    ta.remove();
  }});
}}

async function downloadDocx(fileType) {{
  try {{
    const resp = await fetch(BACKEND + '/api/assist/file?type=' + fileType + '&format=docx');
    if (!resp.ok) throw new Error('HTTP ' + resp.status);
    const blob = await resp.blob();
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url;
    a.download = fileType + '.docx';
    document.body.appendChild(a);
    a.click();
    setTimeout(() => {{ URL.revokeObjectURL(url); a.remove(); }}, 10000);
  }} catch (e) {{
    alert('Download failed: ' + e.message);
  }}
}}

// Collapse / expand — sends resize message to host page
let collapsed = false;
const toggleBtn = document.getElementById('toggle-btn');
const ctab = document.getElementById('ctab');
const body = document.getElementById('body');
const title = document.getElementById('title');

function collapse() {{
  collapsed = true;
  body.style.display = 'none';
  title.style.display = 'none';
  ctab.style.display = 'flex';
  toggleBtn.innerHTML = '&#8249;';
  toggleBtn.title = 'Expand';
  window.parent.postMessage({{action:'resize', width:'40px'}}, '*');
}}

function expand() {{
  collapsed = false;
  body.style.display = 'flex';
  title.style.display = 'block';
  ctab.style.display = 'none';
  toggleBtn.innerHTML = '&#8250;';
  toggleBtn.title = 'Collapse';
  window.parent.postMessage({{action:'resize', width:'320px'}}, '*');
}}

toggleBtn.addEventListener('click', () => collapsed ? expand() : collapse());
ctab.addEventListener('click', expand);

// Hide the autofill bar when the sidebar collapses (mirrors body/title hide)
const autofillBar = document.getElementById('autofill-bar');
toggleBtn.addEventListener('click', () => {{
  // collapsed is already updated by the first listener (collapse/expand ran)
  autofillBar.style.display = collapsed ? 'none' : 'flex';
}});

async function triggerAutofill() {{
  const btn = document.getElementById('btn-autofill');
  const statusEl = document.getElementById('autofill-status');
  btn.disabled = true;
  btn.textContent = 'Autofilling\u2026';
  statusEl.textContent = 'Running \u2014 do not close the browser';
  statusEl.style.color = '#89b4fa';
  try {{
    const r = await fetch(BACKEND + '/api/assist/autofill', {{ method: 'POST' }});
    if (r.status === 409) {{
      statusEl.textContent = 'Already running\u2026';
      return;
    }}
    if (!r.ok) {{
      const err = await r.json().catch(() => ({{}}));
      throw new Error(err.detail || 'HTTP ' + r.status);
    }}
    // Poll until done
    while (true) {{
      await new Promise(res => setTimeout(res, 1500));
      const s = await fetch(BACKEND + '/api/assist/autofill/status').then(x => x.json()).catch(() => ({{active: false}}));
      if (!s.active) break;
    }}
    // Poll complete — fetch final status with field results
    const finalStatus = await fetch(BACKEND + '/api/assist/autofill/status').then(x => x.json()).catch(() => ({{}}));
    btn.textContent = '\u2713 Done \u2014 review & submit';
    btn.style.background = '#a6e3a1';
    statusEl.textContent = 'Fields filled. Check everything before submitting.';
    statusEl.style.color = '#a6e3a1';
    showAutofillResult(finalStatus);
    // Send highlight message to host page
    if (finalStatus.highlight_fields) {{
      window.parent.postMessage({{action: 'highlight', fields: finalStatus.highlight_fields}}, '*');
    }}
    setTimeout(() => {{
      btn.disabled = false;
      btn.textContent = '\u25b6 Autofill';
      btn.style.background = '';
      statusEl.textContent = '';
    }}, 8000);
  }} catch (e) {{
    btn.textContent = '\u25b6 Autofill';
    btn.style.background = '';
    btn.disabled = false;
    statusEl.textContent = 'Error: ' + e.message;
    statusEl.style.color = '#f38ba8';
  }}
}}

function showAutofillResult(data) {{
  const el = document.getElementById('autofill-result');
  if (!data || (!data.filled_count && !data.failed)) {{ el.style.display = 'none'; return; }}
  const filled = data.filled_count || 0;
  const total = data.total_count || 0;
  const low = (data.low_confidence || []).length;
  const failed = (data.failed || []).length;
  let html = '<div class="af-summary">Filled ' + filled + ' of ' + total + ' fields</div><ul class="af-list">';
  if (low) html += '<li class="af-item af-low">\u26a0 ' + low + ' low-confidence (review these)</li>';
  if (failed) html += '<li class="af-item af-failed">\u2717 ' + failed + ' could not fill</li>';
  if (filled - low > 0) html += '<li class="af-item af-filled">\u2713 ' + (filled - low) + ' filled confidently</li>';
  html += '</ul>';
  el.innerHTML = html;
  el.style.display = 'block';
}}

async function scanForm() {{
  const btn = document.getElementById('btn-scan');
  const scanEl = document.getElementById('scan-result');
  btn.disabled = true;
  btn.textContent = 'Scanning\u2026';
  try {{
    const r = await fetch(BACKEND + '/api/assist/scan', {{method: 'POST'}});
    if (!r.ok) throw new Error('HTTP ' + r.status);
    const data = await r.json();
    if (!data.fields || !data.fields.length) {{
      scanEl.innerHTML = '<span style="color:#a6adc8">No form fields found on this page</span>';
    }} else {{
      let html = '<div style="color:#89b4fa;margin-bottom:6px;font-weight:700">Form fields (' + data.count + ')</div>';
      data.fields.forEach(f => {{
        const reqBadge = f.required ? '<span class="sf-req"> *</span>' : '';
        const lbl = (f.label || f.field_id || 'unknown').substring(0, 40);
        html += '<div class="sf-item">' + lbl + reqBadge + ' <span style="color:#6c7086">(' + (f.type || 'text') + ')</span></div>';
      }});
      scanEl.innerHTML = html;
    }}
    scanEl.style.display = 'block';
  }} catch (e) {{
    scanEl.innerHTML = '<span style="color:#f38ba8">Scan failed: ' + e.message + '</span>';
    scanEl.style.display = 'block';
  }} finally {{
    btn.disabled = false;
    btn.textContent = '\U0001F50D Scan';
  }}
}}

load();
</script>
</body>
</html>"""
    from fastapi.responses import HTMLResponse
    return HTMLResponse(html)


@router.get("/api/assist/file")
async def assist_file(type: str = Query(..., pattern="^(resume|cover_letter)$"),
                      format: str = Query("docx", pattern="^(docx|pdf)$")):
    """Serve the active assist session's tailored file to the sidebar.

    Honors the configured PDF/DOCX format: if a sibling file in the preferred
    format exists it is served, otherwise it falls back to the DOCX (which is
    always written and can be regenerated on the fly).
    """
    session = applicant_assist.get_active_session()
    if session is None:
        raise HTTPException(404, "No active Applicant Assist session")

    docx_path = session["resume_path"] if type == "resume" else session["cover_letter_path"]

    cfg = state.load_config()
    fmt = cfg.get("application_honesty", {}).get("document_format", "docx")
    prefer_pdf = str(fmt).lower() == "pdf"
    pdf_path = docx_path.with_suffix(".pdf")

    if prefer_pdf and pdf_path.exists():
        serve_path, ext = pdf_path, "pdf"
    elif docx_path.exists():
        serve_path, ext = docx_path, "docx"
    elif pdf_path.exists():
        serve_path, ext = pdf_path, "pdf"
    else:
        # Regenerate from the plain text stored in the session (DOCX always,
        # plus PDF when that's the configured format).
        plain_text = session.get("resume_text" if type == "resume" else "cover_letter_text", "")
        if not plain_text:
            raise HTTPException(404, f"{type} file not found — tailor this job first")
        try:
            from .. import resume_generator as _rg
            profile = cfg.get("profile", {})
            job = applicant_assist.get_active_job() or {"id": docx_path.stem.split("_")[0]}
            if type == "resume":
                serve_path = Path(_rg.generate_resume(plain_text, profile, job, cfg))
            else:
                serve_path = Path(_rg.generate_cover_letter(plain_text, profile, job, cfg))
            ext = serve_path.suffix.lstrip(".") or "docx"
            logger.info("assist_file: generated missing %s on-the-fly at %s", ext, serve_path)
        except Exception as gen_exc:
            logger.warning("assist_file: could not generate %s: %s", type, gen_exc)
            raise HTTPException(404, f"{type} file not found and could not be generated")

    mime = "application/pdf" if ext == "pdf" else state.DOCX_MIME
    return FileResponse(
        str(serve_path),
        filename=f"{type}.{ext}",
        media_type=mime,
        headers={"Access-Control-Allow-Origin": "*"},
    )


class AssistLaunchRequest(BaseModel):
    job_id: str
    isolated: bool = False  # force the Playwright fallback


@router.post("/api/assist/launch")
async def assist_launch(req: AssistLaunchRequest):
    """Prepare an Applicant Assist handoff: stage the tailored docs, then open
    the user's default browser to a launch page that hands the session off to
    the Jobsmith extension.

    Set `isolated=true` to skip the handoff and use the Playwright fallback.
    """
    job = await db.get_job(req.job_id)
    if not job:
        raise HTTPException(404, "Job not found")

    app_data = job.get("application")
    if not app_data:
        raise HTTPException(400, "No application record for this job — tailor the resume first")

    resume_path = state.RESUMES_DIR / f"{req.job_id}_resume.docx"
    cl_path = state.RESUMES_DIR / f"{req.job_id}_cover_letter.docx"
    resume_text = app_data.get("resume_content") or ""
    cl_text = app_data.get("cover_letter_content") or ""

    cfg = state.load_config()

    if req.isolated:
        asyncio.create_task(
            applicant_assist.launch_assist_isolated(
                job=job,
                resume_text=resume_text,
                resume_docx_path=str(resume_path),
                cover_letter_text=cl_text,
                cover_letter_docx_path=str(cl_path),
                settings=cfg,
            )
        )
        return {"mode": "isolated", "job_id": req.job_id, "opened": True}

    # Default: extension handoff via the user's default browser.
    applicant_assist.register_active_session(
        job=job,
        resume_text=resume_text,
        resume_docx_path=str(resume_path),
        cover_letter_text=cl_text,
        cover_letter_docx_path=str(cl_path),
    )

    token = extension_api.get_or_create_token()
    record = applicant_assist.create_handoff_session(job, setup_token=token)

    # JOBSMITH_PORT is the port the desktop sidecar actually bound (the Tauri
    # shell picks a free one when 8888 is taken); config.yaml only knows the
    # preferred port, so the env var must win or the launch URL points at the
    # wrong (or no) server.
    port = int(os.environ.get("JOBSMITH_PORT") or cfg.get("server", {}).get("port", 8888))
    launch_url = f"http://127.0.0.1:{port}/assist/launch/{record['id']}"

    opened = False
    try:
        import webbrowser
        opened = webbrowser.open(launch_url, new=2)
    except Exception as exc:
        logger.warning("assist_launch: webbrowser.open failed: %s", exc)

    return {
        "mode": "handoff",
        "session_id": record["id"],
        "launch_url": launch_url,
        "opened": bool(opened),
        "job_id": req.job_id,
    }


# ---------------------------------------------------------------------------
# Applicant Assist — default-browser handoff page + state polling
# ---------------------------------------------------------------------------

def _is_loopback_request(request) -> bool:
    client = getattr(request, "client", None)
    host = (client.host if client else "") or ""
    return host in ("127.0.0.1", "::1", "localhost")


@router.get("/assist/launch/{session_id}")
async def assist_launch_page(session_id: str, request: Request):
    """HTML page served into the user's default browser. The extension's
    handshake content script reads the embedded session data; the page then
    polls /api/assist/session/{id}/state and redirects to the job apply URL
    once the extension checks in.
    """
    if not _is_loopback_request(request):
        raise HTTPException(403, "This page is only available on localhost")
    rec = applicant_assist.get_handoff_session(session_id)
    if not rec:
        raise HTTPException(404, "Assist session expired or not found")

    import html as _html
    from fastapi.responses import HTMLResponse

    cfg = state.load_config()
    ext_cfg = cfg.get("extension", {}) or {}
    amo_url = ext_cfg.get("amo_url", "") or ""
    web_store_url = ext_cfg.get("web_store_url", "") or ""

    session_json = json.dumps({
        "session_id": rec["id"],
        "setup_token": rec["setup_token"],
        "apply_url": rec["apply_url"],
        "amo_url": amo_url,
        "web_store_url": web_store_url,
    })

    job_label = _html.escape(f"{rec.get('job_title','')} — {rec.get('job_company','')}".strip(" —"))
    apply_url_attr = _html.escape(rec["apply_url"], quote=True)
    setup_token_attr = _html.escape(rec["setup_token"], quote=True)
    session_id_attr = _html.escape(rec["id"], quote=True)

    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<title>Jobsmith — Applicant Assist</title>
<style>
  :root {{ color-scheme: dark; }}
  body {{ margin:0; font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif;
          background:#1e1e2e; color:#cdd6f4; display:flex; align-items:center;
          justify-content:center; min-height:100vh; padding:24px; }}
  .card {{ max-width:560px; width:100%; background:#313244; border-radius:12px;
            padding:32px; box-shadow:0 8px 24px rgba(0,0,0,.4); }}
  h1 {{ margin:0 0 4px; font-size:20px; color:#89b4fa; }}
  .job {{ font-size:14px; color:#a6adc8; margin-bottom:24px; word-break:break-word; }}
  .status {{ font-size:15px; margin:20px 0; }}
  .spinner {{ display:inline-block; width:14px; height:14px; border:2px solid #45475a;
              border-top-color:#89b4fa; border-radius:50%; margin-right:8px;
              animation: spin 0.8s linear infinite; vertical-align:-2px; }}
  @keyframes spin {{ to {{ transform: rotate(360deg); }} }}
  .install ol {{ padding-left:20px; line-height:1.6; }}
  .install code, .token-block {{ background:#1e1e2e; padding:2px 6px; border-radius:4px;
                                  font-family:"SF Mono",Menlo,Consolas,monospace; font-size:12px;
                                  color:#f9e2af; word-break:break-all; }}
  .token-block {{ display:block; padding:10px; margin-top:8px; }}
  button, a.btn {{ display:inline-block; background:#89b4fa; color:#1e1e2e; border:0;
                    padding:8px 14px; border-radius:6px; font-weight:600; cursor:pointer;
                    text-decoration:none; font-size:13px; margin-right:8px; margin-top:8px; }}
  button.secondary, a.btn.secondary {{ background:#45475a; color:#cdd6f4; }}
  .hidden {{ display:none; }}
</style>
</head>
<body>
<div class="card">
  <h1>Applicant Assist</h1>
  <div class="job">{job_label or 'Preparing application…'}</div>

  <div id="jobsmith-session"
       data-session="{session_id_attr}"
       data-setup-token="{setup_token_attr}"
       data-apply-url="{apply_url_attr}"
       class="hidden"></div>

  <div id="status-pending" class="status">
    <span class="spinner"></span>Looking for the Jobsmith extension…
  </div>

  <div id="status-ready" class="status hidden">
    Extension connected. Redirecting to the application page…
  </div>

  <div id="status-firefox-gate" class="status hidden">
    <strong>Extension connected.</strong>
    <div style="margin-top:10px;font-size:13px;color:#cdd6f4;line-height:1.5">
      <em>Tip:</em> Firefox can't open the sidebar from a webpage.
      Click the <strong>Jobsmith</strong> icon in your toolbar &rarr;
      <strong>Open panel</strong> to start the Assist sidebar.
    </div>
    <div style="margin-top:14px">
      <button id="jobsmith-continue-btn" type="button">Continue now</button>
    </div>
    <div style="font-size:12px;color:#a6adc8;margin-top:10px">
      Continuing in <span id="jobsmith-firefox-countdown">3</span>s…
    </div>
  </div>

  <div id="status-needs-install" class="status hidden">
    <strong>Extension not detected.</strong>
    <div class="install" id="install-instructions"></div>
    <div style="margin-top:16px">
      <button id="manual-token-toggle" class="secondary" type="button">Set token manually</button>
      <button id="isolated-fallback" class="secondary" type="button">Open in isolated mode instead</button>
      <button id="retry" type="button">I installed it — check again</button>
    </div>
    <div id="manual-token" class="hidden" style="margin-top:14px">
      <div style="font-size:13px;color:#a6adc8">Copy this token into the extension popup (Backend URL + Token):</div>
      <code class="token-block">{setup_token_attr}</code>
    </div>
  </div>
</div>

<script>
  const Jobsmith = {session_json};

  const $ = (id) => document.getElementById(id);
  const show = (id) => $(id).classList.remove('hidden');
  const hide = (id) => $(id).classList.add('hidden');

  function renderInstallInstructions() {{
    const ua = navigator.userAgent;
    const isFirefox = /Firefox/i.test(ua);
    const isChromiumFamily = /Chrome|Chromium|Edg|Brave/i.test(ua) && !isFirefox;
    let html = '';
    if (isFirefox) {{
      html += '<p>Install the Jobsmith extension in Firefox (permanent, Mozilla-signed):</p><ol>';
      html += '<li><a href="/api/extension/firefox-xpi">Download &amp; install the signed add-on</a> — Firefox will prompt you to add it.</li>';
      html += '<li>If no prompt appears, open <code>about:addons</code> → the gear icon ⚙️ → <em>Install Add-on From File…</em> → pick the downloaded <code>.xpi</code>.</li>';
      if (Jobsmith.amo_url) html += '<li>Or <a href="' + Jobsmith.amo_url + '" target="_blank">install from Mozilla Add-ons</a>.</li>';
      html += '</ol>';
    }} else if (isChromiumFamily) {{
      html += '<p>Install the Jobsmith extension in this browser:</p><ol>';
      if (Jobsmith.web_store_url) html += '<li><a href="' + Jobsmith.web_store_url + '" target="_blank">Install from the Chrome Web Store</a>, or</li>';
      html += '<li>Open <code>chrome://extensions</code> → enable <em>Developer mode</em> → <em>Load unpacked</em> → choose <code>extension/src/</code> in the Jobsmith repo.</li>';
      html += '</ol>';
    }} else {{
      html += '<p>Your browser doesn\\'t have a Jobsmith extension build. Use the <em>isolated mode</em> fallback below, or open this page in Firefox/Chrome/Edge.</p>';
    }}
    $('install-instructions').innerHTML = html;
  }}

  let pollCount = 0;
  async function pollState() {{
    pollCount++;
    try {{
      const r = await fetch('/api/assist/session/' + encodeURIComponent(Jobsmith.session_id) + '/state');
      if (!r.ok) throw new Error('HTTP ' + r.status);
      const data = await r.json();
      if (data.state === 'extension_ready') {{
        hide('status-pending'); hide('status-needs-install');
        const isFirefox = /Firefox/i.test(navigator.userAgent);
        if (isFirefox) {{
          show('status-firefox-gate');
          startFirefoxPanelGate();
        }} else {{
          show('status-ready');
          setTimeout(() => window.location.replace(Jobsmith.apply_url), 400);
        }}
        return;
      }}
    }} catch (e) {{ /* keep polling */ }}
    if (pollCount >= 10) {{
      hide('status-pending'); show('status-needs-install');
      renderInstallInstructions();
      try {{
        await fetch('/api/assist/session/' + encodeURIComponent(Jobsmith.session_id) + '/needs-install', {{ method: 'POST' }});
      }} catch (e) {{}}
      return;
    }}
    setTimeout(pollState, 500);
  }}

  function startFirefoxPanelGate() {{
    // Firefox can't open the sidebar from a page click. Show a tooltip-style
    // hint, auto-continue after 3s, with a Continue button to skip the wait.
    let done = false;
    const proceed = () => {{
      if (done) return;
      done = true;
      window.location.replace(Jobsmith.apply_url);
    }};
    let seconds = 3;
    const countdownEl = $('jobsmith-firefox-countdown');
    const tick = () => {{
      if (done) return;
      seconds--;
      if (countdownEl) countdownEl.textContent = String(Math.max(0, seconds));
      if (seconds <= 0) {{ proceed(); return; }}
      setTimeout(tick, 1000);
    }};
    setTimeout(tick, 1000);
    $('jobsmith-continue-btn').addEventListener('click', proceed, {{ once: true }});
  }}

  $('retry').addEventListener('click', () => {{
    hide('status-needs-install'); show('status-pending'); pollCount = 0; pollState();
  }});
  $('manual-token-toggle').addEventListener('click', () => $('manual-token').classList.toggle('hidden'));
  $('isolated-fallback').addEventListener('click', async () => {{
    $('isolated-fallback').disabled = true;
    try {{
      await fetch('/api/assist/session/' + encodeURIComponent(Jobsmith.session_id) + '/fallback', {{ method: 'POST' }});
      $('status-needs-install').innerHTML = '<strong>Launching isolated browser…</strong> You can close this tab.';
    }} catch (e) {{
      $('isolated-fallback').disabled = false;
    }}
  }});

  pollState();
</script>
</body>
</html>
"""
    return HTMLResponse(html)


@router.get("/api/assist/session/{session_id}/state")
async def assist_session_state(session_id: str):
    rec = applicant_assist.get_handoff_session(session_id)
    if not rec:
        raise HTTPException(404, "Assist session expired or not found")
    return {"state": rec["state"], "apply_url": rec["apply_url"]}


@router.get("/api/assist/session/{session_id}/handshake-meta")
async def assist_session_handshake_meta(session_id: str, request: Request):
    """Returns the per-session setup token + apply URL so the extension's
    background script can perform the handshake when content-script injection
    isn't reliable (e.g. Firefox MV3 opt-in host permissions). Loopback-only —
    the setup_token is the same value already embedded in the launch page DOM,
    so no new privilege is exposed."""
    if not _is_loopback_request(request):
        raise HTTPException(403, "Only available on localhost")
    rec = applicant_assist.get_handoff_session(session_id)
    if not rec:
        raise HTTPException(404, "Assist session expired or not found")
    return {
        "session_id": rec["id"],
        "setup_token": rec["setup_token"],
        "apply_url": rec["apply_url"],
    }


@router.post("/api/assist/session/{session_id}/needs-install")
async def assist_session_needs_install(session_id: str):
    if not applicant_assist.mark_handoff_needs_install(session_id):
        raise HTTPException(404, "Assist session expired or not found")
    return {"ok": True}


@router.post("/api/assist/session/{session_id}/fallback")
async def assist_session_fallback(session_id: str):
    """User asked to fall back to isolated (Playwright) mode for this session."""
    rec = applicant_assist.get_handoff_session(session_id)
    if not rec:
        raise HTTPException(404, "Assist session expired or not found")
    job_id = rec.get("job_id")
    if not job_id:
        raise HTTPException(400, "Session has no associated job")
    job = await db.get_job(job_id)
    if not job:
        raise HTTPException(404, "Job not found")
    app_data = job.get("application") or {}
    resume_path = state.RESUMES_DIR / f"{job_id}_resume.docx"
    cl_path = state.RESUMES_DIR / f"{job_id}_cover_letter.docx"
    asyncio.create_task(
        applicant_assist.launch_assist_isolated(
            job=job,
            resume_text=app_data.get("resume_content") or "",
            resume_docx_path=str(resume_path),
            cover_letter_text=app_data.get("cover_letter_content") or "",
            cover_letter_docx_path=str(cl_path),
            settings=state.load_config(),
        )
    )
    return {"ok": True}


# ---------------------------------------------------------------------------
# Applicant Assist — autofill on the live browser page
# ---------------------------------------------------------------------------

_assist_autofill_active: bool = False
_assist_autofill_result: dict = {}   # last autofill result, returned by /status


async def _bg_assist_autofill() -> None:
    """Run the auto-apply adapter against the currently open Assist browser page."""
    global _assist_autofill_active, _assist_autofill_result
    _assist_autofill_active = True
    _assist_autofill_result = {}
    try:
        from ..auto_apply.browser_controller import BrowserController
        from ..auto_apply.adapters import ALL_ADAPTERS
        from ..auto_apply.llm_client import LLMClient
        from ..auto_apply.logger import AutoApplyLogger
        from ..auto_apply.models import ApplyMode, UserProfile, JobApplicationRequest

        page = applicant_assist.get_active_page()
        session = applicant_assist.get_active_session()
        job = applicant_assist.get_active_job()

        if page is None or session is None or job is None:
            logger.warning("assist autofill: no active session")
            return

        cfg = state.load_config()
        current_url = page.url

        adapter = next((a for a in ALL_ADAPTERS if a.matches(current_url, "")), ALL_ADAPTERS[-1])

        resume_path = session["resume_path"]
        cover_path = session["cover_letter_path"]

        job_req = JobApplicationRequest(
            job_id=str(job.get("id", "")),
            application_id="assist",
            title=job.get("title", ""),
            company=job.get("company", ""),
            url=current_url,
            description=job.get("description", ""),
            resume_path=str(resume_path) if resume_path.exists() else None,
            cover_letter_path=str(cover_path) if cover_path.exists() else None,
        )

        user_profile = UserProfile.from_config(cfg)
        llm = LLMClient(cfg)
        log = AutoApplyLogger(
            job_id=job_req.job_id,
            app_id="assist",
            site=current_url[:50],
            adapter=adapter.name,
            mode="autofill",
        )

        ctrl = BrowserController.from_existing_page(page, cfg)
        result = await adapter.apply(ctrl, user_profile, job_req, llm, ApplyMode.AUTOFILL, log)

        # Build field-level summary from log entries
        field_entries = [e for e in result.log_entries if e.get("level") == "field"]
        filled = [e for e in field_entries if e.get("action") in ("filled", "selected", "clicked")]
        low_conf = [e for e in filled if e.get("confidence", 1.0) < 0.60]
        failed = [e for e in field_entries if e.get("action") == "skipped"]

        highlight_fields = (
            [{"selector": f'[id="{e["field_id"]}"]', "status": "low_confidence"} for e in low_conf] +
            [{"selector": f'[id="{e["field_id"]}"]', "status": "failed"} for e in failed] +
            [{"selector": f'[id="{e["field_id"]}"]', "status": "filled"}
             for e in filled if e not in low_conf]
        )

        _assist_autofill_result = {
            "filled_count": len(filled),
            "total_count": len(field_entries),
            "low_confidence": [e.get("field_id", "") for e in low_conf],
            "failed": [e.get("field_id", "") for e in failed],
            "highlight_fields": highlight_fields,
            "message": result.message or "",
            "success": result.success,
        }

        if result.success:
            state.push_notification("assist", "Autofill Complete",
                               f"Fields filled for {job.get('title', 'job')} — review and submit", "success")
        else:
            state.push_notification("assist", "Autofill Done",
                               result.message or "Autofill finished with issues — check fields", "info")
    except Exception:
        logger.exception("assist autofill failed")
        state.push_notification("assist", "Autofill Failed", "An error occurred during autofill", "error")
    finally:
        _assist_autofill_active = False


@router.post("/api/assist/autofill", status_code=202)
async def assist_autofill():
    """Trigger autofill on the currently open Applicant Assist browser page."""
    if applicant_assist.get_active_page() is None:
        raise HTTPException(400, "No active Applicant Assist browser — launch one first")
    if _assist_autofill_active:
        raise HTTPException(409, "Autofill already running")
    task = asyncio.create_task(_bg_assist_autofill())
    state.running_tasks["assist_autofill"] = task
    return {"message": "Autofill started"}


@router.get("/api/assist/autofill/status")
async def assist_autofill_status():
    """Return whether an autofill is currently running, plus last-run field results."""
    return {"active": _assist_autofill_active, **_assist_autofill_result}


@router.post("/api/assist/scan")
async def assist_scan():
    """Scan the currently open Assist browser page and return a field list."""
    page = applicant_assist.get_active_page()
    if page is None:
        raise HTTPException(400, "No active Applicant Assist browser")
    try:
        from ..auto_apply.browser_controller import BrowserController
        cfg = state.load_config()
        ctrl = BrowserController.from_existing_page(page, cfg)
        snapshot = await ctrl.get_dom_snapshot()
        fields = [
            {
                "field_id": f.field_id,
                "label": f.label or f.placeholder or f.name,
                "type": f.field_type,
                "required": f.required,
                "selector": ctrl._field_map.get(f.field_id, ""),
            }
            for f in snapshot
        ]
        return {"fields": fields, "count": len(fields)}
    except Exception as exc:
        logger.exception("assist scan failed")
        raise HTTPException(500, f"Scan failed: {exc}")
