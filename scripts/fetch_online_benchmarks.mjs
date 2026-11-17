#!/usr/bin/env node
/**
 * Fetch 20+ Real-World Coding Agent Trajectories from Public Benchmark S3 Archive (Node.js ESM).
 * Dataset: SWE-bench Lite Official SWE-agent + GPT-4 Evaluation Runs.
 */

import { existsSync, mkdirSync, statSync, writeFileSync } from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

const S3_BASE_URL =
  "https://swe-bench-submissions.s3.amazonaws.com/lite/20240402_sweagent_gpt4/trajs";

export const SELECTED_TRAJECTORIES = [
  // Astropy (2)
  "astropy__astropy-14182.traj",
  "astropy__astropy-14365.traj",
  // Django (5)
  "django__django-10914.traj",
  "django__django-10924.traj",
  "django__django-11001.traj",
  "django__django-11019.traj",
  "django__django-11039.traj",
  // Matplotlib (3)
  "matplotlib__matplotlib-23913.traj",
  "matplotlib__matplotlib-23964.traj",
  "matplotlib__matplotlib-24149.traj",
  // Requests (2)
  "psf__requests-2148.traj",
  "psf__requests-2674.traj",
  // Xarray (2)
  "pydata__xarray-3364.traj",
  "pydata__xarray-4094.traj",
  // Pylint (2)
  "pylint-dev__pylint-5859.traj",
  "pylint-dev__pylint-7080.traj",
  // Pytest (2)
  "pytest-dev__pytest-5221.traj",
  "pytest-dev__pytest-7168.traj",
  // Scikit-learn (2)
  "scikit-learn__scikit-learn-10508.traj",
  "scikit-learn__scikit-learn-11040.traj",
  // Sphinx (1)
  "sphinx-doc__sphinx-8273.traj",
  // Sympy (1)
  "sympy__sympy-22005.traj",
];

export async function fetchTrajectories(destDir = "benchmarks/data/online_20") {
  const outPath = path.resolve(destDir);
  mkdirSync(outPath, { recursive: true });
  console.log(`Downloading ${SELECTED_TRAJECTORIES.length} trajectories into ${outPath}...`);

  let totalBytes = 0;
  for (let idx = 0; idx < SELECTED_TRAJECTORIES.length; idx++) {
    const filename = SELECTED_TRAJECTORIES[idx];
    const targetFile = path.join(outPath, filename);
    if (existsSync(targetFile) && statSync(targetFile).size > 1000) {
      const sz = statSync(targetFile).size;
      console.log(`[${idx + 1}/${SELECTED_TRAJECTORIES.length}] Cached: ${filename} (${sz} bytes)`);
      totalBytes += sz;
      continue;
    }

    const url = `${S3_BASE_URL}/${filename}`;
    process.stdout.write(
      `[${idx + 1}/${SELECTED_TRAJECTORIES.length}] Fetching ${url} -> ${filename} ... `
    );
    try {
      const resp = await fetch(url, {
        headers: { "User-Agent": "tmem-benchmark/1.0" },
        signal: AbortSignal.timeout(30000),
      });
      if (!resp.ok) {
        throw new Error(`HTTP ${resp.status}`);
      }
      const data = Buffer.from(await resp.arrayBuffer());
      writeFileSync(targetFile, data);
      totalBytes += data.length;
      console.log(`DONE (${data.length} bytes)`);
    } catch (err) {
      console.log(`FAILED: ${err.message}`);
      process.exitCode = 1;
      return;
    }
  }

  console.log(
    `\nSuccessfully fetched ${SELECTED_TRAJECTORIES.length} trajectories. Total size: ${(
      totalBytes /
      (1024 * 1024)
    ).toFixed(2)} MB`
  );
}

const isEntrypoint =
  process.argv[1] &&
  path.resolve(process.argv[1]) === path.resolve(fileURLToPath(import.meta.url));
if (isEntrypoint) {
  await fetchTrajectories();
}
