/**
 * Platform detection registry.
 *
 * detect(url) returns the canonical platform name or 'generic'.
 * getHandler(platform) returns the handler module for that platform.
 */

import * as linkedin   from './linkedin.js';
import * as indeed     from './indeed.js';
import * as greenhouse from './greenhouse.js';
import * as lever      from './lever.js';
import * as workday    from './workday.js';
import * as icims      from './icims.js';
import * as generic    from './generic.js';

const HANDLERS = [
  { name: 'linkedin',   module: linkedin   },
  { name: 'indeed',     module: indeed     },
  { name: 'greenhouse', module: greenhouse },
  { name: 'lever',      module: lever      },
  { name: 'workday',    module: workday    },
  { name: 'icims',      module: icims      },
];

/**
 * Detect ATS platform from a URL string.
 * @param {string} url
 * @returns {string} platform name
 */
export function detectPlatform(url) {
  if (!url) return 'generic';
  const u = url.toLowerCase();

  for (const { name, module } of HANDLERS) {
    if (module.detect(u)) return name;
  }
  return 'generic';
}

/**
 * Return the handler module for a platform name.
 * @param {string} platform
 * @returns {{ detect, apply }}
 */
export function getHandler(platform) {
  const entry = HANDLERS.find(h => h.name === platform);
  return entry ? entry.module : generic;
}
