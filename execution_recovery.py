"""Windows broker evidence for interrupted runs. Recovery never dispatches tools."""
from contextlib import contextmanager
import json
import os
from pathlib import Path
import re
import uuid

from agent_core import AgentError, atomic_write, digest, encoded, file_hash


HUB = Path(__file__).resolve().parent


class RecoveryError(AgentError):
    """Execution cannot safely advance until the operator inspects the journal."""


def _owner_exited(pid):
    if type(pid) is not int or pid <= 0 or pid == os.getpid():
        return False
    if os.name != 'nt':
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return True
        except OSError:
            return False
        return False
    import ctypes
    from ctypes import wintypes
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    kernel.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
    kernel.OpenProcess.restype = wintypes.HANDLE
    kernel.WaitForSingleObject.argtypes = (wintypes.HANDLE, wintypes.DWORD)
    kernel.WaitForSingleObject.restype = wintypes.DWORD
    kernel.CloseHandle.argtypes = (wintypes.HANDLE,)
    handle = kernel.OpenProcess(0x00100000, False, pid)
    if not handle:
        return ctypes.get_last_error() == 87  # ERROR_INVALID_PARAMETER for absent PID
    try:
        return kernel.WaitForSingleObject(handle, 0) == 0
    finally:
        kernel.CloseHandle(handle)


def checkpoint(project):
    state = project.state()
    return {**{key: state[key] for key in ('commit', 'branch', 'files', 'diff_sha256', 'status')},
            'index_sha256': digest(project.git('diff', '--cached', '--binary').encode())}


def _plain_path(path):
    if path.is_symlink() or getattr(path.lstat(), 'st_file_attributes', 0) & 0x400:
        raise RecoveryError('Recovery storage must not use links or reparse points.')
    if path.is_file() and path.stat().st_nlink != 1:
        raise RecoveryError('Recovery files must not use hard links.')


class RecoveryStore:
    def __init__(self, project):
        self.project = project
        if not isinstance(project.identity, str) or not re.fullmatch(r'[a-f0-9]{64}', project.identity):
            raise RecoveryError('Recovery requires an exact project identity.')
        fixtures = HUB / '.local' / 'tests'
        if project.root.parent.parent == fixtures:
            self.root = project.root.parent / 'execution-recovery'
        else:
            self.root = HUB / '.local' / 'execution-recovery' / project.identity

    @contextmanager
    def locked(self):
        for directory in (HUB / '.local', self.root.parent, self.root):
            directory.mkdir(exist_ok=True)
            _plain_path(directory)
        path = self.root / 'lease'
        if path.exists() or path.is_symlink():
            _plain_path(path)
        with path.open('a+b') as handle:
            handle.seek(0)
            try:
                if os.name == 'nt':
                    import msvcrt
                    msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError as exc:
                raise RecoveryError('Another process owns the project recovery lease.') from exc
            yield

    def path(self, run_id):
        if not isinstance(run_id, str) or not re.fullmatch(r'[a-f0-9]{32}', run_id):
            raise RecoveryError('Invalid recovery run ID.')
        return self.root / (run_id + '.json')

    def backup_path(self, run_id, invocation):
        if not re.fullmatch(r'[a-f0-9]{32}', invocation):
            raise RecoveryError('Invalid journal operation ID.')
        return self.path(run_id).with_suffix('.' + invocation + '.backup')

    def load(self, run_id):
        path = self.path(run_id)
        try:
            _plain_path(path)
            raw = path.read_bytes()
            row = json.loads(raw)
            if (row['version'] != 1 or row['project_id'] != self.project.identity
                    or row['run_id'] != run_id or row['status'] not in {
                        'authorized', 'executing', 'interrupted', 'complete',
                        'failed', 'resumable', 'aborted',
                    } or not isinstance(row['operations'], list)
                    or not isinstance(row['checkpoint']['files'], dict)
                    or not isinstance(row['scope']['writes'], list)):
                raise ValueError
            for op in row['operations']:
                if (op['state'] not in {'executing', 'recorded', 'reconciled'}
                        or op['kind'] not in {'project_write', 'assistant_write', 'command'}
                        or not re.fullmatch(r'[a-f0-9]{32}', op['id'])
                        or (op['state'] == 'recorded' and not isinstance(op.get('evidence'), dict))):
                    raise ValueError
            pending = [op for op in row['operations'] if op['state'] == 'executing']
            if pending and (len(pending) != 1 or pending[0] != row['operations'][-1]
                            or row['status'] not in {'executing', 'interrupted'}):
                raise ValueError
            return row, digest(raw)
        except (OSError, ValueError, KeyError, TypeError) as exc:
            raise RecoveryError('Missing or invalid recovery journal. Preserve it for operator review.') from exc

    def assert_clear(self):
        for path in self.root.glob('*.json'):
            row, _ = self.load(path.stem)
            if row['status'] in {'authorized', 'executing', 'interrupted'}:
                raise RecoveryError('Project has an unresolved execution journal: ' + row['run_id'])

    def resume_scope(self, run_id, writes, assistant_writes, tools, test_config):
        row, _ = self.load(run_id)
        scope = row['scope']
        if row['status'] != 'resumable' or not self.inspect(run_id)['safe_to_reconcile']:
            raise RecoveryError('The accepted recovery checkpoint no longer verifies.')
        if not set(writes) <= set(scope['writes']) or assistant_writes:
            raise RecoveryError('Continuation exceeds the original write scope.')
        if scope['tools'] is not None and (tools is None or not set(tools) <= set(scope['tools'])):
            raise RecoveryError('Continuation exceeds the original tool grant.')
        if test_config and (str(Path(test_config).resolve()) != scope['test_config']
                            or file_hash(test_config) != scope.get('test_config_sha256')):
            raise RecoveryError('Continuation may only use the original pinned suite.')
        return row

    def _stale_launcher_locks(self, owner_pid):
        if not _owner_exited(owner_pid):
            return {}
        folder = HUB / '.local' / 'locks'
        paths = (folder / (self.project.identity + '.lock'), HUB / '.local' / 'inference.lock')
        checked = {}
        for path in paths:
            if not path.is_file():
                continue
            try:
                _plain_path(path.parent)
                _plain_path(path)
                raw = path.read_bytes()
                if json.loads(raw)['pid'] == owner_pid:
                    checked[path] = raw
            except (OSError, ValueError, KeyError, TypeError, RecoveryError):
                continue
        return checked

    def inspect(self, run_id):
        from local_agent import verify_test_receipts

        row, journal_hash = self.load(run_id)
        current = checkpoint(self.project)
        saved = row['checkpoint']
        operations = row['operations']
        pending = next((op for op in operations if op['state'] == 'executing'), None)
        multi_set = bool(row['scope'].get('approved_post_images'))
        write_hashes = {}
        for op in operations:
            if op['kind'] not in {'project_write', 'assistant_write'}:
                continue
            if op['kind'] == 'project_write':
                target = self.project.path(op['path'])
            else:
                from assistant_files import AssistantFiles
                target = AssistantFiles(row['scope']['assistant_root'])._path(op['path'])
            actual = file_hash(target) if target.is_file() else None
            checkpoint_hash = op['before'] if op.get('outcome') == 'matches_before' else op['after']
            write_hashes[(op['kind'], op['path'])] = {
                'kind': op['kind'], 'path': op['path'], 'before': op['before'],
                'after': op['after'], 'actual': actual,
                'checkpoint_sha256': checkpoint_hash,
                'is_regular_or_absent': target.is_file() or not target.exists(),
            }
        references = [op['evidence'] for op in operations
                      if op['kind'] == 'command' and op['state'] == 'recorded']
        tests, integrity = verify_test_receipts(references, self.project, row['output'])
        unchanged = current == saved
        if multi_set:
            post_images = row['scope'].get('approved_post_images', {})
            expected_paths = set(row['scope']['writes'])
            initial = row.get('initial_checkpoint', saved)
            all_before = (current['files'] == initial['files']
                          and all(current[k] == initial[k] for k in
                                  ('branch', 'commit', 'index_sha256')))
            expected_final = dict(initial['files'])
            expected_final.update(post_images)
            all_after = (set(post_images) == expected_paths
                         and current['files'] == expected_final
                         and all(current[k] == initial[k] for k in
                                 ('branch', 'commit', 'index_sha256')))
            recorded_paths = {op.get('path') for op in operations
                              if op['kind'] == 'project_write'
                              and (op['state'] == 'recorded'
                                   or op['state'] == 'reconciled'
                                   and op.get('outcome') == 'matches_after')}
            complete_scope = recorded_paths == expected_paths
            per_file = []
            for name in sorted(expected_paths):
                before = initial['files'].get(name)
                after = post_images.get(name)
                actual = current['files'].get(name)
                state = ('unchanged' if actual == before else
                         'applied' if after is not None and actual == after else
                         'externally_modified' if actual is not None else 'uncertain')
                per_file.append({'path': name, 'before_sha256': before,
                    'approved_after_sha256': after, 'actual_sha256': actual,
                    'classification': state})
            # A complete accepted state requires every authorized target to
            # match its exact post-image and every journal operation recorded.
            pending_after = bool(
                pending and pending['kind'] == 'project_write'
                and pending.get('path') in expected_paths
                and current['files'].get(pending['path']) == pending.get('after'))
            all_after = all_after and (complete_scope or pending_after)
            pending_outcome = ('matches_after' if all_after else
                               'matches_before' if all_before else
                               'partial_or_uncertain' if pending or operations else
                               'no_write_started')
            write_hashes = { ('project_write', item['path']): {
                'kind': 'project_write', 'path': item['path'],
                'before': item['before_sha256'], 'after': item['approved_after_sha256'],
                'actual': item['actual_sha256'],
                'checkpoint_sha256': item['approved_after_sha256'],
                'classification': item['classification'],
                'is_regular_or_absent': item['classification'] != 'uncertain'}
                for item in per_file }
            verified = integrity['valid'] and (all_before or all_after)
            unresolved = row['status'] in {'authorized', 'executing', 'interrupted'}
            stale_locks = self._stale_launcher_locks(row.get('owner_pid')) if unresolved else {}
            options = ((['accept'] if all_after else []) + (['abort'] if all_before else [])) if unresolved else []
            report = {
                'run_id': run_id, 'project_id': self.project.identity, 'status': row['status'],
                'recovery_state': 'reconciliation_required' if unresolved else row['status'],
                'scope': row['scope'], 'journal_sha256': journal_hash, 'pending': pending,
                'last_recorded': next((op for op in reversed(operations) if op['state'] == 'recorded'), None),
                'writes': list(write_hashes.values()), 'file_classifications': per_file,
                'tests': tests, 'receipt_integrity': integrity,
                'checkpoint_matches': unchanged, 'pending_outcome': pending_outcome,
                'stale_launcher_locks': {path.name: digest(raw) for path, raw in stale_locks.items()},
                'safe_to_reconcile': verified, 'current': current, 'options': options,
            }
            if unresolved and not options:
                report['options'] = ['operator must inspect and manually reconcile every target; writes are not retried']
            report['review_sha256'] = digest(encoded(report))
            return report
        writes_match = all(item['is_regular_or_absent'] and item['actual'] == item['checkpoint_sha256']
                           for item in write_hashes.values()
                           if not pending or (item['kind'], item['path']) !=
                           (pending['kind'], pending.get('path')))
        pending_outcome = None
        restore_available = False
        if pending and pending['kind'] in {'project_write', 'assistant_write'}:
            item = write_hashes[(pending['kind'], pending['path'])]
            expected = dict(saved['files'])
            if pending['kind'] == 'project_write':
                expected[pending['path']] = pending['after']
            after_matches = current['files'] == expected and all(
                current[key] == saved[key] for key in ('commit', 'branch', 'index_sha256'))
            if item['is_regular_or_absent'] and item['actual'] == pending['after'] and after_matches:
                pending_outcome = 'matches_after'
            elif item['is_regular_or_absent'] and item['actual'] == pending['before'] and unchanged:
                pending_outcome = 'matches_before'
            else:
                pending_outcome = 'conflict'
            if pending['kind'] == 'project_write' and pending.get('backup'):
                unaffected = dict(current['files'])
                unaffected.pop(pending['path'], None)
                expected.pop(pending['path'], None)
                backup = self.backup_path(run_id, pending['id'])
                if backup.is_file():
                    _plain_path(backup)
                    restore_available = (unaffected == expected and item['is_regular_or_absent']
                                         and all(current[k] == saved[k] for k in (
                                             'commit', 'branch', 'index_sha256'))
                                         and file_hash(backup) == pending['before'])
        elif pending:
            stopped = pending.get('process_stopped') or _owner_exited(pending.get('child_pid'))
            pending_outcome = 'unverified_command' if stopped else 'command_liveness_unknown'
        verified = writes_match and integrity['valid'] and (
            pending_outcome == 'matches_after' or (
                unchanged and pending_outcome in {None, 'matches_before', 'unverified_command'}))
        unresolved = row['status'] in {'authorized', 'executing', 'interrupted'}
        stale_locks = self._stale_launcher_locks(row.get('owner_pid')) if unresolved else {}
        report = {
            'run_id': run_id, 'project_id': self.project.identity, 'status': row['status'],
            'recovery_state': 'reconciliation_required' if unresolved else row['status'],
            'scope': row['scope'], 'journal_sha256': journal_hash, 'pending': pending,
            'last_recorded': next((op for op in reversed(operations) if op['state'] == 'recorded'), None),
            'writes': list(write_hashes.values()), 'tests': tests, 'receipt_integrity': integrity,
            'checkpoint_matches': unchanged, 'pending_outcome': pending_outcome,
            'stale_launcher_locks': {path.name: digest(raw) for path, raw in stale_locks.items()},
            'safe_to_reconcile': verified, 'current': current,
            'options': ((['accept', 'abort'] if verified else
                         ['review current files and reconcile conflicts manually'])
                        + (['restore'] if restore_available else []) if unresolved else []),
        }
        report['review_sha256'] = digest(encoded(report))
        return report

    def reconcile(self, run_id, expected_review, action):
        if action not in {'accept', 'abort', 'restore', 'confirm-command-stopped'}:
            raise RecoveryError('Choose accept, abort, restore, or confirm-command-stopped.')
        with self.locked():
            report = self.inspect(run_id)
            if report['review_sha256'] != expected_review:
                raise RecoveryError('Recovery evidence changed since operator review.')
            row, _ = self.load(run_id)
            if action == 'confirm-command-stopped':
                if (report['pending_outcome'] != 'command_liveness_unknown'
                        or not _owner_exited(row.get('owner_pid'))
                        or report['pending'].get('child_pid') is not None):
                    raise RecoveryError('Only an exited owner with an unrecorded child can use manual confirmation.')
                pending = next(op for op in row['operations'] if op['state'] == 'executing')
                pending.update(process_stopped=True, operator_confirmation_sha256=expected_review)
                ExecutionJournal(self, row).save()
                return {'run_id': run_id, 'status': 'command_stop_confirmed_for_review'}
            if action not in report['options']:
                raise RecoveryError('Recovery evidence is unresolved or the run is already closed.')
            if action == 'restore':
                operation = report['pending']
                backup = self.backup_path(run_id, operation['id'])
                target = self.project.path(operation['path'])
                raw = backup.read_bytes()
                if digest(raw) != operation['before']:
                    raise RecoveryError('Backup hash differs from the approved before state.')
                if target.is_file():
                    displaced = target.read_bytes()
                    if digest(displaced) != report['current']['files'].get(operation['path']):
                        raise RecoveryError('Target changed during restore review.')
                    atomic_write(backup.with_suffix('.displaced-' + uuid.uuid4().hex), displaced)
                if checkpoint(self.project) != report['current']:
                    raise RecoveryError('Project changed during restore review.')
                atomic_write(target, raw)
                if checkpoint(self.project) != row['checkpoint']:
                    raise RecoveryError('Restore needs further reconciliation.')
                # A separate inspect and decision still closes the interrupted run.
                return {'run_id': run_id, 'status': 'restored_for_review'}
            for path, raw in self._stale_launcher_locks(row.get('owner_pid')).items():
                if report['stale_launcher_locks'].get(path.name) != digest(raw):
                    raise RecoveryError('A launcher lock changed during operator review.')
                archive = self.path(run_id).with_suffix('.' + path.name + '.archive')
                atomic_write(archive, raw)
                if path.read_bytes() != raw or not _owner_exited(row['owner_pid']):
                    raise RecoveryError('Launcher lock changed during archival.')
                path.unlink()
            for op in row['operations']:
                if op['state'] == 'executing':
                    observed = report['current']['files'].get(op.get('path')) if op['kind'] == 'project_write' else None
                    op.update(state='reconciled', outcome=report['pending_outcome'],
                              evidence={'recovery_review_sha256': expected_review,
                                        'observed_sha256': observed})
            row.update(status='resumable' if action == 'accept' else 'aborted',
                       checkpoint=report['current'],
                       reconciliation={'action': action, 'review_sha256': expected_review})
            ExecutionJournal(self, row).save()
            return {'run_id': run_id, 'status': row['status']}

    def start(self, output, writes, assistant_files, assistant_writes, allowed_tools,
              test_config, required_operations, resume_from=None, run_id=None,
              approved_post_images=None):
        run_id = run_id or uuid.uuid4().hex
        if not isinstance(run_id, str) or not re.fullmatch(r'[a-f0-9]{32}', run_id):
            raise RecoveryError('Invalid recovery run ID.')
        starting_checkpoint = checkpoint(self.project)
        row = {
            'version': 1, 'run_id': run_id, 'project_id': self.project.identity,
            'output': str(output.resolve()), 'status': 'authorized', 'operations': [],
            'owner_pid': os.getpid(),
            'checkpoint': starting_checkpoint, 'initial_checkpoint': starting_checkpoint,
            'resume_from': resume_from,
            'scope': {'writes': sorted(writes), 'assistant_writes': sorted(assistant_writes),
                      'approved_post_images': dict(approved_post_images or {}),
                      'assistant_root': str(assistant_files.root) if assistant_writes else None,
                      'tools': sorted(allowed_tools) if allowed_tools is not None else None,
                      'test_config': str(Path(test_config).resolve()) if test_config else None,
                      'test_config_sha256': file_hash(test_config) if test_config else None,
                      'required_operations': sorted(required_operations)},
        }
        journal = ExecutionJournal(self, row)
        journal.save()
        return journal


class ExecutionJournal:
    def __init__(self, store, row):
        self.store, self.row = store, row
        self.pending = None
        self.broken = False

    def save(self):
        try:
            atomic_write(self.store.path(self.row['run_id']), encoded(self.row))
        except OSError as exc:
            self.broken = True
            raise RecoveryError('Execution journal could not be persisted.') from exc

    def begin(self, kind, backup=None, **intent):
        if self.pending is not None or self.broken:
            raise RecoveryError('An earlier operation requires reconciliation.')
        if checkpoint(self.store.project) != self.row['checkpoint']:
            self.row['status'] = 'interrupted'
            self.save()
            raise RecoveryError('Project changed outside the recorded checkpoint.')
        operation = {'id': uuid.uuid4().hex, 'kind': kind, 'state': 'executing', **intent}
        if backup is not None:
            if digest(backup) != intent['before']:
                raise RecoveryError('Source changed before the recovery backup.')
            atomic_write(self.store.backup_path(self.row['run_id'], operation['id']), backup)
            operation['backup'] = True
        self.pending = operation
        self.row['operations'].append(operation)
        self.row['status'] = 'executing'
        self.save()

    def record(self, evidence):
        state = checkpoint(self.store.project)
        expected = dict(self.row['checkpoint']['files'])
        if self.pending['kind'] == 'project_write':
            expected[self.pending['path']] = self.pending['after']
        if (state['files'] != expected or any(
                state[k] != self.row['checkpoint'][k] for k in ('branch', 'commit', 'index_sha256'))):
            raise RecoveryError('Observed project state differs from the authorized operation.')
        self.pending.update(state='recorded', evidence=evidence)
        self.row['checkpoint'] = state
        self.save()
        self.pending = None

    def command_process(self, **facts):
        self.pending.update(facts)
        self.save()

    def close(self, status):
        if self.broken:
            return
        if (self.pending is not None or self.row['status'] == 'interrupted'
                or status == 'interrupted'):
            status = 'interrupted'
        elif status != 'complete':
            status = 'interrupted' if self.row['operations'] else 'failed'
        self.row['status'] = status
        self.save()
