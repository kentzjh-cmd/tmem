/**
 * Model-facing Progressive Memory Disclosure Tools (`tmem_search`, `tmem_expand`)
 * for DeepSeek Harness (@deepseek-ai/dsh).
 *
 * Implements the DSH `ToolDefinition` JSON-schema contract natively with zero
 * external npm dependencies. Allows the agent to query the multi-dimensional
 * inverted index across compacted session episodes and verified playbooks, then
 * progressively drill down from L0 (index) -> L1 (slice) -> L2 (turn/diff) -> L3 (raw).
 */

import { formatEpisodeProgressiveView, formatSearchHits } from "./episodes.mjs";
import { formatProgressiveView } from "./raw_storage.mjs";
import { ProjectMemoryStore } from "./store.mjs";

export const TMEM_SYSTEM_PROMPT_SECTION =
  "When prior conversation turns were compacted (marked by `<compact-checkpoint>` / `<tmem_compacted_index>`) or large tool outputs were pruned, " +
  "use `tmem_search` (keyword/file/symbol/error/semantic-role inverted lookup) followed by `tmem_expand` (`slice` -> `turn` / `diff` -> `raw`) " +
  "to progressively recover exact decisions, code edits, stack traces, or pruned tool logs without re-reading the whole repository.";

/**
 * Resolve an episode or playbook by exact or prefix ID and render at the requested tier.
 */
export function expandMemoryTarget(
  workspaceRoot,
  {
    id,
    level = "slice",
    turn = null,
    seq_from = null,
    seq_to = null,
    query = "",
    file = "",
    max_chars = 6000,
  } = {}
) {
  const targetId = String(id || "").trim();
  if (!targetId) {
    return "Error: `id` (episode_id or trace_id) is required.";
  }

  const store = new ProjectMemoryStore(workspaceRoot);
  const clampedMax = Math.min(20000, Math.max(500, Number(max_chars || 6000)));

  // 1. Try Episode store first (or prefix `ep_`)
  const ep = store.loadEpisode(targetId, { touchLru: true });
  if (ep) {
    return formatEpisodeProgressiveView(ep, {
      level,
      turn,
      seqFrom: seq_from,
      seqTo: seq_to,
      query,
      file,
      maxChars: clampedMax,
    });
  }

  // 2. Fallback to Project Playbook store (`tr_...` or session_id match)
  let targetPb = null;
  for (const pb of store.getAllProjectPlaybooks()) {
    if (
      pb.trace_id === targetId ||
      pb.trace_id.startsWith(targetId) ||
      (pb.source_session_ids || []).some((s) => s === targetId || s.startsWith(targetId))
    ) {
      targetPb = pb;
      break;
    }
  }
  if (!targetPb) {
    return `Memory target '${targetId}' not found in .tmem/episodes/ or .tmem/traces/. Call \`tmem_search\` to list available IDs.`;
  }

  const lvlNorm = String(level || "slice").toLowerCase().trim();
  const rawTr = store.getRawTrace(targetPb.trace_id);

  // If a Playbook was promoted from a compacted SessionEpisode (no `.tmem/raw/` file),
  // seamlessly bridge `turn` / `diff` / `raw` expansion to its linked SessionEpisode.
  if (!rawTr && ["raw", "l3", "diff", "turn", "l2"].includes(lvlNorm)) {
    const pbFileSet = new Set((targetPb.touched_files || []).map((f) => f.toLowerCase()));
    const sessIds = [...(targetPb.source_session_ids || [])].reverse();
    let bestEpMeta = null;
    let bestScore = -1;
    for (const sid of sessIds) {
      for (const meta of store.listEpisodes({ sessionId: sid, limit: 20 })) {
        const overlap = (meta.touched_files || []).filter((f) =>
          pbFileSet.has(f.toLowerCase())
        ).length;
        const sc = overlap * 10 + (meta.trigger === "compaction" ? 2 : 0);
        if (sc > bestScore) {
          bestScore = sc;
          bestEpMeta = meta;
        }
      }
      if (bestEpMeta) break;
    }
    if (bestEpMeta) {
      const linkedEp = store.loadEpisode(bestEpMeta.episode_id, { touchLru: true });
      if (linkedEp) {
        return formatEpisodeProgressiveView(linkedEp, {
          level: lvlNorm,
          turn,
          seqFrom: seq_from,
          seqTo: seq_to,
          query,
          file,
          maxChars: clampedMax,
        });
      }
    }
  }

  const mappedLevel = lvlNorm === "slice" || lvlNorm === "turn" ? "playbook" : lvlNorm;
  const rendered = formatProgressiveView(targetPb, rawTr, mappedLevel);
  if (rendered.length <= clampedMax) return rendered;
  return rendered.slice(0, clampedMax - 80) + "\n\n[... truncated at max_chars ...]";
}

/**
 * Build DSH-compatible ToolDefinitions for `tmem_search` and `tmem_expand`.
 */
export function createTmemTools({ resolveWorkspace, resolveSessionId } = {}) {
  const getWs = (exec) =>
    (typeof resolveWorkspace === "function"
      ? resolveWorkspace(exec?.agent)
      : null) ||
    exec?.agent?.session?.header?.cwd ||
    process.cwd();

  const getSessId = (exec) =>
    (typeof resolveSessionId === "function" ? resolveSessionId(exec?.agent) : null) ||
    exec?.agent?.session?.id ||
    null;

  const textOutput = {
    schema: { type: "string" },
    render: (_args, value) => [{ type: "text", text: String(value ?? "") }],
  };

  const tmemSearchTool = {
    name: "tmem_search",
    description:
      "Search compacted session episodes and verified project playbooks via deterministic multi-dimensional inverted index (keywords, files, symbols, errors, and semantic roles). Returns lightweight L0 index hits with exact turn/seq coordinates for progressive expansion via `tmem_expand`.",
    parameters: {
      type: "object",
      properties: {
        query: {
          type: "string",
          description:
            "Keywords, identifiers, Chinese/English phrases, or error fragments to look up in the inverted index.",
        },
        files: {
          type: "array",
          items: { type: "string" },
          description: "Optional file paths to filter/anchor results.",
        },
        symbols: {
          type: "array",
          items: { type: "string" },
          description: "Optional function/class/symbol names to filter results.",
        },
        errors: {
          type: "array",
          items: { type: "string" },
          description: "Optional error names/signatures to filter results.",
        },
        roles: {
          type: "array",
          items: {
            type: "string",
            enum: [
              "intent",
              "decision",
              "error",
              "symbol",
              "file",
              "command",
              "diff",
              "raw_output",
            ],
          },
          description:
            "Optional semantic hierarchy roles to restrict matching (e.g. `decision`, `error`, `diff`, `raw_output`).",
        },
        scope: {
          type: "string",
          enum: ["all", "episodes", "playbooks"],
          default: "all",
          description: "Search across compacted episodes, project playbooks, or both.",
        },
        session_id: {
          type: "string",
          description:
            "Optional DSH session ID filter; pass 'current' to restrict to the active session.",
        },
        limit: {
          type: "integer",
          default: 5,
          description: "Maximum number of L0 hits to return (1-15).",
        },
      },
      required: [],
      additionalProperties: false,
    },
    output: textOutput,
    isConcurrencySafe: () => true,
    presentCall: (args) => ({
      card: "generic",
      kind: "read",
      title: `Search tmem inverted index${args?.query ? `: ${String(args.query).slice(0, 60)}` : ""}`,
    }),
    async execute(args = {}, exec) {
      const ws = getWs(exec);
      const store = new ProjectMemoryStore(ws);
      let sessFilter = args.session_id ? String(args.session_id).trim() : null;
      if (sessFilter === "current") {
        sessFilter = getSessId(exec);
      }
      const limit = Math.min(15, Math.max(1, Number(args.limit || 5)));
      const hits = store.searchMemory({
        query: args.query || "",
        files: args.files || [],
        symbols: args.symbols || [],
        errors: args.errors || [],
        roles: args.roles || [],
        scope: args.scope || "all",
        sessionId: sessFilter,
        limit,
      });
      return formatSearchHits(hits, args.query || "");
    },
  };

  const tmemExpandTool = {
    name: "tmem_expand",
    description:
      "Progressively disclose details of a compacted episode (`ep_...`) or playbook (`tr_...`) returned by `tmem_search` or `<tmem_compacted_index>`. Choose `level`: `index` (L0 brief), `slice` (L1 decisions + turn TOC + critical spine), `turn` (L2 specific turn/window + actions), `diff` (L2 code edit patches), or `raw` (L3 unabridged tool outputs including pruned logs).",
    parameters: {
      type: "object",
      properties: {
        id: {
          type: "string",
          description: "Episode ID (`ep_...`) or Trace ID (`tr_...`) to expand.",
        },
        level: {
          type: "string",
          enum: ["index", "slice", "turn", "diff", "raw"],
          default: "slice",
          description: "Disclosure tier (L0=index, L1=slice, L2=turn/diff, L3=raw).",
        },
        turn: {
          type: "integer",
          description: "Optional Turn number within the episode (for `turn` or `raw` level).",
        },
        seq_from: {
          type: "integer",
          description: "Optional inclusive start event seq (for `turn` or `raw` window).",
        },
        seq_to: {
          type: "integer",
          description: "Optional inclusive end event seq (for `turn` or `raw` window).",
        },
        query: {
          type: "string",
          description:
            "Optional keyword filter to center `turn` or `raw` output around matching steps.",
        },
        file: {
          type: "string",
          description: "Optional file filter when `level` is `diff`.",
        },
        max_chars: {
          type: "integer",
          default: 6000,
          description: "Maximum output characters (500-20000).",
        },
      },
      required: ["id"],
      additionalProperties: false,
    },
    output: textOutput,
    isConcurrencySafe: () => true,
    presentCall: (args) => ({
      card: "generic",
      kind: "read",
      title: `Expand tmem ${args?.id || ""} (${args?.level || "slice"})`,
    }),
    async execute(args = {}, exec) {
      const ws = getWs(exec);
      return expandMemoryTarget(ws, args);
    },
  };

  return [tmemSearchTool, tmemExpandTool];
}
