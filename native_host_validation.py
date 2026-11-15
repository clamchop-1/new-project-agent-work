"""Run isolated MXC and Codex-to-Ollama checks on the normal Windows host.

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


def is_sensitive_environment_variable(name):
    normalized=name.upper()
    markers=('API_KEY','TOKEN','SECRET','PASSWORD','CREDENTIAL','ACCESS_KEY','PRIVATE_KEY','KEYFILE','CERTIFICATE')
    named_credentials={'GOOGLE_APPLICATION_CREDENTIALS','GOOGLE_CLOUD_KEYFILE_JSON'}
    return (any(marker in normalized for marker in markers)
            or normalized in named_credentials)


def main():
    model=os.environ.get('LOCAL_CODEX_MODEL','qwen3.5:4b-codex-test')
    context_window=int(os.environ.get('LOCAL_CODEX_CONTEXT','8192'))
    call_timeout=int(os.environ.get('LOCAL_CODEX_TIMEOUT','120'))
    task_mode=os.environ.get('LOCAL_CODEX_TASK','compact-edit')
    route=os.environ.get('LOCAL_CODEX_ROUTE','responses').lower()
    if route not in {'builtin','responses'}:raise ValueError('LOCAL_CODEX_ROUTE must be builtin or responses.')
    reasoning_effort=os.environ.get('LOCAL_CODEX_REASONING','none')
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
for name in ('AGENTS.md','README.md','.git/config','tests/test_stats.py','.local/pinned-tests.json','security_probe.py'):
    try: (root/name).write_bytes(b'BLOCKED_WRITE');checks[name+'_write_blocked']=False
    except OSError: checks[name+'_write_blocked']=True
try:
    p=root/'calculator.py';p.write_bytes(p.read_bytes());checks['approved_source_write_available']=True
except OSError: checks['approved_source_write_available']=False
try:
    (root/'stats.py').write_text('',encoding='utf-8');checks['approved_new_file_write_available']=True
except OSError: checks['approved_new_file_write_available']=False
print(json.dumps(checks));sys.exit(0 if all(checks.values()) else 3)
''',encoding='utf-8')
    python_root=Path(sys.executable).parent
    provider_config='''[model_providers.local_ollama]
name = "Only local Ollama"
base_url = "http://127.0.0.1:11434/v1"
wire_api = "responses"
requires_openai_auth = false
[model_providers.local_ollama.capabilities]
remote_compaction = "unsupported"
''' if route=='responses' else ''
    selection='model_provider = "local_ollama"' if route=='responses' else 'oss_provider = "ollama"'
    catalog_path=home/'model.json'
    catalog={'models':[{'slug':model,'display_name':model,'context_window':context_window,
        'shell_type':'default','visibility':'list','supported_in_api':True,'priority':0,
        'truncation_policy':{'mode':'bytes','limit':10000},'input_modalities':['text'],
        'base_instructions':'','support_verbosity':True,'default_verbosity':'low',
        'supports_parallel_tool_calls':False,'supports_reasoning_summaries':False,
        'supported_reasoning_levels':[],'default_reasoning_level':None,
        'experimental_supported_tools':[]}]}
    catalog_path.write_text(json.dumps(catalog,indent=2),encoding='utf-8')
    config=f'''model = "{model}"
{selection}
model_catalog_json = {json.dumps(catalog_path.as_posix())}
model_context_window = {context_window}
model_reasoning_effort = "{reasoning_effort}"
approval_policy = "never"
web_search = "disabled"
default_permissions = "fixture"
[permissions.fixture.filesystem]
":root" = "deny"
":minimal" = "read"
'''+json.dumps(python_root.as_posix())+' = "read"\n'+'''
[permissions.fixture.filesystem.":workspace_roots"]
"." = "write"
"AGENTS.md" = "read"
"README.md" = "read"
"tests" = "read"
".local" = "read"
".git" = "deny"
"security_probe.py" = "read"
"calculator.py" = "write"
"stats.py" = "write"
[permissions.fixture.network]
enabled = false
[windows]
sandbox = "mxc"
[analytics]
enabled = false
'''+provider_config
    (home/'config.toml').write_text(config,encoding='utf-8')
    env={k:v for k,v in os.environ.items() if not is_sensitive_environment_variable(k)}
    env.update(CODEX_HOME=str(home),HOME=str(home),NO_PROXY='127.0.0.1,localhost',
               PATH=str(python_root)+os.pathsep+env.get('PATH',''),
               GIT_OPTIONAL_LOCKS='0',PYTHONDONTWRITEBYTECODE='1')
    codex=shutil.which('codex')
    if not codex:raise ValueError('Installed Codex CLI is unavailable; no installation attempted.')
    status={'status':'running','root':str(base),'model':model,'route':route,'context_window':context_window,
            'task_mode':task_mode,
            'reasoning_effort':reasoning_effort,
            'call_timeout':call_timeout,'model_digest':identity['digest'],
            'existing_project_modified':False,'system_settings_changed':False,'cloud_fallback':False}
    (base/'result.json').write_bytes(encoded(status))
    def command(label,args,stdin=None,timeout=90):
        print(label+'...',flush=True)
        try:
            result=subprocess.run([codex,*args],cwd=project.root,env=env,input=stdin,capture_output=True,timeout=timeout)
        except subprocess.TimeoutExpired as exc:
            result=subprocess.CompletedProcess(args,124,exc.output or b'',exc.stderr or b'')
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
        task=('Run Python with -B to print CODEX_TOOL_OK using your available command-execution tool. '
              'Do not include or describe the command in your reply; execute it and report the captured output.'
              if task_mode=='tool-smoke' else
              'You are Codex CLI with a real PowerShell command tool. Execute tool calls; never print commands as text. '
              'First use the tool once to read AGENTS.md, README.md, calculator.py, and tests/test_stats.py. '
              'Then use the tool once to run these PowerShell statements exactly, writing no other files:\n'
              "Set-Content -LiteralPath 'calculator.py' -Encoding utf8 -Value @('def average(values):', '    if not values:', '        raise ValueError(\"Average of empty input\")', '    return sum(values) / len(values)')\n"
              "Set-Content -LiteralPath 'stats.py' -Encoding utf8 -Value @('def clamp(value, low, high):', '    return min(max(value, low), high)')\n"
              'Then use the tool once to run `python -B -m unittest discover -s tests -v`. '
              'If all three tests pass, stop and report marker MAPLE-427. Do not retry, create temporary or backup files, '
              'use apply_patch, ask for escalated permission, or edit tests, instructions, memory, or Git metadata. '
              'No network, installs, or downloads.' if task_mode=='compact-edit' else
              'You are running inside Codex CLI, and your real command-execution tool is available. '
              'This is an authorized disposable coding task, not a request to write commands for a human. '
              'Start by using the tool to read AGENTS.md, README.md, calculator.py, and tests/test_stats.py. '
              'Then edit only calculator.py and stats.py: make average return the arithmetic mean and raise ValueError for empty input; '
              'implement stats.clamp(value, low, high). The blank stats.py has already been created for you. '
              'Use Python with -B to run the tests, fix failures, and rerun them. '
              'Write directly to those two permitted files; do not create temporary or backup files. Stop immediately after the three tests pass. '
              'Do not print or describe tool-call JSON. Do not edit tests, instructions, memory, or Git metadata. No network, installs, or downloads. '
              'Finish with the commands actually run, test result, and any unfinished work.')
        codex_args=['exec','--ephemeral','--json','--color','never']
        if route=='builtin':codex_args.extend(['--oss','--local-provider','ollama'])
        codex_args.extend(['-m',model,'-C',str(project.root),
                       '--output-last-message',str(base/'answer.txt'),'-'])
        native=command('local-codex',codex_args,task.encode(),timeout=call_timeout)
        status['codex_exit']=native.returncode
        native_events=[]
        for line in native.stdout.decode(errors='replace').splitlines():
            try:native_events.append(json.loads(line))
            except json.JSONDecodeError:continue
        status['codex_turn_completed']=any(event.get('type')=='turn.completed' for event in native_events)
        if task_mode=='tool-smoke':
            status['protected_fixture_files_unchanged']=all(file_hash(project.root/name)==value for name,value in before.items())
            status['git_status']=project.git('status','--porcelain')
            status['status']='smoke_completed' if native.returncode==0 else 'failed'
            return 0 if native.returncode==0 else native.returncode
        verified=command('post-tests',prefix+[sys.executable,'-B','-m','unittest','discover','-s','tests','-v'])
        status['test_exit']=verified.returncode
        status['protected_fixture_files_unchanged']=all(file_hash(project.root/name)==value for name,value in before.items())
        status['git_status']=project.git('status','--porcelain')
        allowed_fixture_changes={'calculator.py','stats.py','security_probe.py'}
        status['extra_fixture_files']=[line[3:] for line in status['git_status'].splitlines()
                                       if line[3:] not in allowed_fixture_changes]
        complete=native.returncode==0 or status['codex_turn_completed']
        verified_ok=verified.returncode==0 and status['protected_fixture_files_unchanged']
        if complete and verified_ok:
            status['status']='completed_verified_with_extra_files' if status['extra_fixture_files'] else 'passed'
        elif verified_ok and native.returncode==124:
            status['status']='tests_verified_after_codex_timeout'
        else:
            status['status']='failed'
        return 0 if status['status'] in {'passed','completed_verified_with_extra_files','tests_verified_after_codex_timeout'} else 1
    except BaseException as exc:
        status.update(status='failed',error=type(exc).__name__+': '+str(exc)[:500]);raise
    finally:
        (base/'result.json').write_bytes(encoded(status))
        print(json.dumps({'result':str(base/'result.json'),'status':status['status']},indent=2),flush=True)


if __name__=='__main__':raise SystemExit(main())
