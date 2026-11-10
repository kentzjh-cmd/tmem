---
name: tmem
description: >-
  Capture, slice, or query deterministic execution trace memories at project (.tmem/) or global (~/.tmem/) scope.
  Activate when the user types /tmem, /remember, or confirms saving the current session's effective execution path.
---

# tmem Execution Trace Memory Skill

## Commands

1. **Save to Project Tier (`.tmem/traces/`)**:
   `PYTHONPATH=/home/ubuntu/proj /usr/bin/python3 -m tmem.cli capture --scope project --workspace /home/ubuntu/proj`
2. **Save to Global Tier (`~/.tmem/playbooks/`)**:
   `PYTHONPATH=/home/ubuntu/proj /usr/bin/python3 -m tmem.cli capture --scope global --workspace /home/ubuntu/proj`
3. **Save to Both Project & Global Tiers**:
   `PYTHONPATH=/home/ubuntu/proj /usr/bin/python3 -m tmem.cli capture --scope both --workspace /home/ubuntu/proj`
4. **Check Memory Status**:
   `PYTHONPATH=/home/ubuntu/proj /usr/bin/python3 -m tmem.cli status --workspace /home/ubuntu/proj`
