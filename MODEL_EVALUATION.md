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
