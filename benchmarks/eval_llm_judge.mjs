/**
 * Blinded 50-Trajectory Pairwise LLM Judge Reproduction & Verification Harness (Node.js ESM).
 *
 * Provides two reproducible workflows with zero external dependencies:
 * 1. Verification / Aggregation (default, offline):
 *    Validates `benchmarks/runs/llm_judge_50_report.json` against `gold_annotations_50.json`,
 *    recomputes pairwise win counts and mean 1-5 rubric scores from the 50 per-case records,
 *    and confirms consistency with the committed report.
 * 2. Live Blinded A/B Judge Execution (`--run-judge`, requires `OPENAI_API_KEY`):
 *    Deterministically compiles live B0 vs B1 slices, randomly shuffles Option A / Option B
 *    per case (seeded), calls an OpenAI-compatible LLM judge, and aggregates a fresh report.
 */

import { mkdirSync, readFileSync, writeFileSync } from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { DshAdapter, SweBenchAdapter } from "../tmem/adapters/index.mjs";
import { ActionKind } from "../tmem/models.mjs";
import { BackwardTraceSlicer } from "../tmem/slicer.mjs";

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const WORKSPACE_ROOT = path.resolve(__dirname, "..");

const JUDGE_SYSTEM_PROMPT =
  "You are a strict engineering trajectory evaluator. Given a user problem intent, " +
  "the raw step count, and two anonymized sliced step lists (Option A and Option B), " +
  "score each option from 1 to 5 on completeness, precision_denoising, temporal_causality, " +
  "and overall, and pick a winner ('Option A' or 'Option B'). Respond with strict JSON.";

const B0_IGNORED_PREFIXES = ["rm ", "goto", "scroll", "exit", "submit", "cd "];

function createMulberry32(seed) {
  let a = seed >>> 0;
  return function nextRand() {
    a |= 0;
    a = (a + 0x6d2b79f5) | 0;
    let t = Math.imul(a ^ (a >>> 15), 1 | a);
    t = (t + Math.imul(t ^ (t >>> 7), 61 | t)) ^ t;
    return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
  };
}

function formatStepBrief(action) {
  return {
    step: action.step_idx,
    kind: String(action.kind),
    target_file: action.target_file,
    command: action.command,
    exit_code: action.exit_code,
  };
}

export function buildBlindedJudgePackets(goldPath = null, seed = 42) {
  const targetGold = path.resolve(
    goldPath || path.join(WORKSPACE_ROOT, "benchmarks", "gold", "gold_annotations_50.json")
  );
  const entries = JSON.parse(readFileSync(targetGold, "utf-8"));

  const rand = createMulberry32(seed);
  const swe = new SweBenchAdapter();
  const dsh = new DshAdapter();
  const slicer = new BackwardTraceSlicer();
  const packets = [];

  for (const entry of entries) {
    const trajPath = path.join(WORKSPACE_ROOT, entry.trajectory_file);
    const adapter = entry.agent_format === "dsh" ? dsh : swe;
    const session = adapter.parseTranscript(trajPath, WORKSPACE_ROOT);

    const permFiles = new Set(entry.perm_target_files || []);
    const b0Indices = new Set();
    for (const a of session.actions) {
      if (a.kind === ActionKind.MUTATE && permFiles.has(a.target_file)) {
        b0Indices.add(a.step_idx);
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
      b0Indices.add(passingTests[passingTests.length - 1].step_idx);
    }

    const pb = slicer.sliceSession(session, false);
    const b1Indices = new Set(pb ? slicer.last_sliced_step_indices : []);

    const actionByStep = new Map();
    for (const a of session.actions) {
      actionByStep.set(a.step_idx, a);
    }

    const sortedB0 = Array.from(b0Indices).sort((a, b) => a - b);
    const sortedB1 = Array.from(b1Indices).sort((a, b) => a - b);
    const b0Steps = sortedB0
      .filter((i) => actionByStep.has(i))
      .map((i) => formatStepBrief(actionByStep.get(i)));
    const b1Steps = sortedB1
      .filter((i) => actionByStep.has(i))
      .map((i) => formatStepBrief(actionByStep.get(i)));

    const b1IsOptionA = rand() < 0.5;
    const [optionA, optionB] = b1IsOptionA ? [b1Steps, b0Steps] : [b0Steps, b1Steps];
    packets.push({
      instance_id: entry.instance_id,
      raw_steps: session.actions.length,
      intent: (session.user_intents[0] || "").slice(0, 300),
      option_a_label: b1IsOptionA ? "B1_tmem" : "B0_baseline",
      option_b_label: b1IsOptionA ? "B0_baseline" : "B1_tmem",
      option_a: optionA,
      option_b: optionB,
      b0_step_indices: sortedB0,
      b1_step_indices: sortedB1,
    });
  }
  return packets;
}

export function aggregateJudgeCases(cases) {
  const n = cases.length;
  if (n === 0) {
    throw new Error("No judge cases to aggregate");
  }
  const b1Wins = cases.filter((c) => c.winner === "B1_tmem").length;
  const b0Wins = cases.filter((c) => c.winner === "B0_baseline").length;
  const ties = n - b1Wins - b0Wins;

  const meanGroup = (key) => {
    const dims = ["completeness", "precision_denoising", "temporal_causality", "overall"];
    const out = {};
    for (const d of dims) {
      const sum = cases.reduce((s, c) => s + Number(c[key].llm_score[d]), 0);
      out[d] = Math.round((sum / n) * 100) / 100;
    }
    return out;
  };

  return {
    total_trajectories: n,
    pairwise_llm_judge: {
      B1_tmem_wins: b1Wins,
      B0_baseline_wins: b0Wins,
      ties,
      B1_win_rate_pct: Math.round((b1Wins / n) * 1000) / 10,
    },
    mean_llm_scores: {
      B0_baseline: meanGroup("b0"),
      B1_tmem: meanGroup("b1"),
    },
  };
}

export function verifyLlmJudgeReport(reportPath = null, goldPath = null) {
  const targetReport = path.resolve(
    reportPath || path.join(WORKSPACE_ROOT, "benchmarks", "runs", "llm_judge_50_report.json")
  );
  const targetGold = path.resolve(
    goldPath || path.join(WORKSPACE_ROOT, "benchmarks", "gold", "gold_annotations_50.json")
  );

  const report = JSON.parse(readFileSync(targetReport, "utf-8"));
  const goldEntries = JSON.parse(readFileSync(targetGold, "utf-8"));
  const goldIds = new Set(goldEntries.map((e) => e.instance_id));

  const cases = report.cases || [];
  const caseIds = new Set(cases.map((c) => c.instance_id));
  if (
    cases.length !== goldEntries.length ||
    caseIds.size !== goldIds.size ||
    [...goldIds].some((id) => !caseIds.has(id))
  ) {
    throw new Error(
      `Case mismatch between ${path.basename(targetReport)} (${cases.length}) and ${path.basename(
        targetGold
      )} (${goldEntries.length})`
    );
  }

  const recomputed = aggregateJudgeCases(cases);
  if (report.total_trajectories !== recomputed.total_trajectories) {
    throw new Error("total_trajectories mismatch in llm_judge_50_report.json");
  }
  if (
    JSON.stringify(report.pairwise_llm_judge) !==
    JSON.stringify(recomputed.pairwise_llm_judge)
  ) {
    throw new Error(
      `pairwise_llm_judge mismatch: saved=${JSON.stringify(
        report.pairwise_llm_judge
      )} != recomputed=${JSON.stringify(recomputed.pairwise_llm_judge)}`
    );
  }
  if (
    JSON.stringify(report.mean_llm_scores) !==
    JSON.stringify(recomputed.mean_llm_scores)
  ) {
    throw new Error(
      `mean_llm_scores mismatch: saved=${JSON.stringify(
        report.mean_llm_scores
      )} != recomputed=${JSON.stringify(recomputed.mean_llm_scores)}`
    );
  }

  return recomputed;
}

async function callOpenAiJudge(packet, { apiKey, baseUrl, model }) {
  const userPayload = {
    instance_id: packet.instance_id,
    raw_steps: packet.raw_steps,
    intent: packet.intent,
    "Option A": packet.option_a,
    "Option B": packet.option_b,
    required_schema: {
      option_a_score: {
        completeness: 1,
        precision_denoising: 1,
        temporal_causality: 1,
        overall: 1,
      },
      option_b_score: {
        completeness: 1,
        precision_denoising: 1,
        temporal_causality: 1,
        overall: 1,
      },
      winner: "Option A | Option B",
      judge_reason: "concise rationale",
    },
  };

  const resp = await fetch(`${baseUrl.replace(/\/+$/, "")}/chat/completions`, {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
      Authorization: `Bearer ${apiKey}`,
    },
    body: JSON.stringify({
      model,
      temperature: 0.0,
      response_format: { type: "json_object" },
      messages: [
        { role: "system", content: JUDGE_SYSTEM_PROMPT },
        { role: "user", content: JSON.stringify(userPayload) },
      ],
    }),
    signal: AbortSignal.timeout(60000),
  });
  if (!resp.ok) {
    throw new Error(`OpenAI judge HTTP ${resp.status}: ${await resp.text()}`);
  }
  const data = await resp.json();
  return JSON.parse(data.choices[0].message.content);
}

export async function runLiveJudge({ outputPath, model, baseUrl, apiKey, seed = 42 }) {
  const packets = buildBlindedJudgePackets(null, seed);
  const cases = [];
  for (const pkt of packets) {
    const verdict = await callOpenAiJudge(pkt, { apiKey, baseUrl, model });
    const aIsB1 = pkt.option_a_label === "B1_tmem";
    const b1Score = aIsB1 ? verdict.option_a_score : verdict.option_b_score;
    const b0Score = aIsB1 ? verdict.option_b_score : verdict.option_a_score;
    const rawWinner = String(verdict.winner || "").trim();
    let winner;
    if (rawWinner === "Option A") winner = pkt.option_a_label;
    else if (rawWinner === "Option B") winner = pkt.option_b_label;
    else winner = b1Score.overall >= b0Score.overall ? "B1_tmem" : "B0_baseline";

    cases.push({
      instance_id: pkt.instance_id,
      raw_steps: pkt.raw_steps,
      b0: { steps: pkt.b0_step_indices, llm_score: b0Score },
      b1: { steps: pkt.b1_step_indices, llm_score: b1Score },
      winner,
      judge_reason: verdict.judge_reason || "",
    });
  }

  const summary = aggregateJudgeCases(cases);
  const report = { ...summary, cases };
  mkdirSync(path.dirname(outputPath), { recursive: true });
  writeFileSync(outputPath, JSON.stringify(report, null, 2), "utf-8");
  return summary;
}

async function main(argv = process.argv.slice(2)) {
  const generatePackets = argv.includes("--generate-packets");
  const runJudge = argv.includes("--run-judge");
  let outputPath = path.join(WORKSPACE_ROOT, "benchmarks", "runs", "llm_judge_50_report.json");
  let seed = 42;

  const outIdx = argv.indexOf("--output");
  if (outIdx >= 0 && outIdx + 1 < argv.length) {
    outputPath = path.resolve(argv[outIdx + 1]);
  }
  const seedIdx = argv.indexOf("--seed");
  if (seedIdx >= 0 && seedIdx + 1 < argv.length) {
    seed = parseInt(argv[seedIdx + 1], 10) || 42;
  }

  if (generatePackets) {
    const packets = buildBlindedJudgePackets(null, seed);
    console.log(
      JSON.stringify(
        { total_packets: packets.length, sample_instance: packets[0].instance_id },
        null,
        2
      )
    );
    return 0;
  }

  if (runJudge) {
    const apiKey = (process.env.OPENAI_API_KEY || "").trim();
    if (!apiKey) {
      console.error("Error: OPENAI_API_KEY environment variable is required for --run-judge");
      return 1;
    }
    const baseUrl = process.env.OPENAI_BASE_URL || "https://api.openai.com/v1";
    const model = process.env.TMEM_JUDGE_MODEL || "gpt-4o-mini";
    const summary = await runLiveJudge({ outputPath, model, baseUrl, apiKey, seed });
    console.log(JSON.stringify(summary, null, 2));
    return 0;
  }

  try {
    const verified = verifyLlmJudgeReport();
    console.log(JSON.stringify(verified, null, 2));
    return 0;
  } catch (err) {
    console.error(`LLM judge report verification FAILED: ${err.message}`);
    return 1;
  }
}

const isEntrypoint =
  process.argv[1] &&
  path.resolve(process.argv[1]) === path.resolve(fileURLToPath(import.meta.url));
if (isEntrypoint) {
  process.exitCode = await main();
}
