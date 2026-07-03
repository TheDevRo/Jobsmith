/**
 * Indeed Apply handler.
 *
 * Indeed Apply embeds a multi-step application flow either inline or via a
 * redirect to a dedicated apply page. We detect the apply button, follow the
 * flow, and fill each step.
 */

import { actWithRetry } from '../lib/stagehand-client.js';
import { getAccessibilityContext } from '../lib/accessibility.js';
import {
  extractFields,
  fillPageFields,
  detectSuccess,
  detectDeadEnd,
  takeScreenshot,
  advanceStep,
  sleep,
} from './shared.js';

export function detect(url) {
  return url.includes('indeed.com') || url.includes('indeed.co.');
}

export async function apply({ stagehand, page, job, profile, log, addLog }) {
  const flagged = [];
  let screenshotPath = null;
  const resumePath = job.resume_path || null;
  const MAX_STEPS = 20;

  try {
    // ── Find Apply button ─────────────────────────────────────────────────────
    addLog('indeed_init', 'Looking for Apply button', { page_url: page.url() });
    const clickResult = await actWithRetry(
      stagehand, page,
      'Click the "Apply now" or "Apply on company site" button',
    );

    if (!clickResult.success) {
      screenshotPath = await takeScreenshot(page, job.id, 'indeed_no_apply_btn');
      addLog('indeed_init', 'Could not find Apply button', { error: clickResult.error });
      return { status: 'failed', log, screenshot_path: screenshotPath };
    }

    await sleep(2000);

    // ── Step loop ─────────────────────────────────────────────────────────────
    for (let step = 0; step < MAX_STEPS; step++) {
      addLog('indeed_step', `Processing step ${step + 1}`, { page_url: page.url() });

      if (await detectSuccess(page)) {
        addLog('indeed_success', 'Application submitted');
        return { status: 'submitted', log, screenshot_path: null };
      }

      if (await detectDeadEnd(page)) {
        addLog('indeed_dead_end', 'Job posting appears expired or unavailable');
        return { status: 'failed', log, screenshot_path: null };
      }

      const a11y = await getAccessibilityContext(page);
      const fields = await extractFields(stagehand);
      addLog('indeed_step', `Found ${fields.length} fields on step ${step + 1}`);

      const stepFlags = await fillPageFields(
        stagehand, page, fields, profile, job, a11y, addLog, resumePath,
      );
      flagged.push(...stepFlags);

      const content = await page.evaluate(() => document.documentElement.outerHTML).catch(() => '');
      const isLastStep = /submit|review your application|apply now/i.test(content) && step > 0;

      if (isLastStep && flagged.length > 0) {
        screenshotPath = await takeScreenshot(page, job.id, 'indeed_needs_review');
        addLog('indeed_submit', 'Flagged fields require review — not submitting');
        return { status: 'needs_review', log, screenshot_path: screenshotPath };
      }

      const advResult = await advanceStep(stagehand, page, addLog, { isLastStep });
      if (!advResult.success) {
        screenshotPath = await takeScreenshot(page, job.id, `indeed_stuck_step_${step}`);
        addLog('indeed_advance', 'Could not advance', { error: advResult.error, page_url: page.url() });
        return { status: 'needs_review', log, screenshot_path: screenshotPath };
      }

      await sleep(1200);

      if (await detectSuccess(page)) {
        addLog('indeed_success', 'Application submitted');
        return { status: 'submitted', log, screenshot_path: null };
      }
    }

    screenshotPath = await takeScreenshot(page, job.id, 'indeed_max_steps');
    addLog('indeed_max_steps', `Exceeded ${MAX_STEPS} steps without completion`);
    return { status: 'needs_review', log, screenshot_path: screenshotPath };

  } catch (err) {
    screenshotPath = await takeScreenshot(page, job.id, 'indeed_error');
    addLog('indeed_error', 'Unhandled error in Indeed handler', { error: err.message, page_url: page.url() });
    return { status: 'failed', log, screenshot_path: screenshotPath };
  }
}
