"""Run one bounded public research session through the real chat application.

Uses only an explicit generic public query, at most one DDG search and three
public HTTPS pages, and disposable retained knowledge that is removed before
the application shuts down. No private project material is sent online.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import queue
import signal
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
import uuid

ROOT=Path(__file__).resolve().parents[1]
DEFAULT_OUTPUT=ROOT/'.local'/'tests'/('research-live-'+uuid.uuid4().hex[:12])


def drain(process, output):
    for line in process.stdout:
        output.put(line.rstrip('\r\n'))
    output.put(None)


def launch(port):
    process=subprocess.Popen([sys.executable,'-u','-B',str(ROOT/'agent.py'),'local-chat',
        '--dashboard-port',str(port),'--no-browser-open'],cwd=ROOT,stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,text=True,encoding='utf-8',errors='replace',bufsize=1,
        creationflags=getattr(subprocess,'CREATE_NEW_PROCESS_GROUP',0))
    lines=queue.Queue();threading.Thread(target=drain,args=(process,lines),daemon=True).start()
    output=[];token=None;deadline=time.monotonic()+150
    while time.monotonic()<deadline and token is None:
        try: line=lines.get(timeout=.5)
        except queue.Empty:
            if process.poll() is not None: break
            continue
        if line is None: break
        if line.startswith('One-run dashboard bearer token: '): token=line.split(': ',1)[1]
        else: output.append(line)
    if token is None:
        stop(process)
        raise RuntimeError('Local chat did not start: '+' | '.join(output[-8:]))
    return process,f'http://127.0.0.1:{port}',token,output


def stop(process):
    if process.poll() is not None:return
    try:
        process.send_signal(signal.CTRL_BREAK_EVENT if os.name=='nt' else signal.SIGINT)
        process.wait(timeout=10)
    except (OSError,subprocess.TimeoutExpired):
        process.terminate()
        try:process.wait(timeout=5)
        except subprocess.TimeoutExpired:process.kill();process.wait(timeout=5)


def request(base,token,path,*,method='GET',body=None,stream=False):
    headers={'Authorization':'Bearer '+token};raw=None
    if method in {'POST','DELETE'}:
        headers.update({'Origin':base,'Content-Type':'application/json'})
        raw=json.dumps(body or {}).encode()
    req=urllib.request.Request(base+path,data=raw,headers=headers,method=method)
    response=urllib.request.urlopen(req,timeout=240)
    if stream:return response
    with response:return json.loads(response.read())


def ask(base,token,conversation,model_key,text,mode='local',resume_id=None):
    body={'message':text,'model':model_key,'research_mode':mode}
    if resume_id:body['resume_id']=resume_id
    with request(base,token,f'/api/chat/conversations/{conversation}/messages',
            method='POST',body=body,stream=True) as response:
        events=[json.loads(line) for line in response if line.strip()]
    errors=[row for row in events if row.get('type')=='error']
    if errors:raise RuntimeError(errors[0].get('message','Research chat failed.'))
    return events


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--port',type=int,default=52721)
    parser.add_argument('--output',type=Path,default=DEFAULT_OUTPUT)
    args=parser.parse_args();args.output.mkdir(parents=True,exist_ok=False)
    result={'format':'localcodingagent-live-research-smoke-v1','verified':False,
        'output_dir':str(args.output),'external_limits':{'searches':1,'pages':3}}
    process=None;temporary_ids=[];base=None;token=None
    try:
        process,base,token,startup=launch(args.port)
        result['startup']=[row for row in startup if 'token' not in row.casefold()]
        inventory=request(base,token,'/api/chat/models')
        mac=next(row for row in inventory['models'] if row.get('provider_id')=='ssh_tunnel_ollama'
            and row.get('model_id')=='qwen3-coder:30b' and row.get('authorized')
            and row.get('available') and row.get('compatible'))
        windows=next(row for row in inventory['models'] if row.get('provider_id')=='ollama'
            and row.get('authorized') and row.get('available') and row.get('compatible'))
        result['models']={'mac':mac['model_id'],'windows':windows['model_id']}
        conversation=request(base,token,'/api/chat/conversations',method='POST',body={})['id']
        result['conversation_id']=conversation
        query='Research official Python asyncio task cancellation documentation and explain what exception callers should handle.'
        events=ask(base,token,conversation,mac['model_key'],query,'explicit')
        result['mac_research_events']=[row['type'] for row in events]
        source_event=next(row for row in events if row.get('type')=='sources')
        sources=source_event['sources'];result['sources']=[{key:source.get(key) for key in
            ('title','url','retrieved_at','publication_date','content_sha256')} for source in sources]
        result['research_search_count']=sum('Searching online' in row.get('text','') for row in events)
        if not sources:raise RuntimeError('No live webpages were retrieved; no citation verification is possible.')
        retained=request(base,token,f"/api/research/sources/{sources[0]['source_id']}/save",
            method='POST',body={'conversation_id':conversation})
        temporary_ids.append(retained['document_id'])
        result['retained_document_id']=retained['document_id']
        windows_events=ask(base,token,conversation,windows['model_key'],
            'According to the retained research, what exception signals cancellation?', 'local')
        result['windows_answer_has_library_citation']=any(
            '[K' in row.get('text','') for row in windows_events if row.get('type')=='token')
        result['cross_model_events']=[row['type'] for row in windows_events]
        row=request(base,token,f'/api/chat/conversations/{conversation}')
        result['persisted_source_references']=sum(len(message.get('sources',[])) for message in row['messages'])
        stop(process);process=None
        process,base,token,restart=launch(args.port)
        restored=request(base,token,f'/api/chat/conversations/{conversation}')
        result['conversation_after_restart']=len(restored['messages'])==len(row['messages'])
        result['knowledge_after_restart']=retained['document_id'] in [item['doc_id'] for item in
            request(base,token,'/api/knowledge/documents')['documents']]
        delete_req=urllib.request.Request(base+f"/api/knowledge/documents/{retained['document_id']}",
            data=b'{}',headers={'Authorization':'Bearer '+token,'Origin':base,
            'Content-Type':'application/json'},method='DELETE')
        with urllib.request.urlopen(delete_req,timeout=15) as response:
            result['deletion_result']=json.loads(response.read())
        remaining=request(base,token,'/api/knowledge/documents')['documents']
        result['deleted_after_test']=retained['document_id'] not in [item['doc_id'] for item in remaining]
        post_delete=ask(base,token,conversation,windows['model_key'],
            'What does Task.cancel() do in the Python asyncio documentation?', 'local')
        result['post_delete_source_citations']=sum(1 for row in post_delete if row.get('type')=='sources')
        result['verified']=(inventory.get('connection')=='ready' and bool(sources)
            and result['windows_answer_has_library_citation'] and result['conversation_after_restart']
            and result['knowledge_after_restart'] and result['deleted_after_test']
            and result['post_delete_source_citations']==0)
    except Exception as exc:
        result['failure']=type(exc).__name__+': '+str(exc)[:400]
    finally:
        if process is not None:
            if base and token:
                for document_id in temporary_ids:
                    try:
                        req=urllib.request.Request(base+f'/api/knowledge/documents/{document_id}',
                            data=b'{}',headers={'Authorization':'Bearer '+token,'Origin':base,
                            'Content-Type':'application/json'},method='DELETE')
                        with urllib.request.urlopen(req,timeout=5):pass
                    except Exception:pass
            stop(process)
    (args.output/'result.json').write_text(json.dumps(result,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
    print(json.dumps(result,ensure_ascii=False,indent=2))
    return 0 if result['verified'] else 1


if __name__=='__main__':raise SystemExit(main())
