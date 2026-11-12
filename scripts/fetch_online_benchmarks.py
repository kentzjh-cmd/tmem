#!/usr/bin/env python3
"""
Fetch 20+ Real-World Coding Agent Trajectories from Public Benchmark S3 Archive.
Dataset: SWE-bench Lite Official SWE-agent + GPT-4 Evaluation Runs.
"""

import os
import sys
import urllib.request
from pathlib import Path

S3_BASE_URL = "https://swe-bench-submissions.s3.amazonaws.com/lite/20240402_sweagent_gpt4/trajs"

SELECTED_TRAJECTORIES = [
    # Astropy (2)
    "astropy__astropy-14182.traj",
    "astropy__astropy-14365.traj",
    # Django (5)
    "django__django-10914.traj",
    "django__django-10924.traj",
    "django__django-11001.traj",
    "django__django-11019.traj",
    "django__django-11039.traj",
    # Matplotlib (3)
    "matplotlib__matplotlib-23913.traj",
    "matplotlib__matplotlib-23964.traj",
    "matplotlib__matplotlib-24149.traj",
    # Requests (2)
    "psf__requests-2148.traj",
    "psf__requests-2674.traj",
    # Xarray (2)
    "pydata__xarray-3364.traj",
    "pydata__xarray-4094.traj",
    # Pylint (2)
    "pylint-dev__pylint-5859.traj",
    "pylint-dev__pylint-7080.traj",
    # Pytest (2)
    "pytest-dev__pytest-5221.traj",
    "pytest-dev__pytest-7168.traj",
    # Scikit-learn (2)
    "scikit-learn__scikit-learn-10508.traj",
    "scikit-learn__scikit-learn-11040.traj",
    # Sphinx (1)
    "sphinx-doc__sphinx-8273.traj",
    # Sympy (1)
    "sympy__sympy-22005.traj",
]

def fetch_trajectories(dest_dir: str = "benchmarks/data/online_20") -> None:
    out_path = Path(dest_dir)
    out_path.mkdir(parents=True, exist_ok=True)
    print(f"Downloading {len(SELECTED_TRAJECTORIES)} trajectories into {out_path.resolve()}...")

    total_bytes = 0
    for idx, filename in enumerate(SELECTED_TRAJECTORIES, start=1):
        target_file = out_path / filename
        if target_file.exists() and target_file.stat().st_size > 1000:
            print(f"[{idx}/{len(SELECTED_TRAJECTORIES)}] Cached: {filename} ({target_file.stat().st_size} bytes)")
            total_bytes += target_file.stat().st_size
            continue

        url = f"{S3_BASE_URL}/{filename}"
        print(f"[{idx}/{len(SELECTED_TRAJECTORIES)}] Fetching {url} -> {filename} ...", end=" ", flush=True)
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "tmem-benchmark/1.0"})
            with urllib.request.urlopen(req, timeout=30) as resp:
                data = resp.read()
                target_file.write_bytes(data)
                total_bytes += len(data)
                print(f"DONE ({len(data)} bytes)")
        except Exception as e:
            print(f"FAILED: {e}")
            sys.exit(1)

    print(f"\nSuccessfully fetched {len(SELECTED_TRAJECTORIES)} trajectories. Total size: {total_bytes / (1024*1024):.2f} MB")

if __name__ == "__main__":
    fetch_trajectories()
