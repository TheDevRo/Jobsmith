/**
 * Workday handler.
 *
 * Workday applications (myworkdayjobs.com / workday.com) are heavily
 * JavaScript-driven and often require authentication. The handler:
 *  1. Detects the Apply button on the job posting
 *  2. Handles the Workday sign-in if redirected to auth
 *  3. Steps through the multi-page Workday application wizard
 *
 * Workday pages render slowly — wait times are intentionally generous.
 */

import { actWithRetry } from '../lib/stagehand-client.js';
import { getAccessibilityContext } from '../lib/accessibility.js';
import {
  extractFields,
  fillPageFields,
  detectSuccess,
  takeScreenshot,
  advanceStep,
  sleep,
} from './shared.js';

export function detect(url) {
  return url.includes('myworkdayjobs.com') || url.includes('workday.com/');
}

export async function apply({ stagehand, page, job, profile, log, addLog }) {
  const flagged = [];
  let screenshotPath = null;
  const resumePath = job.resume_path || null;
  const MAX_STEPS = 25;

  try {
    addLog('workday_init', 'Starting Workday application', { page_url: page.url() });

    // ── 1. Find and click Apply ───────────────────────────────────────────────
    const applyResult = await actWithRetry(
      stagehand, page,
      'Click the Apply button to start the job application',
    );

    if (!applyResult.success) {
      // May already be on the application form
      addLog('workday_init', 'Could not find Apply button — checking if already on form', { page_url: page.url() });
    }

    await sleep(3000);

    // ── 2. Handle Workday sign-in ─────────────────────────────────────────────
    const pageContent = await page.evaluate(() => document.documentElement.outerHTML).catch(() => '');
    const isAuthPage = /sign in|create account|log in|workday account/i.test(pageContent);

    if (isAuthPage) {
      addLog('workday_auth', 'Workday sign-in required — attempting login');
      const loginResult = await attemptWorkdayLogin(stagehand, page, profile, addLog);
      if (!loginResult.success) {
        screenshotPath = await takeScreenshot(page, job.id, 'workday_auth_failed');
        addLog('workday_auth', 'Workday login failed', { error: loginResult.error, page_url: page.url() });
        return { status: 'needs_review', log, screenshot_path: screenshotPath };
      }
      await sleep(3000);
    }

    // ── 3. Application step loop ──────────────────────────────────────────────
    for (let step = 0; step < MAX_STEPS; step++) {
      addLog('workday_step', `Processing step ${step + 1}`, { page_url: page.url() });

      if (await detectSuccess(page)) {
        addLog('workday_success', 'Application submitted');
        return { status: 'submitted', log, screenshot_path: null };
      }

      const a11y = await getAccessibilityContext(page);
      const fields = await extractFields(stagehand);
      addLog('workday_step', `Found ${fields.length} fields on step ${step + 1}`);

      if (fields.length > 0) {
        const stepFlags = await fillPageFields(
          stagehand, page, fields, profile, job, a11y, addLog, resumePath,
        );
        flagged.push(...stepFlags);
      }

      // Detect review/submit step
      const content = await page.evaluate(() => document.documentElement.outerHTML).catch(() => '');
      const isLastStep = /submit|review and submit|confirm and submit/i.test(content) && step > 0;

      if (isLastStep && flagged.length > 0) {
        screenshotPath = await takeScreenshot(page, job.id, 'workday_needs_review');
        addLog('workday_submit', 'Flagged fields require review — not submitting');
        return { status: 'needs_review', log, screenshot_path: screenshotPath };
      }

      const advResult = await advanceStep(stagehand, page, addLog, { isLastStep });
      if (!advResult.success) {
        screenshotPath = await takeScreenshot(page, job.id, `workday_stuck_step_${step}`);
        addLog('workday_advance', 'Could not advance past step', { error: advResult.error, page_url: page.url() });
        return { status: 'needs_review', log, screenshot_path: screenshotPath };
      }

      // Workday pages are slow — generous wait
      await sleep(2500);

      if (await detectSuccess(page)) {
        addLog('workday_success', 'Application submitted');
        return { status: 'submitted', log, screenshot_path: null };
      }
    }

    screenshotPath = await takeScreenshot(page, job.id, 'workday_max_steps');
    addLog('workday_max_steps', `Exceeded ${MAX_STEPS} steps without completion`);
    return { status: 'needs_review', log, screenshot_path: screenshotPath };

  } catch (err) {
    screenshotPath = await takeScreenshot(page, job.id, 'workday_error');
    addLog('workday_error', 'Unhandled error in Workday handler', { error: err.message, page_url: page.url() });
    return { status: 'failed', log, screenshot_path: screenshotPath };
  }
}

// ─── Workday login helper ─────────────────────────────────────────────────────

async function attemptWorkdayLogin(stagehand, page, profile, addLog) {
  const email    = profile.workday_email    || profile.email;
  const password = profile.workday_password || '';

  if (!email || !password) {
    return { success: false, error: 'No Workday credentials in profile (workday_email / workday_password)' };
  }

  try {
    await actWithRetry(stagehand, page, `Fill the email or username field with "${email}"`);
    await actWithRetry(stagehand, page, `Fill the password field with "${password}"`);
    await actWithRetry(stagehand, page, 'Click the Sign In or Log In button');
    await sleep(3000);
    addLog('workday_auth', 'Workday sign-in submitted');
    return { success: true };
  } catch (err) {
    return { success: false, error: err.message };
  }
}
