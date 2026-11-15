# Local coding-agent setup — working checkpoint

Work stopped at the user's request to preserve Codex usage. This is a partially
validated implementation, not a finished general-purpose coding environment.
Private resume record: `.local/SETUP_HANDOFF.md`. Do not delete `.local` or clean
this repository: source is presently uncommitted/untracked and evidence is private.

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
receipts. The current provider profile is still local Ollama on IPv4 loopback;
LAN inference and remote execution are separate future phases.

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

- Latest full new-suite result: **20 pass, one error out of 21**. An earlier run
  passed all 21 before an exact-boundary test was added. Current known bug: a valid
  exactly-16-KiB current section can fail while looking ahead to the next heading.
  It fails closed without returning a partial section. See saved logs/handoff.
- Broader native Codex execution and model compatibility remain unvalidated.
- Other installed Qwen models have not received the same comparative coding trial.
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
