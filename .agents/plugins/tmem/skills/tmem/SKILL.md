---
name: tmem
description: >-
  Capture, slice, or query deterministic execution trace memories at project (.tmem/) scope.
  Activate when the user types /tmem, /remember, or confirms saving the current session's effective execution path.
---

# tmem Execution Trace Memory Skill

## Commands

1. **Save Memory Trace**:
   `PYTHONPATH=/home/ubuntu/proj /usr/bin/python3 -m tmem.cli capture --workspace /home/ubuntu/proj`
2. **Check Memory Status**:
   `PYTHONPATH=/home/ubuntu/proj /usr/bin/python3 -m tmem.cli status --workspace /home/ubuntu/proj`
