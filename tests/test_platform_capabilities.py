import unittest
from unittest.mock import patch
from pathlib import Path

from local_agent import TOOLS
from platform_capabilities import available_tools, host_tool_adapter, ToolInvocation


class PlatformCapabilityToolSchemaTests(unittest.TestCase):
    def test_command_enum_does_not_restrict_path_or_mutate_shared_schema(self):
        tools = available_tools(
            TOOLS, system='posix', executable_lookup=lambda _: None,
            test_config='pinned-tests.json',
        )
        command = next(tool for tool in tools if tool['function']['name'] == 'run_command')
        properties = command['function']['parameters']['properties']

        self.assertEqual(properties['name']['enum'], ['python_parse', 'tests'])
        self.assertNotIn('enum', properties['path'])
        original = next(tool for tool in TOOLS if tool['function']['name'] == 'run_command')
        self.assertNotIn('enum', original['function']['parameters']['properties']['name'])
        self.assertNotIn('enum', original['function']['parameters']['properties']['path'])

    def test_test_capability_is_advertised_only_for_pinned_configuration(self):
        without_tests = available_tools(
            TOOLS, system='posix', executable_lookup=lambda _: None
        )
        with_tests = available_tools(
            TOOLS, system='posix', executable_lookup=lambda _: None,
            test_config='pinned-tests.json',
        )
        self.assertNotIn(
            'run_tests',
            [tool['function']['name'] for tool in without_tests],
        )
        self.assertIn(
            'run_tests',
            [tool['function']['name'] for tool in with_tests],
        )
        without_command = next(
            tool for tool in without_tests
            if tool['function']['name'] == 'run_command'
        )
        with_command = next(
            tool for tool in with_tests
            if tool['function']['name'] == 'run_command'
        )
        self.assertEqual(
            without_command['function']['parameters']['properties']['name']['enum'],
            ['python_parse'],
        )
        self.assertEqual(
            with_command['function']['parameters']['properties']['name']['enum'],
            ['python_parse', 'tests'],
        )

    def test_windows_powershell_capability_is_advertised_only_when_available(self):
        available = available_tools(
            TOOLS, system='nt', executable_lookup=lambda _: 'powershell.exe'
        )
        unavailable = available_tools(
            TOOLS, system='nt', executable_lookup=lambda _: None,
            test_config='pinned-tests.json',
        )
        available_enum = next(tool for tool in available if tool['function']['name'] == 'run_command')['function']['parameters']['properties']['name']['enum']
        unavailable_enum = next(tool for tool in unavailable if tool['function']['name'] == 'run_command')['function']['parameters']['properties']['name']['enum']

        self.assertIn('powershell_file_info', available_enum)
        self.assertNotIn('powershell_file_info', unavailable_enum)
        self.assertIn('tests', unavailable_enum)


    def test_posix_adapter_returns_only_typed_fixed_python_invocation(self):
        adapter = host_tool_adapter(
            system='posix', executable_lookup=lambda _: None
        )
        invocation = adapter.invocation(
            'python_parse', 'src/example.py', 'D:/LocalCodingAgent',
            python_executable='python-test',
        )

        self.assertIn(adapter.platform, {'posix', 'macos'})
        self.assertEqual(adapter.command_names, frozenset({'python_parse'}))
        self.assertIsInstance(invocation, ToolInvocation)
        argv, environment = invocation.subprocess_args()
        self.assertEqual(argv, [
            'python-test', '-B', '-I',
            str(Path('D:/LocalCodingAgent') / 'inspect_source.py'),
            str(Path('src/example.py')),
        ])
        self.assertEqual(environment, {})
        with self.assertRaises(ValueError):
            adapter.invocation('arbitrary_shell', 'x', 'y')
        with self.assertRaises(ValueError):
            adapter.invocation('powershell_file_info', 'x', 'y')

    @patch('platform_capabilities.sys.platform', 'darwin')
    def test_macos_profile_detects_only_portable_capability(self):
        adapter = host_tool_adapter(
            system='posix', executable_lookup=lambda _: None
        )
        self.assertEqual(adapter.platform, 'macos')
        self.assertEqual(adapter.command_names, frozenset({'python_parse'}))
    def test_windows_adapter_constructs_powershell_as_fixed_argv_and_env(self):
        adapter = host_tool_adapter(
            system='nt', executable_lookup=lambda _: 'powershell.exe'
        )
        invocation = adapter.invocation(
            'powershell_file_info', 'src/example.py', 'D:/LocalCodingAgent'
        )
        argv, environment = invocation.subprocess_args()

        self.assertIn('powershell_file_info', adapter.command_names)
        self.assertEqual(argv[0], 'powershell.exe')
        self.assertIn('-NonInteractive', argv)
        self.assertEqual(environment, {
            'LOCAL_AGENT_READ_FILE': str(Path('src/example.py'))
        })
if __name__ == '__main__':
    unittest.main()
