# Local assistant setup

This local assistant uses Ollama and Qwen for general help and project-aware coding.
From PowerShell, run `agent` in the folder or drive root you want it to access.
Bounded current-folder reads include PDF text extraction; one-task text edits and
new files require exact-path authorization. Private state and evidence remain under
`.local/`.

## Interactive assistant from any PowerShell location

The `agent` command is available on this Windows host. Start PowerShell in the
folder or drive root you want the assistant to read, then run:

```powershell
Set-Location D:\
agent
```

The selected folder becomes the assistant's read/search scope; a drive root such
as `D:\` covers that drive, while files on another drive require changing scope
with `/root C:\`. Project memory still loads from the selected project (by default
`LocalCodingAgent`), regardless of the current-folder scope. Use `/project NAME`
to select another registered project and its memory. Current-folder tools are bounded and
exclude hidden, private, system, linked, hard-linked, and secret-like paths. A
one-task edit/create requires `/allow-edit PATH` or `/allow-create PATH`; Git
worktree files continue through the project's reviewed permit flow. No delete or
arbitrary PowerShell tool is available. Text/source files are supported; PDF text
extraction requires the optional local `pypdf` package, and scanned PDFs need OCR,
which is not currently provided.

To use another manual as reference, place its text or PDF under the selected
assistant folder and ask for specific pages or sections. The assistant can use
that material for the current task. Lasting guidance must be distilled and saved
through the reviewed project-memory adapter; this does not change model weights.
The terminal accepts local paths, not uploaded attachments.

## Public web research and saved sources

Ask the assistant to search the public web, read an HTTPS page, or retrieve a
public YouTube transcript. Retrieval does not log in, submit forms, run page
scripts, or download video media. It refuses redirects, nonstandard HTTPS ports,
private/reserved addresses, non-HTML page responses, and oversized responses.
Search snippets, page text, and captions are untrusted source material; ask for
citations and the assistant will cite the source URLs it actually read.

To keep a source, explicitly ask the assistant to save or remember it. Saved
pages and transcripts, with URL, title, retrieval date, and content hash, live in
the ignored `.local/sources/sources.jsonl` file. Ask to search saved sources or
forget one; forgetting removes its record and content from the corpus. This is local
retrieval-backed reference storage, not model-weight training. The corpus is
limited to 20 MB and each retrieved text is limited to 16,000 characters.

## Launch local Qwen

In normal PowerShell, this uses the existing Ollama server and installed
`qwen3:8b`, with bounded repository tools and **read-only takeover by default**:

```powershell
& 'D:\Workspaces\LocalCodingAgent\Start-LocalCodingAgent.cmd' --project KronosStage
```

Other registered selections are `PhotoLibraryTools` and `PhotoLibraryPlanning`.
They are distinct main/feature worktrees, with existing changes/holds preserved.
No model is downloaded, no global configuration changed, and no cloud fallback
exists in the local runner. Existing project memory loads automatically through
the adapter; only complete newest sections are provided and identical ones deduplicate.
The agent does not receive a test tool by default. To allow the registered fixture's
pinned suite for one run, pass `--allow-pinned-tests`; this never enables arbitrary
commands or unpinned project tests.

## Host-local project and inference configuration

Run `python -B agent.py config` to display the effective host configuration and
detected capabilities without opening a project or contacting Ollama.

The launcher reads optional `.local/agent-config.json`. Set
`LOCAL_CODING_AGENT_CONFIG` to use a different host-local config file. This file is ignored by
Git because project roots and local model preferences belong to each installation.
Without it, Windows keeps the established project names and roots. On macOS and
other POSIX hosts, the current LocalCodingAgent checkout is the only default project;
add other authorized project roots to the host-local configuration. The default model
qwen3:8b and the Ollama endpoint remain loopback-only.
A configured project uses a stable lowercase
ID independent of its local checkout path:

```json
{
  "version": 1,
  "projects": {
    "kronosstage": {
      "name": "KronosStage",
      "root": "D:/Workspaces/KronosStage"
    },
    "local-agent-repair-fixture": {
      "name": "LocalAgentRepairFixture",
      "root": "D:/Workspaces/LocalCodingAgent/.local/tests/live-repair-001/repo"
    }
  },
  "inference": {
    "provider": "ollama",
    "model": "qwen3:8b",
    "endpoint": "http://127.0.0.1:11434"
  }
}
```

Each configured root must still be the exact registered Git worktree. Configured
endpoints remain restricted to IPv4 loopback Ollama. The stable ID is recorded
alongside run/takeover evidence for cross-installation attribution. Existing
checkout identity remains path-derived and continues to bind permits, Git
snapshots, memory checks, and command receipts. Moving a checkout therefore does
not silently transfer an approval or make two machines' receipts interchangeable.

The launcher detects a small set of fixed host capabilities before advertising
tools. Python source parsing is available on supported hosts. The `run_command`
schema lists only the commands available on this host. The fixed PowerShell
file-information operation appears only on Windows when `powershell.exe` is
present; both schema validation and direct broker execution reject it elsewhere.
Platform detection does not add a general shell capability.

Configuration, registered roots, memory, and run history remain local to each
installation. Stable logical project IDs can match across installations, while
each host resolves its own root and retains its own checkout-bound approvals and
receipts. The default provider remains local Ollama on IPv4 loopback. Version 3
configuration can explicitly select Ollama through a managed SSH local forward.
Version 1 and version 2 configurations remain readable for local operation; the
older unmanaged remote profile is rejected. The SSH transport is inference-only;
all tools and authorization remain on Windows. Direct LAN Ollama and remote
execution are not supported.

### Explicit Mac inference profile (prepared; hardware not validated)

Remote inference is disabled by default. When explicitly selected, the Windows
agent starts a run-scoped OpenSSH tunnel, verifies the enrolled host key, and
checks that the listener belongs to that exact SSH process. The Mac Ollama
service must listen only on its own loopback interface. Do not bind it to
LAN/public interfaces or configure router forwarding.

Version 3 keeps the local provider active unless the operator changes `provider`
to `ssh_tunnel_ollama` and sets `remote.enabled` to `true`. The forwarded port
must be in 20000–29999 and must match the endpoint exactly. The remote profile
records a stable profile ID, SSH alias, expected SSH host-key SHA-256 fingerprint,
and the `ssh-agent` credential reference. Credential values and private keys do
not belong in this file. Configure the SSH alias to use the pinned `known_hosts`
entry and confirm its fingerprint out of band before starting the tunnel.

```json
{
  "version": 3,
  "projects": {
    "kronosstage": {"name": "KronosStage", "root": "D:/Workspaces/KronosStage"}
  },
  "inference": {
    "provider": "ollama",
    "model": "qwen3:8b",
    "endpoint": "http://127.0.0.1:11434",
    "remote": {
      "enabled": false,
      "profile_id": "mac_studio",
      "port": 22001,
      "ssh_host_alias": "mac-studio-inference",
      "ssh_host_key_sha256": "SHA256:REPLACE_WITH_VERIFIED_FINGERPRINT",
      "credential_ref": "ssh-agent",
      "known_hosts_file": "C:/Users/you/.ssh/mac_known_hosts",
      "ssh_config_file": "C:/Users/you/.ssh/mac_config"
    }
  }
}
```

Selecting remote inference requires both `provider: "ssh_tunnel_ollama"` and
`endpoint: "http://127.0.0.1:22001"`; the remote profile must be enabled. The
app rejects arbitrary hosts, ports, credentials in URLs, query strings,
fragments, and redirects. Requests remain non-streaming, bounded to 2 MiB
responses, and use the fixed Ollama API path allowlist. The remote model receives
only inference prompts and tool schemas/results; it gets no Windows filesystem,
shell, repository, approval, or broker connection. Tool proposals are validated
and executed by the Windows broker.

Before launching OpenSSH, the agent runs `ssh-keygen -F` against the explicitly
selected known-hosts file and hashes the returned public host key; it requires
one exact match with the configured fingerprint. OpenSSH then uses that same
known-hosts file with strict checking, public-key-only batch authentication,
password prompts disabled, agent forwarding disabled, cleared forwards, and
`ExitOnForwardFailure`. The SSH process is started with `-N` and only the fixed
`127.0.0.1:PORT:127.0.0.1:11434` forward. Windows listener ownership must match
the spawned process before any HTTP request is sent. Tunnel process and listener
ownership are checked again before each request, and only the owned process is
terminated during cleanup. A stale/missing listener, wrong key, occupied port,
or SSH failure aborts inference by default. A configured version 4 fallback may
change provider only under the explicit fallback policy below.

#### Host-key enrollment and Mac prerequisites

1. On the Mac's local console, inspect its SSH host key fingerprint, for example
   with `sudo ssh-keygen -lf /etc/ssh/ssh_host_ed25519_key.pub -E sha256`.
   Transfer the public host key to Windows through a trusted out-of-band method;
   do not trust `ssh-keyscan` output as enrollment evidence.
2. Put the exact host public key in a dedicated Windows known-hosts file under
   the configured alias. Set the configured SHA-256 fingerprint to the value
   verified from the Mac console. The manager compares it with the key actually
   returned by `ssh-keygen -F` for that alias.
3. Create a dedicated Windows SSH config file with that alias, the Mac's private
   network address, SSH username, and port. Do not add `ProxyCommand`, local
   commands, or unrelated forwarding rules. Use a public key loaded in the
   Windows OpenSSH agent; never put private key text in agent configuration.
4. On the Mac, enable Remote Login only for the dedicated account/key. Restrict
   that `authorized_keys` entry to forwarding with the OpenSSH options
   `restrict,port-forwarding,permitopen="127.0.0.1:11434"`; use a dedicated key.
   Configure Ollama to listen on `127.0.0.1:11434` only.
5. Save the explicit `known_hosts_file` and `ssh_config_file` paths in the
   ignored host-local agent configuration. The app starts and stops the tunnel
   per run; it does not keep a background SSH process alive.

If startup fails, check the enrolled fingerprint, alias, selected SSH config,
OpenSSH availability, agent-loaded public key, and Mac Remote Login logs. An
occupied forwarded port is rejected rather than reused. If a run is interrupted,
the owned SSH child is terminated and waited on, then killed only if that same
child does not exit; unrelated SSH processes are not enumerated or terminated.
If cleanup cannot confirm listener release, remote inference remains unavailable
until the port is clear and a new run revalidates the host key.

### Controlled provider fallback (configuration version 4)

Windows Ollama remains the default provider. Version 4 adds a fallback policy;
the default is `disabled`, which stops a run on provider failure. Fallback is
preflight only: a failed health/model check may switch providers before task
context is sent. Inference requests are never automatically replayed and a
failure during generation stops the run. There is no cloud fallback.

The policy supports four modes:

- `disabled`: do not substitute providers.
- `ask`: display the source and target model, then require the operator to type
  `YES`. If the target is remote, the prompt explicitly says task context will
  be sent to the configured Mac provider.
- `local_only`: use only exact Windows Ollama substitutions.
- `approved`: permit only rows with both `operator_approved` and
  `data_sharing_authorized` set to `true`.

Each substitution binds an exact provider/model pair (or an explicit `*` source
model), target model, and maximum context. Target models are rediscovered and
verified before the run; the context ceiling must cover the requested context.
Authentication, host identity, redirect, malformed response, and other
security errors fail closed and do not trigger fallback. Keep remote enabled
only when explicitly selecting it or configuring an intentional remote
substitution.

Example Windows-local primary with ask-before-remote fallback:

```json
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
```

Place this object alongside `provider`, `model`, `endpoint`, and `remote` under
`inference` in a version 4 host-local configuration. The remote profile uses
the same managed SSH settings documented above. These provider choices never
change Windows tool authorization: all model tool calls are revalidated and
executed by the Windows broker. Tunnel, authentication, health, timeout,
cancellation, and provider errors are recorded using sanitized error codes;
credentials and full prompts are not included in provider diagnostics.

Automated provider/tunnel tests use mocks. The Mac has not arrived, so real
host-key enrollment, macOS SSH behavior, Ollama service compatibility, and
performance remain unverified.

Actual Mac connectivity and host identity remain unvalidated until the hardware
is available. The listener ownership query uses Windows `Get-NetTCPConnection`;
unsupported or failed ownership checks fail closed.

### Evidence-based model routing

The launcher treats the **agent role**, **model**, and **provider** as separate
inputs. A role such as `reviewer` is descriptive routing context; it grants no
tools or permissions. Models are discovered from authorized providers at run
time. Windows Ollama remains the default. The SSH-tunneled provider is considered
only when the host configuration enables it and the operator selects
`--routing-mode remote_allowed` or has configured it as the primary provider.
Routing never changes the independent Milestone 2 fallback policy.

Task classification uses bounded deterministic keyword rules and can assign
several categories (for example, debugging plus test generation). The router
first excludes unavailable, unauthorized, explicitly excluded, context- or
memory-incompatible models, and models with measured capability scores below a
minimum. It then ranks eligible candidates by operator preference, recent
verified outcomes, capability evidence, measured latency, and stable provider
and model identifiers. Outcome rates and latency affect ranking only after at
least three recent receipts in the same task category; smaller samples remain
visible but do not rank models. Missing evidence produces a low-confidence
`uncertain` decision. With `--model auto`, the configured model is the
conservative default when it is available and has no known constraint failure.
Model names and model-reported claims do not establish capabilities.

The version 1 registry is an operator-maintained, private file at
`.local/model-capabilities.json`; it is not created automatically and is
ignored by Git. Profiles are keyed by provider, model ID, and digest. Capability
values range from 0 to 1 and every value carries a provenance source:
`verified_measurement`, `operator_provided`, or `unverified_estimate`. Verified
measurements require timestamped SHA-256 evidence references. Context,
tool-calling, estimated memory, limitations, and benchmark references are
separate fields. The router discards verified profile measurements older than
90 days; a changed digest no longer matches its profile.

Example profile (replace identifiers and evidence references with operator-
verified values; do not copy these sample scores as facts):

```json
{
  "schema_version": 1,
  "models": [{
    "provider_id": "ollama",
    "model_id": "qwen3:8b",
    "model_digest": "sha256:ACTUAL_OLLAMA_DIGEST",
    "capabilities": {
      "code_generation": {
        "value": 0.7,
        "source": "verified_measurement",
        "evidence_refs": [{
          "kind": "pinned_test_report",
          "uri": ".local/evaluations/qwen3-code.json",
          "sha256": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
          "observed_at": "2026-11-15T12:00:00+00:00"
        }]
      },
      "test_generation": {
        "value": 0.7,
        "source": "operator_provided",
        "evidence_refs": []
      }
    },
    "tool_calling": {
      "value": true,
      "source": "operator_provided",
      "evidence_refs": []
    },
    "context_window": {
      "value": 32768,
      "source": "operator_provided",
      "evidence_refs": []
    },
    "estimated_memory_gib": {
      "value": 8,
      "source": "unverified_estimate",
      "evidence_refs": []
    },
    "known_limitations": ["Evidence is task-specific."],
    "benchmark_evidence": []
  }]
}
```

The private `.local/model-performance.jsonl` store receives records only when
the host has an unchanged test receipt with a passing or failing result. It
records inference-call latency, throughput when available, context usage,
provider/model identity, task category, timestamp, and a hash-bound run-result
reference. It does not treat model prose as verification. Rows older than 90
days are ignored. Passing tests are evidence of those tests only; they do not
prove that every natural-language task requirement was met.

Examples:

```powershell
# Automatic local route; reports an uncertain selection if profiles are absent.
agent.py run --project KronosStage --task "Debug and add regression tests" --model auto --routing-mode local_only

# Explicit model override, still checked for availability, provider authorization,
# context, exclusions, and any registered capability failures.
agent.py run --project KronosStage --task "Review the parser" --model qwen3:8b --role reviewer --context 16384 --max-context 32768

# Allow discovery across Windows and an explicitly enabled SSH-tunneled Mac profile.
agent.py run --project KronosStage --task "Plan the migration" --model auto --routing-mode remote_allowed --preferred-model ssh_tunnel_ollama/qwen3:14b --exclude-model qwen3.5:4b

# Operator-declared minimums and a conservative memory ceiling.
agent.py run --project KronosStage --task "Generate tests" --model auto --minimum-capability test_generation=0.7 --max-memory-gib 24
```

The structured `routing_decision` is included in the run result and CLI JSON;
it lists classifications, candidate exclusions, evidence sources, confidence,
constraints, and selected provider/model. It contains no task prompt. The public
`model_router.route_model` interface also accepts agent role, priority, provider
availability, resource limits, and estimated workload for later scheduler use;
this milestone does not implement a scheduler or concurrent agents.

For deterministic inspection without any model request:

```powershell
& 'C:\Users\dsmus\AppData\Local\Programs\Python\Python312\python.exe' -B `
  'D:\Workspaces\LocalCodingAgent\agent.py' inspect --project KronosStage
```

Keep `.cmd` launchers when PowerShell script execution is restricted; no execution
policy change is needed. A PowerShell wrapper exists but may be blocked by policy.

## What actually works

- Local Qwen made real tool calls, fixed `calculator.py`, created `stats.py`, and
  ran pinned unittest tests in a disposable repository. Native result:
  `.local/native-validation/20261113T051932277537Z/run/result.json`.
- A Qwen-generated updated handoff was saved to that fixture's existing pair of
  authoritative memory files, preserving history; the Codex-side adapter read it.
- Real KronosStage source inspection completed read-only. The model's summary
  omitted requested hold/commit facts; wrapper policy displays verified commit and
  makes authorization/hold status explicit. Treat model next actions as proposals.
- Final short local smoke called `git_status`, completed in about 4.6 seconds and
  left source/memory unchanged. Evidence is recorded in the private handoff.
- Arbitrary shell commands, deletion tools, private paths and unauthorized edits
  are blocked. Permitted commands are narrowly typed. General project tests are
  disabled without a qualified OS sandbox.

The pure-Python test executor is a narrow validation capability: pinned trusted
tests and restricted numeric functions. It is **not** a general Python/OS sandbox.
Do not weaken it to run arbitrary project tests. The native Codex route is intended
to provide broader sandboxed execution after the separate host-side safety gate.

## Native Codex gate

The user ran this in normal PowerShell and reported `MXC_OK`:

```powershell
& 'D:\Workspaces\LocalCodingAgent\Test-NativeCodex.cmd'
```

Nested MXC is unavailable inside this Codex executor. That does not prevent the
normal host from supporting MXC. The next optional **manual** check is prepared,
syntax-parsed but not run:

```powershell
& 'D:\Workspaces\LocalCodingAgent\Validate-NativeCodex.cmd'
```

It creates only a new disposable repository, checks filesystem sentinels and, if
they pass, attempts installed Codex against local Ollama. It makes no global install
or configuration changes. It may take several minutes. Inspect its recorded result
before calling native Codex tool/model compatibility validated. No existing project
is its target. Do not use full-access or permissive legacy fallback to bypass failure.

## Interactive Codex with local Ollama

After the native validation gate passes, start an interactive Codex session through
the isolated local provider profile:

```powershell
& 'D:\Workspaces\LocalCodingAgent\Start-Codex-Ollama.cmd' -ProjectPath 'D:\path\to\your\project' -Preview
& 'D:\Workspaces\LocalCodingAgent\Start-Codex-Ollama.cmd' -ProjectPath 'D:\path\to\your\project' -TrustProject
```

The launcher creates a new short-path profile under `.local/co` for every run.
The short path is needed by Codex's Windows app-server socket. It does not read or
modify `%USERPROFILE%\.codex`. It selects the explicit
loopback Ollama Responses provider, disables web search, removes inherited API
keys/tokens/cloud credentials from the Codex process, and uses `workspace-write`
with `on-request` approvals. Codex CLI and an already-running local Ollama server
are required; nothing is installed or downloaded. The default model and 8192 token
context match the tested Windows alias. Override them with `-Model` and
`-ContextWindow` for another installed local model. Use `-Preview` to generate and
inspect the isolated profile without starting Codex or sending a model request.
The launcher uses Codex's `--no-daemon` mode so isolated runs do not leave a shared
app-server running in the background.
`-TrustProject` opts that project into its project-level Codex settings for this
isolated profile; those settings may run project hooks. Trust is not set unless the
switch is passed. Codex may still show a folder-trust confirmation before opening
the project. On Windows, the profile selects the MXC sandbox validated by
`Test-NativeCodex.cmd`; it does not invoke the administrator setup or legacy fallback.

## Shared memory and authorization

`agent_core.py` preserves each project's AGENTS.md and existing
`.local/PROJECT_CONTEXT.md` / `.local/DURABLE_MEMORY.md` authority. Historical
retrieval is explicit and bounded. Memory saves require reviewed authorization,
expected Git/memory snapshots, a lock, history backups and a transaction journal.
Readers reject an incomplete pair; crash recovery currently requires manual review.
`MEMORY_COMPATIBILITY.md` records the compatibility plan audited before implementation.

`agent.py prepare-bridges` produces scoped Claude settings that enable existing
AGENTS.md discovery and disable a parallel Claude auto-memory store. Its append
instruction file contains current bounded memory. No protected CLAUDE.md or global
settings are replaced. Actual Claude model inference has not been tested or launched.

Source-edit permits and reviewed handoff proposals are implemented in `agent.py`;
their public CLI workflows need further validation. Do not treat them as production
acceptance. Existing dirty worktrees are refused for edits, and permits bind exact
task/state/paths. Inspection never resumes a saved hold or authorizes hardware/data work.

## Current limitations

- Latest full suite: **166 passed, 1 skipped**. The skipped symbolic-link test
  requires a Windows privilege unavailable in this account; junction rejection
  and the exact-16-KiB section boundary tests passed. POSIX symbolic-link behavior
  remains unverified on this Windows host.
- Windows native Codex execution passed one compact Qwen3.5 4B / 8k disposable edit-and-test
  task, including all filesystem sentinels and the three fixture tests. This is one
  successful sample, not a broad model-reliability claim.
- Interactive Codex with Qwen3.5 4B completed a read-only tool call, but its broad
  summary included stale claims. A later exact extraction matched the README.
  Interactive coding attempts with Qwen3.5 4B repeated file reads; Qwen3 8B issued
  malformed PowerShell writes and produced a UTF-16 `stats.py` in the disposable
  fixture, which failed Python import with null bytes. The root worktree was not
  changed by these trials. Use the validated `codex exec` fixture route for coding
  evidence until a larger-model interactive task succeeds.
- Qwen3 8B's noninteractive probe used the fixture's `approval_policy = "never"` and
  refused to run commands, so it is not a comparable model trial. Mac model/context
  sizing remains untested; the Mac's 128 GiB unified memory is a separate evaluation.
- Claude settings/context accessibility is prepared; actual Claude comprehension
  is not tested. No cloud agent was launched to manufacture a pass.
- Two-file replacement is recoverable, not OS-atomic across both files. Locks
  coordinate cooperating clients; outside editors must also be coordinated.
- Read-only takeover succeeded after a context-budget failure was diagnosed.
  Model instruction compliance is imperfect; no claim of Codex-level reliability.

Emergency stop: Ctrl+C stops the launcher; its owned bounded command is cleaned up.
Creating a `STOP` file in a run's execution directory stops between model calls.
An in-flight Ollama request may finish before cancellation is observed. Never kill
all Python/Ollama/Codex processes or stop an unrelated session.

## Current validation update — 2026-11-13

The dated setup text above is a historical checkpoint. Current progress and
verified experiment receipts are recorded in `.local/CONTINUATION_20261113.md`.
The repair fixture is registered as `LocalAgentRepairFixture`; it is a disposable
experiment result and currently contains modified `calculator.py` plus new
`stats.py`. Do not reset it as part of routine cleanup.

The latest framework regression run passed **116 tests**, with one symbolic-link
creation test skipped because this account lacks the Windows symlink privilege. The
Windows junction rejection test passed. This does not
establish general OS sandboxing.

To compare existing saved runs without launching inference or contacting a model:

```powershell
& 'C:\Users\dsmus\AppData\Local\Programs\Python\Python314\python.exe' `
  'D:\Workspaces\LocalCodingAgent\model_evaluation.py' `
  'D:\Workspaces\LocalCodingAgent\.local\runs\<run-id>\execution'
```

The report groups verified completions, test outcomes, tool errors, steps, elapsed
time, and available token/context metrics by model. Older runs without those fields
are reported as unavailable rather than treated as zero. Use multiple execution
directories to compare runs; small samples are diagnostic, not a model ranking.
The first three-model sample and its limitations are recorded in
`MODEL_EVALUATION.md`.

## Saving committed work to GitHub

`Sync LocalCodingAgent to GitHub.cmd` runs the repository-specific sync helper.
It checks the repository root, configured `origin`, and current branch, then
performs a non-forced push of the current branch after interactive confirmation.
Use `--dry-run` to check whether Git accepts the push without updating the remote.
The helper never stages or commits files; it reports uncommitted work so it is
clear that only existing commits are uploaded. Configure the intended `origin`
remote before using it.
