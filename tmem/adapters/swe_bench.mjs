/**
 * SWE-bench & ATIF (Agent Trajectory Interchange Format) Public Trajectory Adapter.
 */

import { existsSync, readdirSync, readFileSync, statSync } from "node:fs";
import path from "node:path";
import { ActionKind, ActionNode } from "../models.mjs";
import {
  AgentAdapter,
  ParsedSession,
  cleanRationale,
  extractAssistantConclusion,
  extractErrorSignature,
  extractSymbolsFromText,
  normalizeRelPath,
} from "./base.mjs";

function collectTrajFiles(dir, out = []) {
  if (!existsSync(dir)) return out;
  let entries;
  try {
    entries = readdirSync(dir, { withFileTypes: true });
  } catch (_err) {
    return out;
  }
  for (const ent of entries) {
    const full = path.join(dir, ent.name);
    if (ent.isDirectory()) {
      collectTrajFiles(full, out);
    } else if (
      ent.isFile() &&
      (ent.name.endsWith(".traj") ||
        ent.name.endsWith(".traj.json") ||
        ent.name === "trajectory.json" ||
        ent.name === "atif.json")
    ) {
      out.push(full);
    }
  }
  return out;
}

export class SweBenchAdapter extends AgentAdapter {
  constructor() {
    super();
    this.name = "swe_bench";
  }

  parseTranscript(transcriptPath, workspaceRoot, minStepIdx = 0) {
    const parsed = path.parse(transcriptPath);
    const session = new ParsedSession({
      agent_name: this.name,
      session_id: parsed.name,
      workspace_root: workspaceRoot,
    });
    if (!existsSync(transcriptPath)) return session;

    try {
      const content = readFileSync(transcriptPath, "utf-8").trim();
      if (!content) return session;
      if (content.startsWith("{")) {
        const data = JSON.parse(content);
        this._parseJsonDict(data, session, workspaceRoot, minStepIdx);
      } else {
        this._parseJsonlLines(content.split(/\r?\n/), session, workspaceRoot, minStepIdx);
      }
    } catch (_err) {
      // ignore malformed input
    }
    return session;
  }

  _parseJsonDict(data, session, workspaceRoot, minStepIdx) {
    const instanceId =
      data.instance_id || data.session_id || data.issue_id || session.session_id;
    session.session_id = String(instanceId);

    let problemStmt =
      data.problem_statement ||
      data.issue_description ||
      data.prompt ||
      data.task_description ||
      "";

    if (!problemStmt && Array.isArray(data.history)) {
      for (const h of data.history) {
        if (h && typeof h === "object" && h.role === "user") {
          const cnt = String(h.content || "");
          if (cnt.includes("demonstration of how to correctly accomplish this task")) {
            continue;
          }
          if (cnt.includes("ISSUE:")) {
            problemStmt = cnt.split("ISSUE:")[1].trim();
            break;
          } else if (cnt.includes("solving the following issue")) {
            problemStmt = cnt.trim();
            break;
          }
        }
      }
    }

    if (problemStmt) {
      session.user_intents.push(String(problemStmt).trim().slice(0, 1000));
    }

    if (Array.isArray(data.trajectory)) {
      this._parseSweAgentTrajectory(data.trajectory, session, workspaceRoot, minStepIdx);
    } else if (Array.isArray(data.steps)) {
      this._parseAtifSteps(data.steps, session, workspaceRoot, minStepIdx);
    } else if (Array.isArray(data.history)) {
      this._parseSweAgentTrajectory(data.history, session, workspaceRoot, minStepIdx);
    } else if (Array.isArray(data.events)) {
      this._parseAtifSteps(data.events, session, workspaceRoot, minStepIdx);
    }
  }

  _parseSweAgentTrajectory(traj, session, workspaceRoot, minStepIdx) {
    let lastFileOpened = null;

    for (let i = 0; i < traj.length; i++) {
      const idx = i + 1;
      if (idx < minStepIdx) continue;
      const turn = traj[i] || {};

      const actionStr = String(turn.action || "").trim();
      const obsStr = String(turn.observation || "").trim();
      const thought = String(turn.thought || "");
      const response = String(turn.response || "");

      if (session.user_intents.length === 0 && Array.isArray(turn.query)) {
        for (const q of turn.query) {
          if (q && typeof q === "object" && q.role === "user") {
            session.user_intents.push(String(q.content || "").slice(0, 1000));
          }
        }
      }

      if (response) {
        const conc = extractAssistantConclusion(response);
        if (conc) session.assistant_conclusions.push(conc);
      } else if (thought) {
        const conc = extractAssistantConclusion(thought);
        if (conc) session.assistant_conclusions.push(conc);
      }

      if (turn.state && typeof turn.state === "string") {
        try {
          const sd = JSON.parse(turn.state);
          const of = sd.open_file;
          if (of && of !== "n/a") {
            const cleaned = of.replace(/^\/[^/]+__[^/]+\//, "");
            lastFileOpened = normalizeRelPath(cleaned, workspaceRoot);
          }
        } catch (_err) {
          // ignore
        }
      }

      const mObsF = obsStr.match(/\[File:\s*(?:\/[^/]+__[^/]+\/)?([^\s(\]]+)/);
      if (mObsF) {
        lastFileOpened = normalizeRelPath(mObsF[1], workspaceRoot);
      }

      const node = this._normalizeSweAgentAction(
        idx,
        actionStr,
        obsStr,
        lastFileOpened,
        workspaceRoot
      );
      if (node) {
        const ratRaw = thought || response || "";
        if (ratRaw) {
          node.rationale = cleanRationale(extractAssistantConclusion(ratRaw) || ratRaw);
        }
        if (
          (node.kind === ActionKind.READ || node.kind === ActionKind.MUTATE) &&
          node.target_file
        ) {
          lastFileOpened = node.target_file;
        }

        let exitCode = turn.exit_code;
        if (exitCode === undefined || exitCode === null) {
          const strippedObs = obsStr.trimStart();
          if (
            [
              "Your proposed edit has introduced new syntax error(s)",
              "Not entering edit mode",
              "Edit failed",
              "No replacement was performed",
            ].some((msg) => obsStr.includes(msg))
          ) {
            exitCode = 1;
          } else if (strippedObs.startsWith("[File: ")) {
            exitCode = 0;
          } else if (
            [
              "FAILED (failures=",
              "ERROR: ",
              "Traceback (most recent call last):",
            ].some((err) => obsStr.includes(err))
          ) {
            exitCode = 1;
          } else {
            exitCode = 0;
          }
        }
        node.exit_code = Number(exitCode);

        if (node.exit_code !== 0 && obsStr) {
          const errSig = extractErrorSignature(obsStr);
          if (errSig) {
            node.error_signature = errSig;
            session.active_errors.push(errSig);
          }
        }
        session.actions.push(node);
      }
    }
  }

  _normalizeSweAgentAction(stepIdx, actionStr, obsStr, lastFileOpened, workspaceRoot) {
    if (!actionStr) return null;

    const mOpen = actionStr.match(/^(?:open_file|open|view|cat|read)\s+([^\s\n]+)/);
    if (mOpen) {
      let rawPath = mOpen[1].replace(/^['"]+|['"]+$/g, "");
      rawPath = rawPath.replace(/^\/[^/]+__[^/]+\//, "");
      const fpath = normalizeRelPath(rawPath, workspaceRoot);
      return new ActionNode({
        step_idx: stepIdx,
        kind: ActionKind.READ,
        tool_name: "open_file",
        target_file: fpath,
      });
    }

    const mCreate = actionStr.match(/^(?:create_file|create|touch)\s+([^\s\n]+)/);
    if (mCreate) {
      let rawPath = mCreate[1].replace(/^['"]+|['"]+$/g, "");
      rawPath = rawPath.replace(/^\/[^/]+__[^/]+\//, "");
      const fpath = normalizeRelPath(rawPath, workspaceRoot);
      return new ActionNode({
        step_idx: stepIdx,
        kind: ActionKind.MUTATE,
        tool_name: "create_file",
        target_file: fpath,
      });
    }

    const mEdit = actionStr.match(/^(?:edit_file|edit|patch|write_file)(?:\s+([^\s\n]+))?/);
    if (mEdit) {
      let target = mEdit[1];
      if (!target || /^\d+:\d+$/.test(target)) {
        target = lastFileOpened;
      } else {
        target = target.replace(/^['"]+|['"]+$/g, "").replace(/^\/[^/]+__[^/]+\//, "");
      }
      const fpath = target ? normalizeRelPath(target, workspaceRoot) : null;
      const syms = extractSymbolsFromText(actionStr, fpath);
      return new ActionNode({
        step_idx: stepIdx,
        kind: ActionKind.MUTATE,
        tool_name: "edit_file",
        target_file: fpath,
        symbols_touched: syms,
      });
    }

    if (/^(?:find_file|search_file|search_dir)\s+/.test(actionStr)) {
      return new ActionNode({
        step_idx: stepIdx,
        kind: ActionKind.READ,
        tool_name: "search",
        command: actionStr,
      });
    }

    return new ActionNode({
      step_idx: stepIdx,
      kind: ActionKind.ENV_EXEC,
      tool_name: "bash",
      command: actionStr,
    });
  }

  _parseAtifSteps(steps, session, workspaceRoot, minStepIdx) {
    let lastThought = "";
    for (let i = 0; i < steps.length; i++) {
      const idx = i + 1;
      if (idx < minStepIdx) continue;
      const step = steps[i] || {};

      const stype = step.type || step.role || "";
      const content = step.content || "";

      if (stype === "user" || stype === "human") {
        session.user_intents.push(String(content).slice(0, 1000));
        continue;
      }

      if (stype === "thought" || stype === "assistant" || stype === "model") {
        lastThought = String(content);
        const conc = extractAssistantConclusion(lastThought);
        if (conc) session.assistant_conclusions.push(conc);
      }

      const toolName = String(step.tool_name || step.tool || step.name || "").toLowerCase();
      const toolInput = step.tool_input || step.arguments || step.input || {};
      const obs = String(step.observation || step.output || "");
      const exitCode = Number(step.exit_code ?? step.exitCode ?? 0);

      let node = null;
      if (["read", "view", "open", "file_reader"].some((k) => toolName.includes(k))) {
        const fpath = normalizeRelPath(
          toolInput.path || toolInput.file_path,
          workspaceRoot
        );
        node = new ActionNode({
          step_idx: idx,
          kind: ActionKind.READ,
          tool_name: toolName,
          target_file: fpath,
        });
      } else if (
        ["edit", "write", "patch", "replace", "file_editor"].some((k) =>
          toolName.includes(k)
        )
      ) {
        const fpath = normalizeRelPath(
          toolInput.path || toolInput.file_path,
          workspaceRoot
        );
        const body = String(
          toolInput.content || toolInput.new_str || toolInput.patch || ""
        );
        const syms = extractSymbolsFromText(body, fpath);
        node = new ActionNode({
          step_idx: idx,
          kind: ActionKind.MUTATE,
          tool_name: toolName,
          target_file: fpath,
          symbols_touched: syms,
        });
      } else if (
        ["bash", "exec", "terminal", "shell", "cmd"].includes(toolName) ||
        "command" in toolInput
      ) {
        const cmd = toolInput.command || toolInput.cmd || "";
        node = new ActionNode({
          step_idx: idx,
          kind: ActionKind.ENV_EXEC,
          tool_name: "bash",
          command: String(cmd),
        });
      } else if (toolName) {
        node = new ActionNode({
          step_idx: idx,
          kind: ActionKind.OTHER,
          tool_name: toolName,
        });
      }

      if (node) {
        if (lastThought) {
          node.rationale = cleanRationale(
            extractAssistantConclusion(lastThought) || lastThought
          );
        }
        node.exit_code = exitCode;
        if (exitCode !== 0 && obs) {
          const errSig = extractErrorSignature(obs);
          if (errSig) {
            node.error_signature = errSig;
            session.active_errors.push(errSig);
          }
        }
        session.actions.push(node);
      }
    }
  }

  _parseJsonlLines(lines, session, workspaceRoot, minStepIdx) {
    const steps = [];
    for (const ln of lines) {
      if (!ln.trim()) continue;
      try {
        steps.push(JSON.parse(ln));
      } catch (_err) {
        // ignore
      }
    }
    this._parseAtifSteps(steps, session, workspaceRoot, minStepIdx);
  }

  discoverLatestTranscript(workspaceRoot) {
    const candidates = collectTrajFiles(path.resolve(workspaceRoot));
    if (candidates.length === 0) return null;
    candidates.sort((a, b) => statSync(b).mtimeMs - statSync(a).mtimeMs);
    return candidates[0];
  }
}
