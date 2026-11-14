# Mac Studio inference architecture (proposal)

This document records a future deployment direction. The Mac Studio is not available for validation yet. No network listener, remote execution service, authentication system, or Mac-specific runtime is implemented by this note.

## Responsibilities

- **Windows development workstation:** keep the repository, Git worktrees, edit permits, source writes, test receipts, review UI, and stop control. The Windows broker remains the authority for which files an agent may change.
- **Mac Studio:** optionally serve local model inference and model/resource metadata. It receives bounded prompts and returns model responses. It does not receive a shell, filesystem, or code-execution tool.
- **Local agent broker:** validate every model tool call on Windows, execute only the existing typed capabilities, record receipts, and return bounded results to the model. A remote model response never grants authority.

## Network boundary

Current operation remains loopback-only at `127.0.0.1:11434`. Do not bind Ollama or an agent execution endpoint to a public interface. A future LAN path should require an explicit operator setting and a trusted private network, authenticated clients, encrypted transport, a narrow inference-only API, request size/time limits, and auditable client identity. Prefer a private VPN or mutually authenticated tunnel over opening a router port. Do not put credentials in task text, logs, model prompts, or source-controlled configuration.

The Mac service must not offer arbitrary command execution, file browsing, repository checkout, or remote write tools. If remote orchestration is ever needed, design a separate authenticated task protocol and keep capability execution on the Windows broker. Review that security model separately before implementation.

## Task and review flow

1. Windows captures the selected project identity, Git baseline, task, and exact proposed edit scope.
2. The model receives only the prompt/context needed for reasoning. Sensitive files and unrelated project memory are excluded.
3. Windows validates returned tool calls against schemas and the one-use permit, then runs locally approved tools.
4. Windows binds command outputs to receipts, checks whether receipts remain current after edits, and reports verified status independently from model prose.
5. The user reviews the plan, file diff, test output, and completion classification; they may stop the run at any point.

Do not resume a task from chat text alone. Resume from a saved, integrity-checked run record and revalidate the Git and authorization snapshots.

## Model service operations

- Keep model selection explicit and local by default. Record model name/digest, response timing, prompt/completion token counts, context utilization, tool errors, iterations, and verified task/test outcomes.
- Measure Mac memory pressure, model load, inference latency, and concurrent request contention on actual hardware before selecting concurrency or model sizes.
- Bound per-agent context, response tokens, wall time, queue length, and aggregate concurrent work. A single scheduler should account for shared unified memory.
- Add health checks, request cancellation, bounded retries for transient service errors, and graceful restart behavior. A client timeout must not imply the server stopped an in-flight request.
- Keep per-agent logs and task identifiers separate. Avoid storing credentials or full prompts when a smaller receipt is sufficient.

## Cross-platform implementation plan

Before deployment, remove assumptions that all registered roots use Windows drive paths, validate path/link behavior on macOS, and keep OS-specific launchers behind narrow adapters. Test Apple Silicon acceleration, Ollama or another supported local inference server, service restart behavior, file permissions, and local-network authentication on the actual Mac. Do not infer performance or compatibility from the Windows host.

## Not yet implemented or verified

There is no Mac service, LAN transport, authentication, resource monitor, multi-agent scheduler, or remote approval UI in the current framework. There is no verified Mac benchmark. The Windows agent currently speaks to a loopback Ollama endpoint and performs all repository operations locally.
