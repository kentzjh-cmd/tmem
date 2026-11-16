/**
 * Benchmark Evaluation Suite for tmem Deterministic Execution Memory (Node.js ESM).
 */

import { existsSync, mkdtempSync, readdirSync, rmSync } from "node:fs";
import os from "node:os";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { getAdapter } from "./adapters/index.mjs";
import { RawTraceStore, formatRawTraceReplay } from "./raw_storage.mjs";
import { BackwardTraceSlicer } from "./slicer.mjs";
import { ProjectMemoryStore } from "./store.mjs";

const SELF_DIR = path.dirname(fileURLToPath(import.meta.url));

export class BenchmarkEvaluator {
  constructor(dataDir = null) {
    this.data_dir = path.resolve(
      dataDir || path.join(SELF_DIR, "..", "benchmarks", "data")
    );
  }

  runEvaluation(_verbose = false) {
    if (!existsSync(this.data_dir)) {
      throw new Error(`Benchmark data directory not found: ${this.data_dir}`);
    }

    const files = readdirSync(this.data_dir)
      .filter((name) => {
        const ext = path.extname(name).toLowerCase();
        return [".json", ".traj", ".jsonl"].includes(ext);
      })
      .sort()
      .map((name) => path.join(this.data_dir, name));

    if (files.length === 0) {
      throw new Error(`No trajectory files found in ${this.data_dir}`);
    }

    const tempDir = mkdtempSync(path.join(os.tmpdir(), "tmem_bench_"));
    try {
      const store = new ProjectMemoryStore(tempDir);
      const results = [];
      const progressiveResults = [];

      for (const fpath of files) {
        const [res, progRes] = this._evaluateSingleFile(fpath, tempDir, store);
        results.push(res);
        progressiveResults.push(progRes);
      }

      const total = results.length;
      const totalRaw = results.reduce((s, r) => s + r.raw_steps, 0);
      const totalSliced = results.reduce((s, r) => s + r.sliced_steps, 0);
      const maxLen = total ? Math.max(...results.map((r) => r.raw_steps)) : 0;
      const minLen = total ? Math.min(...results.map((r) => r.raw_steps)) : 0;
      const avgCr = total
        ? results.reduce((s, r) => s + r.compression_ratio, 0) / total
        : 0;
      const avgSliceLat = total
        ? results.reduce((s, r) => s + r.slicing_latency_ms, 0) / total
        : 0;
      const totalL2 = results.reduce((s, r) => s + r.l2_causal_repairs_count, 0);
      const totalRawChars = results.reduce((s, r) => s + r.raw_chars, 0);
      const totalSlicedChars = results.reduce((s, r) => s + r.sliced_chars, 0);
      const totalRawTok = results.reduce((s, r) => s + r.raw_tokens_est, 0);
      const totalSlicedTok = results.reduce((s, r) => s + r.sliced_tokens_est, 0);

      const allPassed =
        total > 0 &&
        avgSliceLat < 100.0 &&
        results.every((r) => r.sliced_steps > 0);

      const tSync0 = performance.now();
      store.syncMemoriesMarkdown();
      const syncLatencyMs = performance.now() - tSync0;

      const totalProg = progressiveResults.length;
      const avgRawChars = totalProg
        ? progressiveResults.reduce((s, p) => s + p.raw_json_chars, 0) / totalProg
        : 0;
      const avgPbChars = totalProg
        ? progressiveResults.reduce((s, p) => s + p.playbook_json_chars, 0) / totalProg
        : 0;
      const rawFid = totalProg
        ? (progressiveResults.filter((p) => p.raw_storage_verified).length / totalProg) *
          100.0
        : 100.0;

      return {
        total_trajectories: total,
        total_raw_steps: totalRaw,
        total_sliced_steps: totalSliced,
        max_trajectory_length: maxLen,
        min_trajectory_length: minLen,
        avg_compression_ratio: Math.round(avgCr * 100) / 100,
        avg_slicing_latency_ms: Math.round(avgSliceLat * 100) / 100,
        total_raw_chars: totalRawChars,
        total_sliced_chars: totalSlicedChars,
        total_raw_tokens_est: totalRawTok,
        total_sliced_tokens_est: totalSlicedTok,
        total_l2_invariants_extracted: totalL2,
        all_passed: allPassed,
        results,
        progressive: {
          total_instances: totalProg,
          avg_raw_json_chars: Math.round(avgRawChars),
          avg_playbook_json_chars: Math.round(avgPbChars),
          raw_fidelity_rate: Math.round(rawFid * 100) / 100,
          living_index_latency_ms: Math.round(syncLatencyMs * 100) / 100,
          results: progressiveResults,
        },
      };
    } finally {
      rmSync(tempDir, { recursive: true, force: true });
    }
  }

  _evaluateSingleFile(filePath, tempWorkspace, store) {
    const stem = path.parse(filePath).name.toLowerCase();
    let agentName = "swe_bench";
    if (stem.includes("dsh")) agentName = "dsh";
    else if (stem.includes("atif")) agentName = "atif";

    const adapter = getAdapter(agentName);
    const session = adapter.parseTranscript(filePath, tempWorkspace);

    const t0 = performance.now();
    const slicer = new BackwardTraceSlicer();
    const playbook = slicer.sliceSession(session, false);
    const slicingMs = performance.now() - t0;

    if (!playbook) {
      throw new Error(`Slicer returned null for benchmark trajectory ${filePath}`);
    }

    const rawCount = Math.max(session.actions.length, 1);
    const slicedCount = playbook.l0_critical_path.length;
    const cr = Math.max(0.0, (1.0 - slicedCount / rawCount) * 100.0);

    const rawChars = session.actions.reduce(
      (s, a) =>
        s +
        String(a.command || "").length +
        String(a.target_file || "").length +
        String(a.output_snippet || "").length,
      0
    );
    const slicedChars = playbook.l0_critical_path.reduce(
      (s, st) => s + String(st.command || "").length + String(st.target || "").length,
      0
    );
    const rawTok = Math.max(1, Math.floor(rawChars / 4));
    const slicedTok = Math.max(1, Math.floor(slicedChars / 4));

    store.savePlaybook(playbook, { targetScope: "project" });

    const res = {
      dataset_name: path.parse(filePath).name,
      instance_id: session.session_id,
      agent_format: agentName,
      raw_steps: rawCount,
      sliced_steps: slicedCount,
      compression_ratio: Math.round(cr * 10) / 10,
      raw_chars: rawChars,
      sliced_chars: slicedChars,
      raw_tokens_est: rawTok,
      sliced_tokens_est: slicedTok,
      slicing_latency_ms: Math.round(slicingMs * 100) / 100,
      l1_files_count: playbook.touched_files.length,
      l2_causal_repairs_count: playbook.l2_causal_repairs.length,
      passed_verification: playbook.l1_verification_cmds.length > 0,
      details: {
        l0_steps: playbook.l0_critical_path,
        l1_topology: playbook.l1_cochange_topology,
        l2_repairs: playbook.l2_causal_repairs.map((r) => r.toDict()),
      },
    };

    const rawTr = slicer.buildRawTrace(session, playbook);
    const rawStore = new RawTraceStore(tempWorkspace);
    rawStore.saveRawTrace(rawTr);
    const loadedRaw = rawStore.loadRawTrace(playbook.trace_id);
    const rawVerified =
      loadedRaw !== null && loadedRaw.actions.length === session.actions.length;
    const replayStr = formatRawTraceReplay(rawTr);
    const replayOk = replayStr.length > 50;

    const pbJsonChars = JSON.stringify(playbook.toDict()).length;
    const rawJsonChars = JSON.stringify(rawTr.toDict()).length;

    const progRes = {
      instance_id: session.session_id,
      dataset_name: path.parse(filePath).name,
      raw_json_chars: rawJsonChars,
      playbook_json_chars: pbJsonChars,
      index_chars: 120,
      raw_storage_verified: rawVerified,
      replay_rendered: replayOk,
    };

    return [res, progRes];
  }
}

export function formatMarkdownReport(summary) {
  const md = [];
  md.push("## 📊 轨迹切片与特征描述评测 (Trajectory Slicing & Profiling)");
  md.push("");
  md.push(
    `- **评测轨迹总量**: ${summary.total_trajectories} 条真实 Agent 轨迹 (原始交互总轮次: ${summary.total_raw_steps} 步，长度区间: ${summary.min_trajectory_length} ~ ${summary.max_trajectory_length} 步)`
  );
  md.push(
    `- **切片后 L0 步骤总计**: ${summary.total_sliced_steps} 步 (平均精简率: ${summary.avg_compression_ratio}%)`
  );
  md.push(
    `- **实测字符数**: ${summary.total_raw_chars} 字符 (原始) -> ${summary.total_sliced_chars} 字符 (切片后)`
  );
  md.push(
    `- **预估 Token 数 (chars // 4)**: ~${summary.total_raw_tokens_est} tok (原始) -> ~${summary.total_sliced_tokens_est} tok (切片后)`
  );
  md.push(
    `- **提炼高熵 L2 因果修复对**: **${summary.total_l2_invariants_extracted} 组** (经堆栈交集与源码约束过滤)`
  );
  md.push(
    `- **平均切片耗时**: **${summary.avg_slicing_latency_ms} ms** (纯本地确定性运行时，零 LLM Token 耗时)`
  );
  md.push(
    `- **基准校验状态**: **${summary.all_passed ? "✅ PASSED" : "❌ FAILED"}**`
  );
  md.push("");
  md.push(
    "> **方法学说明**：切片器以落盘变更与最终通过的测试为锚点，剪除探索性读操作与无关命令。本报告仅展示客观描述性数字（原始步数、L0 步数、实测字符数/Token 估算及切片耗时），不报告自证明的满分推断指标。"
  );
  md.push("");
  md.push("### 轨迹切片描述性统计明细");
  md.push("");
  md.push(
    "| 实例 / 轨迹来源 | 格式 | 原始步数 | L0 步数 | 精简率 | 原始字符数 | 切片字符数 | 预估 Token (原/切) | 协同文件 (L1) | 高熵修复对 (L2) | 验证命令提取 | 切片耗时 |"
  );
  md.push("|---|---|---|---|---|---|---|---|---|---|:---:|---|");
  for (const r of summary.results) {
    md.push(
      `| \`${r.instance_id}\` | \`${r.agent_format}\` | ${r.raw_steps} | ${r.sliced_steps} | ` +
        `**${r.compression_ratio}%** | ${r.raw_chars} | ${r.sliced_chars} | ` +
        `~${r.raw_tokens_est} / ~${r.sliced_tokens_est} | ${r.l1_files_count} 个文件 | ` +
        `${r.l2_causal_repairs_count} 组 | ${r.passed_verification ? "✅" : "-"} | ${r.slicing_latency_ms}ms |`
    );
  }
  md.push("");

  if (summary.progressive) {
    const prog = summary.progressive;
    md.push("## ⚡ 分级渐进披露存储体积描述 (Progressive Disclosure Footprint)");
    md.push("");
    md.push(`- **评测记忆条目**: ${prog.total_instances} 条轨迹`);
    md.push(`- **平均 Playbook JSON 体积**: ~${prog.avg_playbook_json_chars} 字符`);
    md.push(
      `- **平均 Raw Trace JSON 体积**: ~${prog.avg_raw_json_chars} 字符 (包含完整 tool_calls、输出与 Git Diff)`
    );
    md.push(`- **原始轨迹存储校验通过率**: **${prog.raw_fidelity_rate}%**`);
    md.push(`- **活文档索引原子同步耗时**: **${prog.living_index_latency_ms} ms**`);
    md.push("");
    md.push("### 分级存储明细");
    md.push("");
    md.push("| 实例 / 轨迹 | Raw Trace JSON 字符 | Playbook JSON 字符 | 存储校验 | 回放渲染 |");
    md.push("|---|---|---|---|---|");
    for (const p of prog.results) {
      md.push(
        `| \`${p.instance_id}\` | ${p.raw_json_chars} 字符 | ${p.playbook_json_chars} 字符 | ` +
          `${p.raw_storage_verified ? "✅ 通过" : "❌ 失败"} | ` +
          `${p.replay_rendered ? "✅ 通过" : "❌ 失败"} |`
      );
    }
    md.push("");
  }

  return md.join("\n");
}
