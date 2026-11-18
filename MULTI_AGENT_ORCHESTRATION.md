# Milestone 4: Multi-Agent Orchestration

## Architecture

`orchestrator.py` owns a versioned task graph, operator approval, model selection,
bounded scheduling, task state, evidence, and integration proposals. The command
line entry point is `orchestrate.py`. Tasks use the existing model router and
`LegacyCustomHarness`; tools still execute in the host through the existing
Windows broker. Remote inference receives prompts and model requests only. It
does not receive command execution or filesystem access.

The graph is proposed first. Approval binds the graph digest, selected task IDs,
operator-scoped paths, project identity, Git baseline, and private memory
snapshot. Each Developer receives a detached worktree. Independent developers
use separate worktrees. A dependent QA or Reviewer may inspect the single
Developer ancestor's worktree read-only. Multiple developers cannot write the
same path in one graph, and project-relative path leases prevent overlapping
active writes across runs.

The run state, bounded event log, model/routing details, evidence references,
and task worktrees live under the project's ignored `.local/orchestration/`
directory. Recovery checks the recorded process owner before taking over a run;
live locks are not stolen. Paused and cancelled runs retain their artifacts.

## Roles and permissions

| Role | Responsibilities | Tools |
| --- | --- | --- |
| Architect | Read project context and propose bounded tasks | list/read/search, Git status/diff, finish |
| Developer | Implement approved file changes | Architect read tools plus replace/create, finish |
| QA | Run configured tests and inspect evidence | Read tools, configured test/command tools, finish |
| Reviewer | Inspect code and evidence independently | Read tools, Git status/diff, finish |
| Researcher | Retrieve and summarize bounded project knowledge | Read tools, Git status/diff, finish |
| Memory analyst | Suggest evidence-backed reusable lessons | Read tools, Git status/diff, finish |

Role permissions are fixed and may only be narrowed by configuration. They do
not inherit another role's tools. `run_tests` is exposed only when an operator
provides a registered test configuration. The memory analyst can emit a
`lesson_candidate` event tied to evidence; there is no promotion operation in
this milestone.

## Routing and resource limits

For each task, the service asks the existing router using the role, task
classification, explicit task capability requirements, context requirements,
provider availability, operator preferences, and available performance
evidence. It records the selected provider, model, digest, and routing decision.
The configured authorization and fallback policy remain authoritative. Role
defaults are routing hints; explicit task capability minima remain hard
requirements. A role is not permanently pinned to a model.

The CLI defaults are two concurrent agents, two calls per provider, one call per
model, a queue of 32 tasks, an 8 GiB aggregate memory admission budget, and one
retry for eligible read-only tasks. Model memory figures are mock inputs until
real host measurements are available. Unknown model footprints conservatively
reserve the admitted capacity. Writes are not automatically retried. Aging
raises queued task priority to reduce starvation. Deadlines, per-task timeouts,
pause, resume, cancellation, and queue admission are bounded by the scheduler.

## Operator workflow

From the project root, first ask Architect to propose a plan for exact paths:

```powershell
python -B orchestrate.py decompose --project LocalCodingAgent `
  --objective "Improve the parser error reporting" `
  --scope-path src/parser.py --scope-path tests/test_parser.py
```

Review the returned `run_id`, plan digest, roles, task dependencies, and exact
write paths with `status`. Approve only the tasks and paths you intend to grant:

```powershell
python -B orchestrate.py approve --run-id RUN_ID `
  --plan-sha256 PLAN_SHA256 --task architect_review --task developer_parser `
  --task qa_parser --write-grant developer_parser=src/parser.py `
  --write-grant developer_parser=tests/test_parser.py
python -B orchestrate.py run --run-id RUN_ID
python -B orchestrate.py status --run-id RUN_ID
```

Approval does not itself claim that work is tested or reviewed. Inspect the
status and evidence. To create a file-level integration review proposal:

```powershell
python -B orchestrate.py integration-proposal --run-id RUN_ID --task developer_parser
```

The proposal includes bounded unified diffs, before/after hashes, baseline
commit, and QA/reviewer status. Review the diffs and status. Only then apply the
proposal using its exact digest:

```powershell
python -B orchestrate.py integrate --run-id RUN_ID --proposal-sha256 PROPOSAL_SHA256
```

Integration rechecks the proposal content, baseline, project memory, approved
paths, and leases, then calls the existing `Broker.write` for each file. It does
not automatically merge task branches. Before writing, it creates a durable
`.local/orchestration/<run>/integration/journal.json` containing the Git baseline
and each target's before/after hash. The journal is atomically refreshed after
each successful write. A crash between replacing a target and refreshing the
journal is detected by checking the target against both hashes.

If integration is interrupted, inspect without changing files:

```powershell
python -B orchestrate.py integration-recovery --run-id RUN_ID
```

Targets are classified as `before`, `after`, or `conflict`. A conflict matches
neither reviewed hash and must be preserved for manual reconciliation. After
reviewing the paths and confirming the approved proposal digest, an operator can
resume remaining compare-and-swap writes:

```powershell
python -B orchestrate.py integration-reconcile --run-id RUN_ID `
  --proposal-sha256 PROPOSAL_SHA256 --recovery-action resume
```

If every target already matches its reviewed after hash, `accept_applied` records
that outcome. Recovery verifies the proposal, original approval, Git commit and
branch, unrelated file contents, and target hashes. It skips applied targets and
writes only targets still at their exact before hash. It never resets or deletes
work. An unresolved journal blocks task execution and new proposals. If evidence
cannot be verified, preserve the run and reconcile manually; there is no
automatic rollback.

Use `pause`, `resume`, and `cancel` with the run ID to control a live run. Use
`status` to inspect current and terminal state. A crashed run can be reopened
with the service recovery API only after its prior process is gone; interrupted
tasks are reported and are not represented as completed.

## Completion and evidence

Agent-reported completion is recorded as `agent_completed`, not verified
success. Testing requires a valid host-generated command receipt with a
successful exit code, project identity, output hash, and path under the task's
run output directory. Review is reported independently. The aggregate remains
`incomplete` when tasks fail, time out, are cancelled, or lack required
verification. A model's summary alone cannot establish test or review success.

Events record role/task identity, route choice, queue and execution timing,
resource admission, retries, errors, cancellation/cleanup, test receipts, and
integration status. Prompts and secrets are not copied into orchestration event
logs; run evidence remains private under `.local/`.

## Validation and limitations

The orchestration scheduler and resource provider are exercised with mocks. The
Mac Studio has not been measured or validated; do not increase limits based on
assumed capacity. The initial implementation allows a single Developer
ancestor to feed read-only QA/Review tasks, but does not combine independent
developer worktrees. Integration supports bounded UTF-8 text files and stops
when diffs exceed its review limit. Interrupted multi-file integration is
journaled; verified work can be resumed idempotently, while conflicts require
manual operator reconciliation. Candidate lessons remain separate from trusted
skills. The project-scoped, evidence-reviewed skill lifecycle and operator CLI are
documented in [PERSISTENT_SKILLS.md](PERSISTENT_SKILLS.md). RAG, model training,
and fine-tuning were out of scope for the initial orchestration milestone. The
project-scoped retrieval layer is now documented in
[KNOWLEDGE_RETRIEVAL.md](KNOWLEDGE_RETRIEVAL.md); retrieved documents and skills
are bounded reference data and do not grant permissions.

The next milestone should validate real host resource measurements and operational
recovery. The knowledge index builds on the approved skill and project-memory
provenance boundaries; local embedding compatibility remains unverified until an
installed embedding model is explicitly selected.
