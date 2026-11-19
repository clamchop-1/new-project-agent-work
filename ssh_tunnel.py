"""Run-scoped, host-key-pinned Windows OpenSSH local forwarding."""
from __future__ import annotations

import base64
import hashlib
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import threading
import time

from inference_provider import ProviderFailure

STARTUP_TIMEOUT_SECONDS = 12.0
SHUTDOWN_TIMEOUT_SECONDS = 3.0
IDENTITY_TIMEOUT_SECONDS = 5.0


def _fingerprints_from_known_hosts(output: str) -> set[str]:
    fingerprints = set()
    for line in output.splitlines():
        line = line.strip()
        if not line or line.startswith('#'):
            continue
        columns = line.split()
        if columns and columns[0].startswith('@'):
            raise ProviderFailure('identity_mismatch', 'Enrolled SSH alias is revoked or uses an unpinned certificate authority.', security=True)
        if len(columns) < 3:
            continue
        try:
            key_blob = base64.b64decode(columns[2], validate=True)
        except (ValueError, base64.binascii.Error):
            continue
        digest = base64.b64encode(hashlib.sha256(key_blob).digest()).decode('ascii').rstrip('=')
        fingerprints.add('SHA256:' + digest)
    return fingerprints


def verify_enrolled_host(alias: str, known_hosts_file: str, expected_fingerprint: str,
                         run=subprocess.run, which=shutil.which) -> None:
    """Require one direct enrolled key for alias and exact configured fingerprint."""
    if sys.platform != 'win32':
        raise ProviderFailure('unsupported_platform', 'Managed SSH inference tunnels require Windows.', security=True)
    if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._-]{0,99}', alias):
        raise ProviderFailure('identity_mismatch', 'Configured SSH host alias is invalid.', security=True)
    path = Path(known_hosts_file)
    if not path.is_absolute() or not path.is_file() or path.is_symlink():
        raise ProviderFailure('identity_mismatch', 'Selected known-hosts file is unavailable or unsafe.', security=True)
    if path.stat().st_size > 1024 * 1024:
        raise ProviderFailure('identity_mismatch', 'Selected known-hosts file exceeds the configured limit.', security=True)
    keygen = which('ssh-keygen')
    if not keygen:
        raise ProviderFailure('identity_mismatch', 'OpenSSH key verification is unavailable.', security=True)
    try:
        result = run([keygen, '-F', alias, '-f', str(path)], capture_output=True,
                     text=True, timeout=IDENTITY_TIMEOUT_SECONDS, check=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ProviderFailure('identity_mismatch', 'Could not verify the enrolled SSH host key.', security=True) from exc
    if result.returncode != 0:
        raise ProviderFailure('identity_mismatch', 'SSH host alias has no enrolled key in the selected known-hosts file.', security=True)
    found = _fingerprints_from_known_hosts(result.stdout or '')
    if found != {expected_fingerprint}:
        raise ProviderFailure('identity_mismatch', 'Enrolled SSH host key does not exactly match the configured fingerprint.', security=True)


class ManagedSshTunnel:
    """Own and verify one SSH process and its loopback listener for one run."""

    def __init__(self, profile: dict, popen=subprocess.Popen, run=subprocess.run,
                 which=shutil.which, platform=None, monotonic=time.monotonic,
                 sleep=time.sleep):
        self.profile = dict(profile)
        self._popen = popen
        self._run = run
        self._which = which
        self._platform = platform or sys.platform
        self._monotonic = monotonic or time.monotonic
        self._sleep = sleep or time.sleep
        self._process = None
        self._lock = threading.RLock()
        self._stderr = bytearray()
        self._stderr_lock = threading.Lock()
        self._stderr_thread = None

    def _validate_profile(self):
        required = {'enabled', 'profile_id', 'port', 'ssh_host_alias',
                    'ssh_host_key_sha256', 'credential_ref', 'known_hosts_file',
                    'ssh_config_file'}
        p = self.profile
        if set(p) != required or p['enabled'] is not True or p['credential_ref'] != 'ssh-agent':
            raise ProviderFailure('identity_mismatch', 'Remote inference requires an explicitly enabled managed SSH profile.', security=True)
        if type(p['port']) is not int or not 20000 <= p['port'] <= 29999:
            raise ProviderFailure('port_conflict', 'SSH forwarded port is outside the reserved range.', security=True)
        for field in ('known_hosts_file', 'ssh_config_file'):
            value = p[field]
            path = Path(value) if isinstance(value, str) else Path()
            if not path.is_absolute() or not path.is_file() or path.is_symlink():
                raise ProviderFailure('identity_mismatch', f'Selected {field} is unavailable or unsafe.', security=True)
            if path.stat().st_size > 1024 * 1024:
                raise ProviderFailure('identity_mismatch', f'Selected {field} exceeds the configured limit.', security=True)

    def _ssh_path(self):
        ssh = self._which('ssh')
        if not ssh:
            raise ProviderFailure('tunnel_unavailable', 'Windows OpenSSH is unavailable.', retryable=True)
        return ssh

    def _argv(self):
        p = self.profile
        return [
            self._ssh_path(), '-F', p['ssh_config_file'],
            '-o', 'UserKnownHostsFile=' + p['known_hosts_file'],
            '-o', 'GlobalKnownHostsFile=NUL',
            '-o', 'StrictHostKeyChecking=yes',
            '-o', 'BatchMode=yes',
            '-o', 'PreferredAuthentications=publickey',
            '-o', 'PubkeyAuthentication=yes',
            '-o', 'PasswordAuthentication=no',
            '-o', 'KbdInteractiveAuthentication=no',
            '-o', 'ForwardAgent=no',
            '-o', 'PermitLocalCommand=no',
            '-o', 'ProxyCommand=none',
            '-o', 'ProxyJump=none',
            # ClearAllForwardings=yes also removes the explicit -L below.
            # Check the effective forwarding plan before launch instead.
            '-o', 'ClearAllForwardings=no',
            '-o', 'ExitOnForwardFailure=yes',
            '-L', f"127.0.0.1:{p['port']}:127.0.0.1:11434",
            '-N', p['ssh_host_alias'],
        ]

    def _verify_forward_plan(self):
        """Require the effective SSH config to contain only this local forward."""
        argv = self._argv()
        argv[argv.index('-N')] = '-G'
        try:
            result = self._run(
                argv, capture_output=True, text=True,
                timeout=IDENTITY_TIMEOUT_SECONDS, check=False,
                creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0),
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise ProviderFailure(
                'forwarding_unverified',
                'Could not verify the effective SSH forwarding configuration.',
                security=True,
            ) from exc
        if result.returncode != 0:
            raise ProviderFailure(
                'forwarding_unverified',
                'Windows OpenSSH could not verify the effective forwarding configuration.',
                security=True,
            )
        forwards = {'localforward': [], 'remoteforward': [], 'dynamicforward': []}
        for line in (result.stdout or '').splitlines():
            key, separator, value = line.partition(' ')
            if key in forwards and separator:
                forwards[key].append(value.strip())
        expected = f"[127.0.0.1]:{self.profile['port']} [127.0.0.1]:11434"
        if (forwards['localforward'] != [expected]
                or forwards['remoteforward'] or forwards['dynamicforward']):
            raise ProviderFailure(
                'forwarding_unverified',
                'SSH configuration contains unexpected or unverifiable forwarding rules.',
                security=True,
            )

    def _listener_pids(self):
        port = self.profile['port']
        command = ('@(Get-NetTCPConnection -State Listen -LocalPort ' + str(port)
                   + ' -ErrorAction SilentlyContinue | Select-Object -ExpandProperty OwningProcess) -join ","')
        ps = self._which('powershell.exe')
        if not ps:
            raise ProviderFailure('listener_unverified', 'Cannot verify Windows listener ownership.', security=True)
        try:
            result = self._run([ps, '-NoProfile', '-NonInteractive', '-Command', command],
                               capture_output=True, text=True, timeout=2,
                               check=False, creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise ProviderFailure('listener_unverified', 'Could not verify SSH tunnel listener ownership.', security=True) from exc
        if result.returncode != 0:
            raise ProviderFailure('listener_unverified', 'Windows could not verify SSH tunnel listener ownership.', security=True)
        rendered = (result.stdout or '').strip()
        if not rendered:
            return set()
        try:
            return {int(value.strip()) for value in rendered.split(',') if value.strip()}
        except ValueError as exc:
            raise ProviderFailure('listener_unverified', 'Windows returned malformed listener ownership data.', security=True) from exc

    def _drain_stderr(self, stream):
        try:
            while True:
                chunk = stream.read(512)
                if not chunk:
                    return
                with self._stderr_lock:
                    remaining = 4096 - len(self._stderr)
                    if remaining > 0:
                        self._stderr.extend(chunk[:remaining])
        except (OSError, ValueError):
            return

    def _startup_failure(self):
        with self._stderr_lock:
            message = bytes(self._stderr).decode('utf-8', errors='ignore').casefold()
        if 'host key verification failed' in message or 'remote host identification has changed' in message:
            return ProviderFailure('identity_mismatch', 'OpenSSH rejected the Mac host identity.', security=True)
        if 'permission denied' in message or 'no supported authentication methods' in message:
            return ProviderFailure('authentication', 'OpenSSH public-key authentication failed.')
        if ('cannot listen to port' in message or 'address already in use' in message
                or 'address cannot be assigned' in message):
            return ProviderFailure('port_conflict', 'OpenSSH could not bind the configured forwarding port.', security=True)
        return ProviderFailure('tunnel_unavailable', 'Windows OpenSSH tunnel failed to start.', retryable=True)

    def ensure_ready(self, cancel_event=None):
        with self._lock:
            if self._platform != 'win32':
                raise ProviderFailure('unsupported_platform', 'Managed SSH inference tunnels require Windows.', security=True)
            self._validate_profile()
            verify_enrolled_host(self.profile['ssh_host_alias'], self.profile['known_hosts_file'],
                                 self.profile['ssh_host_key_sha256'], self._run, self._which)
            self._verify_forward_plan()
            if self._process is not None:
                if self._process.poll() is not None:
                    self._process = None
                    raise ProviderFailure('tunnel_unavailable', 'Managed SSH tunnel process is stale or has exited.', retryable=True)
                if self._listener_pids() != {self._process.pid}:
                    self.close()
                    raise ProviderFailure('listener_unverified', 'Managed SSH listener ownership changed.', security=True)
                return True
            if self._listener_pids():
                raise ProviderFailure('port_conflict', 'Configured SSH tunnel port is already occupied by an unexpected process.', security=True)
            try:
                self._process = self._popen(
                    self._argv(), stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                    stderr=subprocess.PIPE, shell=False,
                    creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0),
                )
            except OSError as exc:
                self._process = None
                raise ProviderFailure('tunnel_unavailable', 'Could not start the managed Windows OpenSSH tunnel.', retryable=True) from exc
            stderr_stream = getattr(self._process, 'stderr', None)
            if stderr_stream is not None:
                self._stderr = bytearray()
                self._stderr_thread = threading.Thread(
                    target=self._drain_stderr, args=(stderr_stream,), daemon=True
                )
                self._stderr_thread.start()
            deadline = self._monotonic() + STARTUP_TIMEOUT_SECONDS
            while self._monotonic() < deadline:
                if cancel_event is not None and cancel_event.is_set():
                    self.close()
                    raise ProviderFailure('cancelled', 'SSH tunnel startup cancelled.')
                if self._process.poll() is not None:
                    failure = self._startup_failure()
                    if failure.code == 'tunnel_unavailable':
                        failure = ProviderFailure('tunnel_unavailable', 'SSH tunnel process exited before establishing its forwarding listener.', retryable=True)
                    self.close()
                    raise failure
                try:
                    owners = self._listener_pids()
                except BaseException:
                    try:
                        self.close()
                    finally:
                        raise
                if owners:
                    if owners != {self._process.pid}:
                        self.close()
                        raise ProviderFailure('port_conflict', 'Configured tunnel port was claimed by an unexpected process.', security=True)
                    return True
                self._sleep(0.1)
            self.close()
            raise ProviderFailure('tunnel_timeout', 'Timed out waiting for the managed SSH forwarding listener.', retryable=True)

    def close(self):
        with self._lock:
            process = self._process
            if process is None:
                return True
            self._process = None
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=SHUTDOWN_TIMEOUT_SECONDS)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=SHUTDOWN_TIMEOUT_SECONDS)
            if process.poll() is None:
                raise ProviderFailure('cleanup_failed', 'Managed SSH process did not stop; remote inference remains unavailable.', security=True)
            if self._stderr_thread is not None:
                self._stderr_thread.join(timeout=0.5)
                self._stderr_thread = None
            owners = self._listener_pids()
            if process.pid in owners:
                raise ProviderFailure('cleanup_failed', 'Managed SSH listener remained after its process stopped.', security=True)
            return True
