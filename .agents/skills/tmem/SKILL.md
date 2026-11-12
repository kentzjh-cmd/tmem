---
name: tmem
description: >-
  Capture, slice, or query deterministic execution trace memories at project (.tmem/) scope.
  Activate when the user types /tmem, /remember, or confirms saving the current session's effective execution path.
---

# tmem Execution Trace Memory Skill (dsh & Multi-Agent)

## Commands

1. **Save Memory Trace**:
   `python3 -m tmem.cli capture --agent dsh`
2. **Check Memory Status**:
   `python3 -m tmem.cli status`
