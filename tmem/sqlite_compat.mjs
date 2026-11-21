/**
 * Zero-dependency SQLite FTS5 wrapper built on Node >= 22.5 `node:sqlite`.
 * Suppresses Node's ExperimentalWarning for SQLite and guarantees deterministic
 * connection closing and transactional commit/rollback.
 */

import { existsSync, mkdirSync } from "node:fs";
import { dirname, resolve } from "node:path";

let _sqliteModulePromise = null;
let _openHook = null;
let _closeHook = null;
const _walConfiguredPaths = new Set();

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

function isBusySqliteError(err) {
  const msg = String(err?.message || err || "");
  return (
    err?.errcode === 5 ||
    err?.code === "ERR_SQLITE_ERROR" && /busy|locked/i.test(msg)
  );
}

export function openDatabase(dbPath) {
  const isMemory = dbPath === ":memory:";
  const resolved = isMemory ? ":memory:" : resolve(dbPath);
  if (!isMemory && !existsSync(resolved)) {
    _walConfiguredPaths.delete(resolved);
  }
  mkdirSync(dirname(dbPath), { recursive: true });
  const db = new DatabaseSync(dbPath);
  try {
    db.exec("PRAGMA busy_timeout = 3000;");
    if (!isMemory) {
      if (!_walConfiguredPaths.has(resolved)) {
        try {
          db.exec("PRAGMA journal_mode = WAL;");
          _walConfiguredPaths.add(resolved);
        } catch (_walErr) {
          // Ignore if database is read-only or WAL is unsupported
        }
      }
      try {
        db.exec("PRAGMA synchronous = NORMAL;");
      } catch (_syncErr) {
        // Ignore if database is read-only
      }
    }
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

export function withDatabase(dbPath, fn, { readOnly = false } = {}) {
  const maxAttempts = 3;
  for (let attempt = 1; attempt <= maxAttempts; attempt++) {
    const db = openDatabase(dbPath);
    let inTx = false;
    try {
      if (!readOnly) {
        db.exec("BEGIN");
        inTx = true;
      }
      const result = fn(db);
      if (inTx) {
        db.exec("COMMIT");
        inTx = false;
      }
      return result;
    } catch (err) {
      if (inTx) {
        try {
          db.exec("ROLLBACK");
        } catch (_rollbackErr) {
          // Ignore rollback errors if transaction wasn't active
        }
      }
      if (attempt < maxAttempts && isBusySqliteError(err)) {
        continue;
      }
      throw err;
    } finally {
      closeDatabase(db);
    }
  }
}

export function loadSqliteModule() {
  if (!_sqliteModulePromise) {
    _sqliteModulePromise = Promise.resolve({ DatabaseSync });
  }
  return _sqliteModulePromise;
}
