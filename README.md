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
