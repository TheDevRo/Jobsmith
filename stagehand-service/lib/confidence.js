/**
 * Infer whether an LLM-generated answer is low-confidence.
 *
 * Does NOT rely on a model-returned score — LM Studio doesn't expose one
 * reliably. Instead checks for hedging language, placeholder syntax, and
 * suspiciously short free-text answers.
 */

const HEDGE_PATTERNS = [
  /\bi'?m not sure\b/i,
  /\bi don'?t know\b/i,
  /\bi cannot\b/i,
  /\bi can'?t\b/i,
  /\bnot specified\b/i,
  /\bunclear\b/i,
  /\bunable to\b/i,
  /\bno information\b/i,
  /\bnot provided\b/i,
  /\bnot available\b/i,
  /\bplease provide\b/i,
  /\binsert .{1,30} here\b/i,
];

// Template / placeholder syntax: [NAME], {{value}}, <PLACEHOLDER>, ___
const PLACEHOLDER_PATTERNS = [
  /\[[A-Z][A-Z _]{1,30}\]/,   // [FIRST NAME], [YOUR ANSWER]
  /\{\{.{1,30}\}\}/,           // {{value}}
  /<[A-Z][A-Z_]{1,30}>/,       // <PLACEHOLDER>
  /_{3,}/,                     // ___
];

/**
 * @param {string} answer  - The generated answer text
 * @param {string} fieldType - 'textarea' | 'text' | 'select' | other
 * @returns {boolean} true if the answer should be flagged as low-confidence
 */
export function isLowConfidence(answer, fieldType = 'text') {
  if (!answer || typeof answer !== 'string') return true;
  const trimmed = answer.trim();
  if (trimmed.length === 0) return true;

  // Free-text fields shorter than 10 words are suspicious
  if (fieldType === 'textarea') {
    const wordCount = trimmed.split(/\s+/).length;
    if (wordCount < 10) return true;
  }

  for (const pattern of HEDGE_PATTERNS) {
    if (pattern.test(trimmed)) return true;
  }

  for (const pattern of PLACEHOLDER_PATTERNS) {
    if (pattern.test(trimmed)) return true;
  }

  return false;
}
