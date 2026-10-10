import asyncio
import json
import os
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from dotenv import load_dotenv
from websockets.asyncio.client import connect

load_dotenv(Path(__file__).resolve().parents[1] / ".env")


def fetch_account_summary():
    bridge_url = os.getenv("MT5_BRIDGE_URL", "http://127.0.0.1:8000")
    secret = os.getenv("MT5_AGENT_SECRET", "")
    request = Request(
        f"{bridge_url.rstrip('/')}/account",
        headers={"X-MT5-Bridge-Secret": secret},
    )

    try:
        with urlopen(request, timeout=10) as response:
            account = json.loads(response.read()).get("account", {})
    except HTTPError as error:
        raise RuntimeError(f"MT5 probe returned HTTP {error.code}") from error
    except URLError as error:
        raise RuntimeError("MT5 probe is unreachable") from error

    return {
        key: account[key]
        for key in ("login", "server", "currency", "balance")
        if key in account
    }


async def run_connection(server_url, worker_name, force_disconnect):
    async with connect(server_url, open_timeout=10) as connection:
        await connection.send(
            json.dumps({"type": "hello", "workerName": worker_name})
        )
        async for message in connection:
            payload = json.loads(message)
            if payload.get("type") == "hello_ack":
                print(
                    "Server acknowledged connection "
                    f"#{payload.get('connectionNumber', '?')} for {worker_name}."
                )
                continue

            if (
                payload.get("type") != "command"
                or payload.get("command") != "get_account"
            ):
                raise RuntimeError("Server sent an unsupported command")

            try:
                account = await asyncio.to_thread(fetch_account_summary)
                result = {
                    "type": "result",
                    "requestId": payload.get("requestId"),
                    "ok": True,
                    "account": account,
                }
                print(
                    "MT5 account response received; returned fields: "
                    + ", ".join(account.keys())
                )
            except RuntimeError as error:
                result = {
                    "type": "result",
                    "requestId": payload.get("requestId"),
                    "ok": False,
                    "error": str(error),
                }
                print(f"MT5 account request failed: {error}")

            await connection.send(json.dumps(result))
            if force_disconnect:
                print("Forcing one local test disconnect.")
                return True

            print("Account result sent to Node; waiting for disconnect.")

    return False


async def run_worker():
    server_url = os.getenv(
        "WORKER_SERVER_URL", "ws://127.0.0.1:4000/api/worker-hello"
    )
    worker_name = os.getenv("WORKER_NAME", "local-python-worker")
    reconnect_delay = max(1, float(os.getenv("WORKER_RECONNECT_DELAY_SECONDS", "2")))
    force_disconnect_once = os.getenv("WORKER_TEST_DISCONNECT_ONCE") == "true"
    test_disconnect_done = False

    print(f"Connecting to {server_url}")
    while True:
        try:
            forced_disconnect = await run_connection(
                server_url,
                worker_name,
                force_disconnect_once and not test_disconnect_done,
            )
            if forced_disconnect:
                test_disconnect_done = True
        except Exception as error:
            print(
                "Worker connection ended "
                f"({type(error).__name__}); retrying."
            )

        print(f"Reconnecting in {reconnect_delay:g} seconds.")
        await asyncio.sleep(reconnect_delay)


if __name__ == "__main__":
    try:
        asyncio.run(run_worker())
    except KeyboardInterrupt:
        pass