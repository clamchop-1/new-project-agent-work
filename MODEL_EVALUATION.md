# Local model evaluation record

## First controlled task — 2026-11-14

This is a small single-sample tool-use comparison, not a general model ranking. All three models used the same bounded read-only task on `LocalAgentRepairFixture`, the same local Ollama endpoint, and the same already-installed models. The fixture's source files and trusted test file were only read; the pinned test command was the only execution capability used. No permit was issued and no cloud fallback occurred.

Task: read `AGENTS.md`, `README.md`, `calculator.py`, `stats.py`, and `tests/test_stats.py`; run the approved pinned tests; report marker `MAPLE-427`; summarize observed behavior. Each run had the default 16,384-token context and 16-step limit.

| Model | Outcome | Pinned tests | Steps | Wall time | Prompt / completion tokens | Context peak |
|---|---|---:|---:|---:|---:|---:|
| `qwen3.5:4b` | Completed and receipt-verified | 3 passed | 3 | 7.27 s | 8,449 / 378 | 21.2% |
| `qwen2.5-coder:7b-instruct` | Repeated unhandled response | Not run | 10 | 8.53 s | 21,739 / 374 | 15.9% |
| `qwen3:8b` | Completed and receipt-verified | 3 passed | 7 | 20.78 s | 15,060 / 286 | 18.4% |

The 7B trace shows it read `AGENTS.md`, then repeatedly asked for file paths despite already receiving them, with some malformed or unparsed text-form tool requests. It stopped before testing. This is a tool-following failure on this prompt, not evidence that the model is generally inferior. The 8B model used more time than 4B on this small task. One sample per model is too little to infer stable success rates.

Saved evidence:

- 4B: `.local/runs/20261113T235745648277Z-06ac8803/execution/`
- 7B coder: `.local/runs/20261114T000202047583Z-9e428753/execution/`
- 8B: `.local/runs/20261114T000245349980Z-10c51770/execution/`

Report command:

```powershell
& 'C:\Users\dsmus\AppData\Local\Programs\Python\Python314\python.exe' `
  'D:\Workspaces\LocalCodingAgent\model_evaluation.py' `
  'D:\Workspaces\LocalCodingAgent\.local\runs\20261113T235745648277Z-06ac8803\execution' `
  'D:\Workspaces\LocalCodingAgent\.local\runs\20261114T000202047583Z-9e428753\execution' `
  'D:\Workspaces\LocalCodingAgent\.local\runs\20261114T000245349980Z-10c51770\execution'
```

For useful rates, repeat the same tasks several times per model and add separate tasks for repair, tool-schema recovery, review, and concise documentation. Keep model settings, task text, fixture commit, and test hash fixed. Report task success and tool-call accuracy separately from latency/token costs. Do not infer GPU memory use from token metrics; collect resource measurements separately on the actual host.

## Step 57 path-presentation follow-up — 2026-11-14

The presentation-only fix in `local_agent.py` now copies known tool-result path fields and converts contained, validated paths to project-relative form for model messages. Audit events and command receipts retain their original paths; source contents, hashes, errors, parser strictness, and authorization are unchanged. Eleven new regression tests pass; the complete suite ran 133 tests with one existing Windows symlink-privilege skip and no failures/errors.

The read-only Qwen 7B rerun used the exact task from `20261114T155007290149Z-13424e8d`, the same model digest and default settings, and no permit or cloud fallback. New run: `.local/runs/20261114T224800567306Z-fca4a420/execution/`.

The actual model-facing read receipt contained `path: AGENTS.md` with the original SHA-256 and source text. The raw audit event retained the absolute path. Qwen then generated fabricated `<tool_response>` blocks with `sha256: unknown` instead of real read requests. The strict parser rejected those blocks. Result: `repeated_unhandled_response`, 5 steps, one real AGENTS.md read, tests not run, no final summary, 10.23 seconds, 9,935 prompt and 511 completion tokens. The reference also made only one real read and ran no tests (6.05 seconds, 9,668 / 149 tokens), but produced conversational responses and prose-wrapped JSON rather than fabricated result blocks.

The path presentation behavior is validated; autonomous continuation is **not fixed** by this change alone. One run does not establish a reliability rate. Source/private fixture hashes and the reference-run history were unchanged. Existing `M calculator.py` and `?? stats.py` were preserved.

Detailed evidence and next diagnostic proposal: `.local/step57/STEP57_REPORT.md`, `.local/step57/model-tool-messages.jsonl`, and `.local/step57/live-validation.json`. The future worker-pipeline draft is `.local/step57/PIPELINE_PLAN.md`; no pipeline architecture was implemented.

## Qwen continuation follow-up — 2026-11-14

Step 57's bounded read-only acceptance task now passes in two consecutive runs after two small follow-up changes: retain strictly parsed and validated text-form requests as text in conversation history instead of reconstructing them as native tool calls; give an explicit complete-JSON finish example and require requested observations in the summary. Native calls retain native history. The parser, actual execution arguments, broker authorization and raw audit receipts remain unchanged.

Exact captured-message replays reproduced fabricated results for reconstructed native history and real README read requests for text history. A history-only full loop then performed all five real reads and passed the pinned tests but emitted malformed finish JSON. That response remained rejected. The complete-JSON instruction corrected the tested completion behavior without repairing or accepting invalid JSON.

Final runs: `20261114T230240415856Z-2bce3baf` (7.25 seconds) and `20261114T230249351192Z-4246efd3` (4.34 seconds). Both read AGENTS.md, README.md, calculator.py, stats.py, and tests/test_stats.py; each ran three pinned tests successfully; each returned a valid final summary including MAPLE-427 and classified `completed_verified`. Each used 8 steps and 19,680 prompt / 311 completion tokens. Same model/digest, exact reference task/settings, no write permit or cloud fallback. These two samples validate the small fixture task, not general model reliability.

Final suite: 140 tests run, one existing Windows symlink-privilege skip, no failures/errors. Seven history regressions supplement the eleven path-presentation regressions. Fixture source/private data and original reference history stayed unchanged. Earlier incomplete trials remain preserved.

Local details: `.local/step58/QWEN_CONTINUATION_REPORT.md`, `.local/step58/live-validation-e55211888c6f.json`, and exact model-message/replay captures in the same directory. Cross-platform architecture assessment and migration proposal: `ARCHITECTURE_ASSESSMENT.md`. No architecture implementation, commit, or push occurred.

## Windows continuation comparison — 2026-11-15

All runs used the same bounded read-only task on `LocalAgentRepairFixture`: read the five named files, run the pinned tests, report `MAPLE-427`, and make no edits. The fixture began and ended with the existing `M calculator.py` and `?? stats.py` status. All listed runs used local Ollama, no write permit, and no cloud fallback.

| Model / run | Tools and tests | Marker in final summary | Agent time | Assessment |
|---|---|---:|---:|---|
| `qwen2.5-coder:7b-instruct` / `20261115T004026683726Z-98756c18` | Five reads; 3/3 tests | Yes | 7.41 s | Task requirements reported |
| `qwen2.5-coder:7b-instruct` / `20261115T010627642818Z-fde3016c` | Five reads; 3/3 tests | No; despite marker being present in the AGENTS.md receipt | 7.28 s | Objective-reporting miss |
| `qwen2.5-coder:7b-instruct` / `20261115T010845789663Z-02ba678a` | Five reads; 3/3 tests | No; despite marker being present in the AGENTS.md receipt | 7.94 s | Objective-reporting miss |
| `qwen3:8b` / `20261115T010707679742Z-ae7e1d87` | Five reads; 3/3 tests | Yes | 36.09 s | Task requirements reported |

The Qwen2.5 runs all produced valid tool calls and passing test receipts, but only one of these three summaries reported the requested marker. Both observed Qwen3:8B runs (including the earlier 20.78-second run above) reported it. These are small samples and do not establish general reliability. `completed_verified` currently reflects the passing test receipt; it does not prove every natural-language task requirement was satisfied. Inspect the actual read and test receipts when evaluating task completion.

The no-config fallback is now `qwen3:8b`. Qwen3:8B followed the marker instruction in two read-only runs and completed the authorized edit/test trial below. Qwen2.5-Coder is retained as the coding-router fallback because it is faster on the bounded fixture, but it omitted the requested marker in two of three read-only samples. Qwen3.5 is excluded from automatic routing after repeated invalid native finish arguments. These are small, task-specific samples; the default remains subject to broader evaluation.
## Authorized Qwen3:8B edit trial - 2026-11-15

A fresh Git fixture under `.local/tests/repair-trial-20261115T011541499861Z/repo` received a single-use permit bound to its initial Git state and limited to `calculator.py` and `stats.py`. Qwen3:8B repaired `average`, created `clamp`, and used the pinned-test capability three times: the first run exposed the remaining implementation issues, the next exposed the missing `stats.py`, and the final run passed all three tests. It reported `MAPLE-427`; the verified result is `completed_verified` after 12 steps and 40.94 seconds, with no unpermitted writes or cloud fallback. The disposable repo's only source changes are the two permitted files. Evidence: `.local/runs/20261115T011554549282Z-fa75773a/execution/`.

An earlier trial under `.local/host-validation/` correctly had no pinned-test capability because that path is outside the approved fixture layout; it stopped as `completed_not_tested`. It is preserved at `.local/runs/20261115T011423653157Z-7d690a4a/execution/` and is not counted as a coding pass.

## Two-task read-only comparison — 2026-11-15 UTC

This adds one run of each selected model on each of two read-only tasks. Both used `LocalAgentRepairFixture`, the same local Ollama endpoint, 16,384-token context, the exact pinned test file/hash above, no write permit, and no cloud fallback. The first repeated the acceptance prompt; the second additionally required the final summary to report the seven expected values or exception behavior asserted by the tests. These six runs are still a small task-specific sample, not a general ranking.

| Task | Model / run | Reads / tests | Final report | Classification / time |
|---|---|---|---|---|
| Acceptance | `qwen3:8b` / `20261115T014040884412Z-077bc3b4` | 4 of 5 requested files; pinned tests passed | Reported MAPLE-427; omitted `stats.py` from reads and summary | `completed_verified`, 22.79 s |
| Acceptance | `qwen2.5-coder:7b-instruct` / `20261115T014117103296Z-397d9eef` | 5 of 5 requested files; pinned tests passed | Reported MAPLE-427; summary only described the tests and marker | `completed_verified`, 7.49 s |
| Acceptance | `qwen3.5:4b` / `20261115T014131452538Z-5ec040b2` | 5 of 5 requested files; pinned tests passed | No valid final summary; agent rejected the next native call with `Unexpected native tool argument.` | `execution_failed`, 8.88 s |
| Value report | `qwen3:8b` / `20261115T014200737136Z-3ec8744d` | 4 of 5 requested files; pinned tests passed | Reported MAPLE-427 and all seven expected outcomes correctly; omitted `stats.py` from reads | `completed_verified`, 40.49 s |
| Value report | `qwen2.5-coder:7b-instruct` / `20261115T014252944031Z-fdb02e51` | 5 of 5 requested files; pinned tests passed | Reported all seven expected outcomes correctly, but omitted MAPLE-427 | `completed_verified`, 8.49 s |
| Value report | `qwen3.5:4b` / `20261115T014400401802Z-7decfd8e` | 5 of 5 requested files; pinned tests passed | No valid final summary; agent again rejected the next native call with `Unexpected native tool argument.` | `execution_failed`, 9.29 s |

The verified test receipt was `passed` in all six runs, but that does not make the two Qwen3.5 runs completed tasks: both ended with `execution_failed` and no accepted finish call. Qwen3:8B completed both tasks and reported the requested marker, while its receipts show it skipped the requested `stats.py` read in both. Qwen2.5-Coder completed both and read every requested file; it reported the marker on the acceptance task but omitted it on the value-report task. Its expected-value statements were correct. This separates tool/test completion, required reads, and objective reporting instead of treating `completed_verified` as proof of every prompt requirement.

All six run records have valid receipt integrity, zero cloud fallback, and no authorized write paths. Each run's before/after fixture snapshot matches: `M calculator.py` and `?? stats.py` remained unchanged. Per-model summary from `model_evaluation.py`: Qwen3:8B used 63.29 s total (31.64 s average), 35,362 prompt and 747 completion tokens, with 19.3% peak context utilization; Qwen2.5-Coder used 15.98 s total (7.99 s average), 33,785 prompt and 651 completion tokens, with 20.0% peak utilization; Qwen3.5 used 18.18 s total (9.09 s average), 21,558 prompt and 917 completion tokens, with 24.0% peak utilization. Token metrics are recorded by the local agent; no hardware resource measurements were made.

The prompt and response records, tool receipts, result classifications, and pinned-test output for each run are preserved in its `.local/runs/<run-id>/execution/` directory. The repeated Qwen3.5 validation error and Qwen2.5-Coder's variable marker reporting remain observed limitations. No recovery or routing behavior was changed during this comparison.

## Native argument compatibility and repeat checks - 2026-11-15

Inspection of both failed Qwen3.5 traces found the same malformed native-call shape: the model placed a JSON object inside an extra `arguments` property, for example `function.arguments = {"arguments":"{...}"}`. One run wrapped `finish`; the other wrapped the empty arguments for `list_files`. The strict validator rejected both with `Unexpected native tool argument`.

`local_agent.py` now unwraps exactly one such property only when `arguments` is not a declared parameter of that tool, the value is a bounded JSON string, and it decodes to an object. The decoded object still passes the unchanged required-field, type, extra-field, and size checks. Native validation regressions: 12 passed, including valid unwrapping, extra-field rejection, missing required fields, and non-object rejection.

The previously skipped Windows symbolic-link regression now passed on this host: `test_symbolic_link_path_is_rejected` completed without a skip. The test created a symlink and verified the project path guard rejected it. The full Python 3.14 suite also passed all 169 tests, including both this symbolic-link check and `test_windows_junction_path_is_rejected`, with no skips, failures, or errors (73.02 seconds).

An end-to-end Qwen3.5 acceptance rerun used the registered `LocalAgentRepairFixture`, its unchanged pinned test file/hash, no write permit, and the loopback Ollama endpoint. Run `20261115T180232877235Z-fe52fc76` returned the same wrapped native `finish` shape; the compatibility path accepted it after schema validation. The run read all five requested files, passed all three pinned tests with a valid receipt, reported `MAPLE-427`, and finished as `completed_verified` in 8.61 seconds (3 steps, 8,622 prompt / 365 completion tokens; 21.3% peak context). No write or cloud fallback occurred. Before and after fixture state remained `M calculator.py` and `?? stats.py`.

The exact Qwen2.5-Coder value-report task was repeated in run `20261115T180331981760Z-6d5ab21a`. It again read all five requested files and passed the three pinned tests with valid receipt integrity, but omitted `MAPLE-427` from its final summary. Across the six saved Qwen2.5-Coder read-only samples in this report, the marker appears in two summaries and is omitted in four; this small diagnostic sample does not imply a general failure rate. Its requested test-value statements were correct in the value-report trials. This remains an objective-reporting limitation; no prompt or routing changes were made from one more sample.

Evidence is preserved under each run's `.local/runs/<run-id>/execution/` directory. The repair-fixture source and test files, root project data, and local Ollama configuration were not modified by either model run. No commit or push was made.

## Fresh PC coding acceptance trial - 2026-11-15

The reusable local harness `.local/pc_acceptance_trial.py` created a fresh Git fixture from `fixture_repo.make_fixture`, bound a single-use approval record to that fixture's initial project and memory state, and authorized only `calculator.py` and `stats.py`. It used the default local coding model `qwen3:8b`, loopback Ollama, a 16,384-token context, and the fixture's hash-pinned tests.

Run `20261115T181243078088Z-f7a2e053` completed in 10 steps and 56.81 seconds. Qwen3:8B corrected `calculator.average`, created `stats.clamp`, passed all three pinned tests with valid receipt integrity, and reported `MAPLE-427`. The result was `completed_verified`; all six independent harness checks passed: clean starting state, only approved files changed, reported writes within approval, pinned tests passed, receipts valid, and no cloud fallback. It used 28,284 prompt and 832 completion tokens, with 24.9% peak context utilization.

The fixture began at seed commit `b40f611ab235c09891e5cd0a5840f802ebf44b99` and ended with exactly `M calculator.py` and `?? stats.py`. No test, instruction, or other fixture files changed. The dedicated fixture, approval, execution receipts, and final manifest are preserved under `.local/tests/pc-acceptance-20261115T181243078088Z-f7a2e053/` and `.local/runs/20261115T181243078088Z-f7a2e053/`. This is one successful coding acceptance sample, not a reliability estimate.
