/**
 * Global Q&A cache — persisted as a JSON file alongside the SQLite database.
 *
 * Using a JSON file instead of SQLite avoids native binary dependencies in the
 * Node.js service (better-sqlite3 fails to build against newer Node versions).
 * The file lives at data/qa_cache.json — same directory as the SQLite DB.
 *
 * Questions are normalised (lowercased, punctuation stripped) so the same
 * question phrased slightly differently still hits the cache.
 *
 * Applications run one at a time, so no file-locking is needed.
 */

import fs from 'fs';
import path from 'path';
import { fileURLToPath } from 'url';

const __dirname = path.dirname(fileURLToPath(import.meta.url));

function getCachePath() {
  // If DB_PATH is set, put the cache JSON next to the DB file
  if (process.env.DB_PATH) {
    const dbResolved = path.resolve(__dirname, '..', process.env.DB_PATH);
    return path.join(path.dirname(dbResolved), 'qa_cache.json');
  }
  return path.resolve(__dirname, '../../data/qa_cache.json');
}

/**
 * Normalise a question string for use as a cache key.
 */
function normalizeQuestion(question) {
  return question
    .toLowerCase()
    .replace(/[^\w\s]/g, ' ')
    .replace(/\s+/g, ' ')
    .trim();
}

/**
 * Read the full cache from disk. Returns {} on any error.
 * @returns {Record<string, {answer: string, confidence: string, updated_at: string}>}
 */
function readCache() {
  try {
    return JSON.parse(fs.readFileSync(getCachePath(), 'utf8'));
  } catch {
    return {};
  }
}

/**
 * Write the full cache to disk.
 */
function writeCache(cache) {
  const p = getCachePath();
  fs.mkdirSync(path.dirname(p), { recursive: true });
  fs.writeFileSync(p, JSON.stringify(cache, null, 2), 'utf8');
}

/**
 * Look up a cached answer. Returns null if not found.
 *
 * @param {string} question
 * @returns {{ answer: string, confidence: string } | null}
 */
export function getCachedAnswer(question) {
  const cache = readCache();
  const key = normalizeQuestion(question);
  return cache[key] || null;
}

/**
 * Store an answer in the global cache.
 *
 * @param {string} question
 * @param {string} answer
 * @param {'high'|'low'} confidence
 */
export function setCachedAnswer(question, answer, confidence = 'high') {
  try {
    const cache = readCache();
    const key = normalizeQuestion(question);
    cache[key] = { answer, confidence, updated_at: new Date().toISOString() };
    writeCache(cache);
  } catch (err) {
    console.warn('[qa-cache] write failed:', err.message);
  }
}
