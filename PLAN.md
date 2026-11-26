# Local coding agent implementation

## Priority Knowledge Library remediation — 2026-11-25

**Active objective:** temporarily suspend Increment 5 and remediate large-document ingestion, inventory/navigation capabilities, retrieval strategy, study progress, and citation rendering. Preserve all Increment 1–4 work and historical roadmap items below. Do not begin Increment 5 until large-document and document-awareness acceptance is adequate.

### Audit evidence before remediation

- The existing project library is `.local/knowledge/index.sqlite3`, schema v4, bound to this repository identity. Read-only inspection found three current global PDFs (24, 30, and 38 pages), 94 indexed chunks and 94 embeddings. Each stored original length and SHA-256 matched; fresh pypdf parsing confirmed page counts. Two documents each have one page yielding no text. The reported thousands-page documents are not in this library, so their upload outcome cannot be inferred from persisted records.
- Existing implementation limits: 8 MiB upload and HTTP body, 300 PDF pages, 4,000,000 extracted characters, 12,000 library chunks, 256 chunks per semantic batch, and at most eight retrieved passages (two per document). Upload/extraction/chunking/indexing are synchronous; failures leave no durable ingestion job record. Empty pages are skipped without coverage warnings.
- Conversational capabilities do not include document inventory/navigation. Ordinary chat runs one hybrid top-K passage search. Source citations are appended to assistant text and also emitted as a token alongside structured sources, creating duplicate presentation.

### Work sequence

1. Preserve the read-only audit and add isolated large-document fixtures; never rebuild or reprocess the existing library without verified backup and task-specific authorization.
2. Implement persistent, bounded ingestion jobs with streamed upload, page-level extraction, incremental index/embedding batches, truthful progress, cancellation, interruption recovery, duplicate and coverage verification, and explicit partial/failure states.
3. Add trusted read-only document inventory, metadata, page-range, section, adjacent-chunk and document-scoped search capabilities; route inventory/navigation/study requests separately from factual retrieval.
4. Add progressive study records and unpromoted candidate lessons with page provenance; preserve project/conversation isolation and prompt-injection boundaries.
5. Improve Knowledge Library UI, retrieval context budgeting, and single-source citation rendering; verify browser acceptance with authenticated Mac and Windows inference.
6. Exercise hundreds- and thousands-page disposable fixtures, including start/middle/end sentinels, interruption/resume, resource measurements, retrieval scope, citations, and isolation. Run focused checks, then one bounded regression after stabilization.

### Hold and authorization boundaries

- Increment 5 remains suspended through acceptance of this remediation.
- Existing documents, conversations, indexes, candidates, registrations, and evidence are preserved. Existing-library acceptance is read-only. Any reprocessing requires a verified backup and task-specific authorization.
- The separate Phase B.6C forensic hold remains in force. Do not touch VHDX/VM artifacts or ledgers.
- Mac Studio remains inference-only; Windows Broker remains source-write/test authority. Document contents are untrusted and cannot grant execution authority. Candidate skills remain unpromoted until validated and approved.

### Historical roadmap preservation

Retain all prior future phases below: authorized file/project creation and coding, compilation/build and controlled runs, debugging, Git/GitHub, intelligent multi-agent/model routing, additional local model runtimes and licensing, music/audio/vision recognition, validated knowledge/skill acquisition, telemetry and performance metrics, activity-driven UI, knowledge portability, and secure multi-computer access. These remain deferred until the priority Knowledge Library exit criteria are met.

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
- Core usability acceptance: submitting a turn immediately shows the user
  message, selected model, assistant placeholder, and truthful activity state;
  incoming text appears incrementally in the active conversation; completion,
  errors, cancellation, and conversation switching reconcile without a refresh
  or conversation switch. API streaming tests alone do not satisfy this check;
  the authenticated browser must be observed.
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
# Unified platform roadmap and implementation status — 2026-11-24

This section is the current roadmap for LocalCodingAgent. It consolidates the
prior plans while retaining their milestone identifiers and evidence below as
historical records. Newer session authorization governs the bounded model
catalog and chat-selection slice described here; unrelated project and
forensic holds remain in force.

## Architecture reconciliation

LocalCodingAgent is an existing Windows-first local assistant with a chat
surface, not a blank-slate coding chatbot. Reuse its established boundaries:

- `agent.py` owns configuration, provider authorization, CLI entry points,
  locks, and host-side orchestration.
- `inference_provider.py`, `local_agent.py`, and `ssh_tunnel.py` provide a
  bounded provider contract, Ollama HTTP client, and pinned-host loopback SSH
  forwarding. The configured Mac route is the existing trusted transport.
- `coding_workflow.py`, `patch_validation.py`, `execution_recovery.py`, and
  `coding_dashboard.py` retain strict patch validation, one-use approvals,
  Windows Broker execution, fixed tests, recovery journals, and evidence.
- `chat_dashboard_page.py` and `chat_conversations.py` provide authenticated
  local chat, streaming, cancellation, and bounded persistent conversations.
- `model_registry.py`, `model_router.py`, `model_evaluation.py`, and
  `knowledge_index.py` provide capability evidence, deterministic routing,
  offline performance summaries, and project-scoped hybrid retrieval.
- `skill_library.py` captures evidence-backed candidates; promotion remains
  reviewed. `web_retrieval.py` provides bounded public web retrieval.

The main gaps are integration and inventory, not a missing coding core. Chat
previously exposed two hardcoded model names and sent every response through one
startup-selected provider. Capability profiles and routing only cover the two
Ollama provider IDs; they do not prove model quality. Document ingestion,
conversation-to-project context, model-neutral chat routing, and a unified
inventory were missing. Orchestration and knowledge services exist, but the
chat surface does not yet select them automatically. Browser authentication
and startup remain token-based. Do not represent planned adapters as usable
capabilities.

### Implemented in this session: first model catalog slice

Verification update (2026-11-24): the clean baseline full suite ran 635 tests
with four errors: two dashboard connection resets under full-suite load, the
missing optional `pycdlib` dependency, and the existing syntax error in
`diagnostic_witness_service.py`. After this slice, full discovery ran 643 tests
with two errors; only the latter `pycdlib` and syntax errors remained. Focused
catalog, chat, inference-provider, dashboard, and atomic-write suites passed
5/5, 10/10, 21/21, 11/11, and 4/4.

The bounded live smoke discovered 13 Ollama host/model entries, refreshed the
catalog, and generated streamed responses from Mac `qwen3-coder:30b` and
Windows `qwen3:4b` in one persistent conversation. Redacted evidence is at
`.local/tests/model-catalog-live-468d9e437b5b/result.json`. Browser UI
verification was attempted but blocked because the available browser helper
could not determine the URL of the already-open tab; no tab was navigated and
no authentication data was entered. No Auto router, MLX/Hugging Face runtime,
or specialized KronosStage runtime was integrated.

- Added a bounded persistent inventory keyed by provider, runtime, host,
  model name, and digest. This preserves same-name models on different hosts.
- Reused only the already configured Windows loopback Ollama and the enrolled
  Mac SSH-tunnel Ollama route. Discovery uses health and model-list requests;
  it does not load, download, or execute model files.
- Captured provider-reported architecture, parameter size, quantization,
  context window, and embedding dimensions where present. Task capabilities
  remain `unknown`; tags do not establish quality, modality, memory fit, or
  tool-calling correctness.
- Chat now lists compatible, available, authorized discovered models with
  host identity, offers **Refresh models**, resolves the selected provider and
  model separately, and rechecks the model through that provider before chat.
  Streaming, cancellation, bounded context, and conversation storage stay on
  their existing paths. Coding role selectors and Broker permissions remain
  restricted to their existing explicit model allowlist.
- The inventory is adapter-ready in identity/schema terms, but the active
  runtime factory still supports only Ollama and its managed SSH-tunnel route.
  MLX, Hugging Face, PyTorch, ONNX, audio, and vision execution adapters are
  not implemented by this slice.

### Inventory and compatibility evidence

Read-only discovery on 2026-11-24 found Windows Ollama `0.40.18` with 11
installed non-cloud local tags, and Mac Ollama `0.40.17` with 2 tags through
the existing pinned SSH route. The Mac tags are `qwen3-coder:30b` and
`qwen3:8b`; Windows has 11 tags including `qwen3:8b`. The identical `qwen3:8b`
digest was observed on both hosts; the inventory keeps the host-specific
identities distinct. All discovered Ollama entries report GGUF format and are
compatible with the existing Ollama adapter. No model was downloaded or
loaded for inventory.

Windows hardware was read from Windows system metadata: MSI MS-7D25,
Intel Core i7-12700K (20 logical processors), 34,137,300,992 bytes RAM, NVIDIA
GeForce RTX 3070 Ti with 8,192 MiB reported GPU memory and driver 621.24, plus
Intel UHD Graphics 770. Mac hardware was not independently queried because
the Ollama tunnel does not expose a host inventory API; no SSH shell was used.

Bounded read-only inspection of the registered KronosStage `.local/models`
and named model environments found MOSS-Audio-4B-Instruct safetensors,
MOSS-Music-8B-Instruct safetensors, Qwen3-Omni-30B-A3B-Thinking GGUF plus
mmproj, Demucs environments, and TinyMu/SmolLM2 experiment weights. The
KronosStage local installation summary records prior inference validation for
MOSS-Audio, Qwen3-Omni, and Demucs; this session did not execute them or
independently revalidate those records. MOSS-Music and TinyMu remain discovered
artifacts with runtime compatibility unverified. A Hugging Face cache contains
`sentence-transformers/all-MiniLM-L6-v2` snapshot `1110a243…`; its static config
identifies BERT, hidden size 384, max positions 512. It is not registered or
invocable in this slice. No MLX cache was found in the standard Windows cache
path; Mac MLX/cache locations were not inspected. Runtime invocation requires
a separately scoped adapter review and explicit execution authorization.

### Implemented in this session: local document knowledge slice (2026-11-24)

- Preserved the host-aware Ollama catalog and existing model/provider states. The
  LocalCodingAgent chat now has an authenticated Knowledge Library panel with a
  file picker, drag-and-drop, global/project-library and conversation-only scope,
  document listing, status, and removal.
- TXT, Markdown, and PDF uploads are parsed locally with bounded size/page/text
  limits. PDF extraction uses installed `pypdf`; scanned or encrypted PDFs are
  rejected with an OCR/encryption limitation. Unsupported content fails explicitly.
- Original bytes, metadata, extracted chunks, and FTS rows are written together in
  the existing project-bound `.local/knowledge/index.sqlite3`. The shared schema
  is additive and preserves upload tables during explicit project-index rebuilds.
  Exact duplicate content is deduplicated within its scope.
- Chat retrieves bounded passages before inference, labels them untrusted, and
  appends source references from trusted stored metadata. Retrieval is provider-
  independent and the same evidence was passed to multiple authorized chat model
  identities in API integration tests. No retrieved document grants authority or
  becomes a SkillLibrary entry.
- Backup/restore uses SQLite's online backup API, hash and integrity verification,
  exact project identity, and refuses a nonempty restore target. This snapshot
  includes the shared knowledge DB and imported originals. It does not bundle the
  separate SkillLibrary registry or external evidence artifacts; full portable
  backup and evidence rebinding remain incomplete.
- Fresh pre-change baseline: 643 tests with the two known failures (optional
  `pycdlib` missing and `diagnostic_witness_service.py:710` syntax error). Final
  full regression: 655 tests, exactly those same two errors. No new failures.
  Focused knowledge index tests passed 18/18; document and chat integration passed
  12/12. AST parsing and `git diff --check` passed.
- Live authenticated API smoke, using a disposable project and document, passed:
  Mac Studio `qwen3-coder:30b` streamed 953 chars in 10.522s; Windows `qwen3:4b`
  streamed 1562 chars in 5.326s. Each stream named the actual provider/host/model
  and contained one source citation. After restart, the document and four-message
  conversation persisted; removing the document removed it from retrieval. Redacted
  evidence: `.local/tests/knowledge-live-b8bf46b048cc4ad0/result.json`. Cleanup of
  the disposable Git repository was denied on one Git object; its partial residue is
  preserved at `.local/tests/knowledge-disposable-m6wxz9ce` for reconciliation.
- The cached Hugging Face `all-MiniLM-L6-v2` snapshot is at
  `C:\Users\dsmus\.cache\huggingface\hub\models--sentence-transformers--all-MiniLM-L6-v2\snapshots\1110a243fdf4706b3f48f1d95db1a4f5529b4d41`, has a 384-dimensional BERT
  config and 90,868,376-byte safetensors weight. The LocalCodingAgent venv lacks
  `sentence-transformers`, `transformers`, `torch`, `onnxruntime`, and
  `safetensors`. It is discovered and cached, but not runtime-compatible,
  executable-authorized, loaded, or tested here. No weights or dependencies were
  changed. Retrieval is FTS5 keyword-only; semantic accuracy and comparison are
  blocked pending an authorized runtime, so no semantic score is claimed.
- Browser verification was attempted, but the computer-use inventory returned
  `Browsers: The browser access host is unavailable`. No tab opened and no token was
  entered. The disposable authenticated page server was stopped. API integration is
  not represented as browser verification.

## Chat streaming acceptance and next increment (2026-11-24)

- The user manually confirmed that the repaired live chat interface is working
  well. This closes the prior usability release gate.
- Root cause was the dashboard's chunked response writer using the standard
  library handler's HTTP/1.0 default. Browser Fetch exposed a chunk-length line
  to the NDJSON parser. The handler now uses HTTP/1.1. The response placeholder,
  activity-title reference, live progressive text rendering, completion handling,
  and server-plus-page build identifier are preserved.
- Authenticated browser verification used live Mac Studio `qwen3-coder:30b` and
  Windows `deepseek-r1:8b`. Both responses appeared in the active conversation;
  a longer Mac response was visibly partial while generation was still active,
  and the interface returned to Send on completion. Focused chat regressions
  passed 17/17; the page validator, compilation, and `git diff --check` passed.
- Full regression testing was not run after the streaming repair. A fresh full
  suite is required before starting project-intelligence implementation.
- Stop-button cancellation and conversation switching during an active browser
  generation still require explicit end-to-end verification. Future conversational
  features require authenticated browser acceptance plus live inference against
  authorized models; API tests alone are insufficient.
- Future chat UX redesign may explore an audio-waveform-inspired visual that
  reflects genuine model activity while thinking. This is a design direction,
  not authorization to add simulated activity or to expand the current increment.
- A later optional observability view should expose live Mac inference and AI
  usage metrics (for example, token counts and generation speed) alongside
  relevant machine/resource statistics. Show measured values with their source
  and freshness; mark unavailable metrics explicitly rather than estimating.
- The next bounded increment resumes read-only Git/GitHub project intelligence:
  authorized project registration, repository status and history, bounded
  secret-aware source indexing, project-filtered retrieval, validated citations,
  and project-aware chat. Disposable repositories come first; real projects
  remain read-only and require explicit registration/authorization.

### Fresh full regression baseline after streaming repair

- At starting commit `1c98c6b88e8d46cc6f7e32a04b084ed8b9a7d35e`, with the existing
  dirty working tree preserved, `python -m unittest discover -s tests -v` ran
  684 tests in 664.175 seconds: **5 errors, 0 assertion failures**.
- The errors were: missing optional `pycdlib`; the pre-existing syntax error at
  `diagnostic_witness_service.py:710`; and three disposable Git fixture writes
  failing with Windows permission denied while writing `.git/objects`.
  None involved the chat dashboard or streaming repair. The fixture errors are
  environment failures; they are recorded and not repaired in this increment.
- The focused chat suite passed 17/17 after the HTTP/1.1 handler regression
  assertion was added. This is the baseline for project-intelligence work.

## Project Intelligence increment 1 (2026-11-24)

**Implemented on disposable repositories; live model retrieval passed; browser
acceptance remains pending.**

- Reused `Project`, `KnowledgeIndex`, the authorized model catalog and inference
  providers, `ConversationStore`, the chat stream, and existing local research
  authorization. The shared KnowledgeIndex accepts a validated app-private
  storage owner so project source repositories remain read-only.
- Added explicit project registration in chat, local Git status/history/branch
  and remote metadata, bounded tracked/untracked file inventory, manual refresh,
  project selection for new chats, explicit binding to an existing conversation,
  removal of registration, and project-scoped retrieval through the same
  KnowledgeIndex adapter. Removing registration does not remove source files.
- GitHub remotes are shown as sanitized local identities. No network fetch,
  clone, checkout, project script, test, or GitHub API request is performed.
  Online GitHub retrieval remains subject to the existing explicit or approved
  research session and is not implemented in this slice.
- File indexing is allowlisted by supported text/source extensions, caps Git
  inventory at 16 MiB/20 seconds, indexes at most 2,000 files of 1 MiB each,
  excludes generated/cache/credential/model/media paths, rejects links and
  hardlinks, and keeps project-derived data in the LocalCodingAgent ignored
  `.local/project-intelligence` store. Exact source hashes are checked at
  retrieval and again before citations are emitted. Semantic retrieval is used
  when that project's index already has a compatible vector space; otherwise
  keyword retrieval remains available.
- Automated project tests: **7/7 passed**. Chat regressions: **17/17 passed**.
  Knowledge document regressions: **20/20 passed**. The combined 24-test
  project/chat run passed after adding authenticated project-to-chat coverage.
  Python compilation passed. The established full-suite baseline remains 684
  tests with five pre-existing errors; rerun final regression before release.
- Live disposable API inference passed with Mac Studio `qwen3-coder:30b` and
  Windows `qwen3:8b`, both answering with citations to the same indexed fixture.
  Evidence: `.local/tests/project-live-44343b6fe354465f8c6bf7637201eb52/`.
  No production project was registered or modified. The computer-use inventory
  reported `The browser access host is unavailable`; API/live inference is not
  browser verification. Manual browser acceptance is still required.
- Browser checklist: launch `local-chat.cmd`; authenticate with that process's
  one-run token; open Projects; register a disposable Git repo; refresh; choose
  Use in new chat; ask about a known symbol; confirm progressive output and a
  relative path/line/commit/hash citation; open a general conversation and
  verify it receives no project excerpts; switch to a second authorized model
  and confirm the same citation; stop generation and switch conversations while
  a response is active; remove the registration and confirm source files remain.
- Authenticated browser acceptance has now passed in isolated headless Edge
  against the current build. Stop cancellation preserved the partial assistant
  response and persisted `stream_status=cancelled`; switching to another chat
  and returning left the active request attached to its original conversation.
- The user requested an eventual waveform-inspired thinking visual and live
  Mac/Windows system and AI telemetry, including measured token/speed values
  and historical token usage. Keep these as future UX work. Any animated visual
  must reflect real activity; telemetry must carry source and freshness and
  must not estimate unavailable values. This project-intelligence increment
  does not implement the redesign or metrics.
- The earlier browser-access-host limitation was worked around using an
  isolated local Edge profile with Chrome DevTools Protocol and the real
  authenticated dashboard. No authentication controls were weakened.

### Project intelligence acceptance (2026-11-25)

- **PASS — browser and live inference:** `.local/tests/project-browser-b6b761ab461746e5a708d80a5deed82d/browser-result.json`
  records authenticated browser verification on build `cf04e2e9c524`. It
  registered and indexed two disposable Git repositories, queried the Mac
  Studio `qwen3-coder:30b` and Windows `qwen3:8b`, validated both cited path and
  line ranges against the actual source text, refreshed after a committed source
  edit and retrieved the new function/value, isolated two project conversations
  and a general conversation, uploaded and retrieved a disposable Knowledge
  Library document, stopped a live response with its partial reply retained as
  cancelled, switched conversations during generation, restarted the isolated
  dashboard and verified persistence, then removed a registration while
  confirming the source repository remained. No browser console errors.
- The Mac response's first visible text arrived 115 ms after submission and
  appeared progressively across 7 observed DOM snapshots; the initial
  processing indicator appeared in 8 ms. These are measurements from this
  local Edge run, not a performance guarantee. The beta source citation was
  checked against `classify_collection` and its exact `BLUEBIRD_PRIVATE_TAG`
  return expression. The general-chat model answered “I don't know” and did not
  reproduce that private marker.
- During that passing run, switching worked and the Stop control remained
  available, but the status label could remain `Writing your response…` in the
  other chat. A UI update now sets the background-generation label immediately
  when a conversation row is selected and keeps it current as stream events
  arrive. Follow-up attempts reached build `6eba93d36528` but timed out on the
  first Mac Studio SSH-tunnel inference request (5.7 seconds). Another run
  passed both model queries but did not complete the browser test because its
  upload wait watched the library runtime-status text instead of the persisted
  document; API evidence confirms the upload succeeded. The status-label fix
  still awaits a complete post-change browser run with a responsive Mac tunnel.
- **PASS — bounded Git timeout handling:** authenticated project registration
  once hung in its initial `git rev-parse --show-toplevel` call. Git inspection
  now closes inherited stdin, disables `core.fsmonitor`, and converts timeout
  and process-start failures into a bounded `AgentError` returned to the client.
  A regression covers the timeout response. Global Git configuration is
  preserved because disabling it changed the fixture's `core.autocrlf`
  interpretation and falsely marked files modified. No source projects were
  changed.
- **PASS — focused final regressions:** 29/29 combined chat and project tests
  passed after the status-label change. **Project tests:** 12/12
  `test_project_intelligence` tests and
  `py_compile` for changed Python files passed. Three dashboard tests and two
  tests whose fixture Git writes had failed in the full-suite run passed when
  rerun alone; the observed full-suite failures are intermittent Windows
  permission-denied writes into disposable `.git/objects`.
- **Fresh full regression:** 696 tests in 847.543 seconds (14m 7.5s), 3 failures
  and 4 errors, no tests skipped. Captured per-test timings:
  `.local/tests/regression-final-20261125.log`. The slowest were semantic
  ingestion/rebuild cases at 29–35 seconds; the next group was 17–28 seconds,
  mostly repeated SQLite/vector backup, restore, and indexing. This explains
  the long runtime; the suite completed without hanging. Three fixture write
  failures and two fixture errors were Windows `Permission denied` creating
  disposable Git objects; those five affected tests passed on isolated rerun.
  The remaining known errors are unavailable optional `pycdlib` and the
  pre-existing `diagnostic_witness_service.py:710` syntax error. No assertion
  failure or project-intelligence test failure occurred.
- **PASS — live research path, partial smoke:**
  `.local/tests/research-live-f29c465a1621/result.json` records one actual
  authorized public search, a retrieved official Python documentation page,
  citations, cross-model retrieval after retention, restart persistence, and
  deletion of the disposable retained document. The script's final post-delete
  question failed because the local model connection closed; therefore its
  aggregate `verified` flag is false, and that last negative-retrieval check
  remains unverified. Deterministic research authorization, denial, cancellation,
  ordinary-chat-local-only, and web security tests passed in the full suite.
- No real project (KronosStage or Photo Library Tools) was registered or
  modified. The disposable browser app used keyword fallback because the
  embedding worker was not available in that separate test app root; production
  semantic retrieval remains covered by passing knowledge tests and prior live
  evidence. GitHub requests, symbol graphs, classified project status, and
  cross-root project migration remain unimplemented.
- The user additionally requested a later sound-recording waveform-inspired
  thinking visual and an optional live Mac/AI metrics chart. Keep these as future
  UX work; waveform animation must reflect real activity, and telemetry must be
  sourced/timestamped without estimated values. No visual redesign or metrics
  implementation is part of this project-intelligence slice.

## Unified stages

These are architectural groupings, not gates that block useful vertical slices.
Preserve historical milestone IDs and evidence in the records below.

| Stage | Scope | Current assessment |
|---|---|---|
| 1. Existing foundation | Secure inference, Broker, coding workflows, approvals, recovery, tests, knowledge and skills | Fresh 696-test run completed in 847.543s: 3 intermittent Windows disposable Git object permission failures (all 5 affected fixture tests passed alone), missing optional `pycdlib`, and existing `diagnostic_witness_service.py:710` syntax error. No project-intelligence assertion failures. |
| 2. Conversational application | Chat, streaming, history, cancellation, usability | User accepted repaired streaming. Isolated authenticated Edge acceptance confirms progressive visible Mac output, stop persistence, conversation switching, restart persistence, and no console errors. |
| 3. Universal model platform | Discovery, adapters, registry, capabilities, selection, benchmarks | Ollama inventory and host-aware persistent catalog now implemented. Adapter plans include MLX, Hugging Face Transformers, PyTorch, ONNX, GGUF, and specialized audio/vision runtimes. Track checkpoint provenance, version, license, permitted/commercial use, compatibility, resource needs, and measured capabilities; verify exact Music Flamingo/Audio Flamingo 3 checkpoint terms before paid or commercial use. Capability evidence and intelligent routing remain partial. |
| 4. Universal knowledge platform | Documents, semantic retrieval, source adapters, durable knowledge, research | TXT/Markdown/text-PDF chat ingestion now shares the project KnowledgeIndex with persistent CPU MiniLM vectors and FTS5. Hybrid RRF, citations, scope filtering, deletion, recovery, and same-project KnowledgeIndex + SkillLibrary/evidence backup/restore were verified. Cross-root migration/rebinding, user-approved cross-project sharing, additional adapters, and autonomous research remain future work. |
| 5. Intelligent orchestration | Intent, model/agent/tool/knowledge selection, dependencies and resource policy | A fixed trusted capability registry and deterministic bounded dispatcher now expose read-only registered-project actions in chat. Broader model/agent/tool selection, policy engine, and resource-aware scheduling remain incomplete. |
| 6. Real-project intelligence | Git identity/status, source retrieval, dependencies and project context | First local read-only slice passed authenticated Edge acceptance with disposable repositories, live Mac `qwen3-coder:30b` and Windows `qwen3:8b`, exact source-line citation checks, refresh, general/two-project isolation, restart persistence, and safe unregister. Incremental symbol/dependency indexing, classified project status, real-project pilot, and task-authorized GitHub retrieval remain incomplete. |
| 7. Conversational software development | Chat routes into Broker-controlled coding workflow | Increment 4 now supports max-three-file validated change sets with exact one-use approval, recoverable per-file Broker writes, separate pinned-test approval, receipts, and candidate learning. Authenticated Edge/live Mac+Windows acceptance passed on disposable repositories; an actual dashboard kill after the first Broker write recovered as applied/unchanged files and blocked tests/replay. The single full regression ran 736 tests in 920.543s with four known/environmental errors. File creation, builds, runtime execution, iterative repair, and Git/GitHub remain future work. |
| 8. Continuous improvement | Verified lessons, skills, performance and routing | Candidate capture, reviewed promotion, performance evidence, and offline evaluation exist. Automatic capture-quality review and outcome-calibrated routing remain incomplete. |
| 9. Distributed AI platform | More computers, scheduling, installers, pairing, resource telemetry and AI usage metrics | Secure Mac inference transport exists. Future provider-independent telemetry should cover Mac/Windows CPU, GPU, memory/VRAM, pressure/swap, storage, network, thermals/power where available; prompt/output tokens, prompt and generation speed, time-to-first-token, model load time, context, concurrency/queue, and historical per-model/project usage. Ollama versus MLX comparison, metrics charts, installers, pairing, role-based access, and VPN management remain plans only. |
| 10. Optional training | Fine-tuning with curated verified examples | Not implemented; defer until evidence quality, licensing, retention, evaluation, and rollback criteria are designed. |

## Incremental migration strategy

1. Keep `CodingDashboard`, provider factory, remote tunnel, `ConversationStore`,
   and all Broker/approval pathways. Make new model inventory additive and
   host-aware; never use a model's advertised capability as authority.
2. Define adapter contracts around discovery, identity, compatibility,
   availability, verification, streaming, cancellation, and cleanup. Add one
   runtime at a time, with provider-specific authorization and contract tests.
3. Keep discovered/installed/compatible/authorized/available/loaded/tested/
   recommended as separate states. Discovery persists metadata, not executable
   permission. Authorization is supplied by host configuration, never model
   output. Revalidate availability immediately before inference.
4. Add Auto routing only after capability evidence, task requirements,
   resource observations, and bounded failure behavior can be explained and
   audited. Do not silently fall back across hosts or to cloud.
5. Integrate knowledge and project context as cited, bounded data. Keep
   internet results and model output untrusted; keep project writes and tests
   behind the existing Windows Broker and one-use approvals.
6. Deliver later capabilities as chat-first vertical slices; keep internal
   orchestration details in the existing advanced dashboard.

## Security and compatibility risks

- A model label and runtime metadata do not establish accuracy, memory fit,
  modality, or safe tool use. Capability claims need independent evidence.
- Same model names/digests across hosts do not imply identical runtime or
  performance; retain provider and host identity in every decision.
- The configured remote profile is authorized only through its pinned
  loopback SSH tunnel. No model endpoint or shell is exposed to the LAN.
- Refresh can mark a model unavailable; stale persisted inventory is evidence
  only and is never sufficient for a new inference request.
- Future MLX/Hugging Face/PyTorch/ONNX runtimes add loading, dependency,
  deserialization, resource, and licensing risks. Discovery must not invoke
  untrusted weights or scripts.
- Replacing chat choices must not relax the fixed developer workflow model
  allowlist, Broker authority, path policy, patch validation, receipts, or
  recovery state machine.
- Chat conversation history remains text-only. Uploaded document originals and
  passages use separately scoped local SQLite records with their own retention,
  citation, size, and permission boundaries.

## Conversational Agent Integration — Increment 1 (2026-11-24)

**Implemented and passed live model/API and authenticated browser acceptance for the bounded read-only project action slice. Full regression remains pending.**

- Preserved the worktree at baseline commit `1c98c6b88e8d46cc6f7e32a04b084ed8b9a7d35e`; all pre-existing dirty files remain in place. No production repository was registered or modified.
- Added a code-owned, provider-independent capability registry and deterministic intent router. The registry describes structured inputs, outputs, handler, host, availability, permissions, approval, risk, bounds, validation, recovery, and verification. Unknown model or document capability names cannot be dispatched.
- Natural-language chat actions now cover listing registered projects, opening an exactly matching registered project in the current conversation, bounded Git status, eligible source/document/test path listing, bounded UTF-8 source/document reads, and indexed project search. File reads enforce inventory membership, project-bound path checks, extension exclusions, size caps, secret scanning, and hash validation after generation. Project search citations are checked against indexed metadata and current source bytes before display.
- Results are marked as untrusted reference data in the model context. The model has no shell, filesystem, network, or execution authority. Runtime capability descriptions explicitly distinguish these operations from native model abilities and state that source writes, arbitrary commands, builds, and test execution are unavailable.
- Capability execution writes bounded JSONL audit events under the existing private chat store. Records contain capability/conversation/project identity, argument and result hashes, state, and timestamp; they do not contain source text, raw arguments, or credentials. Corrupt or full audit storage blocks the action.
- Project registration remains in the existing authenticated Projects control because chat does not yet expose a one-use, same-origin confirmation prompt bound to a resolved root and scope. Existing Broker-controlled coding workflows, source modification, test execution, builds, and terminal access remain unavailable through chat until an approval-preserving Broker integration is reviewed.
- Focused results: `test_conversational_capabilities.py` 6/6; `test_project_intelligence.py` 14/14; `test_chat_conversations.py` 17/17. Python compilation and Node syntax validation passed.
- Live API/model acceptance passed on Mac Studio `qwen3-coder:30b` and Windows `qwen3:8b`. Both models performed natural-language project selection and file read on a disposable registered repository, and both produced an application-validated citation matching the fixture source hash. Evidence: `.local/tests/conversational-capabilities-live-f618765173a84e5ea9e8ceec4644962f/result.json`.
- Authenticated browser acceptance passed using the existing local isolated Edge/DevTools harness on build `41bb942e698b`, despite the separate computer-use browser host being unavailable. Final evidence after the audit-lock change: `.local/tests/project-browser-7dbab4f8ba9f44ebb7edfa7876149ac0/browser-result.json`. It verified natural-language project listing and selection, Mac and Windows natural-language file reads with citations checked against source lines and completed capability audit records, live progressive rendering, source refresh, project/general isolation, Knowledge Library upload and retrieval, stop/cancel, conversation switching, restart persistence, safe unregister, and zero console errors. The harness initially failed because its assertion checked only the first citation range; it was corrected to select a range that supports the source claim and verify the actual `project.read` audit event. Product behavior passed on rerun.
- Existing recorded blockers remain unresolved: five intermittent Windows Git fixture permission failures, missing optional `pycdlib`, the pre-existing `diagnostic_witness_service.py:710` syntax error, a Mac SSH-tunnel timeout observed in an earlier browser attempt (the current bounded live requests passed), unverified background-generation status-label repair, semantic embedding worker unavailable in the isolated browser environment, and incomplete post-deletion research retrieval verification.
- Final full regression: **704 tests in 662.818 seconds; 0 assertion failures; 4 errors**. Complete per-test timing log: `.local/tests/conversational-agent-final-regression-20261125.log`. Errors were (1) missing optional `pycdlib`, (2) the pre-existing `diagnostic_witness_service.py:710` syntax error preventing one test module import, and (3–4) two Windows `Permission denied` writes to disposable fixture `.git/objects`. Rerunning those two exact knowledge backup tests alone passed 2/2 in 13.517 seconds, confirming the fixture errors are intermittent environment failures. No new failure from conversational capabilities occurred. The suite did not hang; the slowest tests were KnowledgeIndex rebuild (26.829s) and document version/delete (26.037s), with the next slowest at 9.473s. The long total is the cumulative runtime of many isolated filesystem, SQLite/vector, Broker-recovery, and chat fixtures, not one blocked test.
- Future model runtime work remains adapter-based and separately authorized: Ollama, MLX, Hugging Face Transformers, PyTorch, ONNX, GGUF, and interchangeable specialized audio/vision inference. No specialized Music Flamingo, Audio Flamingo 3, Qwen3-Omni, CLAP, Demucs, or Basic Pitch weights were installed or integrated. Before any paid-performance/commercial/distribution use, verify the exact checkpoint license and provenance, version, permitted use, and restrictions.
- Future learning remains candidate-first: capture provenance and evidence, deduplicate, validate, version, review, and support revocation; never automatically promote unverified content or modify neural weights. Optional autonomous research remains user-started, scoped, budgeted, cancellable, and reviewable; ordinary chat remains local-only unless explicitly directed or approved.
- Longer-term conversational execution remains on the unified roadmap: create projects and files, make authorized source changes, compile/build, install specifically approved dependencies, run applications, debug/repair iteratively, and perform authorized Git/GitHub operations. The initial pinned-test action is now exposed through the Windows Broker and one-use chat approval; arbitrary commands and the remaining capabilities stay unavailable. Future browser/desktop interaction also remains planned. Every write, install, process, and remote operation must use the existing Broker/approval boundary.
- Master orchestration remains planned to coordinate models, agents, tools, knowledge sources, and Windows/Mac or future compute hosts from measured capability, reliability, resource availability, and outcomes. Optional user-started learning sessions may study a declared subject within duration/source/resource budgets, provide cancellation, and require review; no unattended online-learning loop is active.
- Future visual redesign remains optional: command-center presentation, an audio-waveform motif driven only by observed lifecycle/token-throughput events (never private model thoughts), multi-agent activity where available, reduced-motion preference, and live/historical charts for the hardware and AI telemetry listed in Stage 9.

### Increment 1 acceptance and limitations

- **PASS:** seven isolated capability tests cover registry identity, clear intent routing, natural project list/select/status/file-list/source-search actions, bounded safe file reads, secret/path rejection, hash-validated citations, project read-only behavior, audit redaction, and cross-process audit-lock failure behavior. The updated 7-test capability suite and 14-test project suite were rerun after the final code change and passed.
- **PASS:** fourteen disposable project tests and seventeen chat tests pass, including authenticated HTTP tests for natural-language Git status and source-file read with trusted citation events.
- **PASS:** actual live operation execution on Mac Studio `qwen3-coder:30b` and Windows `qwen3:8b`; authenticated browser interaction with the new capabilities on build `41bb942e698b` after the final audit-lock change.
- **PASS WITH KNOWN ENVIRONMENT ERRORS:** the bounded 704-test full suite completed in 662.818 seconds, with 0 assertion failures and four errors; two intermittent fixture Git errors passed on isolated rerun. This full run preceded the final small audit-lock/list-metadata change and one additional audit-lock test; those final changes passed the updated 7-test capability and 14-test project suites. The optional dependency and syntax error remain unresolved.
- **DEFERRED:** conversational repository registration pending a one-use user confirmation component; Broker-authorized code modifications, test profiles, compilation, command execution, and repair remain deferred until the trusted Broker approval path is callable from chat.

### Next bounded implementation increment

Add a one-use conversational project-registration proposal and explicit same-origin confirmation bound to conversation, canonical root, read-only scope, expiry, and cancellation. Then add one Broker-backed test-profile action only if existing approval, pinned profile, execution receipt, cancellation, and recovery semantics can be preserved end to end. Do not add arbitrary terminal access.

### Conversational Execution Integration — Increment 2 (2026-11-25)

**Status: conversational registration and pinned-test execution implemented. Focused regression, final-build authenticated Edge acceptance, and live Windows and Mac inference passed, including verified learning-candidate capture. One intermittent Mac SSH-tunnel timeout and its successful bounded retry are recorded below.**

- Added deterministic chat intents for registering an existing repository and running only its matching code-owned pinned test profile. Registration resolves a Git root, rejects traversal, sensitive directories and reparse/junction paths, and binds a five-minute, single-use approval to the conversation and current target identity. Approval is durably consumed before indexing or execution. Registered project selection remains conversation scoped.
- Added an authenticated, same-origin approval and status/cancellation bridge in the existing chat dashboard. It routes only into `ProjectIntelligence`, existing `RecoveryStore`/`ExecutionJournal`, and the Windows `Broker`; it adds no shell or competing execution engine. A cross-process exclusive decision lease protects one-use approval consumption. The chat UI renders application-owned approval controls and operation state, not model-authored approval claims.
- Chat inference failures now preserve only allowlisted `ProviderFailure` categories in the visible message (such as request timeout, tunnel timeout, connection loss, or incomplete response); arbitrary provider text and endpoint details remain hidden. The focused dashboard regression verifies the timeout category is surfaced without leaking the private exception text.
- Pinned runs use the existing `stats_multi_module_v1` or `clamp_percent_v1` exact test-file digest and restricted `pure_tests.py` runner. The Windows Broker captures bounded output, exit code, duration, and receipt; the app verifies that receipt before reporting pass/fail. Results are retained as untrusted evidence and revalidated when the prior run is requested. The project tree is read-only during this capability.
- Added `conversational_execution.py` and focused fixtures in `tests/test_conversational_execution.py`; wired persistent operation metadata through `chat_conversations.py`, authenticated routes and deterministic routing through `coding_dashboard.py`, progress/approval UI through `chat_streaming.py`, and profile confinement through `pure_tests.py`. Capability copy now describes what is actually available.
- Focused verification after candidate capture, profile confinement, cross-process approval leasing, restart-status recovery, explicit cancellation/timeout status, and sentence-punctuation project selection passed. The previous run passed **28 tests in 49.054 seconds** across `test_pure_tests`, `test_conversational_execution`, and `test_conversational_capabilities`; the 11-test Broker recovery suite passed in **21.187 seconds**, including timeout and cancellation child termination, `unverified_command` evidence, and an operator reconciliation gate. A further fault-injection test checks restart inspection: a live owner remains in its active state, while a dead owner transitions to `interrupted` and exposes recovery evidence/options without retrying. After adding the router punctuation case, a targeted combined rerun passed **22 tests in 99.512 seconds**. Three intermittent Git-fixture permission failures passed in an isolated rerun (3/3 in 9.616 seconds). A dashboard workflow route reset also passed isolated (1/1 in 1.797 seconds); two existing chat page tests still intermittently hit `ConnectionResetError`, so they were not repeatedly rerun.
- Follow-up selection of the denial, cross-conversation/single-use replay, expiration, failed-test receipt, and dead-owner reconciliation cases passed **4/4 tests in 11.891 seconds**. An initial hard-kill attempt used the Windows virtualenv redirector, where `Popen.pid` differed from the Python worker PID; it was not treated as acceptance evidence. The later direct-base-Python process test and successful real termination are recorded in the Increment 2 follow-up below.
- The structured chat error-message test passed alone (1/1 in 0.580 seconds). `git diff --check` passed after the final edits.
- Authenticated browser acceptance on the final Windows-only build registered/indexed the disposable fixture after approval, selected it conversationally, and showed host/profile/working directory/side effects/limits before approval. Operation `1b3e2eddd0305c9b35abec59a18688ce` passed 3/3 tests, 0 failed, exit 0, in 1.149 seconds. The app verified the Broker receipt and created SkillLibrary candidate `skill-05497522c096410fc590bd13` v1 (`evidence_valid`, status `candidate`, not promoted). Receipt SHA-256: `223dad710ec18aa7350bb8d2f7ba2dfb8535ea8dc2c44287165c89845242b2a0`; output SHA-256: `2f8be338413b1ff57ac9e66c94863f0bb71bddfa74fe30b389e2a04c03361504`. Records are in `.local/chat/conversations/operations/1b3e2eddd0305c9b35abec59a18688ce.json` and its `-evidence` directory; candidate provenance is in the disposable fixture's ignored `.local` tree. The fixture was exercised through authenticated Edge on `127.0.0.1:52725`.
- Final-build Windows local inference returned exact text `WINDOWS_LOCK_RECOVERED_OK`. Earlier failures that said no response were traced to a stale `.local/inference.lock` owned by dead PID 9624, left after the manually stopped Mac inference request. Its metadata was preserved at `.local/tests/increment2-stale-inference-lock-20261125.json` (SHA-256 `69bde5939777214ef4038fc79b49c373807da5029b4d7faa860259e4d8f5cf7e`); only the confirmed dead-owner lock was removed. The Mac live-chat helper refreshes the catalog, selects the host-scoped authorized model key, and uses a bounded 45-second chat timeout. The first live attempt failed with `tunnel_timeout` despite ready discovery. A bounded retry on 2026-11-26 succeeded on `qwen3-coder:30b`: all three chat turns returned streamed responses (28.489 and 31.144 seconds for the first two), explicit cancellation was accepted without reader errors, and the 8-message conversation was restored after a dashboard restart. Evidence: `.local/tests/increment2-mac-retry-20261126/result.json`. This verifies live Mac inference while retaining the intermittent first-attempt SSH tunnel timeout as a transport reliability issue.
- Authenticated Microsoft Edge acceptance against final build `9297c7fac2dc` passed exact Windows inference (`FINAL_BUILD_CHAT_OK`), conversational registration and indexing of disposable `approved-fixture`, and a real approval-gated Windows Broker test run. The GUI project-binding control successfully bound the fixture to the chat. The run returned 3/3 passing tests (average, clamp, empty), exit code 0, 1.148-second operation duration, verified receipt, and unpromoted SkillLibrary candidate `skill-d82ee55bd3014fb82e26840c` version 1. Operation `93e1bc6cbfb9357078f1989c4399937f`; evidence `.local/chat/conversations/operations/93e1bc6cbfb9357078f1989c4399937f.json` and `.local/chat/conversations/operations/93e1bc6cbfb9357078f1989c4399937f-evidence/command-0.{json,txt}`. Receipt SHA-256 `3fd6c389ac8c95fafa9f2027ecd58441b5b391263687c6d5b1a7d6e212c6d3d9`; output SHA-256 `2f8be338413b1ff57ac9e66c94863f0bb71bddfa74fe30b389e2a04c03361504`. A punctuation-related natural-language project-selection grammar issue was fixed; its router regression passes. The isolated browser access host remains unavailable, so authenticated browser acceptance was performed in native Edge.
- The first bounded full discovery completed **713 tests in 802.713 seconds**: **2 failures, 6 errors**. Errors: the known absent optional `pycdlib`, known `diagnostic_witness_service.py:710` syntax error, three intermittent Windows Git fixture `.git/objects` permission failures, and a transient `ConnectionResetError` in the existing dashboard no-approval-route test. Both failures were existing `test_pure_tests` checks: the initial app-profile support accepted matching profile content for unrelated repository roots. Fixed by requiring the profile config at `.local/chat/test-profiles/<canonical-project-id>/<fixed-profile>.json` and enabling conversational profiles only after that path-to-project binding validates. Follow-up focused run passed **17 tests in 27.201 seconds** (`test_pure_tests` plus `test_conversational_execution`). The final stabilized full run is recorded in the follow-up verification below.
- Passing conversational test operations now submit a provenance-bound, evidence-verified candidate to the existing `SkillLibrary` only when the project `.local` directory is Git-ignored. Failed or unverified output is not promoted; candidates remain reviewable and revocable, and no output is automatically made an authoritative skill. If capture fails, the test result remains valid and explicitly reports that no candidate was recorded.
- The following planned capabilities remain audit-only: creating files/directories and projects; source writes and validated patch application; compilation/build and application runs; dependency installation; debugging, iterative repair/retest; Git/GitHub operations. All require separate conversational intents, exact targets, one-use approval, Broker-enforced profiles, bounded side effects, receipts and interrupted-operation recovery. The smallest next step is a single-file patch proposal routed through the existing `coding_workflow` validator on a disposable repository, with a separately approved Broker apply and this pinned test action as verification. Do not add arbitrary command execution.
- Maintain the prior blockers: intermittent Windows Git fixture permission errors; missing optional `pycdlib`; the unrelated `diagnostic_witness_service.py:710` syntax error; one reproduced Mac SSH-tunnel timeout (`tunnel_timeout`) followed by a successful bounded live retry; background-generation status-label verification; isolated-browser semantic embedding limitation; and incomplete post-deletion research retrieval verification. These are not represented as Increment 2 regressions.

### Increment 2 follow-up verification record

- Final authenticated browser operation record: `.local/chat/conversations/operations/1b3e2eddd0305c9b35abec59a18688ce.json`. Verified Broker receipt: `.local/chat/conversations/operations/1b3e2eddd0305c9b35abec59a18688ce-evidence/command-0.json`, SHA-256 `223dad710ec18aa7350bb8d2f7ba2dfb8535ea8dc2c44287165c89845242b2a0`; bounded output `command-0.txt`, SHA-256 `2f8be338413b1ff57ac9e66c94863f0bb71bddfa74fe30b389e2a04c03361504`. Result: exit 0, 3/3 passed, 0 failed, 1.149 sec. Candidate ID `skill-05497522c096410fc590bd13`, version 1, status `candidate`, `evidence_valid`, not promoted.
- The earlier Mac attempt that stopped at model-name validation is recorded at `.local/tests/phasea-chat-live-6bdb0df4914a/result.json`; its corrected, host-key-scoped retry is at `.local/tests/phasea-chat-live-b9ea707741cd/result.json` (bounded helper). After structured provider-failure feedback was added, a further corrected run classified the failure as `tunnel_timeout` at `.local/tests/phasea-chat-live-505704442b18/result.json`. Each isolated dashboard exited and released its inference lock; no token or credentials were written into these results.
- Final bounded full regression on the stabilized worktree: **718 tests in 804.527 seconds, 0 assertion failures, 3 errors**. Errors were the known missing optional `pycdlib`, the existing `diagnostic_witness_service.py:710` syntax error, and an intermittent chat-page `ConnectionResetError`; the exact chat-page test passed in isolation immediately afterward (1/1 in 0.547 seconds). No second full run was started.
- **PASS — real Windows dashboard termination with a live Broker child:** the final isolated harness launched the dashboard through the base Python binary and verified its PID matched the worker PID (`13660`). It performed conversational repository registration and project selection, proposed the pinned test operation, then observed its Broker-created child (`23292`) alive while terminating the dashboard. The child had been suspended by the harness immediately after process creation and before the Broker could persist its PID; it was resumed only after owner termination, then the harness terminated that exact child. Recovery classified the journal as `command_liveness_unknown`, refused automatic reconciliation, required operator stop confirmation, offered accept/abort, and the harness chose abort. A fresh execution service preserved `interrupted` with `receipt_verified=false`, and rejected approval replay. This verifies process ownership and conservative crash recovery at the spawn-to-journal boundary; it does not claim the pinned test body was running when the dashboard was killed. Evidence: `.local/tests/increment2-hard-kill-6942fb577f29/result.json`; journal: `.local/execution-recovery/825dada9baa3ea76667e0e15637a50d10475f1e5a0a13d23a441b164a055582c/d3031a7dd518f4bf4800377705c84d9f.json`.
- **Receipt-finalization recovery closed in focused fixtures:** when fault injection interrupts finalization after the Broker journal is closed, conversation inspection reconstructs pass/fail from the single intact, project-bound Broker test receipt and its bounded output file. It never reruns the command, and records learning as not replayed after restart. Both a recovered 3/3 pass and recovered failing test are covered. The receipt-finalization boundary remains fault-injected; it was not exercised by killing the actual dashboard at that later point.
- **Stale approvals and junction containment:** after the completion audit found that a changed registration target could remain in `consumed_approve`, both registration and test decisions now finalize changed targets as failed, with no registration, Broker dispatch, receipt claim, or reconciliation flag. A Windows directory-junction escape is also rejected. Focused stale-target/junction checks passed **3/3 in 5.192 seconds**; the complete conversational execution suite then passed **17/17 in 52.371 seconds**.
- Acceptance coverage now includes expiry, denial, conversation binding/replay, decision-lease contention, target mutation and junction escape, cancellation/timeout, real Broker pass/fail receipts, restarted receipt recovery, Mac/Windows model inference, authenticated native Edge, and real Windows dashboard process death. The process-death case covers the spawn-to-journal race: the real child was alive but held before the test body ran, so no test outcome or receipt was claimed. Its unrecorded PID correctly required operator confirmation and reconciliation; a separate fault-injection case covers loss after a complete receipt. The first hard-kill attempt used the Windows virtualenv redirector and exposed a PID mismatch; the successful run used `sys._base_executable`. Remaining environment blockers are the optional `pycdlib`, existing `diagnostic_witness_service.py:710` syntax error, intermittent Windows Git-fixture permissions, intermittent Mac SSH-tunnel timeout, background-generation status-label verification, isolated-browser semantic embedding limitation, and incomplete post-deletion research retrieval verification. Final-build authenticated Edge evidence and operation receipts are retained; the isolated Edge browser host remains unavailable.
- **Next bounded increment:** implement the first validated single-file patch proposal using the existing coding workflow and Broker approval path, then run that disposable project's pinned tests. Keep multi-file changes, compilation and runtime execution out of scope until patch validation, crash recovery and receipt finalization pass the same isolated acceptance gate.

## Conversational Code Modification — Increment 3 (2026-11-25)

**Status: the Increment 3 primary acceptance passed in the final authenticated browser run. One-file patch proposal, explicit approval, Broker application, separately approved pinned tests, verified receipts, candidate learning, and restart persistence all passed on disposable repositories. The single bounded full regression completed with established environment defects plus one timeout-test fixture expectation that was corrected and passed focused regression; the full suite was not repeated.**

- Reused `ConversationalExecution`, the existing structured `CodingWorkflow` patch parser and patch validation, trusted capability dispatch, Windows `Broker`, one-use chat approval controls, `RecoveryStore`/execution journals, and candidate `SkillLibrary`. The model cannot write files directly. A request names one existing source file; proposal validation binds its project identity, path, source hash, strict patch, and expected post-image. The chat approval is single-use, expiring, conversation-bound, and target-state-bound. Changed source or project state invalidates it.
- The application presents the actual diff, intent, risk/side-effect limits, validation, target and proposed fixed profile. Denial was verified to leave the source unchanged. Approval applied only the authorized file through the Windows Broker; independently inspected bytes match post-image SHA-256 `d61f6da52b0d0297db2f6fe80d0993e78db1ba3c3a7ea6ba2bcd25bf92c4612f`. The Broker journal records exactly one `project_write` and is complete. Unrelated pre-existing dirty changes are bound to the approved baseline.
- Natural-language path example exercised in the isolated browser: “Correct the average calculation in `calculator.py` …”. Live Mac Studio `qwen3-coder:30b` generated the bounded proposal. One proposal was rejected with no write; the next exact proposal was approved and applied. A separate test request and approval was required. No model-authored approval statement was accepted.
- Focused disposable end-to-end test passed after the Broker timer adjustment: it verified denial, approved single-file Broker write, exact content and receipt, replay rejection, separately approved pinned tests (**3/3 passed**), and an unpromoted `candidate` lesson with staged patch provenance. Authenticated dashboard HTTP approval with concurrent status polling passed **1 test in 4.414 seconds**; the combined targeted rerun passed **2 tests in 16.097 seconds**. After the final timeout and base-Python changes, the complete conversational-execution module passed **19 tests in 67.745 seconds**. The earlier combined coding-workflow and conversational-execution suite passed **38 tests in 156.412 seconds**, before the final owner/budget hardening changes.
- Final authenticated Edge acceptance passed using isolated app/project state and build `e183de4a7d81`. Evidence: `.local/tests/project-browser-5e33edee00e7425d83868091beabce85/browser-result.json`. It verified 27 checks with zero console errors: live Mac `qwen3-coder:30b` patch generation, Windows `qwen3:8b` inference and streaming (27 snapshots), registration/selection, denial without writes, explicit approval, exact single-file Broker application, separate pinned-test authorization, project/general/two-project isolation, cancellation, restart persistence, and candidate persistence. Write operation `ade286adfa1429468594c9bca005b05f`; journal `5b64ff867f114b1e99c34767866e4ed7.json`. Test operation `4915f2e0e62374e86db003290a7e3bc7` passed 3/3 (exit 0, 0.283 sec); receipt `.local/tests/project-browser-5e33edee00e7425d83868091beabce85/app/.local/chat/conversations/operations/4915f2e0e62374e86db003290a7e3bc7-evidence/command-0.json`, SHA-256 `b9a35fba2caced649be72e145625bed340b48cb8cc4f597d3b38b6853befc947`. Candidate `skill-be1ed13ee1f068019a6157bf` v1 remains unpromoted. The modified disposable file hash is `d61f6da52b0d0297db2f6fe80d0993e78db1ba3c3a7ea6ba2bcd25bf92c4612f`.
- The earlier browser timeout was diagnosed with process/import-time snapshots: the Broker child was alive with negligible CPU and stalled during isolated Python startup. Test children now use the base interpreter with `-B -I -S` and `stdin=DEVNULL`; the bounded pinned-test timeout is 60 seconds, other fixed commands remain 15 seconds. Timeout evidence is capped at 32 KiB, private to the operation, and treated as untrusted. The final fresh browser run succeeded; timed-out operations were not replayed.
- Exactly one bounded full regression was run: `python -m unittest discover -s tests -v`, **725 tests in 823.988 seconds**, with 5 errors and no assertion failures. Four are known/environmental: intermittent chat-page connection reset, missing optional `pycdlib`, existing `diagnostic_witness_service.py:710` syntax error, and intermittent Windows Git fixture object-write permission. The fifth was a timeout-test mock with too few monotonic timestamps; its fixture was corrected afterward, and `tests/test_execution_recovery.py` passed **11/11 in 20.371 seconds**. No second full suite was run. Log: `.local/tests/increment3-final-regression-20261125.log`.
- Existing open issues remain: intermittent Mac SSH-tunnel timeout, background-generation status-label verification, isolated-browser semantic embedding limitation, and incomplete post-deletion research retrieval verification, along with the four regression environment defects above. No production repository was modified.
- **Next bounded step:** multi-file coding only after implementing whole-set source/hash validation, exact approval binding, atomic or recoverable Broker application, and reconciliation evidence. Keep project creation, compilation, runtime launch, iterative repair, and Git/GitHub as later separately bounded capabilities.

## Current verified increment: Increment 4 multi-file conversational coding (2026-11-25)

**Implemented with live authenticated acceptance.** The existing Increment 2/3 work and all user data were preserved. No production repository was modified. The source workflow remains limited to explicitly named, existing files and a maximum of three files per proposal; creation, deletion, rename, package installation, arbitrary commands, and autonomous repair remain out of scope.

- Chat now routes a coordinated multi-file intent through the existing `CodingWorkflow`, one-use conversational operation record, Windows Broker, `RecoveryStore`, pinned test profile, receipt verification, and candidate-first `SkillLibrary`. The Mac Studio is still inference-only; Windows remains the only write/test authority. The application persists the `multi_patch` operation kind and renders the trusted approval control.
- Whole-set validation binds project/repository fingerprint, exact target inventory, source/dependency hashes, strict complete-file proposals, complete diff and patch hash, expected post-images, pinned test configuration, and a change-set hash. It rejects stale/unauthorized/duplicate/case-colliding paths, incomplete or malformed sets, traversal, linked paths, oversized files, and test-profile changes. Python syntax is reported separately from semantic correctness. The review control displays every file diff and requires a distinct user approval; tests require another approval.
- Broker application records a durable per-file journal and backup before each write; it rechecks all approved targets and source hashes, applies only approved paths, and verifies each resulting byte image. This is recoverable multi-file application, not a claim of filesystem atomicity. Interrupted sets block tests and replay; restart classifies unchanged/applied/external/uncertain files and exposes only evidence-supported reconciliation. Recovery does not retry writes or replay learning.
- The trusted pure-test profile now allows exact allowlisted intra-project imports and a narrowly validated, non-nested loop directly over a function argument for the two-module validation example. It continues to reject OS/network imports, arbitrary commands, and unrestricted test selection. Verified browser output was treated as untrusted evidence.
- Focused tests passed: the stabilized conversation/coding/recovery/pinned-test/chat/capability set passed except for the three diagnosed issues described below; discovery-mode `test_execution_recovery.py` passed **11/11 in 20.384 seconds**, and rerunning the two affected chat-page cases passed **2/2 in 1.065 seconds**. Python compilation, Node syntax validation, and `git diff --check` passed.
- Live acceptance passed on authenticated isolated Edge build `1a085c3ff33e`, evidence `.local/tests/project-browser-48b8ea6e85214a35990b59ecdcb4b65b/browser-result.json`. Live Mac Studio `qwen3-coder:30b` produced the coordinated proposal; Windows `qwen3:8b` inference and the Windows Broker test run were verified. A denied proposal changed neither file. The approved example updated `calculator.py` and `stats.py`; Broker post-image SHA-256 values were `30b0562ba754f1a9012694258b2a687069c7218627d4b98d9974ccf02e7a09fd` and `03d3f919be4b6f6e611e0bb2915b977f7122b440d90979824960cee53779aadf`. Both per-file write receipts are in operation `b8a5e2dd052971d986b9444fca69978f`, journal/run `89c1c4305e294871b144d361f8711169`. Separately approved test operation `e0f38686aae1b49729ed22aa4621042d` passed **3/3**, exit 0; receipt SHA-256 `c22db6191a132b7ce7ff1cabe496442c23d40681bae9bf3feccba39c3b3bbc13`. Candidate skill `skill-cda843f12e42cfd15eb8bf43` v1 remains unpromoted. Restart persistence and zero browser console errors were verified.
- Actual Windows process-interruption acceptance killed the isolated dashboard immediately after the first real Broker write. On restart, `calculator.py` was classified applied and `stats.py` unchanged; tests were blocked and approval replay rejected. Evidence `.local/tests/increment4-multi-patch-hard-kill-fb739ed3c038/result.json`; copied per-file recovery journal `.local/tests/increment4-multi-patch-hard-kill-fb739ed3c038/broker-recovery-journal.json` (SHA-256 `4afc4147875be162872be52c3bc2f4301b2673e161487525bd928184f6ac804a`). No subprocess output was expected at this source-write boundary; the journal contains the retained write intent/receipt state. Deterministic tests also cover failure before the first write, a partial set, external concurrent change, all images written before receipt finalization, and restart without learning replay. Actual process termination was exercised at the first-write boundary; the later receipt-finalization and other write boundaries remain fault-injection coverage, not separate hard-kill demonstrations.
- Exactly one bounded full regression was run after stabilization: **736 tests in 920.543 seconds; zero assertion failures; four errors**. Known/environmental errors: missing optional `pycdlib`; the pre-existing `diagnostic_witness_service.py:710` syntax error; and two intermittent Windows Git fixture object-write permission errors. The two chat-page resets seen in an earlier focused combined run did not recur; both targeted cases passed immediately when rerun. No Increment 4 assertion or application regression appeared. Log: `.local/tests/increment4-final-regression-20261125.log`.
- Other preserved open issues: intermittent Mac SSH-tunnel timeout (one earlier browser attempt failed before succeeding on a fresh run); chat-page connection resets remain intermittent; background-generation status-label verification; isolated-browser semantic embedding limitation; incomplete post-deletion research retrieval verification; missing optional `pycdlib`; the existing diagnostic witness syntax error; and intermittent Windows Git fixture permissions. The existing Increment 2 hard-kill evidence remains historical and separate from the Increment 4 multi-file hard-kill result.
- **Next bounded increment:** add one-file creation of an explicitly approved source file through the existing Broker, with no directory creation or shell access. Bind absence, destination path, post-image, approval, receipt, and recovery state; require separate test approval. Continue to defer project scaffolding, builds, application launch, dependency installation, iterative repair, Git/GitHub, and broader multi-agent work.
- **Unified future roadmap retained:** conversational software development and project creation; authorized file/directory creation; multi-file coding, compilation/builds, controlled application runs, debugging and iterative repair; Git/GitHub; intelligent multi-agent orchestration and automatic model selection; Ollama/MLX/Hugging Face and specialized model support; model licensing/commercial compatibility; music/audio/vision recognition; validated continuous knowledge/skill acquisition and user-authorized learning; live Mac/Windows telemetry and historical token/AI performance metrics; activity-driven waveform UI; knowledge portability/backup; and secure multi-computer/remote access. None of those unrelated capabilities was implemented in this increment.

## Current verified increment: local semantic knowledge

- Reused the existing OpenWebUI Python 3.12 environment (sentence-transformers
  5.5.1, torch 2.15.0, transformers 5.5.4, numpy 2.5.5, safetensors 0.8.0).
  No package or weight changes were made. The application venv remains free of
  embedding dependencies; a bounded offline CPU worker loads the cached local
  MiniLM snapshot.
- Model revision `1110a243fdf4706b3f48f1d95db1a4f5529b4d41`, safetensors SHA-256
  `53aa51172d142c89d9012cce15ae4d6cc0ca6895895114379cacb4fab128d9db`, 384
  dimensions, finite normalized output; reported worker peak working set 4.51 MB.
- Disposable 9-document / 10-question evaluation: keyword Recall@3 0.889 / MRR
  0.889 / absent-query false positives 0/1 / median 29 ms; semantic 1.000 /
  0.944 / 0/1 / 44 ms; hybrid RRF 1.000 / 1.000 / 0/1 / 45 ms. Fixture and
  index integrity are retained under `.local/tests/semantic-retrieval-261e5721f9fd/`.
- Authenticated live API verification passed using Mac Studio `qwen3-coder:30b`
  and Windows `qwen3:4b`: semantic paraphrase retrieval, generated citations,
  cross-model access, restart persistence, disposable backup/restore, and
  deletion from both keyword and vector retrieval. Evidence:
  `.local/tests/knowledge-live-a68c00be1b4d44c3/result.json`.
- Browser automation was attempted. The browser access host is unavailable; no
  browser test is claimed. Manual verification requires launching the dashboard,
  signing in with its one-run token, uploading a local TXT/Markdown/PDF, then
  asking a paraphrased question and confirming the source line.
- Scope remains project-bound. `Project.identity` derives from the absolute
  repository root, and approved SkillLibrary references carry project-local
  evidence URIs; portable cross-root migration must explicitly rebind and review
  those skills. No silent project-global migration is allowed.

## Current next implementation stage

The first user-controlled public research slice is implemented over the existing
`KnowledgeSourceAdapter` and `KnowledgeIndex`. Ordinary chat is local-only.
Users can choose local-only, ask-before-research, or research-now in the chat
composer. Approval is one-use and bound to the conversation, objective, and
selected authorized model. The configured provider is Firecrawl's official
keyless JSON Search API; DuckDuckGo HTML was blocked by an anti-bot challenge and
is not used as a fallback. One session is capped at one search, three HTTPS pages, 55 seconds,
and bounded page/context sizes. Retrieved excerpts are untrusted context and
citations are generated from validated retrieved-source metadata. Retention is
an explicit per-source action and stores only a bounded excerpt in the current
project-bound Knowledge Library. No provider key, account, dependency, service,
or background crawler was added. Firecrawl receives only the sanitized search
topic, never documents or chat history. Keyless usage has provider-controlled
daily per-IP limits and is not a zero-data-retention service.

Next, improve the provider adapter contract and resilience with reviewed
official APIs, source-quality evaluation, controlled URL reliability/version
metadata and separate cache freshness settings. Preserve explicit/approval-based access and
local-only defaults. Then proceed with read-only Git/GitHub project awareness
and intent coordination as separately bounded vertical slices. Autonomous
learning remains disabled and, if later authorized, must be a user-started
session with a subject, duration, budgets, source restrictions, cancellation,
and review. Auto model routing and direct unrestricted network access remain
disabled; production projects are out of scope.

---

# Historical milestone and handoff records

The plans and dated records below are retained for provenance. Their historical
“do not start next milestone” statements describe the prior task state and are
not current roadmap instructions where this dated unified section supersedes
them.
