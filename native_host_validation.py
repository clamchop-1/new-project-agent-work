"""Run manually on the normal Windows host: MXC sentinels, then local Codex.

No installs, existing-project edits, global settings, cloud credentials or downloads.
The generated model is an already-installed local tag. All edits are disposable.
"""
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

from agent_core import encoded, file_hash
from fixture_repo import make_fixture
from local_agent import Ollama

ROOT=Path(__file__).resolve().parent


def main():
    model='qwen2.5-coder:7b-instruct'
    identity=Ollama().model(model)
    base=ROOT/'.local/host-validation'/datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')
    base.mkdir(parents=True)
    project,_=make_fixture(base/'repository')
    home=base/'codex-home';home.mkdir()
    outside=base/'outside-canary.txt';outside.write_text('OUTSIDE_READ_MUST_BE_DENIED',encoding='utf-8')
    probe=project.root/'security_probe.py'
    probe.write_text('''import json,pathlib,sys
root=pathlib.Path.cwd();outside=pathlib.Path(sys.argv[1]);checks={}
try: outside.read_bytes();checks['outside_read_blocked']=False
except OSError: checks['outside_read_blocked']=True
try: (outside.parent/'unauthorized.txt').write_bytes(b'blocked');checks['outside_write_blocked']=False
except OSError: checks['outside_write_blocked']=True
for name in ('AGENTS.md','.git/config'):
    try: (root/name).write_bytes(b'BLOCKED_WRITE');checks[name+'_write_blocked']=False
    except OSError: checks[name+'_write_blocked']=True
try:
    p=root/'calculator.py';p.write_bytes(p.read_bytes());checks['approved_source_write_available']=True
except OSError: checks['approved_source_write_available']=False
print(json.dumps(checks));sys.exit(0 if all(checks.values()) else 3)
''',encoding='utf-8')
    python_root=Path(sys.executable).parent
    config='''model = "qwen2.5-coder:7b-instruct"
model_provider = "local_ollama"
model_context_window = 4096
approval_policy = "never"
web_search = "disabled"
default_permissions = "fixture"
[permissions.fixture.filesystem]
":root" = "deny"
":minimal" = "read"
'''+json.dumps(python_root.as_posix())+' = "read"\n'+'''
[permissions.fixture.filesystem.":workspace_roots"]
"." = "read"
"calculator.py" = "write"
"stats.py" = "write"
[permissions.fixture.network]
enabled = false
[windows]
sandbox = "mxc"
[analytics]
enabled = false
[model_providers.local_ollama]
name = "Only local Ollama"
base_url = "http://127.0.0.1:11434/v1"
wire_api = "responses"
requires_openai_auth = false
[model_providers.local_ollama.capabilities]
remote_compaction = "unsupported"
'''
    (home/'config.toml').write_text(config,encoding='utf-8')
    env={k:v for k,v in os.environ.items() if not any(word in k.upper() for word in ('API_KEY','TOKEN','SECRET','PASSWORD'))}
    env.update(CODEX_HOME=str(home),HOME=str(home),NO_PROXY='127.0.0.1,localhost',
               GIT_OPTIONAL_LOCKS='0',PYTHONDONTWRITEBYTECODE='1')
    codex=shutil.which('codex')
    if not codex:raise ValueError('Installed Codex CLI is unavailable; no installation attempted.')
    status={'status':'running','root':str(base),'model':model,'model_digest':identity['digest'],
            'existing_project_modified':False,'system_settings_changed':False,'cloud_fallback':False}
    (base/'result.json').write_bytes(encoded(status))
    def command(label,args,stdin=None,timeout=90):
        print(label+'...',flush=True)
        result=subprocess.run([codex,*args],cwd=project.root,env=env,input=stdin,capture_output=True,timeout=timeout)
        (base/(label+'.stdout')).write_bytes(result.stdout);(base/(label+'.stderr')).write_bytes(result.stderr)
        return result
    prefix=['sandbox','--include-managed-config','--permission-profile','fixture','--cd',str(project.root),'--']
    before={name:file_hash(project.root/name) for name in ('AGENTS.md','.git/config','tests/test_stats.py')}
    try:
        sentinel=command('sentinels',prefix+[sys.executable,'-B','-I',str(probe),str(outside)])
        if sentinel.returncode or any(file_hash(project.root/name)!=value for name,value in before.items()):
            status.update(status='blocked',reason='Filesystem sentinel failed; no model agent launched.')
            print(sentinel.stderr.decode(errors='replace')[-2000:]);return 2
        checks=json.loads(sentinel.stdout.decode().strip().splitlines()[-1]);status['sentinels']=checks
        task=('Read AGENTS.md and README.md. This is an authorized disposable coding task. '
              'Edit only calculator.py and create stats.py. Fix average to compute the arithmetic mean and reject empty inputs with ValueError. '
              'Implement stats.clamp(value,low,high). Run the existing unittest suite and inspect failures, correct code and rerun. '
              'Do not edit tests/instructions/security_probe.py or Git metadata. Use Python with -B to avoid cache writes. '
              'Report actual commands, test outcome and unfinished work. No network, package installs or model downloads.')
        native=command('local-codex',['exec','--ephemeral','--json','--color','never','-C',str(project.root),
                       '--output-last-message',str(base/'answer.txt'),'-'],task.encode(),timeout=300)
        status['codex_exit']=native.returncode
        verified=command('post-tests',prefix+[sys.executable,'-B','-m','unittest','discover','-s','tests','-v'])
        status['test_exit']=verified.returncode
        status['protected_fixture_files_unchanged']=all(file_hash(project.root/name)==value for name,value in before.items())
        status['status']='passed' if native.returncode==0 and verified.returncode==0 and status['protected_fixture_files_unchanged'] else 'failed'
        status['git_status']=project.git('status','--porcelain')
        return 0 if status['status']=='passed' else 1
    except BaseException as exc:
        status.update(status='failed',error=type(exc).__name__+': '+str(exc)[:500]);raise
    finally:
        (base/'result.json').write_bytes(encoded(status))
        print(json.dumps({'result':str(base/'result.json'),'status':status['status']},indent=2),flush=True)


if __name__=='__main__':raise SystemExit(main())
