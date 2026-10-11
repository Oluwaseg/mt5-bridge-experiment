import ctypes
import json
import os
import tempfile
from ctypes import wintypes
from pathlib import Path


class DataBlob(ctypes.Structure):
    _fields_ = [("size", wintypes.DWORD), ("data", ctypes.POINTER(ctypes.c_ubyte))]


def _blob(data):
    buffer = (ctypes.c_ubyte * len(data)).from_buffer_copy(data)
    return DataBlob(len(data), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_ubyte))), buffer


def protect_for_current_user(data):
    if os.name != "nt":
        raise OSError("Windows DPAPI is available only on Windows")
    source, source_buffer = _blob(data)
    destination = DataBlob()
    crypt32 = ctypes.WinDLL("crypt32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    protect = crypt32.CryptProtectData
    protect.argtypes = [
        ctypes.POINTER(DataBlob),
        wintypes.LPCWSTR,
        ctypes.POINTER(DataBlob),
        ctypes.c_void_p,
        ctypes.c_void_p,
        wintypes.DWORD,
        ctypes.POINTER(DataBlob),
    ]
    protect.restype = wintypes.BOOL
    local_free = kernel32.LocalFree
    local_free.argtypes = [ctypes.c_void_p]
    local_free.restype = ctypes.c_void_p
    if not protect(ctypes.byref(source), "Profit Pilot Connector", None, None, None, 0, ctypes.byref(destination)):
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        return ctypes.string_at(destination.data, destination.size)
    finally:
        local_free(destination.data)
        del source_buffer


def unprotect_for_current_user(data):
    if os.name != "nt":
        raise OSError("Windows DPAPI is available only on Windows")
    source, source_buffer = _blob(data)
    destination = DataBlob()
    crypt32 = ctypes.WinDLL("crypt32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    unprotect = crypt32.CryptUnprotectData
    unprotect.argtypes = [
        ctypes.POINTER(DataBlob),
        ctypes.POINTER(wintypes.LPWSTR),
        ctypes.POINTER(DataBlob),
        ctypes.c_void_p,
        ctypes.c_void_p,
        wintypes.DWORD,
        ctypes.POINTER(DataBlob),
    ]
    unprotect.restype = wintypes.BOOL
    local_free = kernel32.LocalFree
    local_free.argtypes = [ctypes.c_void_p]
    local_free.restype = ctypes.c_void_p
    if not unprotect(ctypes.byref(source), None, None, None, None, 0, ctypes.byref(destination)):
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        return ctypes.string_at(destination.data, destination.size)
    finally:
        local_free(destination.data)
        del source_buffer


class WindowsCredentialStore:
    def __init__(self, path=None, protect=protect_for_current_user, unprotect=unprotect_for_current_user):
        if os.name != "nt" and protect is protect_for_current_user:
            raise OSError("Profit Pilot Connector credential storage requires Windows DPAPI")
        base = Path(os.environ.get("LOCALAPPDATA", Path.home())) / "ProfitPilot"
        self.path = Path(path) if path else base / "connector.dat"
        self.protect = protect
        self.unprotect = unprotect

    def load(self):
        try:
            encrypted = self.path.read_bytes()
        except FileNotFoundError:
            return None
        except OSError:
            return None
        try:
            record = json.loads(self.unprotect(encrypted).decode("utf-8"))
        except (OSError, ValueError, UnicodeDecodeError, json.JSONDecodeError):
            return None
        required = ("connectorId", "accountId", "credential", "serverUrl", "terminalPath")
        if not isinstance(record, dict) or not all(isinstance(record.get(key), str) and record[key] for key in required):
            return None
        return record

    def save(self, record):
        required = ("connectorId", "accountId", "credential", "serverUrl", "terminalPath")
        if not isinstance(record, dict) or not all(isinstance(record.get(key), str) and record[key] for key in required):
            raise ValueError("Connector credential record is incomplete")
        encrypted = self.protect(json.dumps(record, separators=(",", ":")).encode("utf-8"))
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, temporary_path = tempfile.mkstemp(prefix="connector-", dir=self.path.parent)
        try:
            with os.fdopen(fd, "wb") as stream:
                stream.write(encrypted)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary_path, self.path)
        finally:
            try:
                os.unlink(temporary_path)
            except FileNotFoundError:
                pass

    def delete(self):
        try:
            self.path.unlink()
        except FileNotFoundError:
            pass
