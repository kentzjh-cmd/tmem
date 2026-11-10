# tmem Speculative Execution Memory & Milestone Protocol

## 1. Consuming Recalled Memory (`<tmem_speculative_memory>`)
- **L0_FULL (Guards Passed)**: Follow the verified `L0 Critical Path` directly instead of performing redundant exploratory `grep`/`find` scans.
- **L1_TOPOLOGY**: Respect the co-change file contract and run the specified verification command before finishing.
- **L2_INVARIANT**: When encountering a matching error signature, apply the known causal repair action first.

## 2. Goal Completion & `/tmem` Memory Persistence
- **Proactive Milestone Prompt**: When you complete a non-trivial coding, debugging, or environment setup goal (or when `<tmem_milestone_reached>` is injected), append a concise one-line prompt at the end of your response asking whether to persist the memory trace:
  > 💡 *是否需要将本次有效执行路径沉淀为记忆？（可输入 `/tmem project` 存项目级、`/tmem global` 存全局级、或 `/tmem both` 同时保存）*
- **Executing Persistence**: If the user asks to save/record memory via natural language or `/tmem`, run:
  `PYTHONPATH=/home/ubuntu/proj /usr/bin/python3 -m tmem.cli capture --agent dsh --scope <project|global|both> --workspace /home/ubuntu/proj`
