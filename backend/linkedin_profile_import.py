"""
linkedin_profile_import.py — Import the user's own LinkedIn profile into
the onboarding wizard.

Reuses the saved LinkedIn session's storage_state.json (same mechanism as
check_linkedin_session_validity, so there are no profile-dir lock conflicts
with a running auto-apply browser) to headlessly open the user's profile
plus its detail pages, scrape the visible text, and map it onto the
UserProfile schema with the résumé parser's strictly-extractive LLM flow.

Like resume_parser, this module never writes config — the wizard shows the
result for review/edit before anything is persisted.
"""

from __future__ import annotations

import logging
import re
from urllib.parse import urlparse

from . import resume_parser
from .auto_apply.linkedin_auth import LINKEDIN_SESSION_DIR, has_linkedin_session

logger = logging.getLogger(__name__)


class LinkedInSessionError(RuntimeError):
    """Raised when there is no usable LinkedIn session (missing or expired)."""


# Per-section character caps keep the combined text inside the parser's
# _MAX_CHARS budget while guaranteeing the later sections (education,
# certifications) aren't truncated away by an oversized experience list.
# Caps are sized for post-_strip_noise text, which is mostly real content.
_SECTIONS = (
    # (label, url suffix relative to the profile, char cap)
    ("PROFILE OVERVIEW", "", 3500),
    ("CONTACT INFO", "overlay/contact-info/", 600),
    ("ALL EXPERIENCE", "details/experience/", 7000),
    ("ALL EDUCATION", "details/education/", 1500),
    ("LICENSES & CERTIFICATIONS", "details/certifications/", 2200),
    ("SKILLS", "details/skills/", 1400),
)

_PROMPT = """You are extracting a job seeker's data from the visible text of \
their own LinkedIn profile pages. Extract ONLY information that is literally \
present in the text below. Do NOT infer, guess, embellish, or invent \
anything. If a field is not clearly stated, return an empty string "" (or an \
empty list for list fields). Never fabricate names, employers, dates, contact \
details, schools, or skills.

The text is scraped from web pages, so it contains UI noise — ignore things \
like "· 3rd", follower/connection counts, "Show all", "Endorse", button \
labels, and duration hints such as "· 2 yrs 3 mos".

Return ONLY a single JSON object, no prose, no markdown fences, with EXACTLY \
these keys:

{{
  "full_name": "",
  "email": "",
  "phone": "",
  "location": "",
  "street_address": "",
  "street_address_2": "",
  "city": "",
  "state": "",
  "zip_code": "",
  "linkedin": "",
  "github": "",
  "portfolio": "",
  "summary": "",
  "skills": [],
  "experience": [
    {{"title": "", "company": "", "start_date": "", "end_date": "Present", "bullets": []}}
  ],
  "education": [
    {{"degree": "", "school": "", "year": ""}}
  ],
  "certifications": []
}}

Rules:
- "location" should be "City, ST" if present; also fill city/state/zip_code \
when an explicit address is given.
- Dates: keep them as written (e.g. "2021", "Jan 2021"). Use "Present" for a \
current role's end_date. Do not copy duration hints like "2 yrs" into dates.
- "summary": the profile's About text, verbatim.
- "bullets": copy each role's description lines verbatim, lightly trimmed, \
one line per array item; do not rewrite them.
- "skills": only skills explicitly listed; one skill per array item; skip \
endorsement counts.
- "certifications": plain strings, one per item, as "Name (Issuer)" when the \
issuer is shown — never objects.
- "email"/"phone": only if shown (e.g. in a CONTACT INFO section).
- Omit empty experience/education objects entirely rather than padding.

LINKEDIN PROFILE TEXT:
\"\"\"
{resume}
\"\"\"

Return only the JSON object."""


def _clean_text(text: str) -> str:
    """Collapse scraped-page whitespace so caps aren't wasted on blank lines."""
    lines = [ln.strip() for ln in (text or "").splitlines()]
    out: list[str] = []
    for ln in lines:
        if ln or (out and out[-1]):  # allow single blank separators only
            out.append(ln)
    return "\n".join(out).strip()


# Every profile page ends with the same footer; everything from these lines
# on is chrome (nav links, help center, a 30+ entry language selector).
_FOOTER_LINES = frozenset({"Profile language", "Select language"})
_FOOTER_PREFIXES = ("LinkedIn Corporation ©",)

# Section headings whose entire block is feed/promo chrome on one's own
# profile — skipped until the next real content heading.
_NOISE_BLOCKS = frozenset({
    "Suggested for you", "Private to you", "Analytics", "Activity",
    "Open to", "Resources", "Who your viewers also viewed",
    "People you may know", "You might like", "Pages for you",
    "Explore Premium profiles", "Promoted",
})

# Headings that mark real profile content and terminate a noise block.
_CONTENT_HEADINGS = frozenset({
    "About", "Top skills", "Featured", "Experience", "Education",
    "Licenses & certifications", "Skills", "Projects", "Publications",
    "Volunteer experience", "Honors & awards", "Languages", "Courses",
    "Contact info",
})

# Standalone lines that are pure UI (buttons, counters, separators).
_NOISE_LINE = re.compile(
    r"^(?:Show all\b.*|Show details|Show credential|Show more"
    r"|Message|Connect|Follow|Endorse|Add section|Add custom button"
    r"|Create a post|Draft a post|Past 7 days|[·…]+"
    r"|Skip to .*|Edit contact info|Your profile"
    r"|[\d,]+\+? (?:followers|connections|notifications|profile views"
    r"|post impressions|search appearances))$"
)


def _strip_noise(text: str) -> str:
    """Drop page chrome so the char caps (and the LLM) get real content.

    Conservative by design: only removes the footer, known promo/feed blocks,
    and standalone button/counter lines. Unrecognized text always passes
    through, so a LinkedIn redesign degrades to the old noisy-but-complete
    behaviour instead of losing profile data.
    """
    out: list[str] = []
    skipping = False
    for ln in (text or "").splitlines():
        s = ln.strip()
        if s in _FOOTER_LINES or s.startswith(_FOOTER_PREFIXES):
            break
        if s in _NOISE_BLOCKS:
            skipping = True
            continue
        if skipping:
            if s in _CONTENT_HEADINGS:
                skipping = False
                out.append(ln)
            continue
        if _NOISE_LINE.match(s):
            continue
        out.append(ln)
    return "\n".join(out)


def combine_sections(sections: list[tuple[str, str]]) -> str:
    """Join labelled page texts under headers, applying each section's cap."""
    caps = {label: cap for label, _, cap in _SECTIONS}
    parts = []
    for label, text in sections:
        text = _clean_text(_strip_noise(text))
        if not text:
            continue
        cap = caps.get(label, 4000)
        if len(text) > cap:
            # Cut on a line boundary so the LLM never sees a half entry.
            cut = text.rfind("\n", 0, cap)
            text = text[: cut if cut > 0 else cap]
        parts.append(f"=== {label} ===\n{text}")
    return "\n\n".join(parts)


def _profile_url_from(page_url: str) -> str:
    """Canonical https://www.linkedin.com/in/<slug>/ from a page URL."""
    path = urlparse(page_url).path
    m = re.match(r"^/in/([^/]+)", path)
    if not m or m.group(1) == "me":
        return ""
    return f"https://www.linkedin.com/in/{m.group(1)}/"


async def _page_text(page, selector: str = "main") -> str:
    """Scroll until content stops growing, expand collapsed text, grab text."""
    grab = (
        f"(document.querySelector({selector!r}) || document.body).innerText"
    )
    try:
        await page.wait_for_selector(selector, timeout=8_000)
    except Exception:
        pass
    last_len = -1
    for _ in range(8):
        try:
            await page.evaluate("window.scrollBy(0, document.body.scrollHeight)")
            await page.wait_for_timeout(600)
            cur_len = await page.evaluate(f"{grab}.length")
        except Exception:
            break
        if cur_len == last_len:
            break
        last_len = cur_len
    # LinkedIn collapses About / role descriptions behind "…see more".
    try:
        await page.evaluate(
            "document.querySelectorAll('button').forEach(b =>"
            " { if (/see more/i.test(b.innerText || '')) b.click(); })"
        )
        await page.wait_for_timeout(400)
    except Exception:
        pass
    try:
        return await page.evaluate(grab)
    except Exception:
        return ""


async def fetch_profile_text() -> tuple[str, str]:
    """Scrape the logged-in user's own LinkedIn profile.

    Returns (combined_text, profile_url). profile_url may be "" if the
    /in/me redirect could not be resolved to a public slug.
    Raises LinkedInSessionError when no session exists or it has expired.
    """
    state_path = LINKEDIN_SESSION_DIR / "storage_state.json"
    if not has_linkedin_session() or not state_path.exists():
        raise LinkedInSessionError(
            "No LinkedIn session — connect LinkedIn first (Settings → Integrations)."
        )

    pw = browser = ctx = None
    try:
        from playwright.async_api import async_playwright
        pw = await async_playwright().start()
        browser = await pw.chromium.launch(
            headless=True,
            args=["--no-sandbox", "--disable-blink-features=AutomationControlled"],
        )
        ctx = await browser.new_context(storage_state=str(state_path))
        page = await ctx.new_page()

        # /in/me redirects to the user's own profile when authenticated.
        await page.goto(
            "https://www.linkedin.com/in/me/",
            wait_until="domcontentloaded",
            timeout=30_000,
        )
        # Give the SPA a moment to resolve the redirect to the real slug.
        try:
            await page.wait_for_url(re.compile(r"/in/(?!me/?$)[^/]+"), timeout=10_000)
        except Exception:
            pass
        if "/login" in page.url or "/authwall" in page.url:
            raise LinkedInSessionError(
                "LinkedIn session expired — reconnect from Settings → Integrations."
            )
        profile_url = _profile_url_from(page.url)
        base = profile_url or "https://www.linkedin.com/in/me/"

        sections: list[tuple[str, str]] = []
        sections.append(("PROFILE OVERVIEW", await _page_text(page)))

        for label, suffix, _cap in _SECTIONS:
            if not suffix:
                continue
            try:
                await page.goto(base + suffix, wait_until="domcontentloaded", timeout=25_000)
                await page.wait_for_timeout(1_000)
                if "/login" in page.url or "/authwall" in page.url:
                    logger.warning("LinkedIn import: auth wall on %s — skipping", suffix)
                    continue
                # Overlays (contact info) render in a dialog above the
                # profile; grabbing <main> would just duplicate the overview.
                selector = '[role="dialog"]' if "overlay/" in suffix else "main"
                sections.append((label, await _page_text(page, selector)))
            except LinkedInSessionError:
                raise
            except Exception:
                logger.warning("LinkedIn import: could not read %s — skipping", suffix, exc_info=True)

        combined = combine_sections(sections)
        logger.info(
            "LinkedIn import: scraped %d sections, %d chars (profile: %s)",
            len(sections), len(combined), profile_url or "unresolved",
        )
        return combined, profile_url
    finally:
        for closer in (ctx, browser):
            if closer:
                try:
                    await closer.close()
                except Exception:
                    pass
        if pw:
            try:
                await pw.stop()
            except Exception:
                pass


async def import_profile(config: dict) -> dict:
    """Scrape + extract the user's LinkedIn profile.

    Returns {"profile": {...}, "warnings": [...], "linkedin_url": str} in the
    same shape the wizard already consumes from /api/onboarding/parse-resume.
    """
    text, profile_url = await fetch_profile_text()
    if not text:
        return {
            "profile": resume_parser._sanitize({}),
            "warnings": ["Could not read any text from your LinkedIn profile — try again."],
            "linkedin_url": profile_url,
        }

    result = await resume_parser.parse_resume(text, config, prompt_template=_PROMPT)
    # Small local models occasionally whiff on a long extraction; when the
    # scrape clearly had content but nothing came back, one retry is cheap.
    p = result["profile"]
    if len(text) > 500 and not p.get("full_name") and not p.get("experience"):
        logger.warning("LinkedIn import: empty extraction from %d chars — retrying once", len(text))
        retry = await resume_parser.parse_resume(text, config, prompt_template=_PROMPT)
        rp = retry["profile"]
        if rp.get("full_name") or rp.get("experience"):
            result = retry

    # The scraped page rarely displays its own URL — fill it from navigation.
    if profile_url:
        result["profile"]["linkedin"] = profile_url
    result["linkedin_url"] = profile_url
    return result
