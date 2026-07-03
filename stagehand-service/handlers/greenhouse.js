/**
 * Greenhouse handler.
 *
 * Greenhouse application pages are typically single-page or lightly paginated.
 * Fields: first/last name, email, phone, resume upload, LinkedIn, portfolio,
 * and a variable set of custom questions at the bottom.
 */

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
  return url.includes('greenhouse.io') || url.includes('boards.greenhouse');
}

export async function apply({ stagehand, page, job, profile, log, addLog }) {
  const flagged = [];
  let screenshotPath = null;
  const resumePath = job.resume_path || null;
  const MAX_STEPS = 10;

  try {
    addLog('greenhouse_init', 'Starting Greenhouse application', { page_url: page.url() });

    if (await detectDeadEnd(page)) {
      addLog('greenhouse_dead_end', 'Job posting appears expired');
      return { status: 'failed', log, screenshot_path: null };
    }

    for (let step = 0; step < MAX_STEPS; step++) {
      addLog('greenhouse_step', `Processing step ${step + 1}`, { page_url: page.url() });

      if (await detectSuccess(page)) {
        addLog('greenhouse_success', 'Application submitted');
        return { status: 'submitted', log, screenshot_path: null };
      }

      const a11y = await getAccessibilityContext(page);
      const fields = await extractFields(stagehand);
      addLog('greenhouse_step', `Found ${fields.length} fields on step ${step + 1}`);

      const stepFlags = await fillPageFields(
        stagehand, page, fields, profile, job, a11y, addLog, resumePath,
      );
      flagged.push(...stepFlags);

      const content = await page.evaluate(() => document.documentElement.outerHTML).catch(() => '');
      const isLastStep = /submit application|i accept|agree and submit/i.test(content);

      if (isLastStep && flagged.length > 0) {
        screenshotPath = await takeScreenshot(page, job.id, 'greenhouse_needs_review');
        addLog('greenhouse_submit', 'Low-confidence fields require human review — not submitting');
        return { status: 'needs_review', log, screenshot_path: screenshotPath };
      }

      const advResult = await advanceStep(stagehand, page, addLog, { isLastStep });
      if (!advResult.success) {
        // Greenhouse is often single-step — if we can't advance and it's not
        // a multi-step form, we may already be on the only page
        if (step === 0) {
          // Try clicking submit directly
          const submitResult = await advanceStep(stagehand, page, addLog, { isLastStep: true });
          if (!submitResult.success) {
            screenshotPath = await takeScreenshot(page, job.id, 'greenhouse_stuck');
            addLog('greenhouse_submit', 'Could not submit', { error: submitResult.error, page_url: page.url() });
            return { status: 'needs_review', log, screenshot_path: screenshotPath };
          }
          await sleep(2000);
          if (await detectSuccess(page)) {
            addLog('greenhouse_success', 'Application submitted');
            return { status: 'submitted', log, screenshot_path: null };
          }
        }
        screenshotPath = await takeScreenshot(page, job.id, `greenhouse_stuck_step_${step}`);
        addLog('greenhouse_advance', 'Could not advance past step', { error: advResult.error, page_url: page.url() });
        return { status: 'needs_review', log, screenshot_path: screenshotPath };
      }

      await sleep(1500);

      if (await detectSuccess(page)) {
        addLog('greenhouse_success', 'Application submitted');
        return { status: 'submitted', log, screenshot_path: null };
      }
    }

    screenshotPath = await takeScreenshot(page, job.id, 'greenhouse_max_steps');
    addLog('greenhouse_max_steps', `Exceeded ${MAX_STEPS} steps without completion`);
    return { status: 'needs_review', log, screenshot_path: screenshotPath };

  } catch (err) {
    screenshotPath = await takeScreenshot(page, job.id, 'greenhouse_error');
    addLog('greenhouse_error', 'Unhandled error in Greenhouse handler', { error: err.message, page_url: page.url() });
    return { status: 'failed', log, screenshot_path: screenshotPath };
  }
}
