/**
 * LM Studio client — OpenAI-compatible local inference.
 *
 * Used for:
 *   1. Mapping form fields → profile values
 *   2. Generating answers to free-text questions not in the Q&A cache
 *
 * All config comes from env vars set in .env (or the parent process env).
 */

import OpenAI from 'openai';

// Build client once; reuse across calls.
let _client = null;

function getClient() {
  if (_client) return _client;
  _client = new OpenAI({
    apiKey: process.env.LM_STUDIO_API_KEY || 'lm-studio',
    baseURL: process.env.LM_STUDIO_BASE_URL || 'http://localhost:1234/v1',
  });
  return _client;
}

const MODEL = () => process.env.LM_STUDIO_MODEL || 'local-model';
const TEMP = () => parseFloat(process.env.LM_STUDIO_TEMPERATURE || '0.2');
const MAX_TOKENS = () => parseInt(process.env.LM_STUDIO_MAX_TOKENS || '1024', 10);

/**
 * Call LM Studio and parse the response as JSON.
 * Strips <think>…</think> blocks that some reasoning models emit.
 *
 * @param {string} systemPrompt
 * @param {string} userPrompt
 * @returns {Promise<any>} parsed JSON object
 */
export async function callJson(systemPrompt, userPrompt) {
  const client = getClient();
  const response = await client.chat.completions.create({
    model: MODEL(),
    messages: [
      { role: 'system', content: systemPrompt },
      { role: 'user', content: userPrompt },
    ],
    temperature: TEMP(),
    max_tokens: MAX_TOKENS(),
  });

  let text = response.choices[0]?.message?.content || '';

  // Strip thinking blocks (Qwen 3.x, Phi-reasoning, etc.)
  text = text.replace(/<think>[\s\S]*?<\/think>/gi, '').trim();

  // Extract JSON from markdown code fences if present
  const fenceMatch = text.match(/```(?:json)?\s*([\s\S]*?)```/);
  if (fenceMatch) text = fenceMatch[1].trim();

  return JSON.parse(text);
}

/**
 * Map visible form fields to values from the user profile.
 *
 * @param {Array<{label:string, type:string, selector:string, options?:string[]}>} fields
 * @param {Object} profile  - Full user profile from config.yaml
 * @param {string} a11yContext - Accessibility tree snapshot (for context)
 * @returns {Promise<Object>} { selector_or_label -> value }
 */
export async function mapFieldsToProfile(fields, profile, a11yContext = '') {
  const systemPrompt = `You are a job application form-fill assistant.
Given a list of form fields and a user profile, return a JSON object mapping each field's label
to the exact value from the profile that should be entered.
Only use values that exist in the profile — never invent data.
For fields with no clear match (e.g. a unique essay question), return null for that label.
For file upload fields, return the string "__RESUME__" as a sentinel.
For EEO/demographic fields, use the exact profile values.
Return ONLY valid JSON, no explanation.`;

  const userPrompt = `FORM FIELDS:
${JSON.stringify(fields, null, 2)}

ACCESSIBILITY TREE CONTEXT:
${a11yContext || '(none)'}

USER PROFILE:
${JSON.stringify(profile, null, 2)}

Return: { "field_label": "value_to_fill_or_null", ... }`;

  return callJson(systemPrompt, userPrompt);
}

/**
 * Generate an answer for a free-text question using the user profile.
 *
 * @param {string} question
 * @param {Object} profile
 * @param {Object} job - { title, company, description }
 * @param {string} a11yContext
 * @returns {Promise<string>} The answer text
 */
export async function generateAnswer(question, profile, job, a11yContext = '') {
  const systemPrompt = `You are helping a job applicant answer application questions.
Answer the question below using ONLY information from the provided user profile.
Do NOT fabricate any facts, credentials, dates, or experiences not present in the profile.
Write in first person as the applicant. Be specific and professional.
Return only the answer text — no labels, no JSON, no preamble.`;

  const userPrompt = `JOB: ${job.title || 'Unknown'} at ${job.company || 'Unknown'}
JOB DESCRIPTION EXCERPT: ${(job.description || '').slice(0, 500)}

QUESTION: ${question}

USER PROFILE:
${JSON.stringify(profile, null, 2)}

ACCESSIBILITY CONTEXT:
${a11yContext || '(none)'}

Answer:`;

  const client = getClient();
  const response = await client.chat.completions.create({
    model: MODEL(),
    messages: [
      { role: 'system', content: systemPrompt },
      { role: 'user', content: userPrompt },
    ],
    temperature: TEMP(),
    max_tokens: MAX_TOKENS(),
  });

  let text = response.choices[0]?.message?.content || '';
  text = text.replace(/<think>[\s\S]*?<\/think>/gi, '').trim();
  return text;
}
