# Persistent project skills

## Architecture and lifecycle

`SkillLibrary` stores project-scoped skill candidates in `.local/skills/registry.json`.
The registry is local state, outside the source tree and Git. Each record contains a
stable skill ID, project identity, category, problem, procedure, source task/agent,
source-event identity, timestamps, evidence references, validation state, version
history, operator decisions, and bounded reuse outcomes. Registry transactions are
serialized and journaled. A pending transaction blocks normal reads and writes until
an operator runs recovery.

Orchestration proposes lessons after completed tasks when verified source or receipt
artifacts are available. A `memory_analyst` message may also propose a candidate,
but the host checks that its task completed and checks every evidence reference.
Candidate extraction is bounded; proposals are always untrusted and do not change
project memory, permissions, or model weights. Deduplication creates a new version
of the matching skill rather than silently replacing history.

Evidence is accepted only from the originating project run. Paths must remain inside
the run, be regular non-linked files, satisfy size limits, and match their SHA-256.
Test receipts must be host-format successful `tests` receipts with a matching output
hash and an identity belonging to the project or a worktree recorded under that run.
Test receipts establish only a test-scoped result. A broader procedural claim needs
an operator validation note describing independent review. Agent summaries and
model self-assessments are not validation evidence.

Lifecycle states are `candidate`, `validated`, `approved`, `rejected`, `superseded`,
and `revoked`. Approval requires a different operator identity from the proposing
agent. Authoritative and executable trust classes require an explicit sensitive
approval flag. Corrections, new evidence, revalidation, supersession, and revocation
append versions; prior records remain inspectable. Stale evidence causes retrieval
to revoke that version. Approved skills may be exported explicitly; import creates
a candidate without source evidence, so it must be locally evidenced, validated,
and approved again.

## Operator review

Use the existing project selector from `.local/agent-config.json` (default:
`LocalCodingAgent`). Commands run from the project root:

```powershell
python -B skillctl.py list --status candidate
python -B skillctl.py show --skill-id SKILL_ID
python -B skillctl.py history --skill-id SKILL_ID
python -B skillctl.py validate --skill-id SKILL_ID --operator OPERATOR `
  --scope procedure --note "I independently reviewed the procedure and its artifacts."
python -B skillctl.py approve --skill-id SKILL_ID --operator OPERATOR --trust-class procedure
python -B skillctl.py reject --skill-id SKILL_ID --operator OPERATOR --reason "Evidence does not support this lesson."
```

For a claim limited to a passing test receipt, use `--scope tests`; that does not
validate broader procedure claims. Corrections create a new candidate version that
must be validated and approved again. Use `revoke` when a trusted lesson should no
longer be returned, or `supersede` after reviewing a replacement. `attach-evidence`
accepts a bounded JSON list of repository-run evidence references; it verifies the
referenced artifacts itself.

Interrupted registry transactions fail closed. Inspect the reported classification,
preserve registry/journal files, then choose the matching operator action:

```powershell
python -B skillctl.py recover --recovery-action finish
python -B skillctl.py recover --recovery-action discard
```

`finish` applies the journaled after-image only when the current registry matches its
recorded before-image. `discard` is available only when the registry is still at the
before-image. Conflicts require manual preservation and reconciliation; neither
action resets project files.

## Retrieval and security

The orchestration service retrieves at most three current, approved, evidence-fresh
skills for a role and task. Context is bounded to 8 KiB and explicitly labeled as
reference data with no permission or tool authority. Architect planning uses the
same bounded approved-skill lookup. Retrieval records skill version and task; later
host-verified outcomes can update reuse records. Skills are never executed by the
library, and retrieval cannot grant broker tools, alter operator write grants, or
unlock a Phase 5A integration recovery lock.

Cross-project reuse requires an operator-created export and explicit import into the
target project's library. The importing project gets a new candidate and must attach
local evidence before approval. The package digest detects accidental corruption; it
does not authenticate its sender. Project registry isolation relies on the existing
project identity boundary and local filesystem access controls. A user with direct
write access to `.local` can alter local state; this implementation is not protected
by a hardware-backed signature or separate OS identity.

Retrieved/model/inter-agent text remains data. It is not promoted solely from its
wording, and secret screening, size bounds, path containment, source-task validation,
and host receipt checks apply before it can be considered. Still, operator approval
requires reading the candidate and its referenced evidence; the system cannot
automatically determine whether a procedure is universally correct.

## Examples

An orchestrator task proposes a candidate with source references and a
`skill.candidate.proposed` event; it remains untrusted until review. After the
commands above approve it, a matching developer task can retrieve it as reference
data:

```powershell
python -B skillctl.py retrieve --category coding `
  --problem "parser boundary regression" --limit 3
```

This retrieves approved skills for the selected local project only. It does not
retrieve another project's library or execute the procedure.

## Limitations

The registry uses bounded local JSON with an atomic transaction journal, not a
database or signed append-only log. Automatic extraction depends on host task/run
artifacts being present and verified; it intentionally skips unsupported outcomes.
Semantic retrieval is deterministic token overlap rather than embedding search.
There is no RAG document index, weight training, dashboard approval UI, or automatic
cross-project synchronization in this phase. The OS account remains the trust root
for project-local registry files.
