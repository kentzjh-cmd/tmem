/**
 * @tmem/dsh-plugin — Zero-Token Historical Session Digest & Runtime Memory Plugin for DeepSeek Harness (@deepseek-ai/dsh)
 *
 * Pure Node.js ESM runtime (zero Python dependency):
 * Resolves `handleDshHook` and `executeCapture` in-process when loaded alongside the `dsh-tmem`
 * package (or via `config.tmemRoot` / `TMEM_ROOT`), with non-blocking async CLI fallback (`tmem hook dsh`)
 * when deployed as a standalone copied plugin file.
 */

import { spawn } from "node:child_process";
import { randomUUID } from "node:crypto";
import { existsSync } from "node:fs";
import path, { dirname, join, resolve } from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";

export const name = "tmem";

const SELF_DIR = dirname(fileURLToPath(import.meta.url));

function resolveDefaultTmemRoot(fallbackDir) {
  if (existsSync(join(SELF_DIR, "hooks", "dsh.mjs"))) {
    return resolve(SELF_DIR, "..");
  }
  const oneUp = resolve(SELF_DIR, "..");
  if (existsSync(join(oneUp, "tmem", "cli.mjs"))) {
    return oneUp;
  }
  const twoUp = resolve(SELF_DIR, "../..");
  if (existsSync(join(twoUp, "tmem", "cli.mjs"))) {
    return twoUp;
  }
  return resolve(fallbackDir || process.cwd());
}

function resolveSessionWorkspace(ctx, agent, configuredWorkspace) {
  if (configuredWorkspace) {
    return resolve(configuredWorkspace);
  }
  try {
    const wdService = typeof ctx?.get === "function" ? ctx.get("workingDirectory") : null;
    if (wdService && typeof wdService.get === "function" && agent?.session) {
      const sessDir = wdService.get(agent.session);
      if (typeof sessDir === "string" && sessDir.trim()) {
        return resolve(sessDir);
      }
    }
  } catch (_err) {
    // Ignore reflection errors
  }
  if (typeof agent?.session?.header?.cwd === "string" && agent.session.header.cwd.trim()) {
    return resolve(agent.session.header.cwd);
  }
  return resolve(process.cwd());
}

function extractTextFromContent(content) {
  if (!content) return "";
  if (typeof content === "string") return content;
  if (Array.isArray(content)) {
    return content
      .map((part) => {
        if (typeof part === "string") return part;
        if (
          part &&
          typeof part === "object" &&
          part.type === "text" &&
          typeof part.text === "string"
        ) {
          return part.text;
        }
        return "";
      })
      .filter(Boolean)
      .join("\n");
  }
  return "";
}

const _runtimeCache = new Map();

async function loadInProcessRuntime(tmemRoot) {
  const key = tmemRoot || "";
  if (_runtimeCache.has(key)) {
    return _runtimeCache.get(key);
  }

  const candidatePairs = [];
  // 1. Sibling modules when loaded directly from tmem/dsh_plugin.mjs
  candidatePairs.push([
    join(SELF_DIR, "hooks", "index.mjs"),
    join(SELF_DIR, "hooks", "dsh.mjs"),
    join(SELF_DIR, "cli.mjs"),
  ]);
  // 2. Explicit tmemRoot or TMEM_ROOT env
  for (const root of [tmemRoot, process.env.TMEM_ROOT].filter(Boolean)) {
    candidatePairs.push([
      join(resolve(root), "tmem", "hooks", "index.mjs"),
      join(resolve(root), "tmem", "hooks", "dsh.mjs"),
      join(resolve(root), "tmem", "cli.mjs"),
    ]);
  }

  for (const [hooksIdxPath, dshModPath, cliModPath] of candidatePairs) {
    const hookEntry = existsSync(hooksIdxPath) ? hooksIdxPath : dshModPath;
    if (existsSync(hookEntry) && existsSync(cliModPath)) {
      try {
        const [dshMod, cliMod] = await Promise.all([
          import(pathToFileURL(hookEntry).href),
          import(pathToFileURL(cliModPath).href),
        ]);
        if (typeof dshMod.handleDshHook === "function") {
          const rt = {
            handleDshHook: dshMod.handleDshHook,
            handleDshSessionEvent: dshMod.handleDshSessionEvent || null,
            createTmemTools: dshMod.createTmemTools || null,
            TMEM_SYSTEM_PROMPT_SECTION: dshMod.TMEM_SYSTEM_PROMPT_SECTION || "",
            executeCapture: cliMod.executeCapture || null,
          };
          _runtimeCache.set(key, rt);
          return rt;
        }
      } catch (_err) {
        // Fall through to package/CLI resolution
      }
    }
  }

  // 3. Installed npm package resolution (`dsh-tmem`)
  try {
    const [dshMod, cliMod] = await Promise.all([
      import("dsh-tmem/hooks"),
      import("dsh-tmem/cli"),
    ]);
    if (typeof dshMod.handleDshHook === "function") {
      const rt = {
        handleDshHook: dshMod.handleDshHook,
        handleDshSessionEvent: dshMod.handleDshSessionEvent || null,
        createTmemTools: dshMod.createTmemTools || null,
        TMEM_SYSTEM_PROMPT_SECTION: dshMod.TMEM_SYSTEM_PROMPT_SECTION || "",
        executeCapture: cliMod.executeCapture || null,
      };
      _runtimeCache.set(key, rt);
      return rt;
    }
  } catch (_err) {
    // Fall through to async CLI spawn fallback
  }

  _runtimeCache.set(key, null);
  return null;
}

function invokeTmemHookCliFallback(event, payload, options = {}) {
  return new Promise((promiseResolve) => {
    const workspaceRoot = resolve(options.workspaceRoot || process.cwd());
    const tmemRoot = resolve(options.tmemRoot || resolveDefaultTmemRoot(workspaceRoot));
    const cliScript = join(tmemRoot, "tmem", "cli.mjs");
    const useDirectScript = existsSync(cliScript);
    const bin = useDirectScript
      ? options.nodeBin || process.env.TMEM_NODE || process.execPath
      : options.tmemBin || process.env.TMEM_BIN || "tmem";
    const args = useDirectScript
      ? [cliScript, "hook", "dsh", event]
      : ["hook", "dsh", event];

    let timer = null;
    let finished = false;
    const safeResolve = (val) => {
      if (finished) return;
      finished = true;
      if (timer) {
        clearTimeout(timer);
        timer = null;
      }
      promiseResolve(val);
    };

    try {
      const child = spawn(bin, args, {
        cwd: workspaceRoot,
        env: process.env,
        stdio: ["pipe", "pipe", "pipe"],
      });

      timer = setTimeout(() => {
        try {
          child.kill("SIGKILL");
        } catch (_e) {}
        safeResolve({});
      }, 5000);

      let stdout = "";
      child.stdout.setEncoding("utf-8");
      child.stdout.on("data", (chunk) => {
        stdout += chunk;
      });

      child.on("error", () => {
        safeResolve({});
      });

      child.on("close", (code) => {
        if (code === 0 && stdout) {
          const trimmed = stdout.trim();
          if (trimmed) {
            try {
              return safeResolve(JSON.parse(trimmed));
            } catch (_e) {
              return safeResolve({});
            }
          }
        }
        safeResolve({});
      });

      child.stdin.end(JSON.stringify({ workspaceRoot, ...payload }), "utf-8");
    } catch (_err) {
      safeResolve({});
    }
  });
}

async function invokeTmemHook(event, payload, options = {}) {
  const workspaceRoot = resolve(options.workspaceRoot || process.cwd());
  const tmemRoot = resolve(options.tmemRoot || resolveDefaultTmemRoot(workspaceRoot));
  const rt = await loadInProcessRuntime(tmemRoot);
  if (rt) {
    try {
      return rt.handleDshHook(
        event,
        { workspaceRoot, ...payload },
        rt.executeCapture
      );
    } catch (_err) {
      return {};
    }
  }
  return invokeTmemHookCliFallback(event, payload, { ...options, workspaceRoot, tmemRoot });
}

function createNoticeMessage(text, summary = "tmem") {
  return {
    id: `tmem_${randomUUID()}`,
    role: "user",
    content: [{ type: "text", text }],
    source: {
      kind: "tmem",
      form: "notice",
      summary,
    },
  };
}

export function apply(ctx, config = {}) {
  const configuredWorkspace = config.workspaceRoot ? resolve(config.workspaceRoot) : "";
  const tmemRoot = resolve(
    config.tmemRoot || resolveDefaultTmemRoot(configuredWorkspace || process.cwd())
  );
  const nodeBin = config.nodeBin || process.env.TMEM_NODE || process.execPath;

  const MUTATE_TOOLS = new Set([
    "write",
    "edit",
    "str_replace_editor",
    "apply_patch",
    "write_file",
    "edit_file",
  ]);
  const SHELL_TOOLS = new Set(["bash", "pwsh", "run_command"]);
  const MAX_COCHANGE_WATCH_STEPS = 3;
  const MAX_CACHED_SESSIONS = 16;
  const MAX_EVENTS_PER_SESSION = 2500;
  const seenPreStepInputBySession = new Map();
  const activeCochangeWatch = new Map();
  const sessionEventsCache = new Map();
  const pendingCompactions = new Map();
  const pendingCompactedIdsBySession = new Map();
  const seenCompactedIdsBySession = new Map();

  // Warm runtime in background so synchronous `session/event` handlers resolve immediately
  let cachedRuntime = null;
  const runtimePromise = loadInProcessRuntime(tmemRoot)
    .then((rt) => {
      cachedRuntime = rt;
      return rt;
    })
    .catch(() => null);

  const getRuntimeSyncOrAsync = async () => cachedRuntime || (await runtimePromise);

  // 1. Register `/tmem` slash command whenever `dsh-commands` service is available
  const registerSlashCommand = (commandService) => {
    if (!commandService || typeof commandService.register !== "function") return;
    try {
      commandService.register({
        definitionId: "@tmem/dsh-command-tmem",
        name: "tmem",
        description:
          "Capture, slice, search, expand, list, inspect, replay, or manage project execution trace & compacted episode memory (.tmem/)",
        input: {
          hint: "[status|search|episodes|expand|list|forget|gc|telemetry|bench|evaluate|inspect|replay|diff|sync] [<intent|query|id>]",
        },
        handler: async (invocation) => {
          const workspaceRoot = resolveSessionWorkspace(
            ctx,
            invocation?.agent,
            configuredWorkspace
          );
          const out = await invokeTmemHook(
            "command",
            {
              sessionId:
                invocation?.agent?.session?.id || invocation?.sessionId || "default",
              commandId: invocation?.commandId || "",
              args: invocation?.rawInput ?? invocation?.args ?? "",
            },
            { workspaceRoot, tmemRoot, nodeBin }
          );
          if (out && (out.kind === "error" || out.kind === "failure")) {
            return {
              kind: "error",
              text: out.text || out.error || "tmem command failed",
            };
          }
          return {
            kind: "success",
            text: out?.text || "tmem command completed.",
          };
        },
      });
    } catch (_err) {
      // Ignore duplicate registration in hot-reload contexts
    }
  };

  if (typeof ctx.inject === "function") {
    ctx.inject(["commands"], (innerCtx) => {
      registerSlashCommand(innerCtx.commands);
    });

    // 1b. Register model-facing progressive recovery tools (`tmem_search`, `tmem_expand`)
    ctx.inject(["tools"], async (innerCtx) => {
      const toolsSvc = innerCtx?.tools;
      if (!toolsSvc || typeof toolsSvc.register !== "function") return;
      const rt = await getRuntimeSyncOrAsync();
      if (!rt || typeof rt.createTmemTools !== "function") return;
      const defs = rt.createTmemTools({
        resolveWorkspace: (agent) =>
          resolveSessionWorkspace(ctx, agent, configuredWorkspace),
        resolveSessionId: (agent) => agent?.session?.id || "default",
      });
      for (const def of defs) {
        try {
          toolsSvc.register(def);
        } catch (_err) {
          // Ignore duplicate registration in hot-reload contexts
        }
      }
    });

    // 1c. Register concise progressive recovery guidance in system prompt
    ctx.inject(["systemPrompt"], async (innerCtx) => {
      const sp = innerCtx?.systemPrompt;
      if (!sp || typeof sp.section !== "function") return;
      const rt = await getRuntimeSyncOrAsync();
      const text = rt?.TMEM_SYSTEM_PROMPT_SECTION;
      if (!text) return;
      const baseOrder =
        typeof sp.getSectionOrder === "function"
          ? sp.getSectionOrder("TOOL_SESSION_QUERY")
          : 2300;
      const safeOrder = Number.isFinite(baseOrder) ? baseOrder + 10 : 2310;
      try {
        sp.section({
          name: "tmem:progressive-memory",
          order: safeOrder,
          text,
        });
      } catch (_err) {
        // Ignore duplicate registration
      }
    });
  }

  // 1d. Session Event Hook: Automatically archive & inverted-index compacted/pruned spans
  const handleLiveCompaction = (rt, kind, payload) => {
    if (!rt || typeof rt.handleDshSessionEvent !== "function") return;
    try {
      const out = rt.handleDshSessionEvent(kind, payload);
      if (kind === "compaction_committed" && out?.compaction_id && payload.session?.id) {
        const sid = payload.session.id;
        let q = pendingCompactedIdsBySession.get(sid);
        if (!q) {
          q = new Set();
          pendingCompactedIdsBySession.set(sid, q);
        }
        q.add(String(out.compaction_id));
      }
    } catch (_err) {
      // Never destabilize host session event loop
    }
  };

  ctx.on("session/event", (session, event) => {
    if (!session || !event || typeof event !== "object") return;
    const sid = String(session.id || "default");

    let evMap = sessionEventsCache.get(sid);
    if (!evMap) {
      if (sessionEventsCache.size >= MAX_CACHED_SESSIONS) {
        const oldestKey = sessionEventsCache.keys().next().value;
        if (oldestKey !== undefined) sessionEventsCache.delete(oldestKey);
      }
      evMap = new Map();
      sessionEventsCache.set(sid, evMap);
    }
    if (typeof event.seq === "number" && Number.isInteger(event.seq)) {
      evMap.set(event.seq, event);
      if (evMap.size > MAX_EVENTS_PER_SESSION) {
        const oldestSeq = evMap.keys().next().value;
        if (oldestSeq !== undefined) evMap.delete(oldestSeq);
      }
    }

    const etype = event.type;
    if (etype === "compaction/summary") {
      const cid = String(event.data?.compactionId || "");
      if (cid) {
        if (pendingCompactions.size > 64) pendingCompactions.clear();
        pendingCompactions.set(`${sid}\0${cid}`, event);
      }
      return;
    }

    if (etype === "compaction/end") {
      const cid = String(event.data?.compactionId || "");
      const key = `${sid}\0${cid}`;
      const summaryEvent = pendingCompactions.get(key);
      pendingCompactions.delete(key);
      if (!summaryEvent || event.data?.error) return;
      const workspaceRoot = resolveSessionWorkspace(ctx, { session }, configuredWorkspace);
      const payload = {
        session,
        summaryEvent,
        endEvent: event,
        workspaceRoot,
        cachedEvents: evMap,
      };
      if (cachedRuntime) {
        handleLiveCompaction(cachedRuntime, "compaction_committed", payload);
      } else {
        void getRuntimeSyncOrAsync().then((rt) =>
          handleLiveCompaction(rt, "compaction_committed", payload)
        );
      }
      return;
    }

    if (etype === "compaction/prune") {
      const workspaceRoot = resolveSessionWorkspace(ctx, { session }, configuredWorkspace);
      const payload = {
        session,
        pruneEvent: event,
        workspaceRoot,
        cachedEvents: evMap,
      };
      if (cachedRuntime) {
        handleLiveCompaction(cachedRuntime, "compaction_prune", payload);
      } else {
        void getRuntimeSyncOrAsync().then((rt) =>
          handleLiveCompaction(rt, "compaction_prune", payload)
        );
      }
    }
  });

  // 2. Pre-Step Hook: Inject compacted-phase L0 index + speculative execution memory
  ctx.on("agent/pre-step", async ({ agent, messages, step }, next) => {
    const decision = await next();
    if (decision?.kind === "reject" || decision?.action === "abort") return decision;

    const currentMessages = Array.isArray(decision?.messages)
      ? [...decision.messages]
      : Array.isArray(messages)
        ? [...messages]
        : [];

    let lastUserInput = "";
    let lastUserIndex = -1;
    const checkpointIds = new Set();
    for (let i = 0; i < currentMessages.length; i++) {
      const m = currentMessages[i];
      if (!m) continue;
      if (m.role === "user" && m.source?.kind === "compact-checkpoint" && m.source.compactionId) {
        checkpointIds.add(String(m.source.compactionId));
      }
      if (m.role === "user" && (!m.source || m.source.kind === "user")) {
        lastUserInput = extractTextFromContent(m.content).trim();
        lastUserIndex = i;
      }
    }

    // Also inspect live session derived surface (for automatic pressure compaction & resume)
    if (typeof agent?.session?.deriveMessages === "function") {
      try {
        for (const m of agent.session.deriveMessages()) {
          if (
            m &&
            m.role === "user" &&
            m.source?.kind === "compact-checkpoint" &&
            m.source.compactionId
          ) {
            checkpointIds.add(String(m.source.compactionId));
          }
        }
      } catch (_err) {
        // ignore derivation errors on partial mocks
      }
    }

    const workspaceRoot = resolveSessionWorkspace(ctx, agent, configuredWorkspace);
    const sessionId = agent?.session?.id || "default";

    const pendingForSess = pendingCompactedIdsBySession.get(sessionId);
    if (pendingForSess) {
      for (const cid of pendingForSess) checkpointIds.add(cid);
      pendingCompactedIdsBySession.delete(sessionId);
    }

    let seenSet = seenCompactedIdsBySession.get(sessionId);
    if (!seenSet) {
      if (seenCompactedIdsBySession.size > 64) seenCompactedIdsBySession.clear();
      seenSet = new Set();
      seenCompactedIdsBySession.set(sessionId, seenSet);
    }
    const freshCheckpointIds = Array.from(checkpointIds).filter((cid) => !seenSet.has(cid));
    for (const cid of freshCheckpointIds) seenSet.add(cid);

    const prevSeenInput = seenPreStepInputBySession.get(sessionId);
    if (
      typeof step === "number" &&
      step > 0 &&
      prevSeenInput === lastUserInput &&
      freshCheckpointIds.length === 0
    ) {
      return decision;
    }
    if (seenPreStepInputBySession.size > 64) {
      seenPreStepInputBySession.clear();
    }
    seenPreStepInputBySession.set(sessionId, lastUserInput);

    const out = await invokeTmemHook(
      "pre_invocation",
      {
        sessionId,
        step,
        lastUserInput,
        compactedCheckpoints: freshCheckpointIds,
      },
      { workspaceRoot, tmemRoot, nodeBin }
    );

    if (out && typeof out.injectedContext === "string" && out.injectedContext.trim()) {
      const injectedText = out.injectedContext.trim();
      const alreadyInjected = currentMessages.some(
        (m) =>
          m &&
          m.source?.kind === "tmem" &&
          extractTextFromContent(m.content).trim() === injectedText
      );
      if (alreadyInjected) {
        return decision;
      }

      const noticeMsg = createNoticeMessage(injectedText, "tmem-speculative-memory");
      if (lastUserIndex >= 0) {
        currentMessages.splice(lastUserIndex, 0, noticeMsg);
      } else {
        currentMessages.push(noticeMsg);
      }
      return {
        ...decision,
        ...(decision?.kind ? {} : { action: "continue" }),
        messages: currentMessages,
      };
    }

    return decision;
  });

  // 3. Post-Tool Hook: Track mutations, errors (L2 repair), and `MUTATE -> VERIFY(0)` milestones
  ctx.on("tools/post-execute", async (exec, result, next) => {
    const downstream = await next();

    let parsedArgs = {};
    if (exec?.arguments && typeof exec.arguments === "object") {
      parsedArgs = exec.arguments;
    } else if (typeof exec?.arguments === "string" && exec.arguments.trim()) {
      try {
        parsedArgs = JSON.parse(exec.arguments);
      } catch (_e) {
        parsedArgs = { raw: exec.arguments };
      }
    }

    const resultText = extractTextFromContent(result?.content);
    const isError = Boolean(result?.isError);
    const exitCode =
      typeof result?.meta?.exitCode === "number"
        ? result.meta.exitCode
        : typeof result?.value?.exitCode === "number"
          ? result.value.exitCode
          : null;

    const workspaceRoot = resolveSessionWorkspace(ctx, exec?.agent, configuredWorkspace);
    const sessionId = exec?.agent?.session?.id || "default";
    const toolName = String(exec?.name || "");

    const watchStepsLeft = activeCochangeWatch.get(sessionId) || 0;
    const isDirectAction =
      isError ||
      (exitCode !== null && exitCode !== 0) ||
      MUTATE_TOOLS.has(toolName) ||
      SHELL_TOOLS.has(toolName);
    const shouldInvoke = isDirectAction || watchStepsLeft > 0;

    if (!shouldInvoke) {
      return downstream;
    }

    if (!isDirectAction && watchStepsLeft > 0) {
      if (watchStepsLeft <= 1) {
        activeCochangeWatch.delete(sessionId);
      } else {
        activeCochangeWatch.set(sessionId, watchStepsLeft - 1);
      }
    }

    const out = await invokeTmemHook(
      "post_tool_use",
      {
        sessionId,
        toolCall: {
          name: toolName,
          args: parsedArgs,
        },
        resultText,
        isError,
        exitCode,
      },
      { workspaceRoot, tmemRoot, nodeBin }
    );

    if (out && typeof out.additionalContext === "string" && out.additionalContext.trim()) {
      const ctxText = out.additionalContext.trim();
      if (ctxText.includes("<tmem_cochange_hint>")) {
        if (activeCochangeWatch.size > 64) {
          activeCochangeWatch.clear();
        }
        activeCochangeWatch.set(sessionId, MAX_COCHANGE_WATCH_STEPS);
      }
      if (ctxText.includes("<tmem_milestone_reached>")) {
        activeCochangeWatch.delete(sessionId);
      }
      const notice = createNoticeMessage(ctxText, "tmem-milestone");
      return {
        ...downstream,
        additionalContexts: [notice, ...(downstream?.additionalContexts || [])],
      };
    }

    return downstream;
  });
}

export default {
  name,
  apply,
};
