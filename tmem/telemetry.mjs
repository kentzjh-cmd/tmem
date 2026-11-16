/**
 * Runtime benefit telemetry (.tmem/telemetry.jsonl) for measuring real DSH effectiveness.
 */

import { appendFileSync, existsSync, mkdirSync, readFileSync } from "node:fs";
import path from "node:path";
import { logger } from "./index.mjs";

export const TELEMETRY_FILENAME = "telemetry.jsonl";

function telemetryPath(workspaceRoot) {
  return path.join(path.resolve(workspaceRoot), ".tmem", TELEMETRY_FILENAME);
}

export function recordTelemetryEvent(workspaceRoot, eventType, data) {
  const filePath = telemetryPath(workspaceRoot);
  try {
    mkdirSync(path.dirname(filePath), { recursive: true });
    const payload = {
      ts: new Date().toISOString(),
      event: eventType,
      ...data,
    };
    appendFileSync(filePath, JSON.stringify(payload) + "\n", "utf-8");
  } catch (err) {
    logger.debug("Failed to write telemetry event:", err?.message || err);
  }
}

export function loadTelemetryEvents(workspaceRoot, limit = 2000) {
  const filePath = telemetryPath(workspaceRoot);
  if (!existsSync(filePath)) return [];
  const events = [];
  try {
    const content = readFileSync(filePath, "utf-8");
    for (const line of content.split(/\r?\n/)) {
      const s = line.trim();
      if (!s) continue;
      try {
        events.push(JSON.parse(s));
      } catch (_err) {
        // ignore malformed line
      }
    }
  } catch (_err) {
    return [];
  }
  if (limit > 0 && events.length > limit) {
    return events.slice(-limit);
  }
  return events;
}

export function computeTelemetrySummary(workspaceRoot) {
  const events = loadTelemetryEvents(workspaceRoot);
  let l2Injections = 0;
  let l2ResolvedGreen = 0;
  let l2Unresolved = 0;
  let l1Hints = 0;
  let l1Adopted = 0;
  let l1AdoptedEdit = 0;
  let l1InspectedOnly = 0;
  let l1Ignored = 0;

  for (const ev of events) {
    const etype = ev.event;
    if (etype === "l2_injected") {
      l2Injections += 1;
    } else if (etype === "l2_outcome") {
      if (ev.resolved) l2ResolvedGreen += 1;
      else l2Unresolved += 1;
    } else if (etype === "l1_cochange_hinted") {
      l1Hints += 1;
    } else if (etype === "l1_outcome") {
      if (ev.adopted) {
        l1Adopted += 1;
        if (ev.resolution === "inspected") l1InspectedOnly += 1;
        else l1AdoptedEdit += 1;
      } else {
        l1Ignored += 1;
      }
    }
  }

  const l2Total = l2ResolvedGreen + l2Unresolved;
  const l1Total = l1Adopted + l1Ignored;

  return {
    total_events: events.length,
    l2_injections: l2Injections,
    l2_resolved_green: l2ResolvedGreen,
    l2_unresolved: l2Unresolved,
    l2_resolution_rate:
      l2Total > 0 ? Math.round((l2ResolvedGreen / l2Total) * 1000) / 1000 : null,
    l1_hints: l1Hints,
    l1_adopted: l1Adopted,
    l1_adopted_edit: l1AdoptedEdit,
    l1_inspected_only: l1InspectedOnly,
    l1_ignored: l1Ignored,
    l1_adoption_rate:
      l1Total > 0 ? Math.round((l1Adopted / l1Total) * 1000) / 1000 : null,
  };
}
