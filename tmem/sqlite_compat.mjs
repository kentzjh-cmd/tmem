/**
 * Zero-dependency SQLite FTS5 wrapper built on Node >= 22.5 `node:sqlite`.
 * Suppresses Node's ExperimentalWarning for SQLite and guarantees deterministic
 * connection closing and transactional commit/rollback.
 */

import { mkdirSync } from "node:fs";
import { dirname } from "node:path";

let _sqliteModulePromise = null;
let _warningFilterInstalled = false;
let _openHook = null;
let _closeHook = null;

function installSqliteWarningFilter() {
  if (_warningFilterInstalled) return;
  _warningFilterInstalled = true;
  const origEmit = process.emit.bind(process);
  process.emit = function filteredEmit(name, data, ...rest) {
    if (
      name === "warning" &&
      data &&
      data.name === "ExperimentalWarning" &&
      String(data.message || "").includes("SQLite")
    ) {
      return false;
    }
    return origEmit(name, data, ...rest);
  };
}

// Install synchronously on module load and import node:sqlite via top-level await.
installSqliteWarningFilter();
const { DatabaseSync } = await import("node:sqlite");

export function setConnectionTrackingHooks(onOpen, onClose) {
  _openHook = typeof onOpen === "function" ? onOpen : null;
  _closeHook = typeof onClose === "function" ? onClose : null;
}

export function openDatabase(dbPath) {
  mkdirSync(dirname(dbPath), { recursive: true });
  const db = new DatabaseSync(dbPath);
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
