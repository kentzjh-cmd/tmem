/**
 * @tmem/dsh-plugin — Zero-Token Historical Session Digest & Runtime Memory Plugin for DeepSeek Harness (@deepseek-ai/dsh)
 *
 * Core Definition:
 * Compiles historical DSH trajectories into zero-LLM-token requirement-and-behavior summaries
 * with inline `session_id` lineage (`.tmem/traces/`, `MEMORIES.md`, `.tmem/raw/`), eliminating
 * token overhead for session recap & forensic replay while guarding live execution:
 *
 * 1. Explicit-Anchor Pre-Step & Env Facts (`agent/pre-step`):
 *    Skips unchanged repeated steps cleanly; injects cross-task verified commands once per session
 *    and recalls matching traces only on explicit file-path or error-signature anchors.
 * 2. Strong-Signal Post-Execute Interception (`tools/post-execute`):
 *    Skips routine reads when idle; intercepts command errors with same-origin L2 fixes, surfaces
 *    statistical L1 co-change partner hints on code edits, emits `<tmem_milestone_reached>` on
 *    `MUTATE -> VERIFY(0)`, and records deterministic resolution telemetry.
 * 3. Native `/tmem` Slash Command (`commands.register`):
 *    Registers `/tmem [status|list|forget|gc|telemetry|inspect|replay|diff|sync]` in `dsh-commands`.
 */

import { spawn } from "node:child_process";
import { randomUUID } from "node:crypto";
import { existsSync } from "node:fs";
import { delimiter, dirname, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";

export const name = "tmem";

const SELF_DIR = dirname(fileURLToPath(import.meta.url));

function resolveDefaultTmemRoot(fallbackDir) {
  const oneUp = resolve(SELF_DIR, "..");
  if (existsSync(join(oneUp, "tmem", "cli.py"))) {
    return oneUp;
  }
  const twoUp = resolve(SELF_DIR, "../..");
  if (existsSync(join(twoUp, "tmem", "cli.py"))) {
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
        if (part && typeof part === "object" && part.type === "text" && typeof part.text === "string") {
          return part.text;
        }
        return "";
      })
      .filter(Boolean)
      .join("\n");
  }
  return "";
}

function invokeTmemHook(event, payload, options = {}) {
  return new Promise((promiseResolve) => {
    const workspaceRoot = resolve(options.workspaceRoot || process.cwd());
    const pythonBin = options.pythonBin || process.env.TMEM_PYTHON || "python3";
    const pythonPathRoot = resolve(options.tmemRoot || resolveDefaultTmemRoot(workspaceRoot));
    const env = {
      ...process.env,
      PYTHONPATH: process.env.PYTHONPATH
        ? `${pythonPathRoot}${delimiter}${process.env.PYTHONPATH}`
        : pythonPathRoot,
    };

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
      const child = spawn(pythonBin, ["-m", "tmem.cli", "hook", "dsh", event], {
        cwd: workspaceRoot,
        env,
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

      child.on("error", (_err) => {
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

      const inputPayload = JSON.stringify({ workspaceRoot, ...payload });
      child.stdin.end(inputPayload, "utf-8");
    } catch (_err) {
      safeResolve({});
    }
  });
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
  const tmemRoot = resolve(config.tmemRoot || resolveDefaultTmemRoot(configuredWorkspace || process.cwd()));
  const pythonBin = config.pythonBin || process.env.TMEM_PYTHON || "python3";

  const MUTATE_TOOLS = new Set([
    "write",
    "edit",
    "str_replace_editor",
    "apply_patch",
    "write_file",
    "edit_file",
  ]);
  const SHELL_TOOLS = new Set(["bash", "pwsh", "run_command"]);
  const seenPreStepInputBySession = new Map();
  const activeCochangeWatch = new Set();

  // 1. Register `/tmem` slash command whenever `dsh-commands` service is available
  const registerSlashCommand = (commandService) => {
    if (!commandService || typeof commandService.register !== "function") return;
    try {
      commandService.register({
        definitionId: "@tmem/dsh-command-tmem",
        name: "tmem",
        description:
          "Capture, slice, list, inspect, replay, or manage project execution trace memory (.tmem/)",
        input: {
          hint: "[status|list|forget|gc|bench|evaluate|inspect|replay|diff|sync] [<intent|trace_id>]",
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
            { workspaceRoot, tmemRoot, pythonBin }
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
  }

  // 2. Pre-Step Hook: Inject speculative execution memory (`<tmem_speculative_memory>`)
  ctx.on("agent/pre-step", async ({ agent, messages, step, signal }, next) => {
    const decision = await next();
    if (decision?.action === "abort") return decision;

    const currentMessages = Array.isArray(decision?.messages)
      ? [...decision.messages]
      : Array.isArray(messages)
        ? [...messages]
        : [];

    // Find latest real user message
    let lastUserInput = "";
    let lastUserIndex = -1;
    for (let i = currentMessages.length - 1; i >= 0; i--) {
      const m = currentMessages[i];
      if (m && m.role === "user" && (!m.source || m.source.kind === "user")) {
        lastUserInput = extractTextFromContent(m.content).trim();
        lastUserIndex = i;
        break;
      }
    }

    const workspaceRoot = resolveSessionWorkspace(ctx, agent, configuredWorkspace);
    const sessionId = agent?.session?.id || "default";

    // Hot-path optimization: avoid spawning Python on every agent loop step when user input hasn't changed.
    const prevSeenInput = seenPreStepInputBySession.get(sessionId);
    if (typeof step === "number" && step > 0 && prevSeenInput === lastUserInput) {
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
      },
      { workspaceRoot, tmemRoot, pythonBin }
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
        action: "continue",
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

    // Hot-path optimization: skip Python cold-start on routine read/search calls unless an error
    // occurred or an L1 co-change watch is active for this session.
    const shouldInvokePython =
      isError ||
      (exitCode !== null && exitCode !== 0) ||
      MUTATE_TOOLS.has(toolName) ||
      SHELL_TOOLS.has(toolName) ||
      activeCochangeWatch.has(sessionId);

    if (!shouldInvokePython) {
      return downstream;
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
      { workspaceRoot, tmemRoot, pythonBin }
    );

    if (out && typeof out.additionalContext === "string" && out.additionalContext.trim()) {
      const ctxText = out.additionalContext.trim();
      if (ctxText.includes("<tmem_cochange_hint>")) {
        activeCochangeWatch.add(sessionId);
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
