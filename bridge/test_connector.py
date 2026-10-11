import asyncio
import io
import json
import os
import tempfile
import threading
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
    exchange_pairing_code,
    validate_api_base_url,
    worker_websocket_url,
)
from bridge.secure_store import WindowsCredentialStore
from bridge import worker_identity


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
        self.record = record
        self.saved = []
        self.delete_calls = 0

    def load(self):
        return self.record

    def save(self, record):
        self.record = dict(record)
        self.saved.append(dict(record))

    def delete(self):
        self.delete_calls += 1
        self.record = None


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
            controller.pair(
                "a" * 32,
                terminal.name,
                "https://profit-pilot.example/api",
                lambda *event: status.append(event),
            )

        self.assertTrue(runner_factory_calls[0].started)
        self.assertEqual(store.record["credential"], "opaque-secret")
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
        controller.disconnect()
        self.assertFalse(controller.paired)
        self.assertIsNone(store.record)
        self.assertEqual(store.delete_calls, 1)

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
            controller.start(terminal.name, lambda *_status: None)
            runner = controller.runner
            runner.callback("Access revoked", "revoked remotely")
            controller.disconnect()

        self.assertTrue(runner.stopped)
        self.assertFalse(controller.paired)
        self.assertIsNone(store.record)
        self.assertEqual(store.delete_calls, 1)

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
            controller.start(terminal.name, lambda *_status: None)
            controller.start(terminal.name, lambda *_status: None)
            self.assertEqual(len(created), 1)
            self.assertEqual(created[0].start_calls, 1)
            self.assertTrue(controller.running)

            controller.stop(lambda *_status: None)
            controller.stop(lambda *_status: None)
            self.assertFalse(controller.running)
            self.assertTrue(created[0].stopped)
            controller.start(terminal.name, lambda *_status: None)
            self.assertEqual(len(created), 2)
            self.assertTrue(controller.running)

            controller.disconnect()
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
            controller.start(terminal.name, lambda *status: statuses.append(status))
            stale_callback = controller.runner.callback
            controller.stop(lambda *status: statuses.append(status))
            stale_callback("Access revoked", "late revocation")

        self.assertEqual(statuses[-1][0], "Disconnected")
        self.assertNotIn(("Access revoked", "late revocation"), statuses)

    def test_pairing_another_account_requires_forgetting_current_pairing(self):
        store = FakeCredentialStore({
            "connectorId": "device-id",
            "accountId": "account-id",
            "credential": "opaque-secret",
            "serverUrl": "wss://example/api/worker-connect",
            "terminalPath": "terminal.exe",
        })
        controller = ConnectorController(
            store,
            runner_factory=FakeRunner,
            exchange=lambda *_args: self.fail("must not exchange another code"),
        )
        with self.assertRaisesRegex(ConnectorError, "one MT5 account"):
            controller.pair("a" * 32, "terminal.exe", "https://example/api", lambda *_status: None)


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
