# Mac Studio inference architecture

This document records the supported Windows-side remote inference boundary and future Mac deployment direction. On 2026-11-22, the configured Mac Ollama endpoint was reached through the existing managed SSH tunnel and used for one bounded, tool-free coding proposal. This verifies the configured path and one inference response; it does not establish long-term availability or production readiness.

## Live verification record (2026-11-22)

- Provider: existing `ssh_tunnel_ollama` using the configured managed SSH
  profile, host-key pin, and loopback-only forward.
- Health: Ollama `0.40.17`.
- Model: `qwen3-coder:30b`, digest
  `06c1097efce0431c2045fe7b2e5108366e43bee1b4603a7aded8f21689e90bca`.
- Initial health/model availability checks sent no task prompt. The tunnel was
  closed after the probe.
- One subsequent coding proposal used the existing Windows orchestration in
  conversational mode with no tools. The proposal stayed in a disposable Git
  fixture, passed allowlist and non-mutating patch applicability checks, and
  was left unapplied. Review found a bool-as-int bug, so the demo is not a
  successful implementation result.
- This milestone made no changes to the Mac service, LAN exposure, Windows
  Broker, or model configuration.

## Live verification record (2026-11-23; replacement coding task)

- The original Milestone 1 task was not recovered. A new operator-authored
  replacement specification defined `clamp_percent` and its required tests.
- The existing managed SSH provider again reached Ollama `0.40.17` and
  `qwen3-coder:30b` with digest
  `06c1097efce0431c2045fe7b2e5108366e43bee1b4603a7aded8f21689e90bca`.
  The request exposed no tools, used a 120-second inference timeout, forwarded
  only through loopback, and closed its tunnel afterward.
- The first separately approved patch had malformed hunk counts in both files:
  source declared 5 additions but contained 8; tests declared 20 but contained
  24. `git apply --check` passed but application consumed only the declared
  lines. A separate baseline clone reproduced the partial application.
- A corrected patch with SHA-256
  `957edaa7ea9aa17a4b32e98766fc5b2e376a0bda13483e246c597883d2d38934` passed
  exact hunk-count validation and isolated `git apply --check`; the simulated
  files matched the intended Windows CRLF bytes. After separate approval, only
  `src/progress.py` and `tests/test_progress.py` were replaced in the disposable
  repository, with new immutable copies of their prior contents preserved.
- The disposable suite passed 3 test methods, 0 failures and 0 errors, covering
  all 10 specified input cases. Fourteen focused LocalCodingAgent regression
  suites passed 193 tests with 0 failures and 0 errors. Full discovery was not
  rerun; the earlier optional `pycdlib` and
  `diagnostic_witness_service.py` errors were not rechecked.
- Candidate lessons for exact bool/int checking and hunk-count validation are
  locally recorded as verified candidates, not promoted to authoritative or
  executable skills. Evidence is under
  `.local/tests/unified-coding-demo/milestone2-recovery-20261123T162731Z-6defa190d5/`
  and `.local/tests/unified-coding-demo/milestone2-apply-20261123T163425Z-a77bf72ccc/`.
- No Mac service, LAN exposure, production repository, Broker authority, or
  model configuration was changed.

## Responsibilities

- **Windows development workstation:** keep the repository, Git worktrees, edit permits, source writes, test receipts, review UI, and stop control. The Windows broker remains the authority for which files an agent may change.
- **Mac Studio:** optionally serve local model inference and model/resource metadata. It receives bounded prompts and returns model responses. It does not receive a shell, filesystem, or code-execution tool.
- **Local agent broker:** validate every model tool call on Windows, execute only the existing typed capabilities, record receipts, and return bounded results to the model. A remote model response never grants authority.

## Network boundary

Windows Ollama at `http://127.0.0.1:11434` remains the default. Remote inference is opt-in and uses a run-scoped Windows OpenSSH local forward to Mac Ollama's own `127.0.0.1:11434`; Ollama is never exposed on the LAN or public internet. The SSH host key is checked against the key in the explicitly configured known-hosts file before the tunnel starts. Windows verifies the listener belongs to its own SSH process before each request. Requests use fixed API paths, bounded body/response sizes and timeouts, and redirects are rejected. The SSH transport provides encryption and public-key authentication. Credentials and private keys are not stored in the agent configuration or logs.

The Mac service must not offer arbitrary command execution, file browsing, repository checkout, or remote write tools. If remote orchestration is ever needed, design a separate authenticated task protocol and keep capability execution on the Windows broker. Review that security model separately before implementation.

## Unified coding workflow hardening (2026-11-23; partial)

`patch_validation.py` now provides a dependency-free unified-diff parser for
strict hunk-count and context validation, exact path allowlists, complete body
consumption, and reconstructed expected bytes. It rejects traversal, symlink
targets, ambiguous boundaries, renames, deletions, and binary/mode changes.
The Milestone 2 under-count failure is covered by a focused regression test.
Eight validator tests passed; the existing 14 focused integration/security
suites passed 193/193. Output is preserved under
`.local/tests/milestone3-validator-final-20261123T090737-9f5f19c8/`.

This is not yet an integrated coding workflow. The existing permit binds a
task, project state, and write paths, but does not bind the exact unified-diff
hash and reconstructed post-images. Until a one-use adapter binds those fields
and routes exact approved bytes through Broker plus the fixed test capability,
do not use an existing permit to apply a model-generated patch. No live Mac
proposal was made in this partial Milestone 3 work; the last verified Mac
connectivity and model evidence remains the Milestone 2 record above.

## Task and review flow

### Milestone 3 continuation — verified approval checkpoint (2026-11-23)

The existing Windows Broker remains the only write and test authority. The
new `coding_workflow.py` connects it to the coding harness, strict
`patch_validation.py`, existing provider factory/SSH tunnel, recovery journal,
and candidate skill library. Qwen receives a bounded prompt with no tools and
cannot write files, invoke shell commands, access the Broker, or operate VMs.

Before approval, the workflow validates the entire unified diff, reconstructs
expected file bytes, checks the allowlist and baseline, and applies the patch
only in a separate disposable simulation. The fixed pinned acceptance tests
must pass there. Approval binds repository identity and baseline, patch bytes,
expected post-image hashes, authorized paths, pinned test configuration and
command, plus a unique one-use identifier. The Broker consumes approval
durably before a live write, uses CAS and its recovery journal, verifies
resulting bytes, then runs only its predefined tests. Ambiguous writes are
terminal and are never retried automatically.

Live Mac proposal run `8a9ba8feea9d465ea4b0edcea26988bb` reached
`awaiting_approval` through the configured pinned-host, loopback-forwarded
`ssh_tunnel_ollama` provider and `qwen3-coder:30b`. Destination:
`.local/tests/milestone3-live-bacc8b0366df/repo`, baseline
`054e52f05a0a3cf4b69f457090f6ba9a0a91e798`, allowlist only
`src/progress.py`. Patch SHA-256:
`2b045865730b8b0ce6e277b206025fd17e1e5d8022080f51337d484998974cb8`;
expected post-image SHA-256:
`07d3ccebb1aed05af8a2c0faa9986897a8ca7aa80a9a78c1840b6c8b6f0e8825`.
Strict validation, isolated `git apply --check`, exact simulated byte
comparison, and three pinned acceptance tests passed. Explicit one-use
approval `4d6cd441cf7949349f4b740665f92b89` was then consumed before the write.
The Broker created only the authorized file and verified its actual bytes
against the post-image hash. The fixed post-write Broker tests passed 3/3
(zero failures/errors), and workflow state is `completed`.

Focused regressions passed 258/258. The exact-integer lesson is recorded as
unpromoted SkillLibrary candidate
`skill-3d78e16793a7034177a718f1`, supported by the Broker test receipt and
workflow provenance. The known full-discovery `pycdlib` dependency and
unrelated diagnostic syntax errors remain outside this milestone.

1. Windows captures the selected project identity, Git baseline, task, and exact proposed edit scope.
2. The model receives only the prompt/context needed for reasoning. Sensitive files and unrelated project memory are excluded.
3. Windows validates returned tool calls against schemas and the one-use permit, then runs locally approved tools.
4. Windows binds command outputs to receipts, checks whether receipts remain current after edits, and reports verified status independently from model prose.
5. The user reviews the plan, file diff, test output, and completion classification; they may stop the run at any point.

Do not resume a task from chat text alone. Resume from a saved, integrity-checked run record and revalidate the Git and authorization snapshots.

## Provider lifecycle and fallback (implemented on Windows; live route verified)

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

There is no additional Mac-side agent service, LAN listener, resource monitor, remote approval UI, or live Mac benchmark. Milestone 4 adds sequential planner, coder, and reviewer calls within the existing Windows workflow, all using the managed provider and receiving no tools. The loopback dashboard presents workflow state but does not authorize or apply patches. Live inference through `ssh_tunnel_ollama` reached the human approval checkpoint; see `.local/tests/milestone4-final-20261123-dbde4b6f/report.md` for the exact run, patch and post-image hashes, validation, and clean unapplied destination. Broader macOS runtime compatibility, SSH key lifecycle, service restart behavior, sustained availability, and performance remain unverified.

The pinned Windows pure-test runner now has an exact canonical registration for the authorized Milestone 4 disposable fixture and BOM-safe decoding for its unchanged pinned test configuration. Path traversal and symlink/junction/reparse-point checks remain enabled; this compatibility fix did not change the Mac inference adapter, Broker authority, or model permissions. After separate exact-patch approval, Broker consumed approval once, applied the disposable `src/progress.py`, verified its expected post-image, and ran the pinned test capability successfully (3/3). Final focused regressions passed 307/307. The verified candidate remains unpromoted. See `.local/tests/milestone4-completion-20261123-dbde4b6f/report.md` for receipts and hashes; earlier checkpoint reports remain preserved.

## Multi-file proposal reliability (2026-11-23)

The two rejected operational-review proposals had distinct causes: one
underdeclared hunk additions (+3 declared versus +6 consumed in both retries);
the other combined an ambiguous task and incorrect behavior with malformed
context, while strict parsing also stopped on a CRLF target. The live repair
uses full-file JSON intents with source hashes and trusted deterministic diff
construction; strict path/hunk/post-image checks and the Windows Broker
approval boundary remain unchanged.

On a fresh disposable two-module Python fixture, `ssh_tunnel_ollama` ran two
planner/coder/reviewer configurations: all `qwen3-coder:30b`, and `qwen3:8b`
planner with `qwen3-coder:30b` coder/reviewer. Each produced a valid two-file
proposal, passed byte-exact simulation and the fixed test capability (3/3),
and reached `awaiting_approval` after an independent advisory review. No live
patch was approved or applied. Focused regressions passed 196/196. The dashboard
model allowlist accepts both installed models, remains loopback-only, and has
no approval/apply route. Browser automation host was unavailable; dashboard
interactive usability remains unverified. See
`.local/tests/milestone4-reliability-20261123-5b3f8aa8925141aabb861b5b7d3e9c48/report.md`
for diffs, patch/post-image hashes, timings, candidate status, and manual
browser checklist. No production readiness is claimed.

### Configuration A application completion (2026-11-23)

The operator separately approved Configuration A workflow
`3abcc66ad4744709820e5a966d1ac018`. Existing Windows approval/application
consumed one patch-bound approval (`dbf35b623bcc4ec19d2aba8ca8efa185`) and
Broker changed only disposable `calculator.py` and `stats.py`. Both resulting
file hashes match their approved post-images. Pinned `stats_multi_module_v1`
tests passed 3/3 (exit 0), and focused inference/tunnel/authorization/recovery/
orchestration/SkillLibrary/coding regressions passed 196/196. The recovery
journal is complete and the workflow is `completed`. Candidate
`skill-90bfc6a2af0b8102e714ede2` version 5 remains unpromoted and now references
this run’s patch, approval receipt, final workflow state, and successful Broker
receipt. Configuration B remains untouched. The legacy `coding-status`
formatter raised a `file_hash` NameError; direct persisted-state and receipt
checks succeeded. See
`.local/tests/milestone4-reliability-20261123-5b3f8aa8925141aabb861b5b7d3e9c48/configuration-a-completion-report.md`.

## Dashboard operational usability update (2026-11-23)

The existing authenticated dashboard now presents overview, bounded disposable
task submission, active workflow/proposal review, persistent history, and
read-only SkillLibrary information. It still binds only to IPv4 loopback and
has no approval, apply, arbitrary evidence-path, or arbitrary command route.
Exact approval details are displayed for the established CLI/Broker checkpoint.

The `coding-status` formatter's missing `file_hash` import is fixed. Status
inspection does not invoke ambiguous-write recovery. A completed state is
reported verified only when the approved post-images, Broker output receipt,
and the matching complete recovery journal all verify. Workflow and dashboard
views share the same status projection. Historical Configuration A remains
unchanged and now reports its exact completed evidence.

The startup probe through the existing `ssh_tunnel_ollama` provider connected
successfully and listed `qwen3-coder:30b` and `qwen3:8b`. Provider health is
presented as cached startup data; the model allowlist is limited to those two
names. Dashboard task timeouts and coder attempts remain bounded.

Chrome rendered the loopback login page and navigation. Automated credential
entry was not performed; operator sign-in is still required for visual review
of authenticated pages. API authentication, task submission, history, model
allowlists, cancellation, approval presentation, and the absent approval/apply
routes were tested. Do not interpret this as full authenticated browser QA.

### Read-only real-project readiness

The host configuration includes project selectors for KronosStage and
PhotoLibraryTools, but neither repository was opened or changed in this review.
The current coding workflow and dashboard deliberately accept only clean
Milestone 3/4 disposable fixtures. General project identity and bounded path
reads exist, and project instruction collection and project-scoped SkillLibrary
retrieval are available components; no end-to-end read-only analysis run was
demonstrated for either target. Coding context remains allowlist-driven and
bounded. Dependency graph awareness and source retrieval across an entire
registered real project are not established by this workflow.

The next smallest step is a read-only registration pilot on an explicitly
approved project copy: verify canonical Git identity, capture project
instructions, select a bounded source set, inspect dependency metadata, enforce
a context budget, isolate retrieved skills/evidence by project identity, and
prove that no write or test capability is exposed. Production write access is
not part of that step. Readiness remains disposable projects only.

## Phase A — chat-first local assistant (2026-11-23)

The existing authenticated loopback server now serves a conversational chat
home page at `/`; configured disposable coding dashboards retain the previous
workflow console at `/developer`. A chat-only launcher can run with no project
root or coding file allowlist. It uses `CodingDashboard`'s loopback binding,
one-run bearer authentication, same-origin POST checks, the existing provider
factory, configured managed SSH tunnel, and installed-model allowlist.

`Ollama.chat_stream` consumes bounded `/api/chat` NDJSON responses and sends
text-only increments to the local UI. The request contains no tools. A bounded
`ConversationStore` writes atomic JSON records beneath `.local/chat`, limits
message count and size, and supplies only recent bounded context. Cancellation
reaches the HTTP provider socket. Model output is rendered with DOM text nodes;
HTML from the model is never interpreted. Chat cannot read project files or
authorize code changes. The existing coding workflow and Broker approval
authority are unchanged.

The `local-chat.cmd` launcher checks the SSH-tunneled provider and installed
models, binds only to `127.0.0.1`, opens the local page in the default browser,
and prints a one-run token in its console.
The token login remains enabled; an automatic browser session bootstrap was
not implemented. The old developer console remains available for explicitly
registered disposable fixtures only.

Live API demonstration on 2026-11-24 UTC verified `qwen3-coder:30b`, the three
acceptance turns, incremental output, follow-up context, cancellation, and
conversation restoration after restart. Exact outputs and latency are in
`.local/tests/phasea-chat-live-613f2e76b46a/result.json`. Browser inspection in
Edge showed the local chat login page and layout at port 52719. The browser
token form was not automated under the computer-use authentication rule, so
this is pre-auth visual review rather than authenticated browser QA.

Focused suites passed 114 tests across chat persistence and streaming,
dashboard, status, workflow, inference, SSH tunnel, authorization, recovery,
patch validation, and conversation security. Full test discovery was not run.
Project selection, source retrieval, automatic intent routing, coding proposals
from chat, SkillLibrary integration, and authenticated browser review remain
later phases. This status records Phase A only; it does not start Milestone 5.

## Unified platform architecture and model inventory slice (2026-11-24)

`PLAN.md` is the authoritative unified roadmap. This document remains the
inference and model-runtime boundary reference. The platform should continue to
use the Windows installation as the execution authority and the Mac as an
inference host; inventory of a file never grants permission to load or run it.

### Current model flow

- `provider_specs` derives eligible endpoints from validated host configuration.
  By default, local chat can discover Windows loopback Ollama and the enabled
  Mac Studio Ollama profile; an explicit `--provider` scopes discovery to that
  configured provider. The remote endpoint is still created only by the
  enrolled `ssh_tunnel_ollama` factory and pinned-host SSH lifecycle.
- `model_catalog.py` normalizes bounded `/api/version` and `/api/tags` results
  into persistent provider/runtime/host/model/digest identities under ignored
  `.local/model-catalog.json`. It records installed, discovered, available,
  compatible, and authorized states separately, plus runtime metadata. Model
  task capabilities remain unknown until independently measured and verified.
- The chat selector displays host and model identity. Refresh repeats bounded
  discovery. Submission resolves a current catalog key and rejects missing,
  unavailable, incompatible, or unauthorized entries. Before inference, the
  existing provider verifies the installed model again. No cloud fallback,
  model download, Broker tool, or project context is involved.
- `model_registry.py` remains the separate store for evidence-backed task
  capability profiles and performance evidence; the runtime inventory does not
  turn model tags into quality scores. `model_router.py` remains deterministic
  routing for agent workflows, not an Auto chat router yet.

This slice is functional for Ollama and managed SSH-tunnel Ollama only. The
provider/runtime factory, streaming API, cancellation, and inventory row
normalizer must be adapted before another runtime can be invoked. A future
adapter must supply bounded discovery, stable identity/digest, compatibility
and availability checks, capability provenance, resource requirements,
streaming/cancellation, and deterministic cleanup. The Broker remains local.

### Verified model/runtime inventory

On 2026-11-24, Windows Ollama `0.40.18` returned these 11 installed tags:

- `qwen3:8b-codex-8k`
- `qwen3.5:4b-codex-test`
- `qwen3:4b-codex-test`
- `deepseek-r1:8b`
- `qwen3.8:27b`
- `qwen3.5:4b`
- `qwen3:8b`
- `gemma3:4b`
- `llama3.2:3b`
- `qwen2.5-coder:7b-instruct`
- `qwen3:4b`

Mac Ollama `0.40.17`, reached through the existing pinned route, returned
`qwen3-coder:30b` and `qwen3:8b`. Both hosts report the same digest for
`qwen3:8b`; the registry keeps their host identities separate. Ollama tag
metadata reports GGUF format for all 13 host-tag entries, so they are
compatible with the existing Ollama provider path. This does not prove
task-specific quality, exact modality, current loaded status, or an adequate
resource budget. The live registry obtains these entries each time the app
starts or the user refreshes; persisted inventory alone is not an invocation
grant.

The live smoke at `.local/tests/model-catalog-live-468d9e437b5b/result.json`
discovered and refreshed all 13 entries, then observed streamed output for
Mac `qwen3-coder:30b` and Windows `qwen3:4b` in the same persistent
conversation. Each stream included a selected-model identity event. This is
live inference evidence for these two Ollama models, not a benchmark or a
claim about the other models. Browser UI verification was blocked because the
available browser helper could not determine the existing tab URL; no
authenticated page was navigated.

Windows hardware was checked read-only: MSI MS-7D25, Core i7-12700K, about
31.8 GiB physical memory, RTX 3070 Ti with 8 GiB reported VRAM, and Intel UHD
Graphics 770. Windows Ollama has no public/LAN exposure. Mac hardware and OS
were not queried: the approved tunnel exposes Ollama's loopback HTTP API, not a
host inventory API, and no shell was opened. To verify Mac hardware and local
MLX/Hugging Face caches, authorize a separate read-only helper with a fixed
allowlist of paths/fields and a bounded response schema. Do not widen the SSH
tunnel or provide a general remote shell.

### Other discovered runtimes and artifacts

Bounded read-only inventory also identified:

- Windows Hugging Face snapshot
  `sentence-transformers/all-MiniLM-L6-v2`, snapshot
  `1110a243fdf4706b3f48f1d95db1a4f5529b4d41`, with BERT config (hidden size
  384, max positions 512) and safetensors/PyTorch/ONNX files. It is cached but
  not registered with the current app, runtime compatibility is unverified,
  and this chat path does not invoke it.
- KronosStage private artifacts include MOSS-Audio-4B-Instruct,
  MOSS-Music-8B-Instruct, Qwen3-Omni-30B-A3B-Thinking Q4_K_M with mmproj,
  Demucs, and TinyMu/SmolLM2 experiment files. Static file presence was
  observed. The local installation summary reports older inference validation
  for MOSS-Audio, Qwen3-Omni, Demucs, CLAP, and an Ollama Qwen3 8B. That
  summary is project evidence, not a fresh validation from this session.
  MOSS-Music and the TinyMu experiments have no current runtime compatibility
  evidence in this slice.
- These artifacts are not exposed as selectable chat models. Additional
  adapters need explicit runtime/environment identity, file integrity and
  provenance checks, modality and hardware requirements, licensing review,
  resource admission, safe weight loading, and cancellation/cleanup tests.
  Do not execute project scripts or weight files during discovery.

### Supported and planned adapter path

| Source/runtime | Discovery boundary | Current state |
|---|---|---|
| Windows Ollama | Configured IPv4 loopback `/api/version` + `/api/tags` | Discovered, persisted, selectable through existing provider |
| Mac Ollama | Existing enrolled profile, pinned host, run-scoped loopback SSH tunnel | Discovered, persisted, selectable; tunnel closes after discovery/request |
| Mac MLX | No read-only host inventory adapter | Requires separately authorized fixed-output helper; runtime not integrated |
| Hugging Face cache | Windows bounded snapshot path was inspected read-only | Artifact discovered, no safe runtime adapter or invocation grant |
| Standalone GGUF / llama.cpp | KronosStage bounded directories inspected | GGUF files discovered; no catalog adapter or chat invocation path |
| PyTorch / ONNX / sentence-transformers | Static files and cache/config inspected where named | No registry-backed adapter, resource policy, or selection/invocation path |
| Audio / vision / speech | MOSS, Qwen Omni, Demucs, CLAP records/artifacts | Specialized model paths are candidates; no universal modality registry or chat routing |
| Other projects / cloud providers | Not scanned or configured | Not authorized for this slice; no silent external transmission |

The next architectural increment should define and contract-test the
runtime-adapter interface, then integrate one non-chat capability (likely the
cached embedding model) behind explicit local authorization. Keep Auto routing
disabled until capability evidence, resource telemetry, model-selection
receipts, and bounded fallback behavior can be explained. Continue to use the
Windows Broker for every project read/write/test capability and approval.


### Local document knowledge in chat (2026-11-24)

The chat-only service now exposes a local Knowledge Library adapter backed by the
same project-bound SQLite database as `KnowledgeIndex`. It stores TXT, Markdown,
and bounded PDF imports, original bytes, chunk metadata, FTS5 keyword entries, and
source hashes. Conversation-only attachments are filtered by conversation identity;
persistent library entries remain project-bound to LocalCodingAgent. Evidence is
passed as bounded untrusted excerpts to whichever already-authorized Ollama model
the user selects. The application constructs the citation block from database
metadata, including document hash and page or heading location. Imported document
content does not become a skill or grant authority.

The local Hugging Face `all-MiniLM-L6-v2` snapshot is now used through a bounded,
offline CPU worker reusing the existing isolated OpenWebUI Python environment; no
dependencies or model weights were installed or changed. The provider validates
the snapshot digest and reports 384-dimensional normalized embeddings. FTS5 and
semantic results are combined with reciprocal-rank fusion and preserve keyword
fallback. `DocumentKnowledgeLibrary` backup now includes vectors, model identities,
SkillLibrary registry, hash-verified referenced evidence, and bounded SkillLibrary
recovery artifacts. Restore remains same-project and refuses nonempty destinations.
Cross-root migration needs explicit evidence rebinding and review because project
identity and approved skill references are root-bound. Disposable live API
verification passed through both the Mac and Windows conversational models,
including restart, restore, citations, and deletion. Browser UI verification
remains unavailable because the computer-use browser host is down.

### User-controlled internet research (2026-11-24)

Research is a Windows-side, task-scoped adapter using the existing safe public
web retrieval boundary. Local-only is the default. The chat offers explicit
research-now and ask-before-research modes; an approval is one-use and bound to
the conversation, sanitized objective, and selected authorized model. The Mac
inference tunnel is not widened and the Mac model receives only bounded public
source excerpts when the user has authorized research. There is no model shell,
general network tool, background crawler, cloud fallback, or automatic source
retention.

The configured `firecrawl_keyless` provider calls Firecrawl's documented JSON
Search API using the standard library and no account or API key. Its keyless
service is free within provider-controlled daily per-IP limits; higher limits
and zero-data-retention search require separate paid/enterprise authorization.
Only the sanitized search topic is sent, and keyless search is not ZDR. Do not
send private project details or sensitive searches to this provider. The prior
DuckDuckGo HTML adapter returned an anti-bot challenge during a live check; the
application does not bypass it or use it as an automatic fallback. Sessions are
bounded to one search, three HTTPS page attempts, one cached `robots.txt` check
per source host, 55 seconds, per-page and context character caps, a 2-second
minimum inter-session interval, and 30 sessions per hour per process. Webpage
URLs are DNS-checked and pinned to public addresses, redirects are refused,
robots policy is checked fail-closed, and all fetched content remains untrusted.
Sensitive-looking credentials, paths,
emails, and code are filtered from query text before dispatch. The user-facing
Research now option identifies that the topic is sent to Firecrawl, and the
approval prompt explains the same disclosure before a request.

The KnowledgeIndex remains the only persistent knowledge store. Research is
temporary by default; users can explicitly save a bounded excerpt with URL,
retrieval/publication metadata, and hashes into the current project library.
Future search APIs must implement the provider contract without granting model
network authority, must disclose data handling and credentials, and must honor
retention restrictions before results can be persisted. Optional autonomous
learning remains a future user-started, budgeted, reviewable session; ordinary
chat never initiates it.

### Read-only project intelligence (2026-11-24)

Project registration is a separate user-authorized capability in the chat
dashboard. A registered project ID is the existing canonical-root-derived
`Project.identity`; project scope is persisted on each conversation. General
conversations have no project identity, and project retrieval is limited to the
one registered project bound to that conversation.

The project adapter uses fixed read-only Git metadata commands only: branch,
commit, porcelain status, bounded recent history, remotes, local remote-tracking
refs, and a bounded `git ls-files` inventory. It never fetches, clones, checks
out, changes the index, or executes repository scripts. Remote relationships
are explicitly labeled cached. Remote URLs are reduced to host/repository
identity so embedded userinfo and query values are not retained.

Supported source files are hash-indexed by the existing `KnowledgeIndex` and
FTS5/optional MiniLM retrieval. The KnowledgeIndex now accepts an owner-validated
storage location: project source bytes remain read-only while the index is stored
under the LocalCodingAgent ignored `.local/project-intelligence/indexes/<project
identity>/` tree. The per-project database records the source project's own
identity. File discovery and indexing have explicit byte, file, size, and time
bounds. Generated directories, environment files, credentials, binary/model
weights, audio/media, linked/reparse paths, and hard-linked files are excluded
or rejected. Untrusted project instructions and source text are passed only as
bounded reference excerpts; they cannot grant tools or authority.

Before emitting source references, the service reopens each indexed chunk,
rechecks the current document hash and indexed line range, and withholds stale
citations if source bytes changed during generation. Citations include project,
relative path, lines, section, commit, and SHA-256. The user-supplied public
research policy remains unchanged; project analysis never triggers a network
fetch. GitHub URL identity may be displayed from local remote config, but public
API/issue/PR retrieval is a future adapter that must use task-scoped research
authorization.

Live disposable verification on 2026-11-24 used actual Windows Ollama
`qwen3:8b` and Mac Studio Ollama `qwen3-coder:30b`. Each generated a response
from the same project index and emitted a currently hash-validated citation to
`pipeline.py`; no production project was registered. Evidence is retained under
`.local/tests/project-live-44343b6fe354465f8c6bf7637201eb52/result.json`.
The browser helper reported that its browser access host was unavailable, so
this is authenticated API/live-model evidence, not browser acceptance. Use the
manual checklist in PLAN.md before treating project UI acceptance as complete.

### Future chat visual and live metrics

The user wants a later visual redesign that may use a sound-recording waveform
motif while the model is working, plus an optional live Mac/AI metrics view for
resource use, tokens, and generation speed. These are not implemented here.
Animations must reflect real lifecycle state, not fabricated model activity.
Any metric must identify its source and observation time; unsupported or
unavailable telemetry must be clearly marked. Mac hardware/OS introspection
requires a separate fixed-output read-only mechanism; the current SSH tunnel
remains inference-only.

### Trusted conversational capability dispatch

The chat dashboard describes a code-owned capability registry at runtime and
recognizes a narrow set of clear user requests for registered-project
operations. Current handlers list registered projects, bind a conversation to
an exact registered project, inspect bounded local Git status, list eligible
relative paths, read one explicitly named bounded UTF-8 file, and search the
existing project KnowledgeIndex. Dispatch accepts fixed capability identifiers
and strict input schemas; model output and repository content cannot add
handlers.

All returned project data is marked as untrusted reference material. File reads
use the existing Git inventory, source exclusions, path containment checks,
regular-file and size checks, and secret scan. Direct reads are rehashed after
model generation; indexed passages are revalidated against index metadata and
current bytes before citations are emitted. Capability audit records retain
identities, result/argument hashes, state, and time, not source content or raw
arguments. Shell access, writes, builds, and test execution remain outside the
chat registry. Registration remains a separate authenticated explicit-confirm
operation until a one-use approval card can bind the selected root, scope, and
conversation.

The next integration should expose a time-limited one-use registration approval
then, separately, a Broker-backed pinned-test capability that preserves the
existing operator approval, receipt, cancellation, and recovery behavior. Do
not grant tools from model-authored proposals or add arbitrary terminal access.

Future model inventory remains adapter-based and may add Ollama, MLX,
Hugging Face Transformers, PyTorch, ONNX, GGUF, and interchangeable audio and
vision runtimes after compatibility and authorization checks. Any future use of
Music Flamingo, Audio Flamingo 3, Qwen3-Omni, CLAP, Demucs, or Basic Pitch must
record checkpoint provenance and license terms. Verify exact checkpoint rights
before paid-performance, commercial, or distribution use.
