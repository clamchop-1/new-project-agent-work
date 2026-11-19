# Phase 2D.1 evidence audit

The Phase 2D input is the preserved report in `.local/runs/phase2d-0c1d32bc4330/`. Each trace listed here is relative to that directory. The archived report and scoring audit agree on the report hash. Rechecking the saved receipts found eight current passing suites and no out-of-scope file changes.

| Scenario | Evidence | Primary cause or control | Correction under test |
|---|---|---|---|
| Single-file | `single-file/execution/events.jsonl` | Passing repair and valid completion | Regression control |
| Multi-file | `multi-file/execution/events.jsonl:7` | Benign duplicate blocked completion. Only the baseline and final tests ran. | Reuse discovery and score receipt order |
| Test generation | `test-generation/execution/events.jsonl:16` | Correct assertions passed before the next inference was cancelled | Show verified progress and log the cancelled attempt |
| Refactor | `refactor/execution/events.jsonl:15` | Passing repair with two earlier guessed paths. The original rubric only checked for a keyword. | Check the local variable with AST and retain the control |
| API contract | `api-contract/execution/events.jsonl:1` | First response took 39.11 seconds. Three redundant reads followed before cancellation. | Provide bounded progress and measure inference separately |
| Error handling | `error-handling/execution/events.jsonl:5` | The model guessed paths and proposed an unavailable creation tool. It was rejected before dispatch. | Expose active tools and guide source discovery |
| Configuration | `configuration/execution/events.jsonl:13` | Edit and tests passed. The next inference was cancelled. | Surface completion readiness and record the cancelled attempt |
| Unfamiliar | `unfamiliar/execution/events.jsonl:9` | Duplicate test request blocked completion. Expected values had different formatting. | Reuse the current verified receipt and score formatting separately |
| Tool cycles | `tool-cycles/execution/events.jsonl:15` | Restricted executor rejected the submitted source. The model still called it verified. | Keep the restriction and show failed verification as outstanding |
| No change | `no-change/execution/events.jsonl:12` | Correct conclusion, no edits, and passing tests. The exact requested marker was absent. | Record semantic acceptance and formatting separately |
| Insufficient | `insufficient/execution/events.jsonl:7` | The original scorer accepted any unfinished text. It accepted a restatement of current values in place of missing inputs. | Require both missing replacement inputs and an explicit clarification request |

All three deadline cases had an unreturned inference attempt. Pinned test execution accounted for 0.13 to 0.46 seconds per scenario. The API contract case recorded 32.33 seconds of Ollama generation across returned responses. The archived logs do not isolate the cancelled attempts, broker overhead, or SSH transport. Context utilization peaked at about 40 percent, with no evidence of context exhaustion.

The paired benchmark uses fresh fixtures, the configured Mac model, the same tool and write grants, the same context and step limits, and a 60-second wall budget. A separate prompt comparison toggles the host progress message on the revised runtime. A single live pair can show observed differences without establishing a repeatable improvement rate.
