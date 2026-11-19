# Recover an interrupted LocalCodingAgent run

Windows records the authorized scope, project snapshot, and a unique recovery ID in a private journal. Before a brokered project or assistant file write, it stores the intended before and after hashes. It records write evidence and pinned command receipt references before returning control to the model. The recovery lease is released by the OS when its owning process exits.

## Inspect and reconcile

Find the recovery ID in the run's `result.json` or in the startup error for an unresolved journal. Run these commands from the LocalCodingAgent repository using a registered project selector.

```powershell
.venv\Scripts\python.exe agent.py recovery-inspect --project PROJECT --recovery-run RUN_ID
.venv\Scripts\python.exe agent.py recovery-reconcile --project PROJECT --recovery-run RUN_ID --recovery-action accept --review-sha256 REVIEW_SHA256
```

Use the report's `review_sha256`. It binds the decision to the current project and journal evidence. Inspect again if evidence changes. The report includes the last recorded operation, pending intent, before and after hashes, current hashes, allowed scope, and command receipt verification.

- **Accept** records a resumable checkpoint when the observed project, writes, and available receipts agree. A write that matches its intended after hash can be acknowledged even if the original receipt was interrupted. The acknowledgment remains distinct from the original receipt.
- **Abort** closes the old run at a verified state. Existing edits and evidence stay in place.
- **Restore** applies a verified before image to the pending project file only after explicit approval. It keeps any displaced bytes in the private recovery directory. Inspect the result and decide separately whether to accept or abort.
- **Confirm-command-stopped** handles the narrow case where the runner exited before recording its child's PID. Use it only after confirming that no test process is still running. Inspect again afterward. A stopped command without a receipt remains unverified.

A partial write or unrelated change blocks accept and abort. Use a verified backup where available or reconcile the affected source manually. Journal or receipt corruption requires operator review. The CLI archives launcher locks only when they belong to the journal owner and the OS confirms that owner exited.

## Continue from a checkpoint

Start a fresh run with `--recovery-run RUN_ID`. Its result links to the accepted journal. For edits, first use the normal `approve` command with the same recovery ID, the new task file, and paths within the original grant. Then use that new permit with `run`. The launcher checks the permit and the broker rechecks the checkpoint before execution. Use a new pinned test run when the task requires test evidence.

The former run keeps its original outcome. No tool is replayed, and the new model conversation starts fresh. Historical test receipts are rechecked when inspected, and a changed worktree can make them stale. Recovery applies to brokered file writes and bounded commands. The memory adapter and orchestration integration retain their own journals and approval flows.

## Evaluate on disposable fixtures

Run `.venv\Scripts\python.exe -B reliability_evaluation.py` to run the workload matrix through the configured managed Mac provider. Pass scenario names to run a subset. The runner seeds separate ignored fixtures and uses a fixed, reviewed test hash. Generated assertion cases are parsed as data; they are never imported or executed. Find per-task metrics and references under `.local/runs/phase2d-*/evaluation.json`. Candidate lessons, when present, are evidence records only and are not promoted into a skill registry.
