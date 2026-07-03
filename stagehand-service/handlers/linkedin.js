/**
 * LinkedIn Easy Apply handler.
 *
 * Flow:
 *  1. Detect the "Easy Apply" button on the job posting page
 *  2. Click it to open the Easy Apply modal
 *  3. For each modal step: extract fields → fill → advance
 *  4. On the final step, submit and confirm
 *
 * LinkedIn's Easy Apply modal is a multi-step dialog — we loop over steps
 * until we detect the success state or hit the step ceiling.
 */

import { actWithRetry } from '../lib/stagehand-client.js';
import { getAccessibilityContext } from '../lib/accessibility.js';
import {
  extractFields,
  fillPageFields,
  detectSuccess,
  detectAuthWall,
  detectStepProgressed,
  takeScreenshot,
  advanceStep,
  sleep,
} from './shared.js';

export function detect(url) {
  return url.includes('linkedin.com');
}

export async function apply({ stagehand, page, job, profile, log, addLog }) {
  const flagged = [];
  let screenshotPath = null;
  const resumePath = job.resume_path || null;
  const MAX_STEPS = 20;

  try {
    // ── 1. Find and click Easy Apply button ──────────────────────────────────
    // LinkedIn is a React SPA — wait for network activity to settle so all
    // dynamic components (including the Easy Apply button) are rendered.
    addLog('linkedin_init', 'Waiting for page to settle before clicking Easy Apply');
    await page.waitForLoadState('networkidle', { timeout: 15000 }).catch(() => {});

    // LinkedIn is a React SPA — networkidle resolves before deferred components render.
    // Scroll to top and wait specifically for the Easy Apply button element.
    await page.evaluate(() => window.scrollTo({ top: 0, behavior: 'instant' })).catch(() => {});
    await page.waitForSelector(
      'button[aria-label*="Easy Apply" i], button:has-text("Easy Apply"), [class*="jobs-apply-button"]',
      { timeout: 8000, state: 'visible' },
    ).catch(() => {});
    await sleep(800); // extra paint settle for React

    addLog('linkedin_init', 'Looking for Easy Apply button');
    const clickResult = await actWithRetry(
      stagehand, page,
      'Click the "Easy Apply" button to open the application modal',
    );

    if (!clickResult.success) {
      // Capture what the page actually looks like so we can diagnose auth/layout issues
      screenshotPath = await takeScreenshot(page, job.id, 'linkedin_no_easy_apply');
      addLog('linkedin_init', 'Could not find Easy Apply button', { error: clickResult.error, page_url: page.url() });
      return { status: 'failed', log, screenshot_path: screenshotPath };
    }

    // Wait for modal to open
    await sleep(1500);

    // ── 2. Step loop ─────────────────────────────────────────────────────────
    for (let step = 0; step < MAX_STEPS; step++) {
      addLog('linkedin_step', `Processing modal step ${step + 1}`, { page_url: page.url() });

      // Check for success before doing anything
      if (await detectSuccess(page)) {
        addLog('linkedin_success', 'Application submitted successfully');
        return { status: 'submitted', log, screenshot_path: null };
      }

      // Capture accessibility context for LLM
      const a11y = await getAccessibilityContext(page);

      // Extract all fields on this step
      const fields = await extractFields(stagehand);
      addLog('linkedin_step', `Found ${fields.length} fields on step ${step + 1}`);

      // Fill fields; collect any low-confidence flags
      const stepFlags = await fillPageFields(
        stagehand, page, fields, profile, job, a11y, addLog, resumePath,
      );
      flagged.push(...stepFlags);

      // Detect if this looks like the final submission step
      const content = await page.evaluate(() => document.documentElement.outerHTML).catch(() => '');
      const isLastStep = /submit application|review your application/i.test(content);

      // Final required-field verification before submitting
      if (isLastStep && flagged.length > 0) {
        screenshotPath = await takeScreenshot(page, job.id, 'linkedin_needs_review');
        addLog('linkedin_submit', 'Flagged fields require review — not submitting', {
          page_url: page.url(),
        });
        return { status: 'needs_review', log, screenshot_path: screenshotPath };
      }

      // Advance to next step or submit
      const prevUrl = page.url();
      const prevFieldCount = fields.length;

      const advResult = await advanceStep(stagehand, page, addLog, { isLastStep });
      if (!advResult.success) {
        screenshotPath = await takeScreenshot(page, job.id, `linkedin_stuck_step_${step}`);
        addLog('linkedin_advance', 'Could not advance past this step', {
          error: advResult.error, page_url: page.url(),
        });
        return { status: 'needs_review', log, screenshot_path: screenshotPath };
      }

      await sleep(1000);

      // Detect validation errors causing silent stalls
      const progressed = await detectStepProgressed(page, prevUrl, prevFieldCount);
      if (!progressed) {
        await page.evaluate(() => {
          const err = document.querySelector('[aria-invalid="true"], [role="alert"], .error, .invalid');
          if (err) err.scrollIntoView({ behavior: 'instant' });
        }).catch(() => {});
        screenshotPath = await takeScreenshot(page, job.id, `linkedin_validation_stuck_step_${step}`);
        addLog('linkedin_advance', 'LinkedIn modal did not advance — likely a required field validation error', {
          page_url: page.url(),
        });
        return { status: 'needs_review', log, screenshot_path: screenshotPath };
      }

      // Re-check for success after advancing
      if (await detectSuccess(page)) {
        addLog('linkedin_success', 'Application submitted successfully');
        return { status: 'submitted', log, screenshot_path: null };
      }
    }

    // Exceeded step ceiling without success
    screenshotPath = await takeScreenshot(page, job.id, 'linkedin_max_steps');
    addLog('linkedin_max_steps', `Exceeded ${MAX_STEPS} steps without completion`);
    return { status: 'needs_review', log, screenshot_path: screenshotPath };

  } catch (err) {
    screenshotPath = await takeScreenshot(page, job.id, 'linkedin_error');
    addLog('linkedin_error', 'Unhandled error in LinkedIn handler', { error: err.message, page_url: page.url() });
    return { status: 'failed', log, screenshot_path: screenshotPath };
  }
}
