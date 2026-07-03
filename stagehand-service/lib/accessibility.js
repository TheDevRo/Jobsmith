/**
 * Capture form field labels and ARIA info from a page for LLM context.
 *
 * Stagehand 3.x uses V3Page (CDP-backed), not a standard Playwright Page, so
 * page.accessibility.snapshot() is unavailable. Instead we use page.evaluate()
 * to extract the same semantic information directly from the DOM.
 */

/**
 * Return a compact accessibility-style summary of interactive elements for
 * injection into every LLM prompt.
 *
 * @param {object} page - V3Page from stagehand.context.activePage()
 * @returns {Promise<string>}
 */
export async function getAccessibilityContext(page) {
  try {
    const { formLines, buttonLines } = await page.evaluate(() => {
      const formLines = [];
      const buttonLines = [];

      // ── Form fields ────────────────────────────────────────────────────────
      // Limit to first 50 interactive elements to avoid token overflow
      const elements = Array.from(document.querySelectorAll(
        'input, select, textarea, button, [role="textbox"], [role="combobox"], [role="listbox"], [role="radio"], [role="checkbox"]'
      )).slice(0, 50);

      elements.forEach(el => {
        const tag  = el.tagName.toLowerCase();
        const type = el.getAttribute('type') || el.tagName.toLowerCase();
        const role = el.getAttribute('role') || '';

        // Resolve label text from multiple sources
        let label = '';
        if (el.id) {
          const labelEl = document.querySelector(`label[for="${el.id}"]`);
          if (labelEl) label = labelEl.textContent.trim();
        }
        if (!label && el.labels && el.labels.length > 0) {
          label = el.labels[0].textContent.trim();
        }
        if (!label) label = el.getAttribute('aria-label') || '';
        if (!label) label = el.getAttribute('placeholder') || '';
        if (!label) label = el.getAttribute('name') || '';
        if (!label && el.closest('label')) {
          label = el.closest('label').textContent.trim();
        }
        if (!label) {
          const prev = el.previousElementSibling;
          if (prev && (prev.tagName === 'LABEL' || prev.tagName === 'SPAN')) {
            label = prev.textContent.trim();
          }
        }

        if (!label && tag === 'button') label = el.textContent.trim().slice(0, 60);

        const required = el.required ? ' [required]' : '';
        const value    = el.value && el.type !== 'password' ? ` [value: "${el.value.slice(0, 40)}"]` : '';

        let entry = `${tag}[${role || type}]`;
        if (label) entry += ` "${label}"`;
        entry += value + required;

        formLines.push(entry);
      });

      // ── Visible buttons ────────────────────────────────────────────────────
      // Gives the model a concise list of actionable buttons with their data-*
      // attributes, so it can identify the right target without re-perceiving.
      const buttons = Array.from(document.querySelectorAll(
        'button:not([disabled]), [role="button"]:not([aria-disabled="true"]), input[type="submit"]:not([disabled])'
      )).slice(0, 20); // Limit buttons too
      buttons.forEach(el => {
        const rect   = el.getBoundingClientRect();
        const inView = rect.width > 0 && rect.height > 0 && rect.top < window.innerHeight && rect.bottom > 0;
        const text   = (el.textContent || el.value || el.getAttribute('aria-label') || '').trim().replace(/\s+/g, ' ').slice(0, 80);
        const autoId = el.getAttribute('data-automation-id') || el.getAttribute('data-testid') || '';
        if (!text) return;
        let line = `BUTTON: "${text}"`;
        if (autoId) line += ` [data-id="${autoId}"]`;
        line += inView ? ' [VISIBLE]' : ' [BELOW_FOLD]';
        buttonLines.push(line);
      });

      return { formLines, buttonLines };
    });

    const parts = [];
    if (buttonLines.length > 0) parts.push('VISIBLE BUTTONS:\n' + buttonLines.join('\n'));
    if (formLines.length > 0)   parts.push('FORM FIELDS:\n' + formLines.join('\n'));
    return parts.join('\n\n');
  } catch {
    return '';
  }
}
