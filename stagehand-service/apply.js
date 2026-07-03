/**
 * Core Step 3 orchestration.
 *
 * applyToJob(job, profile) → { status, log, screenshot_path }
 *
 * Flow:
 *   1. Detect ATS platform from job URL
 *   2. Initialise Stagehand (Layer 1 — backed by LM Studio via OpenAI-compat API)
 *   3. Navigate to the job URL
 *   4. Capture accessibility tree (Layer 2 — injected into every LLM prompt)
 *   5. Dispatch to named ATS handler (LinkedIn, Greenhouse, Lever, Workday,
 *      iCIMS, Indeed) or the generic fallback
 *   6. On any Stagehand failure, actWithRetry escalates to Skyvern (Layer 3)
 *   7. Return structured result; screenshot on any failure path
 *
 * The resume file path is attached to the job object as job.resume_path
 * before this function is called (set in auto_apply.py from application data).
 */

import 'dotenv/config';
import fs from 'fs';
import { initStagehand } from './lib/stagehand-client.js';
import { detectPlatform, getHandler } from './handlers/index.js';
import { createLogger } from './lib/logger.js';
import { takeScreenshot } from './handlers/shared.js';

/**
 * @param {Object} job     - { id, title, company, url, description, resume_path }
 * @param {Object} profile - Full user profile from config.yaml
 * @param {Object} opts
 * @param {boolean} [opts.headless=true] - Driven by GUI "Headless Mode (hide browser)" toggle
 * @returns {Promise<{status: 'submitted'|'failed'|'needs_review', log: Array, screenshot_path: string|null}>}
 */
export async function applyToJob(job, profile, { headless = true } = {}) {
  const { log, entries } = createLogger();

  // addLog(step, action, { error?, page_url? })
  function addLog(step, action, opts = {}) {
    log(step, action, opts);
  }

  let stagehand = null;
  let screenshotPath = null;

  try {
    // ── 1. Platform detection ─────────────────────────────────────────────────
    const platform = detectPlatform(job.url || '');
    addLog('detect_platform', `Platform detected: ${platform}`, { page_url: job.url });

    // ── 2. Stagehand init ─────────────────────────────────────────────────────
    addLog('init_stagehand', `Initialising Stagehand browser (headless=${headless})`);
    stagehand = await initStagehand({ headless });
    // Stagehand 3.x: active Playwright page via context
    const page = stagehand.context.activePage();

    // ── 3. Load session cookies (platform-specific) ───────────────────────────
    if (job.storage_state_path && fs.existsSync(job.storage_state_path)) {
      try {
        const state = JSON.parse(fs.readFileSync(job.storage_state_path, 'utf8'));
        const cookies = (state.cookies || []).map(c => {
          // Remove partitionKey — Playwright/CDP rejects it
          const { partitionKey, ...clean } = c;
          return clean;
        });
        if (cookies.length > 0) {
          await stagehand.context.addCookies(cookies);
          addLog('session', `Loaded ${cookies.length} session cookies from ${job.storage_state_path}`);
        }
      } catch (err) {
        addLog('session', 'Failed to load session cookies — proceeding without session', { error: err.message });
      }
    }

    // ── 4. Navigate to job URL ────────────────────────────────────────────────
    addLog('navigate', `Navigating to ${job.url}`, { page_url: job.url });
    await page.goto(job.url, { waitUntil: 'domcontentloaded', timeout: 30000 });
    await page.waitForTimeout(1000).catch(() => {});

    // Re-detect after navigation (some aggregator URLs redirect to the real ATS)
    const finalUrl = page.url();
    const finalPlatform = detectPlatform(finalUrl) !== 'generic'
      ? detectPlatform(finalUrl)
      : platform;

    if (finalPlatform !== platform) {
      addLog('detect_platform', `Platform updated after redirect: ${finalPlatform}`, { page_url: finalUrl });
    }

    // ── 5–7. Handler dispatch ─────────────────────────────────────────────────
    const handler = getHandler(finalPlatform);
    addLog('handler', `Dispatching to ${finalPlatform} handler`);

    const result = await handler.apply({
      stagehand,
      page,
      job,
      profile,
      log: entries,
      addLog,
    });

    return result;

  } catch (err) {
    const page = stagehand?.context?.activePage();
    if (page) {
      screenshotPath = await takeScreenshot(page, job.id || 'unknown', 'fatal_error').catch(() => null);
    }
    addLog('fatal_error', 'Unhandled error in apply orchestration', { error: err.message });
    return { status: 'failed', log: entries, screenshot_path: screenshotPath };

  } finally {
    if (stagehand) {
      await stagehand.close().catch(() => {});
    }
  }
}
