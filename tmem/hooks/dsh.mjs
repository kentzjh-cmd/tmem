/**
 * DeepSeek Harness (@deepseek-ai/dsh) Cordis hook handler for tmem.
 */

import { existsSync, readdirSync } from "node:fs";
import path from "node:path";
import {
  extractErrorSignature,
  isCodeFile,
  normalizeRelPath,
} from "../adapters/base.mjs";
import { DSH_EXIT_CODE_RE, parseApplyPatchTargets } from "../adapters/dsh.mjs";
import { findCochangePartnersForFiles } from "../cochange.mjs";
import { BenchmarkEvaluator, formatMarkdownReport } from "../evaluate.mjs";
import { formatInjectedContext } from "../guards.mjs";
import { extractMicroSkills, formatMicroSkillsBlock } from "../mem_doc.mjs";
import {
  formatProgressiveView,
  formatRawTraceDiff,
  formatRawTraceReplay,
} from "../raw_storage.mjs";
import { commandsAreSameOrigin } from "../slicer.mjs";
import { ProjectMemoryStore } from "../store.mjs";
import { computeTelemetrySummary, recordTelemetryEvent } from "../telemetry.mjs";
import { parseInspectArgs, parseSlashTmemCommand } from "./commands.mjs";
import {
  applySessionTraceFeedback,
  loadRuntimeState,
  recordToolOutcomeForLoop,
  saveRuntimeState,
} from "./state.mjs";

const FILE_PATH_MENTION_RE =
  /(?<![A-Za-z0-9_./-])((?:[\w.-]+\/)*[\w.-]+\.(?:py|js|mjs|cjs|ts|tsx|jsx|go|rs|java|yml|yaml|json|toml|sh))(?![A-Za-z0-9_.-])/g;
const ERROR_NAME_MENTION_RE = /\b([A-Z][A-Za-z0-9]*(?:Error|Exception|Fault|Panic))\b/g;

function extractExplicitAnchorsFromText(text, workspaceRoot) {
  if (!text) return [[], []];
  const ws = path.resolve(workspaceRoot);
  const files = [];
  for (const m of text.matchAll(FILE_PATH_MENTION_RE)) {
    const rawFp = m[1];
    const rel = normalizeRelPath(rawFp, workspaceRoot);
    if (rel && (existsSync(path.join(ws, rel)) || rel.includes("/")) && !files.includes(rel)) {
      files.push(rel);
    }
  }

  const errors = [];
  const sig = extractErrorSignature(text);
  if (sig) errors.push(sig);
  for (const m of text.matchAll(ERROR_NAME_MENTION_RE)) {
    const errTok = m[1];
    if (!errors.includes(errTok) && errTok.length >= 6) {
      errors.push(errTok);
    }
  }
  return [files, errors];
}

let defaultCaptureFn = null;
export function registerDefaultCaptureFn(fn) {
  defaultCaptureFn = fn;
}

export function handleDshHook(event, payload = {}, captureFn = null) {
  const workspaceRoot = String(
    payload.workspaceRoot ||
      (Array.isArray(payload.workspacePaths) && payload.workspacePaths[0]) ||
      process.cwd()
  );
  const sessionId = String(payload.sessionId || payload.conversationId || "default");

  const store = new ProjectMemoryStore(workspaceRoot);
  const state = loadRuntimeState(workspaceRoot, sessionId);

  if (event === "command") {
    const rawArgs = String(payload.args || "").trim();
    const cmdStr = `/tmem ${rawArgs}`.trim();
    const slashCmd = parseSlashTmemCommand(cmdStr);
    const [actionMode, customIntent] = slashCmd || ["project", rawArgs];

    if (actionMode === "status") {
      const projCnt = existsSync(store.project_traces_dir)
        ? readdirSync(store.project_traces_dir).filter((f) => f.endsWith(".json")).length
        : 0;
      const tel = computeTelemetrySummary(workspaceRoot);
      return {
        kind: "success",
        text:
          `[tmem status]\n` +
          `- Project Tier (${store.project_dir}): ${projCnt} trace(s)\n` +
          `- Telemetry: L2 ${tel.l2_injections} hint(s) (resolved=${tel.l2_resolved_green}/${
            tel.l2_resolved_green + tel.l2_unresolved
          }), L1 ${tel.l1_hints} hint(s) (adopted=${tel.l1_adopted}/${
            tel.l1_adopted + tel.l1_ignored
          })`,
      };
    }

    if (actionMode === "telemetry") {
      const tel = computeTelemetrySummary(workspaceRoot);
      return {
        kind: "success",
        text: `[tmem telemetry]\n${JSON.stringify(tel, null, 2)}`,
      };
    }

    if (actionMode === "gc") {
      const pruneStale = customIntent.includes("--prune");
      const stats = store.runMetabolismGc(pruneStale);
      return {
        kind: "success",
        text: `[tmem gc] Guard metabolism completed: ${JSON.stringify(stats)}`,
      };
    }

    if (actionMode === "list") {
      const items = store.listPlaybooks();
      return {
        kind: "success",
        text: `[tmem list (${items.length})]\n${JSON.stringify(items, null, 2)}`,
      };
    }

    if (actionMode === "bench" || actionMode === "evaluate") {
      const evaluator = new BenchmarkEvaluator();
      const summary = evaluator.runEvaluation();
      return {
        kind: "success",
        text: formatMarkdownReport(summary),
      };
    }

    if (actionMode === "forget") {
      if (!customIntent.trim()) {
        return { kind: "error", text: "Usage: /tmem forget <trace_id>" };
      }
      const delRes = store.deletePlaybook(customIntent);
      return {
        kind: "success",
        text: `[tmem forget] ${JSON.stringify(delRes)}`,
      };
    }

    if (actionMode === "inspect") {
      const [tid, level] = parseInspectArgs(customIntent);
      let targetPb = null;
      if (tid) {
        for (const pb of store.getAllProjectPlaybooks()) {
          if (
            pb.trace_id === tid ||
            pb.trace_id.startsWith(tid) ||
            (pb.source_session_ids || []).some(
              (s) => s === tid || s.startsWith(tid)
            )
          ) {
            targetPb = pb;
            break;
          }
        }
      }
      if (!targetPb) {
        return {
          kind: "error",
          text: `[tmem inspect] Trace '${tid}' not found in project memory.`,
        };
      }
      const rawTr = store.getRawTrace(targetPb.trace_id);
      const rendered = formatProgressiveView(targetPb, rawTr, level);
      return {
        kind: "success",
        text: `[tmem inspect ${targetPb.trace_id} level=${level}]\n${rendered}`,
      };
    }

    if (actionMode === "replay") {
      const tid = customIntent.trim();
      const rawTr = store.getRawTrace(tid);
      if (!rawTr) {
        return {
          kind: "error",
          text: `[tmem replay] Raw execution trace for '${tid}' not found in .tmem/raw/.`,
        };
      }
      return {
        kind: "success",
        text: `[tmem replay ${tid}]\n${formatRawTraceReplay(rawTr)}`,
      };
    }

    if (actionMode === "diff") {
      const tid = customIntent.trim();
      const rawTr = store.getRawTrace(tid);
      if (!rawTr) {
        return {
          kind: "error",
          text: `[tmem diff] Raw execution trace for '${tid}' not found in .tmem/raw/.`,
        };
      }
      return {
        kind: "success",
        text: `[tmem diff ${tid}]\n${formatRawTraceDiff(rawTr)}`,
      };
    }

    if (actionMode === "sync") {
      store.syncProjectIndexIfNeeded();
      const gcStats = store.runMetabolismGc();
      const docPath = store.syncMemoriesMarkdown();
      return {
        kind: "success",
        text: `[tmem sync] Synchronized indexes & generated \`${path.basename(
          docPath
        )}\`. GC: ${JSON.stringify(gcStats)}`,
      };
    }

    // Fallback: capture
    const fn = captureFn || defaultCaptureFn;
    if (!fn) {
      return { kind: "error", text: "Capture handler not initialized." };
    }
    const res = fn({
      workspaceRoot,
      agentName: "dsh",
      scope: "project",
      customIntent: customIntent || null,
      sessionId,
    });
    const statusVal = res.status;
    return {
      kind:
        statusVal === "compiled"
          ? "success"
          : statusVal === "skipped_trivial"
            ? "info"
            : "error",
      text: JSON.stringify(res, null, 2),
    };
  }

  if (event === "pre_step" || event === "pre_invocation") {
    const userMessage = String(payload.userMessage || payload.lastUserInput || "").trim();
    const lastTurn = Number(payload.turnIndex ?? -1);

    const isNewUserTurn =
      lastTurn > Number(state.last_turn_idx ?? -1) ||
      Boolean(userMessage && userMessage !== (state.last_user_input || ""));
    if (lastTurn >= 0) state.last_turn_idx = lastTurn;
    if (userMessage) state.last_user_input = userMessage;

    const uncommitted = [...(state.uncommitted_mutations || [])];
    const promptedSet = new Set(state.milestone_prompted_for_mutations || []);
    const hasCodeFile = uncommitted.some((f) => isCodeFile(f));
    const setsEqual =
      uncommitted.length === promptedSet.size &&
      uncommitted.every((f) => promptedSet.has(f));

    if (uncommitted.length > 0 && state.verified_since_mutation && !setsEqual && hasCodeFile) {
      state.milestone_prompted_for_mutations = uncommitted;
      saveRuntimeState(workspaceRoot, state);
      return {
        injectedContext:
          "<tmem_milestone_reached>\n" +
          `Goal milestone detected: ${uncommitted.length} file(s) modified (${uncommitted
            .slice(0, 4)
            .join(", ")}) and verification succeeded.\n` +
          "When concluding this turn, append a concise one-line prompt asking whether to persist the path to memory:\n" +
          "> 💡 *是否需要将本次有效执行路径沉淀为记忆？（输入 `/tmem` 或 `/remember [备注]` 保存）*\n" +
          "</tmem_milestone_reached>",
      };
    }

    const preBlocks = [];
    if (isNewUserTurn && !state.micro_skills_injected) {
      const skills = extractMicroSkills(store.getAllProjectPlaybooks(), {
        workspaceRoot,
      });
      const skillsBlock = formatMicroSkillsBlock(skills);
      if (skillsBlock) preBlocks.push(skillsBlock);
      state.micro_skills_injected = true;
    }

    const recentErrors = [...(state.recent_errors || [])];
    const recentFiles = [...(state.recent_files || [])];

    const [explicitFiles, explicitErrors] = extractExplicitAnchorsFromText(
      userMessage,
      workspaceRoot
    );
    const shouldRecall = isNewUserTurn || recentErrors.length > 0;
    const anchorFiles = shouldRecall
      ? Array.from(new Set([...recentFiles, ...explicitFiles]))
      : [];
    const anchorErrors = shouldRecall
      ? Array.from(new Set([...recentErrors, ...explicitErrors]))
      : [];

    if (anchorErrors.length > 0 || explicitFiles.length > 0) {
      const matched = store.cascadingLookup({
        queryText: "",
        filePaths: anchorFiles,
        errorSignatures: anchorErrors,
        limit: 2,
      });
      const alreadyInjected = new Set(state.injected_trace_ids || []);
      const freshMatches = matched.filter(
        (ev) => !alreadyInjected.has(ev.playbook.trace_id) || recentErrors.length > 0
      );
      if (freshMatches.length > 0) {
        for (const ev of freshMatches) alreadyInjected.add(ev.playbook.trace_id);
        state.injected_trace_ids = Array.from(alreadyInjected);
        state.recent_errors = [];
        const memBlock = formatInjectedContext(freshMatches);
        if (memBlock) preBlocks.push(memBlock);
      }
    }

    saveRuntimeState(workspaceRoot, state);
    if (preBlocks.length > 0) {
      return { injectedContext: preBlocks.join("\n\n") };
    }
    return {};
  }

  if (event === "post_tool_use" || event === "post_execute") {
    const toolCall = payload.toolCall || payload.tool || {};
    const tName = String(toolCall.name || payload.toolName || "");
    const args = toolCall.args || payload.args || {};
    const resultPayload = payload.result || {};
    const resultText = String(
      payload.resultText ||
        payload.output ||
        resultPayload.text ||
        resultPayload.output ||
        payload.error ||
        ""
    );
    const isError = Boolean(payload.isError || payload.error || resultPayload.error);
    const rawExit = payload.exitCode;

    let effectiveExit = null;
    if (typeof rawExit === "number" && Number.isInteger(rawExit)) {
      effectiveExit = rawExit;
    } else {
      const mExit = resultText.match(DSH_EXIT_CODE_RE);
      if (mExit) {
        effectiveExit = parseInt(mExit[1], 10);
      } else if (isError) {
        effectiveExit = 1;
      } else if (["bash", "pwsh", "run_command"].includes(tName)) {
        effectiveExit = 0;
      }
    }

    const additionalBlocks = [];
    const hasFailure = isError || (effectiveExit !== null && effectiveExit !== 0);
    const isShellTool = ["bash", "pwsh", "run_command"].includes(tName);
    const cmdLine = isShellTool ? String(args.command || args.CommandLine || "") : "";

    let sig = "";
    if (hasFailure && resultText) {
      const lines = resultText.trim().split(/\r?\n/);
      sig =
        extractErrorSignature(resultText) ||
        (lines.length ? lines[lines.length - 1].slice(0, 120) : "");
    }

    // 1. Resolve pending L2 telemetry
    const pendingL2 = [...(state.pending_l2_hints || [])];
    if (isShellTool && pendingL2.length > 0 && cmdLine) {
      const remainingL2 = [];
      for (const hint of pendingL2) {
        const hCmd = hint.failed_command || "";
        const hSig = hint.error_signature || "";
        const hTid = hint.trace_id || "";
        const sameOrigin = !hCmd || commandsAreSameOrigin(hCmd, cmdLine);
        if (!hasFailure && sameOrigin) {
          recordTelemetryEvent(workspaceRoot, "l2_outcome", {
            session_id: sessionId,
            trace_id: hTid,
            error_signature: hSig,
            resolved: true,
            verify_command: cmdLine,
          });
          if (hTid) {
            store.recordTraceOutcome([hTid], "success");
            const rewarded = new Set(state.rewarded_trace_ids || []);
            rewarded.add(hTid);
            state.rewarded_trace_ids = Array.from(rewarded);
          }
        } else if (
          hasFailure &&
          sig &&
          (hSig.toLowerCase().includes(sig.toLowerCase()) ||
            sig.toLowerCase().includes(hSig.toLowerCase()))
        ) {
          recordTelemetryEvent(workspaceRoot, "l2_outcome", {
            session_id: sessionId,
            trace_id: hTid,
            error_signature: hSig,
            resolved: false,
            verify_command: cmdLine,
          });
        } else {
          remainingL2.push(hint);
        }
      }
      state.pending_l2_hints = remainingL2;
    }

    // 2. Immediate L2_INVARIANT lookup on failing command
    if (hasFailure && sig) {
      if (!state.recent_errors.includes(sig)) state.recent_errors.push(sig);
      if (!state.session_errors.includes(sig)) state.session_errors.push(sig);

      const matched = store.lookupL2Repair({
        errorSignatures: [sig],
        filePaths: [...(state.recent_files || [])],
        limit: 1,
      });
      if (matched.length > 0) {
        const memBlock = formatInjectedContext(matched);
        if (memBlock) {
          for (const ev of matched) {
            const tid = ev.playbook.trace_id;
            if (!state.injected_trace_ids.includes(tid)) {
              state.injected_trace_ids.push(tid);
            }
            const alreadyPending = state.pending_l2_hints.some(
              (h) => h.trace_id === tid && h.error_signature === sig
            );
            if (!alreadyPending) {
              state.pending_l2_hints.push({
                trace_id: tid,
                error_signature: sig,
                failed_command: cmdLine,
              });
              const repAct =
                ev.playbook.l2_causal_repairs && ev.playbook.l2_causal_repairs.length > 0
                  ? ev.playbook.l2_causal_repairs[0].repair_action
                  : "";
              recordTelemetryEvent(workspaceRoot, "l2_injected", {
                session_id: sessionId,
                trace_id: tid,
                error_signature: sig,
                failed_command: cmdLine,
                repair_action: repAct,
              });
            }
          }
          state.recent_errors = [];
          additionalBlocks.push(memBlock);
        }
      }
    }

    // 3. Track touched files & resolve/emit L1 co-change hints
    const touchedPaths = [];
    if (tName === "apply_patch") {
      const patchInput = String(args.input || args.patch || args.diff || "");
      for (const [pRaw] of parseApplyPatchTargets(patchInput)) {
        const rel = normalizeRelPath(pRaw, workspaceRoot);
        if (rel) touchedPaths.push(rel);
      }
    } else {
      const rawPath = args.file_path ?? args.path ?? args.Path ?? args.TargetFile;
      const rel = normalizeRelPath(rawPath, workspaceRoot);
      if (rel) touchedPaths.push(rel);
    }

    const isMutateTool = [
      "write",
      "edit",
      "str_replace_editor",
      "apply_patch",
      "write_file",
      "edit_file",
    ].includes(tName);
    const isExplicitReadTool = ["read", "read_file", "view_file"].includes(tName);

    const pendingL1 = [...(state.pending_l1_hints || [])];
    if (
      touchedPaths.length > 0 &&
      pendingL1.length > 0 &&
      !hasFailure &&
      (isMutateTool || isExplicitReadTool)
    ) {
      const touchedSet = new Set(touchedPaths);
      const stillPendingL1 = [];
      for (const hint of pendingL1) {
        const sugg = new Set(hint.suggested_files || []);
        const adopted = Array.from(touchedSet)
          .filter((f) => sugg.has(f))
          .sort();
        if (adopted.length > 0 && isMutateTool) {
          recordTelemetryEvent(workspaceRoot, "l1_outcome", {
            session_id: sessionId,
            edited_file: hint.edited_file || "",
            adopted_files: adopted,
            adopted: true,
            resolution: "mutated",
          });
        } else if (adopted.length > 0 && isExplicitReadTool) {
          const prevInspected = new Set(hint.inspected_files || []);
          hint.inspected_files = Array.from(
            new Set([...prevInspected, ...adopted])
          ).sort();
          stillPendingL1.push(hint);
        } else {
          stillPendingL1.push(hint);
        }
      }
      state.pending_l1_hints = stillPendingL1;
    }

    const mutatedThisStep = [];
    for (const relPath of touchedPaths) {
      if (relPath.startsWith(".tmem/")) continue;
      if (!state.recent_files.includes(relPath)) {
        state.recent_files = [...state.recent_files, relPath].slice(-5);
      }
      if (isMutateTool && !hasFailure) {
        mutatedThisStep.push(relPath);
        if (!state.uncommitted_mutations.includes(relPath)) {
          state.uncommitted_mutations.push(relPath);
          state.verified_since_mutation = false;
        }
      }
    }

    if (mutatedThisStep.length > 0) {
      const codeMutated = mutatedThisStep.filter((f) => isCodeFile(f));
      if (codeMutated.length > 0) {
        const partners = findCochangePartnersForFiles(
          workspaceRoot,
          codeMutated,
          store.getAllProjectPlaybooks(),
          2
        );
        const warnedPairs = new Set(state.cochange_warned_pairs || []);
        const uncommittedSet = new Set(state.uncommitted_mutations || []);
        const freshHints = [];
        for (const [srcF, partnerF, cnt, conf] of partners) {
          const pairKey = `${srcF}->${partnerF}`;
          if (uncommittedSet.has(partnerF) || warnedPairs.has(pairKey)) continue;
          warnedPairs.add(pairKey);
          freshHints.push([srcF, partnerF, cnt, conf]);
        }
        if (freshHints.length > 0) {
          state.cochange_warned_pairs = Array.from(warnedPairs);
          const hintLines = freshHints.map(
            ([srcF, partnerF, cnt, conf]) =>
              `- \`${srcF}\` historically co-changes with \`${partnerF}\` (${cnt}x, ${Math.round(
                conf * 100
              )}%)`
          );
          additionalBlocks.push(
            "<tmem_cochange_hint>\n" +
              "Co-change partners detected from repository history/traces (verify if corresponding updates are needed):\n" +
              hintLines.join("\n") +
              "\n</tmem_cochange_hint>"
          );
          const suggestedFiles = freshHints.map((item) => item[1]);
          state.pending_l1_hints.push({
            edited_file: freshHints[0][0],
            suggested_files: suggestedFiles,
          });
          recordTelemetryEvent(workspaceRoot, "l1_cochange_hinted", {
            session_id: sessionId,
            edited_file: freshHints[0][0],
            suggested_files: suggestedFiles,
          });
        }
      }
    }

    // 4. Detect verification pass
    let verifyPassed = false;
    const verifyKeywords = [
      "test",
      "pytest",
      "unittest",
      "cargo",
      "go test",
      "npm",
      "pnpm",
      "make",
      "tsc",
      "lint",
      "node --test",
    ];
    if (
      isShellTool &&
      effectiveExit === 0 &&
      !isError &&
      state.uncommitted_mutations &&
      state.uncommitted_mutations.length > 0
    ) {
      if (
        verifyKeywords.some((k) => cmdLine.includes(k)) &&
        !cmdLine.includes("tmem.cli") &&
        !cmdLine.includes("cli.mjs")
      ) {
        state.verified_since_mutation = true;
        verifyPassed = true;
        applySessionTraceFeedback(store, state, "success");

        if (state.pending_l1_hints && state.pending_l1_hints.length > 0) {
          for (const hint of state.pending_l1_hints) {
            const inspected = [...(hint.inspected_files || [])];
            recordTelemetryEvent(workspaceRoot, "l1_outcome", {
              session_id: sessionId,
              edited_file: hint.edited_file || "",
              adopted_files: inspected,
              adopted: inspected.length > 0,
              resolution: inspected.length > 0 ? "inspected" : "ignored",
            });
          }
          state.pending_l1_hints = [];
        }

        const uncommitted = [...(state.uncommitted_mutations || [])];
        const promptedSet = new Set(state.milestone_prompted_for_mutations || []);
        const hasCodeFile = uncommitted.some((f) => isCodeFile(f));
        const setsEqual =
          uncommitted.length === promptedSet.size &&
          uncommitted.every((f) => promptedSet.has(f));
        if (!setsEqual && hasCodeFile) {
          state.milestone_prompted_for_mutations = uncommitted;
          additionalBlocks.push(
            "<tmem_milestone_reached>\n" +
              `Milestone detected: ${uncommitted.length} file(s) modified (${uncommitted
                .slice(0, 4)
                .join(", ")}) and verification succeeded (\`${cmdLine.slice(0, 60)}\`).\n` +
              "When concluding this turn, append a concise one-line prompt asking whether to persist the path to memory:\n" +
              "> 💡 *是否需要将本次有效执行路径沉淀为记忆？（输入 `/tmem` 或 `/remember [备注]` 保存）*\n" +
              "</tmem_milestone_reached>"
          );
        }
      }
    }

    const breakerMsg = recordToolOutcomeForLoop(state, {
      hasFailure,
      errorSig: sig,
      mutatedFiles: mutatedThisStep,
      verifyPassed,
    });
    if (breakerMsg) {
      additionalBlocks.push(breakerMsg);
      applySessionTraceFeedback(store, state, "contradict");
    }

    saveRuntimeState(workspaceRoot, state);
    if (additionalBlocks.length > 0) {
      return { additionalContext: additionalBlocks.join("\n\n") };
    }
    return {};
  }

  return {};
}
