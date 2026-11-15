# Windows standalone smoke checklist

This checklist validates the Windows installation without a Mac, network inference,
or cloud fallback. It uses the already registered `LocalAgentRepairFixture` and
the installed local Ollama model. It does not authorize source writes.

## Preconditions

- Run from `D:\Workspaces\LocalCodingAgent` in PowerShell.
- Python 3.12, Git, and Ollama are installed. The current Windows launcher uses
  the fixed Python 3.12 path in `Start-LocalCodingAgent.cmd`.
- `agent.py config` lists `LocalAgentRepairFixture` and the exact fixture Git root.
- The fixture is the registered disposable repository. Preserve all existing
  fixture changes; do not reset, clean, or overwrite it.
- The selected model is already installed. Do not download models during this
  smoke run.

## Checks

1. Read the effective host configuration without contacting Ollama:

   ```powershell
   py -B .\agent.py config
   ```

   Confirm `platform` is `windows`, the provider is `ollama`, the endpoint is
   `http://127.0.0.1:11434`, and `general_shell` is unavailable. Confirm the
   output contains no credentials. Configuration display should not create or
   modify files.

2. Confirm the expected local model exists without downloading anything:

   ```powershell
   ollama list
   ```

   Select an installed model such as `qwen3.5:4b`; do not assume the configured
   default is installed.

3. Verify the fixture registration before running it:

   ```powershell
   git -C 'D:\Workspaces\LocalCodingAgent\.local\tests\live-repair-001\repo' rev-parse --show-toplevel
   git -C 'D:\Workspaces\LocalCodingAgent\.local\tests\live-repair-001\repo' status --short
   ```

   Stop if the resolved Git root differs from the configured root. Record the
   initial status; do not clean existing changes.

4. Run the bounded read-only acceptance task with the installed model:

   ```powershell
   & .\Start-LocalCodingAgent.cmd --project LocalAgentRepairFixture --model qwen2.5-coder:7b-instruct --task "Read AGENTS.md, README.md, calculator.py, stats.py, and tests/test_stats.py. Run only the approved pinned tests. Report marker MAPLE-427 and summarize the observed results. Do not modify files."
   ```

   No write permit is supplied. Do not add one for this check. The only test
   capability available for this project is its registered pinned test config.

5. Review the generated run under `.local\runs\<run-id>\execution`:

   - `result.json` identifies the selected local provider/model.
   - Completion is `completed_verified` only if the pinned test receipt is valid
     and current.
   - The summary reports `MAPLE-427` only if it was actually read.
   - No writes are recorded, `cloud_fallback` is false, and command receipts
     show only the approved pinned tests.
   - Fixture source and test hashes and the Git status match the initial check.

## Stop conditions

- Stop if configuration is malformed, the fixture root is not exact, the model
  is not installed, credentials appear in output, or any source changes occur.
- A model response alone is not a pass. Require actual read and test receipts.
- Do not stop or reconfigure the user's Ollama service to simulate an outage;
  provider and timeout failure behavior belongs in isolated tests.
- Do not enable a network endpoint, cloud provider, arbitrary shell, or write
  permit as a workaround.

## Coverage limits

This validates one Windows host, one installed model, and one bounded fixture
task. It does not establish general model reliability, macOS behavior, distributed
inference, process-tree cleanup, or production-project safety. The native
macOS CI matrix is prepared but has not run.

## Windows smoke observations — 2026-11-15

The `py` launcher returned `No installed Python found` for a full `run --project
...` invocation. This launcher issue was avoided by using the supported
`Start-LocalCodingAgent.cmd`, which pins Python 3.12.

Two Qwen 3.5 attempts (direct Python 3.14 and the `.cmd` launcher) read all five
files and passed all three pinned tests. Both then emitted a native `finish`
call with the summary nested under an unexpected `arguments` field. Strict
validation rejected each call, and both runs were recorded as failed. Evidence:
`.local/runs/20261115T003851003549Z-725aecbd/execution/` and
`.local/runs/20261115T004009998701Z-4f97e3c3/execution/`.

The Qwen 2.5 Coder run through the `.cmd` launcher completed the same task in
seven steps: all five reads, three passing pinned tests, marker `MAPLE-427`, and
a valid final summary. It was `completed_verified`, with zero writes and no cloud
fallback. Evidence: `.local/runs/20261115T004026683726Z-98756c18/execution/`.
The fixture's pre-existing `M calculator.py` and `?? stats.py` status remained
unchanged throughout.

This validates the Windows standalone launcher and provider path for one model
and one fixture task. The Qwen 3.5 formatting failure is model-specific evidence;
do not relax native tool-call validation to turn its failed runs into passes.
