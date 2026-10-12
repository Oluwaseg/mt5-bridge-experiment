# MT5 Windows Bridge and Connector

This folder contains two ways to connect a Windows MT5 terminal to the Profit Pilot server: the packaged desktop connector (`.exe`) and a manual Python worker setup for development and diagnostics. Both authenticate a worker to one account and verify the terminal's login and server.

> **Execution is demo-only.** The current MT5 worker and bridge reject live-account order execution. Orders are additionally restricted to approved test symbols and a maximum volume of `0.01`. A successful connector status is not authorization for live trading.

## Packaged Windows connector

The current workspace build is `dist\ProfitPilotConnector.exe`. Run it on the Windows PC that has the MT5 terminal installed. The `.exe` starts its local bridge and worker itself; do not also start `probe.py` or `worker_identity.py` for the same connector session.

1. In the Profit Pilot app, add the MT5 account and generate a pairing code for that account.
2. Open `ProfitPilotConnector.exe`, choose the matching MT5 terminal executable, and enter the pairing code before it expires.
3. Confirm the connector reports the expected account login and server.

Pairing exchanges the short-lived code for a device credential. The connector stores that credential encrypted with the current Windows user's DPAPI and retrieves the account credentials from the server over its authenticated WebSocket connection. It does not require a Python installation or a customer `.env` file.

The connector can retain multiple paired accounts, but a single MT5 installation cannot be used for concurrent sessions. Use separate MT5 installation directories for simultaneous logins, or disconnect one account before connecting another. Stopping or forgetting a connector does not log out of MT5 or close positions.

The executable is unsigned and is not a signed installer or production release. Build and smoke-test instructions are in [WINDOWS_CONNECTOR_BUILD.md](WINDOWS_CONNECTOR_BUILD.md). The desktop app source can be run with Python for development, but end users should use the packaged executable.

## Manual Python worker

Use this mode when developing or diagnosing the bridge. It requires Windows 10/11, Python 3.11, the Windows `MetaTrader5` dependency, a local MT5 terminal, and a Profit Pilot server with `REMOTE_WORKER_ENABLED=true`.

From PowerShell in this folder:

```powershell
py -3.11 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
python -m pip install -r .\requirements-mt5-windows.txt
Copy-Item .env.example .env
```

Set the local `.env` values. Keep secrets on the machine and out of source control:

```ini
PORT=8000
MT5_BRIDGE_HOST=127.0.0.1
MT5_TERMINAL_PATH=C:\\path\\to\\terminal64.exe
MT5_AGENT_SECRET=replace-with-a-local-secret
MT5_BRIDGE_URL=http://127.0.0.1:8000
WORKER_IDENTITY_SERVER_URL=ws://127.0.0.1:4000/api/worker-connect
WORKER_ID=windows-demo-worker
WORKER_IDENTITY_TOKEN=paste-the-account-worker-token
WORKER_RECONNECT_DELAY_SECONDS=2
```

In the Profit Pilot app, create the MT5 account with its login, password, and exact server name. The server stores the password encrypted. Generate a worker token using `POST /api/mt5/accounts/:accountId/worker-token` and copy the returned token into `WORKER_IDENTITY_TOKEN`; it is returned only when issued. Do not put the account password in this `.env`.

Open two PowerShell terminals in this folder. Start the local probe in the first:

```powershell
.\.venv\Scripts\Activate.ps1
python .\bridge\probe.py
```

Start the authenticated worker in the second:

```powershell
.\.venv\Scripts\Activate.ps1
python .\bridge\worker_identity.py
```

The worker connects to `/api/worker-connect`, receives the account configuration after token authentication, and forwards bridge requests to the local probe. It reconnects after network interruptions. `MT5_AGENT_SECRET` protects the local probe requests; if the server is also configured to launch its own local probe, set server `MT5_BRIDGE_SECRET` to the same value. Set `MT5_BRIDGE_HOST=127.0.0.1`: the probe otherwise binds to `0.0.0.0`. Do not expose Flask's development server directly to the internet; remote bridge access requires a properly secured HTTPS deployment.

For a remote server, use `wss://` for `WORKER_IDENTITY_SERVER_URL` and `https://` for any remote bridge URL. The worker rejects non-loopback `ws://` and `http://` URLs. Do not send account passwords over an unencrypted remote connection.

## Server-side setup

The server must have MongoDB configured, and `REMOTE_WORKER_ENABLED=true` to accept authenticated workers on `/api/worker-connect`. The manual worker uses an account worker token; the desktop connector instead obtains a device credential through a pairing code generated in the app. See [server API documentation](../server/api/README.md) for environment settings, pairing/device management, and the HTTP/WebSocket routes.

Connector pairing codes expire after five minutes and can only be exchanged once. The server can list, rotate, or revoke paired devices; revoking or rotating a credential disconnects that connector.

## Troubleshooting

- **Pairing code invalid or expired:** generate a new code in the app and exchange it promptly.
- **Worker identity rejected:** check that `REMOTE_WORKER_ENABLED=true`, the backend is reachable, and the token belongs to the intended account.
- **Probe unreachable or bridge secret rejected:** confirm `probe.py` is running and `MT5_AGENT_SECRET` matches the worker's local configuration.
- **Login or server mismatch:** check the selected terminal and the account record in Profit Pilot.
- **Terminal unavailable or trading not allowed:** open MT5, sign in to the bound demo account, and enable algorithmic trading if required by the terminal.
- **Connector cannot use terminal:** close the other connector session using that same terminal installation, or select a separate MT5 directory.

Never commit `.env` files, worker tokens, device credentials, or real broker credentials.
