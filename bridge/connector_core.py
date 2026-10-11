import asyncio
import json
import ipaddress
import os
import platform
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

    @property
    def is_running(self):
        with self._lock:
            return any(process.poll() is None for _name, process in self.processes)

    def start(self):
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
        })
        try:
            self._spawn(["probe.py"], env)
            if self.stop_event.is_set():
                raise OSError("The local MT5 bridge exited during startup")
            self._spawn(["worker_identity.py"], env)
        except OSError:
            self.stop()
            self.status_callback("Error", "Could not start the connector components. Try restarting the app.")
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

    def _read_output(self, script_name, process):
        stream = process.stdout
        if stream is None:
            return
        for line in stream:
            if self.stop_event.is_set():
                return
            self._handle_line(script_name, line.strip())

    def _handle_line(self, script_name, line):
        if script_name == "worker_identity.py":
            if line.startswith("Identity accepted:"):
                self.status_callback("Connecting", "Authenticated. Verifying the assigned MT5 account…")
            elif line.startswith("Read-only account result returned"):
                self.connected = True
                self.status_callback("Connected", "MT5 terminal and assigned account verified.")
            elif line.startswith("Read-only account request failed:"):
                lowered = line.casefold()
                if "login does not match" in lowered or "server does not match" in lowered:
                    detail = "The selected MT5 account does not match the account assigned by Profit Pilot. Check the selected terminal and account details."
                elif "not connected" in lowered or "unreachable" in lowered:
                    detail = "MT5 terminal is not connected. Open the selected terminal and sign in; the connector will retry."
                elif "http 503" in lowered:
                    detail = "The MT5 terminal or MetaTrader5 Python component is unavailable. Verify the terminal installation and restart the connector."
                else:
                    detail = "Could not verify the assigned MT5 account. Check the selected terminal and network connection."
                self.connected = False
                self.stop()
                self.status_callback("Error", detail)
            elif "Worker connection ended" in line:
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
                self.connected = False
                self.status_callback("Error", "The selected terminal does not match the account assigned by Profit Pilot.")
        elif script_name == "probe.py" and "Running on" in line:
            if not self.connected:
                self.status_callback("Connecting", "Local MT5 bridge started. Authenticating connector…")

    def _watch_exit(self, script_name, process):
        code = process.wait()
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
        with self._lock:
            self.stop_event.set()
            self.connected = False
            processes, self.processes = self.processes, []
            threads, self.threads = self.threads, []
        for _name, process in processes:
            try:
                terminate_owned_process(process)
            except OSError:
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


class ConnectorController:
    def __init__(self, credential_store, runner_factory=ConnectorRunner, exchange=exchange_pairing_code):
        self.credential_store = credential_store
        self.runner_factory = runner_factory
        self.exchange = exchange
        self.runner = None
        self.record = self.credential_store.load()
        self._lock = threading.RLock()
        self._generation = 0

    @property
    def paired(self):
        return bool(self.record)

    @property
    def running(self):
        return bool(self.runner and self.runner.is_running)

    def _new_status_callback(self, callback):
        self._generation += 1
        generation = self._generation

        def report(state, detail):
            with self._lock:
                if generation != self._generation:
                    return
            callback(state, detail)

        return report

    def pair(self, code, terminal_path, api_base_url, status_callback):
        with self._lock:
            if self.record:
                raise ConnectorError(
                    "This connector supports one MT5 account at a time. Disconnect and forget this pairing before pairing another account."
                )
            callback = self._new_status_callback(status_callback)
            record = self.exchange(code, api_base_url)
            record["serverUrl"] = worker_websocket_url(api_base_url)
            record["terminalPath"] = str(terminal_path)
            self.credential_store.save(record)
            self.record = record
            self.runner = self.runner_factory(record, terminal_path, callback)
            self.runner.start()

    def start(self, terminal_path, status_callback):
        with self._lock:
            if not self.record:
                raise ConnectorError("Enter a pairing code from your Profit Pilot MT5 account first.")
            if self.runner and self.runner.is_running:
                status_callback("Connecting", "Connector is already running for the paired account.")
                return
            if self.runner:
                self.runner.stop()
            selected_terminal = str(terminal_path or self.record.get("terminalPath") or "")
            if not selected_terminal or not Path(selected_terminal).is_file():
                raise ConnectorError("MT5 terminal not found. Select the installed terminal64.exe.")
            self.record["terminalPath"] = selected_terminal
            self.credential_store.save(self.record)
            callback = self._new_status_callback(status_callback)
            self.runner = self.runner_factory(self.record, selected_terminal, callback)
            self.runner.start()

    def stop(self, status_callback=None):
        with self._lock:
            self._generation += 1
            callback = self._new_status_callback(status_callback) if status_callback else None
            runner, self.runner = self.runner, None
            if runner:
                runner.stop()
            if callback:
                callback("Disconnected", "Connector stopped. The paired credential remains saved on this PC.")

    def disconnect(self):
        with self._lock:
            self._generation += 1
            runner, self.runner = self.runner, None
            try:
                if runner:
                    runner.stop()
            finally:
                self.record = None
                self.credential_store.delete()


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
        self._busy = False
        self.root.title("Profit Pilot Connector")
        self.root.geometry("500x430")
        self.root.minsize(460, 390)

        main = ttk.Frame(root, padding=20)
        main.pack(fill="both", expand=True)
        ttk.Label(main, text="Profit Pilot Connector", font=("Segoe UI", 18, "bold")).pack(anchor="w")
        ttk.Label(
            main,
            text="Pair this computer with your selected MT5 account.",
            wraplength=450,
        ).pack(anchor="w", pady=(4, 16))

        terminal_frame = ttk.LabelFrame(main, text="MT5 terminal", padding=10)
        terminal_frame.pack(fill="x", pady=(0, 12))
        self.terminal_var = tk.StringVar(value=(controller.record or {}).get("terminalPath", ""))
        self.terminal_box = ttk.Combobox(terminal_frame, textvariable=self.terminal_var, values=discover_terminals())
        self.terminal_box.pack(side="left", fill="x", expand=True)
        ttk.Button(terminal_frame, text="Browse…", command=self._browse).pack(side="left", padx=(8, 0))

        ttk.Label(main, text="Temporary pairing code").pack(anchor="w")
        self.code_var = tk.StringVar()
        self.code_entry = ttk.Entry(main, textvariable=self.code_var, show="•")
        self.code_entry.pack(fill="x", pady=(4, 10))
        self.code_entry.bind("<Return>", lambda _event: self._pair())

        self.action_frame = ttk.Frame(main)
        self.action_frame.pack(fill="x")
        self.pair_button = ttk.Button(self.action_frame, text="Pair & Connect", command=self._pair)
        self.pair_button.pack(side="left")
        self.control_button = ttk.Button(self.action_frame, text="Start Connector", command=self._toggle_connector)
        self.control_button.pack(side="left", padx=(8, 0))
        self.forget_button = ttk.Button(self.action_frame, text="Disconnect & Forget", command=self._disconnect)
        self.forget_button.pack(side="right")

        ttk.Separator(main).pack(fill="x", pady=16)
        ttk.Label(main, text="Connection status", font=("Segoe UI", 10, "bold")).pack(anchor="w")
        self.status_var = tk.StringVar(value="Disconnected" if controller.paired else "Not paired")
        self.detail_var = tk.StringVar(
            value=(
                "This computer is paired but not running. Select the intended MT5 terminal and start the connector."
                if controller.paired
                else "Enter the temporary code shown on your Profit Pilot MT5 account."
            )
        )
        ttk.Label(main, textvariable=self.status_var, font=("Segoe UI", 12, "bold")).pack(anchor="w", pady=(4, 0))
        ttk.Label(main, textvariable=self.detail_var, wraplength=450).pack(anchor="w", pady=(3, 0))

        ttk.Label(
            main,
            text="One connector instance pairs with one MT5 account. To switch accounts, Disconnect & Forget here, then pair the other account. Forget removes this PC's credential; revoke server access on the Profit Pilot website.",
            wraplength=450,
            foreground="#555555",
        ).pack(anchor="w", pady=(16, 0))
        self.root.protocol("WM_DELETE_WINDOW", self._close)
        self._refresh_controls()
        self.root.after(200, self._drain_status)

    def _browse(self):
        selected = self.filedialog.askopenfilename(
            title="Select MetaTrader 5 terminal",
            filetypes=[("MetaTrader terminal", "terminal64.exe"), ("Executable", "*.exe")],
        )
        if selected:
            self.terminal_var.set(selected)

    def _pair(self):
        if self._busy or self.controller.paired:
            return
        code = self.code_var.get()
        terminal = self.terminal_var.get().strip()
        if not terminal or not Path(terminal).is_file():
            self._set_status("Error", "Select the installed MT5 terminal executable before pairing.")
            return
        operation_id = self._begin_operation()
        self._set_status("Starting", "Exchanging the temporary pairing code and starting the connector…")
        threading.Thread(
            target=self._pair_task,
            args=(operation_id, code, terminal),
            daemon=True,
        ).start()

    def _pair_task(self, operation_id, code, terminal):
        try:
            self.controller.pair(
                code,
                terminal,
                self.api_base_url,
                self._status_callback(operation_id),
            )
            self.status_queue.put((operation_id, "paired", "", ""))
        except Exception as error:
            self.status_queue.put((operation_id, "status", "Error", str(error)))
        finally:
            self.status_queue.put((operation_id, "done", "", ""))

    def _toggle_connector(self):
        if self._busy or not self.controller.paired:
            return
        if self.status_var.get() == "Access revoked":
            return
        operation_id = self._begin_operation()
        if self.controller.running:
            self._set_status("Stopping", "Stopping this connector's worker and local bridge…")
            action = self._stop_task
        else:
            self._set_status("Starting", "Starting the local bridge and worker…")
            action = self._start_task
        threading.Thread(target=action, args=(operation_id,), daemon=True).start()

    def _start_task(self, operation_id):
        try:
            self.controller.start(
                self.terminal_var.get().strip(),
                self._status_callback(operation_id),
            )
        except ConnectorError as error:
            self.status_queue.put((operation_id, "status", "Error", str(error)))
        finally:
            self.status_queue.put((operation_id, "done", "", ""))

    def _stop_task(self, operation_id):
        try:
            self.controller.stop(self._status_callback(operation_id))
        except Exception:
            self.status_queue.put((operation_id, "status", "Error", "Could not fully stop the connector. Close the app and retry; MT5 itself was not asked to log out."))
        finally:
            self.status_queue.put((operation_id, "done", "", ""))

    def _disconnect(self):
        if self._busy or not self.controller.paired:
            return
        confirmed = self.messagebox.askyesno(
            "Disconnect & Forget",
            "Stop this connector and remove its saved credential from this PC? This does not revoke the credential on the Profit Pilot website or change the MT5 account or positions.",
        )
        if not confirmed:
            return
        operation_id = self._begin_operation()
        self._set_status("Stopping", "Stopping the worker and bridge, then removing the local credential…")
        threading.Thread(target=self._forget_task, args=(operation_id,), daemon=True).start()

    def _forget_task(self, operation_id):
        try:
            self.controller.disconnect()
            self.status_queue.put((operation_id, "status", "Not paired", "Local credential removed. Revoke this device on the Profit Pilot website if server-side access should also be disabled."))
        except Exception:
            self.status_queue.put((operation_id, "status", "Error", "Local cleanup encountered an error. Check the connector credential file and retry Disconnect & Forget."))
        finally:
            self.status_queue.put((operation_id, "done", "", ""))

    def _close(self):
        self._operation_id += 1
        self.controller.stop()
        self.root.destroy()

    def _set_status(self, state, detail):
        self.status_queue.put((self._operation_id, "status", state, detail))

    def _status_callback(self, operation_id):
        return lambda state, detail: self.status_queue.put(
            (operation_id, "status", state, detail)
        )

    def _begin_operation(self):
        self._operation_id += 1
        self._busy = True
        self._refresh_controls()
        return self._operation_id

    def _refresh_controls(self):
        paired = self.controller.paired
        self.pair_button.configure(state="disabled" if paired or self._busy else "normal")
        self.code_entry.configure(state="disabled" if paired or self._busy else "normal")
        self.forget_button.configure(state="normal" if paired and not self._busy else "disabled")
        if self._busy:
            state = self.status_var.get()
            label = "Stopping" if state == "Stopping" else "Starting"
            self.control_button.configure(text=label, state="disabled")
        elif not paired:
            self.control_button.configure(text="Start Connector", state="disabled")
        elif self.controller.running:
            self.control_button.configure(text="Stop Connector", state="normal")
        elif self.status_var.get() == "Access revoked":
            self.control_button.configure(text="Start Connector", state="disabled")
        else:
            self.control_button.configure(text="Start Connector", state="normal")

    def _drain_status(self):
        while True:
            try:
                operation_id, kind, state, detail = self.status_queue.get_nowait()
            except __import__("queue").Empty:
                break
            if operation_id != self._operation_id:
                continue
            if kind == "done":
                self._busy = False
                self._refresh_controls()
                continue
            if kind == "paired":
                self.code_var.set("")
                self._refresh_controls()
                continue
            self.status_var.set(state)
            self.detail_var.set(detail)
            self._refresh_controls()
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
