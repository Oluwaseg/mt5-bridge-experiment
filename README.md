# MT5 Bridge Experiment

Disposable Phase 0 experiment for testing whether a Northflank Linux container can run Wine, an MT5 terminal, and a small bridge process.

This is not connected to the main trading application. Do not use live credentials.

## Probe goals

1. Start a persistent container.
2. Verify Wine is available.
3. Verify the MT5 terminal executable is available.
4. Prove whether a compatible Windows Python runtime plus the `MetaTrader5` package can initialize against that terminal.
5. Later, with a demo account only, verify account info, market data, positions, and one controlled demo order.

## Current limitation

The Docker image does not contain a MetaTrader installer. MT5 installation/licensing and the broker demo account must be supplied separately. Set `MT5_TERMINAL_PATH` to the installed executable path when running the bridge.

The official `MetaTrader5` Python package is not treated as a normal Linux dependency. Its compatibility with Linux, Wine, and a Windows Python runtime must be proven separately before this service can execute orders.

## Local checks

```text
docker build -t mt5-bridge-experiment .
docker run --rm -p 8000:8000 mt5-bridge-experiment
```

The health endpoint is `GET /health`.

## Test another MT5 broker locally

Copy `.env.example` to `.env` and fill in the broker account credentials:

```text
MT5_LOGIN=1048288
MT5_PASSWORD=your-demo-password
MT5_SERVER=Capital.com-Demo
```

Keep `.env` local; it is ignored by Git. Restart the bridge after changing
the account, then verify `GET /health` reports the expected `server`,
`company`, and `accountLogin`. Do not place orders until those values match
the intended demo account.

If all three credential fields are empty, the bridge uses the account already
logged into the installed MT5 terminal.

## Northflank gate

Deploy this image as a private continuously running service. Do not add it to the production Node service yet. The experiment passes only when Wine, the terminal, and the Python package all remain healthy after restart.
