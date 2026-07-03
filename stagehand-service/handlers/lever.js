/**
 * Lever handler.
 *
 * Lever application pages (lever.co / jobs.lever.co) are typically
 * single-page with contact fields and custom questions at the bottom.
 * No pagination — one page, one submit.
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
  return url.includes('lever.co') || url.includes('jobs.lever');
}

export async function apply({ stagehand, page, job, profile, log, addLog }) {
  const flagged = [];
  let screenshotPath = null;
  const resumePath = job.resume_path || null;

  try {
    addLog('lever_init', 'Starting Lever application', { page_url: page.url() });

    if (await detectDeadEnd(page)) {
      addLog('lever_dead_end', 'Job posting appears expired');
      return { status: 'failed', log, screenshot_path: null };
    }

    const a11y = await getAccessibilityContext(page);
    const fields = await extractFields(stagehand);
    addLog('lever_fill', `Found ${fields.length} fields`);

    const stepFlags = await fillPageFields(
      stagehand, page, fields, profile, job, a11y, addLog, resumePath,
    );
    flagged.push(...stepFlags);

    if (flagged.length > 0) {
      screenshotPath = await takeScreenshot(page, job.id, 'lever_needs_review');
      addLog('lever_submit', 'Low-confidence fields require human review — not submitting');
      return { status: 'needs_review', log, screenshot_path: screenshotPath };
    }

    const advResult = await advanceStep(stagehand, page, addLog, { isLastStep: true });
    if (!advResult.success) {
      screenshotPath = await takeScreenshot(page, job.id, 'lever_submit_failed');
      addLog('lever_submit', 'Could not click submit', { error: advResult.error, page_url: page.url() });
      return { status: 'needs_review', log, screenshot_path: screenshotPath };
    }

    await sleep(2500);

    if (await detectSuccess(page)) {
      addLog('lever_success', 'Application submitted');
      return { status: 'submitted', log, screenshot_path: null };
    }

    // Lever may redirect to a confirmation page — check the URL too
    const finalUrl = page.url();
    if (finalUrl.includes('confirmation') || finalUrl.includes('thank')) {
      addLog('lever_success', 'Application submitted (redirect detected)');
      return { status: 'submitted', log, screenshot_path: null };
    }

    screenshotPath = await takeScreenshot(page, job.id, 'lever_unconfirmed');
    addLog('lever_submit', 'Submit clicked but success not confirmed', { page_url: finalUrl });
    return { status: 'needs_review', log, screenshot_path: screenshotPath };

  } catch (err) {
    screenshotPath = await takeScreenshot(page, job.id, 'lever_error');
    addLog('lever_error', 'Unhandled error in Lever handler', { error: err.message, page_url: page.url() });
    return { status: 'failed', log, screenshot_path: screenshotPath };
  }
}
