import test from "node:test";
import assert from "node:assert/strict";
import { mkdirSync, mkdtempSync, rmSync, writeFileSync } from "node:fs";
import os from "node:os";
import path from "node:path";

import { ProjectMemoryStore, cjkBigrams } from "../tmem/store.mjs";
import { TracePlaybook } from "../tmem/models.mjs";

/**
 * Regression: Chinese natural-language questions must reach stored
 * playbooks.
 *
 * SQLite FTS5 (unicode61) stores a whole CJK run as ONE token, so the FTS
 * path alone can never match a Chinese question against an indexed Chinese
 * sentence. The store therefore scores playbooks by character-bigram
 * coverage for CJK queries.
 *
 * Found by the independent E2E study (see
 * docs/dsh-independent-e2e-report.md), where `tmem lookup` returned an
 * empty result for every Chinese question while `tmem search` found the
 * same content.
 */
test("CJK natural-language query reaches a stored playbook", (t) => {
  const ws = mkdtempSync(path.join(os.tmpdir(), "tmem_cjk_lookup_"));
  t.after(() => rmSync(ws, { recursive: true, force: true }));
  mkdirSync(path.join(ws, "src"), { recursive: true });
  writeFileSync(path.join(ws, "src", "rates.py"), "DEFAULT_RATES = {}\n", "utf-8");

  const store = new ProjectMemoryStore(ws);
  const pb = new TracePlaybook({
    trace_id: "tr_cjk_lookup_001",
    agent_source: "dsh",
    scope: "project",
    created_at: "2026-11-22T00:00:00Z",
    problem_intent: "结算取汇率时，缓存里没有这个币对就会抛 KeyError，请改成回落到默认汇率表",
    semantic_summary: "汇率缓存未命中时回落到 DEFAULT_RATES，避免 KeyError",
    root_cause: "RATE_CACHE 只有在 fetch_rate 之后才持有对应币对",
    error_signatures: ["KeyError"],
    touched_files: ["src/rates.py"],
    touched_symbols: ["fetch_rate"],
    l0_critical_path: [
      { step: 1, action: "MUTATE", target: "src/rates.py", rationale: "回落默认汇率表" },
    ],
  });
  store.savePlaybook(pb, { mergeSimilar: false });

  const hits = store.cascadingLookup({ queryText: "汇率表定义在哪个文件" });
  assert.ok(
    hits.length > 0,
    "a Chinese question must reach the stored playbook (FTS alone cannot match CJK)",
  );
  assert.ok(
    hits.some((hit) => hit.playbook.trace_id === "tr_cjk_lookup_001"),
    "the matching playbook must be the one that holds the rate table knowledge",
  );
});

test("cjkBigrams extracts character bigrams from CJK runs only", () => {
  assert.deepEqual(cjkBigrams("汇率表"), ["汇率", "率表"]);
  assert.deepEqual(cjkBigrams("a汇率b"), ["汇率"]);
  assert.deepEqual(cjkBigrams("单字"), ["单字"]);
  assert.deepEqual(cjkBigrams("english only"), []);
  assert.deepEqual(cjkBigrams(""), []);
});
