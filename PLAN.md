# Local coding agent implementation

**Historical handoff:** The prior low-usage stop and `.local/SETUP_HANDOFF.md`
remain in force for work outside the narrowly authorized integration milestone
below. Recalling project memory alone does not resume held work.

## Unified Integration Milestone 1 — 2026-11-22

**Status: audit and bounded demonstration complete; proposal rejected on review.**

- Reused the existing Windows `ssh_tunnel_ollama` provider, managed OpenSSH
  tunnel, pinned host-key validation, bounded Ollama client, and normal agent
  orchestration. No competing agent or remote execution service was introduced.
- Verified the configured route to Mac Ollama through the managed SSH tunnel:
  Ollama `0.40.17`, model `qwen3-coder:30b`, digest
  `06c1097efce0431c2045fe7b2e5108366e43bee1b4603a7aded8f21689e90bca`. The
  health/model check used no task prompt and the tunnel was closed afterward.
- Ran one conversational-mode proposal in a disposable Git fixture with no
  model tools. The two-file diff passed path allowlisting and `git apply --check`,
  but manual review found that `isinstance(value, int)` accepts `bool`. It was
  not applied or executed; treat this as a rejected proposal, not a successful
  coding change.
- Focused integration/security regression: 136 passed. Full discovery: 567 run,
  two errors (missing optional `pycdlib`; syntax error importing
  `diagnostic_witness_service.py`). No product source changes were made for this
  milestone.

## Unified Integration Milestone 2 — 2026-11-23

**Status: complete after recovery, separately approved replacement, and passing verification.**

- The original Milestone 1 task prompt was unavailable. The operator supplied a
  new authoritative replacement specification for `clamp_percent`; it is not
  represented as recovered historical task text.
- Reverified the disposable repository at clean baseline
  `e1ba61d4eb627d1eefb36036d84214b7e720d499`, reused the pinned managed
  `ssh_tunnel_ollama` route, confirmed Ollama `0.40.17` and
  `qwen3-coder:30b` digest
  `06c1097efce0431c2045fe7b2e5108366e43bee1b4603a7aded8f21689e90bca`, and
  closed the loopback-only tunnel after one no-tools request.
- The first separately approved proposal had malformed hunk counts in both
  files (5 declared/8 present in source, 20 declared/24 present in tests).
  `git apply --check` passed but application consumed only each declared count.
  A fresh clone reproduced the partial state byte-for-byte.
- The corrected patch hash
  `957edaa7ea9aa17a4b32e98766fc5b2e376a0bda13483e246c597883d2d38934` declared
  and validated all 8 source and 24 test additions. A separate baseline
  simulation passed `git apply --check` and matched the intended CRLF worktree
  bytes exactly. After separate approval, the existing partial files were
  copied to new immutable evidence, then replaced with the approved intended
  bytes at only `src/progress.py` and `tests/test_progress.py`.
- The disposable command passed: 3 test methods, 0 failures, 0 errors, covering
  all 10 required inputs. Focused inference, tunnel, authorization, recovery,
  and diagnostic regression suites passed: 14 suites, 193 tests, 0 failures,
  0 errors. Full discovery was not rerun; the earlier optional `pycdlib` and
  `diagnostic_witness_service.py` errors remain unverified in this recovery.
- Two verified candidate lessons were recorded locally and not promoted:
  exact Python bool/int checks and unified-diff hunk-count validation.
- The disposable repository remains at baseline commit
  `e1ba61d4eb627d1eefb36036d84214b7e720d499` with only the two authorized files
  untracked. No production code or Broker permissions changed.
- Evidence: `.local/tests/unified-coding-demo/milestone2-recovery-20261123T162731Z-6defa190d5/`
  and `.local/tests/unified-coding-demo/milestone2-apply-20261123T163425Z-a77bf72ccc/`.

### Milestone 2 outcome

Milestone 2 acceptance criteria are satisfied. Milestone 3 was not started.

### Historical next smallest milestone from Milestone 1

The following Milestone 1 continuation is retained as historical context and
was superseded by the replacement specification and Milestone 2 record above.

With separate authorization, repeat the disposable coding proposal with a
corrected exact-integer contract, review the diff, obtain explicit approval
before applying it to the disposable repository, then run only its local test.
After that, decide whether to add focused automated coverage for the currently
verified Mac route; do not expand into autonomous or production execution.

The historical local-agent roadmap follows. Its prior statements about the Mac
being held describe the earlier task state and do not supersede the scoped live
verification recorded above.

1. Discovery and memory compatibility audit: complete; private records in .local.
2. Probe the installed Codex 0.167.0 local provider in an isolated fixture/home.
   Keep local inference explicit, remove cloud credentials from child environment,
   refuse model downloads, and test permissions rather than assuming enforcement.
3. Implement a launcher and bounded adapter around the established memory files.
   No global installation/configuration changes. Test existing installed models.
4. Prove real tools/edits/tests/handoffs on disposable Git fixtures; rehearse real
   project takeover read-only. Keep failures and unsupported cases visible.
5. Document commands, evidence, safety boundaries, emergency stop and future endpoint.

Do not modify KronosStage/ChasingLight application code, AGENTS.md, existing memory
or user configurations during implementation/validation. New code belongs here.
Any required global installation or configuration awaits separate approval.

## Unified Integration Milestone 3 — 2026-11-23

**Status: partial validator implementation; workflow and live demonstration not complete.**

- Added `patch_validation.py`, a dependency-free parser/reconstructor that
  checks hunk body counts, exact context, full patch consumption, exact path
  allowlists, and rejects traversal, symlink targets, renames, deletions,
  binary/mode changes, and ambiguous boundaries. It computes SHA-256 over the
  exact patch bytes and reconstructed expected file bytes.
- Added focused regression tests including an under-counted `clamp_percent`
  addition modeled on the Milestone 2 failure, including Git index header and
  new-file symlink escape cases. Validator tests passed 8/8.
- Re-ran the 14 focused inference, tunnel, authorization, recovery, and
  diagnostic suites: 193 passed, 0 failures, 0 errors.
- Evidence: `.local/tests/milestone3-validator-final-20261123T090737-9f5f19c8/`
  (final 8/8 validator and 193/193 regression outputs).
- The patch-bound approval adapter, workflow state machine, controlled apply
  and test integration, candidate capture, and live Mac proposal remain
  incomplete. No code was applied to a disposable or production repository and
  no live Mac request was made in this partial implementation.

### Milestone 3 remaining work

Integrate proposal generation with the existing provider, implement a one-use
approval contract binding repository identity/baseline, patch hash, allowlist,
and predefined tests, then route approved reconstructed bytes through the
existing Broker and fixed test capability. Add state/evidence and candidate
capture, test these interfaces on a new disposable fixture, and perform the
live Mac proposal through the strict validator. Do not claim Milestone 3
complete until approval-bound application and tests pass.

### Milestone 3 continuation — patch-bound workflow (2026-11-23)

**Status: complete for Milestone 3; approved disposable application, tests, and
candidate evidence are verified. No production project was modified.**

- Added `coding_workflow.py`, integrating the existing SSH-tunnel Ollama
  provider, strict patch validator, Broker write/test authority, recovery
  journal, and candidate `SkillLibrary` workflow. The model is invoked with no
  tools. Workflow state and evidence are persisted per run.
- Approval binds disposable repository identity, baseline commit and file
  hashes, patch SHA-256, reconstructed post-image hashes, exact allowlist,
  pinned test profile/configuration, fixed Broker test command, operator, and a
  unique one-use approval record. Applying consumes approval durably before
  writes; partial/interrupted writes become terminal and are never replayed.
- Added `coding-propose`, `coding-status`, `coding-approve`, `coding-reject`,
  and `coding-apply` launcher actions. Only disposable repositories under the
  milestone fixture root are eligible. Proposals are applied only in a
  separate disposable simulation before approval; the pinned acceptance tests
  must pass there.
- Validator and workflow tests cover the Milestone 2 under-counted hunk,
  unconsumed content, traversal/symlinks, unauthorized files, hashes, stale
  state, approval binding/reuse, post-image mismatch, partial writes, recovery,
  timeout/cancellation, fixed tests, and candidate evidence persistence.
- Final focused regression invocation passed **258 tests, 0 failures, 0
  errors** across 19 inference, tunnel, authorization, recovery, diagnostic,
  harness, candidate, validator, and workflow modules. Validator/workflow/
  shared-memory focused tests passed **51/51**. An initial invocation had
  test-loader import errors because legacy tests import siblings by bare name;
  the corrected invocation included the tests directory in `PYTHONPATH` and
  passed. No code change was made for that invocation issue.
- Live run `8a9ba8feea9d465ea4b0edcea26988bb` used the existing pinned-host,
  loopback-only `ssh_tunnel_ollama` provider and `qwen3-coder:30b`. Strict
  validation, isolated `git apply --check`, simulated post-image byte check,
  and the 3-test pinned acceptance suite passed. Patch SHA-256:
  `2b045865730b8b0ce6e277b206025fd17e1e5d8022080f51337d484998974cb8`;
  expected `src/progress.py` SHA-256:
  `07d3ccebb1aed05af8a2c0faa9986897a8ca7aa80a9a78c1840b6c8b6f0e8825`.
  Destination: `.local/tests/milestone3-live-bacc8b0366df/repo`, baseline
  `054e52f05a0a3cf4b69f457090f6ba9a0a91e798`; only `src/progress.py` was
  authorized. Explicit approval `4d6cd441cf7949349f4b740665f92b89` was
  consumed once. Broker wrote the file and verified its approved SHA-256
  `07d3ccebb1aed05af8a2c0faa9986897a8ca7aa80a9a78c1840b6c8b6f0e8825`.
  Final disposable status contains only `?? src/progress.py`.
- The post-application fixed Broker test capability passed 3/3 tests, with
  zero failures and errors. Output SHA-256:
  `151f2e1d55362efd03e5cddecf1f6f76d8fea9976b2a80ca4b25f0426872341f`.
- Candidate `skill-3d78e16793a7034177a718f1` is persisted in the existing
  SkillLibrary with the exact-integer lesson, test-verified Broker receipt,
  and source provenance. It remains an unpromoted candidate.
- The previously known full-discovery errors involving optional `pycdlib` and
  unrelated `diagnostic_witness_service.py` remain tracked and were not
  modified or reclassified.

Evidence report: `.local/tests/milestone3-final-20261123-8a9ba8fe/report.md`;
workflow, approval, Broker journal, receipt, and candidate artifacts remain in
the disposable repository's `.local` evidence directories. No further
Milestone 3 work remains.

### Milestone 4 — Multi-agent coding and unified dashboard (2026-11-23)

**Status: live proposal reached the human approval checkpoint; no patch has
been applied.**

- Extended the existing coding workflow with sequential planner, coder, and
  independent reviewer invocations through the configured provider. Models
  receive zero tools; reviewer findings remain advisory and deterministic
  validation plus operator approval are required before the Broker path.
- Added the loopback-only authenticated coding dashboard on top of the existing
  workflow. It accepts bounded task submissions for explicitly authorized
  disposable fixtures, displays role outputs/diff/validation/test status, and
  exposes cancellation. It has no approval, patch-application, or test-execution
  endpoint. The live run was submitted through its authenticated local API;
  interactive browser UI automation was unavailable in this Windows session.
- Live run `dbde4b6fac7c447e89d050db561d26e1` used
  `ssh_tunnel_ollama` and `qwen3-coder:30b` for all three separate calls.
  Planner output was schema-valid and scoped to `src/progress.py`; the reviewer
  returned `no_blocking_findings` with no findings. Two earlier coder attempts
  in the bounded demonstration were rejected by strict hunk validation: the
  first declared 10 additions but contained 8, and the second declared 8 but
  contained 7 and omitted the final in-range return. Neither was applied.
- Final state is `awaiting_approval`, with no approval record. Destination:
  `.local/tests/milestone4-live-8a12f7d4216a4ceea723c1dd0b60f89e/repo`,
  baseline `054e52f05a0a3cf4b69f457090f6ba9a0a91e798`, still clean. Exact
  proposed file: `src/progress.py`. Patch SHA-256:
  `bc2ef607c219fcd4cb4ee4ca439dc8195fe8b48595df5b9eabdccef619a20960`.
  Expected post-image SHA-256:
  `07d3ccebb1aed05af8a2c0faa9986897a8ca7aa80a9a78c1840b6c8b6f0e8825`.
  Strict path validation, hunk accounting, isolated `git apply --check`,
  simulated post-image, and pinned fixed tests passed. Simulation profile
  `clamp_percent_v1` passed 3/3, with zero failures or errors. No live
  application or Broker execution occurred.
- Focused regression run passed **297 tests, 0 failures, 0 errors** across 21
  inference, tunnel, authorization, recovery, diagnostic, orchestration,
  harness, SkillLibrary/shared-memory, validator, coding workflow, and
  dashboard suites. The old full-discovery `pycdlib` and
  `diagnostic_witness_service.py` errors were not rerun in this milestone.
- Candidate lesson capture is connected to the existing SkillLibrary and is
  covered by tests; no live candidate was created because the live patch has
  not completed the approved-application/test path. No lesson was promoted.

Evidence report: `.local/tests/milestone4-final-20261123-dbde4b6f/report.md`.
The run record and role/patch/simulation evidence are retained under the
disposable repository's `.local/coding-workflows/dbde4b6fac7c447e89d050db561d26e1`.

#### Milestone 4 pinned test-profile compatibility (2026-11-23)

- Root cause: `pure_tests.py` recognized the clamp profile under `milestone3-*`
  fixtures only. The Milestone 4 destination is not a prefix match and uses a
  UTF-8 BOM in `.local/pinned-tests.json`, which the former byte-based JSON
  loader did not decode.
- Added exact canonical registration for
  `.local/tests/milestone4-live-8a12f7d4216a4ceea723c1dd0b60f89e/repo` and the
  unchanged `clamp_percent_v1` test hash/module configuration. Existing
  Milestone 3 fixture handling remains supported. Root, config, test, and
  source paths reject traversal and symlink/junction/reparse-point paths;
  pinned JSON now loads as UTF-8 with optional BOM. Broker command allowlists
  and approval test-command binding are unchanged.
- Added `tests/test_pure_tests.py` coverage for the exact fixture, Milestone 3
  compatibility, unregistered and lookalike roots, traversal, reparse/symlink
  aliases, fixed profile substitution, and BOM decoding. Added a workflow test
  proving an altered test command cannot be approved.
- Post-fix compatibility/workflow/Broker regression suites passed **53/53**;
  the fixture-registration suite after adding the symlink test passed **9/9**.
  The broader 22-module focused regression run passed **305/305** before the
  final BOM-decoding test was added. A first post-BOM combined run had one
  temporary Git-object permission error during test setup; that single test
  passed on retry and the full 53-test suite then passed.
- The pending M4 workflow remains `awaiting_approval`. No approval was
  recorded/consumed, no patch was applied, and no live post-application tests
  were run. The destination remains clean at baseline
  `054e52f05a0a3cf4b69f457090f6ba9a0a91e798`; patch and expected post-image
  hashes remain unchanged. Evidence:
  `.local/tests/milestone4-test-profile-fix-20261123/report.md`.

Do not begin Milestone 5.

#### Milestone 4 final approval and completion (2026-11-23)

**Status: complete.** The compatibility record above was followed by a separate
explicit approval for the exact pending patch, and the existing one-use Broker
workflow completed successfully.

- Approval `57dd66444a79498781cf6c4047c3a040` was bound to workflow
  `dbde4b6fac7c447e89d050db561d26e1`, the authorized disposable repository,
  baseline `054e52f05a0a3cf4b69f457090f6ba9a0a91e798`, patch
  `bc2ef607c219fcd4cb4ee4ca439dc8195fe8b48595df5b9eabdccef619a20960`,
  expected image `07d3ccebb1aed05af8a2c0faa9986897a8ca7aa80a9a78c1840b6c8b6f0e8825`,
  `src/progress.py` only, and the fixed `clamp_percent_v1` test profile. It
  was consumed exactly once before application.
- Broker applied only `src/progress.py`; its actual SHA-256 equals the expected
  post-image. The Broker fixed tests passed **3/3, 0 failures, 0 errors**.
  Recovery journal `659811b24c8b479b852109706e701a6e` is complete; workflow
  state is `completed`. The only disposable worktree change is `?? src/progress.py`.
- Final focused 22-module regression invocation passed **307 tests, 0
  failures, 0 errors**. The BOM-aware compatibility, approval-bound command,
  Broker authorization, workflow, inference, tunnel, recovery, diagnostics,
  validator, and dashboard tests were included. Known optional `pycdlib` and
  unrelated `diagnostic_witness_service.py` full-discovery errors were not
  rerun or modified.
- SkillLibrary candidate `skill-eaa4f64e4c90bd00a48bed1f` version 1 is
  `evidence_valid` and remains `candidate`; it was not promoted.

Completion evidence: `.local/tests/milestone4-completion-20261123-dbde4b6f/report.md`.
Do not begin Milestone 5.

#### Milestone 4 multi-file reliability improvement (2026-11-23)

- Historical live proposal 1 had underdeclared hunk counts in both attempts
  (declared +3, contained +6); proposal 2 combined an ambiguous task, wrong
  behavior, malformed diff context, and a CRLF reconstruction limitation.
  Complete responses and strict failures are preserved; no historic proposal
  was applied.
- Coder now returns bounded structured full-file intents bound to source hashes;
  trusted code constructs and strictly validates unified diffs while preserving
  encoding, BOM, newline convention, and EOF newline state. The workflow retains
  bounded correction attempts, independent advisory review, exact simulation,
  and operator approval.
- Secure live comparison through `ssh_tunnel_ollama` succeeded for both
  all-`qwen3-coder:30b` roles and `qwen3:8b` planner with coder/reviewer
  `qwen3-coder:30b`. Both two-file proposals passed strict validation and the
  pinned disposable tests (3/3), received no-blocking reviewer findings, and
  stopped at `awaiting_approval`. No approval was recorded and neither patch
  was applied. Full hashes/diffs are in
  `.local/tests/milestone4-reliability-20261123-5b3f8aa8925141aabb861b5b7d3e9c48/report.md`.
- Focused reliability suites passed **53/53**; the wider 16-module regression
  passed **196/196**. The one initial failure was a stale assertion that
  expected only the prior test-profile name; it now verifies both names map to
  the same fixed Broker command.
- Browser automation remained unavailable. Chrome showed the existing local
  dashboard authentication gate, so interactive dashboard behavior is not
  claimed as browser-verified; API/security tests passed and a manual checklist
  is preserved in the report. Candidate lesson
  `skill-90bfc6a2af0b8102e714ede2` remains an evidence-valid, unpromoted
  reference candidate.

Readiness remains **Ready for disposable projects only**. Do not begin
Milestone 5.

#### Configuration A multi-file application (2026-11-23)

- Separate approval `dbf35b623bcc4ec19d2aba8ca8efa185` bound to workflow
  `3abcc66ad4744709820e5a966d1ac018`, its disposable destination and clean
  baseline, patch SHA-256, both reconstructed post-image hashes, exact two-file
  allowlist, and pinned `stats_multi_module_v1` test capability. It was
  consumed once through the existing workflow.
- Broker changed only `calculator.py` and `stats.py`. Actual bytes matched the
  approved post-images. The fixed Broker tests passed **3/3**; focused LocalCodingAgent
  regressions passed **196/196**, with no failures or errors.
- Application journal is complete. SkillLibrary candidate
  `skill-90bfc6a2af0b8102e714ede2` version 5 includes this run’s patch,
  approval, final state, and test receipt evidence. It remains unpromoted.
- Final workflow state is `completed`. Configuration B remains unapplied.
  The `coding-status` display command has an unrelated `file_hash` NameError;
  persisted state, hashes, receipt, and journal were verified directly.

Completion evidence: `.local/tests/milestone4-reliability-20261123-5b3f8aa8925141aabb861b5b7d3e9c48/configuration-a-completion-report.md`.
Do not begin Milestone 5.

#### Milestone 4 operational usability improvement (2026-11-23)

- Fixed the `coding-status` `NameError`: `agent.py` used `file_hash` without
  importing it. Status loads now opt out of the normal ambiguous-apply recovery
  transition, so inspection does not rewrite `applying`/`testing` records.
  CLI and dashboard use a shared read-only status projection.
- Completed status is shown as verified only when actual authorized post-image
  hashes, the pinned Broker receipt and output hash, and the complete matching
  recovery journal are all present. Windows path comparison is canonicalized;
  journal lookup matches its recorded Broker output path rather than assuming
  the journal ID equals the workflow ID. Configuration A evidence remained
  byte-identical.
- Reworked the existing dashboard into Overview, New Task, Active Work,
  Workflow History, and Knowledge sections. It now shows real workflow stage,
  elapsed time, calls/retries, changed-file counts, expandable diff/review/test
  evidence, read-only SkillLibrary entries, and the exact approval identity.
  The approval/apply boundary remains CLI + Windows Broker only.
- Configuration A and B presets are selectable; both models are explicit in
  the allowlist. Startup through `ssh_tunnel_ollama` verified the provider and
  confirmed `qwen3-coder:30b` and `qwen3:8b` installed. The dashboard labels
  provider health as a cached startup check. It binds to loopback, rejects an
  occupied port without stopping the existing process, and does not log
  request bodies or tokens.
- Added `coding-dashboard.cmd` as a repeatable launcher. A PowerShell launcher
  was also added, but this host's execution policy blocked running `.ps1`
  scripts; no policy bypass was used. The CMD launcher forwarded `--help`
  successfully. Browser verification loaded the updated login page in Chrome;
  computer-use policy prohibited entering the bearer token, so authenticated
  browser interaction remains for operator verification. Authenticated and
  unauthorized API behavior is covered by tests.
- Focused suites passed **232/232**: dashboard 11, status 3, workflow 20, and
  198 tests across patch validation, pinned tests, inference, tunnel,
  authorization, recovery, and diagnostic suites. No full discovery was run;
  the known optional `pycdlib` and unrelated `diagnostic_witness_service.py`
  discovery errors remain tracked and were not modified.
- Readiness remains **Ready for disposable projects only**. KronosStage and
  photo-library-tools were not opened or registered during this review. The
  next smallest step is a separately scoped, read-only project registration
  pilot with canonical identity, bounded source discovery/context, instruction
  capture, dependency metadata, and project-scoped SkillLibrary retrieval.

Dashboard URL used for the read-only Configuration A history display:
`http://127.0.0.1:52718/`. Launch instructions are in the README. This is not
Milestone 5; do not begin Milestone 5.

## Phase A — chat-first local assistant (2026-11-23)

**Status: implemented and live API verified; authenticated browser interaction remains operator-verifiable.**

- Added the chat-first interface at `/` and retained the workflow console at
  `/developer` when a disposable coding workspace is configured. General chat
  also has a no-project `local-chat` launcher. It uses the existing
  `ssh_tunnel_ollama` provider and allowlisted local models; chat requests have
  no tool definitions, project access, filesystem writes, shell, Broker, or VM
  authority.
- Responses stream from Ollama NDJSON to the authenticated local browser. A
  stop action cancels the request. Conversation records are atomic local JSON
  under `.local/chat/conversations`, bounded to 80 saved messages and 12 recent
  context messages (24,000 characters maximum). No unbounded history is sent.
- Live evidence on 2026-11-24 UTC confirmed `qwen3-coder:30b` over the managed
  SSH tunnel. The three acceptance prompts received streaming answers in
  9.906, 19.041, and 21.834 seconds; the third answer used the prior function
  turn and discussed ignoring extensions. Stop was accepted. After dashboard
  restart, all 7 messages (including the intentionally cancelled turn) were
  restored. Evidence: `.local/tests/phasea-chat-live-613f2e76b46a/result.json`.
- Browser automation displayed the actual chat login page at
  `http://127.0.0.1:52719/`. Automated token entry was not performed under the
  computer-use authentication restriction; authenticated chat UI behavior is
  verified through the live API smoke, not claimed as visually verified.
- Focused chat, streaming, dashboard, status, coding workflow, inference,
  tunnel, authorization, recovery, patch validation, and conversation
  security suites passed **114 tests**, 0 failures, 0 errors. The launcher
  `local-chat.cmd --help` and `git diff --check` passed. Full discovery was not
  run; existing optional `pycdlib` and unrelated diagnostic import errors
  remain tracked.
- Phase B project discovery/read-only analysis, Phase C conversational coding
  workflow, and Phase D knowledge integration are not implemented. The token
  login remains because no reviewed browser-session bootstrap was available;
  authentication was not removed. Default launch is `cmd.exe /c local-chat.cmd`;
  it opens the browser and keeps the one-run token in the launching console;
  URL is loopback port 52718. This
  completes Phase A evaluation only and does not begin Milestone 5.
