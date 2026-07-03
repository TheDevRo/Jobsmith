"""
page_extractor.py — Extract a compact, AI-readable snapshot of the current page state.

Runs JavaScript in the Playwright page to gather all visible interactive elements
(inputs, buttons, selects, textareas, links) along with their labels, types, and
attributes.  The output is a structured dict small enough (~1500-2000 tokens) to
fit in a local model's context window alongside a prompt.

Each element includes a stable CSS selector for reliable re-targeting after SPA
re-renders, plus bounding rect data for prominence scoring.
"""

import logging
from typing import Any

logger = logging.getLogger(__name__)

# Maximum number of options to include per <select> element before truncating.
_MAX_SELECT_OPTIONS = 8

# Maximum character length for any single text value in the snapshot.
_MAX_TEXT_LEN = 200

# JavaScript executed inside the page to collect interactive element metadata.
# Returns a JSON-serialisable object.
_EXTRACT_JS = """
() => {
    const MAX_OPTIONS = """ + str(_MAX_SELECT_OPTIONS) + """;
    const MAX_TEXT = """ + str(_MAX_TEXT_LEN) + """;

    function truncate(s, len) {
        if (!s) return '';
        s = s.trim().replace(/\\s+/g, ' ');
        return s.length > len ? s.slice(0, len) + '...' : s;
    }

    function isVisible(el) {
        if (!el) return false;
        const style = window.getComputedStyle(el);
        if (style.display === 'none' || style.visibility === 'hidden' || parseFloat(style.opacity) < 0.1) return false;
        const rect = el.getBoundingClientRect();
        return rect.width > 0 && rect.height > 0;
    }

    // Stricter check for buttons: must be in or near the visible viewport.
    function isButtonVisible(el) {
        if (!isVisible(el)) return false;
        const rect = el.getBoundingClientRect();
        const vw = window.innerWidth || document.documentElement.clientWidth;
        const vh = window.innerHeight || document.documentElement.clientHeight;
        return rect.right > -100 && rect.left < vw + 100 &&
               rect.bottom > -100 && rect.top < vh + 600;
    }

    // Build a stable CSS selector for an element.
    // Priority: #id > [data-testid] > [name] > nth-of-type chain
    function buildSelector(el) {
        // 1. Unique ID
        if (el.id && document.querySelectorAll('#' + CSS.escape(el.id)).length === 1) {
            return '#' + CSS.escape(el.id);
        }
        // 2. data-testid or data-automation-id (common in ATS platforms)
        for (const attr of ['data-testid', 'data-automation-id', 'data-qa', 'data-test']) {
            const val = el.getAttribute(attr);
            if (val) {
                const sel = '[' + attr + '=' + JSON.stringify(val) + ']';
                if (document.querySelectorAll(sel).length === 1) return sel;
            }
        }
        // 3. name attribute (unique)
        if (el.name) {
            const tag = el.tagName.toLowerCase();
            const sel = tag + '[name=' + JSON.stringify(el.name) + ']';
            if (document.querySelectorAll(sel).length === 1) return sel;
        }
        // 4. type + aria-label combo
        if (el.getAttribute('aria-label')) {
            const tag = el.tagName.toLowerCase();
            const sel = tag + '[aria-label=' + JSON.stringify(el.getAttribute('aria-label')) + ']';
            if (document.querySelectorAll(sel).length === 1) return sel;
        }
        // 5. Build a path using nth-of-type from the nearest ancestor with an id
        let path = [];
        let current = el;
        while (current && current !== document.body && current !== document.documentElement) {
            let seg = current.tagName.toLowerCase();
            if (current.id && document.querySelectorAll('#' + CSS.escape(current.id)).length === 1) {
                path.unshift('#' + CSS.escape(current.id));
                break;
            }
            // Add nth-of-type for disambiguation
            const parent = current.parentElement;
            if (parent) {
                const siblings = Array.from(parent.children).filter(c => c.tagName === current.tagName);
                if (siblings.length > 1) {
                    const idx = siblings.indexOf(current) + 1;
                    seg += ':nth-of-type(' + idx + ')';
                }
            }
            path.unshift(seg);
            current = current.parentElement;
        }
        if (path.length === 0) {
            path.push(el.tagName.toLowerCase());
        }
        const fullSel = path.join(' > ');
        // Verify uniqueness
        try {
            if (document.querySelectorAll(fullSel).length === 1) return fullSel;
        } catch(e) {}
        // Fallback: tag + all classes
        const tag = el.tagName.toLowerCase();
        if (el.className && typeof el.className === 'string') {
            const classes = el.className.trim().split(/\\s+/).filter(Boolean).map(c => '.' + CSS.escape(c)).join('');
            if (classes) {
                const classSel = tag + classes;
                try {
                    if (document.querySelectorAll(classSel).length === 1) return classSel;
                } catch(e) {}
            }
        }
        return fullSel;
    }

    function labelFor(el) {
        // 1. Explicit <label for="id">
        if (el.id) {
            const lbl = document.querySelector('label[for="' + CSS.escape(el.id) + '"]');
            if (lbl) return truncate(lbl.textContent, MAX_TEXT);
        }
        // 2. Wrapping <label>
        const parent = el.closest('label');
        if (parent) return truncate(parent.textContent, MAX_TEXT);
        // 3. aria-label / aria-labelledby
        if (el.getAttribute('aria-label')) return truncate(el.getAttribute('aria-label'), MAX_TEXT);
        const labelledBy = el.getAttribute('aria-labelledby');
        if (labelledBy) {
            const ref = document.getElementById(labelledBy);
            if (ref) return truncate(ref.textContent, MAX_TEXT);
        }
        // 4. Previous sibling text or nearby heading
        const prev = el.previousElementSibling;
        if (prev && ['LABEL', 'SPAN', 'P', 'DIV', 'H3', 'H4', 'H5'].includes(prev.tagName)) {
            const t = truncate(prev.textContent, MAX_TEXT);
            if (t.length > 0 && t.length < 120) return t;
        }
        return '';
    }

    function getRect(el) {
        const r = el.getBoundingClientRect();
        return {x: Math.round(r.x), y: Math.round(r.y), w: Math.round(r.width), h: Math.round(r.height)};
    }

    // Collect inputs, textareas, selects
    const inputs = [];
    const inputEls = document.querySelectorAll('input, textarea, select');
    let idx = 0;
    for (const el of inputEls) {
        if (!isVisible(el)) continue;
        const tag = el.tagName.toLowerCase();
        const entry = {
            index: idx++,
            selector: buildSelector(el),
            tag: tag,
            type: el.type || '',
            name: el.name || '',
            id: el.id || '',
            label: labelFor(el),
            placeholder: truncate(el.placeholder, MAX_TEXT),
            required: el.required || el.getAttribute('aria-required') === 'true',
            value: truncate(el.value, MAX_TEXT),
            rect: getRect(el),
        };
        if (tag === 'select') {
            const opts = Array.from(el.options).map(o => o.text.trim()).filter(Boolean);
            if (opts.length > MAX_OPTIONS) {
                entry.options = opts.slice(0, MAX_OPTIONS);
                entry.options_truncated = opts.length;
            } else {
                entry.options = opts;
            }
        }
        if (el.type === 'radio' || el.type === 'checkbox') {
            entry.checked = el.checked;
        }
        if (el.type === 'file') {
            entry.accept = el.accept || '';
        }
        inputs.push(entry);
    }

    // Collect buttons and submit-like links
    const buttons = [];
    let bIdx = 0;
    const btnEls = document.querySelectorAll('button, input[type="submit"], input[type="button"], [role="button"], [class*="apply-btn" i], [class*="apply-button" i]');
    for (const el of btnEls) {
        if (!isButtonVisible(el)) continue;
        const tag = el.tagName.toLowerCase();
        buttons.push({
            index: bIdx++,
            selector: buildSelector(el),
            tag: tag,
            text: truncate(el.textContent || el.value || '', MAX_TEXT),
            type: el.type || '',
            href: tag === 'a' ? (el.getAttribute('href') || '') : '',
            ariaLabel: el.getAttribute('aria-label') || '',
            rect: getRect(el),
        });
    }

    // Also capture prominent <a> links that look like apply buttons but aren't
    // matched by the button selector (common on Adzuna, Indeed, etc.)
    const linkEls = document.querySelectorAll('a[href]');
    for (const el of linkEls) {
        if (!isButtonVisible(el)) continue;
        const text = (el.textContent || '').trim().toLowerCase();
        // Only capture links with apply-like text
        const applyPattern = /^(apply|apply now|apply online|apply for|quick apply|easy apply|apply today|apply for this job|start application|begin application)/;
        if (!applyPattern.test(text)) continue;
        // Skip if already captured as a button
        const sel = buildSelector(el);
        if (buttons.some(b => b.selector === sel)) continue;
        buttons.push({
            index: bIdx++,
            selector: sel,
            tag: 'a',
            text: truncate(el.textContent || '', MAX_TEXT),
            type: '',
            href: el.getAttribute('href') || '',
            ariaLabel: el.getAttribute('aria-label') || '',
            rect: getRect(el),
        });
    }

    // Collect prominent text blocks near form elements (questions, instructions)
    const textBlocks = [];
    const seen = new Set();
    const textEls = document.querySelectorAll(
        'legend, fieldset > label, .field-label, .question-text, ' +
        '[class*="question"], [class*="Question"], ' +
        'h1, h2, h3, h4, .form-group > label, .form-field > label'
    );
    for (const el of textEls) {
        if (!isVisible(el)) continue;
        const t = truncate(el.textContent, MAX_TEXT);
        if (t.length > 3 && !seen.has(t)) {
            seen.add(t);
            textBlocks.push(t);
        }
        if (textBlocks.length >= 30) break;
    }

    return {
        url: window.location.href,
        title: document.title,
        inputs: inputs,
        buttons: buttons,
        text_blocks: textBlocks,
    };
}
"""


async def extract_page_state(page) -> dict[str, Any]:
    """Run the extraction JS and return a structured page snapshot.

    Parameters
    ----------
    page : playwright.async_api.Page
        The Playwright page to extract state from.

    Returns
    -------
    dict
        A compact representation of all visible interactive elements, suitable
        for passing to an LLM.  Keys: url, title, inputs, buttons, text_blocks.
        Each input/button includes a `selector` field for stable re-targeting.
    """
    try:
        snapshot = await page.evaluate(_EXTRACT_JS)
        logger.debug(
            "Extracted page state: %d inputs, %d buttons, %d text blocks",
            len(snapshot.get("inputs", [])),
            len(snapshot.get("buttons", [])),
            len(snapshot.get("text_blocks", [])),
        )
        return snapshot
    except Exception:
        logger.exception("Page state extraction failed")
        return {
            "url": page.url,
            "title": "",
            "inputs": [],
            "buttons": [],
            "text_blocks": [],
        }


def snapshot_summary(snapshot: dict) -> str:
    """Return a one-line human-readable summary of the snapshot for logging."""
    n_inputs = len(snapshot.get("inputs", []))
    n_buttons = len(snapshot.get("buttons", []))
    n_text = len(snapshot.get("text_blocks", []))
    return (
        f"[{snapshot.get('url', '?')}] "
        f"{n_inputs} inputs, {n_buttons} buttons, {n_text} text blocks"
    )


def snapshot_to_text(snapshot: dict) -> str:
    """Convert a page snapshot to a human-readable text description for AI prompts.

    Instead of raw JSON, this produces a concise natural-language description
    that local LLMs can parse more reliably.
    """
    lines = []
    url = snapshot.get("url", "")
    title = snapshot.get("title", "")
    lines.append(f"PAGE: {title}")
    lines.append(f"URL: {url}")
    lines.append("")

    text_blocks = snapshot.get("text_blocks", [])
    if text_blocks:
        lines.append("VISIBLE TEXT:")
        for t in text_blocks[:15]:
            lines.append(f"  - {t}")
        lines.append("")

    inputs = snapshot.get("inputs", [])
    if inputs:
        lines.append("FORM FIELDS:")
        for inp in inputs:
            tag = inp.get("tag", "?")
            field_type = inp.get("type", "")
            label = inp.get("label", "")
            name = inp.get("name", "")
            placeholder = inp.get("placeholder", "")
            value = inp.get("value", "")
            required = " (required)" if inp.get("required") else ""

            # Build a readable description
            desc = f"  [{inp['index']}] "
            if tag == "select":
                opts = inp.get("options", [])
                opt_str = ", ".join(opts[:6])
                if len(opts) > 6:
                    opt_str += f"... ({len(opts)} total)"
                desc += f"Dropdown"
                if label:
                    desc += f" '{label}'"
                desc += f": options=[{opt_str}]"
            elif tag == "textarea":
                desc += f"Text area"
                if label:
                    desc += f" '{label}'"
                elif placeholder:
                    desc += f" (placeholder: '{placeholder}')"
            elif field_type == "file":
                desc += f"File upload"
                if label:
                    desc += f" '{label}'"
                accept = inp.get("accept", "")
                if accept:
                    desc += f" (accepts: {accept})"
            elif field_type in ("checkbox", "radio"):
                checked = " [checked]" if inp.get("checked") else ""
                desc += f"{field_type.title()}{checked}"
                if label:
                    desc += f" '{label}'"
            else:
                desc += f"Input"
                if field_type and field_type not in ("text",):
                    desc += f" (type={field_type})"
                if label:
                    desc += f" '{label}'"
                elif name:
                    desc += f" (name={name})"
                elif placeholder:
                    desc += f" (placeholder: '{placeholder}')"

            if value:
                desc += f" = '{value}'"
            desc += required
            lines.append(desc)
        lines.append("")

    buttons = snapshot.get("buttons", [])
    if buttons:
        lines.append("BUTTONS:")
        for btn in buttons:
            text = btn.get("text", "").strip()
            aria = btn.get("ariaLabel", "").strip()
            href = btn.get("href", "")
            tag = btn.get("tag", "?")

            desc = f"  [{btn['index']}] "
            display = text or aria or "(no text)"
            desc += f'"{display}"'
            if tag == "a" and href:
                desc += f" → {href[:80]}"
            lines.append(desc)
        lines.append("")

    return "\n".join(lines)
