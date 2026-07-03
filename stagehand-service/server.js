/**
 * Stagehand microservice — Express HTTP server.
 *
 * Endpoints:
 *   POST /apply        — Run Step 3 auto-apply; returns {status, log, screenshot_path}
 *   GET  /health       — Liveness check
 *
 * The Python backend calls POST /apply with the job and profile JSON.
 * One request runs at a time — no concurrency control needed since the Python
 * side only ever has one application running at once.
 */

import 'dotenv/config';
import express from 'express';
import { applyToJob } from './apply.js';

const PORT    = parseInt(process.env.PORT || '3741', 10);
const TIMEOUT = parseInt(process.env.APPLY_TIMEOUT_MS || '300000', 10); // 5 min default

const app = express();
app.use(express.json({ limit: '5mb' }));

// ─── Health check ─────────────────────────────────────────────────────────────

app.get('/health', (_req, res) => {
  res.json({ ok: true, service: 'stagehand-service' });
});

// ─── Apply endpoint ───────────────────────────────────────────────────────────

app.post('/apply', async (req, res) => {
  const { job, profile } = req.body;

  if (!job || !profile) {
    return res.status(400).json({
      status: 'failed',
      log: [{ step: 'validation', action_attempted: 'validate request body',
              error: 'Missing required fields: job, profile', page_url: null,
              timestamp: new Date().toISOString() }],
      screenshot_path: null,
    });
  }

  if (!job.url) {
    return res.status(400).json({
      status: 'failed',
      log: [{ step: 'validation', action_attempted: 'validate job url',
              error: 'job.url is required', page_url: null,
              timestamp: new Date().toISOString() }],
      screenshot_path: null,
    });
  }

  // headless: true = hide browser (default), false = show browser window
  // Driven by the "Headless Mode (hide browser)" toggle in the GUI via config.auto_apply.headless
  const headless = req.body.headless !== false;

  // Set a per-request timeout
  const timer = setTimeout(() => {
    if (!res.headersSent) {
      res.status(504).json({
        status: 'failed',
        log: [{ step: 'timeout', action_attempted: 'apply to job',
                error: `Request timed out after ${TIMEOUT}ms`, page_url: job.url,
                timestamp: new Date().toISOString() }],
        screenshot_path: null,
      });
    }
  }, TIMEOUT);

  try {
    console.log(`[apply] Starting: "${job.title}" at "${job.company}" — ${job.url} (headless=${headless})`);
    const result = await applyToJob(job, profile, { headless });
    console.log(`[apply] Done: status=${result.status}, log_entries=${result.log?.length ?? 0}`);
    if (!res.headersSent) res.json(result);
  } catch (err) {
    console.error('[apply] Unexpected error:', err);
    if (!res.headersSent) {
      res.status(500).json({
        status: 'failed',
        log: [{ step: 'server_error', action_attempted: 'apply to job',
                error: err.message, page_url: job.url,
                timestamp: new Date().toISOString() }],
        screenshot_path: null,
      });
    }
  } finally {
    clearTimeout(timer);
  }
});

// ─── Start ────────────────────────────────────────────────────────────────────

app.listen(PORT, '127.0.0.1', () => {
  console.log(`[stagehand-service] Listening on http://127.0.0.1:${PORT}`);
  console.log(`[stagehand-service] LM Studio: ${process.env.LM_STUDIO_BASE_URL}`);
  console.log(`[stagehand-service] Skyvern:   ${process.env.SKYVERN_BASE_URL}`);
});
