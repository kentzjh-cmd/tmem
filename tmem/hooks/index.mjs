/**
 * Agent hook bridges and slash-command handlers for tmem.
 */

export {
  SLASH_TMEM_RE,
  parseInspectArgs,
  parseSlashTmemCommand,
} from "./commands.mjs";
export { handleDshHook, registerDefaultCaptureFn } from "./dsh.mjs";
export {
  LOOP_STREAK_THRESHOLD,
  applySessionTraceFeedback,
  defaultRuntimeState,
  loadRuntimeState,
  recordToolOutcomeForLoop,
  saveRuntimeState,
} from "./state.mjs";
