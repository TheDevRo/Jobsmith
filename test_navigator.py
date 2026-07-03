"""
test_navigator.py — Smoke test for Phase 2 AI navigator functions.

Uses a local HTML form served inline so tests don't depend on external sites.
Exercises all navigator functions and prints detailed results.
"""

import asyncio
import json
import sys
import yaml
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from backend.page_extractor import extract_page_state, snapshot_summary
from backend.ai_navigator import (
    classify_page,
    map_form_fields,
    answer_questions,
    pick_navigation_target,
    validate_field_mapping,
)

import logging
logging.basicConfig(level=logging.DEBUG, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")

# ---------------------------------------------------------------------------
# Local test HTML pages
# ---------------------------------------------------------------------------

FORM_PAGE_HTML = """<!DOCTYPE html>
<html><head><title>Apply - Security Engineer at Acme Corp</title></head>
<body>
<h1>Apply for Security Engineer</h1>
<h2>Acme Corp - Denver, CO</h2>
<form id="application-form">
    <div class="form-group">
        <label for="first_name">First Name *</label>
        <input type="text" id="first_name" name="first_name" required>
    </div>
    <div class="form-group">
        <label for="last_name">Last Name *</label>
        <input type="text" id="last_name" name="last_name" required>
    </div>
    <div class="form-group">
        <label for="email">Email Address *</label>
        <input type="email" id="email" name="email" required>
    </div>
    <div class="form-group">
        <label for="phone">Phone Number</label>
        <input type="tel" id="phone" name="phone">
    </div>
    <div class="form-group">
        <label for="location">Current Location</label>
        <input type="text" id="location" name="location" placeholder="City, State">
    </div>
    <div class="form-group">
        <label for="linkedin">LinkedIn Profile URL</label>
        <input type="url" id="linkedin" name="linkedin" placeholder="https://linkedin.com/in/...">
    </div>
    <div class="form-group">
        <label for="portfolio">Portfolio / Website</label>
        <input type="url" id="portfolio" name="portfolio">
    </div>
    <div class="form-group">
        <label for="resume">Resume / CV *</label>
        <input type="file" id="resume" name="resume" accept=".pdf,.docx" required>
    </div>
    <div class="form-group">
        <label for="experience_years">Years of cybersecurity experience</label>
        <input type="number" id="experience_years" name="experience_years" min="0" max="50">
    </div>
    <div class="form-group">
        <label for="work_auth">Are you authorized to work in the United States? *</label>
        <select id="work_auth" name="work_auth" required>
            <option value="">-- Select --</option>
            <option value="yes">Yes</option>
            <option value="no">No</option>
        </select>
    </div>
    <div class="form-group">
        <label for="sponsorship">Will you now or in the future require sponsorship? *</label>
        <select id="sponsorship" name="sponsorship" required>
            <option value="">-- Select --</option>
            <option value="yes">Yes</option>
            <option value="no">No</option>
        </select>
    </div>
    <div class="form-group">
        <label for="gender">Gender (optional)</label>
        <select id="gender" name="gender">
            <option value="">-- Select --</option>
            <option value="male">Male</option>
            <option value="female">Female</option>
            <option value="nonbinary">Non-binary</option>
            <option value="decline">Decline to self-identify</option>
        </select>
    </div>
    <h3 class="question-text">Why are you interested in this role?</h3>
    <div class="form-group">
        <label for="cover_letter">Cover Letter / Additional Information</label>
        <textarea id="cover_letter" name="cover_letter" rows="5"></textarea>
    </div>
    <h3 class="question-text">Do you have experience with SIEM tools? If so, which ones?</h3>
    <div class="form-group">
        <label for="siem_experience">SIEM Experience</label>
        <textarea id="siem_experience" name="siem_experience" rows="3"></textarea>
    </div>
    <button type="submit">Submit Application</button>
</form>
</body></html>"""

INTERSTITIAL_PAGE_HTML = """<!DOCTYPE html>
<html><head><title>Security Engineer - Acme Corp</title></head>
<body>
<h1>Security Engineer</h1>
<h2>Acme Corp</h2>
<p>Denver, CO | Full-time | $90,000 - $130,000</p>
<p>We are looking for a talented Security Engineer to join our team...</p>
<div class="actions">
    <a href="/apply" class="btn" role="button">Apply Now</a>
    <a href="/save" class="btn" role="button">Save Job</a>
    <a href="/share" class="btn" role="button">Share</a>
    <button type="button">Sign In</button>
    <a href="/jobs" class="btn" role="button">Back to Jobs</a>
</div>
</body></html>"""

SUCCESS_PAGE_HTML = """<!DOCTYPE html>
<html><head><title>Application Submitted</title></head>
<body>
<h1>Thank you for applying!</h1>
<p>Your application for Security Engineer at Acme Corp has been received.</p>
<p>We will review your application and get back to you within 5 business days.</p>
<a href="/jobs" class="btn" role="button">Browse More Jobs</a>
</body></html>"""


def load_config():
    config_path = Path(__file__).resolve().parent / "config.yaml"
    with open(config_path) as f:
        return yaml.safe_load(f)


def pp(label, data):
    print(f"\n{'='*60}")
    print(f"  {label}")
    print(f"{'='*60}")
    if isinstance(data, (dict, list)):
        print(json.dumps(data, indent=2, ensure_ascii=False, default=str))
    else:
        print(data)


async def test_extract_and_classify(page, html, name, expected_type, config):
    """Load HTML and test extraction + classification."""
    await page.set_content(html, wait_until="domcontentloaded")
    await page.wait_for_timeout(500)

    print(f"\n\n{'#'*60}")
    print(f"# TEST: {name}")
    print(f"{'#'*60}")

    snapshot = await extract_page_state(page)
    print(f"\n  Inputs:  {len(snapshot.get('inputs', []))}")
    print(f"  Buttons: {len(snapshot.get('buttons', []))}")
    print(f"  Text:    {len(snapshot.get('text_blocks', []))}")

    if snapshot.get("inputs"):
        pp("Inputs", snapshot["inputs"])
    if snapshot.get("buttons"):
        pp("Buttons", snapshot["buttons"])
    if snapshot.get("text_blocks"):
        pp("Text blocks", snapshot["text_blocks"])

    classification = await classify_page(snapshot, config)
    pp("Classification", classification)

    match = classification.get("type") == expected_type
    print(f"\n  Expected: {expected_type} | Got: {classification.get('type')} | {'PASS' if match else 'FAIL'}")

    return snapshot, classification


async def test_form_filling(snapshot, config, profile, job):
    """Test field mapping, validation, and question answering."""
    print("\n\n--- Form Field Mapping ---")
    mappings = await map_form_fields(snapshot, profile, config)
    pp("Raw AI mappings", mappings)

    print("\n--- Validation (Anti-Fabrication) ---")
    validated = validate_field_mapping(mappings, profile, strict=True)
    pp("Validated mappings", validated)

    rejected = len(mappings) - len(validated)
    print(f"\n  Mapped: {len(mappings)} | Passed: {len(validated)} | Rejected: {rejected}")

    # Cross-reference with what we expect
    expected_fields = {
        "first_name": "Jane",
        "last_name": "Doe",
        "email": "jane.doe@example.com",
        "phone": "555-555-5555",
        "location": "Denver, CO",
    }
    print("\n--- Expected vs Actual ---")
    for mapping in validated:
        idx = mapping["index"]
        val = mapping["value"]
        src = mapping["source"]
        # Find the input by index
        inp = next((i for i in snapshot["inputs"] if i["index"] == idx), None)
        field_name = inp.get("name", inp.get("id", "?")) if inp else "?"
        expected = expected_fields.get(field_name, "?")
        status = "OK" if expected == "?" or expected.lower() in val.lower() else "CHECK"
        print(f"  [{status}] {field_name}: '{val}' (source: {src})")

    print("\n\n--- Question Answering ---")
    answers = await answer_questions(snapshot, profile, job, config)
    pp("Answers", answers)

    return validated, answers


async def test_navigation(snapshot, config):
    """Test navigation target selection."""
    print("\n\n--- Navigation Target ---")
    nav = await pick_navigation_target(snapshot, config)
    pp("Navigation decision", nav)
    if nav["index"] >= 0 and nav["index"] < len(snapshot.get("buttons", [])):
        btn = snapshot["buttons"][nav["index"]]
        print(f"  Would click: \"{btn.get('text', '').strip()[:60]}\"")
    return nav


async def main():
    config = load_config()
    profile = config.get("profile", {})
    job = {"title": "Security Engineer", "company": "Acme Corp"}

    print("="*60)
    print("  AI Navigator Phase 2 Test")
    print("="*60)
    nav_model = config.get("ai", {}).get("models", {}).get("fast", {}).get("model", "?")
    print(f"  Navigator model: {nav_model}")
    print(f"  LM Studio URL:  {config.get('ai', {}).get('base_url', '?')}")

    from backend.ai_engine import test_connection
    conn = await test_connection(config)
    if not conn.get("connected"):
        print(f"\n  ERROR: Cannot connect to LM Studio: {conn.get('error')}")
        return
    print(f"  Connected! Models: {conn.get('models')}")

    from playwright.async_api import async_playwright
    async with async_playwright() as pw:
        browser = await pw.firefox.launch(headless=True)
        page = await browser.new_page()

        # Test 1: Application form
        snapshot, cls = await test_extract_and_classify(
            page, FORM_PAGE_HTML, "Application Form", "form", config
        )
        if cls.get("type") == "form":
            await test_form_filling(snapshot, config, profile, job)

        # Test 2: Interstitial page
        snapshot, cls = await test_extract_and_classify(
            page, INTERSTITIAL_PAGE_HTML, "Interstitial Page", "interstitial", config
        )
        await test_navigation(snapshot, config)

        # Test 3: Success page
        await test_extract_and_classify(
            page, SUCCESS_PAGE_HTML, "Success Page", "success", config
        )

        await browser.close()

    print(f"\n\n{'='*60}")
    print("  All tests complete!")
    print(f"{'='*60}")


if __name__ == "__main__":
    asyncio.run(main())
