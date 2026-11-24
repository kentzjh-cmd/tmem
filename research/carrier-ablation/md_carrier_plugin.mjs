/**
 * md-carrier-plugin — the prose (MD) memory carrier's injector.
 *
 * WHY THIS EXISTS
 * ---------------
 * The carrier ablation study (docs/dsh-memory-carrier-ablation.md) has to
 * separate two factors:
 *
 *   carrier : tmem structured memory  vs.  prose MD (MEMORY.md)
 *   access  : automatic injection     vs.  on-demand read
 *
 * Arm B1 is "prose MD + automatic injection": the whole MEMORY.md must enter
 * the model's context without the agent spending a single tool call. DSH has
 * no built-in "inject this file into every turn" knob, so this tiny plugin
 * provides exactly that -- and nothing else:
 *
 *   * it contains NO tmem code, NO retrieval, NO index;
 *   * it injects the FULL text of MEMORY.md once per user turn;
 *   * it registers no tools at all.
 *
 * That keeps B1 honest: the only thing that differs from arm A1 is *what*
 * gets into the context (prose vs structured slice) and *how much* (full file
 * vs. a targeted slice).
 *
 * INJECTION CADENCE (phase-1b fix, 2026-11-24)
 * ---------------------------------------------
 * The first implementation injected the carrier on EVERY agent step. The
 * phase-1 transcripts show the effect: 4-7 md-carrier notices per session
 * (39779 bytes of prose for B1 rep1), while tmem's own injector fires exactly
 * once per user turn (its `isNewUserTurn` + `injected_trace_ids` guard).
 * That asymmetry confounded gate CA-1b -- which is supposed to compare the two
 * carriers under the SAME access mechanism -- with the number of injections.
 *
 * The injector therefore keeps the tmem cadence: the carrier enters the
 * context once per DISTINCT user turn. The guard is session-scoped (keyed by
 * session id) because DSH rebuilds the message list between steps, so the
 * "is it already in the list" check alone cannot see the previous injection.
 *
 * The injection mechanism itself (a pre-step hook that splices a message in
 * front of the last user turn) is intentionally identical to the one tmem
 * uses, so the comparison does not confound "injection" with "implementation".
 *
 * Usage: put this file in the workspace and reference it from a DSH patch:
 *
 *   - insert:
 *       - id: md-carrier
 *         name: "/abs/path/md_carrier_plugin.mjs"
 */

import { readFileSync } from "node:fs";
import { dirname, join, resolve } from "node:path";
import { randomUUID } from "node:crypto";
import { fileURLToPath } from "node:url";

const SELF_DIR = dirname(fileURLToPath(import.meta.url));
const DEFAULT_MEMORY_FILE = "MEMORY.md";

/**
 * sessionKey -> the user input that already received the carrier.
 *
 * One dsh process serves one session, so a module-level map is exactly the
 * right lifetime for "has this user turn already been served?".
 */
const servedTurns = new Map();

function sessionKey(agent) {
  const s = agent?.session;
  return String(
    s?.id ?? s?.header?.id ?? s?.header?.sessionId ?? "default-session"
  );
}

/**
 * DSH delivers environment metadata ("<system-reminder>…</system-reminder>")
 * as a user-role message that carries no source. It is NOT a user turn, and
 * treating it as one made the injector fire twice per session (once for the
 * reminder, once for the real prompt) -- tmem does not, because its guard is
 * "new turn index / new user text" plus a per-trace injection ledger.
 *
 * Stripping the reminder blocks makes the turn key the real user text, so the
 * injector fires exactly once per real user turn -- the same cadence tmem
 * uses, which is what gate CA-1b needs to compare carriers instead of
 * injection counts.
 */
function stripSystemReminders(text) {
  return String(text || "")
    .replace(/<system-reminder>[\s\S]*?<\/system-reminder>/g, "")
    .trim();
}

function extractTextFromContent(content) {
  if (!content) return "";
  if (typeof content === "string") return content;
  if (Array.isArray(content)) {
    return content
      .map((part) => {
        if (typeof part === "string") return part;
        if (part && typeof part === "object" && part.type === "text" && typeof part.text === "string") {
          return part.text;
        }
        return "";
      })
      .filter(Boolean)
      .join("\n");
  }
  return "";
}

function resolveWorkspace(ctx, agent, configured) {
  if (configured) return resolve(configured);
  try {
    const wd = typeof ctx?.get === "function" ? ctx.get("workingDirectory") : null;
    if (wd && typeof wd.get === "function" && agent?.session) {
      const dir = wd.get(agent.session);
      if (typeof dir === "string" && dir.trim()) return resolve(dir);
    }
  } catch (_err) {
    /* ignore */
  }
  if (typeof agent?.session?.header?.cwd === "string" && agent.session.header.cwd.trim()) {
    return resolve(agent.session.header.cwd);
  }
  return resolve(process.cwd());
}

function readCarrier(workspaceRoot, fileName) {
  // ONLY the session's workspace is authoritative: reading the carrier from
  // the plugin's own directory would silently inject the wrong file whenever
  // the workspace cannot be resolved.
  try {
    const text = readFileSync(join(workspaceRoot, fileName), "utf-8");
    if (text.trim()) return text.trim();
  } catch (_err) {
    /* no carrier in this workspace */
  }
  return "";
}

export function apply(ctx, config = {}) {
  const memoryFile = config.memoryFile || DEFAULT_MEMORY_FILE;
  const configuredWorkspace = config.workspaceRoot || "";
  const enabled = config.enabled !== false;

  ctx.on("agent/pre-step", async ({ agent, messages, step }, next) => {
    const decision = await next();
    if (!enabled) return decision;

    const currentMessages = Array.isArray(decision?.messages)
      ? [...decision.messages]
      : Array.isArray(messages)
        ? [...messages]
        : [];

    let lastUserInput = "";
    let lastUserIndex = -1;
    for (let i = 0; i < currentMessages.length; i++) {
      const m = currentMessages[i];
      if (!m) continue;
      if (m.role === "user" && (!m.source || m.source.kind === "user")) {
        lastUserInput = extractTextFromContent(m.content).trim();
        lastUserIndex = i;
      }
    }

    const workspaceRoot = resolveWorkspace(ctx, agent, configuredWorkspace);
    const carrier = readCarrier(workspaceRoot, memoryFile);
    if (!carrier) return decision;

    // Inject once per DISTINCT user turn -- the same cadence tmem uses
    // (`isNewUserTurn`). DSH rebuilds the message list between steps, so the
    // message-level check below cannot see a previous injection; the
    // session-scoped guard is what actually enforces the cadence.
    const key = sessionKey(agent);
    const turnKey = stripSystemReminders(lastUserInput);
    if (turnKey === "") return decision; // environment metadata only, not a turn
    if (servedTurns.get(key) === turnKey) return decision;

    const already = currentMessages.some(
      (m) => m && m.source?.kind === "md-carrier" && extractTextFromContent(m.content).trim() === carrier
    );
    if (already) {
      servedTurns.set(key, turnKey);
      return decision;
    }

    const notice = {
      id: `md_carrier_${randomUUID()}`,
      role: "user",
      content: [{ type: "text", text: carrier }],
      source: { kind: "md-carrier", form: "notice", summary: "prose-memory" },
    };

    if (lastUserIndex >= 0) currentMessages.splice(lastUserIndex, 0, notice);
    else currentMessages.push(notice);

    servedTurns.set(key, turnKey);

    return {
      ...decision,
      ...(decision?.kind ? {} : { action: "continue" }),
      messages: currentMessages,
    };
  });
}

export default {
  name: "md-carrier",
  apply,
};
