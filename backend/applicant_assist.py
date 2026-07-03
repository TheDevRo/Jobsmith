"""
applicant_assist.py — Applicant Assist mode for external ATS jobs.

Opens a visible Chromium browser on the job's apply URL and injects a
persistent sidebar showing the tailored resume and cover letter.
The sidebar survives page navigations via add_init_script and provides:
  - Scrollable plain-text panels with "Copy All" buttons
  - "Download DOCX" buttons (fetches from /api/assist/file)
  - Drag handles for dropping the DOCX onto file-upload inputs

Usage (from main.py):
    asyncio.create_task(launch_assist(job, resume_text, resume_docx_path,
                                      cover_letter_text, cover_letter_docx_path,
                                      settings))
"""

import asyncio
import json
import logging
import secrets
import time
from pathlib import Path
from typing import Optional

from playwright.async_api import async_playwright, Browser, BrowserContext, Page

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Active-session state (used by /api/assist/file to know which files to serve)
# ---------------------------------------------------------------------------

_active_session: Optional[dict] = None  # {"resume_path": Path, "cover_letter_path": Path}
_active_page: Optional["Page"] = None   # live Playwright page for the current assist browser
_active_job: Optional[dict] = None      # raw job dict for the current assist session


# ---------------------------------------------------------------------------
# Handoff sessions — short-lived records that bridge a click in the Jobsmith UI to
# the extension running in the user's default browser. Keyed by an unguessable
# id and exposed only via /assist/launch/{id} pages and /api/ext/assist/*.
# ---------------------------------------------------------------------------

_HANDOFF_TTL_SECONDS = 600
_handoff_sessions: dict[str, dict] = {}


def create_handoff_session(job: dict, setup_token: str) -> dict:
    """Create a new browser-handoff session and return its record."""
    _gc_handoff_sessions()
    session_id = secrets.token_urlsafe(16)
    record = {
        "id": session_id,
        "job_id": job.get("id"),
        "job_title": job.get("title", ""),
        "job_company": job.get("company", ""),
        "apply_url": job.get("url", ""),
        "setup_token": setup_token,
        "state": "pending",  # pending → extension_ready (or needs_install)
        "created_at": time.time(),
    }
    _handoff_sessions[session_id] = record
    return record


def get_handoff_session(session_id: str) -> Optional[dict]:
    _gc_handoff_sessions()
    return _handoff_sessions.get(session_id)


def mark_handoff_extension_ready(session_id: str) -> bool:
    rec = _handoff_sessions.get(session_id)
    if not rec:
        return False
    rec["state"] = "extension_ready"
    rec["checked_in_at"] = time.time()
    return True


def mark_handoff_needs_install(session_id: str) -> bool:
    rec = _handoff_sessions.get(session_id)
    if not rec:
        return False
    if rec["state"] == "pending":
        rec["state"] = "needs_install"
    return True


def _gc_handoff_sessions() -> None:
    now = time.time()
    stale = [k for k, v in _handoff_sessions.items() if now - v["created_at"] > _HANDOFF_TTL_SECONDS]
    for k in stale:
        _handoff_sessions.pop(k, None)


def register_active_session(
    job: dict,
    resume_text: str,
    resume_docx_path: str,
    cover_letter_text: str,
    cover_letter_docx_path: str,
) -> None:
    """Register the active session for an extension-driven handoff.

    Mirrors what launch_assist_isolated() sets up, minus the Playwright page,
    so /api/assist/file and /api/assist/content can serve the right files when
    the user's own browser (running the Jobsmith extension) requests them.
    """
    global _active_session, _active_job, _active_page
    _active_session = {
        "job_id": job.get("id"),
        "resume_path": Path(resume_docx_path),
        "cover_letter_path": Path(cover_letter_docx_path),
        "resume_text": resume_text,
        "cover_letter_text": cover_letter_text,
    }
    _active_job = job
    _active_page = None  # no Playwright page in extension mode


def get_active_session() -> Optional[dict]:
    return _active_session


def get_active_page() -> Optional["Page"]:
    return _active_page


def get_active_job() -> Optional[dict]:
    return _active_job


def clear_active_session() -> None:
    global _active_session, _active_page, _active_job
    _active_session = None
    _active_page = None
    _active_job = None


# ---------------------------------------------------------------------------
# Sidebar script builder
# ---------------------------------------------------------------------------

def _build_sidebar_script(backend_url: str) -> str:
    """Return JavaScript that injects an iframe-based Applicant Assist sidebar."""
    backend_json = json.dumps(backend_url)

    return f"""
(function injectAssistSidebar() {{
  if (document.getElementById('__assist-host__')) return;
  // Don't inject into our own sidebar page (avoid infinite iframe recursion —
  // context.add_init_script fires in every frame including the sidebar iframe itself).
  if (/^(localhost|127\\.0\\.0\\.1)(:\\d+)?$/.test(window.location.host)) return;
  if (!document.body) {{
    document.addEventListener('DOMContentLoaded', injectAssistSidebar, {{ once: true }});
    return;
  }}

  const BACKEND = {backend_json};
  const WIDTH_KEY = '__assist_sidebar_width__';

  function sp(el, prop, val) {{ el.style.setProperty(prop, val, 'important'); }}

  // Restore preferred width from previous session (default 320px)
  const savedWidth = localStorage.getItem(WIDTH_KEY) || '320px';

  // --- Main iframe ---
  const iframe = document.createElement('iframe');
  iframe.id = '__assist-host__';
  iframe.src = BACKEND + '/assist-sidebar';
  iframe.allow = 'clipboard-write';
  sp(iframe, 'position', 'fixed');
  sp(iframe, 'top', '0');
  sp(iframe, 'right', '0');
  sp(iframe, 'width', savedWidth);
  sp(iframe, 'height', '100vh');
  sp(iframe, 'z-index', '2147483647');
  sp(iframe, 'border', 'none');
  sp(iframe, 'background', 'transparent');
  sp(iframe, 'margin', '0');
  sp(iframe, 'padding', '0');
  sp(iframe, 'pointer-events', 'auto');
  document.body.appendChild(iframe);

  // --- Drag handle (left edge of sidebar) ---
  const handle = document.createElement('div');
  handle.id = '__assist-drag__';
  sp(handle, 'position', 'fixed');
  sp(handle, 'top', '0');
  sp(handle, 'right', savedWidth);
  sp(handle, 'width', '6px');
  sp(handle, 'height', '100vh');
  sp(handle, 'z-index', '2147483648');
  sp(handle, 'cursor', 'ew-resize');
  sp(handle, 'background', 'rgba(137,180,250,0.25)');
  document.body.appendChild(handle);

  let _dragging = false, _startX = 0, _startW = parseInt(savedWidth) || 320;

  handle.addEventListener('mousedown', (e) => {{
    _dragging = true;
    _startX = e.clientX;
    _startW = parseInt(iframe.style.getPropertyValue('width')) || _startW;
    document.body.style.userSelect = 'none';
    e.preventDefault();
  }});
  document.addEventListener('mousemove', (e) => {{
    if (!_dragging) return;
    const newW = Math.max(250, Math.min(600, _startW + (_startX - e.clientX)));
    const wpx = newW + 'px';
    sp(iframe, 'width', wpx);
    sp(handle, 'right', wpx);
  }});
  document.addEventListener('mouseup', () => {{
    if (!_dragging) return;
    _dragging = false;
    document.body.style.userSelect = '';
    const w = iframe.style.getPropertyValue('width') || savedWidth;
    localStorage.setItem(WIDTH_KEY, w);
  }});

  // --- Messages from the iframe (resize on collapse/expand, highlight fields) ---
  window.addEventListener('message', (e) => {{
    if (!e.data) return;
    if (e.data.action === 'resize') {{
      sp(iframe, 'width', e.data.width);
      sp(handle, 'right', e.data.width);
      if (e.data.width !== '40px') localStorage.setItem(WIDTH_KEY, e.data.width);
    }}
    if (e.data.action === 'highlight') {{
      _applyHighlights(e.data.fields || []);
    }}
  }});

  // --- MutationObserver: re-inject if host is removed by SPA re-render ---
  const observer = new MutationObserver(() => {{
    if (!document.getElementById('__assist-host__')) {{
      observer.disconnect();
      injectAssistSidebar();
    }}
  }});
  observer.observe(document.body, {{ childList: true }});

  // --- Toast notification on sidebar activation ---
  (function showToast() {{
    if (document.getElementById('__assist-toast__')) return;
    const t = document.createElement('div');
    t.id = '__assist-toast__';
    t.textContent = "Auto-apply couldn't complete this form. Use the sidebar to review your materials and fill any remaining fields.";
    sp(t, 'position', 'fixed');
    sp(t, 'top', '0');
    sp(t, 'left', '0');
    sp(t, 'right', '0');
    sp(t, 'padding', '14px 20px');
    sp(t, 'background', '#313244');
    sp(t, 'color', '#cdd6f4');
    sp(t, 'font-size', '14px');
    sp(t, 'font-family', '-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif');
    sp(t, 'z-index', '2147483646');
    sp(t, 'border-bottom', '2px solid #89b4fa');
    sp(t, 'cursor', 'pointer');
    sp(t, 'text-align', 'center');
    document.body.appendChild(t);
    const dismiss = () => t.remove();
    t.addEventListener('click', dismiss);
    setTimeout(dismiss, 8000);
  }})();

  // --- Keyboard shortcut Ctrl+Shift+A / Cmd+Shift+A to toggle sidebar ---
  let _sidebarHidden = false;
  document.addEventListener('keydown', (e) => {{
    if ((e.ctrlKey || e.metaKey) && e.shiftKey && e.key === 'A') {{
      e.preventDefault();
      _sidebarHidden = !_sidebarHidden;
      sp(iframe, 'display', _sidebarHidden ? 'none' : 'block');
      sp(handle, 'display', _sidebarHidden ? 'none' : 'block');
    }}
  }});

  // --- Field highlight injection ---
  function _applyHighlights(fields) {{
    const prev = document.getElementById('__assist-hl__');
    if (prev) prev.remove();
    if (!fields.length) return;
    const style = document.createElement('style');
    style.id = '__assist-hl__';
    style.textContent = fields.map(f => {{
      if (!f.selector) return '';
      const color = f.status === 'filled' ? '#a6e3a1'
                  : f.status === 'low_confidence' ? '#f9e2af'
                  : '#f38ba8';
      return f.selector + ' {{ outline: 2px solid ' + color + ' !important; outline-offset: 2px !important; }}';
    }}).filter(Boolean).join('\\n');
    document.head.appendChild(style);
    // Clear highlight when user interacts with a field
    document.addEventListener('click', (ev) => {{
      const el = ev.target;
      if (el && (el.tagName === 'INPUT' || el.tagName === 'SELECT' || el.tagName === 'TEXTAREA')) {{
        const s = document.getElementById('__assist-hl__');
        if (!s) return;
        const sel = el.id ? '#' + el.id : el.name ? '[name="' + el.name + '"]' : null;
        if (sel) s.textContent = s.textContent.split('\\n').filter(line => !line.startsWith(sel)).join('\\n');
      }}
    }});
  }}
}})();
"""


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------

async def launch_assist_isolated(
    job: dict,
    resume_text: str,
    resume_docx_path: str,
    cover_letter_text: str,
    cover_letter_docx_path: str,
    settings: dict,
) -> None:
    """
    Isolated-mode fallback: open a visible Playwright Chromium window on the
    job's apply URL and inject the Applicant Assist sidebar iframe. Runs until
    the user closes the browser.

    This is the original Apply Assist behavior, retained as a fallback for
    when the default-browser + extension handoff path isn't available (no
    extension installed, unsupported browser, or webbrowser.open() failed).
    """
    global _active_session, _active_page, _active_job

    apply_url = job.get("url", "")
    if not apply_url:
        logger.error("Applicant Assist: job has no URL")
        return

    port = settings.get("server", {}).get("port", 8888)
    backend_url = f"http://localhost:{port}"

    # Store paths and text so /api/assist/file and /api/assist/content can serve them
    _active_session = {
        "job_id": job.get("id"),
        "resume_path": Path(resume_docx_path),
        "cover_letter_path": Path(cover_letter_docx_path),
        "resume_text": resume_text,
        "cover_letter_text": cover_letter_text,
    }
    _active_job = job

    sidebar_script = _build_sidebar_script(backend_url)

    logger.info("Applicant Assist: launching browser → %s", apply_url)

    async with async_playwright() as pw:
        browser: Browser = await pw.chromium.launch(
            headless=False,
            args=["--allow-running-insecure-content"],
        )
        context: BrowserContext = await browser.new_context(
            no_viewport=True,
            bypass_csp=True,
        )

        # Inject sidebar on every new page / navigation (runs at document_start)
        await context.add_init_script(sidebar_script)

        page: Page = await context.new_page()
        _active_page = page

        try:
            await page.goto(apply_url, wait_until="domcontentloaded", timeout=30_000)
        except Exception as e:
            logger.warning("Applicant Assist: initial navigation warning: %s", e)

        # Also evaluate immediately so it appears without waiting for next navigation
        try:
            await page.evaluate(sidebar_script)
        except Exception as e:
            logger.debug("Applicant Assist: immediate evaluate warning: %s", e)

        logger.info("Applicant Assist: sidebar injected — browser open for user interaction")

        # Wait until the user closes the browser.
        # Polling browser.is_connected() is more reliable than wait_for_event("disconnected")
        # because Playwright Python's timeout=0 is 0ms (not "no timeout"), which would
        # cause wait_for_event to exit immediately before the user has done anything.
        try:
            while browser.is_connected():
                await asyncio.sleep(0.5)
        except Exception:
            pass

        try:
            await browser.close()
        except Exception:
            pass

    clear_active_session()
    logger.info("Applicant Assist: session ended")
