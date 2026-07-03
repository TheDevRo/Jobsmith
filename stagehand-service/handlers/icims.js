/**
 * iCIMS / Taleo / BambooHR handler.
 *
 * These platforms share similar patterns: they're all hosted ATS portals with
 * multi-step application forms. We use the same generic step loop that works
 * for all three, with URL-based detection to route to this handler.
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
  return (
    url.includes('icims.com') ||
    url.includes('taleo.net') ||
    url.includes('bamboohr.com') ||
    url.includes('taleocg.com') ||
    url.includes('oraclecloud.com') // Oracle Recruiting (successor to Taleo)
  );
}

export async function apply({ stagehand, page, job, profile, log, addLog }) {
  const flagged = [];
  let screenshotPath = null;
  const resumePath = job.resume_path || null;
  const MAX_STEPS = 8;

  try {
    addLog('icims_init', 'Starting iCIMS/Taleo/BambooHR application', { page_url: page.url() });

    if (await detectDeadEnd(page)) {
      addLog('icims_dead_end', 'Job posting appears expired or unavailable');
      return { status: 'failed', log, screenshot_path: null };
    }

    for (let step = 0; step < MAX_STEPS; step++) {
      addLog('icims_step', `Processing step ${step + 1}`, { page_url: page.url() });

      if (await detectSuccess(page)) {
        addLog('icims_success', 'Application submitted');
        return { status: 'submitted', log, screenshot_path: null };
      }

      const a11y = await getAccessibilityContext(page);
      const fields = await extractFields(stagehand);
      addLog('icims_step', `Found ${fields.length} fields on step ${step + 1}`);

      if (fields.length > 0) {
        const stepFlags = await fillPageFields(
          stagehand, page, fields, profile, job, a11y, addLog, resumePath,
        );
        flagged.push(...stepFlags);
      }

      const content = await page.evaluate(() => document.documentElement.outerHTML).catch(() => '');
      const isLastStep = /submit|finish|complete application/i.test(content) && step > 0;

      if (isLastStep && flagged.length > 0) {
        screenshotPath = await takeScreenshot(page, job.id, 'icims_needs_review');
        addLog('icims_submit', 'Low-confidence fields require human review — not submitting');
        return { status: 'needs_review', log, screenshot_path: screenshotPath };
      }

      const advResult = await advanceStep(stagehand, page, addLog, { isLastStep });
      if (!advResult.success) {
        screenshotPath = await takeScreenshot(page, job.id, `icims_stuck_step_${step}`);
        addLog('icims_advance', 'Could not advance', { error: advResult.error, page_url: page.url() });
        return { status: 'needs_review', log, screenshot_path: screenshotPath };
      }

      await sleep(1500);

      if (await detectSuccess(page)) {
        addLog('icims_success', 'Application submitted');
        return { status: 'submitted', log, screenshot_path: null };
      }
    }

    screenshotPath = await takeScreenshot(page, job.id, 'icims_max_steps');
    addLog('icims_max_steps', `Exceeded ${MAX_STEPS} steps without completion`);
    return { status: 'needs_review', log, screenshot_path: screenshotPath };

  } catch (err) {
    screenshotPath = await takeScreenshot(page, job.id, 'icims_error');
    addLog('icims_error', 'Unhandled error in iCIMS/Taleo/BambooHR handler', { error: err.message, page_url: page.url() });
    return { status: 'failed', log, screenshot_path: screenshotPath };
  }
}
