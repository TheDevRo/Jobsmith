/**
 * Structured action logger for Step 3.
 *
 * Every entry shape:
 *   { step, action_attempted, error, page_url, timestamp }
 *
 * Returns a logger instance bound to a shared log array so handlers can push
 * entries and the orchestrator can read them all at the end.
 */

export function createLogger() {
  const entries = [];

  function log(step, action_attempted, { error = null, page_url = null } = {}) {
    const entry = {
      step,
      action_attempted,
      error: error ?? null,
      page_url: page_url ?? null,
      timestamp: new Date().toISOString(),
    };
    entries.push(entry);
    const prefix = error ? '✗' : '·';
    console.log(`[step3] ${prefix} [${step}] ${action_attempted}${error ? ` — ERROR: ${error}` : ''}`);
    return entry;
  }

  return { log, entries };
}
