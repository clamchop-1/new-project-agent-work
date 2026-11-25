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
from typing import Protocol, runtime_checkable
import uuid
from urllib.parse import urlsplit
import zipfile
from pathlib import Path

from agent_core import AgentError, Project, atomic_write
from knowledge_index import (KnowledgeIndex, _content_text, chunk_document, _is_reparse,
                             _pack_vector, _unpack_vector, _vector, RRF_K)

MAX_UPLOAD_BYTES = 8 * 1024 * 1024
MAX_EXTRACTED_CHARS = 4_000_000
MAX_PDF_PAGES = 300
MAX_LIBRARY_DOCUMENTS = 500
MAX_LIBRARY_CHUNKS = 12000
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
        raise AgentError('Document must be nonempty and no larger than 8 MiB.')
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
                raise AgentError('PDF must contain between 1 and 300 pages.')
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
                INSERT INTO metadata(key,value) VALUES('library_schema_version','4')
                    ON CONFLICT(key) DO NOTHING;
            ''')
            version = self.index._meta(connection, 'library_schema_version')
            if version in {'1','2','3'}:
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
                connection.execute("UPDATE metadata SET value='4' WHERE key='library_schema_version'")
                connection.commit()
                version = '4'
            if version != '4':
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
        """Attach a complete vector set atomically; keep committed FTS on any failure."""
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
                    WHERE doc_id=? ORDER BY ordinal''', (document_id,)).fetchall()
            finally:
                connection.close()
            if not rows or len(rows) > 256:
                raise AgentError('Document exceeds one bounded semantic indexing batch.')
            vectors = provider.embed([row['content'] for row in rows])
            if len(vectors) != len(rows):
                raise AgentError('Embedding provider returned an incomplete document batch.')
            packed = []
            for vector in vectors:
                normalized = _vector(vector, descriptor['dimensions'])
                packed.append(_pack_vector(normalized))
            with self._writer_lock('document-vector-index'):
                connection = self.index._connect()
                try:
                    connection.execute('BEGIN IMMEDIATE')
                    current = connection.execute('''SELECT status FROM library_documents
                        WHERE doc_id=? AND project_id=?''',
                        (document_id, self.project.identity)).fetchone()
                    if current is None or current['status'] != 'current':
                        connection.rollback()
                        return 'removed'
                    connection.executemany('''UPDATE library_chunks SET embedding=?,embedding_dimensions=?,
                        embedding_provider=?,embedding_model=?,embedding_version=?,embedding_digest=?
                        WHERE chunk_id=? AND doc_id=?''', [
                        (sqlite3.Binary(vector), descriptor['dimensions'], descriptor['provider_id'],
                         descriptor['model_id'], descriptor['model_version'], descriptor['model_digest'],
                         row['chunk_id'], document_id) for row, vector in zip(rows, packed)])
                    connection.execute('''UPDATE library_documents SET semantic_status='ready'
                        WHERE doc_id=? AND project_id=?''', (document_id, self.project.identity))
                    connection.commit()
                except BaseException:
                    if connection.in_transaction:
                        connection.rollback()
                    raise
                finally:
                    connection.close()
            return 'ready'
        except (AgentError, OSError, ValueError, KeyError, TypeError, RuntimeError, TimeoutError):
            self._set_semantic_status(document_id, 'keyword_only')
            return 'keyword_only'

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
            rows = connection.execute('''SELECT doc_id,title,original_name,extension,mime_type,
                content_sha256,byte_size,page_count,status,semantic_status,document_version,version_of,
                duplicate_of,scope,created_at
                FROM library_documents WHERE project_id=? AND status='current'
                AND (scope='global' OR (scope='conversation' AND conversation_id=?))
                ORDER BY created_at DESC LIMIT ?''',
                (self.project.identity, conversation_id or '', MAX_LIBRARY_DOCUMENTS + 1)).fetchall()
            return [dict(row) for row in rows]
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

    def search(self, query: str, *, conversation_id=None, limit=MAX_RETRIEVAL_RESULTS,
               provider=None, cancel_event=None, retrieval_mode='hybrid') -> dict:
        if not isinstance(query, str) or not query.strip() or len(query.encode('utf-8')) > 2000:
            raise AgentError('Knowledge query must contain 1 to 2000 UTF-8 bytes.')
        if type(limit) is not int or not 1 <= limit <= MAX_RETRIEVAL_RESULTS:
            raise AgentError('Knowledge result limit is outside the supported range.')
        if retrieval_mode not in {'keyword', 'semantic', 'hybrid'}:
            raise AgentError('Knowledge retrieval mode is unsupported.')
        from agent_core import no_secrets
        no_secrets(query)
        conversation_id = self._conversation(conversation_id)
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
                ORDER BY rank,c.chunk_id LIMIT 40''',
                (expression or '"__no_fts_terms__"', self.project.identity,
                 conversation_id or '')).fetchall() if expression and retrieval_mode != 'semantic' else []
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
                    ORDER BY c.chunk_id LIMIT ?''',
                    (self.project.identity, descriptor['provider_id'], descriptor['model_id'],
                     descriptor['model_version'], descriptor['model_digest'], descriptor['dimensions'],
                     conversation_id or '', MAX_LIBRARY_CHUNKS + 1)).fetchall()
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
                if per_document.get(row['doc_id'], 0) >= 2:
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
                'schema_version':4,'research_sources_checked':len(research_rows),
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
            return {'project_id':self.project.identity,'schema_version':4,
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
        provider = self._embedding_provider
        if provider is not None and callable(getattr(provider, 'close', None)):
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
                'knowledge_schema_version': 4, 'embedding_identity': embedding,
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
