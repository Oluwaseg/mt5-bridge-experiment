import asyncio
import ctypes
import hashlib
import json
import ipaddress
import os
import platform
import re
import runpy
import secrets
import subprocess
import sys
import socket
import threading
import time
import traceback
import urllib.error
import urllib.request
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit
from .diagnostics import diagnostic_path, log_diagnostic

DEFAULT_API_BASE_URL = "https://server--profit-pilot-server--w46tx4dn8rj4.code.run/api"
CONNECTOR_NAME = "Profit Pilot Connector"


class ConnectorError(Exception):
    pass


class PairingCodeExpired(ConnectorError):
    pass


class PairingRateLimited(ConnectorError):
    pass


def validate_api_base_url(value):
    parsed = urlsplit(str(value or ""))
    if not parsed.hostname or parsed.username or parsed.password:
        raise ValueError("Connector backend URL is invalid")
    if parsed.scheme == "https":
        return str(value).rstrip("/")
    try:
        loopback = ipaddress.ip_address(parsed.hostname).is_loopback
    except ValueError:
        loopback = parsed.hostname.casefold() == "localhost"
    if parsed.scheme != "http" or not loopback:
        raise ValueError("Connector backend URL must use HTTPS unless it is local")
    return str(value).rstrip("/")


def worker_websocket_url(api_base_url):
    base_url = validate_api_base_url(api_base_url)
    parsed = urlsplit(base_url)
    path = parsed.path.rstrip("/")
    if path.endswith("/api"):
        path = path[:-4]
    scheme = "wss" if parsed.scheme == "https" else "ws"
    return urlunsplit((scheme, parsed.netloc, f"{path}/api/worker-connect", "", ""))


def exchange_pairing_code(code, api_base_url=DEFAULT_API_BASE_URL, opener=None):
    base_url = validate_api_base_url(api_base_url)
    normalized = str(code or "").strip().replace(" ", "").lower()
    if len(normalized) != 32 or any(char not in "0123456789abcdef" for char in normalized):
        raise ValueError("Enter the 32-character pairing code shown in Profit Pilot")

    payload = json.dumps({"code": normalized, "name": CONNECTOR_NAME}).encode("utf-8")
    request = urllib.request.Request(
        f"{base_url}/mt5/connectors/exchange",
        data=payload,
        headers={"Content-Type": "application/json", "Accept": "application/json"},
        method="POST",
    )
    open_request = opener or urllib.request.urlopen
    try:
        with open_request(request, timeout=15) as response:
            result = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as error:
        error.close()
        if error.code == 400:
            raise PairingCodeExpired("Pairing code is invalid or expired. Generate a fresh code on the website.") from None
        if error.code == 429:
            raise PairingRateLimited("Too many pairing attempts. Wait a few minutes and try again.") from None
        if error.code in (502, 503, 504):
            raise ConnectorError("Profit Pilot is temporarily unavailable. Try again shortly.") from None
        raise ConnectorError(f"Pairing failed with HTTP {error.code}. Check your connection and try again.") from None
    except (urllib.error.URLError, TimeoutError, OSError):
        raise ConnectorError("Could not reach Profit Pilot over HTTPS. Check your internet connection.") from None
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise ConnectorError("Profit Pilot returned an unreadable response. Try again later.") from None

    connector_id = result.get("connectorId")
    account_id = result.get("accountId")
    credential = result.get("credential")
    if not all(isinstance(value, str) and value for value in (connector_id, account_id, credential)):
        raise ConnectorError("Profit Pilot returned an incomplete pairing response. Contact support.")
    return {"connectorId": connector_id, "accountId": account_id, "credential": credential}


def discover_terminals():
    candidates = []
    if platform.system() != "Windows":
        return candidates

    roots = [
        os.environ.get("ProgramFiles", r"C:\Program Files"),
        os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)"),
        os.environ.get("LOCALAPPDATA", ""),
    ]
    for root in filter(None, roots):
        path = Path(root)
        try:
            candidates.extend(path.glob("MetaTrader */terminal64.exe"))
            candidates.extend(path.glob("MetaTrader 5*/terminal64.exe"))
        except OSError:
            continue
    unique = sorted({str(path.resolve()) for path in candidates if path.is_file()})
    return unique


def bundled_script_path(script_name):
    bundle_root = getattr(sys, "_MEIPASS", None)
    if bundle_root:
        return Path(bundle_root) / "bridge" / script_name
    return Path(__file__).resolve().parent / script_name


def get_free_loopback_port():
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


def _minimal_child_environment():
    allowed = (
        "PATH",
        "SYSTEMROOT",
        "WINDIR",
        "TEMP",
        "TMP",
        "USERPROFILE",
        "APPDATA",
        "LOCALAPPDATA",
        "PROGRAMFILES",
        "PROGRAMFILES(X86)",
        "COMMONPROGRAMFILES",
    )
    return {key: os.environ[key] for key in allowed if key in os.environ}


def terminate_owned_process(process, timeout=5):
    if process.poll() is not None:
        return
    try:
        process.terminate()
        process.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=timeout)


def owned_process_descendants(parent_pid):
    if os.name != "nt":
        return []

    from ctypes import wintypes

    class ProcessEntry32(ctypes.Structure):
        _fields_ = [
            ("dwSize", wintypes.DWORD),
            ("cntUsage", wintypes.DWORD),
            ("th32ProcessID", wintypes.DWORD),
            ("th32DefaultHeapID", ctypes.c_size_t),
            ("th32ModuleID", wintypes.DWORD),
            ("cntThreads", wintypes.DWORD),
            ("th32ParentProcessID", wintypes.DWORD),
            ("pcPriClassBase", ctypes.c_long),
            ("dwFlags", wintypes.DWORD),
            ("szExeFile", wintypes.WCHAR * 260),
        ]

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    snapshot = kernel32.CreateToolhelp32Snapshot
    snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
    snapshot.restype = wintypes.HANDLE
    first = kernel32.Process32FirstW
    first.argtypes = [wintypes.HANDLE, ctypes.POINTER(ProcessEntry32)]
    first.restype = wintypes.BOOL
    next_process = kernel32.Process32NextW
    next_process.argtypes = [wintypes.HANDLE, ctypes.POINTER(ProcessEntry32)]
    next_process.restype = wintypes.BOOL
    close_handle = kernel32.CloseHandle
    close_handle.argtypes = [wintypes.HANDLE]
    close_handle.restype = wintypes.BOOL

    invalid_handle = ctypes.c_void_p(-1).value
    handle = snapshot(0x00000002, 0)
    if handle == invalid_handle:
        return []

    try:
        processes = {}
        entry = ProcessEntry32()
        entry.dwSize = ctypes.sizeof(ProcessEntry32)
        has_entry = first(handle, ctypes.byref(entry))
        while has_entry:
            processes[int(entry.th32ProcessID)] = int(entry.th32ParentProcessID)
            has_entry = next_process(handle, ctypes.byref(entry))
    finally:
        close_handle(handle)

    descendants = []
    frontier = [int(parent_pid)]
    seen = {int(parent_pid)}
    while frontier:
        current_parent = frontier.pop(0)
        children = [
            pid for pid, process_parent in processes.items()
            if process_parent == current_parent and pid not in seen
        ]
        for child_pid in children:
            seen.add(child_pid)
            descendants.append(child_pid)
            frontier.append(child_pid)
    return descendants


class TerminalInstanceLock:
    def __init__(self, terminal_path):
        canonical_path = os.path.normcase(os.path.realpath(terminal_path))
        lock_name = hashlib.sha256(canonical_path.encode("utf-8")).hexdigest() + ".lock"
        base = Path(os.environ.get("LOCALAPPDATA", Path.home()))
        self.path = base / "ProfitPilot" / "terminal-locks" / lock_name
        self.stream = None

    def acquire(self):
        if os.name != "nt":
            raise ConnectorError("MT5 terminal instance locks are available only on Windows.")
        import msvcrt

        self.path.parent.mkdir(parents=True, exist_ok=True)
        stream = self.path.open("a+b")
        stream.seek(0, os.SEEK_END)
        if stream.tell() == 0:
            stream.write(b"\0")
            stream.flush()
        stream.seek(0)
        try:
            msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
        except OSError:
            stream.close()
            raise ConnectorError(
                "Another Profit Pilot connector instance is using this MT5 installation. Stop that account or select a separate installation."
            ) from None
        self.stream = stream

    def release(self):
        if not self.stream:
            return
        import msvcrt

        try:
            self.stream.seek(0)
            msvcrt.locking(self.stream.fileno(), msvcrt.LK_UNLCK, 1)
        finally:
            self.stream.close()
            self.stream = None


def smoke_test_bundled_bridge():
    port = get_free_loopback_port()
    env = _minimal_child_environment()
    env.update({
        "PORT": str(port),
        "MT5_BRIDGE_HOST": "127.0.0.1",
        "MT5_AGENT_SECRET": secrets.token_urlsafe(32),
        "MT5_TERMINAL_PATH": "",
        "PYTHON_DOTENV_DISABLED": "1",
        "PROFIT_PILOT_BUNDLE_SMOKE": "1",
    })
    script = bundled_script_path("probe.py")
    process = subprocess.Popen(
        [sys.executable, "--run-script", str(script)],
        env=env,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    child_output = []

    def capture_output():
        if process.stdout is None:
            return
        for line in process.stdout:
            child_output.append(line.rstrip())
            if len(child_output) > 40:
                del child_output[:-40]

    output_thread = threading.Thread(target=capture_output, daemon=True)
    output_thread.start()
    deadline = time.monotonic() + 15
    try:
        while time.monotonic() < deadline:
            if process.poll() is not None:
                diagnostic = "\n".join(child_output[-12:])
                raise ConnectorError(
                    "Packaged local bridge exited before its health endpoint started."
                    + (f"\nBridge startup output:\n{diagnostic}" if diagnostic else "")
                )
            try:
                with urllib.request.urlopen(
                    f"http://127.0.0.1:{port}/health", timeout=0.5
                ) as response:
                    if response.status == 200:
                        return True
            except urllib.error.HTTPError as error:
                error.close()
                if error.code == 503:
                    return True
            except (urllib.error.URLError, TimeoutError, OSError):
                pass
            threading.Event().wait(0.1)
        diagnostic = "\n".join(child_output[-12:])
        raise ConnectorError(
            "Packaged local bridge did not start within 15 seconds."
            + (f"\nBridge startup output:\n{diagnostic}" if diagnostic else "")
        )
    finally:
        terminate_owned_process(process)
        if output_thread.is_alive():
            output_thread.join(timeout=1)


def smoke_test_bundled_mt5_import():
    try:
        import numpy
    except Exception as error:
        diagnostic = traceback.format_exc()
        diagnostic_path = Path(os.environ.get("TEMP", Path.home())) / "ProfitPilotConnector-numpy-smoke.txt"
        diagnostic_path.write_text(diagnostic, encoding="utf-8")
        raise ConnectorError(
            f"Bundled NumPy import failed ({type(error).__name__}: {error}).\n"
            f"Full import trace written to {diagnostic_path}.\n{diagnostic}"
        ) from None
    try:
        import MetaTrader5
    except Exception as error:
        diagnostic = traceback.format_exc()
        diagnostic_path = Path(os.environ.get("TEMP", Path.home())) / "ProfitPilotConnector-mt5-smoke.txt"
        diagnostic_path.write_text(diagnostic, encoding="utf-8")
        raise ConnectorError(
            f"Bundled MetaTrader5 import failed with NumPy {numpy.__version__} "
            f"from {numpy.__file__} ({type(error).__name__}: {error}). "
            f"Full import trace written to {diagnostic_path}.\n{diagnostic}"
        ) from None
    return getattr(MetaTrader5, "__version__", "unknown")


async def _smoke_test_bundled_worker_async():
    from websockets.asyncio.server import serve

    heartbeat_received = asyncio.Event()

    async def handle_worker(connection):
        identify = json.loads(await asyncio.wait_for(connection.recv(), timeout=10))
        if identify.get("type") != "identify" or identify.get("token") != "bundle-smoke-credential":
            raise ConnectorError("Packaged worker did not send the existing identify message.")
        await connection.send(json.dumps({
            "type": "identity_ack",
            "workerId": "bundle-smoke-worker",
            "userId": "bundle-smoke-user",
            "accountId": "bundle-smoke-account",
            "accountConfig": {
                "accountId": "bundle-smoke-account",
                "login": "123456",
                "server": "Demo-Server",
                "password": "smoke-only-password",
            },
        }))
        await connection.send(json.dumps({
            "type": "command",
            "command": "ping",
            "requestId": "bundle-smoke-ping",
        }))
        pong = json.loads(await asyncio.wait_for(connection.recv(), timeout=10))
        if pong.get("type") != "pong" or pong.get("requestId") != "bundle-smoke-ping":
            raise ConnectorError("Packaged worker did not answer the existing ping command.")
        heartbeat = json.loads(await asyncio.wait_for(connection.recv(), timeout=10))
        if heartbeat.get("type") != "heartbeat":
            raise ConnectorError("Packaged worker did not send its existing heartbeat.")
        await connection.send(json.dumps({"type": "heartbeat_ack", "at": "smoke"}))
        heartbeat_received.set()
        await connection.close(code=4001, reason="Connector revoked")

    async with serve(handle_worker, "127.0.0.1", 0) as server:
        port = server.sockets[0].getsockname()[1]
        env = _minimal_child_environment()
        env.update({
            "WORKER_IDENTITY_SERVER_URL": f"ws://127.0.0.1:{port}/api/worker-connect",
            "WORKER_ID": "bundle-smoke-worker",
            "WORKER_IDENTITY_TOKEN": "bundle-smoke-credential",
            "REMOTE_WORKER_HEARTBEAT_INTERVAL_MS": "1000",
            "WORKER_RECONNECT_DELAY_SECONDS": "1",
            "PYTHON_DOTENV_DISABLED": "1",
            "PROFIT_PILOT_BUNDLE_SMOKE": "1",
        })
        worker_script = bundled_script_path("worker_identity.py")
        worker_args = (
            [sys.executable, "--run-script", str(worker_script)]
            if getattr(sys, "frozen", False)
            else [sys.executable, str(worker_script)]
        )
        process = subprocess.Popen(
            worker_args,
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        try:
            await asyncio.wait_for(heartbeat_received.wait(), timeout=15)
            exit_code = await asyncio.to_thread(process.wait, timeout=10)
            if exit_code != 20:
                raise ConnectorError(
                    "Packaged worker did not stop after the simulated credential revocation."
                )
            return True
        finally:
            await asyncio.to_thread(terminate_owned_process, process)


def smoke_test_bundled_worker():
    asyncio.run(_smoke_test_bundled_worker_async())
    return True


class ConnectorRunner:
    def __init__(self, credential_record, terminal_path, status_callback, popen=None):
        self.record = dict(credential_record)
        self.terminal_path = str(terminal_path)
        self.status_callback = status_callback
        self.popen = popen or subprocess.Popen
        self.processes = []
        self.threads = []
        self.stop_event = threading.Event()
        self.connected = False
        self.starting = False
        self._lock = threading.RLock()
        self.terminal_lock = None
        self._verified_identity = None
        self._verified_trade_allowed = None
        self._connected_reported = False

    @property
    def is_running(self):
        with self._lock:
            return any(process.poll() is None for _name, process in self.processes)

    def start(self):
        log_diagnostic(
            "runner_start_requested",
            self.record.get("connectorId"),
            account_id=self.record.get("accountId"),
            terminal_path=self.terminal_path,
        )
        with self._lock:
            if self.starting or any(process.poll() is None for _name, process in self.processes):
                self.status_callback("Connecting", "Connector components are already running.")
                return
            self.starting = True
            self.processes.clear()
            if platform.system() != "Windows":
                raise ConnectorError("The MT5 Connector can only start MT5 on Windows.")
            if not self.terminal_path or not Path(self.terminal_path).is_file():
                raise ConnectorError("MT5 terminal not found. Select the installed terminal64.exe.")
            self.stop_event.clear()
            self.connected = False
            self._verified_identity = None
            self._verified_trade_allowed = None
            self._connected_reported = False
            self.status_callback("Connecting", "Starting the local MT5 bridge and authenticating with Profit Pilot…")
        inherited_environment = (
            "PATH",
            "SYSTEMROOT",
            "WINDIR",
            "TEMP",
            "TMP",
            "USERPROFILE",
            "APPDATA",
            "LOCALAPPDATA",
            "PROGRAMFILES",
            "PROGRAMFILES(X86)",
            "COMMONPROGRAMFILES",
            "SSL_CERT_FILE",
            "REQUESTS_CA_BUNDLE",
        )
        env = {
            key: os.environ[key]
            for key in inherited_environment
            if key in os.environ
        }
        bridge_secret = secrets.token_urlsafe(32)
        bridge_port = get_free_loopback_port()
        for legacy_setting in (
            "MT5_LOGIN",
            "MT5_PASSWORD",
            "MT5_SERVER",
            "WORKER_IDENTITY_TOKEN",
            "WORKER_IDENTITY_SERVER_URL",
        ):
            env.pop(legacy_setting, None)
        env.update({
            "PORT": str(bridge_port),
            "MT5_BRIDGE_HOST": "127.0.0.1",
            "MT5_TERMINAL_PATH": self.terminal_path,
            "MT5_BRIDGE_URL": f"http://127.0.0.1:{bridge_port}",
            "MT5_AGENT_SECRET": bridge_secret,
            "WORKER_IDENTITY_SERVER_URL": self.record["serverUrl"],
            "WORKER_ID": f"profit-pilot-{self.record['connectorId'][:12]}",
            "WORKER_IDENTITY_TOKEN": self.record["credential"],
            "WORKER_RECONNECT_DELAY_SECONDS": "3",
            "REMOTE_WORKER_HEARTBEAT_INTERVAL_MS": "5000",
            "PROFIT_PILOT_CONNECTOR_MODE": "1",
            "PYTHON_DOTENV_DISABLED": "1",
            "PYTHONUNBUFFERED": "1",
        })
        try:
            self.terminal_lock = TerminalInstanceLock(self.terminal_path)
            self.terminal_lock.acquire()
            self._spawn(["probe.py"], env)
            if self.stop_event.is_set():
                raise OSError("The local MT5 bridge exited during startup")
            self._spawn(["worker_identity.py"], env)
        except (OSError, ConnectorError) as error:
            log_diagnostic(
                "runner_start_failed",
                self.record.get("connectorId"),
                account_id=self.record.get("accountId"),
                error=str(error),
            )
            self.stop()
            self.status_callback("Error", "Could not start the connector components. Try restarting the app.")
            if isinstance(error, ConnectorError):
                raise
            raise ConnectorError("Could not start the local MT5 connector components.") from None
        finally:
            with self._lock:
                self.starting = False

    def _spawn(self, script_names, env):
        script = bundled_script_path(script_names[0])
        if not script.is_file():
            raise OSError("Bundled connector component is missing")
        if getattr(sys, "frozen", False):
            args = [sys.executable, "--run-script", str(script)]
        else:
            args = [sys.executable, str(script)]
        process = self.popen(
            args,
            cwd=str(script.parent.parent),
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        self.processes.append((script_names[0], process))
        self._output_tail = getattr(self, "_output_tail", {})
        self._output_tail[script_names[0]] = []
        log_diagnostic(
            "worker_process_started" if script_names[0] == "worker_identity.py" else "bridge_process_started",
            self.record.get("connectorId"),
            account_id=self.record.get("accountId"),
            component=script_names[0],
            pid=getattr(process, "pid", None),
        )
        output_thread = threading.Thread(
            target=self._read_output,
            args=(script_names[0], process),
            daemon=True,
        )
        exit_thread = threading.Thread(
            target=self._watch_exit,
            args=(script_names[0], process),
            daemon=True,
        )
        self.threads.extend((output_thread, exit_thread))
        output_thread.start()
        exit_thread.start()
        if script_names[0] == "worker_identity.py":
            diagnostics_thread = threading.Thread(
                target=self._read_worker_diagnostics,
                args=(process,),
                daemon=True,
            )
            self.threads.append(diagnostics_thread)
            diagnostics_thread.start()

    def _read_worker_diagnostics(self, process):
        launcher_pid = int(getattr(process, "pid", os.getpid()))
        offsets = {}
        pending_by_pid = {}
        worker_stages = {
            "worker_identity_acknowledged",
            "worker_account_check_requested",
            "worker_account_verification_succeeded",
            "worker_account_verification_failed",
            "worker_account_result_emitted",
        }
        attached_pids = set()
        log_diagnostic(
            "worker_diagnostic_reader_started",
            self.record.get("connectorId"),
            worker_launcher_pid=launcher_pid,
        )
        while True:
            descendant_pids = owned_process_descendants(launcher_pid)
            candidate_pids = list(dict.fromkeys([*descendant_pids, launcher_pid]))
            for candidate_pid in candidate_pids:
                path = diagnostic_path(candidate_pid)
                try:
                    with path.open("rb") as stream:
                        stream.seek(offsets.get(candidate_pid, 0))
                        chunk = stream.read()
                        offsets[candidate_pid] = stream.tell()
                except FileNotFoundError:
                    continue
                except OSError as error:
                    log_diagnostic(
                        "worker_diagnostic_read_failed",
                        self.record.get("connectorId"),
                        worker_pid=candidate_pid,
                        error=str(error),
                    )
                    continue

                if not chunk:
                    continue
                pending = pending_by_pid.get(candidate_pid, b"") + chunk
                records, separator, trailing = pending.rpartition(b"\n")
                pending_by_pid[candidate_pid] = trailing if separator else pending
                if not separator:
                    continue
                for raw_record in records.splitlines():
                    try:
                        entry = json.loads(raw_record.decode("utf-8"))
                    except (UnicodeDecodeError, json.JSONDecodeError):
                        log_diagnostic(
                            "worker_diagnostic_record_invalid",
                            self.record.get("connectorId"),
                            worker_pid=candidate_pid,
                            byte_count=len(raw_record),
                        )
                        continue
                    if (
                        entry.get("pid") != candidate_pid
                        or entry.get("stage") not in worker_stages
                    ):
                        continue
                    if candidate_pid not in attached_pids:
                        attached_pids.add(candidate_pid)
                        log_diagnostic(
                            "worker_diagnostic_reader_attached",
                            self.record.get("connectorId"),
                            launcher_pid=launcher_pid,
                            worker_pid=candidate_pid,
                        )
                    self._handle_worker_diagnostic_event(entry)

            if process.poll() is not None:
                if not any(
                    diagnostic_path(pid).exists()
                    and offsets.get(pid, 0) < diagnostic_path(pid).stat().st_size
                    for pid in candidate_pids
                ):
                    break
                continue
            threading.Event().wait(0.1)

        for candidate_pid, pending in pending_by_pid.items():
            if not pending:
                continue
            try:
                entry = json.loads(pending.decode("utf-8"))
                if entry.get("pid") == candidate_pid and entry.get("stage") in worker_stages:
                    self._handle_worker_diagnostic_event(entry)
            except (UnicodeDecodeError, json.JSONDecodeError):
                log_diagnostic(
                    "worker_diagnostic_trailing_record_invalid",
                    self.record.get("connectorId"),
                    worker_pid=candidate_pid,
                    byte_count=len(pending),
                )
        log_diagnostic(
            "worker_diagnostic_reader_finished",
            self.record.get("connectorId"),
            worker_launcher_pid=launcher_pid,
            worker_pids=sorted(attached_pids),
        )

    def _handle_worker_diagnostic_event(self, entry):
        if not isinstance(entry, dict):
            return
        fields = entry.get("fields")
        if not isinstance(fields, dict):
            return
        stage = entry.get("stage")
        if stage == "worker_account_verification_succeeded":
            identity = (
                f"login ending {fields.get('login_suffix', '')}; "
                f"server={fields.get('server', '')}"
            )
            if not re.fullmatch(r"login ending \d{4}; server=.{1,128}", identity):
                log_diagnostic(
                    "worker_ipc_identity_invalid",
                    self.record.get("connectorId"),
                    worker_event=stage,
                )
                return
            log_diagnostic(
                "worker_ipc_identity_received",
                self.record.get("connectorId"),
                login_suffix=fields.get("login_suffix"),
                server=fields.get("server"),
            )
            self._report_verified_identity(identity)
            trade_allowed = fields.get("trade_allowed")
            self._verified_trade_allowed = trade_allowed if isinstance(trade_allowed, bool) else None
        elif stage == "worker_account_result_emitted":
            if fields.get("ok") is True:
                log_diagnostic(
                    "worker_ipc_account_result_received",
                    self.record.get("connectorId"),
                    ok=True,
                )
                self._report_connected()
            else:
                self._report_verification_failed("Worker reported an unsuccessful account verification.")
        elif stage == "worker_account_verification_failed":
            reason = fields.get("reason", "Account verification failed.")
            log_diagnostic(
                "worker_ipc_verification_failure_received",
                self.record.get("connectorId"),
                reason=reason,
            )
            self._report_verification_failed(str(reason))
        elif stage == "worker_identity_acknowledged":
            log_diagnostic(
                "worker_ipc_backend_authenticated",
                self.record.get("connectorId"),
                account_id=fields.get("account_id"),
            )
            self.status_callback(
                "_backend_connected",
                "Backend authenticated; MT5 account verification pending.",
            )

    def _report_verified_identity(self, identity):
        if identity == self._verified_identity:
            return
        self._verified_identity = identity
        self.status_callback("_account", identity)

    def _report_connected(self):
        if self._connected_reported:
            return
        self._connected_reported = True
        self.connected = True
        trade_allowed_label = (
            "Yes" if self._verified_trade_allowed is True
            else "No" if self._verified_trade_allowed is False
            else "Unknown"
        )
        self.status_callback(
            "Connected",
            "MT5 login and server match the paired account. "
            f"MT5 terminal reports trading allowed: {trade_allowed_label}. "
            "This does not guarantee every trade can execute.",
        )

    def _report_verification_failed(self, detail):
        self._connected_reported = False
        self.connected = False
        self.stop()
        self.status_callback("Error", detail)

    def _read_output(self, script_name, process):
        stream = process.stdout
        if stream is None:
            log_diagnostic("worker_output_stream_missing", self.record.get("connectorId"), component=script_name)
            return
        for line in stream:
            if self.stop_event.is_set():
                log_diagnostic("worker_output_reader_stopped", self.record.get("connectorId"), component=script_name)
                return
            raw_line = line.rstrip("\r\n")
            output_tail = getattr(self, "_output_tail", {}).setdefault(script_name, [])
            output_tail.append(raw_line)
            del output_tail[:-12]
            log_diagnostic(
                "worker_output_received",
                self.record.get("connectorId"),
                component=script_name,
                raw_output=raw_line,
            )
            self._handle_line(script_name, raw_line.strip())

    def _handle_line(self, script_name, line):
        message_type = "unmatched"
        valid = False
        if script_name == "worker_identity.py":
            if line.startswith("Identity accepted:"):
                message_type, valid = "identity_accepted", True
                self.status_callback(
                    "_backend_connected",
                    "Backend authenticated; MT5 account verification pending.",
                )
            elif line.startswith("Verified MT5 account:"):
                identity = line.partition(":")[2].strip()
                match = re.fullmatch(r"login ending\s+(\d{4});\s*server=(.{1,128})", identity)
                valid = bool(
                    match
                    and match.group(2).strip()
                    and not any(ord(character) < 32 for character in identity)
                )
                message_type = "account_identity_verified"
                log_diagnostic(
                    "worker_message_parsed",
                    self.record.get("connectorId"),
                    component=script_name,
                    message_type=message_type,
                    valid=valid,
                    login_suffix=match.group(1) if match else "invalid",
                    server=match.group(2).strip() if match else "invalid",
                )
                if valid:
                    self._report_verified_identity(identity)
                return
            elif line.startswith("MT5 trade allowed:"):
                value = line.partition(":")[2].strip().casefold()
                labels = {"yes": True, "no": False, "unknown": None}
                valid = value in labels
                message_type = "account_trade_permission"
                if valid:
                    self._verified_trade_allowed = labels[value]
            elif line.startswith("Read-only account result returned"):
                message_type, valid = "account_result_success", True
                self._report_connected()
            elif line.startswith("Read-only account request failed:"):
                message_type, valid = "account_result_failure", True
                lowered = line.casefold()
                if "login does not match" in lowered or "server does not match" in lowered:
                    detail = "The selected MT5 account does not match the account assigned by Profit Pilot. Check the selected terminal and account details."
                elif "not connected" in lowered or "unreachable" in lowered:
                    detail = "MT5 terminal is not connected. Open the selected terminal and sign in; the connector will retry."
                elif "http 503" in lowered:
                    detail = "The MT5 terminal or MetaTrader5 Python component is unavailable. Verify the terminal installation and restart the connector."
                else:
                    detail = "Could not verify the assigned MT5 account. Check the selected terminal and network connection."
                self._report_verification_failed(detail)
            elif "Worker connection ended" in line:
                message_type, valid = "worker_connection_ended", True
                self.connected = False
                lowered = line.casefold()
                if any(
                    marker in lowered
                    for marker in (
                        "accountconfig",
                        "identity",
                        "credential",
                        "revoked",
                        "rotated",
                        "already connected",
                    )
                ):
                    if "already connected" in lowered:
                        detail = "This account already has an active connector. Disconnect or revoke that device in Profit Pilot before connecting this one."
                    else:
                        detail = "Pairing was rejected or revoked. Generate a fresh code on the website and pair again."
                elif "login does not match" in lowered or "server does not match" in lowered:
                    detail = "The selected MT5 account does not match the account assigned by Profit Pilot. Check the selected terminal and account details."
                elif "not connected" in lowered or "unreachable" in lowered:
                    detail = "MT5 terminal is unavailable. Open the selected terminal and check its network connection. Retrying automatically."
                else:
                    detail = "Connection interrupted. The connector will retry automatically."
                self.status_callback("Disconnected", detail)
            elif "Connected MT5 login" in line or "Connected MT5 server" in line:
                message_type, valid = "legacy_account_mismatch", True
                self.connected = False
                self.status_callback("Error", "The selected terminal does not match the account assigned by Profit Pilot.")
        elif script_name == "probe.py" and "Running on" in line:
            message_type, valid = "bridge_ready", True
            if not self.connected:
                self.status_callback("Connecting", "Local MT5 bridge started. Authenticating connector…")
        log_diagnostic(
            "worker_message_parsed",
            self.record.get("connectorId"),
            component=script_name,
            message_type=message_type,
            valid=valid,
        )

    def _watch_exit(self, script_name, process):
        code = process.wait()
        log_diagnostic(
            "worker_process_exited" if script_name == "worker_identity.py" else "bridge_process_exited",
            self.record.get("connectorId"),
            account_id=self.record.get("accountId"),
            component=script_name,
            pid=getattr(process, "pid", None),
            exit_code=code,
            captured_output_tail=getattr(self, "_output_tail", {}).get(script_name, []),
            stderr="merged into stdout by connector subprocess configuration",
        )
        if self.stop_event.is_set():
            return
        if script_name == "worker_identity.py" and code in (20, 21, 22):
            self.stop()
            if code == 20:
                self.status_callback(
                    "Access revoked",
                    "Profit Pilot revoked this connector. Pair again to restore access; use Forget to remove the saved credential.",
                )
            elif code == 21:
                self.status_callback(
                    "Error",
                    "This connector credential was rotated on the website. Forget this credential and pair again.",
                )
            else:
                self.status_callback(
                    "Error",
                    "Profit Pilot rejected this credential or account assignment. Check device access on the website, then pair again.",
                )
            return
        if script_name == "probe.py":
            self.stop()
            self.status_callback("Error", "Local MT5 bridge stopped. Restart the connector and check the terminal path.")
        else:
            self.stop()
            self.status_callback("Error", "Worker stopped unexpectedly. Restart the connector and check the network connection.")

    def stop(self):
        log_diagnostic(
            "runner_stop_started",
            self.record.get("connectorId"),
            process_count=len(self.processes),
            account_id=self.record.get("accountId"),
        )
        with self._lock:
            self.stop_event.set()
            self.connected = False
            processes, self.processes = self.processes, []
            threads, self.threads = self.threads, []
        for _name, process in processes:
            try:
                log_diagnostic(
                    "owned_process_termination_started",
                    self.record.get("connectorId"),
                    component=_name,
                    pid=getattr(process, "pid", None),
                )
                terminate_owned_process(process)
                log_diagnostic(
                    "owned_process_termination_completed",
                    self.record.get("connectorId"),
                    component=_name,
                    pid=getattr(process, "pid", None),
                    exit_code=process.poll(),
                )
            except OSError:
                log_diagnostic(
                    "owned_process_termination_failed",
                    self.record.get("connectorId"),
                    component=_name,
                    pid=getattr(process, "pid", None),
                )
                pass
            stream = process.stdout
            if stream is not None and threading.current_thread() not in threads:
                try:
                    stream.close()
                except OSError:
                    pass
        for thread in threads:
            if thread is not threading.current_thread():
                thread.join(timeout=1)
        if self.terminal_lock:
            self.terminal_lock.release()
            self.terminal_lock = None
        log_diagnostic("runner_stop_completed", self.record.get("connectorId"))


class ConnectorSession:
    def __init__(self, record):
        self.record = dict(record)
        self.runner = None
        self.status = "Disconnected"
        self.detail = "Paired on this PC. Start this account's connector to connect."
        self.backend_state = "Disconnected"
        self.mt5_state = "Not verified"
        self.identity = ""
        self.last_update = None
        self.generation = 0


class ConnectorController:
    def __init__(self, credential_store, runner_factory=ConnectorRunner, exchange=exchange_pairing_code):
        self.credential_store = credential_store
        self.runner_factory = runner_factory
        self.exchange = exchange
        loader = getattr(self.credential_store, "load_all", None)
        records = loader() if loader else []
        if not records:
            legacy = self.credential_store.load()
            records = [legacy] if legacy else []
        self.sessions = {
            record["connectorId"]: ConnectorSession(record)
            for record in records
            if isinstance(record, dict) and record.get("connectorId")
        }
        self._lock = threading.RLock()

    @property
    def paired(self):
        return bool(self.sessions)

    @property
    def record(self):
        return next(iter(self.sessions.values())).record if len(self.sessions) == 1 else None

    @property
    def running(self):
        return any(session.runner and session.runner.is_running for session in self.sessions.values())

    def _persist(self, records=None):
        if records is None:
            records = [session.record for session in self.sessions.values()]
        saver = getattr(self.credential_store, "save_all", None)
        if saver:
            saver(records)
            return
        if len(records) > 1:
            raise ConnectorError("This credential store cannot safely persist multiple account pairings.")
        if records:
            self.credential_store.save(records[0])
        else:
            self.credential_store.delete()

    def _session_callback(self, connector_id, generation, callback):
        def report(state, detail):
            with self._lock:
                session = self.sessions.get(connector_id)
                if not session or session.generation != generation:
                    log_diagnostic("session_callback_discarded", connector_id, state=state, generation=generation)
                    return
                before = {
                    "status": session.status,
                    "backend_state": session.backend_state,
                    "mt5_state": session.mt5_state,
                    "identity_present": bool(session.identity),
                    "verified_at_present": bool(session.record.get("lastVerifiedAt")),
                }
                if state == "_account":
                    session.identity = detail
                    session.mt5_state = "Verified"
                    session.record["identitySummary"] = detail
                    session.record["lastVerifiedAt"] = time.time()
                    try:
                        self._persist()
                    except (OSError, ValueError):
                        pass
                elif state == "_backend_connected":
                    session.backend_state = "Connected"
                    session.mt5_state = "Verifying"
                    session.status = "Connecting"
                    session.detail = detail
                    session.last_update = time.time()
                elif state == "Connected":
                    session.backend_state = "Connected"
                    session.mt5_state = "Verified"
                    session.status = "Connected"
                    session.detail = detail
                    session.last_update = time.time()
                else:
                    session.status = state
                    session.detail = detail
                    session.last_update = time.time()
                    if state == "Connecting":
                        if session.backend_state != "Connected":
                            session.backend_state = "Connecting"
                        if session.mt5_state != "Verified":
                            session.mt5_state = "Verifying"
                    elif state == "Disconnected":
                        session.backend_state = "Disconnected"
                        session.mt5_state = "Not verified"
                    elif state == "Access revoked":
                        session.backend_state = "Access revoked"
                        session.mt5_state = "Not verified"
                    elif state == "Error":
                        session.backend_state = "Disconnected"
                        session.mt5_state = "Error"
                after = {
                    "status": session.status,
                    "backend_state": session.backend_state,
                    "mt5_state": session.mt5_state,
                    "identity_present": bool(session.identity),
                    "verified_at_present": bool(session.record.get("lastVerifiedAt")),
                }
                log_diagnostic(
                    "session_state_updated",
                    connector_id,
                    state=state,
                    account_id=session.record.get("accountId"),
                    before=before,
                    after=after,
                )
            callback(state, detail)

        return report

    def pair(self, code, terminal_path, api_base_url, status_callback, display_name=""):
        record = self.exchange(code, api_base_url)
        record["serverUrl"] = worker_websocket_url(api_base_url)
        record["terminalPath"] = str(terminal_path)
        record["displayName"] = str(display_name or "MT5 account").strip()[:64]
        with self._lock:
            if record["connectorId"] in self.sessions:
                raise ConnectorError("Profit Pilot returned an already-saved connector ID.")
            session = ConnectorSession(record)
            self.sessions[record["connectorId"]] = session
            session.generation += 1
            self._persist()
        try:
            self.start(
                record["connectorId"],
                lambda state, detail: status_callback(record["connectorId"], state, detail),
                terminal_path,
            )
        except ConnectorError as error:
            with self._lock:
                session.status = "Error"
                session.detail = str(error)
            status_callback(record["connectorId"], "Error", str(error))
        return record["connectorId"]

    def start(self, connector_id, status_callback, terminal_path=None):
        with self._lock:
            session = self.sessions.get(connector_id)
            if not session:
                raise ConnectorError("This account pairing is no longer saved.")
            if session.status == "Access revoked":
                raise ConnectorError("Access was revoked. Forget this pairing and pair the account again.")
            if session.runner and session.runner.is_running:
                status_callback("Connecting", "This account's connector is already running.")
                return
            selected_terminal = str(terminal_path or session.record.get("terminalPath") or "")
            if not selected_terminal or not Path(selected_terminal).is_file():
                message = "MT5 terminal not found. Select the installed terminal64.exe."
                session.status, session.detail = "Error", message
                status_callback("Error", message)
                raise ConnectorError(message)
            terminal_key = os.path.normcase(os.path.realpath(selected_terminal))
            for other_id, other in self.sessions.items():
                if other_id == connector_id or not other.runner:
                    continue
                active = other.runner.is_running or other.status in {"Starting", "Connecting", "Connected"}
                if active and os.path.normcase(os.path.realpath(other.runner.terminal_path)) == terminal_key:
                    message = "Another account is using this MT5 installation. Stop it or select a separate MT5 installation/data folder for concurrent connections."
                    session.status, session.detail = "Error", message
                    status_callback("Error", message)
                    raise ConnectorError(message)
            for other_id, other in self.sessions.items():
                active = other.runner and (
                    other.runner.is_running or other.status in {"Starting", "Connecting", "Connected"}
                )
                if other_id != connector_id and active and other.record["accountId"] == session.record["accountId"]:
                    message = "This Profit Pilot account already has a running connector on this PC."
                    session.status, session.detail = "Error", message
                    status_callback("Error", message)
                    raise ConnectorError(message)
            session.record["terminalPath"] = selected_terminal
            session.generation += 1
            callback = self._session_callback(connector_id, session.generation, status_callback)
            session.status = "Starting"
            session.detail = "Starting this account's bridge and worker…"
            session.backend_state = "Connecting"
            session.mt5_state = "Not verified"
            session.runner = self.runner_factory(session.record, selected_terminal, callback)
            self._persist()
            runner = session.runner
        try:
            runner.start()
        except Exception as error:
            with self._lock:
                session.status = "Error"
                session.detail = str(error)
                log_diagnostic(
                    "session_start_failed",
                    connector_id,
                    account_id=session.record.get("accountId"),
                    status=session.status,
                    error=str(error),
                )
            raise

    def stop(self, connector_id, status_callback=None):
        with self._lock:
            session = self.sessions.get(connector_id)
            if not session:
                return
            session.generation += 1
            callback = self._session_callback(connector_id, session.generation, status_callback) if status_callback else None
            runner, session.runner = session.runner, None
            session.status = "Stopping"
            session.detail = "Stopping this account's worker and bridge…"
            session.backend_state = "Stopping"
            if runner:
                runner.stop()
            if callback:
                callback("Disconnected", "This connector stopped. Its pairing remains saved on this PC.")
            else:
                session.status = "Disconnected"
                session.detail = "This connector stopped. Its pairing remains saved on this PC."
                session.backend_state = "Disconnected"
                session.mt5_state = "Not verified"

    def disconnect(self, connector_id):
        with self._lock:
            session = self.sessions.get(connector_id)
            if not session:
                return
            log_diagnostic(
                "forget_cleanup_started",
                connector_id,
                account_id=session.record.get("accountId"),
                runner_present=bool(session.runner),
            )
            session.generation += 1
            runner, session.runner = session.runner, None
            try:
                if runner:
                    log_diagnostic("forget_runner_stop_started", connector_id)
                    runner.stop()
                    log_diagnostic("forget_runner_stop_completed", connector_id)
                remaining_records = [
                    other.record
                    for other_id, other in self.sessions.items()
                    if other_id != connector_id
                ]
                self._persist(remaining_records)
                del self.sessions[connector_id]
                log_diagnostic("forget_local_credential_removed", connector_id)
            except Exception as error:
                session.status = "Error"
                session.detail = "Could not remove this local credential. Retry Disconnect & Forget."
                log_diagnostic(
                    "forget_cleanup_failed",
                    connector_id,
                    account_id=session.record.get("accountId"),
                    error=str(error),
                )
                raise

    def status_snapshot(self):
        with self._lock:
            return [
                {
                    "connectorId": connector_id,
                    "record": dict(session.record),
                    "status": session.status,
                    "detail": session.detail,
                    "backendState": session.backend_state,
                    "mt5State": session.mt5_state,
                    "identity": session.identity,
                    "lastUpdate": session.last_update,
                    "running": bool(session.runner and session.runner.is_running),
                }
                for connector_id, session in self.sessions.items()
            ]

    def stop_all(self):
        for connector_id in list(self.sessions):
            self.stop(connector_id)


class TkStatusApp:
    def __init__(self, root, controller, api_base_url=DEFAULT_API_BASE_URL):
        import tkinter as tk
        from tkinter import filedialog, messagebox, ttk

        self.tk = tk
        self.ttk = ttk
        self.filedialog = filedialog
        self.messagebox = messagebox
        self.root = root
        self.controller = controller
        self.api_base_url = api_base_url
        self.status_queue = __import__("queue").Queue()
        self._operation_id = 0
        self._pairing_busy = False
        self._busy_sessions = set()
        self._forgetting_sessions = set()
        self.root.title("Profit Pilot Connector")
        self.root.geometry("760x720")
        self.root.minsize(620, 520)
        self.terminal_options = discover_terminals()

        main = ttk.Frame(root, padding=20)
        main.pack(fill="both", expand=True)
        ttk.Label(main, text="Profit Pilot Connector", font=("Segoe UI", 18, "bold")).pack(anchor="w")
        ttk.Label(
            main,
            text="Pair and manage each MT5 account independently.",
            wraplength=450,
        ).pack(anchor="w", pady=(4, 16))

        terminal_frame = ttk.LabelFrame(main, text="MT5 terminal", padding=10)
        terminal_frame.pack(fill="x", pady=(0, 12))
        self.terminal_var = tk.StringVar(value=(controller.record or {}).get("terminalPath", ""))
        self.terminal_box = ttk.Combobox(terminal_frame, textvariable=self.terminal_var, values=self.terminal_options)
        self.terminal_box.pack(side="left", fill="x", expand=True)
        ttk.Button(terminal_frame, text="Browse…", command=self._browse).pack(side="left", padx=(8, 0))

        ttk.Label(main, text="Account label").pack(anchor="w")
        self.label_var = tk.StringVar()
        self.label_entry = ttk.Entry(main, textvariable=self.label_var)
        self.label_entry.pack(fill="x", pady=(4, 8))
        ttk.Label(main, text="Temporary pairing code").pack(anchor="w")
        self.code_var = tk.StringVar()
        self.code_entry = ttk.Entry(main, textvariable=self.code_var, show="•")
        self.code_entry.pack(fill="x", pady=(4, 10))
        self.code_entry.bind("<Return>", lambda _event: self._pair())

        self.action_frame = ttk.Frame(main)
        self.action_frame.pack(fill="x")
        self.pair_button = ttk.Button(self.action_frame, text="Pair & Connect", command=self._pair)
        self.pair_button.pack(side="left")
        ttk.Separator(main).pack(fill="x", pady=14)
        self.accounts_frame = ttk.LabelFrame(main, text="Paired accounts", padding=8)
        self.accounts_frame.pack(fill="both", expand=True)
        self.accounts_canvas = tk.Canvas(self.accounts_frame, highlightthickness=0)
        self.accounts_scrollbar = ttk.Scrollbar(
            self.accounts_frame, orient="vertical", command=self.accounts_canvas.yview
        )
        self.accounts_canvas.configure(yscrollcommand=self.accounts_scrollbar.set)
        self.accounts_scrollbar.pack(side="right", fill="y")
        self.accounts_canvas.pack(side="left", fill="both", expand=True)
        self.accounts_body = ttk.Frame(self.accounts_canvas)
        self.accounts_window = self.accounts_canvas.create_window(
            (0, 0), window=self.accounts_body, anchor="nw"
        )
        self.accounts_body.bind(
            "<Configure>",
            lambda _event: self.accounts_canvas.configure(
                scrollregion=self.accounts_canvas.bbox("all")
            ),
        )
        self.accounts_canvas.bind(
            "<Configure>",
            lambda event: self.accounts_canvas.itemconfigure(
                self.accounts_window, width=event.width
            ),
        )
        self._rendered_rows = {}
        self.pair_status_var = tk.StringVar(value="")
        ttk.Label(main, textvariable=self.pair_status_var, wraplength=700).pack(anchor="w", pady=(6, 0))

        ttk.Label(
            main,
            text="Each account has its own worker and bridge. For simultaneous connections, use separate MT5 installations in different directories; the same terminal path cannot serve two active logins safely.",
            wraplength=700,
            foreground="#555555",
        ).pack(anchor="w", pady=(16, 0))
        self.root.protocol("WM_DELETE_WINDOW", self._close)
        self._render_sessions()
        self.root.after(200, self._drain_status)

    def _browse(self):
        selected = self.filedialog.askopenfilename(
            title="Select MetaTrader 5 terminal",
            filetypes=[("MetaTrader terminal", "terminal64.exe"), ("Executable", "*.exe")],
        )
        if selected:
            self.terminal_var.set(selected)

    def _pair(self):
        if self._pairing_busy:
            return
        code = self.code_var.get()
        terminal = self.terminal_var.get().strip()
        if not terminal or not Path(terminal).is_file():
            self.pair_status_var.set("Select the installed MT5 terminal executable before pairing.")
            return
        self._operation_id += 1
        operation_id = self._operation_id
        self._pairing_busy = True
        self.pair_button.configure(state="disabled", text="Pairing…")
        self.pair_status_var.set("Exchanging the temporary code and starting this account…")
        threading.Thread(
            target=self._pair_task,
            args=(operation_id, code, terminal, self.label_var.get().strip()),
            daemon=True,
        ).start()

    def _pair_task(self, operation_id, code, terminal, display_name):
        try:
            connector_id = self.controller.pair(
                code,
                terminal,
                self.api_base_url,
                lambda key, state, detail: self._queue_session_status(key, state, detail),
                display_name,
            )
            self.status_queue.put(("pair_done", operation_id, connector_id, ""))
        except Exception as error:
            log_diagnostic("pairing_failed", fields_error=str(error))
            self.status_queue.put(("pair_error", operation_id, "Error", str(error)))
        finally:
            self.status_queue.put(("pair_finished", operation_id, None, None))

    def _toggle_session(self, connector_id, terminal_var=None):
        if connector_id in self._busy_sessions:
            return
        snapshot = next(
            (item for item in self.controller.status_snapshot() if item["connectorId"] == connector_id),
            None,
        )
        if not snapshot or snapshot["status"] == "Access revoked":
            return
        self._busy_sessions.add(connector_id)
        self._render_sessions()
        if snapshot["running"]:
            threading.Thread(target=self._stop_task, args=(connector_id,), daemon=True).start()
        else:
            terminal_path = terminal_var.get().strip() if terminal_var else snapshot["record"].get("terminalPath", "")
            threading.Thread(target=self._start_task, args=(connector_id, terminal_path), daemon=True).start()

    def _start_task(self, connector_id, terminal_path):
        try:
            self.controller.start(connector_id, lambda state, detail: self._queue_session_status(connector_id, state, detail), terminal_path)
        except ConnectorError as error:
            self.status_queue.put(("session", connector_id, "Error", str(error)))
        finally:
            self.status_queue.put(("session_done", connector_id, None, None))

    def _stop_task(self, connector_id):
        try:
            self.controller.stop(connector_id, lambda state, detail: self._queue_session_status(connector_id, state, detail))
        except Exception:
            self.status_queue.put(("session", connector_id, "Error", "Could not fully stop this connector. MT5 itself was not asked to log out."))
        finally:
            self.status_queue.put(("session_done", connector_id, None, None))

    def _forget_session(self, connector_id):
        snapshot = next(
            (item for item in self.controller.status_snapshot() if item["connectorId"] == connector_id),
            None,
        )
        if not snapshot or connector_id in self._busy_sessions:
            return
        log_diagnostic("forget_confirmation_requested", connector_id)
        record = snapshot["record"]
        account_name = record.get("displayName") or "MT5 account"
        identity_summary = snapshot["identity"] or record.get("identitySummary", "")
        login_suffix = identity_summary.split(";", 1)[0].replace("login ending", "").strip()
        target = f"{account_name} (login ending {login_suffix})" if login_suffix else account_name
        confirmed = self.messagebox.askyesno(
            "Disconnect & Forget",
            f"Disconnect and forget {target}?\n\nThis stops only this account's connector session, worker, and bridge, then removes its saved credential from this PC. The MT5 terminal will not be logged out and open positions will not be closed. The website credential may remain valid unless you separately revoke this device on the Profit Pilot dashboard.",
        )
        if not confirmed:
            log_diagnostic("forget_confirmation_cancelled", connector_id)
            return
        log_diagnostic("forget_confirmation_accepted", connector_id)
        self._busy_sessions.add(connector_id)
        self._forgetting_sessions.add(connector_id)
        self._render_sessions()
        threading.Thread(target=self._forget_task, args=(connector_id,), daemon=True).start()

    def _forget_task(self, connector_id):
        log_diagnostic("forget_task_started", connector_id)
        try:
            self.controller.disconnect(connector_id)
            log_diagnostic("forget_task_succeeded", connector_id)
            self.status_queue.put(("forgotten", connector_id, None, None))
        except Exception as error:
            log_diagnostic("forget_task_failed", connector_id, error=str(error))
            self.status_queue.put(("forget_failed", connector_id, None, None))
        finally:
            self.status_queue.put(("forget_finished", connector_id, None, None))

    def _close(self):
        self.controller.stop_all()
        self.root.destroy()

    def _queue_session_status(self, connector_id, state, detail):
        log_diagnostic("ui_status_queued", connector_id, state=state, detail=detail)
        self.status_queue.put(("session", connector_id, state, detail))

    def _render_sessions(self):
        snapshots = self.controller.status_snapshot()
        active_ids = {item["connectorId"] for item in snapshots}
        for connector_id in set(self._rendered_rows) - active_ids:
            self._rendered_rows.pop(connector_id)["frame"].destroy()
        for item in snapshots:
            connector_id = item["connectorId"]
            widgets = self._rendered_rows.get(connector_id)
            if widgets is None:
                frame = self.ttk.LabelFrame(self.accounts_body, padding=8)
                frame.pack(fill="x", pady=(0, 8))
                title = self.ttk.Label(frame, font=("Segoe UI", 10, "bold"))
                title.pack(anchor="w")
                identity = self.ttk.Label(frame)
                identity.pack(anchor="w", pady=(2, 0))
                terminal_var = self.tk.StringVar(value="")
                terminal_box = self.ttk.Combobox(
                    frame,
                    textvariable=terminal_var,
                    values=self.terminal_options,
                    width=70,
                )
                terminal_box.pack(fill="x", pady=(4, 2))
                terminal = self.ttk.Label(frame)
                terminal.pack(anchor="w", pady=(2, 0))
                backend_state = self.ttk.Label(frame, font=("Segoe UI", 10, "bold"))
                backend_state.pack(anchor="w", pady=(6, 0))
                mt5_state = self.ttk.Label(frame, font=("Segoe UI", 10, "bold"))
                mt5_state.pack(anchor="w", pady=(2, 0))
                detail = self.ttk.Label(frame, wraplength=650)
                detail.pack(anchor="w", pady=(2, 6))
                controls = self.ttk.Frame(frame)
                controls.pack(fill="x")
                action = self.ttk.Button(controls)
                action.pack(side="left")
                forget = self.ttk.Button(controls, text="Disconnect & Forget")
                forget.pack(side="right")
                widgets = self._rendered_rows[connector_id] = {
                    "frame": frame, "title": title, "identity": identity,
                    "terminal_var": terminal_var, "terminal_box": terminal_box,
                    "terminal": terminal, "backend_state": backend_state,
                    "mt5_state": mt5_state, "detail": detail, "action": action,
                    "forget": forget,
                }
            record = item["record"]
            widgets["title"].configure(text=record.get("displayName") or "MT5 account")
            identity_summary = item["identity"] or record.get("identitySummary", "")
            widgets["identity"].configure(
                text=identity_summary or f"Account ID {record['accountId']} · account identity not yet verified"
            )
            if not widgets["terminal_var"].get() or not self._busy_sessions.__contains__(connector_id):
                widgets["terminal_var"].set(record.get("terminalPath", ""))
            widgets["terminal"].configure(text=f"Terminal: {record.get('terminalPath', '')}")
            widgets["backend_state"].configure(
                text=f"Backend: {item['backendState']}"
            )
            widgets["mt5_state"].configure(
                text=f"MT5 account: {item['mt5State']}"
            )
            verified_at = record.get("lastVerifiedAt")
            verified_text = (
                "Last verified " + time.strftime("%Y-%m-%d %H:%M", time.localtime(verified_at))
                if verified_at else "Not yet verified"
            )
            widgets["detail"].configure(text=f"{item['detail']} · {verified_text}")
            log_diagnostic(
                "account_row_rendered",
                connector_id,
                status=item["status"],
                backend_state=item["backendState"],
                mt5_state=item["mt5State"],
                identity_present=bool(identity_summary),
                verified=bool(verified_at),
                running=item["running"],
            )
            busy = connector_id in self._busy_sessions
            revoked = item["status"] == "Access revoked"
            running = item["running"]
            widgets["action"].configure(
                text="Stopping…" if item["status"] == "Stopping" else "Starting…" if item["status"] == "Starting" else "Stop / Disconnect" if running else "Connect",
                state="disabled" if busy or revoked else "normal",
                command=lambda key=connector_id, value=widgets["terminal_var"]: self._toggle_session(key, value),
            )
            widgets["terminal_box"].configure(state="disabled" if busy or running else "normal")
            widgets["forget"].configure(
                text="Forgetting…" if connector_id in self._forgetting_sessions else "Disconnect & Forget",
                state="disabled" if busy else "normal",
                command=lambda key=connector_id: self._forget_session(key),
            )
        if not snapshots:
            if not getattr(self, "empty_label", None):
                self.empty_label = self.ttk.Label(
                    self.accounts_body, text="No accounts paired on this PC."
                )
                self.empty_label.pack(anchor="w", padx=4, pady=8)
        elif getattr(self, "empty_label", None):
            self.empty_label.destroy()
            self.empty_label = None

    def _drain_status(self):
        changed = False
        while True:
            try:
                event, key, state, detail = self.status_queue.get_nowait()
            except __import__("queue").Empty:
                break
            if event == "session":
                log_diagnostic("ui_status_received", key, state=state, detail=detail)
                changed = True
            elif event == "session_done":
                self._busy_sessions.discard(key)
                changed = True
            elif event == "forgotten":
                self._busy_sessions.discard(key)
                self._forgetting_sessions.discard(key)
                changed = True
            elif event == "forget_failed":
                changed = True
            elif event == "forget_finished":
                self._busy_sessions.discard(key)
                self._forgetting_sessions.discard(key)
                changed = True
            elif event == "pair_error" and key == self._operation_id:
                self.pair_status_var.set(detail)
            elif event == "pair_done" and key == self._operation_id:
                self.code_var.set("")
                self.label_var.set("")
                self.pair_status_var.set("Account pairing saved. Its connection state is shown below.")
                changed = True
            elif event == "pair_finished" and key == self._operation_id:
                self._pairing_busy = False
                self.pair_button.configure(state="normal", text="Pair & Connect")
        if changed:
            self._render_sessions()
        self.root.after(200, self._drain_status)


def main():
    if len(sys.argv) == 2 and sys.argv[1] == "--smoke-test-bundle":
        smoke_test_bundled_mt5_import()
        smoke_test_bundled_bridge()
        smoke_test_bundled_worker()
        return 0

    if len(sys.argv) == 2 and sys.argv[1] == "--smoke-test-ui":
        import tkinter as tk

        class MemoryStore:
            def load(self):
                return None

            def save(self, _record):
                raise AssertionError("UI smoke test must not save credentials")

            def delete(self):
                return None

        root = tk.Tk()
        TkStatusApp(root, ConnectorController(MemoryStore()))
        root.update_idletasks()
        root.update()
        window_visible = bool(root.winfo_exists() and root.winfo_viewable())
        root.destroy()
        return 0 if window_visible else 2

    if len(sys.argv) >= 3 and sys.argv[1] == "--run-script":
        script = sys.argv[2]
        if Path(script).name not in {"probe.py", "worker_identity.py"}:
            raise SystemExit("Unsupported connector child process")
        sys.argv = [script]
        try:
            runpy.run_path(script, run_name="__main__")
        except Exception as error:
            if os.environ.get("PROFIT_PILOT_BUNDLE_SMOKE") == "1":
                print(
                    f"Frozen connector child failed ({type(error).__name__}: {error}).",
                    file=sys.stderr,
                    flush=True,
                )
                raise SystemExit(1) from None
            raise
        return

    import tkinter as tk
    from .secure_store import WindowsCredentialStore

    api_base_url = os.environ.get("PROFIT_PILOT_API_URL", DEFAULT_API_BASE_URL)
    store = WindowsCredentialStore()
    controller = ConnectorController(store)
    root = tk.Tk()
    TkStatusApp(root, controller, api_base_url)
    root.mainloop()
