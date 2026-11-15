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
    selector_map, validate_config, validate_endpoint,
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
