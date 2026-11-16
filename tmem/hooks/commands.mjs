/**
 * Shared slash-command and argument parsers for tmem DSH hooks.
 */

export const SLASH_TMEM_RE =
  /^\/(?:tmem|remember|trace)(?:\s+(project|status|list|forget|gc|bench|evaluate|inspect|replay|diff|sync|telemetry))?(?:\s+(.+))?$/is;

export function parseSlashTmemCommand(userInput) {
  const m = String(userInput || "").trim().match(SLASH_TMEM_RE);
  if (!m) return null;
  const actionMode = (m[1] || "project").toLowerCase();
  const customIntent = (m[2] || "").trim();
  return [actionMode, customIntent];
}

export function parseInspectArgs(raw) {
  const parts = String(raw || "")
    .trim()
    .split(/\s+/)
    .filter(Boolean);
  let tid = "";
  let level = "playbook";
  let i = 0;
  while (i < parts.length) {
    const p = parts[i];
    if ((p === "--level" || p === "-l") && i + 1 < parts.length) {
      level = parts[i + 1].toLowerCase();
      i += 2;
    } else if (["index", "playbook", "raw", "diff"].includes(p)) {
      level = p;
      i += 1;
    } else if (!tid && !p.startsWith("-")) {
      tid = p;
      i += 1;
    } else {
      i += 1;
    }
  }
  return [tid, level];
}
