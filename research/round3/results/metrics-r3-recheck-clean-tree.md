# Independent E2E metrics

| Gate | Hypothesis | Metric | Value | Threshold | Verdict |
| :--- | :--- | :--- | :--- | :--- | :--- |
| G1 | H1 | session_capture_rate | 0.833 | >= 0.85 | FAIL |
| G2 | H1 | multiframe_coverage | 1.0 | >= 0.80 | PASS |
| G3 | H2 | session_edit_recall / session_scope_precision | recall=1.0, precision=0.908 | recall >= 0.80 and precision >= 0.50 | PASS |
| G4 | H3 | error_signature_recall | 1.0 | >= 0.66 | PASS |
| G5 | H4 | pair_recall/precision | recall=1.0, precision=1.0 | recall >= 0.80 and precision >= 0.80 | PASS |
| G6 | H5 | fact_recall_at_lookup / fact_recall_at_search | lookup=0.75, search=0.625 | >= 0.70 (主判据：lookup；search 作为对照） | PASS |
| G7 | H6 | keypoint_hit_rate(memory-only questions) | A_loose=1.0, B_loose=1.0, C_loose=1.0, delta_A_C_loose=0.0, A_strict=1.0, B_strict=1.0, C_strict=0.0, delta_A_C_strict=1.0, delta_A_B_strict=0.0 | A >= 0.60 and (A - C) >= 0.30 | FAIL |
| G8 | H7 | memory_block_injected AND fix_adopted | memory_block_injected=True, injected_block_carries_prior_repair=True, fix_pattern_present_in_new_location=True, l2_injected_telemetry_events=15, l1_cochange_hints=6, l1_outcomes=6 | injected AND block carries repair AND fix adopted (telemetry reported separately) | PASS |
| G9 | 环境隔离 | repo_tracked_dirty / new_dsh_files / credential_leaks | repo_tracked_dirty=[], authorized_local_fixes=[], repo_untracked=0, new_dsh_files=[], credential_leaks=[], session_elapsed_sec=[124.6, 171.3, 104.9, 200.5, 16.8, 41.5] | no tracked changes, no new DSH config files, no credential leak | PASS |

**7/9 gates passed** (failed: ['G1', 'G7'])
