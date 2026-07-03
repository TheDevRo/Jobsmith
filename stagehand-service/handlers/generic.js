/**
 * Generic ATS fallback handler.
 *
 * Used for any platform not matched by the named handlers: Ashby,
 * SmartRecruiters, JobVite, Rippling, and any custom ATS.
 *
 * The flow is the same step loop used by named handlers, but we first try to
 * find an "Apply" button in case the URL is a job listing page rather than
 * the application form itself.
 */

import { actWithRetry } from '../lib/stagehand-client.js';
import { getAccessibilityContext } from '../lib/accessibility.js';
import {
  extractFields,
  fillPageFields,
  detectSuccess,
  detectDeadEnd,
  detectAuthWall,
  detectStepProgressed,
  takeScreenshot,
  advanceStep,
  sleep,
} from './shared.js';

export function detect() {
  return false; // This handler is always the last resort — never matched by URL
}

export async function apply({ stagehand, page, job, profile, log, addLog }) {
  const flagged = [];
  let screenshotPath = null;
  const resumePath = job.resume_path || null;
  const MAX_STEPS = 8;

  try {
    addLog('generic_init', 'Starting generic application', { page_url: page.url() });

    if (await detectDeadEnd(page)) {
      addLog('generic_dead_end', 'Job posting appears expired or unavailable');
      return { status: 'failed', log, screenshot_path: null };
    }

    // ── Look for an Apply button if we're on a listing page, not a form ───────
    const content = await page.evaluate(() => document.documentElement.outerHTML).catch(() => '');
    const looksLikeListing = !/<form/i.test(content) || /apply now|apply online|apply here/i.test(content);

    if (looksLikeListing) {
      addLog('generic_init', 'Looks like job listing — looking for Apply button');
      const clickResult = await actWithRetry(
        stagehand, page,
        'Click the Apply, Apply Now, or Apply Online button',
        { useSkyvernFallback: false }, // Don't burn Skyvern on button finding
      );
      if (clickResult.success) {
        await sleep(2000);
        addLog('generic_init', 'Clicked Apply button — now on application form');
      } else {
        addLog('generic_init', 'No Apply button found — treating current page as the form');
      }
    }

    // ── Step loop ─────────────────────────────────────────────────────────────
    for (let step = 0; step < MAX_STEPS; step++) {
      addLog('generic_step', `Processing step ${step + 1}`, { page_url: page.url() });

      if (await detectSuccess(page)) {
        addLog('generic_success', 'Application submitted');
        return { status: 'submitted', log, screenshot_path: null };
      }

      if (await detectDeadEnd(page)) {
        addLog('generic_dead_end', 'Navigated to expired or unavailable page');
        return { status: 'failed', log, screenshot_path: null };
      }

      // Check for auth/account-creation gate before attempting to fill fields
      const authWall = await detectAuthWall(page);
      if (authWall.detected) {
        screenshotPath = await takeScreenshot(page, job.id, 'generic_auth_wall');
        const reason = authWall.isSSOOnly ? 'auth_sso_wall' : 'auth_no_credentials';
        const msg = authWall.isSSOOnly
          ? 'Site requires SSO login (Google/Microsoft) — open manually to apply'
          : 'Site requires account login — no credentials configured';
        addLog('generic_auth_wall', msg, { page_url: page.url() });
        return {
          status: 'needs_review',
          log,
          screenshot_path: screenshotPath,
          manual_url: page.url(),
          block_reason: reason,
        };
      }

      const a11y = await getAccessibilityContext(page);
      const fields = await extractFields(stagehand);
      addLog('generic_step', `Found ${fields.length} fields on step ${step + 1}`);

      if (fields.length === 0 && step === 0) {
        // No fields found on first step — likely a listing or redirect page
        addLog('generic_step', 'No fields found on step 1 — attempting to navigate to application');
        const navResult = await actWithRetry(
          stagehand, page,
          'Find and click any button or link that leads to the job application form',
        );
        if (!navResult.success) {
          screenshotPath = await takeScreenshot(page, job.id, 'generic_no_form');
          addLog('generic_no_form', 'Could not find application form', { error: navResult.error });
          return { status: 'failed', log, screenshot_path: screenshotPath };
        }
        await sleep(2000);
        continue;
      }

      if (fields.length > 0) {
        const stepFlags = await fillPageFields(
          stagehand, page, fields, profile, job, a11y, addLog, resumePath,
        );
        flagged.push(...stepFlags);
      }

      const stepContent = await page.evaluate(() => document.documentElement.outerHTML).catch(() => '');
      const isLastStep = /submit|finish|complete|send application/i.test(stepContent) && step > 0;

      if (isLastStep && flagged.length > 0) {
        screenshotPath = await takeScreenshot(page, job.id, 'generic_needs_review');
        addLog('generic_submit', 'Low-confidence fields require human review — not submitting');
        return { status: 'needs_review', log, screenshot_path: screenshotPath };
      }

      const prevUrl = page.url();
      const prevFieldCount = fields.length;

      const advResult = await advanceStep(stagehand, page, addLog, { isLastStep });
      if (!advResult.success) {
        screenshotPath = await takeScreenshot(page, job.id, `generic_stuck_step_${step}`);
        addLog('generic_advance', 'Could not advance', { error: advResult.error, page_url: page.url() });
        return { status: 'needs_review', log, screenshot_path: screenshotPath };
      }

      await sleep(1500);

      // Detect if the page actually moved forward — inline validation errors stall silently
      const progressed = await detectStepProgressed(page, prevUrl, prevFieldCount);
      if (!progressed) {
        // Scroll to make any validation error visible for the screenshot
        await page.evaluate(() => {
          const err = document.querySelector('[aria-invalid="true"], [role="alert"], .error, .invalid');
          if (err) err.scrollIntoView({ behavior: 'instant' });
        }).catch(() => {});
        screenshotPath = await takeScreenshot(page, job.id, `generic_validation_stuck_step_${step}`);
        addLog('generic_advance', 'Page did not advance — likely a validation error on a required field', {
          page_url: page.url(),
        });
        return { status: 'needs_review', log, screenshot_path: screenshotPath };
      }

      if (await detectSuccess(page)) {
        addLog('generic_success', 'Application submitted');
        return { status: 'submitted', log, screenshot_path: null };
      }
    }

    screenshotPath = await takeScreenshot(page, job.id, 'generic_max_steps');
    addLog('generic_max_steps', `Exceeded ${MAX_STEPS} steps without completion`);
    return { status: 'needs_review', log, screenshot_path: screenshotPath };

  } catch (err) {
    screenshotPath = await takeScreenshot(page, job.id, 'generic_error');
    addLog('generic_error', 'Unhandled error in generic handler', { error: err.message, page_url: page.url() });
    return { status: 'failed', log, screenshot_path: screenshotPath };
  }
}
