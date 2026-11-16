/**
 * tmem: Invisible Agent Execution Trace Compiler & Repository-Scoped Memory Engine (Node.js ESM).
 */

export const VERSION = "0.3.0";

let debugEnabled = process.env.TMEM_DEBUG === "1";

export function setDebug(enabled) {
  debugEnabled = Boolean(enabled);
}

export function isDebugEnabled() {
  return debugEnabled || process.env.TMEM_DEBUG === "1";
}

export const logger = {
  debug(...args) {
    if (isDebugEnabled()) {
      const ts = new Date().toISOString();
      console.error(`[${ts}] [DEBUG] [tmem]`, ...args);
    }
  },
  warn(...args) {
    const ts = new Date().toISOString();
    console.error(`[${ts}] [WARN] [tmem]`, ...args);
  },
};
