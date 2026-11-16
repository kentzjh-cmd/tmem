/**
 * Agent adapter registry for tmem (primary: dsh; offline benchmark support: swe_bench / atif).
 */

import { DshAdapter } from "./dsh.mjs";
import { SweBenchAdapter } from "./swe_bench.mjs";

const ADAPTER_REGISTRY = {
  dsh: DshAdapter,
  swe_bench: SweBenchAdapter,
  swe: SweBenchAdapter,
  atif: SweBenchAdapter,
};

export function getAdapter(agentName) {
  const key = String(agentName || "").trim().toLowerCase();
  const Cls = ADAPTER_REGISTRY[key];
  if (!Cls) {
    throw new Error(
      `Unsupported agent adapter '${agentName}'. Available: ${Object.keys(ADAPTER_REGISTRY).join(", ")}`
    );
  }
  return new Cls();
}

export { DshAdapter, SweBenchAdapter };
