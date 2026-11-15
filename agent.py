"""Human-facing launcher and shared-memory bridge; no global configuration writes."""
import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import subprocess
import sys
import uuid

from agent_core import AgentError, Project, atomic_write, encoded, digest, exclusive, save_handoff
from assistant_files import AssistantFiles
from local_agent import HUB, run_agent
from model_router import get_models, choose_model
from conversation_session import load_session
from agent_config import (config_path, default_projects, load_config,
                          legacy_name_map, selector_map)

REPAIR_ROOT = (HUB / '.local/tests/live-repair-001/repo').resolve()
REPAIR_WRITE_PATHS = frozenset({'calculator.py', 'stats.py'})
PINNED_TEST_PROJECTS = frozenset({'LocalAgentSandbox', 'LocalAgentRepairFixture'})

PROJECTS = default_projects(HUB)
PRIVATE = HUB/'.local'


def validate_write_paths(project_name, write_paths):
    paths = tuple(write_paths)
    if project_name == 'LocalAgentRepairFixture':
        if not paths or len(paths) != len(set(paths)) or not set(paths) <= REPAIR_WRITE_PATHS:
            raise AgentError('Repair fixture permits may authorize only calculator.py and stats.py.')
    return paths


def new_directory(kind):
    folder = PRIVATE/kind/(datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')+'-'+uuid.uuid4().hex[:8])
    folder.mkdir(parents=True)
    return folder


def snapshot(project, folder):
    packet = project.takeover()
    packet['memory_snapshot'] = project.memory_snapshot()
    atomic_write(folder/'takeover.json', encoded(packet))
    return packet


def prepare_bridges(project, folder, packet):
    state = {k: v for k, v in packet['state'].items() if k != 'files'}
    memory = json.dumps(packet['memory'], ensure_ascii=False, indent=2)
    common = ('Use the selected project only. Its existing AGENTS.md hierarchy remains authoritative. '
              'Newest leading memory supersedes older retained statements. Recall does not resume a hold. '
              'Do not load historical sections automatically, create a separate memory store or overwrite instructions. '
              'Git and actual command receipts are evidence; agent prose is not test proof.\n'
              'Memory updates must use the shared adapter with reviewed authorization, lock and expected hashes.\n'
              'Live Git snapshot at preparation:\n'+json.dumps(state,ensure_ascii=False)+'\n'
              'Complete bounded authoritative current sections, deduplicated:\n'+memory)
    (folder/'shared-current.md').write_text(common, encoding='utf-8')
    settings = {'autoMemoryEnabled': False, 'pluginConfigs': {'cc-plugin-agents-md@builtin':
                {'options': {'instructionFiles': 'claude-md-and-agents-md'}}}}
    atomic_write(folder/'claude-settings.json', encoded(settings))
    atomic_write(folder/'empty-mcp.json', encoded({'mcpServers': {}}))
    command = ['claude', '--permission-mode', 'plan', '--settings', str(folder/'claude-settings.json'),
               '--append-system-prompt-file', str(folder/'shared-current.md')]
    atomic_write(folder/'claude-command.json', encoded({'cwd':str(project.root),'argv':command,
                  'executed':False,'cloud_client_requires_explicit_user_launch':True}))
    return {'context':str(folder/'shared-current.md'),'claude_settings':str(folder/'claude-settings.json'),
            'claude_command':command,'notice':'Prepared only; no paid/cloud inference was launched.'}


def main():
    effective_config_path = config_path(HUB)
    config = load_config(effective_config_path, PROJECTS)
    configured_projects = legacy_name_map(config)
    selectors = selector_map(config)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=['config','inspect','run','approve','history','prepare-bridges','prepare-handoff','save-handoff'])
    parser.add_argument('--project', choices=selectors,
                        default='KronosStage' if 'KronosStage' in configured_projects else next(iter(configured_projects)))
    parser.add_argument('--task');parser.add_argument('--task-file',type=Path)
    parser.add_argument('--assistant-root', type=Path,
                        help='Assistant file root, usually the PowerShell current directory; reads are broad and writes require exact paths.')
    parser.add_argument('--assistant-write-path', action='append', default=[],
                        help='Authorize one exact relative text-file path for this task only.')
    parser.add_argument('--model',default=config['inference']['model']);parser.add_argument('--endpoint',default=config['inference']['endpoint'])
    parser.add_argument('--write-path',action='append',default=[])
    parser.add_argument(
        '--allow-pinned-tests', action='store_true',
        help='Expose this project’s registered pinned test suite to the agent for this run.',
    )
    parser.add_argument('--resume-held-project',action='store_true')
    parser.add_argument('--permit',type=Path);parser.add_argument('--run-directory',type=Path)
    parser.add_argument('--proposal',type=Path);parser.add_argument('--authorize-memory-save',action='store_true')
    parser.add_argument('--agent',choices=['codex','claude','qwen'],default='qwen')
    parser.add_argument('--history-index',type=int);parser.add_argument('--authorize-history',action='store_true')
    parser.add_argument('--conversation', action='store_true')
    args=parser.parse_args()

    assistant_write_paths = ()
    if args.assistant_write_path:
        if args.action != 'run' or not args.assistant_root:
            raise AgentError('Assistant file write paths require a run with an explicit assistant root.')
        assistant_scope = AssistantFiles(args.assistant_root)
        assistant_write_paths = tuple(
            assistant_scope.canonical_path(path) for path in args.assistant_write_path
        )
        if len(set(assistant_write_paths)) != len(assistant_write_paths):
            raise AgentError('Assistant file write paths must be unique.')

    # Reject unauthorized conversation use before project preparation.
    if args.conversation:
        if args.action != 'run':
            raise AgentError(
                'Conversation history is supported only for run actions.'
            )
        if args.permit or args.write_path:
            raise AgentError(
                'Conversation history cannot be combined with project write permits.'
            )

    if args.action == 'config':
        from platform_capabilities import capability_report
        print(json.dumps({'host_capabilities': capability_report(),
                          'projects': [{'id': key, **record} for key, record in config['projects'].items()],
                          'inference': config['inference'],
                          'config_path': str(effective_config_path)}, indent=2))
        return
    PRIVATE.mkdir(exist_ok=True)
    logical_project_id = selectors[args.project]
    selected_project_name = config['projects'][logical_project_id]['name']
    project=Project(configured_projects[selected_project_name])
    locks=PRIVATE/'locks';locks.mkdir(exist_ok=True)
    with exclusive(locks/(project.identity+'.lock'),args.agent):
        if args.action in ('inspect','prepare-bridges'):
            folder=new_directory('takeovers');packet=snapshot(project,folder)
            packet['logical_project_id'] = logical_project_id
            from platform_capabilities import capability_report
            packet['host_capabilities'] = capability_report()
            atomic_write(folder/'takeover.json', encoded(packet))
            bridge=prepare_bridges(project,folder,packet)
            print(json.dumps({'folder':str(folder),'project':str(project.root),'branch':packet['state']['branch'],
                 'commit':packet['state']['commit'],'status':packet['state']['status'],'deduplicated_memory_sections':len(packet['memory']),
                 'write_authorized':False,**bridge},indent=2));return
        if args.action=='history':
            if args.history_index is None:raise AgentError('Explicit historical section index is required.')
            print(json.dumps(project.memory(args.history_index,args.authorize_history),indent=2));return
        if args.action=='approve':
            if not args.resume_held_project:raise AgentError('Existing project holds require an explicit, task-specific resume decision.')
            if not args.task_file or not args.write_path:raise AgentError('Reviewable task file and exact write paths are required.')
            state=project.state()
            if state['status'].strip():raise AgentError('Existing worktree changes must be reviewed/isolated before edit authorization.')
            write_paths = validate_write_paths(selected_project_name, args.write_path)
            for path in write_paths:project.path(path)
            folder=new_directory('approvals')
            permit={'project_id':project.identity,'state':state,'memory':project.memory_snapshot(),
                    'task':args.task_file.read_text(encoding='utf-8'),'write_paths':list(write_paths),'used':False}
            atomic_write(folder/'permit.json',encoded(permit))
            print('Single-task edit permit: '+str(folder/'permit.json'));return
        if args.action=='run':
            task=args.task or (args.task_file.read_text(encoding='utf-8') if args.task_file else
                'Perform a read-only takeover. Read relevant source and explain architecture, newest hold/task, unfinished work and next safe step. Do not edit or resume work.')
            writes=[]
            if args.permit:
                permit_path=args.permit.resolve()
                if not permit_path.is_relative_to(PRIVATE/'approvals'):raise AgentError('Use an operator-created permit in this launcher workspace.')
                permit=json.loads(permit_path.read_bytes())
                if permit['used'] or permit['project_id']!=project.identity or permit['state']!=project.state() or permit['memory']!=project.memory_snapshot():
                    raise AgentError('Permit is used, cross-project or stale.')
                if not args.task and not args.task_file:task=permit['task']
                if task!=permit['task']:raise AgentError('Approved task changed.')
                writes=validate_write_paths(selected_project_name, permit['write_paths'])
                permit['used']=True;atomic_write(permit_path,encoded(permit))
            parent=new_directory('runs');packet=snapshot(project,parent)
            packet['logical_project_id'] = logical_project_id
            if args.assistant_root:
                packet['assistant_location'] = {
                    'root': str(args.assistant_root.resolve()),
                    'access': 'read-only',
                }
            from platform_capabilities import capability_report
            packet['host_capabilities'] = capability_report()
            atomic_write(parent/'takeover.json', encoded(packet))
            prepare_bridges(project,parent,packet)
            try:
                with exclusive(PRIVATE/'inference.lock','qwen'):
                    selected_model = args.model
                    if selected_model == 'auto':
                        provider_name = config['inference']['provider']
                        routing = choose_model(
                            get_models(args.endpoint, provider_name), task,
                            provider_name,
                        )
                        selected_model = routing['model']
                        print('LOCAL MODEL ROUTING: ' + json.dumps(routing), flush=True)
                    conversation = ()
                    if args.conversation:
                        if writes:
                            raise AgentError(
                                'Conversation history cannot be combined with write permits.'
                            )
                        conversation = load_session(selected_project_name)

                    test_config = None
                    if args.allow_pinned_tests and selected_project_name in PINNED_TEST_PROJECTS:
                        test_config = project.root / '.local' / 'pinned-tests.json'

                    result=run_agent(
                        project,selected_model,task,
                        parent/'execution',args.endpoint,
                        writes=writes,
                        conversation=conversation,
                        provider_name=config['inference']['provider'],
                        test_config=test_config,
                        assistant_root=args.assistant_root,
                        assistant_write_paths=assistant_write_paths,
                    )
            finally:
                atomic_write(parent/'post-run.json',encoded({'state':project.state(),'memory':packet['memory_snapshot'],
                              'task':task,'run':str(parent/'execution'),'logical_project_id':logical_project_id,
                              'assistant_location': packet.get('assistant_location')}))
            result.setdefault('project_id', project.identity)
            result.setdefault('commands', [])
            result['logical_project_id'] = logical_project_id
            atomic_write(parent/'execution'/'result.json', encoded(result))
            print(json.dumps({'run':str(parent),'logical_project_id':logical_project_id,
                              'status':result['status'],'summary':result.get('summary'),
                              'operator_policy':result['operator_policy'],'verified_test_status':result['verified_test_status']},indent=2));return
        if args.action=='prepare-handoff':
            if not args.run_directory:raise AgentError('Select an actual saved run.')
            run=args.run_directory.resolve()
            if not run.is_relative_to(PRIVATE/'runs'):raise AgentError('Run must belong to this launcher.')
            post=json.loads((run/'post-run.json').read_bytes());result=json.loads((run/'execution/result.json').read_bytes())
            if result['project_id']!=project.identity:raise AgentError('Run belongs to another project.')
            record={'event_id':'handoff_'+uuid.uuid4().hex,'agent':args.agent,'objective':post['task'],
                    'completed':[result.get('summary',{}).get('summary','No completed agent summary')],
                    'unfinished':result.get('summary',{}).get('unfinished',[]),
                    'decisions':['Preserve the existing authoritative project-memory workflow'],
                    'assumptions':[],'next_actions':result.get('summary',{}).get('next_actions',[]),
                    'constraints':['Existing holds and hardware/data safeguards remain in force'],
                    'test_receipts':result['commands']}
            proposal={'record':record,'expected_state':post['state'],'expected_memory':post['memory']}
            atomic_write(run/'handoff-proposal.json',encoded(proposal))
            print('Review proposal before saving: '+str(run/'handoff-proposal.json'));return
        if args.action=='save-handoff':
            if not args.proposal or not args.proposal.resolve().is_relative_to(PRIVATE):raise AgentError('Select a reviewed local proposal.')
            proposal=json.loads(args.proposal.read_bytes())
            result=save_handoff(project,proposal['record'],proposal['expected_state'],proposal['expected_memory'],args.authorize_memory_save)
            print(json.dumps(result,indent=2));return


if __name__=='__main__':
    try:main()
    except (AgentError,OSError,ValueError,KeyError) as exc:
        print('BLOCKED: '+str(exc),file=sys.stderr);raise SystemExit(2)
