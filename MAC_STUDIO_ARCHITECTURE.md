# Mac Studio inference architecture

This document records the supported Windows-side remote inference boundary and future Mac deployment direction. The Mac Studio is not available for validation yet; no live Mac connectivity or inference has been verified.

## Responsibilities

- **Windows development workstation:** keep the repository, Git worktrees, edit permits, source writes, test receipts, review UI, and stop control. The Windows broker remains the authority for which files an agent may change.
- **Mac Studio:** optionally serve local model inference and model/resource metadata. It receives bounded prompts and returns model responses. It does not receive a shell, filesystem, or code-execution tool.
- **Local agent broker:** validate every model tool call on Windows, execute only the existing typed capabilities, record receipts, and return bounded results to the model. A remote model response never grants authority.

## Network boundary

Windows Ollama at `http://127.0.0.1:11434` remains the default. Remote inference is opt-in and uses a run-scoped Windows OpenSSH local forward to Mac Ollama's own `127.0.0.1:11434`; Ollama is never exposed on the LAN or public internet. The SSH host key is checked against the key in the explicitly configured known-hosts file before the tunnel starts. Windows verifies the listener belongs to its own SSH process before each request. Requests use fixed API paths, bounded body/response sizes and timeouts, and redirects are rejected. The SSH transport provides encryption and public-key authentication. Credentials and private keys are not stored in the agent configuration or logs.

The Mac service must not offer arbitrary command execution, file browsing, repository checkout, or remote write tools. If remote orchestration is ever needed, design a separate authenticated task protocol and keep capability execution on the Windows broker. Review that security model separately before implementation.

## Task and review flow

1. Windows captures the selected project identity, Git baseline, task, and exact proposed edit scope.
2. The model receives only the prompt/context needed for reasoning. Sensitive files and unrelated project memory are excluded.
3. Windows validates returned tool calls against schemas and the one-use permit, then runs locally approved tools.
4. Windows binds command outputs to receipts, checks whether receipts remain current after edits, and reports verified status independently from model prose.
5. The user reviews the plan, file diff, test output, and completion classification; they may stop the run at any point.

Do not resume a task from chat text alone. Resume from a saved, integrity-checked run record and revalidate the Git and authorization snapshots.

## Provider lifecycle and fallback (implemented on Windows; Mac untested)

The provider contract exposes model verification/discovery, a no-context health probe, bounded inference, structured sanitized failures, cancellation checks, and cleanup. Remote provider construction requires the validated managed SSH profile. Discovery and health checks close their run-scoped provider afterward; inference runs close the provider on completion or failure. Retries are limited to one retry of the idempotent version health GET. Inference requests are never automatically replayed.

Configuration version 4 adds `inference.fallback`. It defaults to `{"mode":"disabled","substitutions":[]}`. Only exact configured provider/model pairs or an explicit `*` source model may be substituted. `disabled` stops on failure. `ask` requires a per-run typed `YES` approval and identifies when task context will be sent to Mac. `local_only` permits only explicitly configured Windows Ollama models. `approved` permits only substitutions with both `operator_approved` and `data_sharing_authorized` set. Cloud providers/models are rejected in all modes. A substitution must declare a context ceiling at least as large as the task context; the target model is rediscovered and verified before the run starts. Identity, authentication, malformed/partial response, redirect, and other security errors do not trigger fallback. Failures during generation do not trigger fallback, so requests with possible side effects are not replayed.

Fallback changes inference destination only. Tool schemas and returned tool proposals remain untrusted data; the Windows broker continues to enforce the existing capability allowlist, project baseline, permits, receipts, and stop controls. No model response can grant filesystem, shell, repository, or approval authority. There is no cloud fallback.

Example local-primary configuration with explicitly authorized ask-before-switching to a remote Mac profile:

```json
{
  "version": 4,
  "projects": {"example": {"name": "Example", "root": "D:/Work/Example"}},
  "inference": {
    "provider": "ollama",
    "model": "qwen3:8b",
    "endpoint": "http://127.0.0.1:11434",
    "remote": {
      "enabled": true,
      "profile_id": "mac_studio",
      "port": 22001,
      "ssh_host_alias": "mac-studio-inference",
      "ssh_host_key_sha256": "SHA256:AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA",
      "credential_ref": "ssh-agent",
      "known_hosts_file": "C:/Users/you/.ssh/mac_known_hosts",
      "ssh_config_file": "C:/Users/you/.ssh/mac_config"
    },
    "fallback": {
      "mode": "ask",
      "substitutions": [{
        "from_provider": "ollama", "from_model": "qwen3:8b",
        "to_provider": "ssh_tunnel_ollama", "to_model": "qwen3:14b",
        "max_context": 32768,
        "operator_approved": false,
        "data_sharing_authorized": false
      }]
    }
  }
}
```

Use `mode: "local_only"` for remote-to-Windows recovery, or `mode: "approved"` only after documenting explicit operator and data-sharing authorization for every row. Preserve local endpoint defaults by omitting a version 4 fallback policy or setting it to disabled.

## Model routing (implemented; Mac evidence pending)

The agent role, model, and provider are independent. The role is descriptive
task context for routing; it grants no capabilities. Ollama models are
discovered from each explicitly authorized provider per run. Model identity is
provider ID plus exact model ID and Ollama digest where available. The Windows
broker still owns all permissions and tool execution.

`model_router.py` classifies tasks deterministically into one or more categories
without another inference call. The version 1 private registry at
`.local/model-capabilities.json` stores per-model capability scores with
provenance, tool-calling and context evidence, memory estimates, limitations,
and benchmark references. Verified measurements require timestamped,
hash-bound references and expire after 90 days. Operator-provided information
is labeled separately; estimates remain unverified. Model-generated capability
claims are not accepted as evidence. A digest change prevents use of the old
profile.

Routing applies authorization, availability, exclusions, context, explicit
capability minimums, and configured memory limits before ranking. Ranking uses
operator preferences, recent receipt-backed outcomes for the same task
category, supported capability evidence, measured latency, then a stable
provider/model order. Outcome rates and latency influence rank only after three
recent receipts in the same task category. Missing evidence is reported as uncertainty; `--model auto`
may use the already configured model as an explicitly conservative
default if no known constraint rejects it. An explicit model override still
passes provider, availability, context, capability and exclusion checks.

The private `.local/model-performance.jsonl` records only independent test
receipt outcomes, with provider/model/digest, category, test identifier,
inference latency, throughput when available, context use, timestamp, and a
hash-bound result reference. A passing test receipt verifies that suite only;
it does not prove every requested task detail. Stale performance rows older
than 90 days are ignored. No Mac measurements are seeded from estimates or
model names.

`configured` routing uses the provider already selected in host configuration.
`local_only` considers Windows Ollama only. `remote_allowed` may consider the
Mac only when its managed SSH profile is enabled. CLI controls include model
override or `auto`, provider selection, minimum capabilities, context ceiling,
preferred and excluded models, role, and a memory ceiling. The route produces
a prompt-free structured explanation in the run result. Milestone 2 fallback
remains a separate preflight policy; routing does not replay generation or
grant remote tools. Agent concurrency/resource scheduling remains future work.

## Model service operations

- Keep model selection explicit and local by default. Record model name/digest, response timing, prompt/completion token counts, context utilization, tool errors, iterations, and verified task/test outcomes.
- Measure Mac memory pressure, model load, inference latency, and concurrent request contention on actual hardware before selecting concurrency or model sizes.
- Bound per-agent context, response tokens, wall time, queue length, and aggregate concurrent work. A single scheduler should account for shared unified memory.
- Add health checks, request cancellation, bounded retries for transient service errors, and graceful restart behavior. A client timeout must not imply the server stopped an in-flight request.
- Keep per-agent logs and task identifiers separate. Avoid storing credentials or full prompts when a smaller receipt is sufficient.

## Cross-platform implementation plan

Before deployment, remove assumptions that all registered roots use Windows drive paths, validate path/link behavior on macOS, and keep OS-specific launchers behind narrow adapters. Test Apple Silicon acceleration, Ollama or another supported local inference server, service restart behavior, file permissions, and local-network authentication on the actual Mac. Do not infer performance or compatibility from the Windows host.

## Not yet implemented or verified

There is no Mac service, LAN listener, resource monitor, multi-agent scheduler, remote approval UI, or live Mac benchmark. The managed SSH route and fallback use mocks in automated tests only; actual macOS runtime compatibility, SSH handshake, host-key enrollment, Mac service behavior and performance remain hardware-dependent validation tasks.
