# Coding harness foundation (schema version 1)

`coding_harness.py` defines a harness boundary. It is not selected by the CLI in
this phase. `agent.py` continues to call the existing `local_agent.run_agent`
path directly, so the current default and public commands are unchanged.

## Authority boundary

`HarnessTaskRequest` is serializable descriptive input: task/run/project IDs,
objective, model/provider profile, budgets, evidence references, and an
optional session reference. It contains no filesystem scope, shell/network
permission, memory-write permission, or tool list. `LegacyExecutionContext` is
host-only and is not serializable. The host supplies the selected `Project`,
output location, provider endpoint, pinned tests, and already approved exact
write paths. The harness cannot expand those grants. In the legacy implementation
all tool execution still enters `Broker.call()` and its existing path/CAS,
command, receipt, and memory rules.

Model inference and execution authority remain separate. A future remote model
endpoint may receive prompts and return model output, but the Windows host retains
the project object, Broker, permits, receipts, and verifier. No remote tool
transport is defined here. Windows and macOS use the host-local context and the
existing platform capability adapters.

## Lifecycle and budgets

`CodingHarness` exposes `start` and `resume`; returned `HarnessRun` supports
`events`, `wait`, `cancel`, and `close`. Cancellation is cooperative: the legacy
runner checks the host cancellation event before each model turn, after a model
response, and before each Broker dispatch. The wall timer requests cancellation;
it cannot terminate an in-flight network request/thread. Bounded Broker child
commands retain their existing timeout and process cleanup. `close` waits up to
the requested/cancellation grace period; it never removes run output or receipt
evidence.

The legacy adapter maps `max_turns` to the existing step limit and enforces
`max_tool_calls` before Broker dispatch. Wall time triggers cooperative cancel.
The default direct `run_agent` call retains its old behavior because the new
parameters default to no tool-call limit and no cancellation event. Legacy
sessions do not support native resume; capability reporting says so and `resume`
fails closed. A future harness may advertise native resume explicitly.

## Versioned records

Requests, events, and results use `schema_version: 1`. Parsers reject unsupported
versions, unknown event kinds, unknown top-level fields, malformed references,
and invalid candidate source hashes. Events have a run ID, monotonically
increasing per-run sequence, event ID, timestamp, harness ID, type, and JSON
payload. The legacy adapter forwards assistant turns and tool proposals/results
as the custom loop runs; command receipts and artifacts are referenced by
path/URI and SHA-256. These events are an audit stream, not authorization
evidence.

`skill.candidate.proposed` reserves fields for a candidate lesson reference,
source/evidence references, validation status, and source hashes. This phase
does not extract candidates, deduplicate them, promote skills, or fine-tune a
model. Any later registry must be distinct from project memory and require
evidence, deduplication, review, and approval before authoritative or executable
use.

`HarnessResult.verification_authority` is fixed to `host_pending`; a harness
cannot claim independent verification. Host verification must independently
inspect the actual Git/worktree snapshot and hashes, validate command receipts
against project identity and run containment, and recheck applicable AGENTS.md,
memory, permit, and pinned-test scope. Reported summaries and tool events are
untrusted claims.

## Adapter acceptance constraints

Any later Qwen Code adapter must pass a separate capability/isolation probe
before selection. It must have no direct project filesystem, shell, network,
memory, skills, hooks, or subagent authority. Every tool proposal must route to
the host Broker; permissions are checked there against the current project and
operator grant. Unknown tools and implicit native capabilities fail closed.
Qwen session/memory/skill features must not become project-memory authority.
This module does not install, launch, or configure Qwen Code.
