import contextlib
import io
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import agent
from agent_config import (
    CONFIG_VERSION, DEFAULT_ENDPOINT, DEFAULT_MODEL, default_projects, load_config,
    selector_map, validate_config, validate_endpoint, validate_forwarded_endpoint,
)
from agent_core import AgentError, Project
from model_router import get_models


def valid_config(root='D:/Example'):
    return {
        'version': CONFIG_VERSION,
        'projects': {'example_project': {'name': 'Example', 'root': root}},
        'inference': {
            'provider': 'ollama', 'model': 'qwen2.5-coder:7b-instruct',
            'endpoint': DEFAULT_ENDPOINT,
        },
    }


class AgentConfigTests(unittest.TestCase):
    def test_legacy_defaults_follow_host_without_losing_windows_compatibility(self):
        with tempfile.TemporaryDirectory() as temporary:
            hub = Path(temporary) / 'LocalCodingAgent'
            hub.mkdir()
            mac_defaults = default_projects(hub, 'darwin')
            windows_defaults = default_projects(hub, 'win32')

        self.assertEqual(mac_defaults, {'LocalCodingAgent': str(hub.resolve())})
        self.assertEqual(windows_defaults['KronosStage'], 'D:/Workspaces/KronosStage')
        self.assertEqual(windows_defaults['PhotoLibraryTools'], 'D:/Workspaces/ChasingLight')
        self.assertIn('LocalAgentRepairFixture', windows_defaults)
    def test_missing_config_uses_legacy_names_and_stable_ids(self):
        with tempfile.TemporaryDirectory() as temporary:
            config = load_config(Path(temporary) / 'missing.json',
                                 {'PhotoLibraryTools': 'D:/Photos', 'LocalAgentSandbox': 'D:/Agent'})
        self.assertEqual(config['version'], CONFIG_VERSION)
        self.assertEqual(config['projects']['photolibrarytools'],
                         {'name': 'PhotoLibraryTools', 'root': 'D:/Photos'})
        self.assertEqual(config['projects']['localagentsandbox']['name'], 'LocalAgentSandbox')
        self.assertEqual(DEFAULT_MODEL, 'qwen3:8b')
        self.assertEqual(config['inference']['model'], DEFAULT_MODEL)
        self.assertEqual(config['inference']['endpoint'], DEFAULT_ENDPOINT)
        self.assertEqual(selector_map(config)['PhotoLibraryTools'], 'photolibrarytools')
        self.assertEqual(selector_map(config)['photolibrarytools'], 'photolibrarytools')

    def test_versioned_config_accepts_stable_ids_and_canonicalizes_endpoint(self):
        config = valid_config()
        config['inference']['endpoint'] += '/'
        validated = validate_config(config)
        self.assertEqual(validated['projects']['example_project']['name'], 'Example')
        self.assertEqual(validated['inference']['endpoint'], DEFAULT_ENDPOINT)

    def test_invalid_missing_and_malformed_configuration(self):
        with self.assertRaises(AgentError):
            validate_config({})
        bad_version = valid_config()
        bad_version['version'] = 2
        with self.assertRaises(AgentError):
            validate_config(bad_version)
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / 'agent-config.json'
            path.write_text('{broken', encoding='utf-8')
            with self.assertRaises(AgentError):
                load_config(path, {'Legacy': 'D:/Legacy'})

    def test_provider_endpoints_are_strictly_loopback_and_pinned(self):
        self.assertEqual(validate_endpoint(DEFAULT_ENDPOINT + '/'), DEFAULT_ENDPOINT)
        for endpoint in (
            'http://localhost:11434', 'http://127.0.0.1', 'http://127.0.0.1:11435',
            'http://10.0.0.2:11434', 'https://127.0.0.1:11434',
            'http://user:pass@127.0.0.1:11434', 'http://127.0.0.1:11434/v1',
            'http://127.0.0.1:11434?redirect=1', 'http://[::1]:11434',
        ):
            with self.subTest(endpoint=endpoint), self.assertRaises(AgentError):
                validate_endpoint(endpoint)
        with self.assertRaises(ValueError):
            get_models('http://localhost:11434')

    def test_remote_provider_requires_explicit_tunnel_profile_and_host_identity(self):
        config = {
            'version': 3,
            'projects': {'example_project': {'name': 'Example', 'root': 'D:/Example'}},
            'inference': {
                'provider': 'ssh_tunnel_ollama', 'model': 'qwen3:8b',
                'endpoint': 'http://127.0.0.1:22001',
                'remote': {
                    'enabled': True, 'profile_id': 'mac_studio', 'port': 22001,
                    'ssh_host_alias': 'mac-studio-inference',
                    'ssh_host_key_sha256': 'SHA256:' + 'A' * 43,
                    'credential_ref': 'ssh-agent',
                    'known_hosts_file': 'C:/Users/test/.ssh/mac_known_hosts',
                    'ssh_config_file': 'C:/Users/test/.ssh/config',
                },
            },
        }
        validated = validate_config(config)
        self.assertEqual(validated['inference']['provider'], 'ssh_tunnel_ollama')
        self.assertEqual(validated['inference']['endpoint'], 'http://127.0.0.1:22001')
        self.assertEqual(validated['inference']['remote']['credential_ref'], 'ssh-agent')

        disabled = json.loads(json.dumps(config))
        disabled['inference']['remote']['enabled'] = False
        with self.assertRaisesRegex(AgentError, 'disabled'):
            validate_config(disabled)

    def test_remote_profile_rejects_url_port_identity_and_credential_tampering(self):
        base = {
            'version': 3,
            'projects': {'example_project': {'name': 'Example', 'root': 'D:/Example'}},
            'inference': {
                'provider': 'ssh_tunnel_ollama', 'model': 'qwen3:8b',
                'endpoint': 'http://127.0.0.1:22001',
                'remote': {
                    'enabled': True, 'profile_id': 'mac_studio', 'port': 22001,
                    'ssh_host_alias': 'mac-studio-inference',
                    'ssh_host_key_sha256': 'SHA256:' + 'A' * 43,
                    'credential_ref': 'ssh-agent',
                    'known_hosts_file': 'C:/Users/test/.ssh/mac_known_hosts',
                    'ssh_config_file': 'C:/Users/test/.ssh/config',
                },
            },
        }
        cases = [
            ('endpoint', 'http://127.0.0.1:22002'),
            ('endpoint', 'http://user:pass@127.0.0.1:22001'),
            ('endpoint', 'http://10.0.0.2:22001'),
            ('port', 11434),
            ('ssh_host_key_sha256', 'SHA256:short'),
            ('credential_ref', 'password=secret'),
        ]
        for field, bad_value in cases:
            altered = json.loads(json.dumps(base))
            if field in ('endpoint',):
                altered['inference'][field] = bad_value
            else:
                altered['inference']['remote'][field] = bad_value
            with self.subTest(field=field, value=bad_value), self.assertRaises(AgentError):
                validate_config(altered)

    def test_v2_local_config_keeps_remote_profile_disabled(self):
        config = {
            'version': 2,
            'projects': {'example_project': {'name': 'Example', 'root': 'D:/Example'}},
            'inference': {
                'provider': 'ollama', 'model': 'qwen3:8b', 'endpoint': DEFAULT_ENDPOINT,
                'remote': {
                    'enabled': False, 'profile_id': 'mac_studio', 'port': 22001,
                    'ssh_host_alias': 'mac-studio-inference',
                    'ssh_host_key_sha256': 'SHA256:' + 'A' * 43,
                    'credential_ref': 'ssh-agent',
                },
            },
        }
        self.assertEqual(validate_config(config)['inference']['provider'], 'ollama')
        with self.assertRaisesRegex(AgentError, 'only when selecting'):
            config['inference']['remote']['enabled'] = True
            validate_config(config)

    def test_milestone1_v2_remote_config_is_preserved_but_cannot_open_tunnel(self):
        config = {
            'version': 2,
            'projects': {'example_project': {'name': 'Example', 'root': 'D:/Example'}},
            'inference': {
                'provider': 'ssh_tunnel_ollama', 'model': 'qwen3:8b',
                'endpoint': 'http://127.0.0.1:22001',
                'remote': {
                    'enabled': True, 'profile_id': 'mac_studio', 'port': 22001,
                    'ssh_host_alias': 'mac-studio-inference',
                    'ssh_host_key_sha256': 'SHA256:' + 'A' * 43,
                    'credential_ref': 'ssh-agent',
                },
            },
        }
        with self.assertRaisesRegex(AgentError, 'version 3 is required'):
            validate_config(config)

    def test_forwarded_endpoint_is_distinct_from_default_local_endpoint(self):
        self.assertEqual(validate_forwarded_endpoint('http://127.0.0.1:22001/', 22001),
                         'http://127.0.0.1:22001')
        for endpoint, port in (
            ('http://127.0.0.1:11434', 11434),
            ('http://localhost:22001', 22001),
            ('http://127.0.0.1:22001/path', 22001),
            ('http://127.0.0.1:22001?x=1', 22001),
        ):
            with self.subTest(endpoint=endpoint), self.assertRaises(AgentError):
                validate_forwarded_endpoint(endpoint, port)

    def test_v4_fallback_defaults_to_disabled_and_requires_authorized_substitutions(self):
        local = valid_config()
        local['version'] = 4
        local['inference']['remote'] = {
            'enabled': False, 'profile_id': 'mac_studio', 'port': 22001,
            'ssh_host_alias': 'mac-studio-inference',
            'ssh_host_key_sha256': 'SHA256:' + 'A' * 43,
            'credential_ref': 'ssh-agent',
            'known_hosts_file': 'C:/Users/test/.ssh/known_hosts',
            'ssh_config_file': 'C:/Users/test/.ssh/config',
        }
        local['inference']['fallback'] = {'mode': 'disabled', 'substitutions': []}
        accepted = validate_config(local)
        self.assertEqual(accepted['inference']['fallback']['mode'], 'disabled')

        row = {'from_provider': 'ssh_tunnel_ollama', 'from_model': 'mac-model',
               'to_provider': 'ollama', 'to_model': 'windows-model',
               'max_context': 32768, 'operator_approved': False,
               'data_sharing_authorized': False}
        local['inference']['fallback'] = {'mode': 'local_only', 'substitutions': [row]}
        local['inference']['remote']['enabled'] = True
        self.assertEqual(validate_config(local)['inference']['fallback']['mode'], 'local_only')
        local['inference']['fallback']['mode'] = 'ask'
        local['inference']['fallback']['substitutions'][0]['operator_approved'] = True
        with self.assertRaisesRegex(AgentError, 'await per-run'):
            validate_config(local)
        local['inference']['fallback'] = {'mode': 'approved', 'substitutions': [
            {**row, 'operator_approved': True, 'data_sharing_authorized': True}]}
        self.assertEqual(validate_config(local)['inference']['fallback']['mode'], 'approved')

    def test_v4_rejects_remote_fallback_without_enabled_profile_and_cloud_targets(self):
        config = valid_config()
        config['version'] = 4
        config['inference'].update(remote={
            'enabled': False, 'profile_id': 'mac_studio', 'port': 22001,
            'ssh_host_alias': 'mac-studio-inference',
            'ssh_host_key_sha256': 'SHA256:' + 'A' * 43,
            'credential_ref': 'ssh-agent',
            'known_hosts_file': 'C:/Users/test/.ssh/known_hosts',
            'ssh_config_file': 'C:/Users/test/.ssh/config',
        }, fallback={'mode': 'local_only', 'substitutions': []})
        remote_row = {'from_provider': 'ollama', 'from_model': 'windows-model',
                      'to_provider': 'ssh_tunnel_ollama', 'to_model': 'mac-model',
                      'max_context': 8192, 'operator_approved': False,
                      'data_sharing_authorized': False}
        config['inference']['fallback']['substitutions'] = [remote_row]
        with self.assertRaisesRegex(AgentError, 'Windows Ollama only'):
            validate_config(config)
        remote_row['from_provider'] = 'ssh_tunnel_ollama'
        remote_row['to_provider'] = 'ollama'
        config['inference']['fallback'] = {'mode': 'local_only', 'substitutions': [remote_row]}
        with self.assertRaisesRegex(AgentError, 'explicitly enabled'):
            validate_config(config)

    def test_project_authorization_rejects_nested_non_git_root(self):
        root = Path(__file__).resolve().parents[1]
        Project(root)
        with self.assertRaisesRegex(AgentError, 'exact registered Git worktree root'):
            Project(root / 'tests')

    def test_config_cli_is_read_only_and_does_not_emit_credentials(self):
        with tempfile.TemporaryDirectory() as temporary:
            private = Path(temporary) / 'private-does-not-exist'
            output = io.StringIO()
            with patch.object(agent, 'PRIVATE', private), patch.object(
                sys, 'argv', ['agent.py', 'config', '--project', 'kronosstage']
            ), contextlib.redirect_stdout(output):
                agent.main()
            rendered = output.getvalue()
            self.assertFalse(private.exists())
            self.assertIn('"id": "kronosstage"', rendered)
            self.assertNotIn('api_key', rendered.casefold())
            self.assertNotIn('password', rendered.casefold())
            self.assertNotIn('access_token', rendered.casefold())


if __name__ == '__main__':
    unittest.main()
