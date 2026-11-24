#!/usr/bin/env python3
"""Shared helpers for the archived sample trajectories.

The trajectories under `research/*/results/raw/` are the primary evidence: the
raw `session.v4.jsonl.zstd` transcripts of the agent sessions that produced the
published numbers.  These helpers define, once, how a transcript is turned into
the facts the reports publish, so that the archiver and the verifier cannot
drift apart.

The chain of custody is:

    session.v4.jsonl.zstd
        -> trajectory digest (events, tool calls, workspace root)
        -> raw_manifest.json (sha256 of every file)
        -> per-session record inside the machine verdict
        -> aggregate / gate values
        -> the published prose

Every link is checked by `research/carrier-ablation/verify_report_numbers.py`.
"""

from __future__ import annotations

import hashlib
import json
import subprocess
from pathlib import Path

# tool calls that write into the workspace
WRITE_TOOLS = ("edit", "write", "multiedit", "notebookedit")
# path fragments that are memory-store internals rather than user code edits
TMEM_MANAGED = ".tmem/"


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def read_events(zst: Path) -> list[dict]:
    """Decode a zstd-compressed session transcript into events."""
    out = subprocess.run(["zstd", "-dc", str(zst)], capture_output=True)
    if out.returncode != 0:
        raise RuntimeError(f"zstd failed on {zst}: {out.stderr.decode('utf-8', 'replace')[:200]}")
    events = []
    for line in out.stdout.decode("utf-8", "replace").split("\n"):
        if not line.strip():
            continue
        try:
            events.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return events


def workspace_root(events: list[dict]) -> str | None:
    """The workspace the agent worked in, as recorded by the session event."""
    for e in events:
        if e.get("type") == "session" and isinstance(e.get("cwd"), str):
            return e["cwd"]
    return None


def _call_path(data: dict) -> str | None:
    """The file a tool call targeted, if any."""
    raw = data.get("arguments")
    args: dict = {}
    if isinstance(raw, str) and raw.strip():
        try:
            args = json.loads(raw)
        except json.JSONDecodeError:
            return None
    elif isinstance(raw, dict):
        args = raw
    for key in ("file_path", "filePath", "path", "target_file"):
        val = args.get(key)
        if isinstance(val, str) and val:
            return val
    return None


def edited_files(events: list[dict], root: str | None = None) -> list[str]:
    """Workspace-relative files the session wrote to, sorted and de-duplicated.

    Memory-store internals (`<root>/.tmem/...`) are excluded: the plugin writes
    them, not the agent's code changes, and the verdict's `session_edited_files`
    counts only the latter.
    """
    if root is None:
        root = workspace_root(events)
    found: set[str] = set()
    for e in events:
        if e.get("type") != "tool/call":
            continue
        data = e.get("data")
        if not isinstance(data, dict) or data.get("name") not in WRITE_TOOLS:
            continue
        path = _call_path(data)
        if not path:
            continue
        # only files inside the workspace count: scratch files elsewhere (e.g.
        # /tmp) are not part of the session's edits to the project
        if not root or not path.startswith(root):
            continue
        rel = path[len(root):].lstrip("/")
        if rel.startswith(TMEM_MANAGED) or "/.tmem/" in rel:
            continue
        found.add(rel)
    return sorted(found)


def digest_events(events: list[dict]) -> dict:
    """The trajectory-level facts the published numbers are derived from."""
    tool_calls = [
        e["data"].get("name")
        for e in events
        if e.get("type") == "tool/call" and isinstance(e.get("data"), dict)
    ]
    counts: dict[str, int] = {}
    for e in events:
        counts[e.get("type", "?")] = counts.get(e.get("type", "?"), 0) + 1
    return {
        "events": len(events),
        "tool_calls": tool_calls,
        "tmem_tool_calls": [n for n in tool_calls if isinstance(n, str) and n.startswith("tmem_")],
        "user_messages": counts.get("user/message", 0),
        "assistant_messages": counts.get("assistant/message", 0),
        "event_types": dict(sorted(counts.items())),
    }
