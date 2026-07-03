/**
 * Shared helpers used by all ATS handler modules.
 *
 * Handles the common sub-tasks every handler needs:
 *   - Extracting and mapping form fields to profile values
 *   - Filling text/select/radio/checkbox fields via Stagehand act()
 *   - Uploading resume files via Playwright setInputFiles()
 *   - Answering free-text questions via Q&A cache → LM Studio
 *   - Taking debug screenshots
 *   - Detecting success/failure page states
 */

import path from 'path';
import fs from 'fs';
import { fileURLToPath } from 'url';
import { z } from 'zod';
import { actWithRetry, safeExtract, fillFormDirectly } from '../lib/stagehand-client.js';
import { getAccessibilityContext } from '../lib/accessibility.js';
import { mapFieldsToProfile, generateAnswer } from '../lib/lm-studio.js';
import { getCachedAnswer, setCachedAnswer } from '../lib/qa-cache.js';
import { isLowConfidence } from '../lib/confidence.js';

const __dirname = path.dirname(fileURLToPath(import.meta.url));

// ─── Screenshot ──────────────────────────────────────────────────────────────

/**
 * Take a screenshot and return the saved file path (or null on error).
 * @param {import('playwright').Page} page
 * @param {string} jobId
 * @param {string} label
 * @returns {Promise<string|null>}
 */
export async function takeScreenshot(page, jobId, label) {
  try {
    const screenshotsDir = process.env.SCREENSHOTS_DIR
      ? path.resolve(__dirname, '..', process.env.SCREENSHOTS_DIR)
      : path.resolve(__dirname, '../../../data/screenshots');

    fs.mkdirSync(screenshotsDir, { recursive: true });

    const ts = Date.now();
    const filename = `${jobId}_${label}_${ts}.png`;
    const filepath = path.join(screenshotsDir, filename);
    await page.screenshot({ path: filepath, fullPage: true });
    return filepath;
  } catch {
    return null;
  }
}

// ─── Field extraction schema ─────────────────────────────────────────────────

const FieldSchema = z.object({
  fields: z.array(z.object({
    label:    z.string(),
    type:     z.enum(['text', 'email', 'tel', 'number', 'textarea', 'select', 'radio', 'checkbox', 'file', 'other']).default('text'),
    selector: z.string().optional(),
    options:  z.array(z.string()).optional(),
    required: z.boolean().optional(),
  })),
});

/**
 * Extract all visible form fields from the current page.
 * Stagehand 3.x: extract(instruction, schema)
 * @param {import('@browserbasehq/stagehand').Stagehand} stagehand
 * @returns {Promise<Array>}
 */
export async function extractFields(stagehand) {
  const result = await safeExtract(
    stagehand,
    'Extract every visible form field on this page. For each field include its label, input type, and any available options (for selects, radios, checkboxes).',
    FieldSchema,
  );
  return result?.fields ?? [];
}

// ─── Field filling ───────────────────────────────────────────────────────────

/**
 * Fill all mappable fields on the current page step.
 *
 * Returns a list of {field, reason} entries for any fields that were flagged
 * as needs_review due to low-confidence answers.
 *
 * @param {import('@browserbasehq/stagehand').Stagehand} stagehand
 * @param {import('playwright').Page} page
 * @param {Array} fields       - From extractFields()
 * @param {Object} profile     - User profile
 * @param {Object} job         - { title, company, description }
 * @param {string} a11yContext - From getAccessibilityContext()
 * @param {Function} addLog    - Logger from createLogger()
 * @param {string} [resumePath] - Absolute path to the resume file
 * @returns {Promise<Array<{field: string, reason: string}>>} low-confidence flags
 */
export async function fillPageFields(stagehand, page, fields, profile, job, a11yContext, addLog, resumePath) {
  const flagged = [];

  if (fields.length === 0) return flagged;

  // Separate file inputs from fillable fields
  const fileFields  = fields.filter(f => f.type === 'file');
  const textFields  = fields.filter(f => f.type !== 'file');

  // ── Resume upload ─────────────────────────────────────────────────────────
  for (const field of fileFields) {
    if (!resumePath) {
      addLog('resume_upload', `Skipping file upload — no resume path provided`);
      continue;
    }
    try {
      // V3Page uses locator() instead of $(); V3Locator supports setInputFiles
      await page.locator('input[type="file"]').setInputFiles(resumePath);
      addLog('resume_upload', `Uploaded resume: ${resumePath}`);
    } catch (err) {
      addLog('resume_upload', `Resume upload failed`, { error: err.message });
    }
  }

  if (textFields.length === 0) return flagged;

  // ── Map text/select/radio fields to profile values ────────────────────────
  let mapping = {};
  try {
    mapping = await mapFieldsToProfile(textFields, profile, a11yContext);
  } catch (err) {
    addLog('field_mapping', 'LM Studio field mapping failed — filling known fields manually', { error: err.message });
    mapping = buildDirectMapping(textFields, profile);
  }

  // ── Fill each field ───────────────────────────────────────────────────────
  for (const field of textFields) {
    const rawValue = mapping[field.label];

    // null → no match from profile; skip
    if (rawValue === null || rawValue === undefined) continue;

    // Free-text / textarea: may need Q&A cache or LM Studio
    if (field.type === 'textarea' || isLikelyQuestion(field.label)) {
      const qaResult = await resolveQuestion(field.label, profile, job, a11yContext);
      if (qaResult.lowConfidence) {
        flagged.push({ field: field.label, reason: 'low-confidence answer' });
        addLog('qa_answer', `Low-confidence answer for "${field.label}" — flagging needs_review`);
        // Skip filling this field; human will review
        continue;
      }
      const result = await actWithRetry(
        stagehand, page,
        `Fill the "${field.label}" field with: ${qaResult.answer}`,
      );
      addLog('fill_field', `Fill textarea "${field.label}"`, result.success ? {} : { error: result.error });
      continue;
    }

    // File sentinel was returned for a non-file field (shouldn't happen, skip)
    if (rawValue === '__RESUME__') continue;

    // Standard field
    let result;
    const directFilled = await fillFormDirectly(page, field, rawValue);
    if (directFilled) {
      result = { success: true, via: 'direct' };
    } else {
      const action = buildFillAction(field, rawValue);
      result = await actWithRetry(stagehand, page, action);
    }

    addLog('fill_field', `Fill "${field.label}" → "${String(rawValue).slice(0, 60)}"`,
      result.success ? {} : { error: result.error, page_url: page.url() });
  }

  return flagged;
}

// ─── Q&A resolution ──────────────────────────────────────────────────────────

/**
 * Check Q&A cache first; generate with LM Studio if not cached.
 * @returns {Promise<{answer: string, lowConfidence: boolean}>}
 */
async function resolveQuestion(question, profile, job, a11yContext) {
  // Cache lookup
  const cached = getCachedAnswer(question);
  if (cached) {
    return { answer: cached.answer, lowConfidence: cached.confidence === 'low' };
  }

  // Generate via LM Studio
  let answer;
  try {
    answer = await generateAnswer(question, profile, job, a11yContext);
  } catch {
    return { answer: '', lowConfidence: true };
  }

  const lowConfidence = isLowConfidence(answer, 'textarea');
  setCachedAnswer(question, answer, lowConfidence ? 'low' : 'high');

  return { answer, lowConfidence };
}

// ─── Auth wall detection ──────────────────────────────────────────────────────

const AUTH_WALL_TEXT = [
  /create (an? )?account/i,
  /sign (in|up) to (apply|continue)/i,
  /log ?in (to|and) (apply|continue)/i,
  /you must (be |)(logged in|signed in)/i,
  /register (to apply|an account)/i,
];

const SSO_ONLY_TEXT = [
  /sign in with google/i,
  /sign in with microsoft/i,
  /sign in with apple/i,
  /continue with google/i,
  /continue with microsoft/i,
];

/**
 * Detect whether the current page is gating the application behind a login/
 * account-creation wall and characterise what kind of wall it is.
 *
 * @param {import('playwright').Page} page
 * @returns {Promise<{detected: boolean, canAttemptLogin: boolean, isSSOOnly: boolean}>}
 */
export async function detectAuthWall(page) {
  try {
    const content = await page.evaluate(() => document.documentElement.outerHTML).catch(() => '');

    const hasAuthText = AUTH_WALL_TEXT.some(p => p.test(content));
    if (!hasAuthText) return { detected: false, canAttemptLogin: false, isSSOOnly: false };

    const isSSOOnly = SSO_ONLY_TEXT.some(p => p.test(content));
    const hasPasswordField = await page.locator('input[type="password"]')
      .isVisible({ timeout: 500 }).catch(() => false);
    const hasEmailField = await page.locator('input[type="email"], input[name*="email" i]')
      .isVisible({ timeout: 500 }).catch(() => false);

    return {
      detected: true,
      canAttemptLogin: hasPasswordField && hasEmailField && !isSSOOnly,
      isSSOOnly,
    };
  } catch {
    return { detected: false, canAttemptLogin: false, isSSOOnly: false };
  }
}

// ─── Step stall detection ─────────────────────────────────────────────────────

/**
 * Check whether a step-advance actually moved the form forward.
 * Returns true if the page progressed (URL changed or significant DOM change).
 * Returns false if we're likely stuck on a validation error.
 *
 * @param {import('playwright').Page} page
 * @param {string} prevUrl - URL before advance
 * @param {number} prevFieldCount - Number of form inputs before advance
 * @returns {Promise<boolean>}
 */
export async function detectStepProgressed(page, prevUrl, prevFieldCount) {
  try {
    const currentUrl = page.url();
    if (currentUrl !== prevUrl) return true; // URL changed = clearly progressed

    const content = await page.evaluate(() => document.documentElement.outerHTML).catch(() => '');
    const currentFieldCount = (content.match(/<input|<select|<textarea/gi) || []).length;

    // Significant DOM change = new step rendered in-place (Greenhouse, Lever style)
    if (Math.abs(currentFieldCount - prevFieldCount) > 5) return true;

    // If inline validation errors are present, we're stuck
    const hasErrors = /aria-invalid="true"|class="[^"]*\b(error|invalid)\b|role="alert"/i.test(content);
    if (hasErrors) return false;

    return true;
  } catch {
    return true; // on error, assume progressed and let the handler deal with it
  }
}

// ─── Success / failure detection ─────────────────────────────────────────────

const SUCCESS_PATTERNS = [
  /thank you for (applying|your application)/i,
  /application (submitted|received|complete)/i,
  /we'?ve? received your application/i,
  /your application has been (sent|submitted|received)/i,
  /successfully (applied|submitted)/i,
  /application was sent/i,
  /you'?ve? applied/i,
];

const FAILURE_PATTERNS = [
  /job (listing |posting )?(is |has been )?(no longer|expired|closed|removed)/i,
  /position (has been |is )(filled|closed)/i,
  /this (job|posting|listing) is not available/i,
  /404|not found|page not found/i,
];

/**
 * Detect application success from current page content.
 * @param {import('playwright').Page} page
 * @returns {Promise<boolean>}
 */
export async function detectSuccess(page) {
  try {
    // V3Page: use evaluate() instead of content() (Playwright-only)
    const content = await page.evaluate(() => document.documentElement.outerHTML);
    return SUCCESS_PATTERNS.some(p => p.test(content));
  } catch {
    return false;
  }
}

/**
 * Detect a dead-end (expired listing, 404, etc.)
 * @param {object} page - V3Page
 * @returns {Promise<boolean>}
 */
export async function detectDeadEnd(page) {
  try {
    const content = await page.evaluate(() => document.documentElement.outerHTML);
    return FAILURE_PATTERNS.some(p => p.test(content));
  } catch {
    return false;
  }
}

// ─── Navigation ───────────────────────────────────────────────────────────────

/**
 * Click the next/continue/submit button on the current step.
 * Returns the action result.
 */
export async function advanceStep(stagehand, page, addLog, { isLastStep = false } = {}) {
  const action = isLastStep
    ? 'Click the Submit or Apply button to submit the application'
    : 'Click the Next, Continue, or Save and Continue button to advance to the next step';

  const result = await actWithRetry(stagehand, page, action);
  addLog('advance_step', action, result.success ? {} : { error: result.error, page_url: page.url() });

  if (result.success) {
    // Wait for navigation / DOM update
    await sleep(1500);
  }

  return result;
}

// ─── Internal helpers ─────────────────────────────────────────────────────────

function isLikelyQuestion(label) {
  const l = label.toLowerCase();
  return l.includes('why') || l.includes('describe') || l.includes('tell us') ||
         l.includes('explain') || l.includes('additional') || l.includes('cover letter') ||
         l.includes('message') || l.includes('comments') || l.endsWith('?');
}

function buildFillAction(field, value) {
  const v = String(value);
  if (field.type === 'select') {
    return `Select "${v}" from the "${field.label}" dropdown`;
  }
  if (field.type === 'radio') {
    return `Select the "${v}" option for "${field.label}"`;
  }
  if (field.type === 'checkbox') {
    const shouldCheck = /^(yes|true|1)$/i.test(v);
    return `${shouldCheck ? 'Check' : 'Uncheck'} the "${field.label}" checkbox`;
  }
  return `Fill the "${field.label}" field with "${v}"`;
}

/**
 * Fallback direct mapping when LM Studio is unavailable.
 * Maps common field label patterns to profile fields by heuristic.
 */
function buildDirectMapping(fields, profile) {
  const nameParts = (profile.full_name || '').split(' ');
  const firstName = nameParts[0] || '';
  const lastName  = nameParts.slice(1).join(' ') || '';

  const map = {
    'first name':          firstName,
    'last name':           lastName,
    'full name':           profile.full_name,
    'name':                profile.full_name,
    'email':               profile.email,
    'email address':       profile.email,
    'phone':               profile.phone,
    'phone number':        profile.phone,
    'mobile':              profile.phone,
    'address':             profile.street_address,
    'street address':      profile.street_address,
    'city':                profile.city,
    'state':               profile.state,
    'zip':                 profile.zip_code,
    'zip code':            profile.zip_code,
    'postal code':         profile.zip_code,
    'linkedin':            profile.linkedin,
    'linkedin url':        profile.linkedin,
    'linkedin profile':    profile.linkedin,
    'website':             profile.portfolio,
    'portfolio':           profile.portfolio,
    'salary':              profile.desired_salary,
    'desired salary':      profile.desired_salary,
    'salary expectation':  profile.desired_salary,
    'work authorization':  profile.work_authorization,
    'authorized to work':  profile.work_authorization,
    'sponsorship':         profile.sponsorship_required,
    'require sponsorship': profile.sponsorship_required,
    'gender':              profile.gender,
    'race':                profile.race_ethnicity,
    'ethnicity':           profile.race_ethnicity,
    'veteran':             profile.veteran_status,
    'disability':          profile.disability_status,
  };

  const result = {};
  for (const field of fields) {
    const key = field.label.toLowerCase().trim();
    result[field.label] = map[key] ?? null;
  }
  return result;
}

// ─── Shared sleep helper ──────────────────────────────────────────────────────
// Exported so handlers can use it instead of page.waitForTimeout() (Playwright-only)
export function sleep(ms) {
  return new Promise(resolve => setTimeout(resolve, ms));
}
