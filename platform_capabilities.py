"""Host detection and fixed capability adapters; never grants shell access."""
from dataclasses import dataclass
import copy
import os
import shutil
import sys
from pathlib import Path
from typing import Optional, Protocol


@dataclass(frozen=True)
class ToolInvocation:
    """Immutable subprocess request produced only by a typed host adapter."""
    argv: tuple[str, ...]
    environment: tuple[tuple[str, str], ...] = ()

    def subprocess_args(self) -> tuple[list[str], dict[str, str]]:
        return list(self.argv), dict(self.environment)


class ToolAdapter(Protocol):
    platform: str
    command_names: frozenset[str]

    def invocation(self, name: str, target: Path, hub: Path,
                   python_executable: Optional[str] = None) -> ToolInvocation:
        ...


@dataclass(frozen=True)
class HostToolAdapter:
    platform: str
    command_names: frozenset[str]

    def invocation(self, name: str, target: Path, hub: Path,
                   python_executable: Optional[str] = None) -> ToolInvocation:
        target = Path(target)
        if name == 'python_parse' and name in self.command_names:
            argv = (
                python_executable or sys.executable, '-B', '-I',
                str(Path(hub) / 'inspect_source.py'), str(target),
            )
            return ToolInvocation(argv)

        if name == 'powershell_file_info' and name in self.command_names:
            argv = (
                'powershell.exe', '-NoProfile', '-NonInteractive', '-Command',
                'Get-Item -LiteralPath $env:LOCAL_AGENT_READ_FILE | '
                'Select-Object Name,Length | ConvertTo-Json -Compress',
            )
            environment = (('LOCAL_AGENT_READ_FILE', str(target)),)
            return ToolInvocation(argv, environment)

        raise ValueError('Command is not available through this host adapter.')


def platform_name(system: Optional[str] = None) -> str:
    system = system or os.name
    if system == 'nt':
        return 'windows'
    if sys.platform == 'darwin':
        return 'macos'
    if system == 'posix':
        return 'posix'
    return 'unsupported'


def host_tool_adapter(system: Optional[str] = None,
                      executable_lookup=shutil.which) -> HostToolAdapter:
    """Discover implemented typed tools for this process host."""
    name = platform_name(system)
    commands = {'python_parse'}
    if name == 'windows' and executable_lookup('powershell.exe'):
        commands.add('powershell_file_info')
    return HostToolAdapter(name, frozenset(commands))


def capability_report(system: Optional[str] = None,
                     executable_lookup=shutil.which) -> dict[str, str]:
    adapter = host_tool_adapter(system, executable_lookup)
    commands = adapter.command_names
    return {
        'platform': adapter.platform,
        'python_parse': 'available' if 'python_parse' in commands else 'unavailable',
        'powershell_file_info': (
            'available' if 'powershell_file_info' in commands else 'unavailable'
        ),
        'general_shell': 'unavailable',
    }


def command_invocation(name: str, target: Path, hub: Path,
                       python_executable: Optional[str] = None,
                       system: Optional[str] = None) -> ToolInvocation:
    """Compatibility wrapper around the typed host adapter contract."""
    return host_tool_adapter(system).invocation(name, target, hub, python_executable)


def available_commands(system: Optional[str] = None,
                       executable_lookup=shutil.which) -> frozenset[str]:
    return host_tool_adapter(system, executable_lookup).command_names


def available_tools(tools: list, system: Optional[str] = None,
                    executable_lookup=shutil.which, test_config=None) -> list:
    commands = available_commands(system, executable_lookup)
    presented = copy.deepcopy(tools)
    if test_config is None:
        presented = [
            tool for tool in presented
            if tool['function']['name'] != 'run_tests'
        ]
    for tool in presented:
        if tool['function']['name'] == 'run_command':
            properties = tool['function']['parameters']['properties']
            allowed = commands | ({'tests'} if test_config is not None else set())
            properties['name'] = {**properties['name'], 'enum': sorted(allowed)}
    return presented
