/**
 * Stagehand 3.x initialisation and retry wrapper.
 *
 * Layer 1 — Primary: Stagehand wraps Playwright with natural-language act()
 * and extract() calls backed by LM Studio via CustomOpenAIClient.
 *
 * Layer 3 — Fallback: after STAGEHAND_MAX_RETRIES failures, skyvernAct() is
 * called for that specific step. Stagehand resumes once Skyvern advances.
 */

import OpenAI from 'openai';
import { Stagehand, CustomOpenAIClient } from '@browserbasehq/stagehand';
import { skyvernAct } from './skyvern.js';
import { getAccessibilityContext } from './accessibility.js';
import { z } from 'zod';

const MAX_RETRIES = () => parseInt(process.env.STAGEHAND_MAX_RETRIES || '2', 10);

/**
 * Create and initialise a Stagehand 3.x instance backed by LM Studio.
 * Returns the stagehand instance; get the active page via stagehand.context.activePage()
 *
 * @param {object} opts
 * @param {boolean} [opts.headless] - Override per-request. Falls back to HEADLESS env var.
 * @returns {Promise<import('@browserbasehq/stagehand').Stagehand>}
 */
/**
 * Patch an OpenAI client so that `response_format: { type: "json_object" }` is
 * converted to `{ type: "text" }` before every API call.
 *
 * LM Studio only accepts "json_schema" or "text" — it rejects "json_object" with a
 * 400 error. Stagehand 3.x hard-codes "json_object" for all structured-output calls.
 * Since Stagehand already appends the JSON schema as a user message instruction, the
 * model receives full formatting guidance even in text mode.
 */
function patchClientForLmStudio(client) {
  const originalCreate = client.chat.completions.create.bind(client.chat.completions);
  client.chat.completions.create = async (body, options) => {
    if (body?.response_format?.type === 'json_object') {
      body = { ...body, response_format: { type: 'text' } };
    }
    return originalCreate(body, options);
  };
}

export async function initStagehand({ headless } = {}) {
  // Per-request headless flag wins; env var is the process-level default.
  const isHeadless = headless !== undefined ? headless : process.env.HEADLESS !== 'false';

  // Stagehand 3.x delegates headless to chrome-launcher via the HEADLESS env var.
  // chrome-launcher adds --headless whenever HEADLESS is set to any value,
  // so we must explicitly set or delete it to match the per-request intent.
  if (isHeadless) {
    process.env.HEADLESS = 'true';
  } else {
    delete process.env.HEADLESS;
  }

  const openaiClient = new OpenAI({
    apiKey: process.env.LM_STUDIO_API_KEY || 'lm-studio',
    baseURL: process.env.LM_STUDIO_BASE_URL || 'http://localhost:1234/v1',
  });

  // Must patch before passing to CustomOpenAIClient
  patchClientForLmStudio(openaiClient);

  const stagehand = new Stagehand({
    env: 'LOCAL',
    llmClient: new CustomOpenAIClient({
      modelName: process.env.LM_STUDIO_MODEL || 'local-model',
      client: openaiClient,
    }),
    verbose: 0,
    disablePino: true,
    localBrowserLaunchOptions: {
      headless: isHeadless,
      args: ['--no-sandbox', '--disable-dev-shm-usage'],
    },
  });

  await stagehand.init();
  return stagehand;
}

/**
 * Layer 0: Try to click an element directly via Playwright CSS/text selectors
 * without invoking the LLM at all. Returns true if the click succeeded.
 *
 * Covers all "progress application" actions: opening the apply modal, advancing
 * through multi-step forms, and final submission.
 *
 * @param {import('playwright').Page} page
 * @param {string} instruction
 * @returns {Promise<boolean>}
 */
async function tryDirectClick(page, instruction) {
  const i = instruction.toLowerCase();

  // Build an ordered list of Playwright locator expressions to try.
  // More specific selectors go first; broad ones go last.
  const candidates = [];

  // ── "Apply" / "Easy Apply" entry buttons ─────────────────────────────────
  if (i.includes('easy apply')) {
    candidates.push(
      'button:has-text("Easy Apply")',
      '[class*="jobs-apply-button"]',
      'button[aria-label*="Easy Apply" i]',
      'a:has-text("Easy Apply")',
    );
  }

  // ── Next / Continue / Save and continue (mid-form) ────────────────────────
  if (i.includes('next') || i.includes('continue') || i.includes('save and continue')) {
    candidates.push(
      'button:has-text("Next")',
      'button:has-text("Continue")',
      'button:has-text("Save and continue")',
      'button:has-text("Next step")',
      '[aria-label*="Continue" i]',
      '[aria-label*="Next step" i]',
      'input[type="button"][value*="Next" i]',
      'input[type="button"][value*="Continue" i]',
    );
  }

  // ── Submit / final Apply ──────────────────────────────────────────────────
  if (i.includes('submit') || i.includes('apply')) {
    candidates.push(
      // Dialog-scoped first — Workday's 3-choice popover needs these to win
      '[role="dialog"] button:has-text("Apply")',
      '[aria-modal="true"] button:has-text("Apply")',
      'button:has-text("Submit application")',
      'button:has-text("Submit Application")',
      'button:has-text("Submit")',
      // data-* ATS patterns
      'button[data-automation-id*="apply" i]',   // Workday
      'button[data-testid*="apply" i]',           // Greenhouse/Lever
      'button[data-action*="apply" i]',           // SmartRecruiters
      'button:has-text("Apply now")',
      'button:has-text("Apply Now")',
      'button:has-text("Apply for this job")',
      'button:has-text("Apply for Job")',
      'button:has-text("Quick Apply")',
      'a:has-text("Quick Apply")',
      'button:has-text("Apply")',
      'a[href*="apply"][class*="btn"]',
      'input[type="submit"]',
      'button[type="submit"]',
    );
  }

  // ── Generic: extract any quoted text from the instruction and try it ──────
  for (const m of instruction.matchAll(/"([^"]{2,40})"/g)) {
    const text = m[1];
    candidates.push(`button:has-text("${text}")`);
    candidates.push(`[role="button"]:has-text("${text}")`);
    candidates.push(`a:has-text("${text}")`);
  }

  // First pass: try all candidates in order (longer timeout for Easy Apply since
  // React SPAs render the button asynchronously)
  const isEasyApply = i.includes('easy apply');
  for (const sel of candidates) {
    try {
      const loc = page.locator(sel).first();
      const visibleTimeout = isEasyApply ? 4000 : 2000;
      if (await loc.isVisible({ timeout: visibleTimeout })) {
        await loc.scrollIntoViewIfNeeded().catch(() => {});
        await loc.click({ timeout: 5000 });
        return true;
      }
    } catch {
      // not found or not clickable — try next candidate
    }
  }

  // Scroll-to-find pass: for apply actions, scroll to mid-page and retry the
  // first 5 candidates — Apply buttons are often below the job description fold
  if (i.includes('apply') || i.includes('submit')) {
    try {
      await page.evaluate(() => window.scrollTo({ top: document.body.scrollHeight / 2, behavior: 'instant' }));
      await new Promise(r => setTimeout(r, 600));
      for (const sel of candidates.slice(0, 5)) {
        try {
          const loc = page.locator(sel).first();
          if (await loc.isVisible({ timeout: 1500 })) {
            await loc.scrollIntoViewIfNeeded().catch(() => {});
            await loc.click({ timeout: 5000 });
            return true;
          }
        } catch { /* continue */ }
      }
    } catch { /* ignore scroll errors */ }
  }

  return false;
}

/**
 * Attempt a Stagehand act() with automatic retry and Skyvern escalation.
 *
 * Layer 0 — Direct Playwright click by CSS/text selector (no LLM, fastest)
 * Layer 1 — Lightweight element picking (Compact LLM prompt)
 * Layer 2 — Stagehand LLM act() with prompt variation on each retry
 * Layer 3 — Skyvern fallback after MAX_RETRIES Stagehand failures
 *
 * @param {import('@browserbasehq/stagehand').Stagehand} stagehand
 * @param {import('playwright').Page} page
 * @param {string} instruction - Natural-language action description
 * @param {object} opts
 * @param {boolean} [opts.useSkyvernFallback=true]
 * @returns {Promise<{success: boolean, via: 'direct'|'stagehand'|'skyvern', error?: string}>}
 */
export async function actWithRetry(stagehand, page, instruction, { useSkyvernFallback = true } = {}) {
  // Layer 0: direct Playwright click — no LLM needed
  try {
    const clicked = await tryDirectClick(page, instruction);
    if (clicked) return { success: true, via: 'direct' };
  } catch {
    // fall through to Stagehand
  }

  // Layer 1: Lightweight element picking
  try {
    const picked = await pickElementFromList(stagehand, page, instruction);
    if (picked) return { success: true, via: 'direct' };
  } catch {
    // fall through to Stagehand
  }

  // Layer 2: Stagehand LLM with retries
  const max = MAX_RETRIES();
  let lastStagehandError = 'unknown error';

  for (let attempt = 0; attempt <= max; attempt++) {
    try {
      let prompt = instruction;

      // On retry attempts, inject the current DOM state so the model has
      // explicit element context rather than re-perceiving from scratch.
      if (attempt >= 1) {
        const domCtx = await getAccessibilityContext(page).catch(() => '');
        const ctxPrefix = domCtx ? `[PAGE STATE]\n${domCtx}\n\n[ACTION]\n` : '';
        if (attempt === 1) {
          prompt = `${ctxPrefix}Try again — ${instruction}`;
        } else {
          prompt = `${ctxPrefix}The previous attempt failed. The element must be one of the VISIBLE BUTTONS listed above. ${instruction}`;
        }
      }

      await stagehand.act(prompt);
      return { success: true, via: 'stagehand' };
    } catch (err) {
      lastStagehandError = err.message;
      if (attempt < max) {
        await sleep(800 * (attempt + 1));
        continue;
      }
    }
  }

  // Layer 3: Skyvern fallback
  if (useSkyvernFallback) {
    const pageUrl = page.url();
    const skyvernResult = await skyvernAct(pageUrl, instruction);
    if (skyvernResult.success) {
      await page.reload().catch(() => {});
      return { success: true, via: 'skyvern' };
    }
    return {
      success: false,
      via: 'skyvern',
      error: `stagehand: ${lastStagehandError} | skyvern: ${skyvernResult.error}`,
    };
  }

  return { success: false, via: 'stagehand', error: lastStagehandError };
}

/**
 * Run stagehand.extract() and return null on failure instead of throwing.
 * Stagehand 3.x signature: extract(instruction, schema, options?)
 *
 * @param {import('@browserbasehq/stagehand').Stagehand} stagehand
 * @param {string} instruction
 * @param {import('zod').ZodSchema} schema
 * @returns {Promise<any|null>}
 */
export async function safeExtract(stagehand, instruction, schema) {
  try {
    return await stagehand.extract(instruction, schema);
  } catch {
    return null;
  }
}

/**
 * Extract a compact list of interactive elements with persistent IDs.
 */
async function getCompactElementList(page) {
  return await page.evaluate(() => {
    const elements = document.querySelectorAll(
      'button:not([disabled]), [role="button"]:not([aria-disabled="true"]), input[type="submit"]:not([disabled]), a:not([disabled])'
    );
    const results = [];
    elements.forEach((el, index) => {
      const rect = el.getBoundingClientRect();
      const isVisible = rect.width > 0 && rect.height > 0 && rect.top < window.innerHeight && rect.bottom > 0;
      if (!isVisible) return;

      const id = `el-${index}`;
      el.setAttribute('data-stagehand-id', id);

      const text = (el.textContent || el.value || el.getAttribute('aria-label') || '').trim().replace(/\s+/g, ' ').slice(0, 50);
      if (!text && !el.getAttribute('data-automation-id')) return;

      results.push({
        id,
        text,
        tag: el.tagName.toLowerCase(),
        role: el.getAttribute('role') || '',
        automationId: el.getAttribute('data-automation-id') || el.getAttribute('data-testid') || ''
      });
    });
    return results;
  });
}

/**
 * Lightweight navigation: pick an element from a compact list using a small LLM prompt.
 */
async function pickElementFromList(stagehand, page, instruction) {
  const elements = await getCompactElementList(page);
  if (elements.length === 0) return false;

  const listStr = elements.map(el => `[${el.id}] ${el.tag}${el.role ? ':' + el.role : ''} "${el.text}" ${el.automationId ? '(' + el.automationId + ')' : ''}`).join('\n');

  const prompt = `Based on the following list of interactive elements, pick the ID of the element that best matches this instruction: "${instruction}"
If no element matches, return "none".

Elements:
${listStr}

Return ONLY the ID (e.g., "el-5") or "none".`;

  const response = await stagehand.extract(prompt, z.object({ id: z.string() }));
  const pickedId = response?.id;

  if (pickedId && pickedId !== 'none') {
    const selector = `[data-stagehand-id="${pickedId}"]`;
    const loc = page.locator(selector).first();
    if (await loc.isVisible({ timeout: 2000 })) {
      await loc.scrollIntoViewIfNeeded().catch(() => {});
      await loc.click({ timeout: 5000 });
      return true;
    }
  }
  return false;
}

/**
 * Direct form filling using Playwright locators by label/placeholder.
 */
export async function fillFormDirectly(page, field, value) {
  const label = field.label;
  const v = String(value);

  // Try different locator strategies for the field
  const strategies = [
    page.getByLabel(label, { exact: false }),
    page.getByPlaceholder(label, { exact: false }),
    page.locator(`input[name*="${label}" i]`),
    page.locator(`input[id*="${label}" i]`),
    page.locator(`textarea[name*="${label}" i]`),
    page.locator(`textarea[id*="${label}" i]`),
  ];

  for (const loc of strategies) {
    try {
      if (await loc.first().isVisible({ timeout: 1000 })) {
        const target = loc.first();
        if (field.type === 'select') {
          await target.selectOption({ label: v }).catch(() => target.selectOption(v));
        } else if (field.type === 'checkbox') {
          const shouldCheck = /^(yes|true|1)$/i.test(v);
          if (shouldCheck) await target.check();
          else await target.uncheck();
        } else if (field.type === 'radio') {
          // Radios usually need to match the value within the group
          const radioLoc = page.locator(`input[type="radio"][value*="${v}" i], label:has-text("${v}") input[type="radio"]`).first();
          if (await radioLoc.isVisible({ timeout: 500 })) {
            await radioLoc.check();
          } else {
            await target.check();
          }
        } else {
          await target.fill(v);
        }
        return true;
      }
    } catch {
      continue;
    }
  }
  return false;
}

function sleep(ms) {
  return new Promise(resolve => setTimeout(resolve, ms));
}
