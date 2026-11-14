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
