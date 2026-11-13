# tmem Deterministic Engineering Memory & Milestone Protocol (DSH)

## 1. Consuming Recalled Memory (`MEMORIES.md`, L2 Runtime Repairs & L1 Co-Change Hints)
- **Living Memory Index (`MEMORIES.md`)**: When navigating or planning modifications in this repository, consult `MEMORIES.md` (or `MEM.md`) for verified execution paths, recurring co-change hints, and known error repairs.
- **L0_FULL (Guards Passed)**: Treat verified critical paths as clues; verify context before execution rather than blindly skipping exploration.
- **L1_TOPOLOGY (`<tmem_cochange_hint>`)**: Treat recurring co-change hints as structural reminders and inspect partner files / run the specified verification command before finishing.
- **L2_INVARIANT**: When encountering a matching error signature via post-execute interception, apply the known causal repair action first.
- **Progressive Disclosure**: Detailed playbooks are in `.tmem/traces/*.json`; full forensic step replay is in `.tmem/raw/*.raw.json` (`python3 -m tmem.cli inspect <id> --level raw`).

## 2. Goal Completion & `/tmem` Memory Persistence
- **Proactive Milestone Prompt**: When you complete a non-trivial coding, debugging, or environment setup goal (or when `<tmem_milestone_reached>` is injected), append a concise one-line prompt at the end of your response asking whether to persist the memory trace:
  > 💡 *是否需要将本次有效执行路径沉淀为记忆？（输入 `/tmem` 或 `/remember [备注]` 保存）*
- **Executing Persistence**: If the user asks to save/record memory via natural language or `/tmem`, run:
  `python3 -m tmem.cli capture --agent dsh`
