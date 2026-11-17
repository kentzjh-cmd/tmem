/**
 * P1.3 Retrieval Benchmark (Node.js ESM).
 *
 * Evaluates deterministic memory retrieval using genuine user issue queries
 * (never playbook's self-generated intent titles):
 * - Reports Recall@1, Recall@3, and Mean Reciprocal Rank (MRR)
 * - Evaluates False Positive Rate (mis-retrieval rate) on unrelated tasks
 */

import { mkdirSync, mkdtempSync, readdirSync, rmSync, writeFileSync } from "node:fs";
import os from "node:os";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { DshAdapter, SweBenchAdapter } from "../tmem/adapters/index.mjs";
import { BackwardTraceSlicer } from "../tmem/slicer.mjs";
import { ProjectMemoryStore } from "../tmem/store.mjs";

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const WORKSPACE_ROOT = path.resolve(__dirname, "..");

export const NEGATIVE_HOLDOUT_QUERIES = [
  "How to prepare authentic Italian carbonara with guanciale and pecorino",
  "Deploy high-availability Kubernetes cluster on AWS using Terraform and EKS",
  "Resolve lifetime elision and borrow checker conflict in Rust async trait",
  "Configure Nginx reverse proxy with Let's Encrypt automated TLS certificates",
  "Train a transformer language model with deepspeed pipeline parallelism",
  "Docker compose configuration for PostgreSQL read-write replica pool",
];

export function runRetrievalBenchmark(dataDir = null) {
  const targetDataDir = path.resolve(
    dataDir || path.join(WORKSPACE_ROOT, "benchmarks", "data", "online_20")
  );
  const files = readdirSync(targetDataDir)
    .filter((f) => f.endsWith(".traj"))
    .sort()
    .map((f) => path.join(targetDataDir, f));

  const tempDir = mkdtempSync(path.join(os.tmpdir(), "tmem_retrieval_eval_"));
  try {
    const store = new ProjectMemoryStore(tempDir);
    const adapter = new SweBenchAdapter();
    const slicer = new BackwardTraceSlicer();

    const indexedPlaybooks = [];
    const testQueries = [];

    for (const fpath of files) {
      const session = adapter.parseTranscript(fpath, tempDir);
      const pb = slicer.sliceSession(session, false);
      if (!pb) continue;
      store.savePlaybook(pb, { targetScope: "project" });
      indexedPlaybooks.push(pb);

      const issueText = session.user_intents[0] || "";
      testQueries.push({
        instance_id: pb.trace_id,
        dataset_instance: session.session_id,
        raw_issue_query: issueText.slice(0, 200).trim(),
        target_files: [...pb.touched_files],
      });
    }

    const dshAdapter = new DshAdapter();
    const benchDataDir = path.join(WORKSPACE_ROOT, "benchmarks", "data");
    const dshFiles = readdirSync(benchDataDir)
      .filter((f) => f.startsWith("dsh_") && f.endsWith(".jsonl"))
      .sort()
      .map((f) => path.join(benchDataDir, f));

    for (const dshF of dshFiles) {
      const session = dshAdapter.parseTranscript(dshF, tempDir);
      const pb = slicer.sliceSession(session, false);
      if (pb) {
        store.savePlaybook(pb, { targetScope: "project" });
        indexedPlaybooks.push(pb);
        const issueText = session.user_intents[0] || "";
        testQueries.push({
          instance_id: pb.trace_id,
          dataset_instance: session.session_id,
          raw_issue_query: issueText.slice(0, 200).trim(),
          target_files: [...pb.touched_files],
        });
      }
    }

    const totalQueries = testQueries.length;
    let hitsAt1 = 0;
    let hitsAt3 = 0;
    const reciprocalRanks = [];
    const perQueryResults = [];

    for (const q of testQueries) {
      const evaluated = store.cascadingLookup({
        queryText: q.raw_issue_query,
        limit: 5,
      });
      const rankedIds = evaluated.map((ev) => ev.playbook.trace_id);
      const idx = rankedIds.indexOf(q.instance_id);
      const rank = idx >= 0 ? idx + 1 : 999;

      if (rank === 1) hitsAt1 += 1;
      if (rank <= 3) hitsAt3 += 1;
      reciprocalRanks.push(rank <= 10 ? 1.0 / rank : 0.0);

      perQueryResults.push({
        dataset_instance: q.dataset_instance,
        target_trace_id: q.instance_id,
        query_snippet: `${q.raw_issue_query.slice(0, 80)}...`,
        retrieval_rank: rank <= 10 ? rank : ">10",
        top_retrieved: rankedIds[0] || "NONE",
      });
    }

    const meanRr = totalQueries
      ? reciprocalRanks.reduce((a, b) => a + b, 0) / totalQueries
      : 0.0;
    const recallAt1 = totalQueries ? (hitsAt1 / totalQueries) * 100 : 0.0;
    const recallAt3 = totalQueries ? (hitsAt3 / totalQueries) * 100 : 0.0;

    let falsePositives = 0;
    const negativeResults = [];
    for (const negQ of NEGATIVE_HOLDOUT_QUERIES) {
      const res = store.cascadingLookup({ queryText: negQ, limit: 3 });
      const isFp = res.some((r) => r.l0_valid || r.playbook.confidence > 0.6);
      if (isFp) falsePositives += 1;
      negativeResults.push({
        query: negQ,
        retrieved_count: res.length,
        false_positive: isFp,
      });
    }

    const fpRate = (falsePositives / NEGATIVE_HOLDOUT_QUERIES.length) * 100;

    return {
      total_indexed_playbooks: indexedPlaybooks.length,
      total_eval_queries: totalQueries,
      metrics: {
        recall_at_1: Math.round(recallAt1 * 10) / 10,
        recall_at_3: Math.round(recallAt3 * 10) / 10,
        mrr: Math.round(meanRr * 1000) / 1000,
        negative_holdout_queries: NEGATIVE_HOLDOUT_QUERIES.length,
        false_positive_rate: Math.round(fpRate * 10) / 10,
      },
      queries: perQueryResults,
      negative_queries: negativeResults,
    };
  } finally {
    rmSync(tempDir, { recursive: true, force: true });
  }
}

export function formatMarkdownReport(data) {
  const m = data.metrics;
  const md = [];
  md.push("# 🔍 P1.3 独立任务检索评测报告 (Independent Issue Retrieval Benchmark)");
  md.push("");
  md.push(
    "> **方法学说明**：查询完全使用下游任务的 **原始 Issue / 需求文本**，绝不使用 Playbook 自编的意图标题。"
  );
  md.push("");
  md.push(`- **索引记忆总量**: ${data.total_indexed_playbooks} 条真实工程 Playbook`);
  md.push(`- **测试查询总量**: ${data.total_eval_queries} 条独立任务 Issue 原文`);
  md.push(`- **Top-1 召回率 (Recall@1)**: **${m.recall_at_1}%**`);
  md.push(`- **Top-3 召回率 (Recall@3)**: **${m.recall_at_3}%**`);
  md.push(`- **平均倒数排名 (MRR)**: **${m.mrr}**`);
  md.push(
    `- **不相关任务误召回率 (False Positive Rate)**: **${m.false_positive_rate}%** (基于 ${m.negative_holdout_queries} 条跨领域负样本)`
  );
  md.push("");
  md.push("### 检索结果明细");
  md.push("");
  md.push("| 评测任务实例 | 目标 Trace ID | 检索排名 | 首选召回结果 | Issue 查询摘要 |");
  md.push("| :--- | :--- | :---: | :--- | :--- |");
  for (const q of data.queries) {
    const statusIcon =
      q.retrieval_rank === 1
        ? "🥇"
        : q.retrieval_rank === 2 || q.retrieval_rank === 3
          ? "🥈"
          : "❌";
    md.push(
      `| \`${q.dataset_instance}\` | \`${q.target_trace_id}\` | ${statusIcon} \`${q.retrieval_rank}\` | \`${q.top_retrieved}\` | ${q.query_snippet} |`
    );
  }
  md.push("");
  return md.join("\n");
}

function main(argv = process.argv.slice(2)) {
  const outputJson = argv.includes("--json");
  let outputFile = null;
  const outIdx = argv.indexOf("--output");
  if (outIdx >= 0 && outIdx + 1 < argv.length) {
    outputFile = argv[outIdx + 1];
  }

  const data = runRetrievalBenchmark();
  const runsDir = path.join(WORKSPACE_ROOT, "benchmarks", "runs");
  mkdirSync(runsDir, { recursive: true });
  writeFileSync(
    path.join(runsDir, "retrieval_report.json"),
    JSON.stringify(data, null, 2),
    "utf-8"
  );

  if (outputJson) {
    console.log(JSON.stringify(data, null, 2));
  } else {
    const md = formatMarkdownReport(data);
    console.log(md);
    if (outputFile) {
      writeFileSync(outputFile, md, "utf-8");
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
