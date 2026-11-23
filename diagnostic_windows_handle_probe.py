"""Minimal Windows handle probe for disposable-file tests.

The probe never opens or resolves a source path. It accepts an already-open
handle, queries its granted access mask and file metadata, and fails closed if
the native rights query or stable file-ID APIs are unavailable. This module is
not a collector, filesystem parser, Broker, or production source adapter.
"""
from __future__ import annotations

from dataclasses import dataclass
import os
import ctypes
from ctypes import wintypes


class HandleProbeError(RuntimeError):
    """The supplied handle is invalid, unsafe, unsupported, or mismatched."""


class HandleProbeUnsupported(HandleProbeError):
    """The OS/filesystem does not provide a required query safely."""


@dataclass(frozen=True)
class FileIdentity:
    volume_serial: int
    file_id: bytes
    size: int


@dataclass(frozen=True)
class HandleProbeResult:
    granted_access: int
    identity: FileIdentity
    file_type: int
    attributes: int
    reparse_tag: int
    link_count: int
    directory: bool
    identity_stable: bool
    expected_identity_matched: bool
    api_calls: tuple[str, ...]


# Generic read maps to the standard read rights plus read-data/EA/attributes
# and synchronize. Unknown bits are rejected, not silently permitted.
FILE_GENERIC_READ = 0x00120089
FILE_READ_DATA = 0x00000001
FILE_READ_EA = 0x00000008
FILE_READ_ATTRIBUTES = 0x00000080
READ_CONTROL = 0x00020000
SYNCHRONIZE = 0x00100000
READ_ONLY_MASK = FILE_READ_DATA | FILE_READ_EA | FILE_READ_ATTRIBUTES | READ_CONTROL | SYNCHRONIZE
FILE_TYPE_DISK = 0x0001
FILE_ATTRIBUTE_DIRECTORY = 0x0010
FILE_ATTRIBUTE_REPARSE_POINT = 0x0400
FILE_INFO_STANDARD = 1
FILE_INFO_ATTRIBUTE_TAG = 9
FILE_INFO_ID = 18
OBJECT_BASIC_INFORMATION = 0
STATUS_SUCCESS = 0
STATUS_INVALID_HANDLE = 0xC0000008
UNSUPPORTED_WINERRORS = {1, 50, 87, 120, 124}


class _ObjectBasicInformation(ctypes.Structure):
    _fields_ = [
        ("attributes", wintypes.ULONG),
        ("granted_access", wintypes.ULONG),
        ("handle_count", wintypes.ULONG),
        ("pointer_count", wintypes.ULONG),
        ("reserved", wintypes.ULONG * 10),
    ]


class _FileIdInfo(ctypes.Structure):
    _fields_ = [("volume_serial", ctypes.c_ulonglong), ("file_id", ctypes.c_ubyte * 16)]


class _FileStandardInfo(ctypes.Structure):
    _fields_ = [
        ("allocation_size", ctypes.c_longlong),
        ("end_of_file", ctypes.c_longlong),
        ("number_of_links", wintypes.DWORD),
        ("delete_pending", wintypes.BOOLEAN),
        ("directory", wintypes.BOOLEAN),
    ]


class _FileAttributeTagInfo(ctypes.Structure):
    _fields_ = [("file_attributes", wintypes.DWORD), ("reparse_tag", wintypes.DWORD)]


class _WindowsApis:
    """Small query-only API facade, injectable for unsupported-API tests."""

    def __init__(self):
        if os.name != "nt":
            raise HandleProbeUnsupported("Windows handle APIs are unavailable on this platform.")
        self.kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        self.ntdll = ctypes.WinDLL("ntdll")
        self.kernel32.GetFileType.argtypes = [wintypes.HANDLE]
        self.kernel32.GetFileType.restype = wintypes.DWORD
        self.kernel32.GetFileInformationByHandleEx.argtypes = [
            wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]
        self.kernel32.GetFileInformationByHandleEx.restype = wintypes.BOOL
        self.ntdll.NtQueryObject.argtypes = [
            wintypes.HANDLE, wintypes.ULONG, ctypes.c_void_p, wintypes.ULONG,
            ctypes.POINTER(wintypes.ULONG)]
        self.ntdll.NtQueryObject.restype = ctypes.c_long

    def granted_access(self, handle: int) -> int:
        info = _ObjectBasicInformation()
        returned = wintypes.ULONG()
        status = int(self.ntdll.NtQueryObject(
            wintypes.HANDLE(handle), OBJECT_BASIC_INFORMATION,
            ctypes.byref(info), ctypes.sizeof(info), ctypes.byref(returned))) & 0xFFFFFFFF
        if status == STATUS_INVALID_HANDLE:
            raise HandleProbeError("NtQueryObject reports an invalid or closed handle.")
        if status != STATUS_SUCCESS:
            raise HandleProbeUnsupported(f"NtQueryObject(ObjectBasicInformation) status 0x{status:08x}.")
        return int(info.granted_access)

    def file_type(self, handle: int) -> int:
        ctypes.set_last_error(0)
        result = int(self.kernel32.GetFileType(wintypes.HANDLE(handle)))
        error = ctypes.get_last_error()
        if result == 0 and error:
            raise HandleProbeError(f"GetFileType failed with WinError {error}.")
        return result

    def file_information(self, handle: int, info_class: int, structure):
        info = structure()
        ctypes.set_last_error(0)
        ok = self.kernel32.GetFileInformationByHandleEx(
            wintypes.HANDLE(handle), info_class, ctypes.byref(info), ctypes.sizeof(info))
        if not ok:
            error = ctypes.get_last_error()
            if error in UNSUPPORTED_WINERRORS:
                raise HandleProbeUnsupported(
                    f"GetFileInformationByHandleEx class {info_class} unsupported (WinError {error}).")
            if error in {6, 87}:
                raise HandleProbeError(f"GetFileInformationByHandleEx rejected handle (WinError {error}).")
            raise HandleProbeError(
                f"GetFileInformationByHandleEx class {info_class} failed (WinError {error}).")
        return info


class WindowsHandleProbe:
    """Inspect one already-provisioned handle without reopening a pathname."""

    def __init__(self, api=None):
        self._api = api or _WindowsApis()

    def inspect(self, handle: int, *, expected_identity: FileIdentity | None = None) -> HandleProbeResult:
        if type(handle) is not int or handle in {0, -1}:
            raise HandleProbeError("A non-null, already-open Windows handle is required.")
        granted = self._api.granted_access(handle)
        if not granted & FILE_READ_DATA:
            raise HandleProbeError("Handle does not grant FILE_READ_DATA.")
        if granted & ~READ_ONLY_MASK:
            raise HandleProbeError(f"Handle has non-read-only or unrecognized access bits 0x{granted & ~READ_ONLY_MASK:08x}.")

        file_type = self._api.file_type(handle)
        if file_type != FILE_TYPE_DISK:
            raise HandleProbeError("Handle is not a disk-file handle.")
        first = self._identity(handle)
        attributes = self._api.file_information(
            handle, FILE_INFO_ATTRIBUTE_TAG, _FileAttributeTagInfo)
        standard = self._api.file_information(
            handle, FILE_INFO_STANDARD, _FileStandardInfo)
        if attributes.file_attributes & FILE_ATTRIBUTE_REPARSE_POINT:
            raise HandleProbeError("Reparse-point handles are rejected.")
        if attributes.file_attributes & FILE_ATTRIBUTE_DIRECTORY or standard.directory:
            raise HandleProbeError("Directory handles are not accepted by this file probe.")
        if int(standard.number_of_links) != 1:
            raise HandleProbeError("Hard-linked files are rejected.")
        second = self._identity(handle)
        if first != second:
            raise HandleProbeError("File identity or size changed during the probe.")
        matched = expected_identity is None or first == expected_identity
        if not matched:
            raise HandleProbeError("Supplied handle identity does not match the approved identity.")
        return HandleProbeResult(
            granted_access=granted,
            identity=first,
            file_type=file_type,
            attributes=int(attributes.file_attributes),
            reparse_tag=int(attributes.reparse_tag),
            link_count=int(standard.number_of_links),
            directory=bool(standard.directory),
            identity_stable=True,
            expected_identity_matched=matched,
            api_calls=("NtQueryObject(ObjectBasicInformation)", "GetFileType",
                       "GetFileInformationByHandleEx(FileIdInfo)",
                       "GetFileInformationByHandleEx(FileAttributeTagInfo)",
                       "GetFileInformationByHandleEx(FileStandardInfo)"),
        )

    def _identity(self, handle: int) -> FileIdentity:
        info = self._api.file_information(handle, FILE_INFO_ID, _FileIdInfo)
        standard = self._api.file_information(handle, FILE_INFO_STANDARD, _FileStandardInfo)
        return FileIdentity(int(info.volume_serial), bytes(info.file_id), int(standard.end_of_file))
