# Shared memory compatibility plan — audited before implementation

The authority remains each project's existing `.local/PROJECT_CONTEXT.md` and
`.local/DURABLE_MEMORY.md`, governed by its own AGENTS.md. Git records code state;
notes are not proof of completed work. No replacement store or shared cross-project
vector memory will be introduced. Private adapter receipts/logs are non-authoritative.

KronosStage is master77c1f9a, held, clean. Its matching current sections are165lines.
Existing helpers prepend the same current section to both files, preserve complete
old bytes, back them up, and verify bounded reads. Read budgets stream4/8/16KiB,
256lines, complete H2 section only, fences respected, duplicate sections read once.
Historical sections are not automatic fallback when a current section is invalid.

PhotoLibraryTools resolves to ChasingLight. Main6225d77 has pre-existing modified
AGENTS.md; Planning worktree e048337 has an untracked qa_planning_debug.py. Each has
its own current paths/branch identity; both newest sections match within the pair.
They remain separate registered workspaces, with explicit provenance and no silent
switch from main to feature. All existing holds and independently running work remain.

Adapter design:

- Resolve and verify the registered root/Git identity and instruction hierarchy.
- Read only complete newest sections; preserve source path/line/hash; deduplicate
  identical content only inside the selected project. Report mismatches explicitly.
- Produce a bounded startup packet from authoritative files plus live Git state.
  Preserve the leading hold and constraints. Do not silently truncate an over-limit
  section or ingest the rest of its history into a small model.
- History requires an explicit section selector, bounded stream/byte/line budget
  and no automatic load. Return one complete selected record, never a partial prefix.
- A typed handoff separates verified evidence, agent-reported results, assumptions,
  unfinished work, decisions and next steps. Include agent/time/branch/commit,
  changed-file hashes and actual test receipts; reject stale Git/memory state.
- Save only when authorized. Exclusive project lock plus compare-and-swap hashes,
  immutable old-byte backups and a recoverable transaction journal protect the pair.
  Each file replacement is atomic; two distinct files cannot become one OS-atomic
  transaction. Readers must reject an unfinished transaction until reconciled.
  Preserve history and reject duplicate event IDs/content; never silently overwrite
  concurrent external edits or automatically break another writer's lock.
- Keep secrets out through bounded schema/input validation and credential-pattern
  rejection; no logging inherited environment values or credential-file contents.
- For Claude2.1.319, use supported AGENTS.md instruction discovery through a scoped
  `--settings` file, and a generated append-system-prompt file for bounded current
  memory. No new/copying CLAUDE.md in protected projects or changed global settings.
- Codex/Qwen wrappers use the same adapter on startup. Save commands/tools use the
  same authoritative workflow; no agent gets to replace instructions via a model reply.

Validation writes only disposable fixture memories. Existing project memories and
AGENTS files stay byte-identical. Real takeover is read-only and cannot resume a hold.
Claude bridge loading can be tested without sending data to Anthropic; actual Claude
inference must remain separately identified if not run. This plan introduces adapter
operations, not a change to project authority or blanket memory-writing permission.
