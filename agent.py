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
from model_router import discover_candidates, route_model, read_recent_performance
from model_registry import (ModelRegistryError, append_performance_evidence,
                            load_registry)
from conversation_session import load_session
from agent_config import (config_path, default_projects, load_config,
                          legacy_name_map, selector_map, DEFAULT_ENDPOINT,
                          validate_endpoint)

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


def approve_provider_fallback(proposal):
    print('Provider fallback request: '
          + proposal['from_provider'] + '/' + proposal['from_model'] + ' -> '
          + proposal['to_provider'] + '/' + proposal['to_model'])
    if proposal.get('data_sharing'):
        print('Approval permits sending this task context to the configured remote inference provider.')
    return input('Approve this provider change for this run? Type YES to approve: ').strip() == 'YES'


def parse_capability_minimums(values):
    parsed = {}
    for item in values:
        try:
            name, raw_score = item.split('=', 1)
            score = float(raw_score)
        except (ValueError, TypeError) as exc:
            raise AgentError('Capability minimums must use capability=0..1 format.') from exc
        if name in parsed or not 0 <= score <= 1:
            raise AgentError('Capability minimums must be unique and between zero and one.')
        parsed[name] = score
    return parsed


def provider_specs(config, endpoint, mode, provider_override=None):
    inference = config['inference']
    remote = inference.get('remote')
    local_endpoint = validate_endpoint(endpoint if inference['provider'] == 'ollama'
                                       else DEFAULT_ENDPOINT)
    if mode == 'configured':
        provider = provider_override or inference['provider']
        if provider == 'ollama':
            return {'ollama': {'endpoint': local_endpoint}}
        if provider == 'ssh_tunnel_ollama' and isinstance(remote, dict) and remote.get('enabled') is True:
            return {'ssh_tunnel_ollama': {
                'endpoint': f"http://127.0.0.1:{remote['port']}", 'profile': remote,
            }}
        raise AgentError('Requested provider is not explicitly authorized by host configuration.')
    specs = {'ollama': {'endpoint': local_endpoint}}
    if mode == 'remote_allowed' and isinstance(remote, dict) and remote.get('enabled') is True:
        specs['ssh_tunnel_ollama'] = {
            'endpoint': f"http://127.0.0.1:{remote['port']}", 'profile': remote,
        }
    if provider_override and provider_override not in specs:
        raise AgentError('Requested provider is not authorized for the selected routing mode.')
    return specs


def _record_verified_performance(result, execution_directory, registry_path):
    status = result.get('verified_test_status')
    integrity = result.get('receipt_integrity')
    if status not in ('passed', 'failed') or not isinstance(integrity, dict) or not integrity.get('valid'):
        return False
    routing = result.get('routing_decision') or {}
    selected = routing.get('selected') or {}
    provider = result.get('provider') or selected.get('provider_id')
    model = result.get('model') or selected.get('model_id')
    if provider not in ('ollama', 'ssh_tunnel_ollama') or not isinstance(model, str):
        return False
    result_path = Path(execution_directory) / 'result.json'
    try:
        import hashlib
        evidence_hash = hashlib.sha256(result_path.read_bytes()).hexdigest()
        inference_seconds = 0.0
        generation_seconds = 0.0
        events_path = Path(execution_directory) / 'events.jsonl'
        if events_path.is_file():
            for line in events_path.read_text(encoding='utf-8').splitlines():
                try:
                    event = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if type(event.get('step')) is int and type(event.get('seconds')) in (int, float):
                    inference_seconds += max(0.0, float(event['seconds']))
                    duration = (event.get('timings') or {}).get('eval_duration')
                    if type(duration) in (int, float) and duration > 0:
                        generation_seconds += duration / 1_000_000_000
        task_categories = routing.get('task_classification', {}).get('categories', [])
        metrics = result.get('model_metrics') or {}
        observed_at = datetime.now(timezone.utc).isoformat()
        record = {
            'schema_version': 1,
            'provider_id': provider,
            'model_id': model,
            'model_digest': result.get('model_digest'),
            'task_category': task_categories[0] if task_categories else 'general_reasoning',
            'test_or_benchmark_id': 'verified_test_receipts',
            'latency_ms': max(0, int(inference_seconds * 1000)),
            'tokens_per_second': (
                metrics.get('completion_tokens', 0) / generation_seconds
                if generation_seconds > 0 else None
            ),
            'context_tokens': int(metrics.get('max_prompt_tokens', 0)),
            'outcome': 'success' if status == 'passed' else 'failure',
            'verification': 'test_receipt',
            'timestamp': observed_at,
            'evidence_ref': {
                'kind': 'run_result',
                'uri': str(result_path.relative_to(HUB)).replace('\\', '/'),
                'sha256': evidence_hash,
                'observed_at': observed_at,
            },
        }
        append_performance_evidence(registry_path, record)
        return True
    except (OSError, ValueError, ModelRegistryError):
        return False


def _may_defer_to_configured_fallback(route, provider_id, model_id, policy):
    if policy.get('mode', 'disabled') == 'disabled':
        return False
    substitutions = policy.get('substitutions', [])
    if not any(row.get('from_provider') == provider_id
               and row.get('from_model') in (model_id, '*')
               for row in substitutions):
        return False
    for candidate in route.get('candidate_models', []):
        if (candidate.get('provider_id') == provider_id
                and candidate.get('model_id') == model_id):
            reasons = candidate.get('excluded_reasons', [])
            return reasons == ['model_unavailable']
    errors = route.get('provider_discovery_errors', [])
    return any(error.get('provider_id') == provider_id and error.get('code') in {
        'unavailable', 'connection_lost', 'service_unavailable', 'timeout',
        'tunnel_unavailable', 'tunnel_timeout', 'model_missing',
    } for error in errors) or not any(
        row.get('provider_id') == provider_id and row.get('model_id') == model_id
        for row in route.get('candidate_models', [])
    )


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
    parser.add_argument('--provider', choices=['ollama', 'ssh_tunnel_ollama'])
    parser.add_argument('--routing-mode', choices=['configured', 'local_only', 'remote_allowed'], default='configured')
    parser.add_argument('--role', help='Task-specific agent role for routing; does not grant tools or permissions.')
    parser.add_argument('--context', type=int, default=16384)
    parser.add_argument('--max-context', type=int, default=32768)
    parser.add_argument('--max-memory-gib', type=float)
    parser.add_argument('--minimum-capability', action='append', default=[], metavar='CAPABILITY=0..1')
    parser.add_argument('--preferred-model', action='append', default=[])
    parser.add_argument('--exclude-model', action='append', default=[])
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
                    fallback_policy = config['inference'].get(
                        'fallback', {'mode': 'disabled', 'substitutions': []}
                    )
                    if args.context < 1 or args.max_context < args.context or args.max_context > 131072:
                        raise AgentError('Requested and maximum context values are invalid.')
                    minimums = parse_capability_minimums(args.minimum_capability)
                    specs = provider_specs(config, args.endpoint, args.routing_mode, args.provider)
                    candidates, discovery_errors = discover_candidates(specs)
                    registry_path = PRIVATE / 'model-capabilities.json'
                    evidence_path = PRIVATE / 'model-performance.jsonl'
                    registry = load_registry(registry_path)
                    performance = read_recent_performance(evidence_path)
                    explicit_model = None if args.model == 'auto' else args.model
                    route = route_model(
                        task, candidates, registry, performance_evidence=performance,
                        requested_context=args.context, max_context=args.max_context,
                        provider_mode=args.routing_mode,
                        authorized_providers=tuple(specs),
                        provider_filter=args.provider,
                        minimum_capabilities=minimums,
                        preferred_models=args.preferred_model,
                        excluded_models=args.exclude_model,
                        conservative_default=(config['inference']['model'] if args.model == 'auto' else None),
                        model_override=explicit_model,
                        agent_role=args.role,
                        resource_limits=({'max_memory_gib': args.max_memory_gib}
                                         if args.max_memory_gib is not None else None),
                        provider_errors=discovery_errors,
                    )
                    if route['selected'] is None:
                        # Preserve Milestone 2 preflight fallback for an explicitly
                        # configured provider/model pair; this is not a router claim
                        # that the unavailable provider is usable.
                        configured_provider = args.provider or config['inference']['provider']
                        if (args.routing_mode == 'configured'
                                and _may_defer_to_configured_fallback(
                                    route, configured_provider, args.model, fallback_policy)):
                            provider_name = configured_provider
                            selected_model = args.model
                            route['status'] = 'uncertain'
                            route['selected'] = {'provider_id': provider_name,
                                                 'model_id': selected_model,
                                                 'model_digest': None}
                            route['reason'] = 'Configured model retained for the independent preflight fallback policy.'
                        else:
                            raise AgentError('No available model satisfies the routing constraints. Review the routing explanation and model registry.')
                    else:
                        provider_name = route['selected']['provider_id']
                        selected_model = route['selected']['model_id']
                    selected_spec = specs[provider_name]
                    selected_endpoint = selected_spec['endpoint']
                    selected_profile = selected_spec.get('profile')
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
                        parent/'execution',selected_endpoint,
                        writes=writes,
                        conversation=conversation,
                        provider_name=provider_name,
                        context=args.context,
                        test_config=test_config,
                        assistant_root=args.assistant_root,
                        assistant_write_paths=assistant_write_paths,
                        provider_profile=selected_profile,
                        remote_profile=config['inference'].get('remote'),
                        fallback_policy=fallback_policy,
                        fallback_approval=(approve_provider_fallback
                                           if fallback_policy['mode'] == 'ask' else None),
                        routing_decision=route,
                    )
            finally:
                atomic_write(parent/'post-run.json',encoded({'state':project.state(),'memory':packet['memory_snapshot'],
                              'task':task,'run':str(parent/'execution'),'logical_project_id':logical_project_id,
                              'assistant_location': packet.get('assistant_location')}))
            result.setdefault('project_id', project.identity)
            result.setdefault('commands', [])
            result['logical_project_id'] = logical_project_id
            atomic_write(parent/'execution'/'result.json', encoded(result))
            _record_verified_performance(
                result, parent/'execution', PRIVATE/'model-performance.jsonl'
            )
            print(json.dumps({'run':str(parent),'logical_project_id':logical_project_id,
                              'status':result['status'],'summary':result.get('summary'),
                              'operator_policy':result['operator_policy'],'verified_test_status':result['verified_test_status'],
                              'routing_decision': result.get('routing_decision')},indent=2));return
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
