"""Local document ingestion and retrieval over the shared project knowledge DB.

Uploaded material is untrusted reference data. It is never executed or promoted
to the SkillLibrary. Original bytes and extracted passages share the existing
KnowledgeIndex SQLite file so its project identity and recovery boundary apply.
"""
from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
import os
import re
import sqlite3
import threading
from typing import Protocol, runtime_checkable
import uuid
from urllib.parse import urlsplit
import zipfile
from pathlib import Path

from agent_core import AgentError, Project, atomic_write
from knowledge_index import (KnowledgeIndex, _content_text, chunk_document, _is_reparse,
                             _pack_vector, _unpack_vector, _vector, RRF_K)

MAX_UPLOAD_BYTES = 512 * 1024 * 1024
MAX_EXTRACTED_CHARS = 250_000_000
MAX_PDF_PAGES = 10_000
MAX_LIBRARY_DOCUMENTS = 500
MAX_LIBRARY_CHUNKS = 12000
MAX_EMBEDDING_BATCH = 64
MAX_DOCUMENT_VERSIONS = 32
MAX_BACKUP_BYTES = 256 * 1024 * 1024
MAX_RETRIEVAL_RESULTS = 8
MIN_SEMANTIC_SIMILARITY = 0.28
_FTS_STOPWORDS = frozenset('''a an and are as at be been being by can could did do does for from had has have
how i in into is it its may might must of on or our should that the their them then there these
this those to was were what when where which who why will with would you your'''.split())
SUPPORTED = {'.txt': 'text/plain', '.md': 'text/markdown', '.markdown': 'text/markdown',
             '.pdf': 'application/pdf'}
CONVERSATION_ID = re.compile(r'^[a-f0-9]{32}$')
DOCUMENT_ID = re.compile(r'^[a-f0-9]{32}$')


class _IngestionCancelled(Exception):
    """Internal signal for a safely checkpointed user cancellation."""


class _IngestionPaused(Exception):
    """Internal signal for a clean shutdown between durable checkpoints."""


@runtime_checkable
class KnowledgeSourceAdapter(Protocol):
    """Bounded, provenance-preserving source adapter contract."""
    source_id: str

    def ingest(self, filename: str, content: bytes, **scope) -> dict: ...
    def search(self, query: str, **scope) -> dict: ...
    def list_documents(self, **scope) -> list[dict]: ...
    def remove(self, document_id: str, **scope) -> dict: ...


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _safe_filename(value: str) -> str:
    if not isinstance(value, str) or len(value.encode('utf-8', errors='replace')) > 512:
        raise AgentError('Document filename is missing or too long.')
    name = value.replace('\\', '/').split('/')[-1]
    name = ''.join(char for char in name if char.isalnum() or char in ' ._-()')
    name = name.strip(' .')[:120]
    if not name or name in {'.', '..'}:
        raise AgentError('Document filename is invalid.')
    suffix = '.' + name.rsplit('.', 1)[-1].casefold() if '.' in name else ''
    if suffix not in SUPPORTED:
        raise AgentError('Supported uploads are TXT, Markdown, and PDF documents.')
    return name


def _extract(filename: str, raw: bytes) -> tuple[list[dict], int | None, str]:
    suffix = '.' + filename.rsplit('.', 1)[-1].casefold()
    if not raw or len(raw) > MAX_UPLOAD_BYTES:
        raise AgentError(f'Document must be nonempty and no larger than {MAX_UPLOAD_BYTES // (1024 * 1024)} MiB.')
    pages = None
    if suffix == '.pdf':
        if not raw.startswith(b'%PDF-'):
            raise AgentError('PDF signature is invalid.')
        try:
            from pypdf import PdfReader
            import io
            reader = PdfReader(io.BytesIO(raw), strict=True)
            if reader.is_encrypted:
                raise AgentError('Encrypted PDFs are not supported.')
            pages = len(reader.pages)
            if pages < 1 or pages > MAX_PDF_PAGES:
                raise AgentError(f'PDF must contain between 1 and {MAX_PDF_PAGES} pages.')
            chunks = []
            total = 0
            for number, page in enumerate(reader.pages, 1):
                text = page.extract_text() or ''
                total += len(text)
                if total > MAX_EXTRACTED_CHARS:
                    raise AgentError('Extracted PDF text exceeds the 4 million character limit.')
                if text.strip():
                    for part in chunk_document(text, '.txt'):
                        part['section'] = f'Page {number}'
                        part['page_number'] = number
                        chunks.append(part)
            if not chunks:
                raise AgentError('PDF contains no extractable text. Scanned PDFs need an authorized OCR capability.')
            text_for_hash = '\n'.join(row['text'] for row in chunks)
        except AgentError:
            raise
        except Exception as exc:
            raise AgentError('PDF could not be safely parsed as a supported document.') from exc
    else:
        text = _content_text(raw, suffix)
        if len(text) > MAX_EXTRACTED_CHARS:
            raise AgentError('Extracted document text exceeds the configured size limit.')
        chunks = chunk_document(text, suffix)
        for part in chunks:
            part['page_number'] = None
        text_for_hash = text
    if not chunks or len(chunks) > MAX_LIBRARY_CHUNKS:
        raise AgentError('Document has no indexable passages or exceeds the chunk limit.')
    return chunks, pages, text_for_hash


class DocumentKnowledgeLibrary:
    """Global and conversation-scoped document knowledge in KnowledgeIndex SQLite."""
    source_id = 'local_documents'

    def __init__(self, project: Project, embedding_provider=None):
        self.index = KnowledgeIndex(project)
        self.project = project
        self._embedding_provider = embedding_provider
        self.index._check_private(create=True)
        restore_journal = self.index.directory / 'restore.pending.json'
        if restore_journal.exists():
            raise AgentError('A previous knowledge restore is incomplete; preserve its journal and reconcile it first.')
        connection = self.index._connect()
        try:
            connection.executescript('''
                CREATE TABLE IF NOT EXISTS library_documents (
                    doc_id TEXT PRIMARY KEY, project_id TEXT NOT NULL,
                    scope TEXT NOT NULL CHECK(scope IN ('global','conversation')),
                    conversation_id TEXT, title TEXT NOT NULL, original_name TEXT NOT NULL,
                    extension TEXT NOT NULL, mime_type TEXT NOT NULL,
                    content_sha256 TEXT NOT NULL, byte_size INTEGER NOT NULL,
                    page_count INTEGER, status TEXT NOT NULL, duplicate_of TEXT,
                    semantic_status TEXT NOT NULL DEFAULT 'requires_reindex',
                    document_version INTEGER NOT NULL DEFAULT 1, version_of TEXT,
                    created_at TEXT NOT NULL, original BLOB NOT NULL
                );
                CREATE INDEX IF NOT EXISTS library_documents_scope
                    ON library_documents(project_id,scope,conversation_id,status);
                CREATE INDEX IF NOT EXISTS library_documents_hash
                    ON library_documents(project_id,content_sha256);
                CREATE TABLE IF NOT EXISTS library_chunks (
                    chunk_id TEXT PRIMARY KEY, doc_id TEXT NOT NULL
                        REFERENCES library_documents(doc_id) ON DELETE CASCADE,
                    ordinal INTEGER NOT NULL, section TEXT NOT NULL,
                    page_number INTEGER, line_start INTEGER NOT NULL,
                    line_end INTEGER NOT NULL, content TEXT NOT NULL,
                    content_sha256 TEXT NOT NULL, embedding BLOB, embedding_dimensions INTEGER,
                    embedding_provider TEXT, embedding_model TEXT, embedding_version TEXT,
                    embedding_digest TEXT, UNIQUE(doc_id,ordinal)
                );
                CREATE INDEX IF NOT EXISTS library_chunks_doc_id ON library_chunks(doc_id);
                CREATE VIRTUAL TABLE IF NOT EXISTS library_fts USING fts5(
                    chunk_id UNINDEXED, content, tokenize='unicode61'
                );
                CREATE TABLE IF NOT EXISTS research_source_metadata (
                    doc_id TEXT PRIMARY KEY REFERENCES library_documents(doc_id) ON DELETE CASCADE,
                    source_url TEXT NOT NULL, source_title TEXT NOT NULL,
                    provider_id TEXT NOT NULL, retrieved_at TEXT NOT NULL,
                    publication_date TEXT, source_content_sha256 TEXT NOT NULL,
                    research_objective_sha256 TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS library_ingestion_jobs (
                    job_id TEXT PRIMARY KEY, doc_id TEXT NOT NULL UNIQUE
                        REFERENCES library_documents(doc_id) ON DELETE CASCADE,
                    project_id TEXT NOT NULL, spool_name TEXT NOT NULL,
                    state TEXT NOT NULL, stage TEXT NOT NULL,
                    total_bytes INTEGER NOT NULL, total_pages INTEGER,
                    processed_pages INTEGER NOT NULL DEFAULT 0,
                    extracted_pages INTEGER NOT NULL DEFAULT 0,
                    indexed_chunks INTEGER NOT NULL DEFAULT 0,
                    embedded_chunks INTEGER NOT NULL DEFAULT 0,
                    extracted_chars INTEGER NOT NULL DEFAULT 0,
                    active_section TEXT NOT NULL DEFAULT '',
                    empty_page_count INTEGER NOT NULL DEFAULT 0,
                    empty_pages_json TEXT NOT NULL DEFAULT '[]',
                    failure_pages_json TEXT NOT NULL DEFAULT '[]',
                    warnings_json TEXT NOT NULL DEFAULT '[]',
                    error TEXT, cancel_requested INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL, updated_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS library_jobs_scope
                    ON library_ingestion_jobs(project_id,state,updated_at);
                CREATE TABLE IF NOT EXISTS library_studies (
                    study_id TEXT PRIMARY KEY, project_id TEXT NOT NULL,
                    doc_id TEXT NOT NULL REFERENCES library_documents(doc_id) ON DELETE CASCADE,
                    conversation_id TEXT, source_sha256 TEXT NOT NULL, state TEXT NOT NULL,
                    total_sections INTEGER NOT NULL, completed_sections INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL, updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS library_study_sections (
                    study_id TEXT NOT NULL REFERENCES library_studies(study_id) ON DELETE CASCADE,
                    ordinal INTEGER NOT NULL, section TEXT NOT NULL, page_start INTEGER, page_end INTEGER,
                    source_chunks_json TEXT NOT NULL, state TEXT NOT NULL DEFAULT 'pending',
                    summary TEXT, concepts_json TEXT NOT NULL DEFAULT '[]',
                    examples_json TEXT NOT NULL DEFAULT '[]', lessons_json TEXT NOT NULL DEFAULT '[]',
                    updated_at TEXT NOT NULL, PRIMARY KEY(study_id,ordinal)
                );
                INSERT INTO metadata(key,value) VALUES('library_schema_version','5')
                    ON CONFLICT(key) DO NOTHING;
            ''')
            job_columns = {row['name'] for row in connection.execute(
                'PRAGMA table_info(library_ingestion_jobs)').fetchall()}
            if 'active_section' not in job_columns:
                connection.execute("ALTER TABLE library_ingestion_jobs ADD COLUMN active_section TEXT NOT NULL DEFAULT ''")
                connection.commit()
            version = self.index._meta(connection, 'library_schema_version')
            if version in {'1','2','3','4'}:
                document_columns = {row['name'] for row in connection.execute(
                    'PRAGMA table_info(library_documents)').fetchall()}
                if version in {'1','2'}:
                    for name, declaration in (
                        ('semantic_status', "TEXT NOT NULL DEFAULT 'requires_reindex'"),
                        ('document_version','INTEGER NOT NULL DEFAULT 1'), ('version_of','TEXT')):
                        if name not in document_columns:
                            connection.execute(f'ALTER TABLE library_documents ADD COLUMN {name} {declaration}')
                    chunk_columns = {row['name'] for row in connection.execute(
                        'PRAGMA table_info(library_chunks)').fetchall()}
                    for name, declaration in (
                        ('embedding', 'BLOB'), ('embedding_dimensions', 'INTEGER'),
                        ('embedding_provider', 'TEXT'), ('embedding_model', 'TEXT'),
                        ('embedding_version', 'TEXT'), ('embedding_digest', 'TEXT')):
                        if name not in chunk_columns:
                            connection.execute(f'ALTER TABLE library_chunks ADD COLUMN {name} {declaration}')
                connection.execute("UPDATE metadata SET value='5' WHERE key='library_schema_version'")
                connection.commit()
                version = '5'
            if version != '5':
                raise AgentError('Document library schema is unsupported; preserve the knowledge index.')
            identity = self.index._meta(connection, 'project_id')
            if identity is None:
                if (connection.execute('SELECT count(*) FROM documents').fetchone()[0]
                        or connection.execute('SELECT count(*) FROM library_documents').fetchone()[0]):
                    raise AgentError('Unclaimed knowledge database contains data; preserve it for review.')
                connection.execute('INSERT INTO metadata(key,value) VALUES(?,?)',
                                   ('project_id', project.identity))
                connection.commit()
            elif identity != project.identity:
                raise AgentError('Document library database belongs to another project scope.')
        finally:
            connection.close()
        try:
            with self._writer_lock('document-reconcile-incomplete-indexing'):
                connection = self.index._connect()
                try:
                    connection.execute('''UPDATE library_documents SET semantic_status='requires_reindex'
                        WHERE project_id=? AND status='current' AND semantic_status='semantic_indexing'
                        AND NOT EXISTS (SELECT 1 FROM library_chunks c WHERE c.doc_id=library_documents.doc_id
                            AND c.embedding IS NOT NULL)''', (self.project.identity,))
                    connection.commit()
                finally:
                    connection.close()
        except AgentError:
            # A concurrent or unreconciled writer owns the database; leave its state untouched.
            pass
        self._job_condition = threading.Condition()
        self._worker = None
        self._worker_stop = False
        self._active_job = None
        self._requeue_interrupted_jobs()

    def _provider(self):
        provider = self._embedding_provider
        if provider is None:
            return None
        try:
            descriptor = dict(provider.descriptor())
            if (descriptor.get('authorized') is not True or descriptor.get('available') is not True
                    or type(descriptor.get('dimensions')) is not int
                    or not 1 <= descriptor['dimensions'] <= 8192
                    or not isinstance(descriptor.get('model_digest'), str)
                    or not re.fullmatch(r'[0-9a-f]{64}', descriptor['model_digest'])):
                return None
            return provider
        except (AgentError, OSError, ValueError, KeyError, TypeError):
            return None

    @staticmethod
    def _conversation(value):
        if value is not None and (not isinstance(value, str) or not CONVERSATION_ID.fullmatch(value)):
            raise AgentError('Conversation identity is invalid.')
        return value

    def queue_ingestion_stream(self, filename, source, content_length, *, scope='global',
                               conversation_id=None, title=None) -> dict:
        """Persist an upload and a resumable job while reading it in bounded blocks."""
        filename = _safe_filename(filename)
        if (type(content_length) is not int or not 1 <= content_length <= MAX_UPLOAD_BYTES
                or not callable(getattr(source, 'read', None))):
            raise AgentError(f'Document must be nonempty and no larger than {MAX_UPLOAD_BYTES // (1024 * 1024)} MiB.')
        if scope not in {'global', 'conversation'}:
            raise AgentError('Document retention must be global or this conversation.')
        conversation_id = self._conversation(conversation_id)
        if scope == 'conversation' and conversation_id is None:
            raise AgentError('Choose or create a conversation before attaching a temporary document.')
        if scope == 'global':
            conversation_id = None
        display_title = title.strip() if isinstance(title, str) else filename.rsplit('.', 1)[0]
        display_title = display_title[:160]
        if not display_title or len(display_title.encode('utf-8')) > 512:
            raise AgentError('Document title is invalid.')

        self.index._check_private(create=True)
        spool_directory = self.index.directory / 'ingestion-spool'
        spool_directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        if spool_directory.is_symlink() or not spool_directory.resolve().is_relative_to(self.index.directory.resolve()):
            raise AgentError('Knowledge upload spool is not a private project-local directory.')
        job_id = uuid.uuid4().hex
        spool_name = job_id + '.upload'
        spool_path = spool_directory / spool_name
        digest = hashlib.sha256()
        received = 0
        try:
            with spool_path.open('xb') as spool:
                while received < content_length:
                    block = source.read(min(1024 * 1024, content_length - received))
                    if not block:
                        raise AgentError('Upload ended before its declared content length.')
                    if not isinstance(block, bytes):
                        raise AgentError('Upload stream returned non-byte content.')
                    received += len(block)
                    if received > content_length:
                        raise AgentError('Upload exceeded its declared content length.')
                    digest.update(block)
                    spool.write(block)
                spool.flush()
                os.fsync(spool.fileno())

            raw_hash = digest.hexdigest()
            with self._writer_lock('document-ingestion-queue'):
                connection = self.index._connect()
                try:
                    existing = connection.execute('''SELECT doc_id,title,scope FROM library_documents
                        WHERE project_id=? AND scope=? AND ifnull(conversation_id,'')=ifnull(?,'')
                        AND content_sha256=? AND status='current' LIMIT 1''',
                        (self.project.identity, scope, conversation_id, raw_hash)).fetchone()
                    if existing:
                        spool_path.unlink(missing_ok=True)
                        return {'document_id': existing['doc_id'], 'duplicate': True,
                                'duplicate_of': existing['doc_id'], 'title': existing['title'],
                                'scope': existing['scope'], 'status': 'already_indexed'}
                    previous = connection.execute('''SELECT doc_id,document_version FROM library_documents
                        WHERE project_id=? AND scope=? AND ifnull(conversation_id,'')=ifnull(?,'')
                        AND lower(original_name)=lower(?) AND status='current' LIMIT 1''',
                        (self.project.identity, scope, conversation_id, filename)).fetchone()
                    if previous and previous['document_version'] >= MAX_DOCUMENT_VERSIONS:
                        raise AgentError('Document version history reached its bounded 32-version limit.')
                    active_count = connection.execute('''SELECT count(*) FROM library_documents
                        WHERE project_id=? AND status='current' ''', (self.project.identity,)).fetchone()[0]
                    if active_count >= MAX_LIBRARY_DOCUMENTS:
                        raise AgentError('Knowledge library is at its bounded document capacity.')
                    connection.execute('BEGIN IMMEDIATE')
                    cursor = connection.execute('''INSERT INTO library_documents
                        (doc_id,project_id,scope,conversation_id,title,original_name,extension,mime_type,
                         content_sha256,byte_size,page_count,status,duplicate_of,semantic_status,
                         document_version,version_of,created_at,original)
                        VALUES(?,?,?,?,?,?,?,?,?,?,NULL,'current',NULL,'extracting',?,?,?,zeroblob(?))''',
                        (job_id, self.project.identity, scope, conversation_id, display_title, filename,
                         '.' + filename.rsplit('.', 1)[-1].casefold(),
                         SUPPORTED['.' + filename.rsplit('.', 1)[-1].casefold()], raw_hash,
                         content_length, (previous['document_version'] + 1) if previous else 1,
                         previous['doc_id'] if previous else None, _now(), content_length))
                    rowid = cursor.lastrowid
                    with connection.blobopen('library_documents', 'original', rowid, readonly=False) as target:
                        with spool_path.open('rb') as stored:
                            while True:
                                block = stored.read(1024 * 1024)
                                if not block:
                                    break
                                target.write(block)
                    connection.execute('''INSERT INTO library_ingestion_jobs
                        (job_id,doc_id,project_id,spool_name,state,stage,total_bytes,created_at,updated_at)
                        VALUES(?,?,?,?,?,?,?,?,?)''',
                        (job_id, job_id, self.project.identity, spool_name, 'queued', 'queued',
                         content_length, _now(), _now()))
                    connection.commit()
                except BaseException:
                    if connection.in_transaction:
                        connection.rollback()
                    raise
                finally:
                    connection.close()
        except BaseException:
            spool_path.unlink(missing_ok=True)
            raise
        self._notify_worker()
        return {'document_id': job_id, 'job_id': job_id, 'duplicate': False,
                'title': display_title, 'scope': scope, 'status': 'queued',
                'byte_size': content_length, 'content_sha256': raw_hash}

    def _notify_worker(self):
        self.start_ingestion_worker()
        with self._job_condition:
            self._job_condition.notify_all()

    def start_ingestion_worker(self):
        """Start one project-scoped daemon worker for queued durable jobs."""
        connection=self.index._connect()
        try:
            pending=connection.execute('''SELECT count(*) FROM library_ingestion_jobs
                WHERE project_id=? AND state='queued' ''',(self.project.identity,)).fetchone()[0]
        finally:
            connection.close()
        if not pending:
            return
        with self._job_condition:
            if self._worker is not None and self._worker.is_alive():
                return
            self._worker_stop = False
            self._worker = threading.Thread(target=self._ingestion_loop,
                name='knowledge-ingestion-' + self.project.identity[:8], daemon=True)
            self._worker.start()

    def _requeue_interrupted_jobs(self):
        connection = self.index._connect()
        try:
            connection.execute('''UPDATE library_ingestion_jobs SET state='queued',stage='resuming',updated_at=?
                WHERE project_id=? AND state IN ('extracting','indexing','embedding')''',
                (_now(), self.project.identity))
            connection.commit()
        finally:
            connection.close()

    def list_ingestion_jobs(self, *, conversation_id=None) -> list[dict]:
        conversation_id = self._conversation(conversation_id)
        connection = self.index._connect()
        try:
            rows = connection.execute('''SELECT j.*,d.title,d.original_name,d.extension,d.byte_size,
                    d.content_sha256,d.scope,d.conversation_id,d.page_count,d.semantic_status
                FROM library_ingestion_jobs j JOIN library_documents d ON d.doc_id=j.doc_id
                WHERE j.project_id=? AND (d.scope='global' OR d.conversation_id=?)
                ORDER BY j.created_at DESC LIMIT ?''',
                (self.project.identity, conversation_id or '', MAX_LIBRARY_DOCUMENTS)).fetchall()
            result = []
            for row in rows:
                item = dict(row)
                for name in ('empty_pages_json','failure_pages_json','warnings_json'):
                    try:
                        item[name[:-5]] = json.loads(item.pop(name))
                    except (json.JSONDecodeError, TypeError):
                        item[name[:-5]] = []
                item['processing_percent'] = (round(100 * item['processed_pages'] / item['total_pages'], 1)
                    if item['total_pages'] else (100.0 if item['state'] in {'ready','partially_available'} else None))
                item['search_ready'] = item['indexed_chunks'] > 0
                result.append(item)
            return result
        finally:
            connection.close()

    def cancel_ingestion(self, job_id: str) -> dict:
        if not isinstance(job_id, str) or not DOCUMENT_ID.fullmatch(job_id):
            raise AgentError('Knowledge ingestion job identity is invalid.')
        connection = self.index._connect()
        try:
            row = connection.execute('''SELECT state FROM library_ingestion_jobs
                WHERE job_id=? AND project_id=?''', (job_id, self.project.identity)).fetchone()
            if row is None:
                raise AgentError('Knowledge ingestion job is unavailable in this project.')
            if row['state'] in {'ready','partially_available','cancelled','failed'}:
                return {'job_id': job_id, 'state': row['state']}
            connection.execute('UPDATE library_ingestion_jobs SET cancel_requested=1,updated_at=? WHERE job_id=?',
                                (_now(), job_id))
            connection.commit()
        finally:
            connection.close()
        self._notify_worker()
        return {'job_id': job_id, 'state': 'cancelling'}

    def resume_ingestion(self, job_id: str) -> dict:
        if not isinstance(job_id, str) or not DOCUMENT_ID.fullmatch(job_id):
            raise AgentError('Knowledge ingestion job identity is invalid.')
        connection = self.index._connect()
        try:
            row = connection.execute('''SELECT state,spool_name FROM library_ingestion_jobs
                WHERE job_id=? AND project_id=?''', (job_id, self.project.identity)).fetchone()
            if row is None:
                raise AgentError('Knowledge ingestion job is unavailable in this project.')
            if row['state'] in {'ready','partially_available'}:
                return {'job_id': job_id, 'state': row['state']}
            spool_path = self.index.directory / 'ingestion-spool' / row['spool_name']
            if spool_path.is_symlink() or not spool_path.is_file():
                raise AgentError('Original upload spool is missing; preserve the document and job record.')
            connection.execute('''UPDATE library_ingestion_jobs SET state='queued',stage='queued',
                cancel_requested=0,error=NULL,updated_at=? WHERE job_id=?''', (_now(), job_id))
            connection.commit()
        finally:
            connection.close()
        self._notify_worker()
        return {'job_id': job_id, 'state': 'queued'}

    def _ingestion_loop(self):
        while not self._worker_stop:
            connection = self.index._connect()
            try:
                row = connection.execute('''SELECT job_id FROM library_ingestion_jobs
                    WHERE project_id=? AND state='queued' ORDER BY created_at LIMIT 1''',
                    (self.project.identity,)).fetchone()
            finally:
                connection.close()
            if row is None:
                return
            self._active_job = row['job_id']
            try:
                self._process_ingestion_job(row['job_id'])
            except _IngestionCancelled:
                pass
            except _IngestionPaused:
                self._update_ingestion(row['job_id'], state='queued', stage='resuming')
                return
            except Exception as exc:
                self._finish_ingestion_job(row['job_id'], 'failed', 'failed', str(exc)[:500])
            finally:
                self._active_job = None

    def _job_row(self, job_id):
        connection = self.index._connect()
        try:
            row = connection.execute('''SELECT j.*,d.original_name,d.extension,d.content_sha256,
                    d.byte_size,d.page_count,d.version_of FROM library_ingestion_jobs j
                JOIN library_documents d ON d.doc_id=j.doc_id
                WHERE j.job_id=? AND j.project_id=?''', (job_id, self.project.identity)).fetchone()
            if row is None:
                raise AgentError('Knowledge ingestion job disappeared from its project index.')
            return dict(row)
        finally:
            connection.close()

    def _update_ingestion(self, job_id, *, state=None, stage=None, **values):
        permitted = {'total_pages','processed_pages','extracted_pages','indexed_chunks',
            'embedded_chunks','extracted_chars','empty_page_count','empty_pages_json',
            'failure_pages_json','warnings_json','error','cancel_requested'}
        if set(values) - permitted:
            raise AgentError('Knowledge job attempted an unsupported state update.')
        fields = {**values, 'updated_at': _now()}
        if state is not None:
            fields['state'] = state
        if stage is not None:
            fields['stage'] = stage
        assignments = ','.join(f'{key}=?' for key in fields)
        connection = self.index._connect()
        try:
            connection.execute(f'UPDATE library_ingestion_jobs SET {assignments} WHERE job_id=? AND project_id=?',
                (*fields.values(), job_id, self.project.identity))
            connection.commit()
        finally:
            connection.close()

    def _finish_ingestion_job(self, job_id, state, stage, error=None):
        self._update_ingestion(job_id, state=state, stage=stage, error=error)
        if state == 'failed':
            self._set_semantic_status(job_id, 'ingestion_failed')

    def _process_ingestion_job(self, job_id):
        job = self._job_row(job_id)
        spool = self.index.directory / 'ingestion-spool' / job['spool_name']
        if spool.is_symlink() or not spool.is_file() or not spool.resolve().is_relative_to(
                (self.index.directory / 'ingestion-spool').resolve()):
            raise AgentError('Original upload spool is missing or outside its private directory.')
        digest = hashlib.sha256()
        byte_count = 0
        with spool.open('rb') as source:
            while block := source.read(1024 * 1024):
                digest.update(block); byte_count += len(block)
        if byte_count != job['total_bytes'] or digest.hexdigest() != job['content_sha256']:
            raise AgentError('Original upload failed length or SHA-256 verification.')
        extension = job['extension']
        if extension == '.pdf':
            from pypdf import PdfReader
            with spool.open('rb') as source:
                reader = PdfReader(source, strict=True)
                if reader.is_encrypted:
                    raise AgentError('Encrypted PDFs are not supported.')
                page_count = len(reader.pages)
                if not 1 <= page_count <= MAX_PDF_PAGES:
                    raise AgentError(f'PDF must contain between 1 and {MAX_PDF_PAGES} pages.')
                self._update_ingestion(job_id, state='extracting', stage='extracting', total_pages=page_count)
                job=self._job_row(job_id)
                active_section=job['active_section']
                empty_pages=json.loads(job['empty_pages_json'])
                failures=json.loads(job['failure_pages_json'])
                batch_chunks=[];batch_chars=0;batch_extracted=0;batch_empty=0
                for page_number, page in enumerate(reader.pages, 1):
                    if self._worker_stop:
                        raise _IngestionPaused
                    if page_number <= job['processed_pages']:
                        continue
                    if page_number % 5 == 0:
                        latest=self._job_row(job_id)
                        if latest['cancel_requested']:
                            self._finish_ingestion_job(job_id, 'cancelled', 'cancelled')
                            return
                    try:
                        text = page.extract_text() or ''
                    except Exception as exc:
                        text='';failure=f'{type(exc).__name__}: {exc}'
                    else:
                        failure='page text exceeds 1,000,000 characters' if len(text)>1_000_000 else None
                    if batch_chars+job['extracted_chars']+len(text)>MAX_EXTRACTED_CHARS:
                        failure='document text exceeds the configured character limit';text=''
                    if failure:
                        if len(failures)<200:
                            failures.append({'page':page_number,'error':failure[:240]})
                    elif not text.strip():
                        batch_empty+=1
                        if len(empty_pages)<200:
                            empty_pages.append(page_number)
                    else:
                        page_chunks,active_section=self._page_chunks(text,page_number,active_section)
                        batch_chunks.extend(page_chunks);batch_chars+=len(text);batch_extracted+=1
                    is_checkpoint=(page_number%25==0 or page_number==page_count)
                    if is_checkpoint:
                        self._insert_ingestion_chunks(job_id,batch_chunks,progress={
                            'page':page_number,'text_chars':batch_chars,'extracted_pages':batch_extracted,
                            'empty_count':batch_empty,'empty_pages':empty_pages,'failures':failures,
                            'active_section':active_section})
                        job=self._job_row(job_id)
                        batch_chunks=[];batch_chars=0;batch_extracted=0;batch_empty=0
            self._complete_ingestion_index(job_id)
            return
        self._update_ingestion(job_id, state='extracting', stage='extracting')
        self._process_text_file(job_id, spool, extension)
        self._complete_ingestion_index(job_id)

    def _process_text_file(self, job_id, path, extension):
        from agent_core import no_secrets
        import io
        total_chars = 0
        line_number = 0
        current_section = ''
        buffer = []
        buffer_chars = 0
        chunk_group = 0
        initial_progress = self._job_row(job_id)['processed_pages']
        with path.open('rb') as raw:
            text_stream = io.TextIOWrapper(raw, encoding='utf-8-sig', errors='strict')
            try:
                for line in text_stream:
                    if self._worker_stop:
                        raise _IngestionPaused
                    line_number += 1
                    total_chars += len(line)
                    if total_chars > MAX_EXTRACTED_CHARS:
                        raise AgentError('Extracted text exceeds the configured character limit.')
                    heading = re.match(r'^\s{0,3}#{1,6}\s+(.+?)\s*#*\s*$', line) if extension in {'.md','.markdown'} else None
                    if heading and buffer:
                        self._record_text_group(job_id, buffer, chunk_group, current_section,
                                                total_chars, initial_progress)
                        chunk_group += 1; buffer = []; buffer_chars = 0
                    if heading:
                        current_section = heading.group(1).strip()[:180]
                    no_secrets(line)
                    buffer.append(line)
                    buffer_chars += len(line)
                    if buffer_chars >= 40_000:
                        self._record_text_group(job_id, buffer, chunk_group, current_section,
                                                total_chars, initial_progress)
                        chunk_group += 1; buffer = []; buffer_chars = 0
                if buffer:
                    self._record_text_group(job_id, buffer, chunk_group, current_section,
                                            total_chars, initial_progress)
            finally:
                text_stream.detach()
        if line_number == 0 or total_chars == 0:
            raise AgentError('Document contains no extractable text.')

    def _record_text_group(self, job_id, lines, group_number, section, total_chars, initial_progress):
        latest = self._job_row(job_id)
        if latest['cancel_requested']:
            self._finish_ingestion_job(job_id, 'cancelled', 'cancelled')
            raise _IngestionCancelled
        progress_number = group_number + 1
        if progress_number <= initial_progress:
            return
        text = ''.join(lines)
        if not text.strip():
            return
        chunks = chunk_document(text, '.txt')
        for part in chunks:
            part['section'] = section or part.get('section') or ''
        self._insert_ingestion_chunks(job_id, chunks, progress={
            'page':progress_number,'text_chars':len(text),'empty':False,'failure':None,
            'empty_pages':json.loads(latest['empty_pages_json']),
            'failures':json.loads(latest['failure_pages_json']),'warnings':[],'text_block':True})

    @staticmethod
    def _page_chunks(text, page_number, active_section):
        chunks=chunk_document(text,'.txt')
        section=active_section or f'Page {page_number}'
        for line in text.splitlines()[:24]:
            heading=re.match(r'^\s{0,3}#{1,6}\s+(.+)',line) or re.match(
                r'^\s*((?:chapter|part|section)\s+[\w.-]+[^\n]{0,140})$',line,re.I)
            if heading:
                active_section=heading.group(1).strip()[:180]
                section=active_section
                break
        for chunk in chunks:
            chunk['section']=section
            chunk['page_number']=page_number
        return chunks,active_section

    def _insert_ingestion_chunks(self, job_id, chunks, progress=None):
        with self._writer_lock('document-ingestion-page'):
            connection = self.index._connect()
            try:
                connection.execute('BEGIN IMMEDIATE')
                job = connection.execute('''SELECT * FROM library_ingestion_jobs WHERE job_id=? AND project_id=?''',
                    (job_id,self.project.identity)).fetchone()
                if job is None:
                    raise AgentError('Knowledge ingestion job disappeared.')
                indexed = job['indexed_chunks']
                total = connection.execute('SELECT count(*) FROM library_chunks').fetchone()[0]
                if total + len(chunks) > MAX_LIBRARY_CHUNKS:
                    raise AgentError('Knowledge library passage capacity was reached; document remains partially available.')
                for part in chunks:
                    ordinal = indexed
                    chunk_hash = hashlib.sha256(part['text'].encode('utf-8')).hexdigest()
                    chunk_id = hashlib.sha256(f'{job_id}\0{ordinal}\0{chunk_hash}'.encode()).hexdigest()
                    connection.execute('''INSERT INTO library_chunks
                        (chunk_id,doc_id,ordinal,section,page_number,line_start,line_end,content,content_sha256)
                        VALUES(?,?,?,?,?,?,?,?,?)''', (chunk_id,job_id,ordinal,part.get('section') or '',
                        part.get('page_number'),part['line_start'],part['line_end'],part['text'],chunk_hash))
                    connection.execute('INSERT INTO library_fts(chunk_id,content) VALUES(?,?)',
                                       (chunk_id,part['text']))
                    indexed += 1
                updates = {'indexed_chunks':indexed,'updated_at':_now()}
                if progress is not None:
                    updates['processed_pages'] = progress['page']
                    updates['extracted_chars'] = job['extracted_chars'] + progress['text_chars']
                    if not progress.get('text_block'):
                        updates['extracted_pages'] = job['extracted_pages'] + progress['extracted_pages']
                        updates['empty_page_count'] = job['empty_page_count'] + progress['empty_count']
                        updates['empty_pages_json'] = json.dumps(progress['empty_pages'])
                        updates['failure_pages_json'] = json.dumps(progress['failures'])
                        updates['active_section'] = progress.get('active_section',job['active_section'])
                assignments = ','.join(f'{key}=?' for key in updates)
                connection.execute(f'UPDATE library_ingestion_jobs SET {assignments} WHERE job_id=?',
                    (*updates.values(),job_id))
                connection.commit()
            except BaseException:
                if connection.in_transaction:
                    connection.rollback()
                raise
            finally:
                connection.close()

    def _complete_ingestion_index(self, job_id):
        job = self._job_row(job_id)
        if job['indexed_chunks'] == 0:
            self._finish_ingestion_job(job_id,'failed','failed','No searchable text was extracted from this document.')
            return
        with self._writer_lock('document-ingestion-complete'):
            connection = self.index._connect()
            try:
                connection.execute('''UPDATE library_documents SET page_count=? WHERE doc_id=? AND project_id=?''',
                    (job['total_pages'],job_id,self.project.identity))
                if job['version_of']:
                    connection.execute("UPDATE library_documents SET status='superseded' WHERE doc_id=? AND project_id=?",
                                       (job['version_of'],self.project.identity))
                connection.commit()
            finally:
                connection.close()
        self._update_ingestion(job_id,state='embedding',stage='embedding')
        semantic = self._embed_document(job_id)
        job = self._job_row(job_id)
        complete_pages = (job['total_pages'] is None or job['processed_pages'] == job['total_pages'])
        no_gaps = not json.loads(job['failure_pages_json']) and job['empty_page_count'] == 0
        complete_vectors = semantic == 'ready'
        ready = complete_pages and no_gaps and complete_vectors
        state = 'ready' if ready else 'partially_available'
        warnings = json.loads(job['warnings_json'])
        if job['empty_page_count']:
            warnings.append({'kind':'empty_pages','count':job['empty_page_count'],
                             'pages':json.loads(job['empty_pages_json'])[:200]})
        if json.loads(job['failure_pages_json']):
            warnings.append({'kind':'extraction_failures','count':len(json.loads(job['failure_pages_json']))})
        if not complete_vectors:
            warnings.append({'kind':'semantic_index_incomplete','status':semantic})
        self._update_ingestion(job_id,state=state,stage='complete' if ready else 'partial',
            warnings_json=json.dumps(warnings),error=None)
        if ready:
            spool = self.index.directory / 'ingestion-spool' / job['spool_name']
            spool.unlink(missing_ok=True)

    def ingest(self, filename: str, content: bytes, *, scope='global', conversation_id=None,
               title=None) -> dict:
        filename = _safe_filename(filename)
        if not isinstance(content, bytes):
            raise AgentError('Uploaded document content must be bytes.')
        if scope not in {'global', 'conversation'}:
            raise AgentError('Document retention must be global or this conversation.')
        conversation_id = self._conversation(conversation_id)
        if scope == 'conversation' and conversation_id is None:
            raise AgentError('Choose or create a conversation before attaching a temporary document.')
        if scope == 'global':
            conversation_id = None
        raw_hash = hashlib.sha256(content).hexdigest()
        chunks, page_count, _ = _extract(filename, content)
        display_title = title.strip() if isinstance(title, str) else filename.rsplit('.', 1)[0]
        display_title = display_title[:160]
        if not display_title or len(display_title.encode('utf-8')) > 512:
            raise AgentError('Document title is invalid.')
        self.index._check_private(create=True)
        with self._writer_lock('document-ingest'):
            connection = self.index._connect()
            try:
                project_id = self.project.identity
                existing = connection.execute('''SELECT doc_id,title,scope FROM library_documents
                    WHERE project_id=? AND scope=? AND ifnull(conversation_id,'')=ifnull(?,'')
                    AND content_sha256=? AND status='current' LIMIT 1''',
                    (project_id, scope, conversation_id, raw_hash)).fetchone()
                if existing:
                    return {'document_id': existing['doc_id'], 'duplicate': True,
                            'duplicate_of': existing['doc_id'], 'title': existing['title'],
                            'scope': existing['scope'], 'status': 'already_indexed'}
                previous_version = connection.execute('''SELECT doc_id,document_version FROM library_documents
                    WHERE project_id=? AND scope=? AND ifnull(conversation_id,'')=ifnull(?,'')
                    AND lower(original_name)=lower(?) AND status='current' LIMIT 1''',
                    (project_id,scope,conversation_id,filename)).fetchone()
                if previous_version and previous_version['document_version'] >= MAX_DOCUMENT_VERSIONS:
                    raise AgentError('Document version history reached its bounded 32-version limit.')
                count = connection.execute('SELECT count(*) FROM library_documents WHERE project_id=? AND status="current"',
                                           (project_id,)).fetchone()[0]
                chunks_count = connection.execute('SELECT count(*) FROM library_chunks').fetchone()[0]
                if count >= MAX_LIBRARY_DOCUMENTS or chunks_count + len(chunks) > MAX_LIBRARY_CHUNKS:
                    raise AgentError('Knowledge library is at its bounded document or passage capacity.')
                doc_id = uuid.uuid4().hex
                connection.execute('BEGIN IMMEDIATE')
                connection.execute('''INSERT INTO library_documents
                    (doc_id,project_id,scope,conversation_id,title,original_name,extension,mime_type,
                     content_sha256,byte_size,page_count,status,duplicate_of,semantic_status,
                     document_version,version_of,created_at,original)
                    VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)''',
                    (doc_id, project_id, scope, conversation_id, display_title, filename,
                     '.' + filename.rsplit('.', 1)[-1].casefold(),
                     SUPPORTED['.' + filename.rsplit('.',1)[-1].casefold()],
                     raw_hash, len(content), page_count, 'current', None, 'semantic_indexing',
                     (previous_version['document_version'] + 1) if previous_version else 1,
                     previous_version['doc_id'] if previous_version else None,
                     _now(), sqlite3.Binary(content)))
                for ordinal, part in enumerate(chunks):
                    chunk_hash = hashlib.sha256(part['text'].encode('utf-8')).hexdigest()
                    chunk_id = hashlib.sha256(f'{doc_id}\0{ordinal}\0{chunk_hash}'.encode()).hexdigest()
                    connection.execute('''INSERT INTO library_chunks
                        (chunk_id,doc_id,ordinal,section,page_number,line_start,line_end,content,content_sha256)
                        VALUES(?,?,?,?,?,?,?,?,?)''',
                        (chunk_id, doc_id, ordinal, part.get('section') or '', part.get('page_number'),
                         part['line_start'], part['line_end'], part['text'], chunk_hash))
                    connection.execute('INSERT INTO library_fts(chunk_id,content) VALUES(?,?)',
                                       (chunk_id, part['text']))
                if previous_version:
                    connection.execute("UPDATE library_documents SET status='superseded' WHERE doc_id=?",
                                       (previous_version['doc_id'],))
                connection.commit()
            except BaseException:
                if connection.in_transaction:
                    connection.rollback()
                raise
            finally:
                connection.close()
        semantic_status = self._embed_document(doc_id)
        return {'document_id': doc_id, 'duplicate': False, 'title': display_title,
                'scope': scope, 'status': 'indexed', 'chunks': len(chunks),
                'page_count': page_count, 'byte_size': len(content),
                'content_sha256': raw_hash, 'indexing': semantic_status,
                'semantic_status': semantic_status,
                'document_version': (previous_version['document_version'] + 1) if previous_version else 1}

    def retain_research_source(self, source: dict, *, conversation_id=None) -> dict:
        """Persist a short, user-selected research excerpt with trusted provenance."""
        from agent_core import no_secrets
        conversation_id = self._conversation(conversation_id)
        if (not isinstance(source, dict) or source.get('untrusted_content') is not True
                or not re.fullmatch(r'[0-9a-f]{32}', str(source.get('source_id','')))
                or not re.fullmatch(r'[0-9a-f]{64}', str(source.get('content_sha256','')))
                or not re.fullmatch(r'[a-z0-9][a-z0-9_-]{0,63}',str(source.get('provider_id','')))
                or not all(isinstance(source.get(key),str) and source[key]
                    for key in ('url','title','retrieved_at','content'))
                or hashlib.sha256(source['content'].encode('utf-8')).hexdigest()!=source['content_sha256']):
            raise AgentError('Only a verified public source from this research session can be retained.')
        try:
            parsed=urlsplit(source['url'])
            safe_url=(parsed.scheme=='https' and bool(parsed.hostname) and not parsed.username
                and not parsed.password and parsed.port in (None,443))
        except ValueError:
            safe_url=False
        if not safe_url:
            raise AgentError('Research source URL is not a public HTTPS reference.')
        excerpt=source.get('research_excerpt')
        if not isinstance(excerpt,str) or not excerpt.strip():
            excerpt=source['content'][:2000]
        excerpt=excerpt[:2000]
        no_secrets(excerpt)
        publication=source.get('publication_date')
        if publication is not None and (not isinstance(publication,str) or len(publication)>80):
            publication=None
        objective_hash=source.get('research_objective_sha256')
        if not isinstance(objective_hash,str) or not re.fullmatch(r'[0-9a-f]{64}',objective_hash):
            raise AgentError('Research retention is missing its verified task provenance.')
        raw=(f"# {source['title'][:150]}\n\nSource URL: {source['url']}\n"
             f"Provider: {source['provider_id']}\nRetrieved: {source['retrieved_at']}\n"
             f"Publication date: {publication or 'not reliably available'}\n"
             f"Source content SHA-256: {source['content_sha256']}\n\n"
             f"## Retrieved excerpt (untrusted reference)\n\n{excerpt}\n").encode('utf-8')
        imported=self.ingest('web-'+source['source_id']+'.md',raw,scope='global',title=source['title'][:150])
        try:
            with self._writer_lock('research-source-provenance'):
                connection=self.index._connect()
                try:
                    connection.execute('''INSERT INTO research_source_metadata
                        (doc_id,source_url,source_title,provider_id,retrieved_at,publication_date,
                         source_content_sha256,research_objective_sha256)
                        VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(doc_id) DO NOTHING''',
                        (imported['document_id'],source['url'],source['title'][:300],source['provider_id'],
                         source['retrieved_at'],publication,source['content_sha256'],objective_hash))
                    connection.commit()
                finally:
                    connection.close()
        except BaseException:
            if not imported.get('duplicate'):
                try:
                    self.remove(imported['document_id'])
                except BaseException:
                    # Preserve an incomplete indexed source for explicit reconciliation.
                    pass
            raise
        return {**imported,'scope':'global','retained_excerpt_chars':len(excerpt),
                'source_url':source['url'],'source_content_sha256':source['content_sha256']}

    def _embed_document(self, document_id: str) -> str:
        """Attach vectors in resumable bounded batches while keyword search remains available."""
        provider = self._provider()
        if provider is None:
            self._set_semantic_status(document_id, 'keyword_only')
            return 'keyword_only'
        try:
            descriptor = dict(provider.descriptor())
            if (descriptor.get('authorized') is not True or descriptor.get('available') is not True
                    or type(descriptor.get('dimensions')) is not int
                    or not 1 <= descriptor['dimensions'] <= 8192):
                raise AgentError('Embedding provider is incompatible or unauthorized.')
            connection = self.index._connect()
            try:
                rows = connection.execute('''SELECT chunk_id,content FROM library_chunks
                    WHERE doc_id=? AND (embedding IS NULL OR embedding_provider<>? OR embedding_model<>?
                        OR embedding_version<>? OR embedding_digest<>? OR embedding_dimensions<>?)
                    ORDER BY ordinal''', (document_id,descriptor['provider_id'],descriptor['model_id'],
                    descriptor['model_version'],descriptor['model_digest'],descriptor['dimensions'])).fetchall()
                embedded = connection.execute('''SELECT count(*) FROM library_chunks WHERE doc_id=?
                    AND embedding_provider=? AND embedding_model=? AND embedding_version=?
                    AND embedding_digest=? AND embedding_dimensions=? AND embedding IS NOT NULL''',
                    (document_id,descriptor['provider_id'],descriptor['model_id'],descriptor['model_version'],
                     descriptor['model_digest'],descriptor['dimensions'])).fetchone()[0]
            finally:
                connection.close()
            if not rows:
                if embedded == 0:
                    raise AgentError('Document has no passages to embed.')
                self._set_semantic_status(document_id,'ready')
                return 'ready'
            batch_limit=min(MAX_EMBEDDING_BATCH,getattr(provider,'MAX_BATCH',8))
            for offset in range(0, len(rows), batch_limit):
                if self._worker_stop:
                    raise _IngestionPaused
                batch = rows[offset:offset + batch_limit]
                vectors = provider.embed([row['content'] for row in batch])
                if len(vectors) != len(batch):
                    raise AgentError('Embedding provider returned an incomplete document batch.')
                packed = [_pack_vector(_vector(vector, descriptor['dimensions'])) for vector in vectors]
                with self._writer_lock('document-vector-index'):
                    connection = self.index._connect()
                    try:
                        connection.execute('BEGIN IMMEDIATE')
                        current = connection.execute('''SELECT status FROM library_documents
                            WHERE doc_id=? AND project_id=?''', (document_id,self.project.identity)).fetchone()
                        if current is None or current['status'] != 'current':
                            connection.rollback()
                            return 'removed'
                        connection.executemany('''UPDATE library_chunks SET embedding=?,embedding_dimensions=?,
                            embedding_provider=?,embedding_model=?,embedding_version=?,embedding_digest=?
                            WHERE chunk_id=? AND doc_id=?''', [
                            (sqlite3.Binary(vector),descriptor['dimensions'],descriptor['provider_id'],
                             descriptor['model_id'],descriptor['model_version'],descriptor['model_digest'],
                             row['chunk_id'],document_id) for row,vector in zip(batch,packed)])
                        connection.execute('UPDATE library_documents SET semantic_status=? WHERE doc_id=?',
                            ('semantic_indexing' if offset + len(batch) < len(rows) else 'ready',document_id))
                        connection.commit()
                    except BaseException:
                        if connection.in_transaction:
                            connection.rollback()
                        raise
                    finally:
                        connection.close()
                embedded += len(batch)
                try:
                    self._update_ingestion(document_id, embedded_chunks=embedded, stage='embedding')
                except sqlite3.Error:
                    pass
            return 'ready'
        except (AgentError, OSError, ValueError, KeyError, TypeError, RuntimeError, TimeoutError):
            connection = self.index._connect()
            try:
                embedded = connection.execute('''SELECT count(*) FROM library_chunks
                    WHERE doc_id=? AND embedding IS NOT NULL''',(document_id,)).fetchone()[0]
            finally:
                connection.close()
            status = 'keyword_only' if embedded == 0 else 'partial_embedding'
            self._set_semantic_status(document_id, status)
            return status

    def _set_semantic_status(self, document_id: str, status: str) -> None:
        with self._writer_lock('document-semantic-status'):
            connection = self.index._connect()
            try:
                connection.execute('''UPDATE library_documents SET semantic_status=?
                    WHERE doc_id=? AND project_id=? AND status='current' ''',
                    (status, document_id, self.project.identity))
                connection.commit()
            finally:
                connection.close()

    def reindex_semantic(self, *, limit=256) -> dict:
        """Re-embed bounded pending/old-version chunks without mixing vector spaces."""
        if type(limit) is not int or not 1 <= limit <= 256:
            raise AgentError('Semantic reindex limit must be between one and 256 passages.')
        connection = self.index._connect()
        try:
            provider = self._provider()
            if provider is None:
                return {'status': 'unavailable', 'indexed': 0, 'remaining': connection.execute(
                    "SELECT count(*) FROM library_documents WHERE semantic_status!='ready' AND status='current'").fetchone()[0]}
            descriptor = dict(provider.descriptor())
            rows = connection.execute('''SELECT c.chunk_id,c.content,c.content_sha256,d.doc_id
                FROM library_chunks c JOIN library_documents d ON d.doc_id=c.doc_id
                WHERE d.project_id=? AND d.status='current' AND
                (c.embedding IS NULL OR c.embedding_provider<>? OR c.embedding_model<>? OR
                 c.embedding_version<>? OR c.embedding_digest<>? OR c.embedding_dimensions<>?)
                ORDER BY d.created_at,c.ordinal LIMIT ?''',
                (self.project.identity, descriptor['provider_id'], descriptor['model_id'],
                 descriptor['model_version'], descriptor['model_digest'], descriptor['dimensions'], limit)).fetchall()
        finally:
            connection.close()
        if not rows:
            return {'status': 'ready', 'indexed': 0, 'remaining': 0}
        vectors = provider.embed([row['content'] for row in rows])
        if len(vectors) != len(rows):
            raise AgentError('Embedding provider returned an incomplete reindex batch.')
        packed = [_pack_vector(_vector(vector, descriptor['dimensions'])) for vector in vectors]
        with self._writer_lock('document-vector-reindex'):
            connection = self.index._connect()
            try:
                connection.execute('BEGIN IMMEDIATE')
                for row, vector in zip(rows, packed):
                    fresh = connection.execute('''SELECT c.content,c.content_sha256,d.status FROM library_chunks c
                        JOIN library_documents d ON d.doc_id=c.doc_id WHERE c.chunk_id=? AND d.project_id=?''',
                        (row['chunk_id'], self.project.identity)).fetchone()
                    if (fresh is None or fresh['status']!='current' or
                            hashlib.sha256(fresh['content'].encode()).hexdigest()!=fresh['content_sha256']):
                        raise AgentError('Knowledge changed during semantic reindex; transaction was halted.')
                    connection.execute('''UPDATE library_chunks SET embedding=?,embedding_dimensions=?,
                        embedding_provider=?,embedding_model=?,embedding_version=?,embedding_digest=? WHERE chunk_id=?''',
                        (sqlite3.Binary(vector),descriptor['dimensions'],descriptor['provider_id'],
                         descriptor['model_id'],descriptor['model_version'],descriptor['model_digest'],row['chunk_id']))
                for doc_id in {row['doc_id'] for row in rows}:
                    incomplete = connection.execute('''SELECT 1 FROM library_chunks WHERE doc_id=? AND
                        (embedding IS NULL OR embedding_provider<>? OR embedding_model<>? OR
                         embedding_version<>? OR embedding_digest<>? OR embedding_dimensions<>?) LIMIT 1''',
                        (doc_id,descriptor['provider_id'],descriptor['model_id'],descriptor['model_version'],
                         descriptor['model_digest'],descriptor['dimensions'])).fetchone()
                    connection.execute('UPDATE library_documents SET semantic_status=? WHERE doc_id=?',
                                       ('requires_reindex' if incomplete else 'ready',doc_id))
                connection.commit()
            except BaseException:
                if connection.in_transaction:
                    connection.rollback()
                raise
            finally:
                connection.close()
        return {'status':'partial' if len(rows)==limit else 'ready','indexed':len(rows)}

    def _writer_lock(self, owner):
        from knowledge_index import _writer_lock
        return _writer_lock(self.index.lock, owner + ':' + uuid.uuid4().hex)

    def list_documents(self, *, conversation_id=None) -> list[dict]:
        conversation_id = self._conversation(conversation_id)
        connection = self.index._connect()
        try:
            rows = connection.execute('''SELECT d.doc_id,d.title,d.original_name,d.extension,d.mime_type,
                d.content_sha256,d.byte_size,d.page_count,d.status,d.semantic_status,d.document_version,d.version_of,
                d.duplicate_of,d.scope,d.created_at,
                (SELECT count(*) FROM library_chunks c WHERE c.doc_id=d.doc_id) AS indexed_chunks,
                (SELECT count(*) FROM library_chunks c WHERE c.doc_id=d.doc_id AND c.embedding IS NOT NULL) AS embedded_chunks,
                (SELECT count(DISTINCT c.page_number) FROM library_chunks c WHERE c.doc_id=d.doc_id
                    AND c.page_number IS NOT NULL) AS pages_with_chunks
                FROM library_documents d WHERE d.project_id=? AND d.status='current'
                AND (d.scope='global' OR (d.scope='conversation' AND d.conversation_id=?))
                ORDER BY d.created_at DESC LIMIT ?''',
                (self.project.identity, conversation_id or '', MAX_LIBRARY_DOCUMENTS + 1)).fetchall()
            return [dict(row) for row in rows]
        finally:
            connection.close()

    def _resolve_document(self, reference=None, *, conversation_id=None):
        rows = self.list_documents(conversation_id=conversation_id)
        if not rows:
            raise AgentError('No accessible documents are present in this project and conversation scope.')
        if reference is None or (isinstance(reference,str) and reference.casefold().strip() in {'latest','most recent'}):
            return rows[0]
        if not isinstance(reference,str) or not reference.strip() or len(reference)>200:
            raise AgentError('Document reference must be an exact title, filename, or document identity.')
        value=reference.casefold().strip()
        matches=[row for row in rows if value in {row['doc_id'].casefold(),row['title'].casefold(),
            row['original_name'].casefold(),Path(row['original_name']).stem.casefold()}
            or value in row['title'].casefold() or value in row['original_name'].casefold()]
        if not matches:
            raise AgentError('No accessible document has that exact title, filename, or identity.')
        if len(matches)>1:
            raise AgentError('Document reference is ambiguous; use its exact filename or document identity.')
        return matches[0]

    def document_inventory(self, *, conversation_id=None, name_filter=None, limit=30, offset=0):
        if type(limit) is not int or not 1<=limit<=100 or type(offset) is not int or not 0<=offset<=10000:
            raise AgentError('Document inventory page bounds are invalid.')
        rows=self.list_documents(conversation_id=conversation_id)
        if name_filter:
            if not isinstance(name_filter,str) or len(name_filter)>80:
                raise AgentError('Document inventory filter is invalid.')
            needle=name_filter.casefold()
            rows=[row for row in rows if needle in row['title'].casefold()
                or needle in row['original_name'].casefold()]
        return {'total':len(rows),'offset':offset,'limit':limit,
            'totals':{'documents':len(rows),'pages':sum(row['page_count'] or 0 for row in rows),
                'pages_with_chunks':sum(row['pages_with_chunks'] or 0 for row in rows),
                'indexed_chunks':sum(row['indexed_chunks'] for row in rows),
                'embedded_chunks':sum(row['embedded_chunks'] for row in rows)},
            'documents':rows[offset:offset+limit],
            'latest_document':rows[0] if offset==0 and rows else None,
            'project_id':self.project.identity,'scope':'project and authorized conversation'}

    def inspect_document(self, reference=None, *, conversation_id=None):
        doc=self._resolve_document(reference,conversation_id=conversation_id)
        jobs={job['doc_id']:job for job in self.list_ingestion_jobs(conversation_id=conversation_id)}
        job=jobs.get(doc['doc_id'])
        return {'document':doc,'ingestion':job or {'state':'legacy_indexed',
            'coverage':'not recorded by the previous ingestion schema'},
            'distinction':'Indexed excerpts are not a claim that the complete document was read.'}

    def list_sections(self, reference=None, *, conversation_id=None, limit=40, offset=0):
        if type(limit) is not int or not 1<=limit<=100 or type(offset) is not int or not 0<=offset<=10000:
            raise AgentError('Document section page bounds are invalid.')
        doc=self._resolve_document(reference,conversation_id=conversation_id)
        connection=self.index._connect()
        try:
            total=connection.execute('SELECT count(DISTINCT section) FROM library_chunks WHERE doc_id=?',
                (doc['doc_id'],)).fetchone()[0]
            rows=connection.execute('''SELECT section,min(page_number) first_page,max(page_number) last_page,
                count(*) chunks FROM library_chunks WHERE doc_id=? GROUP BY section ORDER BY min(ordinal) LIMIT ? OFFSET ?''',
                (doc['doc_id'],limit,offset)).fetchall()
            job=connection.execute('SELECT state,processed_pages,total_pages FROM library_ingestion_jobs WHERE doc_id=?',
                (doc['doc_id'],)).fetchone()
            return {'document_id':doc['doc_id'],'title':doc['title'],'sections':[dict(row) for row in rows],
                'total_sections':total,'offset':offset,'limit':limit,
                'ingestion_complete':bool(job and job['state']=='ready'),
                'navigation_note':'Section labels come from extracted headings when available; otherwise pages or source blocks.'}
        finally:
            connection.close()

    def read_pages(self, reference, start_page, end_page, *, conversation_id=None):
        if (type(start_page) is not int or type(end_page) is not int or start_page<1
                or end_page<start_page or end_page-start_page>19):
            raise AgentError('Read at most 20 consecutive PDF pages per request.')
        doc=self._resolve_document(reference,conversation_id=conversation_id)
        if doc['extension']!='.pdf':
            raise AgentError('Page navigation is supported for PDF documents; use source sections or search for text files.')
        from pypdf import PdfReader
        connection=self.index._connect()
        try:
            row=connection.execute('''SELECT rowid,content_sha256,byte_size,page_count FROM library_documents
                WHERE doc_id=? AND project_id=? AND status='current' ''',
                (doc['doc_id'],self.project.identity)).fetchone()
            if row is None:
                raise AgentError('Document is no longer available in this scope.')
            with connection.blobopen('library_documents','original',row['rowid'],readonly=True) as original:
                digest=hashlib.sha256()
                while block:=original.read(1024*1024):
                    digest.update(block)
                if digest.hexdigest()!=row['content_sha256']:
                    raise AgentError('Original document failed its SHA-256 check.')
                original.seek(0)
                reader=PdfReader(original,strict=True)
                actual_pages=len(reader.pages)
                if (row['page_count'] is not None and actual_pages!=row['page_count']) or end_page>actual_pages:
                    raise AgentError('Requested page range is outside the verified document page count.')
                pages=[]; total_chars=0
                for number in range(start_page,end_page+1):
                    try:
                        content=reader.pages[number-1].extract_text() or ''
                    except Exception as exc:
                        pages.append({'page':number,'text':None,'status':'extraction_failed',
                                      'error':type(exc).__name__})
                        continue
                    total_chars+=len(content)
                    if total_chars>6000:
                        raise AgentError('That page range exceeds the 6,000-character read bound; request fewer pages.')
                    pages.append({'page':number,'text':content,'status':'read' if content.strip() else 'no_extractable_text'})
                return {'document_id':doc['doc_id'],'title':doc['title'],'filename':doc['original_name'],
                    'document_sha256':doc['content_sha256'],'pages':pages,
                    'range':{'start':start_page,'end':end_page},'source':'original PDF bytes hash verified',
                    'trust':'untrusted_reference_data'}
        finally:
            connection.close()

    def read_section(self, reference, section_reference, *, conversation_id=None, max_chars=5000):
        if (not isinstance(section_reference,str) or not section_reference.strip()
                or len(section_reference)>100 or type(max_chars) is not int or not 500<=max_chars<=5000):
            raise AgentError('Section reference or bounded read size is invalid.')
        doc=self._resolve_document(reference,conversation_id=conversation_id)
        sections=self.list_sections(reference,conversation_id=conversation_id,limit=100)
        target=section_reference.casefold().strip()
        words={'one':'1','two':'2','three':'3','four':'4','five':'5','six':'6','seven':'7',
               'eight':'8','nine':'9','ten':'10','eleven':'11','twelve':'12'}
        for word,digit in words.items():
            target=re.sub(r'\b'+word+r'\b',digit,target)
        def normalized(value):
            result=value.casefold()
            for word,digit in words.items():
                result=re.sub(r'\b'+word+r'\b',digit,result)
            return result
        matches=[item for item in sections['sections'] if target in normalized(item['section'])]
        if not matches:
            raise AgentError('That heading is not present in the indexed section map. Ask for its PDF page range or a document search instead.')
        connection=self.index._connect()
        try:
            rows=[]
            for item in matches:
                rows.extend(connection.execute('''SELECT chunk_id,ordinal,section,page_number,line_start,
                    line_end,content,content_sha256 FROM library_chunks WHERE doc_id=? AND section=? ORDER BY ordinal''',
                    (doc['doc_id'],item['section'])).fetchall())
            content_parts=[]; total_chars=0; retained_chars=0
            for row in rows:
                if hashlib.sha256(row['content'].encode()).hexdigest()!=row['content_sha256']:
                    raise AgentError('Section passage failed its stored content hash check.')
                total_chars+=len(row['content'])
                if retained_chars<max_chars:
                    remaining=max_chars-retained_chars
                    kept=row['content'][:remaining]
                    content_parts.append(kept);retained_chars+=len(kept)
            text='\n'.join(part for part in content_parts if part)
            pages=sorted({row['page_number'] for row in rows if row['page_number'] is not None})
            return {'document_id':doc['doc_id'],'title':doc['title'],'document_sha256':doc['content_sha256'],
                'section':matches[0]['section'],'page_start':min(pages) if pages else None,
                'page_end':max(pages) if pages else None,'text':text,'total_chunks':len(rows),
                'text_complete':total_chars<=max_chars,
                'omitted_chars':max(0,total_chars-max_chars),'trust':'untrusted_reference_data',
                'source':'hash-verified indexed passages'}
        finally:
            connection.close()

    def adjacent_chunks(self, reference, chunk_id, *, conversation_id=None, before=1, after=1):
        if (not isinstance(chunk_id,str) or not re.fullmatch(r'[a-f0-9]{64}',chunk_id)
                or type(before) is not int or type(after) is not int
                or not 0<=before<=2 or not 0<=after<=2 or before+after>3):
            raise AgentError('Adjacent chunk request is outside its bounded schema.')
        doc=self._resolve_document(reference,conversation_id=conversation_id)
        connection=self.index._connect()
        try:
            center=connection.execute('SELECT ordinal FROM library_chunks WHERE doc_id=? AND chunk_id=?',
                (doc['doc_id'],chunk_id)).fetchone()
            if center is None:
                raise AgentError('Chunk identity does not belong to the selected document.')
            rows=connection.execute('''SELECT chunk_id,ordinal,section,page_number,line_start,line_end,
                content,content_sha256 FROM library_chunks WHERE doc_id=? AND ordinal BETWEEN ? AND ?
                ORDER BY ordinal''',(doc['doc_id'],max(0,center['ordinal']-before),center['ordinal']+after)).fetchall()
            result=[]
            for row in rows:
                if hashlib.sha256(row['content'].encode()).hexdigest()!=row['content_sha256']:
                    raise AgentError('Adjacent document passage failed its stored content hash check.')
                result.append({**dict(row),'trust':'untrusted_reference_data',
                    'citation':self._citation({**dict(row),'doc_id':doc['doc_id'],'title':doc['title'],
                        'document_sha256':doc['content_sha256']})})
            return {'document_id':doc['doc_id'],'chunks':result,'source':'indexed adjacent chunks'}
        finally:
            connection.close()

    def remove(self, document_id: str, *, conversation_id=None) -> dict:
        if not isinstance(document_id, str) or not DOCUMENT_ID.fullmatch(document_id):
            raise AgentError('Document identity is invalid.')
        conversation_id = self._conversation(conversation_id)
        with self._writer_lock('document-remove'):
            connection = self.index._connect()
            try:
                row = connection.execute('SELECT * FROM library_documents WHERE doc_id=? AND project_id=?',
                    (document_id, self.project.identity)).fetchone()
                if row is None or row['status'] != 'current':
                    raise AgentError('Document is not present in this knowledge scope.')
                if row['scope'] == 'conversation' and row['conversation_id'] != conversation_id:
                    raise AgentError('Temporary document belongs to another conversation.')
                connection.execute('BEGIN IMMEDIATE')
                versions = [item['doc_id'] for item in connection.execute('''WITH RECURSIVE versions(doc_id) AS (
                    SELECT doc_id FROM library_documents WHERE doc_id=?
                    UNION SELECT d.version_of FROM library_documents d JOIN versions v ON d.doc_id=v.doc_id
                        WHERE d.version_of IS NOT NULL
                    UNION SELECT d.doc_id FROM library_documents d JOIN versions v ON d.version_of=v.doc_id
                    ) SELECT DISTINCT doc_id FROM versions''', (document_id,)).fetchall()]
                for version_id in versions:
                    connection.execute('DELETE FROM library_fts WHERE chunk_id IN '
                        '(SELECT chunk_id FROM library_chunks WHERE doc_id=?)', (version_id,))
                connection.executemany('DELETE FROM library_documents WHERE doc_id=?',
                                       [(version_id,) for version_id in versions])
                connection.commit()
                return {'removed': True, 'document_id': document_id,
                        'title': row['title'], 'scope': row['scope'],
                        'original_deleted': True, 'chunks_removed': True,
                        'versions_removed': len(versions)}
            except BaseException:
                if connection.in_transaction:
                    connection.rollback()
                raise
            finally:
                connection.close()

    def start_study(self, reference, *, conversation_id=None):
        """Create a persistent, source-version-bound section study plan."""
        doc=self._resolve_document(reference,conversation_id=conversation_id)
        connection=self.index._connect()
        try:
            prior=connection.execute('''SELECT study_id FROM library_studies WHERE doc_id=? AND source_sha256=?
                AND coalesce(conversation_id,'')=coalesce(?,'') ORDER BY updated_at DESC LIMIT 1''',
                (doc['doc_id'],doc['content_sha256'],conversation_id)).fetchone()
            if prior: return self.study_status(prior['study_id'],conversation_id=conversation_id)
            chunks=connection.execute('''SELECT chunk_id,section,page_number FROM library_chunks
                WHERE doc_id=? ORDER BY ordinal''',(doc['doc_id'],)).fetchall()
            if not chunks: raise AgentError('This document has no indexed passages to study.')
            groups=[]
            for chunk in chunks:
                label=(chunk['section'] or '').strip() or (f"Page {chunk['page_number']}" if chunk['page_number'] else 'Document')
                if not groups or groups[-1]['section']!=label: groups.append({'section':label,'chunks':[],'pages':[]})
                groups[-1]['chunks'].append(chunk['chunk_id'])
                if chunk['page_number'] is not None: groups[-1]['pages'].append(int(chunk['page_number']))
            now=_now(); study_id=uuid.uuid4().hex
            with self._writer_lock('create-document-study'):
                connection.execute('''INSERT INTO library_studies(study_id,project_id,doc_id,conversation_id,
                    source_sha256,state,total_sections,created_at,updated_at)
                    VALUES(?,?,?,?,?,'in_progress',?,?,?)''',
                    (study_id,self.project.identity,doc['doc_id'],conversation_id,doc['content_sha256'],len(groups),now,now))
                connection.executemany('''INSERT INTO library_study_sections(study_id,ordinal,section,page_start,
                    page_end,source_chunks_json,state,updated_at) VALUES(?,?,?,?,?,?,'pending',?)''',
                    [(study_id,i,g['section'],min(g['pages']) if g['pages'] else None,
                      max(g['pages']) if g['pages'] else None,json.dumps(g['chunks']),now)
                     for i,g in enumerate(groups)])
                connection.commit()
            return self.study_status(study_id,conversation_id=conversation_id)
        finally: connection.close()

    def study_status(self, study_id, *, conversation_id=None):
        connection=self.index._connect()
        try:
            row=connection.execute('''SELECT s.*,d.title,d.original_name FROM library_studies s
                JOIN library_documents d ON d.doc_id=s.doc_id WHERE s.study_id=? AND s.project_id=?
                AND (s.conversation_id IS NULL OR s.conversation_id=?)''',
                (study_id,self.project.identity,conversation_id)).fetchone()
            if row is None: raise AgentError('No accessible study session has that identity.')
            return {'study_id':row['study_id'],'document_id':row['doc_id'],'document':row['title'],
                'filename':row['original_name'],'state':row['state'],'total_sections':row['total_sections'],
                'completed_sections':row['completed_sections'],'source_sha256':row['source_sha256']}
        finally: connection.close()

    def next_study_section(self, study_id, *, conversation_id=None, max_chars=6000):
        status=self.study_status(study_id,conversation_id=conversation_id)
        connection=self.index._connect()
        try:
            row=connection.execute('''SELECT * FROM library_study_sections WHERE study_id=?
                AND state!='complete' ORDER BY ordinal LIMIT 1''',(study_id,)).fetchone()
            if row is None: return {**status,'section':None,'complete':True}
            ids=json.loads(row['source_chunks_json']); marks=','.join('?' for _ in ids)
            chunks=connection.execute(f'''SELECT chunk_id,section,page_number,content,content_sha256
                FROM library_chunks WHERE doc_id=? AND chunk_id IN ({marks}) ORDER BY ordinal''',
                (status['document_id'],*ids)).fetchall()
            passages=[]; used=0
            for chunk in chunks:
                if hashlib.sha256(chunk['content'].encode()).hexdigest()!=chunk['content_sha256']:
                    raise AgentError('Study source passage failed integrity verification.')
                part=chunk['content'][:max(0,max_chars-used)]; used+=len(part)
                passages.append({'chunk_id':chunk['chunk_id'],'page':chunk['page_number'],
                    'section':chunk['section'],'text':part,'truncated':len(part)<len(chunk['content'])})
                if used>=max_chars: break
            with self._writer_lock('start-study-section'):
                connection.execute('''UPDATE library_study_sections SET state='in_progress',updated_at=?
                    WHERE study_id=? AND ordinal=?''',(_now(),study_id,row['ordinal']))
            return {**status,'section':{'ordinal':row['ordinal'],'label':row['section'],
                'page_start':row['page_start'],'page_end':row['page_end'],'passages':passages,
                'complete_source':len(passages)==len(chunks) and all(not p['truncated'] for p in passages)}}
        finally: connection.close()

    def record_study_section(self, study_id, ordinal, *, summary, concepts=(), examples=(), lessons=(),
                             conversation_id=None):
        """Record interpretations with source provenance as unverified candidate lessons."""
        def bounded(values,limit):
            if not isinstance(values,(list,tuple)): raise AgentError('Study annotations must be lists.')
            return [str(v)[:limit] for v in values[:40] if str(v).strip()]
        summary=str(summary)[:4000]
        if not summary.strip(): raise AgentError('A nonempty section summary is required.')
        connection=self.index._connect()
        try:
            with self._writer_lock('record-study-section'):
                row=connection.execute('''SELECT s.doc_id,s.state,s.source_sha256,d.content_sha256 FROM library_studies s
                    JOIN library_documents d ON d.doc_id=s.doc_id WHERE s.study_id=? AND s.project_id=?
                    AND (s.conversation_id IS NULL OR s.conversation_id=?)''',
                    (study_id,self.project.identity,conversation_id)).fetchone()
                if row is None or row['source_sha256']!=row['content_sha256']:
                    raise AgentError('Study session is unavailable or its source version changed.')
                sec=connection.execute('''SELECT state,source_chunks_json FROM library_study_sections
                    WHERE study_id=? AND ordinal=?''',(study_id,int(ordinal))).fetchone()
                if sec is None: raise AgentError('Study section does not exist.')
                lessons_value=[{'text':v,'status':'unverified_candidate','executable':False}
                    for v in bounded(lessons,500)]
                connection.execute('''UPDATE library_study_sections SET state='complete',summary=?,concepts_json=?,
                    examples_json=?,lessons_json=?,updated_at=? WHERE study_id=? AND ordinal=?''',
                    (summary,json.dumps(bounded(concepts,300)),json.dumps(bounded(examples,500)),
                     json.dumps(lessons_value),_now(),study_id,int(ordinal)))
                if sec['state']!='complete':
                    connection.execute('''UPDATE library_studies SET completed_sections=completed_sections+1,
                        updated_at=? WHERE study_id=?''',(_now(),study_id))
                counts=connection.execute('SELECT total_sections,completed_sections FROM library_studies WHERE study_id=?',
                    (study_id,)).fetchone()
                state='complete' if counts['completed_sections']>=counts['total_sections'] else 'in_progress'
                connection.execute('UPDATE library_studies SET state=?,updated_at=? WHERE study_id=?',
                    (state,_now(),study_id)); connection.commit()
            return self.study_status(study_id,conversation_id=conversation_id)
        finally: connection.close()

    def search(self, query: str, *, conversation_id=None, limit=MAX_RETRIEVAL_RESULTS,
               provider=None, cancel_event=None, retrieval_mode='hybrid', document_id=None) -> dict:
        if not isinstance(query, str) or not query.strip() or len(query.encode('utf-8')) > 2000:
            raise AgentError('Knowledge query must contain 1 to 2000 UTF-8 bytes.')
        if type(limit) is not int or not 1 <= limit <= MAX_RETRIEVAL_RESULTS:
            raise AgentError('Knowledge result limit is outside the supported range.')
        if retrieval_mode not in {'keyword', 'semantic', 'hybrid'}:
            raise AgentError('Knowledge retrieval mode is unsupported.')
        from agent_core import no_secrets
        no_secrets(query)
        conversation_id = self._conversation(conversation_id)
        if document_id is not None:
            if not isinstance(document_id,str) or not DOCUMENT_ID.fullmatch(document_id):
                raise AgentError('Knowledge document identity is invalid.')
            if document_id not in {row['doc_id'] for row in self.list_documents(conversation_id=conversation_id)}:
                raise AgentError('Knowledge document is unavailable in this project and conversation scope.')
        terms = [term for term in dict.fromkeys(re.findall(r'[A-Za-z0-9_]{2,}', query.casefold()))
                 if term not in _FTS_STOPWORDS][:24]
        expression = (' OR '.join('"' + term.replace('"', '""') + '"' for term in terms)
                      if terms else None)
        provider = None if retrieval_mode == 'keyword' else (provider or self._provider())
        descriptor = None
        query_vector = None
        semantic_status = 'unavailable_keyword_fallback'
        if provider is not None:
            try:
                descriptor = dict(provider.descriptor())
                if descriptor.get('authorized') is not True or descriptor.get('available') is not True:
                    raise AgentError('Embedding model is unavailable or unauthorized.')
                query_vector = _vector(provider.embed([query], cancel_event=cancel_event)[0],
                                       descriptor['dimensions'])
                semantic_status = 'available'
            except (AgentError, OSError, ValueError, KeyError, TypeError, IndexError, RuntimeError):
                if cancel_event is not None and cancel_event.is_set():
                    raise AgentError('Knowledge retrieval was cancelled.')
                query_vector = None
                descriptor = None
                semantic_status = 'provider_failed_keyword_fallback'
        connection = self.index._connect()
        try:
            document_count = connection.execute('''SELECT count(*) FROM library_documents
                WHERE project_id=? AND status='current' AND
                (scope='global' OR (scope='conversation' AND conversation_id=?))''',
                (self.project.identity, conversation_id or '')).fetchone()[0]
            keyword_rows = connection.execute('''SELECT c.chunk_id,c.ordinal,c.section,c.page_number,
                c.line_start,c.line_end,c.content,c.content_sha256,d.doc_id,d.title,
                d.original_name,d.extension,d.content_sha256 AS document_sha256,d.scope,
                d.semantic_status,d.created_at,bm25(library_fts) AS rank
                FROM library_fts JOIN library_chunks c ON c.chunk_id=library_fts.chunk_id
                JOIN library_documents d ON d.doc_id=c.doc_id
                WHERE library_fts MATCH ? AND d.project_id=? AND d.status='current'
                AND (d.scope='global' OR (d.scope='conversation' AND d.conversation_id=?))
                AND (? IS NULL OR d.doc_id=?)
                ORDER BY rank,c.chunk_id LIMIT 40''',
                (expression or '"__no_fts_terms__"', self.project.identity,
                 conversation_id or '',document_id,document_id)).fetchall() if expression and retrieval_mode != 'semantic' else []
            if len(terms) >= 3 and keyword_rows:
                required_matches = 2
                keyword_rows = [row for row in keyword_rows
                    if len(set(terms) & set(re.findall(r'[a-z0-9_]{2,}', row['content'].casefold())))
                    >= required_matches]
            semantic_rows = []
            if query_vector is not None:
                stored = connection.execute('''SELECT c.chunk_id,c.ordinal,c.section,c.page_number,
                    c.line_start,c.line_end,c.content,c.content_sha256,d.doc_id,d.title,
                    d.original_name,d.extension,d.content_sha256 AS document_sha256,d.scope,
                    d.semantic_status,d.created_at,c.embedding,c.embedding_dimensions
                    FROM library_chunks c JOIN library_documents d ON d.doc_id=c.doc_id
                    WHERE d.project_id=? AND d.status='current' AND c.embedding IS NOT NULL
                    AND c.embedding_provider=? AND c.embedding_model=? AND c.embedding_version=?
                    AND c.embedding_digest=? AND c.embedding_dimensions=?
                    AND (d.scope='global' OR (d.scope='conversation' AND d.conversation_id=?))
                    AND (? IS NULL OR d.doc_id=?)
                    ORDER BY c.chunk_id LIMIT ?''',
                    (self.project.identity, descriptor['provider_id'], descriptor['model_id'],
                     descriptor['model_version'], descriptor['model_digest'], descriptor['dimensions'],
                     conversation_id or '',document_id,document_id, MAX_LIBRARY_CHUNKS + 1)).fetchall()
                if len(stored) > MAX_LIBRARY_CHUNKS:
                    raise AgentError('Semantic candidate set exceeds its configured bound.')
                scored = []
                for row in stored:
                    vector = _unpack_vector(row['embedding'], row['embedding_dimensions'])
                    similarity = sum(left * right for left, right in zip(query_vector, vector))
                    if similarity >= MIN_SEMANTIC_SIMILARITY:
                        scored.append((similarity, row))
                semantic_rows = [row for _, row in sorted(scored,
                    key=lambda pair: (-pair[0], pair[1]['chunk_id']))[:40]]
                if not stored:
                    semantic_status = 'reindex_required_keyword_fallback'
            ranked: dict[str, tuple[sqlite3.Row, float, set[str]]] = {}
            for method, rows in (('keyword', keyword_rows), ('semantic', semantic_rows)):
                for rank, row in enumerate(rows, 1):
                    previous = ranked.get(row['chunk_id'], (row, 0.0, set()))
                    previous[2].add(method)
                    ranked[row['chunk_id']] = (row, previous[1] + 1.0 / (RRF_K + rank), previous[2])
            ordered = sorted(ranked.values(), key=lambda item: (-item[1],item[0]['doc_id'],item[0]['ordinal']))
            results=[]; per_document={}
            for row, score, methods in ordered:
                if document_id is None and per_document.get(row['doc_id'], 0) >= 2:
                    continue
                if hashlib.sha256(row['content'].encode('utf-8')).hexdigest() != row['content_sha256']:
                    raise AgentError('Retrieved knowledge passage failed its stored content hash check.')
                result={key: row[key] for key in ('chunk_id','ordinal','section','page_number',
                    'line_start','line_end','content','content_sha256','doc_id','title',
                    'original_name','extension','document_sha256','scope','semantic_status','created_at')}
                result.update({'trust':'untrusted_reference_data','evidence_state':'indexed_content_hash_verified',
                    'rank_method':'reciprocal_rank_fusion' if len(methods)>1 else next(iter(methods)),
                    'retrieval_methods':sorted(methods),'relevance':round(score,6),
                    'citation':self._citation(result)})
                source=connection.execute('''SELECT source_url,provider_id,retrieved_at,
                    publication_date,source_content_sha256 FROM research_source_metadata WHERE doc_id=?''',
                    (row['doc_id'],)).fetchone()
                if source:
                    result['citation'].update({'source_url':source['source_url'],
                        'source_provider':source['provider_id'],'source_retrieved_at':source['retrieved_at'],
                        'source_publication_date':source['publication_date'],
                        'source_content_sha256':source['source_content_sha256']})
                results.append(result);per_document[row['doc_id']]=per_document.get(row['doc_id'],0)+1
                if len(results)>=limit:break
            strategy = ('hybrid' if keyword_rows and semantic_rows else
                        'keyword' if keyword_rows else 'semantic' if semantic_rows else 'keyword')
            if query_vector is not None and semantic_rows:
                semantic_status = 'ready'
            return {'results':results,'strategy':strategy,'semantic_status':semantic_status,
                    'available_documents':document_count,
                    'embedding_identity': descriptor if descriptor is not None else None}
        finally:
            connection.close()

    @staticmethod
    def _citation(row):
        location = ('page ' + str(row['page_number']) if row.get('page_number') else
                    row.get('section') or ('lines ' + str(row.get('line_start')) + '-' + str(row.get('line_end'))))
        return {'document_id':row['doc_id'],'title':row['title'],'location':location,
                'chunk_id':row['chunk_id'],'chunk_id_short':row['chunk_id'][:12],
                'document_sha256':row['document_sha256']}

    def verify(self) -> dict:
        connection=self.index._connect()
        try:
            integrity=connection.execute('PRAGMA integrity_check').fetchone()[0]
            try:
                connection.execute("INSERT INTO library_fts(library_fts) VALUES('integrity-check')")
                fts_integrity='ok'
            except sqlite3.DatabaseError:
                fts_integrity='failed'
            # SQLite has no built-in SHA-256; verify originals and every extracted chunk in Python.
            rows=connection.execute('SELECT * FROM library_documents WHERE project_id=?',
                                    (self.project.identity,)).fetchall()
            checks=[]; vectors_checked=0; invalid_vectors=0
            for row in rows:
                raw=bytes(row['original'])
                valid=len(raw)==row['byte_size'] and hashlib.sha256(raw).hexdigest()==row['content_sha256']
                chunks=connection.execute('''SELECT content,content_sha256,embedding,embedding_dimensions,
                    embedding_provider,embedding_model,embedding_version,embedding_digest
                    FROM library_chunks WHERE doc_id=?''',
                                          (row['doc_id'],)).fetchall()
                for item in chunks:
                    valid=valid and hashlib.sha256(item['content'].encode()).hexdigest()==item['content_sha256']
                    if item['embedding'] is not None:
                        vectors_checked += 1
                        try:
                            vector = _unpack_vector(item['embedding'], item['embedding_dimensions'])
                            norm = sum(value * value for value in vector) ** 0.5
                            identity_valid = all(item[name] for name in
                                ('embedding_provider','embedding_model','embedding_version','embedding_digest'))
                            vector_valid = identity_valid and abs(norm-1.0) <= 1e-3
                        except (AgentError, TypeError, ValueError, OverflowError):
                            vector_valid = False
                        if not vector_valid:
                            invalid_vectors += 1
                            valid = False
                if row['semantic_status']=='ready' and any(item['embedding'] is None for item in chunks):
                    valid = False
                checks.append(valid)
            research_rows=connection.execute('''SELECT source_url,source_content_sha256,
                research_objective_sha256 FROM research_source_metadata''').fetchall()
            invalid_research=0
            for item in research_rows:
                try:
                    parsed=urlsplit(item['source_url'])
                    valid=(parsed.scheme=='https' and bool(parsed.hostname)
                        and not parsed.username and not parsed.password and parsed.port in (None,443)
                        and re.fullmatch(r'[0-9a-f]{64}',item['source_content_sha256']) is not None
                        and re.fullmatch(r'[0-9a-f]{64}',item['research_objective_sha256']) is not None)
                except (TypeError,ValueError):
                    valid=False
                if not valid: invalid_research+=1
            return {'valid':integrity=='ok' and fts_integrity=='ok' and all(checks)
                        and invalid_research==0,
                    'integrity':integrity,'fts_integrity':fts_integrity,
                    'documents_checked':len(rows),'chunks_checked':connection.execute(
                    'SELECT count(*) FROM library_chunks c JOIN library_documents d ON d.doc_id=c.doc_id WHERE d.project_id=?',
                    (self.project.identity,)).fetchone()[0],
                    'embeddings_checked':vectors_checked,'invalid_embeddings':invalid_vectors,
                'schema_version':5,'research_sources_checked':len(research_rows),
                'invalid_research_sources':invalid_research}
        finally:
            connection.close()

    def status(self) -> dict:
        connection = self.index._connect()
        try:
            rows = connection.execute('''SELECT semantic_status,count(*) AS count FROM library_documents
                WHERE project_id=? AND status='current' GROUP BY semantic_status''',
                (self.project.identity,)).fetchall()
            provider = self._provider()
            return {'project_id':self.project.identity,'schema_version':5,
                'document_count':sum(row['count'] for row in rows),
                'semantic_states':{row['semantic_status']:row['count'] for row in rows},
                'retrieval':'hybrid' if provider else 'keyword',
                'embedding_model':provider.descriptor() if provider else None,
                'skill_library':'separate_project_scoped_registry'}
        finally:
            connection.close()

    def _skill_backup_entries(self) -> dict[str, bytes]:
        """Collect the project SkillLibrary registry and its verified run evidence."""
        from skill_library import SkillLibrary, MAX_REGISTRY_BYTES
        skills = SkillLibrary(self.project)
        registry = skills._load_registry()
        assets = {}
        if skills.registry_path.is_file():
            raw_registry = skills.registry_path.read_bytes()
            if len(raw_registry) > MAX_REGISTRY_BYTES:
                raise AgentError('Skill registry is too large for a bounded knowledge backup.')
            assets['skills/registry.json'] = raw_registry
        recovery = skills.recovery_dir
        if recovery.exists():
            if _is_reparse(recovery) or recovery.resolve().parent != skills.store.resolve():
                raise AgentError('Skill recovery directory is linked or outside private storage.')
            for artifact in recovery.iterdir():
                if _is_reparse(artifact) or not artifact.is_file() or artifact.stat().st_size > 2*1024*1024:
                    raise AgentError('Skill recovery contains an unsupported or oversized artifact.')
                from agent_core import no_secrets
                content=artifact.read_bytes()
                try:
                    no_secrets(content.decode('utf-8'))
                except UnicodeError as exc:
                    raise AgentError('Binary SkillLibrary recovery artifacts are not portable.') from exc
                assets['skills/recovery/'+artifact.name]=content
        if not assets:
            return {}
        evidence_root = (self.project.root / '.local' / 'orchestration' / 'runs').resolve()
        references = []

        def collect(value):
            if isinstance(value, dict):
                if {'kind','uri','sha256'} <= set(value):
                    references.append(value)
                for child in value.values():
                    collect(child)
            elif isinstance(value, list):
                for child in value:
                    collect(child)

        collect(registry)
        for reference in references:
            run_id = reference.get('run_id')
            if not isinstance(run_id, str):
                raise AgentError('Skill evidence is missing its source run identity.')
            verified = skills._verify_reference(reference, run_id)
            source = Path(verified['uri'])
            if not source.is_relative_to(evidence_root):
                raise AgentError('Skill evidence cannot be portably backed up outside orchestration runs.')
            entry = 'evidence/' + source.relative_to(self.project.root).as_posix()
            if entry not in assets:
                content = source.read_bytes()
                if len(content) > 2 * 1024 * 1024:
                    raise AgentError('A skill evidence artifact exceeds its two MiB backup limit.')
                from agent_core import no_secrets
                try:
                    no_secrets(content.decode('utf-8'))
                except UnicodeError as exc:
                    raise AgentError('Binary skill evidence is excluded from portable text backups.') from exc
                assets[entry] = content
                if len(assets) > 1000 or sum(map(len, assets.values())) > MAX_BACKUP_BYTES:
                    raise AgentError('Skill evidence exceeds the bounded portable backup capacity.')
        return assets

    def close(self) -> None:
        with self._job_condition:
            self._worker_stop = True
            self._job_condition.notify_all()
            worker = self._worker
        if worker is not None and worker.is_alive():
            worker.join(timeout=3.0)
        provider = self._embedding_provider
        if (worker is None or not worker.is_alive()) and provider is not None and callable(getattr(provider, 'close', None)):
            provider.close()

    def backup(self) -> dict:
        """Create a create-new SQLite snapshot; it includes imported original bytes."""
        directory = self.index.directory / 'backups'
        self.index._check_private(create=True)
        if directory.exists() and (_is_reparse(directory) or directory.resolve().parent != self.index.directory.resolve()):
            raise AgentError('Knowledge backup directory is linked or outside private storage.')
        directory.mkdir(exist_ok=True)
        archive = directory / ('knowledge-' + uuid.uuid4().hex + '.zip')
        temp_db = directory / ('.snapshot-' + uuid.uuid4().hex + '.sqlite3')
        skill_assets = self._skill_backup_entries()
        try:
            with self._writer_lock('knowledge-backup'):
                source = self.index._connect()
                target = sqlite3.connect(str(temp_db))
                try:
                    source.backup(target)
                    integrity = target.execute('PRAGMA integrity_check').fetchone()[0]
                    if integrity != 'ok':
                        raise AgentError('Knowledge backup snapshot failed SQLite integrity verification.')
                finally:
                    target.close(); source.close()
            size = temp_db.stat().st_size
            if size + sum(map(len, skill_assets.values())) > MAX_BACKUP_BYTES:
                raise AgentError('Knowledge database exceeds the 256 MiB backup limit.')
            raw = temp_db.read_bytes()
            identity = None
            embedding = None
            embedding_identities = []
            check = sqlite3.connect(str(temp_db))
            try:
                identity_row = check.execute("SELECT value FROM metadata WHERE key='project_id'").fetchone()
                embedding_row = check.execute("SELECT value FROM metadata WHERE key='embedding_identity'").fetchone()
                identity = identity_row[0] if identity_row else None
                embedding = json.loads(embedding_row[0]) if embedding_row else None
                embedding_identities = [dict(zip(('provider_id','model_id','model_version',
                    'model_digest','dimensions'), row)) for row in check.execute('''SELECT DISTINCT
                    embedding_provider,embedding_model,embedding_version,embedding_digest,embedding_dimensions
                    FROM library_chunks WHERE embedding IS NOT NULL ORDER BY embedding_provider,
                    embedding_model,embedding_version,embedding_digest,embedding_dimensions''')]
            finally:
                check.close()
            asset_manifest = {name:{'sha256':hashlib.sha256(value).hexdigest(),'bytes':len(value)}
                for name,value in skill_assets.items()}
            manifest = {'schema_version': 2, 'project_id': self.project.identity,
                'created_at': _now(), 'database_sha256': hashlib.sha256(raw).hexdigest(),
                'database_bytes': size, 'sqlite_integrity': 'ok',
                'knowledge_schema_version': 5, 'embedding_identity': embedding,
                'embedding_identities':embedding_identities,
                'portable_files':asset_manifest,
                'skill_registry_included':'skills/registry.json' in skill_assets,
                'skill_evidence_files':sum(name.startswith('evidence/') for name in skill_assets)}
            with zipfile.ZipFile(archive, 'x', compression=zipfile.ZIP_DEFLATED) as bundle:
                bundle.writestr('manifest.json', json.dumps(manifest, sort_keys=True).encode('utf-8'))
                bundle.write(temp_db, 'index.sqlite3')
                for name, content in skill_assets.items():
                    bundle.writestr(name, content)
            return {'backup': str(archive), 'manifest': manifest,
                    'integrity_verified': True,
                    'skill_registry_included':manifest['skill_registry_included'],
                    'skill_evidence_files':manifest['skill_evidence_files']}
        except BaseException:
            archive.unlink(missing_ok=True)
            raise
        finally:
            temp_db.unlink(missing_ok=True)

    def restore(self, archive_path: str | Path) -> dict:
        """Restore only into an empty exact-project index; never overwrite an active library."""
        archive = Path(archive_path)
        backup_directory = self.index.directory / 'backups'
        if (not backup_directory.exists() or _is_reparse(backup_directory)
                or backup_directory.resolve().parent != self.index.directory.resolve()
                or _is_reparse(archive)
                or not archive.resolve(strict=True).is_relative_to(backup_directory.resolve())):
            raise AgentError('Restore accepts only a backup in this project knowledge backup directory.')
        if self.index.database.exists():
            if self._has_indexed_data():
                raise AgentError('Restore refused because the destination knowledge library contains data.')
        restore_journal = self.index.directory / 'restore.pending.json'
        if restore_journal.exists():
            raise AgentError('A previous knowledge restore is incomplete; preserve its journal and reconcile it first.')
        if archive.stat().st_size > MAX_BACKUP_BYTES:
            raise AgentError('Knowledge backup exceeds the configured restore limit.')
        self.index._check_private(create=True)
        temporary = self.index.directory / ('.restore-' + uuid.uuid4().hex + '.sqlite3')
        stage = self.index.directory / ('.restore-assets-' + uuid.uuid4().hex)
        skill_registry = self.project.root / '.local' / 'skills' / 'registry.json'
        copied_assets = []
        try:
            with zipfile.ZipFile(archive, 'r') as bundle:
                names = bundle.namelist()
                if (len(names) > 1002 or len(set(names)) != len(names)
                        or not {'index.sqlite3','manifest.json'} <= set(names)):
                    raise AgentError('Knowledge backup contains unsupported entries.')
                manifest = json.loads(bundle.read('manifest.json'))
                info = bundle.getinfo('index.sqlite3')
                if info.file_size > MAX_BACKUP_BYTES:
                    raise AgentError('Knowledge backup database exceeds its size limit.')
                raw = bundle.read('index.sqlite3')
                asset_manifest = manifest.get('portable_files', {})
                if not isinstance(asset_manifest, dict) or len(asset_manifest) > 1000:
                    raise AgentError('Knowledge backup asset manifest is invalid.')
                if set(names) != {'index.sqlite3','manifest.json',*asset_manifest.keys()}:
                    raise AgentError('Knowledge backup files do not match their integrity manifest.')
                total_assets = 0
                asset_data = {}
                for name, details in asset_manifest.items():
                    parts = Path(name.replace('\\','/')).parts
                    if (not isinstance(name,str) or name.startswith('/') or '..' in parts
                    or not (name == 'skills/registry.json'
                        or name.startswith('skills/recovery/')
                        or name.startswith('evidence/.local/orchestration/runs/'))
                            or not isinstance(details,dict) or set(details)!={'sha256','bytes'}
                            or not re.fullmatch(r'[0-9a-f]{64}',str(details.get('sha256','')))
                            or type(details.get('bytes')) is not int or not 0 <= details['bytes'] <= 2*1024*1024):
                        raise AgentError('Knowledge backup contains an unsafe or malformed asset entry.')
                    content = bundle.read(name)
                    if len(content) != details['bytes'] or hashlib.sha256(content).hexdigest() != details['sha256']:
                        raise AgentError('Knowledge backup asset failed its content hash check.')
                    total_assets += len(content)
                    asset_data[name] = content
                if total_assets + len(raw) > MAX_BACKUP_BYTES:
                    raise AgentError('Knowledge backup exceeds its total uncompressed size limit.')
            if (manifest.get('schema_version') not in (1,2) or manifest.get('project_id') != self.project.identity
                    or manifest.get('database_bytes') != len(raw)
                    or hashlib.sha256(raw).hexdigest() != manifest.get('database_sha256')):
                raise AgentError('Knowledge backup identity or integrity verification failed.')
            if asset_data:
                from skill_library import SkillLibrary
                skills = SkillLibrary(self.project)
                if (skills.registry_path.exists() or skills.pending_path.exists() or skills.lock_path.exists()):
                    raise AgentError('Restore refused because the destination SkillLibrary contains or is reconciling data.')
                for name in asset_data:
                    if name == 'skills/registry.json':
                        target=skills.registry_path
                    elif name.startswith('skills/recovery/'):
                        target=skills.recovery_dir.joinpath(*Path(name[len('skills/recovery/'):]).parts)
                    else:
                        target=self.project.root.joinpath(*Path(name[len('evidence/'):]).parts)
                    if target.exists() or _is_reparse(target):
                        raise AgentError('Restore refused because a skill or evidence destination already exists.')
                    if not target.resolve().is_relative_to(self.project.root):
                        raise AgentError('Restore asset path escapes the destination project.')
            with open(temporary, 'xb') as handle:
                handle.write(raw); handle.flush(); os.fsync(handle.fileno())
            check = sqlite3.connect(str(temporary))
            check.row_factory = sqlite3.Row
            try:
                if check.execute('PRAGMA integrity_check').fetchone()[0] != 'ok':
                    raise AgentError('Knowledge restore snapshot failed SQLite integrity verification.')
                row = check.execute("SELECT value FROM metadata WHERE key='project_id'").fetchone()
                if not row or row[0] != self.project.identity:
                    raise AgentError('Restored knowledge database belongs to another project.')
                check.execute("INSERT INTO library_fts(library_fts) VALUES('integrity-check')")
                for document in check.execute('SELECT * FROM library_documents WHERE project_id=?',
                        (self.project.identity,)).fetchall():
                    original = bytes(document['original'])
                    if (len(original) != document['byte_size'] or
                            hashlib.sha256(original).hexdigest() != document['content_sha256']):
                        raise AgentError('Restored document original failed its content hash check.')
                    chunks = check.execute('''SELECT content,content_sha256,embedding,embedding_dimensions,
                        embedding_provider,embedding_model,embedding_version,embedding_digest
                        FROM library_chunks WHERE doc_id=?''',(document['doc_id'],)).fetchall()
                    for chunk in chunks:
                        if hashlib.sha256(chunk['content'].encode()).hexdigest() != chunk['content_sha256']:
                            raise AgentError('Restored passage failed its content hash check.')
                        if chunk['embedding'] is not None:
                            vector = _unpack_vector(chunk['embedding'],chunk['embedding_dimensions'])
                            if (abs(sum(value*value for value in vector)**0.5-1.0)>1e-3 or
                                    any(not chunk[name] for name in ('embedding_provider','embedding_model',
                                        'embedding_version','embedding_digest'))):
                                raise AgentError('Restored vector identity or normalization is invalid.')
                        elif ('semantic_status' in document.keys()
                              and document['semantic_status']=='ready'):
                            raise AgentError('Restored semantic document is missing an embedding.')
            finally:
                check.close()
            if self.index.database.exists() and self._has_indexed_data():
                raise AgentError('Knowledge data appeared during restore; destination was preserved.')
            if asset_data:
                atomic_write(restore_journal, json.dumps({'schema_version':1,
                    'project_id':self.project.identity,'stage':str(stage),
                    'assets':sorted(asset_data),'database_sha256':manifest['database_sha256']},
                    sort_keys=True).encode('utf-8'))
                stage.mkdir()
                for name, content in asset_data.items():
                    staged = stage.joinpath(*Path(name).parts)
                    staged.parent.mkdir(parents=True, exist_ok=True)
                    with open(staged, 'xb') as handle:
                        handle.write(content); handle.flush(); os.fsync(handle.fileno())
                from skill_library import SkillLibrary
                skills = SkillLibrary(self.project)
                for name in sorted(asset_data, key=lambda value: value == 'skills/registry.json'):
                    if name == 'skills/registry.json':
                        target=skills.registry_path
                    elif name.startswith('skills/recovery/'):
                        target=skills.recovery_dir.joinpath(*Path(name[len('skills/recovery/'):]).parts)
                    else:
                        target=self.project.root.joinpath(*Path(name[len('evidence/'):]).parts)
                    target.parent.mkdir(parents=True, exist_ok=True)
                    if target.exists() or _is_reparse(target):
                        raise AgentError('Restore destination appeared during commit; recovery journal preserved.')
                    with open(target, 'xb') as handle:
                        handle.write(asset_data[name]); handle.flush(); os.fsync(handle.fileno())
                    copied_assets.append(str(target))
                if skill_registry.exists():
                    from skill_library import SkillLibrary
                    restored_skills = SkillLibrary(self.project)
                    restored_registry = restored_skills._load_registry()
                    restored_references=[]
                    def collect_references(value):
                        if isinstance(value,dict):
                            if {'kind','uri','sha256'} <= set(value):
                                restored_references.append(value)
                            for child in value.values(): collect_references(child)
                        elif isinstance(value,list):
                            for child in value: collect_references(child)
                    collect_references(restored_registry)
                    for reference in restored_references:
                        restored_skills._verify_reference(reference,reference.get('run_id'))
            os.replace(temporary, self.index.database)
            if restore_journal.exists():
                restore_journal.unlink()
            if stage.exists() and stage.resolve().parent == self.index.directory.resolve():
                import shutil
                shutil.rmtree(stage)
            return {'restored': True, 'project_id': self.project.identity,
                    'database_sha256': manifest['database_sha256'],
                    'integrity_verified': True, 'documents': len(self.list_documents()),
                    'skill_registry_included':'skills/registry.json' in asset_data,
                    'evidence_files_restored':sum(name.startswith('evidence/') for name in asset_data),
                    'library_verify':self.verify()}
        finally:
            temporary.unlink(missing_ok=True)

    def _has_indexed_data(self) -> bool:
        try:
            connection = self.index._connect()
            try:
                return any(connection.execute(f'SELECT count(*) FROM {table}').fetchone()[0]
                           for table in ('documents','chunks','library_documents','library_chunks'))
            finally:
                connection.close()
        except (sqlite3.DatabaseError, AgentError):
            return True
