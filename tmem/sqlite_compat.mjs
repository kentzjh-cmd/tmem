/**
 * Zero-dependency SQLite FTS5 wrapper built on Node >= 22.5 `node:sqlite`.
 * Suppresses Node's ExperimentalWarning for SQLite and guarantees deterministic
 * connection closing and transactional commit/rollback.
 */

import { mkdirSync } from "node:fs";
import { dirname } from "node:path";

let _sqliteModulePromise = null;
let _openHook = null;
let _closeHook = null;

function installSqliteWarningFilter() {
  const origEmit = process.emit;
  let restored = false;
  const restore = () => {
    if (restored) return;
    restored = true;
    if (process.emit === filteredEmit) {
      process.emit = origEmit;
    }
  };
  function filteredEmit(name, data) {
    if (
      name === "warning" &&
      data &&
      data.name === "ExperimentalWarning" &&
      String(data.message || "").includes("SQLite")
    ) {
      restore();
      return false;
    }
    return Reflect.apply(origEmit, this, arguments);
  }
  process.emit = filteredEmit;
  return restore;
}

// Suppress the one-time SQLite ExperimentalWarning during module load, then restore process.emit.
const _restoreEmit = installSqliteWarningFilter();
const { DatabaseSync } = await import("node:sqlite");
const _fallbackTimer = setImmediate(_restoreEmit);
if (typeof _fallbackTimer.unref === "function") {
  _fallbackTimer.unref();
}

export function setConnectionTrackingHooks(onOpen, onClose) {
  _openHook = typeof onOpen === "function" ? onOpen : null;
  _closeHook = typeof onClose === "function" ? onClose : null;
}

export function openDatabase(dbPath) {
  mkdirSync(dirname(dbPath), { recursive: true });
  const db = new DatabaseSync(dbPath);
  try {
    db.exec("PRAGMA busy_timeout = 3000;");
  } catch (err) {
    try {
      db.close();
    } catch (_closeErr) {
      // ignore close errors
    }
    throw err;
  }
  if (_openHook) _openHook(dbPath);
  return db;
}

export function closeDatabase(db) {
  if (!db) return;
  try {
    db.close();
  } finally {
    if (_closeHook) _closeHook();
  }
}

export function withDatabase(dbPath, fn) {
  const db = openDatabase(dbPath);
  try {
    db.exec("BEGIN");
    const result = fn(db);
    db.exec("COMMIT");
    return result;
  } catch (err) {
    try {
      db.exec("ROLLBACK");
    } catch (_rollbackErr) {
      // Ignore rollback errors if transaction wasn't active
    }
    throw err;
  } finally {
    closeDatabase(db);
  }
}

export function loadSqliteModule() {
  if (!_sqliteModulePromise) {
    _sqliteModulePromise = Promise.resolve({ DatabaseSync });
  }
  return _sqliteModulePromise;
}
