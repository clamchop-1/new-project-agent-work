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

### Next smallest milestone

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
