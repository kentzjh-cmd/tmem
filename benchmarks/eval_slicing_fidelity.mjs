/**
 * P1.1 Slicing Fidelity Evaluation Benchmark (Node.js ESM).
 *
 * Compares tmem backward trace slicing against baselines on ground truth annotated trajectories:
 * - B0: Trivial baseline (git diff modified files + last passing verification command)
 * - B1: tmem Deterministic Backward Slicer
 * - B2: Oracle / LLM one-shot upper bound
 */

import { existsSync, mkdirSync, readFileSync, writeFileSync } from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { DshAdapter, SweBenchAdapter } from "../tmem/adapters/index.mjs";
import { ActionKind } from "../tmem/models.mjs";
import { BackwardTraceSlicer } from "../tmem/slicer.mjs";

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const WORKSPACE_ROOT = path.resolve(__dirname, "..");

const B0_IGNORED_PREFIXES = ["rm ", "goto", "scroll", "exit", "submit", "cd "];

export function evaluateBaselines(goldPath) {
  const resolvedGold = path.resolve(goldPath);
  const goldEntries = JSON.parse(readFileSync(resolvedGold, "utf-8"));

  const adapterSwe = new SweBenchAdapter();
  const adapterDsh = new DshAdapter();
  const slicer = new BackwardTraceSlicer();

  const b0List = [];
  const b1List = [];
  const b2List = [];
  const perCaseResults = [];

  for (const entry of goldEntries) {
    const iid = entry.instance_id;
    const goldSteps = new Set(entry.gold_step_indices);
    const fmt = entry.agent_format;
    const trajFile = path.join(WORKSPACE_ROOT, entry.trajectory_file);

    const adapter = fmt === "dsh" ? adapterDsh : adapterSwe;
    const session = adapter.parseTranscript(trajFile, WORKSPACE_ROOT);

    // Baseline 0: Only git diff permanent mutated files + last passed test
    const permFiles = new Set(entry.perm_target_files || []);
    const b0Steps = new Set();
    for (const a of session.actions) {
      if (a.kind === ActionKind.MUTATE && permFiles.has(a.target_file)) {
        b0Steps.add(a.step_idx);
      }
    }
    const passingTests = session.actions.filter(
      (a) =>
        a.kind === ActionKind.ENV_EXEC &&
        a.exit_code === 0 &&
        a.command &&
        !B0_IGNORED_PREFIXES.some((pfx) => a.command.startsWith(pfx))
    );
    if (passingTests.length > 0) {
      b0Steps.add(passingTests[passingTests.length - 1].step_idx);
    }

    // Baseline 1: tmem backward trace slicer
    const playbook = slicer.sliceSession(session, false);
    const b1Steps = new Set(playbook ? slicer.last_sliced_step_indices : []);

    // Baseline 2: Oracle gold standard
    const b2Steps = new Set(goldSteps);

    const score = (sSet, gSet) => {
      if (sSet.size === 0) return [0.0, 0.0, 0.0];
      const inter = [...sSet].filter((x) => gSet.has(x)).length;
      const p = inter / sSet.size;
      const r = gSet.size > 0 ? inter / gSet.size : 0.0;
      const f1 = p + r > 0 ? (2 * p * r) / (p + r) : 0.0;
      return [p, r, f1];
    };

    const [p0, r0, f0] = score(b0Steps, goldSteps);
    const [p1, r1, f1] = score(b1Steps, goldSteps);
    const [p2, r2, f2] = score(b2Steps, goldSteps);

    b0List.push({ p: p0, r: r0, f1: f0, steps: b0Steps.size });
    b1List.push({ p: p1, r: r1, f1: f1, steps: b1Steps.size });
    b2List.push({ p: p2, r: r2, f1: f2, steps: b2Steps.size });

    const r1d = (v) => Math.round(v * 1000) / 10;
    perCaseResults.push({
      instance_id: iid,
      raw_steps: session.actions.length,
      gold_steps: goldSteps.size,
      b0: { p: r1d(p0), r: r1d(r0), f1: r1d(f0), steps: b0Steps.size },
      b1: { p: r1d(p1), r: r1d(r1), f1: r1d(f1), steps: b1Steps.size },
      b2: { p: r1d(p2), r: r1d(r2), f1: r1d(f2), steps: b2Steps.size },
    });
  }

  const n = Math.max(goldEntries.length, 1);
  const aggregate = (statList) => ({
    mean_precision:
      Math.round((statList.reduce((s, x) => s + x.p, 0) / n) * 1000) / 10,
    mean_recall:
      Math.round((statList.reduce((s, x) => s + x.r, 0) / n) * 1000) / 10,
    mean_f1:
      Math.round((statList.reduce((s, x) => s + x.f1, 0) / n) * 1000) / 10,
    avg_steps:
      Math.round((statList.reduce((s, x) => s + x.steps, 0) / n) * 10) / 10,
  });

  return {
    total_instances: n,
    summary: {
      B0_trivial_git_diff: aggregate(b0List),
      B1_tmem_slicer: aggregate(b1List),
      B2_oracle_upper_bound: aggregate(b2List),
    },
    per_case: perCaseResults,
  };
}

export function formatReportMarkdown(data) {
  const md = [];
  md.push("# 🎯 P1.1 轨迹切片保真度评测报告 (Slicing Fidelity Benchmark)");
  md.push("");
  md.push(
    `- **评测样本量**: ${data.total_instances} 条轨迹 (覆盖 SWE-bench \`online_20\` 及 live \`dsh\` 会话)`
  );
  md.push("- **对比基线**:");
  md.push("  - **B0**: 平凡基线 (`git diff` 变更文件 + 最后一条通过测试命令)");
  md.push("  - **B1**: `tmem` 确定性后向切片器 (`l0_critical_path`)");
  md.push("  - **B2**: Oracle 上限参考 (人工标注金标准)");
  md.push("");
  md.push("### 基线对比总表");
  md.push("");
  md.push(
    "| 基线模型 / 策略 | Precision (查准率) | Recall (查全率) | F1-Score | 平均保留步数 | 策略特性分析 |"
  );
  md.push("| :--- | :--- | :--- | :--- | :--- | :--- |");

  const b0 = data.summary.B0_trivial_git_diff;
  const b1 = data.summary.B1_tmem_slicer;
  const b2 = data.summary.B2_oracle_upper_bound;

  md.push(
    `| **B0 (git diff + last test)** | **${b0.mean_precision}%** | ${b0.mean_recall}% | **${b0.mean_f1}%** | ${b0.avg_steps} 步 | 高查准、低查全（丢失定位所需的关键上下文读操作与环境安装） |`
  );
  md.push(
    `| **B1 (tmem Slicer)** | ${b1.mean_precision}% | **${b1.mean_recall}%** | ${b1.mean_f1}% | ${b1.avg_steps} 步 | 高查全、保留了定位所需的代码阅读与工具链步骤 |`
  );
  md.push(
    `| **B2 (Oracle Upper Bound)** | ${b2.mean_precision}% | ${b2.mean_recall}% | ${b2.mean_f1}% | ${b2.avg_steps} 步 | 人工标注理想上限 |`
  );
  md.push("");
  md.push("### 逐样本切片明细");
  md.push("");
  md.push("| 实例 ID | 原始步数 | 金标准步数 | B0 (P / R / F1) | B1 (P / R / F1) |");
  md.push("| :--- | :--- | :--- | :--- | :--- |");
  for (const r of data.per_case) {
    const b0Str = `${r.b0.p}% / ${r.b0.r}% / ${r.b0.f1}%`;
    const b1Str = `${r.b1.p}% / ${r.b1.r}% / ${r.b1.f1}%`;
    md.push(`| \`${r.instance_id}\` | ${r.raw_steps} | ${r.gold_steps} | ${b0Str} | ${b1Str} |`);
  }
  md.push("");
  return md.join("\n");
}

function main(argv = process.argv.slice(2)) {
  let goldPath = path.join(WORKSPACE_ROOT, "benchmarks", "gold", "gold_annotations_50.json");
  let outputJson = false;
  let outputFile = null;

  for (let i = 0; i < argv.length; i++) {
    const tok = argv[i];
    if (tok === "--gold-path" && i + 1 < argv.length) {
      goldPath = argv[++i];
    } else if (tok === "--json") {
      outputJson = true;
    } else if (tok === "--output" && i + 1 < argv.length) {
      outputFile = argv[++i];
    }
  }

  const resolvedGold = path.resolve(goldPath);
  if (!existsSync(resolvedGold)) {
    console.error(`Error: Gold annotations not found at ${resolvedGold}`);
    return 1;
  }

  const data = evaluateBaselines(resolvedGold);
  if (path.basename(resolvedGold) === "gold_annotations_50.json") {
    const runsDir = path.join(WORKSPACE_ROOT, "benchmarks", "runs");
    mkdirSync(runsDir, { recursive: true });
    writeFileSync(
      path.join(runsDir, "slicing_fidelity_report.json"),
      JSON.stringify(data, null, 2),
      "utf-8"
    );
  }

  if (outputJson) {
    console.log(JSON.stringify(data, null, 2));
  } else {
    const mdText = formatReportMarkdown(data);
    console.log(mdText);
    if (outputFile) {
      writeFileSync(outputFile, mdText, "utf-8");
    }
  }
  return 0;
}

const isEntrypoint =
  process.argv[1] &&
  path.resolve(process.argv[1]) === path.resolve(fileURLToPath(import.meta.url));
if (isEntrypoint) {
  process.exitCode = main();
}
