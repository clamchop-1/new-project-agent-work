"""Opt-in authenticated API smoke using disposable documents and live local models."""
from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
import tempfile
import threading
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path

from agent import HUB, PRIVATE, PROJECTS, provider_specs
from agent_config import config_path, load_config
from agent_core import Project, exclusive
from chat_conversations import ConversationStore
from coding_dashboard import CodingDashboard
from local_agent import create_inference_provider
from knowledge_documents import DocumentKnowledgeLibrary
from knowledge_embeddings import LocalSentenceTransformerProvider
from model_catalog import discover_model_catalog
from fixture_repo import make_fixture


def main():
    config = load_config(config_path(HUB), PROJECTS)
    specs = provider_specs(config, config['inference']['endpoint'], 'remote_allowed')
    hosts = {'ollama': 'windows', 'ssh_tunnel_ollama': config['inference'].get('remote', {}).get('profile_id', 'mac_studio')}
    catalog = discover_model_catalog(specs, host_ids=hosts)
    choices = {}
    for provider_id, model_id in [('ssh_tunnel_ollama', 'qwen3-coder:30b'), ('ollama', 'qwen3:4b')]:
        match = next((row for row in catalog['models'] if row['provider_id'] == provider_id
            and row['model_id'] == model_id and row['authorized'] and row['available']
            and row['installed'] and row['compatible']), None)
        if match is None:
            raise RuntimeError('Required authorized live model unavailable: ' + provider_id + '/' + model_id)
        choices[model_id] = match

    evidence = HUB / '.local' / 'tests' / ('knowledge-live-' + uuid.uuid4().hex[:16])
    evidence.mkdir(parents=True, exist_ok=False)
    fixture_root = Path(tempfile.mkdtemp(prefix='knowledge-disposable-', dir=HUB / '.local' / 'tests'))
    project_root = fixture_root / 'repo'
    # Make a normal disposable fixture; no registered project path is accessed.
    project, _ = make_fixture(project_root)
    embedding_provider = LocalSentenceTransformerProvider()
    library = DocumentKnowledgeLibrary(project, embedding_provider=embedding_provider)
    history = fixture_root / 'history'
    conversations = ConversationStore(history)

    def runner(messages, model, cancel, on_token):
        provider_id = model['provider_id']; model_id = model['model_id']
        spec = specs.get(provider_id)
        if spec is None:
            raise RuntimeError('Provider is not in current authorization configuration.')
        with exclusive(PRIVATE / 'inference.lock', 'knowledge-live-chat'):
            provider = create_inference_provider(provider_id, spec['endpoint'], spec.get('profile'))
            try:
                provider.model(model_id)
                provider.chat_stream(model_id, messages, 16384, {'temperature': 0.2}, on_token,
                    cancel_event=cancel, timeout=180)
            finally:
                provider.close()

    server = None; thread = None
    def start_dashboard():
        nonlocal server, thread
        dashboard = CodingDashboard((), allowed_models=('qwen3:8b',),
            chat_store=conversations, chat_runner=runner, chat_models=catalog['models'],
            knowledge_store=DocumentKnowledgeLibrary(project,embedding_provider=embedding_provider))
        server = dashboard.start()
        thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
        return dashboard

    def stop_dashboard():
        nonlocal server, thread
        server.shutdown(); server.server_close(); thread.join(timeout=5)
        server = None; thread = None

    def request(dashboard, base, path, *, method='GET', body=None, headers=None):
        request_headers={'Authorization':'Bearer '+dashboard.token}; request_headers.update(headers or {})
        if method in {'POST','DELETE'}: request_headers.setdefault('Origin',base)
        req=urllib.request.Request(base+path,data=body,headers=request_headers,method=method)
        try:
            with urllib.request.urlopen(req,timeout=210) as response:return response.status,response.read()
        except urllib.error.HTTPError as error:
            try:return error.code,error.read()
            finally:error.close()

    result={'schema_version':2,'started_at':datetime.now(timezone.utc).isoformat(),
            'fixture_project_id':project.identity,'models':[],'checks':[]}
    try:
        dashboard=start_dashboard(); base='http://127.0.0.1:'+str(server.server_port)
        conversation=json.loads(request(dashboard,base,'/api/chat/conversations',method='POST',body=b'{}',
            headers={'Content-Type':'application/json'})[1])
        conversation_id=conversation['id']
        source=(b'# Bounded Stream Queue\n\nThe fixture uses a 73-item queue. When it is full, producer backpressure stops writes; '
                b'a 250 millisecond drain timeout drops the oldest event. This prevents queue memory from growing without bound.\n')
        boundary='lca-live-'+uuid.uuid4().hex
        upload=(f'--{boundary}\r\nContent-Disposition: form-data; name="scope"\r\n\r\nglobal\r\n'
                f'--{boundary}\r\nContent-Disposition: form-data; name="file"; filename="queue-manual.md"\r\n'
                f'Content-Type: application/octet-stream\r\n\r\n').encode()+source+f'\r\n--{boundary}--\r\n'.encode()
        status,body=request(dashboard,base,'/api/knowledge/documents',method='POST',body=upload,
            headers={'Content-Type':'multipart/form-data; boundary='+boundary})
        if status!=201: raise RuntimeError('Upload failed: '+body.decode('utf-8','replace')[:240])
        document=json.loads(body); result['document_sha256']=document['content_sha256']
        if document.get('semantic_status')!='ready':
            raise RuntimeError('The disposable document did not receive actual semantic vectors.')
        result['checks'].append('uploaded_keyword_and_semantic_index')
        paraphrase='What protects memory when producers outpace consumers?'
        semantic=library.search(paraphrase,retrieval_mode='semantic',conversation_id=conversation_id)
        if not semantic['results'] or semantic['results'][0]['doc_id']!=document['document_id']:
            raise RuntimeError('Actual MiniLM retrieval did not find the paraphrased fixture passage.')
        result['semantic_query_sha256']=hashlib.sha256(paraphrase.encode()).hexdigest()
        result['semantic_result']=semantic['results'][0]['citation']
        result['checks'].append('semantic_paraphrase_retrieved_actual_passage')

        for model_id,question in [('qwen3-coder:30b',paraphrase),
                                  ('qwen3:4b','How many items fit in the fixture queue?')]:
            started=time.monotonic()
            status,stream=request(dashboard,base,'/api/chat/conversations/'+conversation_id+'/messages',
                method='POST',body=json.dumps({'message':question,'model':choices[model_id]['model_key']}).encode(),
                headers={'Content-Type':'application/json'})
            if status!=200: raise RuntimeError('Chat request failed: '+stream.decode('utf-8','replace')[:240])
            events=[json.loads(line) for line in stream.splitlines() if line]
            model_event=next((event for event in events if event.get('type')=='model'),None)
            tokens=''.join(event.get('text','') for event in events if event.get('type')=='token')
            sources=[event for event in events if event.get('type')=='sources']
            if not model_event or not tokens or not sources:
                raise RuntimeError('Live stream lacked model identity, generated text, or application citations.')
            result['models'].append({'model_id':model_event['model_id'],'provider_id':model_event['provider_id'],
                'host_id':model_event['host_id'],'question_sha256':hashlib.sha256(question.encode()).hexdigest(),
                'response_sha256':hashlib.sha256(tokens.encode()).hexdigest(),
                'response_chars':len(tokens),'source_count':len(sources[0]['sources']),
                'elapsed_seconds':round(time.monotonic()-started,3)})
        result['checks'].append('mac_and_windows_streamed_with_sources')
        stop_dashboard()
        library=DocumentKnowledgeLibrary(project,embedding_provider=embedding_provider); conversations=ConversationStore(history)
        dashboard=start_dashboard(); base='http://127.0.0.1:'+str(server.server_port)
        documents=json.loads(request(dashboard,base,'/api/knowledge/documents')[1])['documents']
        saved=json.loads(request(dashboard,base,'/api/chat/conversations/'+conversation_id)[1])
        if not documents or len(saved['messages'])!=4:
            raise RuntimeError('Knowledge or conversation did not persist across dashboard restart.')
        if documents[0]['semantic_status']!='ready':
            raise RuntimeError('Persisted document lost its semantic index after restart.')
        result['checks'].append('knowledge_and_conversation_survived_restart')
        backup=library.backup()
        library.index.database.unlink()
        restored=DocumentKnowledgeLibrary(project,embedding_provider=embedding_provider).restore(backup['backup'])
        library=DocumentKnowledgeLibrary(project,embedding_provider=embedding_provider)
        restored_query=library.search(paraphrase,retrieval_mode='semantic',conversation_id=conversation_id)
        if not restored['library_verify']['valid'] or not restored_query['results']:
            raise RuntimeError('Backup restore did not preserve the semantic index and citations.')
        result['backup_restore']={'database_sha256':restored['database_sha256'],
            'library_verify':restored['library_verify'],'semantic_source':restored_query['results'][0]['citation']}
        result['checks'].append('backup_restore_preserved_semantic_evidence')
        status,_=request(dashboard,base,'/api/knowledge/documents/'+document['document_id'],
                         method='DELETE',body=b'')
        keyword_after_delete=library.search('73-item queue',conversation_id=conversation_id,retrieval_mode='keyword')
        semantic_after_delete=library.search(paraphrase,conversation_id=conversation_id,retrieval_mode='semantic')
        if status!=200 or keyword_after_delete['results'] or semantic_after_delete['results']:
            raise RuntimeError('Removed test document remained in keyword or vector retrieval.')
        result['checks'].append('removed_document_absent_from_keyword_and_vector_search')
        result['library_integrity']=library.verify()
        result['finished_at']=datetime.now(timezone.utc).isoformat()
        (evidence/'result.json').write_text(json.dumps(result,indent=2)+'\n',encoding='utf-8')
        print(json.dumps({'result':str(evidence/'result.json'),'checks':result['checks'],
                          'models':result['models']},indent=2))
    finally:
        if server is not None: stop_dashboard()
        embedding_provider.close()


if __name__=='__main__':
    main()
