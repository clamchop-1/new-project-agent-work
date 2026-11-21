import base64
import hashlib
import io
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from agent_core import AgentError
from inference_provider import ProviderFailure
from local_agent import create_inference_provider
from ssh_tunnel import (CONNECT_TIMEOUT_SECONDS, STARTUP_TIMEOUT_SECONDS,
                        ManagedSshTunnel, verify_enrolled_host)


def fingerprint(blob):
    return 'SHA256:' + base64.b64encode(hashlib.sha256(blob).digest()).decode().rstrip('=')


class FakeProcess:
    pid = 44123

    def __init__(self, alive=True, stderr=None):
        self.alive = alive
        self.terminated = False
        self.killed = False
        self.stderr = io.BytesIO(stderr) if stderr is not None else None

    def poll(self):
        return None if self.alive else 1

    def terminate(self):
        self.terminated = True
        self.alive = False

    def kill(self):
        self.killed = True
        self.alive = False

    def wait(self, timeout=None):
        self.alive = False
        return 0


class TunnelTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        self.known_hosts = root / 'known_hosts'
        self.ssh_config = root / 'config'
        self.known_hosts.write_text('enrolled key data\n', encoding='utf-8')
        self.ssh_config.write_text('Host mac-inference\n  HostName 192.0.2.50\n', encoding='utf-8')
        self.key_blob = b'fake-ed25519-public-key-blob'
        self.key_line = 'mac-inference ssh-ed25519 ' + base64.b64encode(self.key_blob).decode()
        self.profile = {
            'enabled': True, 'profile_id': 'mac_studio', 'port': 22001,
            'ssh_host_alias': 'mac-inference',
            'ssh_host_key_sha256': fingerprint(self.key_blob),
            'credential_ref': 'ssh-agent',
            'known_hosts_file': str(self.known_hosts),
            'ssh_config_file': str(self.ssh_config),
        }

    def test_enrolled_fingerprint_is_derived_from_selected_known_hosts_key(self):
        def run(argv, **kwargs):
            self.assertEqual(argv[1:3], ['-F', 'mac-inference'])
            self.assertIn(str(self.known_hosts), argv)
            return SimpleNamespace(returncode=0, stdout=self.key_line)

        verify_enrolled_host('mac-inference', str(self.known_hosts),
                             self.profile['ssh_host_key_sha256'], run=run,
                             which=lambda name: 'ssh-keygen.exe')
        with self.assertRaisesRegex(AgentError, 'does not exactly match'):
            verify_enrolled_host('mac-inference', str(self.known_hosts),
                                 fingerprint(b'wrong-key'), run=run,
                                 which=lambda name: 'ssh-keygen.exe')

    def _manager(self, listener_state, process=None, clock=None):
        process = process or FakeProcess()
        popen = Mock(return_value=process)
        run = Mock(side_effect=lambda argv, **kwargs: (
            SimpleNamespace(returncode=0, stdout=self.key_line)
            if 'ssh-keygen' in argv[0]
            else SimpleNamespace(
                returncode=0,
                stdout=f"localforward [127.0.0.1]:{self.profile['port']} [127.0.0.1]:11434\n",
            ) if '-G' in argv
            else SimpleNamespace(returncode=0, stdout=','.join(map(str, listener_state['pids'])))
        ))
        manager = ManagedSshTunnel(
            self.profile, popen=popen, run=run,
            which=lambda name: 'ssh-keygen.exe' if name == 'ssh-keygen' else
            ('powershell.exe' if name == 'powershell.exe' else 'ssh.exe'),
            platform='win32', monotonic=clock[0] if clock else None,
            sleep=clock[1] if clock else None,
        )
        return manager, process, popen, run

    def test_startup_uses_fixed_secure_forward_and_listener_pid_is_owned(self):
        state = {'pids': set()}
        process = FakeProcess()
        manager, _, popen, run = self._manager(state, process)
        def listener_side_effect(argv, **kwargs):
            if 'powershell.exe' in argv[0] and popen.called and process.alive:
                state['pids'] = {process.pid}
            elif 'powershell.exe' in argv[0]:
                state['pids'] = set()
            return (SimpleNamespace(returncode=0, stdout=self.key_line)
                    if 'ssh-keygen.exe' in argv[0]
                    else SimpleNamespace(
                        returncode=0,
                        stdout=f"localforward [127.0.0.1]:{self.profile['port']} [127.0.0.1]:11434\n",
                    ) if '-G' in argv
                    else SimpleNamespace(returncode=0, stdout=','.join(map(str, state['pids']))))
        run.side_effect = listener_side_effect

        self.assertTrue(manager.ensure_ready())
        argv = popen.call_args.args[0]
        self.assertIn('BatchMode=yes', argv)
        self.assertIn('StrictHostKeyChecking=yes', argv)
        self.assertIn(f'ConnectTimeout={CONNECT_TIMEOUT_SECONDS}', argv)
        self.assertLess(CONNECT_TIMEOUT_SECONDS, STARTUP_TIMEOUT_SECONDS)
        self.assertIn('UserKnownHostsFile=' + str(self.known_hosts), argv)
        self.assertIn('GlobalKnownHostsFile=NUL', argv)
        self.assertIn('ExitOnForwardFailure=yes', argv)
        self.assertIn('ClearAllForwardings=no', argv)
        self.assertIn('ForwardAgent=no', argv)
        self.assertIn('PasswordAuthentication=no', argv)
        self.assertIn('KbdInteractiveAuthentication=no', argv)
        self.assertIn('PreferredAuthentications=publickey', argv)
        self.assertIn('ProxyCommand=none', argv)
        self.assertIn('ProxyJump=none', argv)
        self.assertEqual(argv[argv.index('-F') + 1], str(self.ssh_config))
        self.assertNotIn('-i', argv)
        self.assertIn('-N', argv)
        self.assertIn('127.0.0.1:22001:127.0.0.1:11434', argv)
        self.assertTrue(manager.close())
        self.assertTrue(process.terminated)
        self.assertFalse(process.killed)

    def test_unexpected_listener_rejects_occupied_port_without_starting_ssh(self):
        manager, _, popen, _ = self._manager({'pids': {888}})
        with self.assertRaisesRegex(AgentError, 'already occupied'):
            manager.ensure_ready()
        popen.assert_not_called()

    def test_unexpected_configured_forward_is_rejected_before_ssh_launch(self):
        manager, _, popen, run = self._manager({'pids': set()})
        expected = f"localforward [127.0.0.1]:{self.profile['port']} [127.0.0.1]:11434"
        run.side_effect = lambda argv, **kwargs: (
            SimpleNamespace(returncode=0, stdout=self.key_line)
            if 'ssh-keygen.exe' in argv[0]
            else SimpleNamespace(returncode=0, stdout=(
                expected + '\nlocalforward [127.0.0.1]:22002 [127.0.0.1]:11434\n'
            )) if '-G' in argv
            else SimpleNamespace(returncode=0, stdout='')
        )
        with self.assertRaisesRegex(AgentError, 'unexpected or unverifiable forwarding'):
            manager.ensure_ready()
        popen.assert_not_called()

    def test_effective_forward_plan_query_failure_fails_closed(self):
        manager, _, popen, run = self._manager({'pids': set()})
        run.side_effect = lambda argv, **kwargs: (
            SimpleNamespace(returncode=0, stdout=self.key_line)
            if 'ssh-keygen.exe' in argv[0]
            else SimpleNamespace(returncode=1, stdout='') if '-G' in argv
            else SimpleNamespace(returncode=0, stdout='')
        )
        with self.assertRaisesRegex(AgentError, 'could not verify the effective forwarding'):
            manager.ensure_ready()
        popen.assert_not_called()

    def test_wrong_enrolled_key_fails_before_process_launch(self):
        manager, _, popen, run = self._manager({'pids': set()})
        run.side_effect = lambda argv, **kwargs: (
            SimpleNamespace(returncode=0, stdout='mac-inference ssh-ed25519 '
                             + base64.b64encode(b'wrong-key').decode())
            if 'ssh-keygen.exe' in argv[0]
            else SimpleNamespace(returncode=0, stdout='')
        )
        with self.assertRaisesRegex(AgentError, 'does not exactly match'):
            manager.ensure_ready()
        popen.assert_not_called()

    def test_ssh_process_failure_and_startup_timeout_clean_only_owned_process(self):
        dead = FakeProcess(alive=False)
        manager, _, popen, _ = self._manager({'pids': set()}, dead)
        with self.assertRaisesRegex(AgentError, 'exited before'):
            manager.ensure_ready()
        self.assertEqual(popen.call_count, 1)

        time_state = {'now': 0.0}
        def monotonic():
            return time_state['now']
        def sleep(seconds):
            time_state['now'] += seconds
        process = FakeProcess()
        manager, _, popen, run = self._manager({'pids': set()}, process,
                                               (monotonic, sleep))
        with self.assertRaisesRegex(ProviderFailure, 'Timed out.*listener_readiness_deadline') as caught:
            manager.ensure_ready()
        self.assertEqual(caught.exception.code, 'tunnel_timeout')
        self.assertTrue(process.terminated)
        self.assertFalse(process.killed)
        popen.assert_called_once()

    def test_startup_stderr_is_classified_without_exposing_raw_text(self):
        cases = (
            (b'connect to host 192.0.2.50 port 22: Connection timed out secret=hidden',
             'tunnel_timeout', 'ssh_tcp_connection_timeout'),
            (b'No route to host', 'tunnel_unavailable', 'ssh_host_unreachable'),
            (b'Permission denied (publickey).', 'authentication',
             'public_key_authentication_failed'),
            (b'Host key verification failed.', 'identity_mismatch', 'host_key_rejected'),
            (b'bind [127.0.0.1]:22001: Address already in use',
             'port_conflict', 'local_forward_bind_failure'),
            (b'Could not request local forwarding.',
             'tunnel_unavailable', 'ssh_forwarding_failed'),
            (b'connection refused', 'tunnel_unavailable', 'ssh_connection_refused'),
        )
        for stderr, expected_code, expected_category in cases:
            with self.subTest(category=expected_category):
                manager, _, _, _ = self._manager({'pids': set()})
                manager._stderr.extend(stderr)
                failure = manager._startup_failure(1.234)
                self.assertEqual(failure.code, expected_code)
                self.assertIn(expected_category, str(failure))
                self.assertIn('elapsed_ms=1234', str(failure))
                if expected_code in {'identity_mismatch', 'port_conflict'}:
                    self.assertTrue(failure.security)
                if expected_code == 'authentication':
                    self.assertFalse(failure.retryable)
                self.assertNotIn('192.0.2.50', str(failure))
                self.assertNotIn('secret=hidden', str(failure))

    def test_unknown_timeout_reports_only_readiness_deadline_and_bounded_time(self):
        manager, _, _, _ = self._manager({'pids': set()})
        manager._stderr.extend(b'opaque SSH diagnostic with secret=hidden')
        failure = manager._startup_failure(90.0, readiness_deadline=True)
        self.assertEqual(failure.code, 'tunnel_timeout')
        self.assertIn('listener_readiness_deadline_exceeded', str(failure))
        self.assertIn('elapsed_ms=12000', str(failure))
        self.assertNotIn('opaque', str(failure))
        self.assertNotIn('secret=hidden', str(failure))

    def test_unknown_early_exit_is_distinct_and_does_not_include_stderr(self):
        manager, _, _, _ = self._manager({'pids': set()})
        manager._stderr.extend(b'unclassified private diagnostic')
        failure = manager._startup_failure(0.321)
        self.assertEqual(failure.code, 'tunnel_unavailable')
        self.assertIn('ssh_process_exited_early', str(failure))
        self.assertIn('elapsed_ms=321', str(failure))
        self.assertNotIn('unclassified private diagnostic', str(failure))

    def test_connection_timeout_exit_cleans_up_once_without_retry(self):
        process = FakeProcess(alive=False, stderr=(
            b'connect to host 192.0.2.50 port 22: Connection timed out'))
        manager, _, popen, _ = self._manager({'pids': set()}, process)
        with self.assertRaises(ProviderFailure) as caught:
            manager.ensure_ready()
        self.assertEqual(caught.exception.code, 'tunnel_timeout')
        self.assertIn('ssh_tcp_connection_timeout', str(caught.exception))
        popen.assert_called_once()
        self.assertIn(b'connection timed out', bytes(manager._stderr).lower())

    def test_stale_process_or_changed_listener_owner_fails_closed(self):
        state = {'pids': set()}
        process = FakeProcess()
        manager, _, popen, run = self._manager(state, process)
        def listener_side_effect(argv, **kwargs):
            if 'ssh-keygen.exe' in argv[0]:
                return SimpleNamespace(returncode=0, stdout=self.key_line)
            if '-G' in argv:
                return SimpleNamespace(
                    returncode=0,
                    stdout=f"localforward [127.0.0.1]:{self.profile['port']} [127.0.0.1]:11434\n",
                )
            return SimpleNamespace(returncode=0, stdout=(
                str(process.pid) if popen.called and process.alive else
                ('999' if popen.called else '')
            ))
        run.side_effect = listener_side_effect
        self.assertTrue(manager.ensure_ready())
        process.alive = False
        with self.assertRaisesRegex(AgentError, 'stale or has exited'):
            manager.ensure_ready()

        state = {'pids': set()}
        process = FakeProcess()
        manager, _, popen, run = self._manager(state, process)
        def listener_owner_changes(argv, **kwargs):
            if 'ssh-keygen.exe' in argv[0]:
                return SimpleNamespace(returncode=0, stdout=self.key_line)
            if '-G' in argv:
                return SimpleNamespace(
                    returncode=0,
                    stdout=f"localforward [127.0.0.1]:{self.profile['port']} [127.0.0.1]:11434\n",
                )
            return SimpleNamespace(returncode=0,
                                   stdout='' if not popen.called else '999')
        run.side_effect = listener_owner_changes
        with self.assertRaisesRegex(AgentError, 'claimed by an unexpected process'):
            manager.ensure_ready()
        self.assertTrue(process.terminated)

    def test_cancellation_during_startup_stops_only_managed_process(self):
        state = {'pids': set()}
        process = FakeProcess()
        manager, _, _, _ = self._manager(state, process)
        cancel = threading.Event()
        cancel.set()
        with self.assertRaisesRegex(AgentError, 'startup cancelled'):
            manager.ensure_ready(cancel)
        self.assertTrue(process.terminated)

    def test_remote_provider_without_managed_profile_is_rejected(self):
        with self.assertRaises(AgentError):
            create_inference_provider('ssh_tunnel_ollama', 'http://127.0.0.1:22001')

    def test_remote_response_requires_tunnel_ownership_before_and_after_http(self):
        profile = dict(self.profile)
        provider = create_inference_provider(
            'ssh_tunnel_ollama', 'http://127.0.0.1:22001', profile
        )
        tunnel = Mock()
        provider.tunnel = tunnel
        class Response:
            status = 200
            def __init__(self): self.done = False
            def read(self, limit):
                if self.done: return b''
                self.done = True
                return b'{"models": []}'
        connection = Mock()
        connection.sock = None
        connection.getresponse.return_value = Response()
        with patch('local_agent.http.client.HTTPConnection', return_value=connection):
            self.assertEqual(provider.request('/api/tags'), {'models': []})
        self.assertEqual(tunnel.ensure_ready.call_count, 2)


if __name__ == '__main__':
    unittest.main()
