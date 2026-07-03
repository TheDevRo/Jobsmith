/**
 * Skyvern REST client — Layer 3 visual fallback.
 *
 * Only called after Stagehand has exhausted its STAGEHAND_MAX_RETRIES retries
 * on a single step. Passes the current page URL + intended action to Skyvern
 * as a natural language task, polls until Skyvern finishes, then returns.
 *
 * Skyvern advances the browser state in its own container; after it completes
 * we reload the Stagehand page to reflect whatever Skyvern did.
 */

const BASE_URL = () => process.env.SKYVERN_BASE_URL || 'http://localhost:8000';
const API_KEY  = () => process.env.SKYVERN_API_KEY  || 'skyvern-key';
const TIMEOUT  = () => parseInt(process.env.SKYVERN_TIMEOUT_SECONDS || '300', 10) * 1000;
const POLL_MS  = 3000;

async function skyvernFetch(path, options = {}) {
  const url = `${BASE_URL()}${path}`;
  const res = await fetch(url, {
    ...options,
    headers: {
      'Content-Type': 'application/json',
      'x-api-key': API_KEY(),
      ...(options.headers || {}),
    },
  });
  if (!res.ok) {
    const body = await res.text().catch(() => '');
    throw new Error(`Skyvern ${options.method || 'GET'} ${path} → ${res.status}: ${body}`);
  }
  return res.json();
}

/**
 * Ask Skyvern to perform one action on the current page.
 *
 * @param {string} pageUrl   - URL Skyvern should navigate to
 * @param {string} action    - Natural-language description of what to do
 * @returns {Promise<{success: boolean, error?: string}>}
 */
export async function skyvernAct(pageUrl, action) {
  let taskId;
  try {
    const task = await skyvernFetch('/api/v1/agent/tasks', {
      method: 'POST',
      body: JSON.stringify({
        url: pageUrl,
        navigation_goal: action,
        // Skyvern will use whatever LLM is configured in its own env
      }),
    });
    taskId = task.task_id;
  } catch (err) {
    return { success: false, error: `Skyvern task creation failed: ${err.message}` };
  }

  // Poll for completion
  const deadline = Date.now() + TIMEOUT();
  while (Date.now() < deadline) {
    await sleep(POLL_MS);
    let status;
    try {
      const result = await skyvernFetch(`/api/v1/agent/tasks/${taskId}`);
      status = result.status;
      if (status === 'completed') return { success: true };
      if (status === 'failed' || status === 'terminated') {
        return { success: false, error: `Skyvern task ${status}: ${result.failure_reason || ''}` };
      }
    } catch (err) {
      return { success: false, error: `Skyvern poll error: ${err.message}` };
    }
  }

  return { success: false, error: 'Skyvern task timed out' };
}

function sleep(ms) {
  return new Promise(resolve => setTimeout(resolve, ms));
}
