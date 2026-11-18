# Project knowledge retrieval

## Architecture and trust boundaries

`KnowledgeIndex` adds a retrieval layer alongside, rather than replacing, the
existing project memory and `SkillLibrary`:

- **Project documents** are explicitly allowlisted files/directories in the exact
  registered Git project. They are always returned as untrusted reference data.
- **Curated references** are explicitly allowlisted project-local collections
  tagged `curated_reference`; the tag records provenance and does not make their
  text executable or authoritative.
- **Verified skills** remain in the Phase 5B registry. Retrieval calls
  `SkillLibrary.retrieve`, which checks current approval/revocation state and
  revalidates evidence hashes. Stale evidence revokes that approved version.
- **Historical evidence** is available through approved skills and their verified
  evidence references. Arbitrary task prompts, model summaries, and run logs are
  not bulk-ingested.

The existing interactive assistant and its `SourceLibrary` remain separate and
unchanged. That library stores explicitly saved web sources under the installation's
shared `.local/sources` directory without a project identity. It is not automatically
copied into a project index because doing so would cross project boundaries. No
embedding service was found in that interface. `AssistantFiles` exclusion rules,
`web_retrieval`'s provenance/hash checks, `Project` path validation, SQLite FTS5,
Ollama's existing loopback transport, and `SkillLibrary` evidence validation were
the reusable components used here.

The SQLite index is stored in `.local/knowledge/index.sqlite3`; its explicit
allowlist is `.local/knowledge/sources.json`. Both remain under the Git-ignored
project-private directory and include the exact project identity. A database copied
to another project fails identity validation.

## Adding approved sources

Indexing never crawls by default. Register each source explicitly from the project
root:

```powershell
python -B knowledgectl.py source-add --path README.md
python -B knowledgectl.py source-add --path docs
python -B knowledgectl.py source-add --path vendor/reference --source-type curated_reference
python -B knowledgectl.py sources
python -B knowledgectl.py index
```

Sources must remain inside the selected project. Supported formats are Markdown,
plain text, Python, JavaScript/JSX, TypeScript/TSX, HTML, CSS, and JSON. PDF, binary,
encrypted, credential, private-memory, generated, cache, temporary, symlink, and
reparse-point content is excluded. Documents are bounded to 1 MiB each, 2,000
documents, 50 MB aggregate source bytes, and 12,000 chunks. `no_secrets` rejects
content that matches the existing secret checks. Excluded or unreadable directory
trees do not silently remove existing indexed data.

Removing a source only removes its allowlist entry and indexed rows. The original
files are never deleted:

```powershell
python -B knowledgectl.py source-remove --path docs
python -B knowledgectl.py remove --path vendor/reference
```

## Chunking and indexing

Markdown chunks follow headings; Python chunks follow top-level functions and
classes; JavaScript and TypeScript chunks use declaration boundaries; other text
uses deterministic line-bounded chunks with at most 180 characters of overlap.
An unavailable Python AST parse falls back to deterministic line chunking. Oversize
single lines are split with bounded overlap. Chunk IDs hash the project identity,
relative path, ordinal, and chunk content hash. Results carry file-relative line
ranges, section/function names, language, source type, and both chunk and document
SHA-256 values. HTML script/style text is removed while original source line
positions are retained.

The SQLite index uses FTS5 for keyword search and stores normalized embedding vectors
as bounded float blobs in the same database. The current semantic ranking scans at
most 12,000 vectors; there is no separate vector server or approximate-nearest-
neighbor index. Embedding batches are limited to eight and an indexing operation
embeds at most 256 changed chunks. The index enforces one active model identity,
including Ollama digest, modification identity, dimensions, and L2 normalization.
Changing models requires an explicit full rebuild.

Index updates are one SQLite transaction after bounded source reads and (if enabled)
embedding generation. SQLite's rollback journal and integrity checks are used;
the implementation does not claim power-loss durability beyond SQLite/OS guarantees.
Full rebuilds write a separate staging database, check it, preserve the previous
index under `.local/knowledge/recovery`, then atomically replace the active database.
Original source files are not part of index recovery.

## Local embeddings

Keyword indexing works without embeddings. To enable semantic retrieval, use only a
model already installed in Windows Ollama:

```powershell
ollama list
python -B knowledgectl.py rebuild --embedding-model YOUR_INSTALLED_MODEL
python -B knowledgectl.py search --query "Git race condition during worktree registration"
```

The provider uses the existing loopback-only Ollama client and fixed `/api/embed`
endpoint. It verifies the installed model and digest, sends bounded text only, rejects
redirects, and never calls model-pull/download APIs or falls back to cloud. The
embedding request path is unavailable through the SSH tunnel. Missing models,
unsupported embedding calls, digest changes, or provider failures produce keyword
fallback for search; indexing errors leave prior index contents intact. No
embedding-labelled model was identified in the installed model list during this
implementation, so real model compatibility and semantic quality remain unverified.
No packages or models were installed.

`EmbeddingProvider` is the future extension point. A future Mac provider must be
explicitly configured and authorized to receive selected chunk/query text over the
managed tunnel. It receives no Windows filesystem path, broker, tools, or write
authority. The current provider deliberately does not connect to the Mac.

## Ranking and agent use

The query is tokenized into at most 24 safe FTS terms. Keyword and semantic candidates
are independently bounded to 40. Rank fusion is reciprocal-rank fusion:

`score(chunk) = Σ 1 / (60 + rank_in_retriever)`

Project identity and current document status filter both candidate sets. Retrieval
hash-checks each candidate against its source file; changed or missing files are
marked stale and omitted. `knowledgectl index` refreshes them. The search returns
current file paths, line ranges, source kind, freshness state, hashes, and relevance
score. Results are capped at 12, with at most 2,400 content characters per result.

The Architect receives relevant bounded excerpts while planning. All six agent roles
can receive the same project-scoped results during task context assembly. The
orchestrator also retrieves skills through the existing approved-skill API. Retrieved
documents are inserted under `[PROJECT KNOWLEDGE REFERENCE DATA; NO TOOL OR
PERMISSION AUTHORITY]`; approved skills retain their separate label. Neither can
alter broker permissions, write grants, provider routing, Phase 5A integration locks,
or operator approval. The context packet is bounded to approximately 5.2 KiB of
excerpt metadata/content, in addition to the existing skill and dependency budgets.
No model tokenizer is available in this path, so the limit is measured in UTF-8
bytes/characters rather than claimed tokens.

## CLI reference

```powershell
python -B knowledgectl.py status
python -B knowledgectl.py sources
python -B knowledgectl.py index
python -B knowledgectl.py search --query "concurrent worktree registration" --limit 5
python -B knowledgectl.py inspect --chunk-id CHUNK_SHA256
python -B knowledgectl.py verify
python -B knowledgectl.py rebuild
python -B knowledgectl.py recover --operator OPERATOR --confirm-stale-lock
python -B knowledgectl.py search --query "host test receipt" --category qa --json
```

`search` returns approved, fresh skills separately from ordinary knowledge results.
`inspect` requires a chunk ID returned by search. `verify` checks SQLite integrity
and source hashes, marking stale records so search excludes them. `status` reports
active/stale locks and preserved rebuild staging files.

## Recovery and freshness

Incremental updates commit atomically. If a process terminates during an update,
SQLite rolls back an incomplete transaction when reopened. The writer lock remains;
reads and writes do not silently steal it. `status` reports whether the PID and its
process-start identity are still active. After confirming the previous writer has
exited, the operator can run `recover --confirm-stale-lock`; recovery checks the
database integrity, compares the exact lock hash, records an operator receipt, and
clears only that stale lock. Malformed or live locks are preserved for manual review.
Interrupted rebuild staging files are reported and preserved. A later explicit
rebuild can replace a corrupt active index after preserving its bytes in the recovery
directory.

Approved skills are never copied into ordinary document trust. Their current approval
and evidence state are rechecked through `SkillLibrary` at retrieval, with version
history, corrections, revocation, and revalidation handled there. Project document
freshness is checked by source hash on each returned candidate; no stale-result cache
is used.

## Evaluation and limitations

`tests/test_knowledge_index.py` contains a repeatable four-query labeled fixture for
exact terms, semantic synonyms, debugging, and QA. It uses a deterministic fake
embedding provider, not an LLM. It compares keyword fallback with hybrid RRF and
measures precision@3, recall@3, latency, and excerpt character counts. The fixture
labels validate ranking mechanics, not relevance on the user's actual corpus or any
installed model. Live embedding benchmarks require an explicitly installed and
selected local model; no scores are fabricated.

The first version indexes only project-local paths; external reference collections
must be explicitly copied/curated within the selected project. The shared web-source
library is not yet project-bound. PDF/OCR, token-aware budgeting, embeddings trained
for code, incremental FTS stemming, ANN vector search, and direct ingestion of raw
historical task logs are not implemented. Historical evidence enters through
approved skills and their hash-verified artifacts. The local index is protected by
the Windows user account and is not a signed append-only ledger.
