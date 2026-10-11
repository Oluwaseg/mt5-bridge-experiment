# MT5 Connector and developer worker

This folder contains the existing Windows-side MT5 bridge and authenticated remote worker, plus an early desktop connector prototype. It is not yet a signed installer or production customer release.

## Desktop connector prototype

The prototype UI starts with:

```powershell
py -3.11 .\connector_app.py
```

It accepts the temporary code from the authenticated website, exchanges it with `POST /api/mt5/connectors/exchange`, stores the returned device credential protected with the current Windows user's DPAPI, and starts the existing bridge and WebSocket worker. The local bridge is bound to loopback on an ephemeral port in connector mode. The worker still receives assigned account credentials from the backend and verifies the live MT5 account before the UI reports Connected.

The source-run prototype requires the Windows MT5 Python dependency. It does not need a customer `.env` file or an account-specific environment value. `PROFIT_PILOT_API_URL` is an optional developer override; the production API origin is currently in `bridge/connector_core.py`.

For the reproducible executable build procedure, see [WINDOWS_CONNECTOR_BUILD.md](WINDOWS_CONNECTOR_BUILD.md). PyInstaller output is currently unsigned and must not be represented as a released installer.

The existing environment-based worker instructions below are retained for developer use; the customer connector does not use or replace that setup.

## What actually starts here

The authenticated remote worker entry point is:

- `bridge/worker_identity.py`

It connects to the backend WebSocket endpoint:

- `ws://127.0.0.1:4000/api/worker-connect`

It does not start `bridge/probe.py` or `bridge/health.py` directly. Instead, it authenticates with a worker token, then sends commands like `get_account`, `get_positions`, `get_candles`, and `place_order` through the backend-managed worker connection. The actual trading-capable MT5 bridge is the existing probe service reached via `MT5_BRIDGE_URL`.

## Required prerequisites

- Windows 10 or 11
- Python 3.11.x
- A local MT5 terminal installation with a demo account that you intend to exercise
- The Node backend running with `REMOTE_WORKER_ENABLED=true`
- A user-linked MT5 account in the app, plus a generated worker token from the backend

## Local Windows setup

From a PowerShell terminal in this folder:

```powershell
py -3.11 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
python -m pip install -r .\requirements-mt5-windows.txt
Copy-Item .env.example .env
```

Then populate the local `.env` file with placeholders only:

```ini
PORT=8000
MT5_TERMINAL_PATH=C:\\path\\to\\terminal\\terminal.exe
MT5_AGENT_SECRET=
MT5_BRIDGE_URL=http://127.0.0.1:8000
WORKER_IDENTITY_SERVER_URL=ws://127.0.0.1:4000/api/worker-connect
WORKER_ID=windows-demo-worker
WORKER_IDENTITY_TOKEN=
WORKER_RECONNECT_DELAY_SECONDS=2
```

Do not commit any real `.env` file or credentials. Keep them local only.

The sample `ws://` and `http://` URLs are for loopback development only. Use `wss://` for a remote backend and `https://` for a remote bridge because the authenticated connection carries the selected account password in memory.

## MT5 terminal and broker setup

1. Install the MT5 terminal and set `MT5_TERMINAL_PATH` to its executable.
2. The selected customer account login, server, and password come from the backend's encrypted MT5 account record after worker-token authentication. Do not put customer-specific account fields in this `.env`.
3. The backend sends that account config over the authenticated worker WebSocket. The worker holds it in memory and forwards it on each loopback bridge request; the probe uses it only for that request and does not persist it.

The shared bridge secret and worker identity token remain local secrets and must not be committed.

## Backend configuration and pairing

The Node backend must be started with the remote worker enabled. Required config names are:

- `REMOTE_WORKER_ENABLED=true`
- `MT5_BRIDGE_URL` (the actual MT5 bridge HTTP URL)
- `MT5_BRIDGE_SECRET` (must match `MT5_AGENT_SECRET` in the worker environment)
- `MT5_WORKER_PYTHON` (optional override to the Python executable used to launch the local worker process)

To generate a secure per-account worker token, create the MT5 account in the app and then call the existing backend route:

- `POST /api/mt5/accounts/:accountId/worker-token`

That returns a one-time token to copy into `WORKER_IDENTITY_TOKEN` for the worker process.

Do not invent a pairing API; this flow is the implemented mechanism.

## Worker startup

From the `mt5-bridge-experiment` folder:

```powershell
.\.venv\Scripts\Activate.ps1
python .\bridge\worker_identity.py
```

Equivalent module start:

```powershell
.\.venv\Scripts\Activate.ps1
python -m bridge.worker_identity
```

./.venv/Scripts/python.exe ./bridge/probe.py

This is the authenticated remote MT5 worker. It listens to the backend worker identity socket and reconnects automatically on loss of connection.

## Connection verification

Before attempting any order activity, verify the backend-selected worker is online:

1. Start the Node backend with `REMOTE_WORKER_ENABLED=true`.
2. Confirm the MT5 account is attached to the correct user.
3. Confirm the worker is connected by checking the MT5 account worker status route.
4. Confirm the worker identity handshake is accepted by the backend.
5. Confirm the account and terminal match the bound account login/server.

The backend uses `findMt5WorkerIdentity` and `workerConnections` to bind the worker to a specific account. If no worker is connected for that account, routing fails with an explicit 404/503 error rather than silently falling back to a local in-process MT5 terminal.

## Troubleshooting

- `WORKER_IDENTITY_TOKEN` is missing or invalid: verify the token generated for the exact MT5 account.
- Worker says `Server did not acknowledge worker identity`: confirm the backend is running and the worker socket endpoint is reachable.
- `MT5 probe is unreachable`: confirm `MT5_BRIDGE_URL` and `MT5_AGENT_SECRET` match the bridge service.
- `Connected MT5 login does not match the bound account`: verify the demo account login and server match the user’s configured MT5 account.
- `MT5 terminal is not connected` or `tradeAllowed` is false: the terminal must be running and logged into the correct demo account.
- Repeated disconnects: the worker automatically reconnects, but the backend still requires a healthy, bound MT5 account.

## Shutdown

Stop the worker with Ctrl+C in the terminal. If the Node backend is also running locally, shut it down after the worker exits.

## Known blocker for the first remote demo test

A real remote MT5 demo test is blocked until all of the following are available and verified:

- a valid Windows MT5 terminal installation
- a demo account login/server that matches the bound user MT5 account
- a generated worker token for that exact account
- a reachable backend URL and matching bridge secret
- a running MT5 bridge service that reports the expected account identity

No real broker credentials or shared secrets should be stored in this repo.
