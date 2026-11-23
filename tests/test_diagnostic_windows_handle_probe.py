"""Windows-only tests using disposable files and already-open handles."""
import ctypes
import os
from pathlib import Path
import tempfile
import unittest
from ctypes import wintypes

from diagnostic_windows_handle_probe import (
    FILE_INFO_ID,
    FILE_READ_DATA,
    HandleProbeError,
    HandleProbeUnsupported,
    WindowsHandleProbe,
    _FileIdInfo,
    _FileStandardInfo,
)


@unittest.skipUnless(os.name == "nt", "Windows handle APIs are required")
class WindowsHandleProbeTests(unittest.TestCase):
    GENERIC_READ = 0x80000000
    GENERIC_WRITE = 0x40000000
    SHARE_ALL = 0x00000007
    OPEN_EXISTING = 3
    OPEN_REPARSE_POINT = 0x00200000
    INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value

    @classmethod
    def setUpClass(cls):
        cls.kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        cls.kernel32.CreateFileW.argtypes = [
            wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p,
            wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
        cls.kernel32.CreateFileW.restype = ctypes.c_void_p
        cls.kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        cls.kernel32.CloseHandle.restype = wintypes.BOOL
        cls.kernel32.CreateHardLinkW.argtypes = [wintypes.LPCWSTR, wintypes.LPCWSTR, ctypes.c_void_p]
        cls.kernel32.CreateHardLinkW.restype = wintypes.BOOL

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.probe = WindowsHandleProbe()
        self.open_handles = []

    def tearDown(self):
        for handle in reversed(self.open_handles):
            self.kernel32.CloseHandle(wintypes.HANDLE(handle))
        self.temp.cleanup()

    def _file(self, name="source.dat", content=b"disposable test data"):
        path = self.root / name
        path.write_bytes(content)
        return path

    def _open(self, path, access=GENERIC_READ, flags=0):
        handle = self.kernel32.CreateFileW(
            str(path), access, self.SHARE_ALL, None, self.OPEN_EXISTING, flags, None)
        value = ctypes.cast(handle, ctypes.c_void_p).value
        if value in {None, self.INVALID_HANDLE_VALUE}:
            raise OSError(ctypes.get_last_error(), "CreateFileW test setup failed")
        self.open_handles.append(value)
        return value

    def _close(self, handle):
        self.kernel32.CloseHandle(wintypes.HANDLE(handle))
        self.open_handles.remove(handle)

    def test_valid_read_only_handle_reports_access_and_stable_identity(self):
        path = self._file()
        handle = self._open(path)
        result = self.probe.inspect(handle)
        self.assertTrue(result.granted_access & FILE_READ_DATA)
        self.assertEqual(result.granted_access, 0x00120089)
        self.assertTrue(result.identity_stable)
        self.assertEqual(len(result.identity.file_id), 16)
        self.assertNotEqual(result.identity.file_id, bytes(16))
        self.assertEqual(result.identity.size, len(b"disposable test data"))
        self.assertEqual(result.link_count, 1)
        self.assertTrue(result.expected_identity_matched)
        self.assertNotIn("CreateFileW", result.api_calls)

    def test_writable_handle_is_rejected(self):
        handle = self._open(self._file(), self.GENERIC_READ | self.GENERIC_WRITE)
        with self.assertRaisesRegex(HandleProbeError, "non-read-only"):
            self.probe.inspect(handle)

    def test_invalid_closed_and_non_handle_values_are_rejected(self):
        for handle in (0, -1, 0x76543210):
            with self.subTest(handle=handle), self.assertRaises(HandleProbeError):
                self.probe.inspect(handle)
        handle = self._open(self._file("closed.dat"))
        self._close(handle)
        with self.assertRaises(HandleProbeError):
            self.probe.inspect(handle)
        with self.assertRaises(HandleProbeError):
            self.probe.inspect(True)

    def test_source_substitution_and_expected_identity_mismatch_are_rejected(self):
        first = self._open(self._file("first.dat", b"first"))
        identity = self.probe.inspect(first).identity
        second = self._open(self._file("second.dat", b"other"))
        with self.assertRaisesRegex(HandleProbeError, "does not match"):
            self.probe.inspect(second, expected_identity=identity)

    def test_reparse_point_handle_is_rejected(self):
        target = self._file("target.dat")
        link = self.root / "link.dat"
        try:
            os.symlink(target, link)
        except (OSError, NotImplementedError) as exc:
            self.skipTest(f"Symlink creation unavailable: {exc}")
        handle = self._open(link, flags=self.OPEN_REPARSE_POINT)
        with self.assertRaisesRegex(HandleProbeError, "Reparse-point"):
            self.probe.inspect(handle)

    def test_hard_link_is_rejected(self):
        original = self._file("hard-source.dat")
        alias = self.root / "hard-alias.dat"
        if not self.kernel32.CreateHardLinkW(str(alias), str(original), None):
            self.skipTest(f"Hard-link creation unavailable: WinError {ctypes.get_last_error()}")
        handle = self._open(original)
        with self.assertRaisesRegex(HandleProbeError, "Hard-linked"):
            self.probe.inspect(handle)

    def test_identity_mismatch_is_rejected(self):
        first = self._open(self._file("identity-a.dat", b"a"))
        second = self._open(self._file("identity-b.dat", b"b"))
        expected = self.probe.inspect(first).identity
        with self.assertRaises(HandleProbeError):
            self.probe.inspect(second, expected_identity=expected)

    def test_restricted_worker_token_can_probe_preopened_handle(self):
        handle = self._open(self._file("restricted.dat"))
        expected = self.probe.inspect(handle).identity
        restricted = _make_restricted_impersonation_token()
        try:
            self.assertTrue(restricted.is_restricted)
            self.assertTrue(restricted.impersonate())
            result = self.probe.inspect(handle, expected_identity=expected)
            self.assertTrue(result.identity_stable)
        finally:
            restricted.revert_and_close()

    def test_unsupported_native_rights_query_fails_closed(self):
        class UnsupportedApi:
            def granted_access(self, handle):
                raise HandleProbeUnsupported("simulated unsupported NtQueryObject")

        with self.assertRaisesRegex(HandleProbeUnsupported, "simulated unsupported"):
            WindowsHandleProbe(UnsupportedApi()).inspect(0x1234)

    def test_unsupported_file_identity_query_fails_closed(self):
        native = self.probe._api

        class UnsupportedIdentityApi:
            granted_access = native.granted_access
            file_type = native.file_type

            def file_information(self, handle, info_class, structure):
                if info_class == FILE_INFO_ID:
                    raise HandleProbeUnsupported("simulated FileIdInfo unsupported")
                return native.file_information(handle, info_class, structure)

        handle = self._open(self._file("unsupported-id.dat"))
        with self.assertRaisesRegex(HandleProbeUnsupported, "FileIdInfo unsupported"):
            WindowsHandleProbe(UnsupportedIdentityApi()).inspect(handle)


class _RestrictedToken:
    def __init__(self, token, impersonation_token, sid, advapi, kernel32):
        self.token = token
        self.impersonation_token = impersonation_token
        self.sid = sid
        self.advapi = advapi
        self.kernel32 = kernel32
        self.is_restricted = bool(self.advapi.IsTokenRestricted(self.impersonation_token))

    def impersonate(self):
        return bool(self.advapi.ImpersonateLoggedOnUser(self.impersonation_token))

    def revert_and_close(self):
        self.advapi.RevertToSelf()
        for handle in (self.impersonation_token, self.token):
            if handle:
                self.kernel32.CloseHandle(handle)
        if self.sid:
            self.kernel32.LocalFree(self.sid)


def _make_restricted_impersonation_token():
    advapi = ctypes.WinDLL("advapi32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    handle = wintypes.HANDLE()
    process = kernel32.GetCurrentProcess()
    advapi.OpenProcessToken.argtypes = [wintypes.HANDLE, wintypes.DWORD, ctypes.POINTER(wintypes.HANDLE)]
    advapi.OpenProcessToken.restype = wintypes.BOOL
    if not advapi.OpenProcessToken(process, 0x0002 | 0x0008, ctypes.byref(handle)):
        raise OSError(ctypes.get_last_error(), "OpenProcessToken failed")

    sid = ctypes.c_void_p()
    advapi.ConvertStringSidToSidW.argtypes = [wintypes.LPCWSTR, ctypes.POINTER(ctypes.c_void_p)]
    advapi.ConvertStringSidToSidW.restype = wintypes.BOOL
    if not advapi.ConvertStringSidToSidW("S-1-5-12", ctypes.byref(sid)):
        kernel32.CloseHandle(handle)
        raise OSError(ctypes.get_last_error(), "ConvertStringSidToSidW failed")

    class SidAndAttributes(ctypes.Structure):
        _fields_ = [("sid", ctypes.c_void_p), ("attributes", wintypes.DWORD)]

    restricting_sid = SidAndAttributes(sid, 0)
    restricted = wintypes.HANDLE()
    advapi.CreateRestrictedToken.argtypes = [
        wintypes.HANDLE, wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p,
        wintypes.DWORD, ctypes.c_void_p, wintypes.DWORD,
        ctypes.POINTER(SidAndAttributes), ctypes.POINTER(wintypes.HANDLE)]
    advapi.CreateRestrictedToken.restype = wintypes.BOOL
    if not advapi.CreateRestrictedToken(handle, 0x1, 0, None, 0, None, 1,
                                        ctypes.byref(restricting_sid), ctypes.byref(restricted)):
        kernel32.CloseHandle(handle)
        kernel32.LocalFree(sid)
        raise OSError(ctypes.get_last_error(), "CreateRestrictedToken failed")

    impersonation = wintypes.HANDLE()
    advapi.DuplicateTokenEx.argtypes = [
        wintypes.HANDLE, wintypes.DWORD, ctypes.c_void_p, ctypes.c_int, ctypes.c_int,
        ctypes.POINTER(wintypes.HANDLE)]
    advapi.DuplicateTokenEx.restype = wintypes.BOOL
    if not advapi.DuplicateTokenEx(restricted, 0x0008 | 0x0004, None, 2, 2,
                                   ctypes.byref(impersonation)):
        kernel32.CloseHandle(restricted)
        kernel32.CloseHandle(handle)
        kernel32.LocalFree(sid)
        raise OSError(ctypes.get_last_error(), "DuplicateTokenEx failed")
    kernel32.CloseHandle(restricted)
    return _RestrictedToken(handle, impersonation, sid, advapi, kernel32)


if __name__ == "__main__":
    unittest.main()
