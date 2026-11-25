"""Opt-in, disposable actual-MiniLM retrieval comparison; writes evidence under .local/tests."""
from __future__ import annotations

from datetime import datetime, timezone
import json
import shutil
import tempfile
import time
import uuid
from pathlib import Path

from fixture_repo import make_fixture
from knowledge_documents import DocumentKnowledgeLibrary
from knowledge_embeddings import LocalSentenceTransformerProvider
from local_agent import HUB


DOCUMENTS = [
    ('async.md', '# Cooperative scheduling\n\nAn asyncio coroutine suspends at each await so the event loop can schedule other ready tasks. Long CPU-bound work blocks the loop and should be moved to a worker.\n'),
    ('queue.md', '# Bounded work queue\n\nA bounded asyncio queue applies producer backpressure when consumers fall behind. Producers pause instead of retaining unlimited buffered jobs, which prevents memory growth.\n'),
    ('errors.md', '# Exception chains\n\nUse raise DomainError(...) from exc to preserve the original exception as __cause__ while presenting a stable application-level error. Do not discard the causal traceback.\n'),
    ('retry.md', '# Retry code example\n\n```python\ndef safe_retry_delay(attempt: int, base: float = 0.25) -> float:\n    """Return bounded exponential backoff with deterministic jitter."""\n    delay = min(8.0, base * (2 ** max(0, attempt - 1)))\n    return delay\n```\n'),
    ('qa.md', '# Deterministic tests\n\nInject a monotonic clock into timeout logic. Advance the fake clock in the test instead of sleeping against wall time, so boundary cases remain deterministic and fast.\n'),
    ('payments.md', '# Idempotent payment API\n\nPOST /v1/payments accepts an idempotency key. Repeating a request with the same key returns the original payment result and must not charge the customer twice.\n'),
    ('fermentation.md', '# Sourdough starter\n\nMix equal weights of flour and water. Feed the starter on a regular schedule and keep it at a moderate room temperature while wild yeast ferments the mixture.\n'),
    ('timeout-a.md', '# Callback timeout migration note A\n\nThe legacy callback client uses a 5 second timeout by default.\n'),
    ('timeout-b.md', '# Callback timeout migration note B\n\nThe new callback client uses a 15 second timeout by default.\n'),
]
QUESTIONS = [
    ('exact_identifier','What does safe_retry_delay return?',['retry.md']),
    ('async_paraphrase','How can long-running work avoid starving other tasks?',['async.md']),
    ('queue_synonym','What prevents memory exhaustion when clients outpace workers?',['queue.md']),
    ('exception_behavior','How should an application retain the root cause while returning a stable error?',['errors.md']),
    ('code_example','Show the helper that caps retry waiting time.',['retry.md']),
    ('qa_clock','How do tests avoid waiting on real time for deadline cases?',['qa.md']),
    ('api_identifier','What happens if the same payment request is submitted twice?',['payments.md']),
    ('unrelated_but_supported','How should I feed a sourdough culture?',['fermentation.md']),
    ('conflicting_sources','What default callback timeout do the references describe?',['timeout-a.md','timeout-b.md']),
    ('absent_information','What color was the launch room wall?',[]),
]


def metrics(results, expected):
    ids=[row['original_name'] for row in results]
    relevant=set(expected)
    return {'top_1_recall':(1.0 if ids and ids[0] in relevant else 0.0) if relevant else None,
        'top_3_recall':len(set(ids[:3]) & relevant)/len(relevant) if relevant else None,
        'top_5_recall':len(set(ids[:5]) & relevant)/len(relevant) if relevant else None,
        'mrr':next((1.0/(index+1) for index,value in enumerate(ids) if value in relevant),0.0) if relevant else None,
        'false_positive':bool(ids) if not relevant else False,'returned':ids}


def main():
    evidence_dir=HUB/'.local'/'tests'/('semantic-retrieval-'+uuid.uuid4().hex[:12])
    evidence_dir.mkdir(parents=True,exist_ok=False)
    disposable=Path(tempfile.mkdtemp(prefix='semantic-repo-',dir=HUB/'.local'/'tests'))
    project,_=make_fixture(disposable/'repo')
    provider=LocalSentenceTransformerProvider()
    library=DocumentKnowledgeLibrary(project,embedding_provider=provider)
    indexing=[]
    for name,content in DOCUMENTS:
        started=time.perf_counter()
        row=library.ingest(name,content.encode('utf-8'))
        indexing.append({'name':name,'chunks':row['chunks'],'semantic_status':row['semantic_status'],
            'elapsed_seconds':round(time.perf_counter()-started,6)})
    comparison={}
    for mode in ('keyword','semantic','hybrid'):
        comparison[mode]={}
        for label,query,expected in QUESTIONS:
            started=time.perf_counter()
            result=library.search(query,retrieval_mode=mode,limit=8)
            comparison[mode][label]={**metrics(result['results'],expected),
                'latency_seconds':round(time.perf_counter()-started,6),
                'reported_strategy':result['strategy'],'semantic_status':result['semantic_status']}
    report={'schema_version':1,'started_at':datetime.now(timezone.utc).isoformat(),
        'model':provider.descriptor(),'runtime':'CPU only, offline, local_files_only, trust_remote_code=False',
        'documents':indexing,'queries':len(QUESTIONS),'retrievals':len(QUESTIONS)*3,
        'ranking_strategy':'reciprocal-rank fusion, k=60, cosine prefilter >= 0.28',
        'comparison':comparison,'library_integrity':library.verify(),
        'disposable_project_id':project.identity,'fixture_directory':str(disposable),
        'evidence_directory':str(evidence_dir)}
    summaries={}
    for mode,rows in comparison.items():
        labeled=[row for row in rows.values() if row['top_3_recall'] is not None]
        absent=[row for row in rows.values() if row['top_3_recall'] is None]
        summaries[mode]={'mean_recall_at_3':sum(row['top_3_recall'] for row in labeled)/len(labeled),
            'mean_mrr':sum(row['mrr'] for row in labeled)/len(labeled),
            'absent_query_false_positive_rate':sum(row['false_positive'] for row in absent)/max(1,len(absent)),
            'median_latency_seconds':sorted(row['latency_seconds'] for row in rows.values())[len(rows)//2]}
    report['summary']=summaries
    report['finished_at']=datetime.now(timezone.utc).isoformat()
    (evidence_dir/'result.json').write_text(json.dumps(report,indent=2)+'\n',encoding='utf-8')
    provider.close()
    print(json.dumps({'evidence':str(evidence_dir/'result.json'),'model':report['model'],
        'summary':summaries,'integrity':report['library_integrity']},indent=2))


if __name__=='__main__':
    main()
