/**
 * Runtime state persistence for tmem agent hooks.
 */

import { randomBytes } from "node:crypto";
import {
  closeSync,
  existsSync,
  mkdirSync,
  openSync,
  readFileSync,
  renameSync,
  statSync,
  unlinkSync,
  writeFileSync,
} from "node:fs";
import path from "node:path";
import { logger } from "../index.mjs";

export const LOOP_STREAK_THRESHOLD = 3;
export const MAX_TRACKED_SESSIONS = 32;

export function defaultRuntimeState(convId = "default") {
  return {
    conversation_id: convId,
    last_turn_idx: -1,
    last_captured_step_idx: 0,
    injected_trace_ids: [],
    injected_compaction_ids: [],
    rewarded_trace_ids: [],
    penalized_trace_ids: [],
    micro_skills_injected: false,
    session_errors: [],
    recent_errors: [],
    recent_files: [],
    uncommitted_mutations: [],
    verified_since_mutation: false,
    milestone_prompted_for_mutations: [],
    error_streak: { signature: "", count: 0 },
    edit_fail_streak: {},
    loop_intervened_keys: [],
    pending_loop_breaker: "",
    pending_l2_hints: [],
    pending_l1_hints: [],
    cochange_warned_pairs: [],
  };
}

function normalizeSessionState(rawState, convId) {
  const def = defaultRuntimeState(convId);
  const state = { ...def, ...rawState };
  if (!state.error_streak || typeof state.error_streak !== "object") {
    state.error_streak = { signature: "", count: 0 };
  }
  if (!state.edit_fail_streak || typeof state.edit_fail_streak !== "object") {
    state.edit_fail_streak = {};
  }
  return state;
}

function readStateContainer(stateFile) {
  if (!existsSync(stateFile)) return [{}, ""];
  try {
    const data = JSON.parse(readFileSync(stateFile, "utf-8"));
    if (!data || typeof data !== "object" || Array.isArray(data)) return [{}, ""];
    if (data.sessions && typeof data.sessions === "object" && !Array.isArray(data.sessions)) {
      const sessions = {};
      for (const [k, v] of Object.entries(data.sessions)) {
        if (v && typeof v === "object" && !Array.isArray(v)) {
          sessions[String(k)] = { ...v };
        }
      }
      return [sessions, String(data.latest_session_id || "")];
    }
    const legacyId = String(data.conversation_id || "default");
    return [{ [legacyId]: { ...data } }, legacyId];
  } catch (err) {
    logger.debug("Failed to read runtime_state.json:", err?.message || err);
    return [{}, ""];
  }
}

export function loadRuntimeState(workspaceRoot, convId = "default") {
  const stateFile = path.join(workspaceRoot, ".tmem", "runtime_state.json");
  const [sessions, latestId] = readStateContainer(stateFile);
  const targetId = String(convId || "").trim() || "default";

  if (targetId in sessions) {
    return normalizeSessionState(sessions[targetId], targetId);
  }
  if (targetId === "default" && latestId && latestId in sessions) {
    return normalizeSessionState(sessions[latestId], latestId);
  }
  return defaultRuntimeState(targetId);
}

function acquireFileLock(lockPath) {
  const deadline = Date.now() + 500;
  while (Date.now() < deadline) {
    try {
      const fd = openSync(lockPath, "wx");
      closeSync(fd);
      return true;
    } catch (err) {
      if (err && err.code === "EEXIST") {
        try {
          const st = statSync(lockPath);
          if (Date.now() - st.mtimeMs > 5000) {
            unlinkSync(lockPath);
            continue;
          }
        } catch (_stErr) {
          continue;
        }
        // Brief synchronous yield via Atomics.wait on a dummy Int32Array
        Atomics.wait(new Int32Array(new SharedArrayBuffer(4)), 0, 0, 5);
        continue;
      }
      return false;
    }
  }
  return false;
}

export function saveRuntimeState(workspaceRoot, state) {
  const stateDir = path.join(workspaceRoot, ".tmem");
  const stateFile = path.join(stateDir, "runtime_state.json");
  mkdirSync(stateDir, { recursive: true });
  const convId = String(state.conversation_id || "default");
  const lockPath = path.join(stateDir, ".runtime_state.lock");
  const locked = acquireFileLock(lockPath);

  let tmpPath = null;
  try {
    const [sessions] = readStateContainer(stateFile);
    if (convId in sessions) delete sessions[convId];
    sessions[convId] = normalizeSessionState(state, convId);

    const keys = Object.keys(sessions);
    if (keys.length > MAX_TRACKED_SESSIONS) {
      const excess = keys.length - MAX_TRACKED_SESSIONS;
      for (let i = 0; i < excess; i++) {
        delete sessions[keys[i]];
      }
    }

    const container = {
      latest_session_id: convId,
      sessions,
    };

    const rand = randomBytes(4).toString("hex");
    tmpPath = path.join(stateDir, `.runtime_state.${process.pid}.${rand}.tmp`);
    writeFileSync(tmpPath, JSON.stringify(container, null, 2), "utf-8");
    renameSync(tmpPath, stateFile);
    tmpPath = null;
  } catch (err) {
    logger.debug("Failed to write runtime_state.json:", err?.message || err);
  } finally {
    if (tmpPath && existsSync(tmpPath)) {
      try {
        unlinkSync(tmpPath);
      } catch (_err) {
        // ignore
      }
    }
    if (locked) {
      try {
        unlinkSync(lockPath);
      } catch (_err) {
        // ignore
      }
    }
  }
}

export function recordToolOutcomeForLoop(
  state,
  { hasFailure, errorSig = "", mutatedFiles = null, verifyPassed = false }
) {
  if (verifyPassed) {
    state.error_streak = { signature: "", count: 0 };
    state.edit_fail_streak = {};
    state.pending_loop_breaker = "";
    return null;
  }

  if (!hasFailure) return null;

  const intervened = [...(state.loop_intervened_keys || [])];
  const reasons = [];
  const triggerKeys = [];

  if (errorSig) {
    let streak = state.error_streak || { signature: "", count: 0 };
    if (streak.signature === errorSig) {
      streak.count = Number(streak.count || 0) + 1;
    } else {
      streak = { signature: errorSig, count: 1 };
    }
    state.error_streak = streak;
    if (streak.count >= LOOP_STREAK_THRESHOLD) {
      const key = `err:${errorSig}`;
      if (!intervened.includes(key)) {
        triggerKeys.push(key);
        reasons.push(
          `Same error signature repeated ${streak.count} times in a row: \`${errorSig}\``
        );
      }
    }
  }

  const editStreak = { ...(state.edit_fail_streak || {}) };
  let targetFiles = Array.isArray(mutatedFiles) ? [...mutatedFiles] : [];
  if (
    targetFiles.length === 0 &&
    state.uncommitted_mutations &&
    state.uncommitted_mutations.length > 0
  ) {
    targetFiles = [state.uncommitted_mutations[state.uncommitted_mutations.length - 1]];
  }

  for (const fp of targetFiles) {
    editStreak[fp] = Number(editStreak[fp] || 0) + 1;
    if (editStreak[fp] >= LOOP_STREAK_THRESHOLD) {
      const key = `file:${fp}`;
      if (!intervened.includes(key)) {
        triggerKeys.push(key);
        reasons.push(
          `Repeated edit/failure loop (${editStreak[fp]}x) involving \`${fp}\``
        );
      }
    }
  }
  state.edit_fail_streak = editStreak;

  if (triggerKeys.length === 0) return null;

  intervened.push(...triggerKeys);
  state.loop_intervened_keys = intervened;
  return (
    "<tmem_loop_breaker>\n" +
    "Failure-loop circuit breaker triggered:\n" +
    reasons.map((r) => `- ${r}`).join("\n") +
    "\nAction required: Stop repeating local edits or re-running the same failing command. " +
    "Re-read the surrounding module/imports or switch your debugging hypothesis before editing again.\n" +
    "</tmem_loop_breaker>"
  );
}

export function applySessionTraceFeedback(store, state, outcome) {
  const injected = [...(state.injected_trace_ids || [])];
  if (injected.length === 0) return;

  if (outcome === "success") {
    const already = new Set(state.rewarded_trace_ids || []);
    const targets = injected.filter((tid) => !already.has(tid));
    if (targets.length > 0) {
      store.recordTraceOutcome(targets, "success");
      state.rewarded_trace_ids = Array.from(new Set([...already, ...targets]));
    }
  } else if (outcome === "contradict") {
    const already = new Set(state.penalized_trace_ids || []);
    const targets = injected.filter((tid) => !already.has(tid));
    if (targets.length > 0) {
      store.recordTraceOutcome(targets, "contradict");
      state.penalized_trace_ids = Array.from(new Set([...already, ...targets]));
    }
  }
}
