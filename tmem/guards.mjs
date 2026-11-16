/**
 * Speculative Execution Guard Evaluator & 3-Tier Progressive Degradation Engine.
 *
 * Validates L0 guards in <5ms with zero LLM tokens:
 * - Full Pass (L0 valid): Injects concrete L0 file paths + L1 co-change topology + L2 causal repairs.
 * - Partial/Failed Guard (Environment/Codebase drifted): Flushes stale L0 paths and degrades gracefully
 *   to L1 (co-change contract) + L2 (error->fix root cause invariant) so the agent is never misled.
 */

import { existsSync, readFileSync } from "node:fs";
import path from "node:path";
import { checkTsSymbolPresent, parsePythonStructure } from "./ast_parser.mjs";

function escapeRegExp(s) {
  return String(s).replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
}

function checkSymbolInPythonAst(content, expectedSymbol) {
  const { definitions, assignments } = parsePythonStructure(content);
  const target = (expectedSymbol || "").trim();
  if (!target) return false;
  for (const d of definitions) {
    if (d.qualified === target) return true;
    if (!target.includes(".") && d.leaf === target) return true;
  }
  for (const a of assignments) {
    if (a.qualified === target) return true;
    if (!target.includes(".") && a.name === target) return true;
  }
  return false;
}

export function checkSymbolInAst(content, filePath, expectedSymbol) {
  if (filePath && filePath.toLowerCase().endsWith(".py")) {
    return checkSymbolInPythonAst(content, expectedSymbol);
  }
  const tsRes = checkTsSymbolPresent(content, filePath, expectedSymbol);
  if (tsRes !== null) {
    return tsRes;
  }
  const re = new RegExp(`\\b${escapeRegExp(expectedSymbol)}\\b`);
  return re.test(content);
}

export function evaluateGuards(playbook, workspaceRoot) {
  const ws = path.resolve(workspaceRoot);
  const failed = [];
  const checkedFiles = new Set();

  for (const g of playbook.guards || []) {
    checkedFiles.add(g.target);
    const targetPath = path.join(ws, g.target);
    if (g.probe_type === "file_exists") {
      if (!existsSync(targetPath)) {
        failed.push(`missing_file:${g.target}`);
      }
    } else if (g.probe_type === "symbol_present") {
      if (!existsSync(targetPath)) {
        failed.push(`missing_file:${g.target}`);
      } else {
        try {
          const content = readFileSync(targetPath, "utf-8");
          const present = checkSymbolInAst(content, g.target, g.expected);
          if (!present) {
            failed.push(`missing_symbol:${g.target}#${g.expected}`);
          }
        } catch (_err) {
          failed.push(`unreadable:${g.target}`);
        }
      }
    } else if (g.probe_type === "file_contains") {
      if (!existsSync(targetPath)) {
        failed.push(`missing_file:${g.target}`);
      } else {
        try {
          const content = readFileSync(targetPath, "utf-8");
          if (!content.includes(g.expected)) {
            failed.push(`missing_substring:${g.target}`);
          }
        } catch (_err) {
          failed.push(`unreadable:${g.target}`);
        }
      }
    }
  }

  const dedupFailed = Array.from(new Set(failed));
  const failedTargets = new Set(
    dedupFailed
      .filter((f) => f.includes(":"))
      .map((f) => f.slice(f.indexOf(":") + 1).split("#")[0])
  );
  const passedTargets = Array.from(checkedFiles)
    .filter((f) => !failedTargets.has(f))
    .sort();

  const hasRepairs = Boolean(playbook.l2_causal_repairs && playbook.l2_causal_repairs.length);
  if (failed.length === 0 && (playbook.guards || []).length > 0) {
    if (playbook.status === "stale" || playbook.status === "invalidated" || playbook.confidence < 0.3) {
      const reason =
        playbook.status === "stale" || playbook.status === "invalidated"
          ? `status:${playbook.status}`
          : `low_confidence:${Number(playbook.confidence).toFixed(2)}`;
      const tier = hasRepairs ? "L2_INVARIANT" : "L1_TOPOLOGY";
      return {
        playbook,
        l0_valid: false,
        effective_tier: tier,
        failed_guards: [reason],
        passed_files: passedTargets,
      };
    }
    return {
      playbook,
      l0_valid: true,
      effective_tier: "L0_FULL",
      failed_guards: [],
      passed_files: passedTargets,
    };
  }

  const tier = hasRepairs ? "L2_INVARIANT" : "L1_TOPOLOGY";
  return {
    playbook,
    l0_valid: false,
    effective_tier: tier,
    failed_guards: dedupFailed,
    passed_files: passedTargets,
  };
}

export function formatInjectedContext(evaluatedList) {
  if (!evaluatedList || evaluatedList.length === 0) return "";

  const lines = [
    "<tmem_speculative_memory>",
    "Deterministic execution traces matched from prior verified sessions:",
  ];

  for (const ev of evaluatedList.slice(0, 3)) {
    const pb = ev.playbook;
    const sessIds = Array.isArray(pb.source_session_ids) ? pb.source_session_ids : [];
    const sessTag = sessIds.length ? ` | Session(s): ${sessIds.slice(-3).join(", ")}` : "";
    lines.push(
      `\n[Trace ${pb.trace_id}${sessTag} | Scope: ${String(pb.scope || "project").toUpperCase()} | Tier: ${ev.effective_tier} | Confidence: ${Number(pb.confidence).toFixed(2)}]`
    );
    lines.push(`- Historical Intent: ${pb.problem_intent}`);
    if (pb.semantic_summary) {
      lines.push(`- Semantic Summary: ${pb.semantic_summary}`);
    }
    if (pb.root_cause) {
      lines.push(`- Root Cause: ${pb.root_cause}`);
    }

    if (ev.l0_valid && pb.l0_critical_path && pb.l0_critical_path.length > 0) {
      const stepsDesc = [];
      for (const s of pb.l0_critical_path.slice(0, 6)) {
        const action = s.action;
        const target = s.target || s.command;
        const diffSum = s.diff_summary;
        const rationale = s.rationale;
        const extra = diffSum ? ` (${diffSum})` : rationale ? ` — ${rationale}` : "";
        stepsDesc.push(`${action}(${target})${extra}`);
      }
      lines.push(`- Verified L0 Critical Path (Guards Passed): ${stepsDesc.join(" -> ")}`);
    } else {
      if (ev.failed_guards && ev.failed_guards.length > 0) {
        lines.push(
          `- Note: L0 concrete paths drifted (${ev.failed_guards.join(", ")}); degraded to structural/causal invariants:`
        );
      }
      if (ev.passed_files && ev.passed_files.length > 0 && pb.l0_critical_path) {
        const passedSet = new Set(ev.passed_files);
        const surviving = pb.l0_critical_path.filter(
          (s) => !s.target || passedSet.has(s.target)
        );
        if (surviving.length > 0) {
          const stepsDesc = surviving
            .slice(0, 4)
            .map((s) => `${s.action}(${s.target || s.command})`);
          lines.push(`- Partial L0 Path (Drifted files hidden): ${stepsDesc.join(" -> ")}`);
        }
      }
    }

    if (pb.l1_cochange_topology && pb.l1_cochange_topology.length > 0 && pb.scope === "project") {
      for (const topo of pb.l1_cochange_topology) {
        lines.push(`- L1 Topology: ${topo}`);
      }
    }
    if (pb.l1_verification_cmds && pb.l1_verification_cmds.length > 0 && ev.l0_valid) {
      lines.push(
        `- Verification Command: \`${pb.l1_verification_cmds[pb.l1_verification_cmds.length - 1]}\``
      );
    }

    for (const rep of (pb.l2_causal_repairs || []).slice(0, 2)) {
      lines.push(
        `- L2 Causal Repair: On \`${rep.error_signature}\` => \`${rep.repair_action}\` (verify: \`${rep.verification_cmd}\`)`
      );
    }

    if (pb.raw_trace_path) {
      const sessHint = sessIds.length ? ` | session(s): ${sessIds.slice(-3).join(", ")}` : "";
      lines.push(
        `- Forensic Audit: Raw trace archived at \`${pb.raw_trace_path}\`${sessHint} (inspect: \`tmem inspect ${pb.trace_id} --level raw\`)`
      );
    }
  }

  lines.push("</tmem_speculative_memory>");
  return lines.join("\n");
}
