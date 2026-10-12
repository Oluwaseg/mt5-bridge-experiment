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

    @staticmethod
    def _valid_record(record):
        required = ("connectorId", "accountId", "credential", "serverUrl", "terminalPath")
        return isinstance(record, dict) and all(
            isinstance(record.get(key), str) and record[key] for key in required
        )

    def _read_payload(self):
        try:
            encrypted = self.path.read_bytes()
        except (FileNotFoundError, OSError):
            return None
        try:
            return json.loads(self.unprotect(encrypted).decode("utf-8"))
        except (OSError, ValueError, UnicodeDecodeError, json.JSONDecodeError):
            return None

    def load(self):
        records = self.load_all()
        return records[0] if records else None

    def load_all(self):
        payload = self._read_payload()
        if self._valid_record(payload):
            return [payload]
        if not isinstance(payload, dict) or payload.get("version") != 2:
            return []
        records = payload.get("records")
        if not isinstance(records, list):
            return []
        return [record for record in records if self._valid_record(record)]

    def save(self, record):
        if not self._valid_record(record):
            raise ValueError("Connector credential record is incomplete")
        records = [existing for existing in self.load_all() if existing["connectorId"] != record["connectorId"]]
        records.append(dict(record))
        self.save_all(records)

    def save_all(self, records):
        if not isinstance(records, list) or any(not self._valid_record(record) for record in records):
            raise ValueError("Connector credential records are incomplete")
        connector_ids = [record["connectorId"] for record in records]
        if len(connector_ids) != len(set(connector_ids)):
            raise ValueError("Connector IDs must be unique")
        if not records:
            self.delete()
            return
        payload = {"version": 2, "records": records}
        encrypted = self.protect(json.dumps(payload, separators=(",", ":")).encode("utf-8"))
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

    def delete(self, connector_id=None):
        if connector_id is not None:
            records = [
                record
                for record in self.load_all()
                if record["connectorId"] != str(connector_id)
            ]
            if records:
                self.save_all(records)
                return
        try:
            self.path.unlink()
        except FileNotFoundError:
            pass
