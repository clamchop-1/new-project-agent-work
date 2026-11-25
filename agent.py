"""Human-facing launcher and shared-memory bridge; no global configuration writes."""
import argparse
from datetime import datetime, timezone
import getpass
import json
from pathlib import Path
import subprocess
import sys
import socket
import uuid
import webbrowser

from agent_core import AgentError, Project, atomic_write, encoded, digest, exclusive, save_handoff, file_hash
from assistant_files import AssistantFiles
from local_agent import (HUB, TOOLS, run_agent, RESTRICTED_EVALUATOR_SHA256,
                         RESTRICTED_ACCEPTANCE_SPEC_ID, create_inference_provider)
from execution_recovery import RecoveryStore
from model_router import discover_candidates, route_model, read_recent_performance
from model_registry import (ModelRegistryError, append_performance_evidence,
                            load_registry)
from conversation_session import load_session
from chat_conversations import ConversationStore
from agent_config import (config_path, default_projects, load_config,
                          legacy_name_map, selector_map, DEFAULT_ENDPOINT,
                          validate_endpoint)
from temporary_project_registry import (
    DEFAULT_TTL_SECONDS, canonical_worktree,
    inspect as inspect_temporary_project,
    register as register_temporary_project,
    revoke as revoke_temporary_project,
)

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


def _run_collaborative_proposal(workflow, role_models, config, provider_name,
                               routing_mode='configured', endpoint=None,
                               timeout=120.0, attempts=2, cancel_event=None):
    if type(timeout) not in (int, float) or not 1 <= timeout <= 600:
        raise AgentError('Per-role coding timeout must be between one and 600 seconds.')
    if type(attempts) is not int or not 1 <= attempts <= 3:
        raise AgentError('Coder proposal attempts must be between one and three.')
    specs = provider_specs(config, endpoint or config['inference']['endpoint'],
                           routing_mode, provider_name)
    selected = specs[provider_name]
    PRIVATE.mkdir(parents=True, exist_ok=True)
    with exclusive(PRIVATE / 'inference.lock', 'coding-collaboration'):
        provider = create_inference_provider(provider_name, selected['endpoint'], selected.get('profile'))
        try:
            provider.health_check()
            for model in dict.fromkeys(role_models.values()):
                provider.model(model)
            return workflow.generate_collaborative(provider, role_models=role_models,
                provider_id=provider_name, timeout=timeout, max_attempts=attempts,
                cancel_event=cancel_event)
        finally:
            provider.close()


def _coding_workflow_cli(args, config):
    from coding_workflow import CodingWorkflow
    if args.action in ('coding-dashboard', 'local-chat'):
        chat_only = args.action == 'local-chat'
        if args.project or args.temporary_project:
            raise AgentError('Local chat and coding dashboard do not accept production project selectors.')
        if chat_only and (args.dashboard_root or args.coding_path):
            raise AgentError('Local chat starts without project access or coding file permissions.')
        if not chat_only and (not args.dashboard_root or not args.coding_path):
            raise AgentError('Coding dashboard requires explicit disposable fixtures and exact file paths.')
        if type(args.chat_timeout) is not int or not 30 <= args.chat_timeout <= 180:
            raise AgentError('Local chat timeout must be between 30 and 180 seconds.')
        if type(args.dashboard_port) is not int or not 1024 <= args.dashboard_port <= 65535:
            raise AgentError('Dashboard port must be between 1024 and 65535.')
        probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            probe.settimeout(0.2)
            if probe.connect_ex(('127.0.0.1', args.dashboard_port)) == 0:
                raise AgentError(f'Loopback dashboard port {args.dashboard_port} is already in use; existing processes were left untouched.')
        finally:
            probe.close()
        from coding_dashboard import CodingDashboard
        models = tuple(args.dashboard_model or ['qwen3-coder:30b', 'qwen3:8b'])
        provider_name = args.provider or config['inference']['provider']
        runner = (lambda workflow, role_models, cancel, limits: _run_collaborative_proposal(
            workflow, role_models, config, provider_name, args.routing_mode,
            args.endpoint, limits['timeout'], limits['attempts'], cancel)) if not chat_only else None
        from model_catalog import discover_model_catalog, save_catalog
        if args.provider:
            chat_specs = provider_specs(config, args.endpoint, 'configured', provider_name)
        else:
            chat_specs = provider_specs(config, args.endpoint, 'remote_allowed')
        chat_host_ids = {
            'ollama': 'windows',
            'ssh_tunnel_ollama': config['inference'].get('remote', {}).get('profile_id', 'mac_studio'),
        }
        catalog_path = PRIVATE / 'model-catalog.json'

        def refresh_chat_models():
            catalog = discover_model_catalog(chat_specs, host_ids=chat_host_ids)
            save_catalog(catalog_path, catalog)
            return catalog

        provider_status = {'status': 'unavailable', 'checked_at': None, 'installed_models': [],
                           'provider': provider_name, 'providers': [], 'discovery_errors': []}
        chat_catalog = {'models': [], 'providers': [], 'errors': [], 'observed_at': None}
        try:
            chat_catalog = refresh_chat_models()
            primary = next((row for row in chat_catalog['providers']
                            if row['provider_id'] == provider_name), None)
            installed = [row['model_id'] for row in chat_catalog['models']
                         if row['provider_id'] == provider_name and row['available']]
            provider_status = {'status': 'connected' if primary and primary['available'] else 'unavailable',
                'checked_at': chat_catalog['observed_at'], 'provider': provider_name,
                'installed_models': installed, 'providers': chat_catalog['providers'],
                'discovery_errors': chat_catalog['errors'],
                'catalog_observed_at': chat_catalog['observed_at']}
        except (AgentError, OSError, ValueError) as exc:
            provider_status.update({'checked_at': datetime.now(timezone.utc).isoformat(),
                                    'error': type(exc).__name__})
        default_chat_model = next((row['model_key'] for row in chat_catalog.get('models', [])
                                   if row['provider_id'] == provider_name
                                   and row['model_id'] == config['inference']['model']
                                   and row['authorized'] and row['available']
                                   and row['compatible']), None)
        def chat_runner(messages, model, cancel_event, on_token):
            if not isinstance(model, dict):
                raise AgentError('Chat model selection has no provider identity.')
            selected_provider = model.get('provider_id')
            selected_model = model.get('model_id')
            spec = chat_specs.get(selected_provider)
            if spec is None or not isinstance(selected_model, str):
                raise AgentError('Chat model provider is no longer authorized for this session.')
            PRIVATE.mkdir(parents=True, exist_ok=True)
            with exclusive(PRIVATE / 'inference.lock', 'local-chat'):
                provider = create_inference_provider(selected_provider, spec['endpoint'], spec.get('profile'))
                try:
                    provider.model(selected_model)
                    provider.chat_stream(selected_model, messages, 16384, {'temperature': 0.2}, on_token,
                                         cancel_event=cancel_event, timeout=args.chat_timeout)
                finally:
                    provider.close()
        chat_store = ConversationStore(HUB)
        knowledge_store = None
        project_intelligence = None
        if chat_only:
            from knowledge_documents import DocumentKnowledgeLibrary
            from knowledge_embeddings import LocalSentenceTransformerProvider
            embedding_provider = None
            try:
                embedding_provider = LocalSentenceTransformerProvider()
            except (AgentError, OSError, ValueError, KeyError, TypeError):
                pass
            knowledge_store = DocumentKnowledgeLibrary(Project(HUB),
                embedding_provider=embedding_provider)
            from project_intelligence import ProjectIntelligence
            project_intelligence = ProjectIntelligence(Project(HUB),
                embedding_provider=embedding_provider)
        dashboard = CodingDashboard(args.dashboard_root, allowed_paths=tuple(args.coding_path),
                                    allowed_models=models, runner=runner,
                                    test_profile=args.coding_test_profile,
                                    provider_status=provider_status,
                                    chat_store=chat_store, chat_runner=chat_runner,
                                    chat_models=chat_catalog.get('models', []),
                                    chat_model_refresher=refresh_chat_models,
                                    chat_default_model=default_chat_model,
                                    knowledge_store=knowledge_store,
                                    project_intelligence=project_intelligence)
        server = dashboard.start(port=args.dashboard_port)
        print('Inference provider check: ' + provider_status['status']
              + (' (cached at ' + provider_status['checked_at'] + ')' if provider_status.get('checked_at') else ''))
        installed = provider_status.get('installed_models', [])
        print('Chat model inventory: ' + str(len(chat_catalog.get('models', [])))
              + ' discovered across authorized Ollama providers')
        print('Configured provider models confirmed: '
              + (', '.join(installed) if installed else 'not confirmed'))
        print('Local dashboard: http://127.0.0.1:' + str(server.server_port))
        print('One-run dashboard bearer token: ' + dashboard.token)
        if chat_only and not args.no_browser_open:
            try:
                webbrowser.open('http://127.0.0.1:' + str(server.server_port) + '/', new=2)
            except Exception:
                print('Could not open a browser automatically; use the local URL above.')
        if not chat_only:
            print('Developer workflow view: http://127.0.0.1:' + str(server.server_port) + '/developer')
            print('Approval/application remain available only through coding-approve and coding-apply.')
        try:
            server.serve_forever(poll_interval=0.25)
        except KeyboardInterrupt:
            print('Dashboard stopped by operator.')
        finally:
            server.shutdown(); server.server_close()
            if knowledge_store is not None:
                knowledge_store.close()
        return
    if not args.coding_root or args.project or args.temporary_project:
        raise AgentError('Coding actions require only an exact --coding-root disposable repository.')
    project = Project(args.coding_root)
    if args.action == 'coding-collaborate':
        if not args.coding_path or (not args.task and not args.task_file):
            raise AgentError('Collaborative proposal requires a task and one or more exact --coding-path values.')
        task = args.task if args.task is not None else args.task_file.read_text(encoding='utf-8')
        coder_model = args.coder_model or args.model
        role_models = {'planner': args.planner_model or coder_model,
                       'coder': coder_model, 'reviewer': args.reviewer_model or coder_model}
        workflow = CodingWorkflow(project, task, tuple(args.coding_path))
        provider_name = args.provider or config['inference']['provider']
        proposal = _run_collaborative_proposal(workflow, role_models, config, provider_name,
            args.routing_mode, args.endpoint, args.coding_timeout, args.coding_attempts)
        print(json.dumps(proposal, ensure_ascii=True, indent=2))
        return
    if args.action == 'coding-propose':
        if not args.coding_path or (not args.task and not args.task_file):
            raise AgentError('Coding proposal requires a task and one or more exact --coding-path values.')
        task = args.task if args.task is not None else args.task_file.read_text(encoding='utf-8')
        workflow = CodingWorkflow(project, task, tuple(args.coding_path))
        provider_name = args.provider or config['inference']['provider']
        specs = provider_specs(config, args.endpoint, args.routing_mode, provider_name)
        selected = specs[provider_name]
        PRIVATE.mkdir(parents=True, exist_ok=True)
        with exclusive(PRIVATE / 'inference.lock', 'coding-workflow'):
            provider = create_inference_provider(provider_name, selected['endpoint'], selected.get('profile'))
            try:
                provider.health_check()
                provider.model(args.model)
                proposal = workflow.generate(provider, args.model, provider_id=provider_name,
                                             max_attempts=args.coding_attempts)
            finally:
                provider.close()
        print(json.dumps(proposal, ensure_ascii=True, indent=2))
        return
    if not args.workflow_id:
        raise AgentError('Coding workflow action requires --workflow-id.')
    workflow = CodingWorkflow.load(project, args.workflow_id,
        recover_ambiguous=args.action != 'coding-status')
    if args.action == 'coding-status':
        result = dict(workflow.row)
        patch_path = workflow.run_dir / 'approved.patch'
        if patch_path.is_file():
            result['patch_sha256'] = file_hash(patch_path)
        from coding_status import status_projection
        from coding_dashboard import CodingDashboard
        completion_verified = CodingDashboard._verified_test_receipt(
            project, workflow.run_id, result.get('test_result'))
        application_verified = CodingDashboard._verified_post_images(project, result)
        recovery_status = CodingDashboard._recovery_status(project, workflow.run_id)
        candidate_status = CodingDashboard._candidate_status(project, workflow.run_id)
        result['status_summary'] = status_projection(
            result, patch_sha256=result.get('patch_sha256'),
            completion_verified=(result.get('state') == 'completed' and completion_verified
                                 and application_verified and recovery_status.get('status') == 'complete'),
            candidate_status=candidate_status, recovery_status=recovery_status.get('status'))
        print(json.dumps(result, ensure_ascii=True, indent=2))
        return
    if args.action in ('coding-approve', 'coding-reject'):
        patch_path = workflow.run_dir / 'approved.patch'
        if not patch_path.is_file():
            raise AgentError('Reviewed patch evidence is missing.')
        print(json.dumps({'workflow_id': workflow.run_id,
            'repository': str(project.root), 'baseline_commit': workflow.baseline['commit'],
            'patch_sha256': workflow.row['patch_sha256'],
            'expected_post_images': workflow.row['expected_post_images'],
            'allowed_paths': workflow.row['allowed_paths'],
            'validation_report': workflow.row.get('validation_report'),
            'planner': workflow.row.get('planner_output'),
            'review': workflow.row.get('review_output'),
            'review_advisory_only': workflow.row.get('review_advisory_only', False),
            'role_models': workflow.row.get('role_models')}, indent=2))
        print(patch_path.read_text(encoding='utf-8'))
        sha = input('Type the exact patch SHA-256 to approve/reject this proposal: ').strip()
        if args.action == 'coding-reject':
            workflow.reject(patch_sha256=sha)
            print(json.dumps({'state': workflow.row['state'], 'workflow_id': workflow.run_id}))
            return
        destination = input('Type the exact repository path to approve: ').strip()
        fixed_command = workflow.row['test_command'].replace('→', '->')
        print('Fixed test command: ' + fixed_command)
        test_command = input('Type the fixed test command exactly as shown: ').strip()
        operator = args.coding_operator or getpass.getuser()
        approval = workflow.approve(patch_sha256=sha, repository=destination,
                                    operator=operator, test_command=test_command)
        print(json.dumps({'state': workflow.row['state'], 'approval': approval}, ensure_ascii=True, indent=2))
        return
    if args.action == 'coding-apply':
        operator = args.coding_operator or getpass.getuser()
        print(json.dumps(workflow.apply_and_test(operator=operator), ensure_ascii=True, indent=2))


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
    parser.add_argument('action', choices=['config','inspect','run','approve','history','prepare-bridges','prepare-handoff','save-handoff','recovery-inspect','recovery-reconcile',
                                           'temporary-register','temporary-inspect','temporary-revoke',
                                           'coding-propose','coding-collaborate','coding-dashboard',
                                           'local-chat',
                                           'coding-approve','coding-reject','coding-apply','coding-status'])
    parser.add_argument('--project', choices=selectors)
    parser.add_argument('--temporary-project', help='Select one active temporary registration by its generated ID.')
    parser.add_argument('--temporary-project-root', type=Path, help='Absolute Git worktree root for explicit temporary registration.')
    parser.add_argument('--temporary-project-ttl-seconds', type=int, default=DEFAULT_TTL_SECONDS)
    parser.add_argument('--task');parser.add_argument('--task-file',type=Path)
    parser.add_argument('--coding-root', type=Path, help='Exact fresh disposable Milestone 3 repository root.')
    parser.add_argument('--dashboard-root', type=Path, action='append', default=[],
                        help='One explicitly authorized fresh Milestone 4 disposable repository; repeat to add fixtures.')
    parser.add_argument('--coding-path', action='append', default=[], help='Exact relative path authorized for a coding patch.')
    parser.add_argument('--coding-attempts', type=int, default=3, help='Bound proposal generation/correction attempts (1..3).')
    parser.add_argument('--coding-test-profile', choices=['clamp_percent_v1', 'stats_multi_module_v1'],
                        default='clamp_percent_v1', help='Select one exact existing trusted disposable test fixture.')
    parser.add_argument('--coding-timeout', type=float, default=120.0,
                        help='Per-role inference timeout in seconds (1..600).')
    parser.add_argument('--planner-model', help='Installed local model for the read-only Planner role.')
    parser.add_argument('--coder-model', help='Installed local model for the patch-generating Coder role.')
    parser.add_argument('--reviewer-model', help='Installed local model for the independent read-only Reviewer role.')
    parser.add_argument('--dashboard-model', action='append', default=[], choices=['qwen3-coder:30b', 'qwen3:8b'],
                        help='Model identifier permitted in the dashboard role selectors; repeat to allow more models.')
    parser.add_argument('--dashboard-port', type=int, default=52718,
                        help='Loopback dashboard port; an occupied port is left untouched and reported.')
    parser.add_argument('--chat-timeout', type=int, default=180,
                        help='Maximum local chat response time in seconds (30..180).')
    parser.add_argument('--no-browser-open', action='store_true',
                        help='Keep chat in API-only mode for a verified local test run.')
    parser.add_argument('--workflow-id', help='Persisted coding workflow identifier.')
    parser.add_argument('--coding-operator', help='Operator identity for patch approval/application; defaults to current Windows account.')
    parser.add_argument('--assistant-root', type=Path,
                        help='Assistant file root, usually the PowerShell current directory; reads are broad and writes require exact paths.')
    parser.add_argument('--assistant-write-path', action='append', default=[],
                        help='Authorize one exact relative text-file path for this task only.')
    parser.add_argument('--model',default=config['inference']['model']);parser.add_argument('--endpoint',default=config['inference']['endpoint'])
    parser.add_argument('--provider', choices=['ollama', 'ssh_tunnel_ollama'])
    parser.add_argument('--routing-mode', choices=['configured', 'local_only', 'remote_allowed'], default='configured')
    parser.add_argument('--role', help='Task-specific agent role for routing; does not grant tools or permissions.')
    parser.add_argument('--max-model-calls', type=int, help='Cap all chat attempts for this run.')
    parser.add_argument('--max-source-mutations', type=int, choices=(1,),
                        help='Limit this run to one attempted mutation of one approved project path.')
    parser.add_argument('--transport-retries', type=int, choices=(0, 1), help='Set the session-wide timeout retry budget.')
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
    parser.add_argument(
        '--authorize-restricted-evaluation', action='store_true',
        help='Separately authorize one restricted calculator evaluation for this run.',
    )
    parser.add_argument(
        '--restricted-source-path',
        help='Exact project-relative Python source path covered by the separate evaluation authorization.',
    )
    parser.add_argument('--resume-held-project',action='store_true')
    parser.add_argument('--permit',type=Path);parser.add_argument('--run-directory',type=Path)
    parser.add_argument('--recovery-run', help='Recovery journal ID for inspection, reconciliation, or continuation.')
    parser.add_argument('--recovery-action',
                        choices=['accept', 'abort', 'restore', 'confirm-command-stopped'])
    parser.add_argument('--review-sha256')
    parser.add_argument('--proposal',type=Path);parser.add_argument('--authorize-memory-save',action='store_true')
    parser.add_argument('--agent',choices=['codex','claude','qwen'],default='qwen')
    parser.add_argument('--history-index',type=int);parser.add_argument('--authorize-history',action='store_true')
    parser.add_argument('--conversation', action='store_true')
    parser.add_argument('--completion-policy',
                        choices=['conversational', 'read_only_analysis',
                                 'tool_required', 'file_modification'],
                        help='Host-selected completion contract; write permits default to file_modification.')
    parser.add_argument('--require-operation', action='append', default=[],
                        choices=sorted(tool['function']['name'] for tool in TOOLS
                                       if tool['function']['name'] != 'finish'),
                        help='Require this registered operation before finish; repeat for multiple requirements.')
    args=parser.parse_args()
    if args.action.startswith('coding-') or args.action == 'local-chat':
        _coding_workflow_cli(args, config)
        return
    if args.max_source_mutations is not None and (
            args.action != 'run' or args.recovery_run or args.assistant_write_path
            or args.allow_pinned_tests):
        parser.error('Source mutation bounds require a fresh run without assistant writes or pinned tests')
    if args.max_model_calls is not None and args.max_model_calls < 1:
        parser.error('--max-model-calls must be positive')
    if args.action != 'run' and (args.max_model_calls is not None or args.transport_retries is not None):
        parser.error('Execution bounds apply only to run')

    execution_bounds = {name: getattr(args, name) for name in
                        ('max_model_calls', 'transport_retries', 'max_source_mutations')
                        if getattr(args, name) is not None}
    default_project = 'KronosStage' if 'KronosStage' in configured_projects else next(iter(configured_projects))
    if args.action == 'temporary-register':
        if args.temporary_project or args.project or not args.temporary_project_root:
            raise AgentError('Temporary registration requires only an absolute temporary project root.')
        protected_roots = [HUB, *configured_projects.values()]
        candidate, _ = canonical_worktree(args.temporary_project_root, protected_roots)
        confirmation = input(
            'Register this disposable Git worktree for a bounded session? Type its canonical root exactly: '
        ).strip()
        if confirmation != str(candidate.root):
            raise AgentError('Temporary project registration was not explicitly confirmed.')
        record = register_temporary_project(
            candidate.root, protected_roots=protected_roots,
            ttl_seconds=args.temporary_project_ttl_seconds, confirmed=True,
        )
        print(json.dumps({key: record[key] for key in (
            'registration_id', 'project_id', 'project_root', 'worktree',
            'created_at', 'expires_at', 'allowed_source_path', 'capabilities',
            'write_authorized', 'test_execution_authorized')}, indent=2))
        return
    if args.action in ('temporary-inspect', 'temporary-revoke'):
        if not args.temporary_project or args.temporary_project_root or args.project:
            raise AgentError('Select exactly one temporary registration ID.')
        result = (inspect_temporary_project(args.temporary_project)
                  if args.action == 'temporary-inspect'
                  else revoke_temporary_project(args.temporary_project))
        print(json.dumps(result, indent=2))
        return
    if args.temporary_project and args.project:
        raise AgentError('Select either a configured project or one temporary registration.')
    if not args.temporary_project:
        args.project = args.project or default_project
    elif args.action not in ('inspect', 'approve', 'run'):
        raise AgentError('Temporary registrations are supported only for inspect, approve, and run.')

    if args.authorize_restricted_evaluation and args.action != 'run':
        raise AgentError('Restricted evaluation authorization is available only for a run action.')
    if args.restricted_source_path and not args.authorize_restricted_evaluation:
        raise AgentError('A restricted source path requires explicit evaluation authorization.')
    if args.authorize_restricted_evaluation and not args.restricted_source_path:
        raise AgentError('Explicit restricted evaluation authorization requires one exact source path.')
    if ('evaluate_restricted_source' in args.require_operation
            and not args.authorize_restricted_evaluation):
        raise AgentError('Restricted evaluation is a separate authorization from operation requirements.')

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
    temporary_registration = None
    if args.temporary_project:
        temporary_registration = inspect_temporary_project(args.temporary_project)
        logical_project_id = 'temporary-' + temporary_registration['registration_id']
        selected_project_name = 'temporary-disposable'
        project = Project(temporary_registration['project_root'])
        if (project.identity != temporary_registration['project_id']
                or project.root != Path(temporary_registration['project_root'])):
            raise AgentError('Temporary project identity changed after registration.')
    else:
        logical_project_id = selectors[args.project]
        selected_project_name = config['projects'][logical_project_id]['name']
        project=Project(configured_projects[selected_project_name])
    restricted_evaluation_authorization = None
    if args.authorize_restricted_evaluation:
        target = project.path(args.restricted_source_path)
        canonical = target.relative_to(project.root).as_posix()
        if canonical != args.restricted_source_path or target.suffix.casefold() != '.py':
            raise AgentError('Restricted evaluation requires an exact canonical project-relative Python source path.')
        restricted_evaluation_authorization = {
            'project_id': project.identity,
            'project_root': str(project.root),
            'source_path': canonical,
            'evaluator_sha256': RESTRICTED_EVALUATOR_SHA256,
            'acceptance_spec_id': RESTRICTED_ACCEPTANCE_SPEC_ID,
        }
        if temporary_registration is not None:
            if canonical != temporary_registration['allowed_source_path']:
                raise AgentError('Restricted evaluation path is outside the temporary registration scope.')
            restricted_evaluation_authorization['registration_id'] = temporary_registration['registration_id']
            restricted_evaluation_authorization['worktree_identity'] = temporary_registration['worktree']['identity']
    recovery = RecoveryStore(project)
    if args.action == 'recovery-inspect':
        print(json.dumps(recovery.inspect(args.recovery_run), indent=2))
        return
    if args.action == 'recovery-reconcile':
        print(json.dumps(recovery.reconcile(args.recovery_run, args.review_sha256,
                                            args.recovery_action), indent=2))
        return
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
            if state['status'].strip() and not args.recovery_run:raise AgentError('Existing worktree changes must be reviewed/isolated before edit authorization.')
            write_paths = validate_write_paths(selected_project_name, args.write_path)
            for path in write_paths:project.path(path)
            if args.recovery_run:
                old, _ = recovery.load(args.recovery_run)
                with recovery.locked():
                    recovery.assert_clear()
                    recovery.resume_scope(args.recovery_run, write_paths, (), old['scope']['tools'], None)
            folder=new_directory('approvals')
            permit={'project_id':project.identity,'state':state,'memory':project.memory_snapshot(),
                    'task':args.task_file.read_text(encoding='utf-8'),'write_paths':list(write_paths),'used':False}
            if temporary_registration is not None:
                permit['temporary_registration_id'] = temporary_registration['registration_id']
                permit['temporary_worktree_identity'] = temporary_registration['worktree']['identity']
            if args.recovery_run:
                permit['recovery_run'] = args.recovery_run
            atomic_write(folder/'permit.json',encoded(permit))
            print('Single-task edit permit: '+str(folder/'permit.json'));return
        if args.action=='run':
            with recovery.locked():
                recovery.assert_clear()
            task=args.task or (args.task_file.read_text(encoding='utf-8') if args.task_file else
                'Perform a read-only takeover. Read relevant source and explain architecture, newest hold/task, unfinished work and next safe step. Do not edit or resume work.')
            writes=[]
            if args.recovery_run and assistant_write_paths:
                raise AgentError('Recovery continuation cannot write to assistant locations.')
            if args.permit:
                permit_path=args.permit.resolve()
                if not permit_path.is_relative_to(PRIVATE/'approvals'):raise AgentError('Use an operator-created permit in this launcher workspace.')
                permit=json.loads(permit_path.read_bytes())
                if permit['used'] or permit['project_id']!=project.identity or permit['state']!=project.state() or permit['memory']!=project.memory_snapshot():
                    raise AgentError('Permit is used, cross-project or stale.')
                if not args.task and not args.task_file:task=permit['task']
                if task!=permit['task']:raise AgentError('Approved task changed.')
                if permit.get('recovery_run') != args.recovery_run:
                    raise AgentError('The write permit does not match this recovery checkpoint.')
                expected_registration = (temporary_registration['registration_id']
                                         if temporary_registration else None)
                if permit.get('temporary_registration_id') != expected_registration:
                    raise AgentError('The write permit does not match this project registration.')
                if (temporary_registration is not None
                        and permit.get('temporary_worktree_identity')
                        != temporary_registration['worktree']['identity']):
                    raise AgentError('The write permit does not match this Git worktree identity.')
                writes=validate_write_paths(selected_project_name, permit['write_paths'])
                if args.max_source_mutations is not None and len(writes) != 1:
                    raise AgentError('A bounded write permit must name one exact project path.')
                if (args.max_source_mutations is not None and temporary_registration is not None
                        and writes != (temporary_registration['allowed_source_path'],)):
                    raise AgentError('A bounded temporary project permit must name its registered source path.')
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

                    completion_policy = args.completion_policy or (
                        'file_modification' if writes or assistant_write_paths
                        else 'tool_required'
                    )

                    recovery_options = {}
                    if args.recovery_run:
                        previous, _ = recovery.load(args.recovery_run)
                        recovery_options.update(resume_from=args.recovery_run,
                                                allowed_tools=previous['scope']['tools'])
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
                        completion_policy=completion_policy,
                        required_operations=tuple(args.require_operation),
                        restricted_evaluation_authorization=restricted_evaluation_authorization,
                        temporary_project_registration=temporary_registration,
                        **execution_bounds,
                        **recovery_options,
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
                              'completion_classification':result.get('completion_classification'),
                              'host_completion_evidence':result.get('host_completion_evidence'),
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
