import asyncio
import io
import json
import os
import queue
import tempfile
import threading
from types import SimpleNamespace
import unittest
import urllib.error
from pathlib import Path
from unittest.mock import patch

from bridge.connector_core import (
    ConnectorController,
    ConnectorError,
    ConnectorRunner,
    PairingCodeExpired,
    PairingRateLimited,
    TkStatusApp,
    exchange_pairing_code,
    TerminalInstanceLock,
    validate_api_base_url,
    worker_websocket_url,
)
from bridge.secure_store import WindowsCredentialStore
from bridge import worker_identity
from bridge.diagnostics import diagnostic_path, log_diagnostic


class FakeResponse:
    def __init__(self, payload, status=201):
        self.payload = payload
        self.status = status

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return None

    def read(self):
        return json.dumps(self.payload).encode("utf-8")


class PairingExchangeTests(unittest.TestCase):
    def test_maps_https_api_root_to_secure_worker_websocket_endpoint(self):
        self.assertEqual(
            worker_websocket_url("https://profit-pilot.example/api"),
            "wss://profit-pilot.example/api/worker-connect",
        )
        self.assertEqual(
            worker_websocket_url("http://127.0.0.1:4000/api"),
            "ws://127.0.0.1:4000/api/worker-connect",
        )
        with self.assertRaisesRegex(ValueError, "HTTPS"):
            validate_api_base_url("http://profit-pilot.example/api")

    def test_sends_exact_contract_only_over_https_and_returns_credential(self):
        captured = {}

        def opener(request, timeout):
            captured["url"] = request.full_url
            captured["method"] = request.method
            captured["payload"] = json.loads(request.data)
            captured["timeout"] = timeout
            return FakeResponse({
                "connectorId": "device-1",
                "accountId": "account-1",
                "credential": "opaque-device-credential",
            })

        result = exchange_pairing_code(
            "ab12 cd34 ef56 7890 ab12 cd34 ef56 7890",
            "https://profit-pilot.example/api",
            opener=opener,
        )
        self.assertEqual(captured, {
            "url": "https://profit-pilot.example/api/mt5/connectors/exchange",
            "method": "POST",
            "payload": {"code": "ab12cd34ef567890ab12cd34ef567890", "name": "Profit Pilot Connector"},
            "timeout": 15,
        })
        self.assertEqual(result["credential"], "opaque-device-credential")
        self.assertNotIn("credential", captured["payload"])

    def test_rejects_non_https_backend_and_bad_code(self):
        with self.assertRaisesRegex(ValueError, "HTTPS"):
            validate_api_base_url("http://profit-pilot.example/api")
        with self.assertRaisesRegex(ValueError, "32-character"):
            exchange_pairing_code("not-a-code", "https://profit-pilot.example/api")

    def test_expired_or_rate_limited_pairing_has_actionable_error(self):
        def response_error(status):
            return urllib.error.HTTPError("https://example/api", status, "", {}, io.BytesIO(b"{}"))

        for status, expected in ((400, PairingCodeExpired), (429, PairingRateLimited)):
            with self.subTest(status=status):
                with self.assertRaises(expected):
                    exchange_pairing_code(
                        "a" * 32,
                        "https://profit-pilot.example/api",
                        opener=lambda *_args, s=status, **_kwargs: (_ for _ in ()).throw(response_error(s)),
                    )

    def test_exchange_errors_never_include_response_body(self):
        error = urllib.error.HTTPError(
            "https://example/api",
            500,
            "server error",
            {},
            io.BytesIO(b"secret-credential-value"),
        )
        with self.assertRaises(ConnectorError) as caught:
            exchange_pairing_code(
                "a" * 32,
                "https://profit-pilot.example/api",
                opener=lambda *_args, **_kwargs: (_ for _ in ()).throw(error),
            )
        self.assertNotIn("secret-credential-value", str(caught.exception))


class FakeCredentialStore:
    def __init__(self, record=None):
        self.records = list(record) if isinstance(record, list) else [record] if record else []
        self.record = record
        self.saved = []
        self.delete_calls = 0

    def load(self):
        return self.records[0] if self.records else None

    def load_all(self):
        return [dict(record) for record in self.records]

    def save(self, record):
        self.records = [existing for existing in self.records if existing["connectorId"] != record["connectorId"]]
        self.records.append(dict(record))
        self.record = dict(record)
        self.saved.append(dict(record))

    def save_all(self, records):
        self.records = [dict(record) for record in records]
        self.record = self.records[0] if len(self.records) == 1 else None
        self.saved.append([dict(record) for record in records])

    def delete(self, connector_id=None):
        self.delete_calls += 1
        if connector_id is None:
            self.records = []
        else:
            self.records = [record for record in self.records if record["connectorId"] != connector_id]
        self.record = self.records[0] if len(self.records) == 1 else None


class FakeRunner:
    def __init__(self, record, terminal_path, callback):
        self.record = record
        self.terminal_path = terminal_path
        self.callback = callback
        self.started = False
        self.stopped = False
        self.start_calls = 0

    @property
    def is_running(self):
        return self.started and not self.stopped

    def start(self):
        self.started = True
        self.stopped = False
        self.start_calls += 1

    def stop(self):
        self.stopped = True


class CredentialStorageTests(unittest.TestCase):
    def test_dpapi_store_persists_only_encrypted_bytes_and_roundtrips(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "connector.dat"
            protect = lambda data: b"dpapi:" + data[::-1]
            unprotect = lambda data: data[6:][::-1]
            store = WindowsCredentialStore(path, protect=protect, unprotect=unprotect)
            record = {
                "connectorId": "device-id",
                "accountId": "account-id",
                "credential": "do-not-store-plaintext",
                "serverUrl": "wss://example/api/worker-connect",
                "terminalPath": "C:/Program Files/MetaTrader 5/terminal64.exe",
            }

            store.save(record)
            persisted = path.read_bytes()
            self.assertTrue(persisted.startswith(b"dpapi:"))
            self.assertNotIn(b"do-not-store-plaintext", persisted)
            self.assertEqual(store.load(), record)
            store.delete()
            self.assertIsNone(store.load())

    def test_legacy_single_record_loads_then_migrates_without_credential_loss(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "connector.dat"
            protect = lambda data: b"dpapi:" + data[::-1]
            unprotect = lambda data: data[6:][::-1]
            store = WindowsCredentialStore(path, protect=protect, unprotect=unprotect)
            legacy = {
                "connectorId": "legacy-device",
                "accountId": "legacy-account",
                "credential": "legacy-secret",
                "serverUrl": "wss://example/api/worker-connect",
                "terminalPath": "legacy-terminal.exe",
            }
            path.write_bytes(protect(json.dumps(legacy).encode("utf-8")))

            self.assertEqual(store.load_all(), [legacy])
            second = {**legacy, "connectorId": "second-device", "accountId": "second-account", "credential": "second-secret"}
            store.save(second)

            self.assertEqual(
                {record["connectorId"] for record in store.load_all()},
                {"legacy-device", "second-device"},
            )
            self.assertNotIn(b"legacy-secret", path.read_bytes())
            self.assertNotIn(b"second-secret", path.read_bytes())
            store.delete("legacy-device")
            self.assertEqual([record["connectorId"] for record in store.load_all()], ["second-device"])

    def test_pair_saves_exchange_credential_once_without_logging_it(self):
        store = FakeCredentialStore()
        runner_factory_calls = []
        record = {
            "connectorId": "device-id",
            "accountId": "account-id",
            "credential": "opaque-secret",
        }

        def runner_factory(*args):
            runner = FakeRunner(*args)
            runner_factory_calls.append(runner)
            return runner

        controller = ConnectorController(
            store,
            runner_factory,
            exchange=lambda code, api: record,
        )
        status = []
        with tempfile.NamedTemporaryFile() as terminal:
            connector_id = controller.pair(
                "a" * 32,
                terminal.name,
                "https://profit-pilot.example/api",
                lambda *event: status.append(event),
            )

        self.assertEqual(connector_id, "device-id")
        self.assertTrue(runner_factory_calls[0].started)
        self.assertEqual(store.records[0]["credential"], "opaque-secret")
        self.assertEqual(
            store.record["serverUrl"],
            "wss://profit-pilot.example/api/worker-connect",
        )
        self.assertNotIn("opaque-secret", repr(status))
        self.assertEqual(
            runner_factory_calls[0].record["serverUrl"],
            "wss://profit-pilot.example/api/worker-connect",
        )


class ConnectorStartupAndStatusTests(unittest.TestCase):
    def test_real_worker_verification_output_updates_correct_session_and_ui_queue(self):
        store = FakeCredentialStore([
            {"connectorId": "capital-device", "accountId": "capital-account", "credential": "not-for-logs", "serverUrl": "wss://example/api/worker-connect", "terminalPath": "capital.exe"},
            {"connectorId": "deriv-device", "accountId": "deriv-account", "credential": "also-not-for-logs", "serverUrl": "wss://example/api/worker-connect", "terminalPath": "deriv.exe"},
        ])
        controller = ConnectorController(store, runner_factory=FakeRunner)
        ui = SimpleNamespace(
            status_queue=queue.Queue(),
            _busy_sessions=set(),
            root=SimpleNamespace(after=lambda *_args: None),
        )
        rendered = []
        ui._drain_status = lambda: TkStatusApp._drain_status(ui)
        ui._render_sessions = lambda: rendered.append(
            {
                item["connectorId"]: (item["backendState"], item["mt5State"])
                for item in controller.status_snapshot()
            }
        )

        def make_status_callback(connector_id):
            generation = controller.sessions[connector_id].generation + 1
            controller.sessions[connector_id].generation = generation
            return controller._session_callback(
                connector_id,
                generation,
                lambda state, detail: TkStatusApp._queue_session_status(
                    ui, connector_id, state, detail
                ),
            )

        capital_callback = make_status_callback("capital-device")
        deriv_callback = make_status_callback("deriv-device")
        capital_runner = ConnectorRunner(
            store.records[0], "capital.exe", capital_callback
        )
        deriv_runner = ConnectorRunner(
            store.records[1], "deriv.exe", deriv_callback
        )

        class OutputProcess:
            def __init__(self, text):
                self.stdout = io.StringIO(text)

        capital_runner._read_output(
            "worker_identity.py",
            OutputProcess(
                "Identity accepted: worker=capital user=user-a account=capital-account\n"
                "Verified MT5 account: login ending 8288; server=Capital-Demo\n"
                "MT5 trade allowed: Yes\n"
                "Read-only account result returned; fields: login, server\n"
            ),
        )
        deriv_runner._read_output(
            "worker_identity.py",
            OutputProcess(
                "Identity accepted: worker=deriv user=user-a account=deriv-account\n"
                "Verified MT5 account: login ending 0012; server=Deriv-Demo\n"
                "MT5 trade allowed: No\n"
                "Read-only account result returned; fields: login, server\n"
            ),
        )
        TkStatusApp._drain_status(ui)

        snapshots = {
            item["connectorId"]: item for item in controller.status_snapshot()
        }
        self.assertEqual(snapshots["capital-device"]["status"], "Connected")
        self.assertEqual(snapshots["deriv-device"]["status"], "Connected")
        self.assertEqual(snapshots["capital-device"]["backendState"], "Connected")
        self.assertEqual(snapshots["capital-device"]["mt5State"], "Verified")
        self.assertIn("login and server match", snapshots["capital-device"]["detail"])
        self.assertIn("trading allowed: Yes", snapshots["capital-device"]["detail"])
        self.assertEqual(snapshots["deriv-device"]["backendState"], "Connected")
        self.assertEqual(snapshots["deriv-device"]["mt5State"], "Verified")
        self.assertIn("trading allowed: No", snapshots["deriv-device"]["detail"])
        self.assertIn("8288", snapshots["capital-device"]["identity"])
        self.assertIn("Capital-Demo", snapshots["capital-device"]["identity"])
        self.assertIn("0012", snapshots["deriv-device"]["identity"])
        self.assertIn("Deriv-Demo", snapshots["deriv-device"]["identity"])
        self.assertTrue(rendered)
        self.assertEqual(
            rendered[-1],
            {
                "capital-device": ("Connected", "Verified"),
                "deriv-device": ("Connected", "Verified"),
            },
        )
        self.assertTrue(ui.status_queue.empty())

    def test_backend_connected_is_visible_while_mt5_verification_is_pending(self):
        store = FakeCredentialStore({
            "connectorId": "capital-device",
            "accountId": "capital-account",
            "credential": "not-for-logs",
            "serverUrl": "wss://example/api/worker-connect",
            "terminalPath": "capital.exe",
        })
        controller = ConnectorController(store, runner_factory=FakeRunner)
        ui = SimpleNamespace(
            status_queue=queue.Queue(),
            _busy_sessions=set(),
            root=SimpleNamespace(after=lambda *_args: None),
        )
        rendered = []
        ui._drain_status = lambda: TkStatusApp._drain_status(ui)
        ui._render_sessions = lambda: rendered.append(controller.status_snapshot()[0])
        session = controller.sessions["capital-device"]
        session.generation = 1
        callback = controller._session_callback(
            "capital-device",
            1,
            lambda state, detail: TkStatusApp._queue_session_status(
                ui, "capital-device", state, detail
            ),
        )
        runner = ConnectorRunner(store.records[0], "capital.exe", callback)

        class OutputProcess:
            def __init__(self, text):
                self.stdout = io.StringIO(text)

        runner._read_output(
            "worker_identity.py",
            OutputProcess(
                "Identity accepted: worker=capital user=user-a account=capital-account\n"
            ),
        )
        TkStatusApp._drain_status(ui)

        snapshot = controller.status_snapshot()[0]
        self.assertEqual(snapshot["backendState"], "Connected")
        self.assertEqual(snapshot["mt5State"], "Verifying")
        self.assertFalse(snapshot["identity"])
        self.assertEqual(rendered[-1]["backendState"], "Connected")
        self.assertEqual(rendered[-1]["mt5State"], "Verifying")
        self.assertNotEqual(snapshot["mt5State"], "Verified")

    def test_launcher_reads_worker_descendant_diagnostic_pid(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(
            os.environ, {"LOCALAPPDATA": directory}
        ):
            store = FakeCredentialStore({
                "connectorId": "deriv-device",
                "accountId": "deriv-account",
                "credential": "not-for-logs",
                "serverUrl": "wss://example/api/worker-connect",
                "terminalPath": "deriv.exe",
            })
            controller = ConnectorController(store, runner_factory=FakeRunner)
            session = controller.sessions["deriv-device"]
            session.generation = 1
            backend_authenticated = threading.Event()
            connected = threading.Event()
            callback = controller._session_callback(
                "deriv-device",
                1,
                lambda state, _detail: (
                    backend_authenticated.set()
                    if state == "_backend_connected"
                    else connected.set()
                    if state == "Connected"
                    else None
                ),
            )
            runner = ConnectorRunner(store.records[0], "deriv.exe", callback)
            launcher_pid = 744
            worker_pid = 23064
            worker_log = Path(directory) / "ProfitPilot" / "diagnostics" / f"connector-diagnostics-{worker_pid}.jsonl"
            worker_log.parent.mkdir(parents=True, exist_ok=True)

            class LauncherProcess:
                pid = launcher_pid

                def __init__(self):
                    self.exited = threading.Event()

                def poll(self):
                    return 0 if self.exited.is_set() else None

            launcher = LauncherProcess()
            with patch(
                "bridge.connector_core.owned_process_descendants",
                return_value=[worker_pid],
            ), patch(
                "bridge.connector_core.diagnostic_path",
                side_effect=lambda process_id=None: Path(directory) / "ProfitPilot" / "diagnostics" / f"connector-diagnostics-{process_id}.jsonl",
            ):
                reader = threading.Thread(
                    target=runner._read_worker_diagnostics,
                    args=(launcher,),
                    daemon=True,
                )
                reader.start()
                identity_ack = {
                    "timestamp": "2026-10-12T08:19:14.728+00:00",
                    "pid": worker_pid,
                    "stage": "worker_identity_acknowledged",
                    "session": "redacted-session",
                    "fields": {
                        "worker_id": "redacted-worker",
                        "account_id": "redacted-account",
                        "configured_login_suffix": "3083",
                        "configured_server": "Deriv-Demo",
                    },
                }
                with worker_log.open("w", encoding="utf-8") as stream:
                    stream.write(json.dumps(identity_ack) + "\n")
                self.assertTrue(backend_authenticated.wait(timeout=2))
                pending_snapshot = controller.status_snapshot()[0]
                self.assertEqual(pending_snapshot["backendState"], "Connected")
                self.assertEqual(pending_snapshot["mt5State"], "Verifying")
                self.assertNotEqual(pending_snapshot["mt5State"], "Verified")

                worker_events = [
                    {
                        "timestamp": "2026-10-12T08:19:18.694+00:00",
                        "pid": worker_pid,
                        "stage": "worker_account_verification_succeeded",
                        "session": "redacted-session",
                        "fields": {
                            "login_suffix": "3083",
                            "server": "Deriv-Demo",
                            "trade_allowed": True,
                        },
                    },
                    {
                        "timestamp": "2026-10-12T08:19:18.696+00:00",
                        "pid": worker_pid,
                        "stage": "worker_account_result_emitted",
                        "session": "redacted-session",
                        "fields": {"ok": True, "request_id": "redacted-request"},
                    },
                ]
                with worker_log.open("a", encoding="utf-8") as stream:
                    for event in worker_events:
                        stream.write(json.dumps(event) + "\n")
                self.assertTrue(connected.wait(timeout=2))
                launcher.exited.set()
                reader.join(timeout=2)

            snapshot = controller.status_snapshot()[0]
            self.assertEqual(snapshot["backendState"], "Connected")
            self.assertEqual(snapshot["mt5State"], "Verified")
            self.assertIn("3083", snapshot["identity"])
            self.assertIn("Deriv-Demo", snapshot["identity"])
            self.assertIn("trading allowed: Yes", snapshot["detail"])
            self.assertFalse(reader.is_alive())

    def test_worker_diagnostic_file_updates_session_when_stdout_is_silent(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(
            os.environ, {"LOCALAPPDATA": directory}
        ):
            diagnostic_file = Path(directory) / "worker-events.jsonl"
            store = FakeCredentialStore({
                "connectorId": "capital-device",
                "accountId": "capital-account",
                "credential": "never-log-this",
                "serverUrl": "wss://example/api/worker-connect",
                "terminalPath": "capital.exe",
            })
            controller = ConnectorController(store, runner_factory=FakeRunner)
            session = controller.sessions["capital-device"]
            session.generation = 1
            received = threading.Event()
            callback = controller._session_callback(
                "capital-device",
                1,
                lambda state, _detail: received.set() if state == "Connected" else None,
            )
            runner = ConnectorRunner(store.records[0], "capital.exe", callback)

            class ChildProcess:
                pid = 43210

                def __init__(self):
                    self.exited = threading.Event()

                def poll(self):
                    return 0 if self.exited.is_set() else None

            process = ChildProcess()
            with patch("bridge.connector_core.diagnostic_path", return_value=diagnostic_file), patch(
                "bridge.diagnostics.diagnostic_path", return_value=diagnostic_file
            ):
                reader = threading.Thread(
                    target=runner._read_worker_diagnostics,
                    args=(process,),
                    daemon=True,
                )
                reader.start()
                worker_events = (
                    {
                        "timestamp": "2026-10-12T08:19:18.694+00:00",
                        "pid": process.pid,
                        "stage": "worker_account_verification_succeeded",
                        "session": "redacted-session",
                        "fields": {
                            "login_suffix": "8288",
                            "server": "Capital.ComBah-Demo",
                            "trade_allowed": True,
                        },
                    },
                    {
                        "timestamp": "2026-10-12T08:19:18.696+00:00",
                        "pid": process.pid,
                        "stage": "worker_account_result_emitted",
                        "session": "redacted-session",
                        "fields": {"ok": True, "request_id": "safe-test-request"},
                    },
                )
                with diagnostic_file.open("w", encoding="utf-8") as stream:
                    for event in worker_events:
                        stream.write(json.dumps(event) + "\n")
                self.assertTrue(received.wait(timeout=2))
                process.exited.set()
                reader.join(timeout=2)

            snapshot = controller.status_snapshot()[0]
            self.assertEqual(snapshot["status"], "Connected")
            self.assertEqual(
                snapshot["identity"],
                "login ending 8288; server=Capital.ComBah-Demo",
            )
            self.assertFalse(reader.is_alive())

    def test_incomplete_identity_output_does_not_mark_session_verified(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(
            os.environ, {"LOCALAPPDATA": directory}
        ):
            statuses = []
            runner = ConnectorRunner({}, "", lambda *event: statuses.append(event))
            runner._handle_line(
                "worker_identity.py",
                "Verified MT5 account: login ending 82; server=",
            )
            self.assertEqual(statuses, [])
            content = diagnostic_path().read_text(encoding="utf-8")
            self.assertIn('"message_type":"account_identity_verified"', content)
            self.assertIn('"valid":false', content)

    def test_worker_error_and_early_exit_are_recorded_in_diagnostic_file(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(
            os.environ, {"LOCALAPPDATA": directory}
        ):
            statuses = []
            runner = ConnectorRunner(
                {"connectorId": "diagnostic-device", "credential": "never-log-this"},
                "",
                lambda *event: statuses.append(event),
            )
            runner._handle_line(
                "worker_identity.py",
                "Worker connection ended (OSError); retrying.",
            )

            class ExitedProcess:
                pid = 4321
                stdout = None

                def poll(self):
                    return 1

                def wait(self):
                    return 1

            runner._watch_exit("worker_identity.py", ExitedProcess())
            content = diagnostic_path().read_text(encoding="utf-8")
            self.assertIn("worker_message_parsed", content)
            self.assertIn("worker_process_exited", content)
            self.assertIn("exit_code", content)
            self.assertNotIn("never-log-this", content)

    def test_diagnostic_records_redact_sensitive_values_and_support_concurrent_sessions(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(
            os.environ, {"LOCALAPPDATA": directory}
        ):
            threads = [
                threading.Thread(
                    target=log_diagnostic,
                    args=("test_event", f"session-{index}"),
                    kwargs={
                        "raw_output": f"login=123456 credential=raw-secret-{index} account=short-account-{index}",
                        "account_id": "0123456789abcdef01234567",
                    },
                )
                for index in range(12)
            ]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join()

            records = [
                json.loads(line)
                for line in diagnostic_path().read_text(encoding="utf-8").splitlines()
            ]
            self.assertEqual(len(records), 12)
            self.assertTrue(all("timestamp" in record and "session" in record for record in records))
            diagnostic_text = json.dumps(records)
            self.assertNotIn("123456", diagnostic_text)
            self.assertNotIn("raw-secret", diagnostic_text)
            self.assertNotIn("short-account", diagnostic_text)
            self.assertNotIn("0123456789abcdef01234567", diagnostic_text)

    def test_missing_terminal_fails_before_starting_subprocesses(self):
        statuses = []
        runner = ConnectorRunner(
            {"connectorId": "device-id", "credential": "opaque"},
            "Z:/missing/terminal64.exe",
            lambda *value: statuses.append(value),
            popen=lambda *_args, **_kwargs: self.fail("should not spawn"),
        )
        with patch("bridge.connector_core.platform.system", return_value="Windows"):
            with self.assertRaisesRegex(ConnectorError, "terminal not found"):
                runner.start()
        self.assertEqual(statuses, [])

    def test_packaged_child_environment_uses_pairing_credential_not_legacy_values(self):
        class FakeProcess:
            stdout = None

            def __init__(self):
                self.exited = threading.Event()

            def poll(self):
                return 0 if self.exited.is_set() else None

            def terminate(self):
                self.exited.set()

            def kill(self):
                self.exited.set()

            def wait(self, timeout=None):
                if not self.exited.wait(timeout):
                    raise __import__("subprocess").TimeoutExpired("fake", timeout)
                return 0

        calls = []

        def fake_popen(args, **kwargs):
            calls.append((args, kwargs))
            return FakeProcess()

        statuses = []
        runner = ConnectorRunner(
            {
                "connectorId": "device-id-123456789",
                "accountId": "account-id",
                "credential": "paired-credential",
                "serverUrl": "wss://example/api/worker-connect",
            },
            "terminal.exe",
            lambda *value: statuses.append(value),
            popen=fake_popen,
        )
        with patch("bridge.connector_core.platform.system", return_value="Windows"), patch(
            "bridge.connector_core.Path.is_file", return_value=True
        ), patch.dict(
            os.environ,
            {
                "MT5_LOGIN": "legacy-login",
                "MT5_PASSWORD": "legacy-password",
                "MT5_SERVER": "legacy-server",
                "WORKER_IDENTITY_TOKEN": "legacy-token",
            },
        ):
            runner.start()

        self.assertEqual(len(calls), 2)
        env = calls[0][1]["env"]
        self.assertEqual(env["WORKER_IDENTITY_TOKEN"], "paired-credential")
        self.assertEqual(env["MT5_TERMINAL_PATH"], "terminal.exe")
        self.assertEqual(env["MT5_BRIDGE_HOST"], "127.0.0.1")
        self.assertTrue(env["MT5_BRIDGE_URL"].startswith("http://127.0.0.1:"))
        self.assertNotEqual(env["PORT"], "8000")
        self.assertNotIn("MT5_LOGIN", env)
        self.assertNotIn("MT5_PASSWORD", env)
        self.assertNotIn("MT5_SERVER", env)
        self.assertNotIn("JWT_SECRET", env)
        self.assertNotIn("JWT", env)
        self.assertEqual(env["PYTHON_DOTENV_DISABLED"], "1")
        self.assertEqual(env["PYTHONUNBUFFERED"], "1")
        self.assertNotIn("paired-credential", repr(statuses))
        runner.stop()

    def test_verified_account_read_is_connected_and_network_loss_keeps_retry_message_safe(self):
        statuses = []
        runner = ConnectorRunner({}, "", lambda *value: statuses.append(value))
        runner._handle_line("worker_identity.py", "Read-only account result returned; fields: login, server")
        self.assertTrue(runner.connected)
        self.assertEqual(statuses[-1][0], "Connected")

        runner._handle_line("worker_identity.py", "Worker connection ended (ConnectionClosedError); retrying.")
        self.assertFalse(runner.connected)
        self.assertEqual(statuses[-1][0], "Disconnected")
        self.assertIn("retry automatically", statuses[-1][1])
        self.assertNotIn("credential", statuses[-1][1].casefold())

    def test_login_mismatch_stops_worker_and_is_actionable(self):
        statuses = []
        runner = ConnectorRunner({}, "", lambda *value: statuses.append(value))
        runner._handle_line(
            "worker_identity.py",
            "Read-only account request failed: Connected MT5 login does not match the bound account",
        )
        self.assertEqual(statuses[-1][0], "Error")
        self.assertIn("does not match", statuses[-1][1].casefold())
        self.assertTrue(runner.stop_event.is_set())
        self.assertNotIn("credential", statuses[-1][1].casefold())

    def test_temporary_websocket_loss_is_not_reported_as_revoked(self):
        statuses = []
        runner = ConnectorRunner({}, "", lambda *value: statuses.append(value))
        runner._handle_line(
            "worker_identity.py",
            "Worker connection ended (WebSocket 1006: connection closed); retrying.",
        )
        self.assertEqual(statuses[-1][0], "Disconnected")
        self.assertIn("retry automatically", statuses[-1][1])
        self.assertNotIn("revoked", statuses[-1][1].casefold())

    def test_only_explicit_backend_revocation_is_classified_as_revoked(self):
        self.assertEqual(worker_identity.terminal_worker_exit_code("Connector revoked"), 20)
        self.assertEqual(
            worker_identity.terminal_worker_exit_code("Connector credential revoked"),
            20,
        )
        self.assertIsNone(
            worker_identity.terminal_worker_exit_code("connection closed")
        )

    def test_revoked_worker_exit_stops_bridge_and_reports_access_revoked(self):
        class ExitedWorker:
            stdout = None

            def poll(self):
                return 20

            def wait(self):
                return 20

        bridge = OwnedProcessCleanupTests.FakeProcess("owned-bridge")
        statuses = []
        runner = ConnectorRunner({}, "", lambda *status: statuses.append(status))
        runner.processes = [("probe.py", bridge), ("worker_identity.py", ExitedWorker())]
        runner._watch_exit("worker_identity.py", runner.processes[1][1])

        self.assertIsNotNone(bridge.poll())
        self.assertEqual(statuses[-1][0], "Access revoked")
        self.assertEqual(runner.processes, [])

    def test_account_read_failure_and_existing_worker_are_actionable(self):
        statuses = []
        runner = ConnectorRunner({}, "", lambda *value: statuses.append(value))
        runner._handle_line(
            "worker_identity.py",
            "Read-only account request failed: MT5 probe returned HTTP 503",
        )
        self.assertEqual(statuses[-1][0], "Error")
        self.assertIn("MetaTrader5", statuses[-1][1])
        runner._handle_line(
            "worker_identity.py",
            "Worker connection ended (ConnectionClosedError); retrying. WebSocket 1013: Worker already connected for this account",
        )
        self.assertEqual(statuses[-1][0], "Disconnected")
        self.assertIn("active connector", statuses[-1][1])

    def test_disconnect_forgets_local_credential(self):
        store = FakeCredentialStore({
            "connectorId": "device-id",
            "accountId": "account-id",
            "credential": "opaque-secret",
            "serverUrl": "wss://example/api/worker-connect",
            "terminalPath": "terminal.exe",
        })
        controller = ConnectorController(store, runner_factory=FakeRunner)
        controller.disconnect("device-id")
        self.assertFalse(controller.paired)
        self.assertIsNone(store.record)
        self.assertEqual(store.records, [])

    def test_disconnect_persistence_failure_keeps_session_visible_for_retry(self):
        class FailingStore(FakeCredentialStore):
            def save_all(self, _records):
                raise OSError("credential file is locked")

        record = {
            "connectorId": "device-id",
            "accountId": "account-id",
            "credential": "opaque-secret",
            "serverUrl": "wss://example/api/worker-connect",
            "terminalPath": "terminal.exe",
        }
        store = FailingStore(record)
        controller = ConnectorController(store, runner_factory=FakeRunner)
        with self.assertRaisesRegex(OSError, "locked"):
            controller.disconnect("device-id")

        snapshot = controller.status_snapshot()[0]
        self.assertEqual(snapshot["connectorId"], "device-id")
        self.assertEqual(snapshot["status"], "Error")
        self.assertIn("Retry Disconnect & Forget", snapshot["detail"])
        self.assertEqual(store.records[0]["credential"], "opaque-secret")

    def test_forget_task_always_notifies_ui_to_release_busy_state(self):
        failing_ui = SimpleNamespace(
            controller=SimpleNamespace(
                disconnect=lambda _connector_id: (_ for _ in ()).throw(OSError("locked"))
            ),
            status_queue=queue.Queue(),
        )
        TkStatusApp._forget_task(failing_ui, "capital-device")
        self.assertEqual(failing_ui.status_queue.get_nowait()[0], "forget_failed")
        self.assertEqual(failing_ui.status_queue.get_nowait()[0], "forget_finished")

        ui = SimpleNamespace(
            status_queue=queue.Queue(),
            _busy_sessions={"capital-device"},
            _forgetting_sessions={"capital-device"},
            root=SimpleNamespace(after=lambda *_args: None),
            _render_sessions=lambda: None,
        )
        ui._drain_status = lambda: TkStatusApp._drain_status(ui)
        ui.status_queue.put(("forget_failed", "capital-device", None, None))
        ui.status_queue.put(("forget_finished", "capital-device", None, None))
        TkStatusApp._drain_status(ui)
        self.assertNotIn("capital-device", ui._busy_sessions)
        self.assertNotIn("capital-device", ui._forgetting_sessions)

    def test_forget_after_revocation_stops_runner_and_deletes_only_local_record(self):
        store = FakeCredentialStore({
            "connectorId": "device-id",
            "accountId": "account-id",
            "credential": "opaque-secret",
            "serverUrl": "wss://example/api/worker-connect",
            "terminalPath": "terminal.exe",
        })
        controller = ConnectorController(store, runner_factory=FakeRunner)
        with tempfile.NamedTemporaryFile() as terminal:
            controller.start("device-id", lambda *_status: None, terminal.name)
            runner = controller.sessions["device-id"].runner
            runner.callback("Access revoked", "revoked remotely")
            controller.disconnect("device-id")

        self.assertTrue(runner.stopped)
        self.assertFalse(controller.paired)
        self.assertIsNone(store.record)
        self.assertEqual(store.records, [])

    def test_start_stop_and_repeated_actions_reuse_only_one_runner(self):
        store = FakeCredentialStore({
            "connectorId": "device-id",
            "accountId": "account-id",
            "credential": "opaque-secret",
            "serverUrl": "wss://example/api/worker-connect",
            "terminalPath": "terminal.exe",
        })
        created = []

        def factory(*args):
            runner = FakeRunner(*args)
            created.append(runner)
            return runner

        controller = ConnectorController(store, runner_factory=factory)
        with tempfile.NamedTemporaryFile() as terminal:
            controller.start("device-id", lambda *_status: None, terminal.name)
            controller.start("device-id", lambda *_status: None, terminal.name)
            self.assertEqual(len(created), 1)
            self.assertEqual(created[0].start_calls, 1)
            self.assertTrue(controller.running)

            controller.stop("device-id", lambda *_status: None)
            controller.stop("device-id", lambda *_status: None)
            self.assertFalse(controller.running)
            self.assertTrue(created[0].stopped)
            controller.start("device-id", lambda *_status: None, terminal.name)
            self.assertEqual(len(created), 2)
            self.assertTrue(controller.running)

            controller.disconnect("device-id")
            self.assertFalse(controller.paired)
            self.assertFalse(controller.running)

    def test_stop_discards_stale_runner_callbacks(self):
        store = FakeCredentialStore({
            "connectorId": "device-id",
            "accountId": "account-id",
            "credential": "opaque-secret",
            "serverUrl": "wss://example/api/worker-connect",
            "terminalPath": "terminal.exe",
        })
        controller = ConnectorController(store, runner_factory=FakeRunner)
        statuses = []
        with tempfile.NamedTemporaryFile() as terminal:
            controller.start("device-id", lambda *status: statuses.append(status), terminal.name)
            stale_callback = controller.sessions["device-id"].runner.callback
            controller.stop("device-id", lambda *status: statuses.append(status))
            stale_callback("Access revoked", "late revocation")

        self.assertEqual(statuses[-1][0], "Disconnected")
        self.assertNotIn(("Access revoked", "late revocation"), statuses)

    def test_two_accounts_pair_and_run_independently_with_distinct_terminals(self):
        store = FakeCredentialStore()
        pair_records = iter((
            {"connectorId": "capital-device", "accountId": "capital-account", "credential": "capital-secret"},
            {"connectorId": "deriv-device", "accountId": "deriv-account", "credential": "deriv-secret"},
        ))
        controller = ConnectorController(
            store,
            runner_factory=FakeRunner,
            exchange=lambda *_args: next(pair_records),
        )
        with tempfile.NamedTemporaryFile() as capital_terminal, tempfile.NamedTemporaryFile() as deriv_terminal:
            capital_id = controller.pair("a" * 32, capital_terminal.name, "https://example/api", lambda *_event: None, "Capital.com Demo")
            deriv_id = controller.pair("b" * 32, deriv_terminal.name, "https://example/api", lambda *_event: None, "Deriv Demo")

            self.assertEqual({record["connectorId"] for record in store.records}, {capital_id, deriv_id})
            self.assertEqual(controller.sessions[capital_id].record["credential"], "capital-secret")
            self.assertEqual(controller.sessions[deriv_id].record["credential"], "deriv-secret")
            self.assertTrue(controller.sessions[capital_id].runner.is_running)
            self.assertTrue(controller.sessions[deriv_id].runner.is_running)

            capital_runner = controller.sessions[capital_id].runner
            deriv_runner = controller.sessions[deriv_id].runner
            controller.stop(capital_id)
            self.assertTrue(capital_runner.stopped)
            self.assertFalse(deriv_runner.stopped)
            controller.disconnect(capital_id)

        self.assertEqual([record["connectorId"] for record in store.records], ["deriv-device"])
        self.assertIn(deriv_id, controller.sessions)
        self.assertNotIn(capital_id, controller.sessions)

    def test_two_sessions_cannot_start_on_the_same_terminal_path(self):
        store = FakeCredentialStore([
            {"connectorId": "account-a-device", "accountId": "account-a", "credential": "secret-a", "serverUrl": "wss://example/api/worker-connect", "terminalPath": "terminal.exe"},
            {"connectorId": "account-b-device", "accountId": "account-b", "credential": "secret-b", "serverUrl": "wss://example/api/worker-connect", "terminalPath": "terminal.exe"},
        ])
        controller = ConnectorController(store, runner_factory=FakeRunner)
        with tempfile.NamedTemporaryFile() as terminal:
            controller.start("account-a-device", lambda *_status: None, terminal.name)
            with self.assertRaisesRegex(ConnectorError, "separate MT5 installation"):
                controller.start("account-b-device", lambda *_status: None, terminal.name)
        self.assertTrue(controller.sessions["account-a-device"].runner.is_running)
        self.assertIsNone(controller.sessions["account-b-device"].runner)

    def test_revoking_and_forgetting_one_account_preserves_other_account(self):
        capital = {"connectorId": "capital-device", "accountId": "capital", "credential": "capital-secret", "serverUrl": "wss://example/api/worker-connect", "terminalPath": "capital.exe", "displayName": "Capital.com Demo"}
        deriv = {"connectorId": "deriv-device", "accountId": "deriv", "credential": "deriv-secret", "serverUrl": "wss://example/api/worker-connect", "terminalPath": "deriv.exe", "displayName": "Deriv Demo"}
        store = FakeCredentialStore([capital, deriv])
        controller = ConnectorController(store, runner_factory=FakeRunner)
        statuses = {"capital-device": [], "deriv-device": []}
        with tempfile.NamedTemporaryFile() as capital_terminal, tempfile.NamedTemporaryFile() as deriv_terminal:
            controller.start("capital-device", lambda *event: statuses["capital-device"].append(event), capital_terminal.name)
            controller.start("deriv-device", lambda *event: statuses["deriv-device"].append(event), deriv_terminal.name)
            capital_runner = controller.sessions["capital-device"].runner
            deriv_runner = controller.sessions["deriv-device"].runner

            capital_runner.stop()
            capital_runner.callback("Access revoked", "revoked by website")
            self.assertEqual(controller.sessions["capital-device"].status, "Access revoked")
            self.assertEqual(controller.sessions["deriv-device"].status, "Starting")
            self.assertTrue(capital_runner.stopped)
            self.assertFalse(deriv_runner.stopped)
            controller.disconnect("capital-device")

        self.assertEqual([record["connectorId"] for record in store.records], ["deriv-device"])
        self.assertTrue(controller.sessions["deriv-device"].runner.is_running)
        self.assertEqual(statuses["deriv-device"], [])

    def test_stale_callback_from_forgotten_session_cannot_change_another_row(self):
        store = FakeCredentialStore([
            {"connectorId": "capital-device", "accountId": "capital", "credential": "capital-secret", "serverUrl": "wss://example/api/worker-connect", "terminalPath": "capital.exe"},
            {"connectorId": "deriv-device", "accountId": "deriv", "credential": "deriv-secret", "serverUrl": "wss://example/api/worker-connect", "terminalPath": "deriv.exe"},
        ])
        controller = ConnectorController(store, runner_factory=FakeRunner)
        with tempfile.NamedTemporaryFile() as capital_terminal, tempfile.NamedTemporaryFile() as deriv_terminal:
            controller.start("capital-device", lambda *_event: None, capital_terminal.name)
            controller.start("deriv-device", lambda *_event: None, deriv_terminal.name)
            stale_callback = controller.sessions["capital-device"].runner.callback
            controller.disconnect("capital-device")
            deriv_status = controller.sessions["deriv-device"].status
            stale_callback("Access revoked", "late event")

        self.assertEqual(controller.sessions["deriv-device"].status, deriv_status)

    def test_repeated_account_identity_callbacks_are_session_scoped(self):
        store = FakeCredentialStore([
            {"connectorId": "capital-device", "accountId": "capital", "credential": "capital-secret", "serverUrl": "wss://example/api/worker-connect", "terminalPath": "capital.exe"},
            {"connectorId": "deriv-device", "accountId": "deriv", "credential": "deriv-secret", "serverUrl": "wss://example/api/worker-connect", "terminalPath": "deriv.exe"},
        ])
        controller = ConnectorController(store, runner_factory=FakeRunner)
        with tempfile.NamedTemporaryFile() as capital_terminal, tempfile.NamedTemporaryFile() as deriv_terminal:
            controller.start("capital-device", lambda *_event: None, capital_terminal.name)
            controller.start("deriv-device", lambda *_event: None, deriv_terminal.name)
            controller.sessions["capital-device"].runner.callback("_account", "login ending 8288; server=Capital-Demo")
            controller.sessions["deriv-device"].runner.callback("_account", "login ending 0012; server=Deriv-Demo")

        snapshots = {item["connectorId"]: item for item in controller.status_snapshot()}
        self.assertEqual(snapshots["capital-device"]["identity"], "login ending 8288; server=Capital-Demo")
        self.assertEqual(snapshots["deriv-device"]["identity"], "login ending 0012; server=Deriv-Demo")
        self.assertEqual(
            snapshots["capital-device"]["record"]["identitySummary"],
            "login ending 8288; server=Capital-Demo",
        )
        self.assertEqual(
            snapshots["deriv-device"]["record"]["identitySummary"],
            "login ending 0012; server=Deriv-Demo",
        )
        self.assertIsNotNone(snapshots["capital-device"]["record"]["lastVerifiedAt"])
        self.assertIsNotNone(snapshots["deriv-device"]["record"]["lastVerifiedAt"])


class OwnedProcessCleanupTests(unittest.TestCase):
    class FakeProcess:
        stdout = None

        def __init__(self, pid):
            self.pid = pid
            self.exited = threading.Event()
            self.terminate_calls = 0

        def poll(self):
            return 0 if self.exited.is_set() else None

        def terminate(self):
            self.terminate_calls += 1
            self.exited.set()

        def kill(self):
            self.exited.set()

        def wait(self, timeout=None):
            if not self.exited.wait(timeout):
                raise __import__("subprocess").TimeoutExpired("fake", timeout)
            return 0

    def test_repeated_start_and_stop_terminate_only_owned_worker_and_bridge(self):
        created = []
        unrelated = self.FakeProcess("unrelated")

        def popen(*_args, **_kwargs):
            process = self.FakeProcess(len(created) + 1)
            created.append(process)
            return process

        runner = ConnectorRunner(
            {
                "connectorId": "device-id-123456789",
                "accountId": "account-id",
                "credential": "paired-credential",
                "serverUrl": "wss://example/api/worker-connect",
            },
            "terminal.exe",
            lambda *_status: None,
            popen=popen,
        )
        with patch("bridge.connector_core.platform.system", return_value="Windows"), patch(
            "bridge.connector_core.Path.is_file", return_value=True
        ):
            runner.start()
            runner.start()
            self.assertEqual(len(created), 2)
            runner.stop()
            runner.stop()

        self.assertEqual([process.terminate_calls for process in created], [1, 1])
        self.assertTrue(all(process.poll() is not None for process in created))
        self.assertIsNone(unrelated.poll())

    def test_stopping_one_runner_leaves_another_sessions_processes_alive(self):
        created = []
        child_environments = []

        def popen(*_args, **kwargs):
            process = self.FakeProcess(len(created) + 1)
            created.append(process)
            child_environments.append(kwargs["env"])
            return process

        runners = [
            ConnectorRunner(
                {
                    "connectorId": f"device-{index}-123456789",
                    "accountId": f"account-{index}",
                    "credential": f"credential-{index}",
                    "serverUrl": "wss://example/api/worker-connect",
                },
                f"terminal-{index}.exe",
                lambda *_status: None,
                popen=popen,
            )
            for index in (1, 2)
        ]
        with tempfile.TemporaryDirectory() as directory, patch(
            "bridge.connector_core.platform.system", return_value="Windows"
        ), patch("bridge.connector_core.Path.is_file", return_value=True), patch.dict(
            os.environ, {"LOCALAPPDATA": directory}
        ):
            runners[0].start()
            runners[1].start()
            worker_environments = [
                environment
                for environment in child_environments
                if "WORKER_IDENTITY_TOKEN" in environment
            ]
            self.assertEqual(
                {environment["WORKER_IDENTITY_TOKEN"] for environment in worker_environments},
                {"credential-1", "credential-2"},
            )
            self.assertEqual(
                len({environment["MT5_BRIDGE_URL"] for environment in worker_environments}),
                2,
            )
            runners[0].stop()
            self.assertTrue(all(process.poll() is not None for process in created[:2]))
            self.assertTrue(all(process.poll() is None for process in created[2:]))
            runners[1].stop()

    def test_terminal_instance_lock_excludes_only_the_same_installation(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(
            os.environ, {"LOCALAPPDATA": directory}
        ):
            first = TerminalInstanceLock("C:/MT5/Capital/terminal64.exe")
            same = TerminalInstanceLock("c:\\mt5\\capital\\terminal64.exe")
            separate = TerminalInstanceLock("C:/MT5/Deriv/terminal64.exe")
            with patch("bridge.connector_core.os.name", "nt"):
                first.acquire()
                separate.acquire()
                with self.assertRaisesRegex(ConnectorError, "Another Profit Pilot"):
                    same.acquire()
                first.release()
                same.acquire()
                same.release()
                separate.release()

    def test_existing_worker_reconnects_after_a_connection_ends(self):
        class StopAfterReconnect(Exception):
            pass

        attempts = []

        async def interrupted_connection(*args):
            attempts.append(args)
            return False

        async def stop_after_second_attempt(delay):
            if len(attempts) >= 2:
                raise StopAfterReconnect()

        with patch.dict(
            os.environ,
            {
                "WORKER_IDENTITY_SERVER_URL": "ws://127.0.0.1:4000/api/worker-connect",
                "WORKER_ID": "connector-test",
                "WORKER_IDENTITY_TOKEN": "development-test-credential",
                "WORKER_RECONNECT_DELAY_SECONDS": "1",
                "WORKER_TEST_DISCONNECT_ONCE": "false",
            },
        ), patch.object(worker_identity, "run_connection", interrupted_connection), patch.object(
            worker_identity.asyncio, "sleep", stop_after_second_attempt
        ):
            with self.assertRaises(StopAfterReconnect):
                asyncio.run(worker_identity.run_worker())

        self.assertEqual(len(attempts), 2)


if __name__ == "__main__":
    unittest.main()
