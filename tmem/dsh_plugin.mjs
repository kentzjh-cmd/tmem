/**
 * @tmem/dsh-plugin — Invisible Execution Trace Memory Plugin for DeepSeek Harness (@deepseek-ai/dsh)
 *
 * Capabilities:
 * 1. Pre-Step Speculative Memory Recall (`agent/pre-step`):
 *    Silently queries Project (`.tmem/`) memory tier on new user turns or
 *    after tool errors, injecting `<tmem_speculative_memory>` (L0_FULL / L1_TOPOLOGY / L2_INVARIANT).
 * 2. Post-Tool Execution Observation & Milestone Detection (`tools/post-execute`):
 *    Tracks file mutations (`write`, `edit`, `str_replace_editor`, `apply_patch`) and verification commands
 *    (`bash`, `pwsh`). Injects L2 repair hints on errors and `<tmem_milestone_reached>` when `MUTATE -> VERIFY(0)` completes.
 * 3. Native `/tmem` Slash Command (`commands.register`):
 *    Registers `/tmem [status|list|forget|gc|inspect|replay|diff|sync] [<intent|trace_id>]` directly in `dsh-commands`.
 */

import { spawnSync } from "node:child_process";
import { randomUUID } from "node:crypto";
import { existsSync } from "node:fs";
import { dirname, join, resolve } from "node:path";
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
  const workspaceRoot = resolve(options.workspaceRoot || process.cwd());
  const pythonBin = options.pythonBin || process.env.TMEM_PYTHON || "python3";
  const pythonPathRoot = resolve(options.tmemRoot || resolveDefaultTmemRoot(workspaceRoot));
  const env = {
    ...process.env,
    PYTHONPATH: process.env.PYTHONPATH
      ? `${pythonPathRoot}:${process.env.PYTHONPATH}`
      : pythonPathRoot,
  };

  try {
    const res = spawnSync(pythonBin, ["-m", "tmem.cli", "hook", "dsh", event], {
      cwd: workspaceRoot,
      env,
      input: JSON.stringify({ workspaceRoot, ...payload }),
      encoding: "utf-8",
      timeout: 5000,
    });
    if (res.status === 0 && res.stdout) {
      const trimmed = res.stdout.trim();
      if (trimmed) {
        return JSON.parse(trimmed);
      }
    }
  } catch (_err) {
    // Fail-open: never disrupt the host dsh agent loop
  }
  return {};
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
          const out = invokeTmemHook(
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
    const out = invokeTmemHook(
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
    const out = invokeTmemHook(
      "post_tool_use",
      {
        sessionId,
        toolCall: {
          name: exec?.name || "",
          args: parsedArgs,
        },
        resultText,
        isError,
        exitCode,
      },
      { workspaceRoot, tmemRoot, pythonBin }
    );

    if (out && typeof out.additionalContext === "string" && out.additionalContext.trim()) {
      const notice = createNoticeMessage(out.additionalContext.trim(), "tmem-milestone");
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
