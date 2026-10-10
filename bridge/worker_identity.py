import asyncio
from contextlib import suppress
from decimal import Decimal, InvalidOperation
import json
import math
import os
from pathlib import Path
import re
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode
from urllib.request import Request, urlopen

from dotenv import load_dotenv
from websockets.asyncio.client import connect

load_dotenv(Path(__file__).resolve().parents[1] / ".env")

APPROVED_TEST_SYMBOLS = {"EURUSD", "GBPUSD", "USDJPY", "XAUUSD"}


def validate_demo_order_payload(payload):
    if not isinstance(payload, dict):
        raise ValueError("order payload must be an object")
    if payload.get("confirm") != "DEMO_ONLY":
        raise ValueError("confirm must equal DEMO_ONLY")

    symbol = str(payload.get("symbol", "")).upper()
    if symbol not in APPROVED_TEST_SYMBOLS:
        raise ValueError("symbol is not approved for the demo test")

    side = str(payload.get("side", "")).upper()
    if side not in {"BUY", "SELL"}:
        raise ValueError("side must be BUY or SELL")

    try:
        volume = float(payload.get("volume", 0))
    except (TypeError, ValueError):
        raise ValueError("volume must be numeric") from None
    if not math.isfinite(volume) or volume <= 0 or volume > 0.01:
        raise ValueError("demo order volume must be between 0 and 0.01")

    try:
        stop_loss = float(payload.get("stopLoss", 0) or 0)
        take_profit = float(payload.get("takeProfit", 0) or 0)
    except (TypeError, ValueError):
        raise ValueError("stopLoss and takeProfit must be numeric") from None
    if (
        not math.isfinite(stop_loss)
        or not math.isfinite(take_profit)
        or stop_loss < 0
        or take_profit < 0
    ):
        raise ValueError("stopLoss and takeProfit cannot be negative")

    client_order_id = str(payload.get("clientOrderId") or "")
    if client_order_id and not re.fullmatch(r"mt5-[a-f0-9]{24}", client_order_id):
        raise ValueError("clientOrderId is invalid")

    return {
        "symbol": symbol,
        "side": side,
        "volume": volume,
        "stopLoss": stop_loss,
        "takeProfit": take_profit,
        "comment": client_order_id or payload.get("comment", "demo-worker-order"),
        "clientOrderId": client_order_id or None,
        "confirm": "DEMO_ONLY",
    }


def validate_bound_account(
    account_health, expected_login, expected_server, require_trade_allowed=True
):
    account = account_health.get("account") or {}
    terminal = account_health.get("terminal") or {}
    if str(account.get("login", "")) != str(expected_login):
        raise ValueError("Connected MT5 login does not match the bound account")
    if str(account.get("server", "")).strip().casefold() != str(
        expected_server
    ).strip().casefold():
        raise ValueError("Connected MT5 server does not match the bound account")
    if terminal.get("connected") is not True:
        raise ValueError("MT5 terminal is not connected")
    if require_trade_allowed and terminal.get("tradeAllowed") is not True:
        raise ValueError("MT5 terminal trading is not allowed")
    if account.get("trade_mode") != 0:
        raise ValueError("MT5 account is not in demo trade mode")
    return account


def validate_symbol_order_specs(order, symbol_info):
    if not isinstance(symbol_info, dict):
        raise ValueError("MT5 symbol specifications are unavailable")

    try:
        volume = Decimal(str(order["volume"]))
        minimum = Decimal(str(symbol_info["volume_min"]))
        maximum = Decimal(str(symbol_info["volume_max"]))
        step = Decimal(str(symbol_info["volume_step"]))
        trade_mode = int(symbol_info["trade_mode"])
    except (KeyError, InvalidOperation, TypeError, ValueError):
        raise ValueError("MT5 symbol volume or trading specifications are unavailable") from None

    if not all(value.is_finite() for value in (volume, minimum, maximum, step)):
        raise ValueError("MT5 symbol volume specifications are invalid")
    if minimum <= 0 or maximum < minimum or step <= 0:
        raise ValueError("MT5 symbol volume specifications are invalid")
    if volume < minimum or volume > maximum:
        raise ValueError(
            f"Volume {volume} is outside symbol limits {minimum}..{maximum}"
        )
    if (volume - minimum) % step != 0:
        raise ValueError(f"Volume {volume} does not match symbol volume step {step}")

    if trade_mode == 0 or trade_mode == 3:
        raise ValueError("MT5 symbol is disabled or close-only")
    if order["side"] == "BUY" and trade_mode == 2:
        raise ValueError("MT5 symbol does not allow opening BUY positions")
    if order["side"] == "SELL" and trade_mode == 1:
        raise ValueError("MT5 symbol does not allow opening SELL positions")
    if trade_mode not in {1, 2, 4}:
        raise ValueError("MT5 symbol trading mode is unsupported")


def validate_demo_position_payload(payload, protection=False):
    if not isinstance(payload, dict) or payload.get("confirm") != "DEMO_ONLY":
        raise ValueError("confirm must equal DEMO_ONLY")
    try:
        ticket = int(payload.get("ticket", 0))
    except (TypeError, ValueError):
        raise ValueError("ticket must be a positive integer") from None
    if ticket <= 0:
        raise ValueError("ticket must be a positive integer")

    result = {"ticket": ticket, "confirm": "DEMO_ONLY"}
    if protection:
        provided = [key for key in ("stopLoss", "takeProfit") if key in payload]
        if not provided:
            raise ValueError("stopLoss or takeProfit is required")
        for key in provided:
            try:
                value = float(payload[key])
            except (TypeError, ValueError):
                raise ValueError(f"{key} must be numeric") from None
            if not math.isfinite(value) or value < 0:
                raise ValueError(f"{key} must be a non-negative number")
            result[key] = value
    return result


def validate_position_history_ticket(ticket):
    try:
        position_ticket = int(ticket)
    except (TypeError, ValueError):
        raise ValueError("position ticket must be a positive integer") from None
    if position_ticket <= 0:
        raise ValueError("position ticket must be a positive integer")
    return position_ticket


def classify_mt5_failure(message="", retcode=None):
    retcode_categories = {
        10013: "VALIDATION_ERROR",
        10014: "VOLUME_ERROR",
        10016: "INVALID_STOPS",
        10017: "TRADE_DISABLED",
        10018: "MARKET_CLOSED",
        10030: "VALIDATION_ERROR",
        10031: "CONNECTION_ERROR",
    }
    try:
        category = retcode_categories.get(int(retcode))
    except (TypeError, ValueError):
        category = None
    if category:
        return category
    text = str(message or "").casefold()
    if any(value in text for value in ("volume", "lot size")):
        return "VOLUME_ERROR"
    if any(value in text for value in ("stop loss", "take profit", "stops")):
        return "INVALID_STOPS"
    if any(value in text for value in ("symbol", "instrument")):
        return "SYMBOL_ERROR"
    if any(value in text for value in ("trade disabled", "trading is not allowed", "disabled")):
        return "TRADE_DISABLED"
    if any(value in text for value in ("market closed", "market is closed")):
        return "MARKET_CLOSED"
    if any(value in text for value in ("connection", "terminal is not connected", "unreachable")):
        return "CONNECTION_ERROR"
    if any(value in text for value in ("confirm", "approved", "numeric", "required", "must be")):
        return "VALIDATION_ERROR"
    return "TRADE_REJECTED"


def normalize_order_response(response, request_id):
    if response.get("sent") is True:
        return {
            "type": "result",
            "requestId": request_id,
            "ok": True,
            "executionState": "SUCCESS",
            "requestSent": True,
            "safeToRetry": False,
            "order": response,
        }

    mt5_result = response.get("result") or response.get("check") or {}
    retcode = mt5_result.get("retcode")
    mt5_message = mt5_result.get("comment") or response.get("error") or ""
    http_status = response.get("http_status")
    if http_status is not None and int(http_status) >= 500:
        return {
            "type": "result",
            "requestId": request_id,
            "ok": False,
            "executionState": "UNKNOWN",
            "requestSent": True,
            "safeToRetry": False,
            "errorCategory": "UNKNOWN_EXECUTION",
            "error": str(
                response.get("error")
                or "MT5 probe failed after order submission started"
            ),
            "mt5HttpStatus": http_status,
            "order": response,
        }

    if http_status in {400, 401, 403}:
        category = "VALIDATION_ERROR"
    else:
        category = classify_mt5_failure(mt5_message, retcode)
    return {
        "type": "result",
        "requestId": request_id,
        "ok": False,
        "executionState": "FAILED",
        "requestSent": True,
        "safeToRetry": False,
        "errorCategory": category,
        "error": str(mt5_message or "MT5 rejected the order"),
        "retcode": retcode,
        "mt5Message": mt5_message or None,
        "order": response,
    }


def pre_submission_failure(message, request_id):
    return {
        "type": "result",
        "requestId": request_id,
        "ok": False,
        "executionState": "FAILED",
        "requestSent": False,
        "safeToRetry": True,
        "errorCategory": classify_mt5_failure(message),
        "error": str(message),
    }


def fetch_symbol_market_data(symbol):
    bridge_url = os.getenv("MT5_BRIDGE_URL", "http://127.0.0.1:8000")
    bridge_secret = os.getenv("MT5_AGENT_SECRET", "")
    request = Request(
        f"{bridge_url.rstrip('/')}/market-data/{symbol}",
        headers={"X-MT5-Bridge-Secret": bridge_secret},
    )
    try:
        with urlopen(request, timeout=10) as response:
            payload = json.loads(response.read())
    except HTTPError as error:
        response_body = error.read().decode("utf-8", errors="replace")
        try:
            error_payload = json.loads(response_body)
        except json.JSONDecodeError:
            error_payload = {"error": response_body}
        raise ValueError(
            error_payload.get("error") or f"MT5 symbol query returned HTTP {error.code}"
        ) from error
    except URLError as error:
        raise ValueError("MT5 probe is unreachable") from error

    return payload


def submit_demo_order(payload):
    bridge_url = os.getenv("MT5_BRIDGE_URL", "http://127.0.0.1:8000")
    bridge_secret = os.getenv("MT5_AGENT_SECRET", "")
    validated = validate_demo_order_payload(payload)
    request = Request(
        f"{bridge_url.rstrip('/')}/demo-order-send",
        data=json.dumps({**validated, "confirm": "DEMO_ONLY"}).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "X-MT5-Bridge-Secret": bridge_secret,
        },
        method="POST",
    )

    try:
        with urlopen(request, timeout=10) as response:
            return json.loads(response.read())
    except HTTPError as error:
        response_body = error.read().decode("utf-8", errors="replace")
        try:
            payload = json.loads(response_body)
        except json.JSONDecodeError:
            payload = {"error": response_body}
        payload["http_status"] = error.code
        return payload
    except URLError as error:
        raise RuntimeError("MT5 probe is unreachable") from error


async def send_heartbeats(connection):
    interval_ms = max(
        1000, int(os.getenv("REMOTE_WORKER_HEARTBEAT_INTERVAL_MS", "5000"))
    )
    while True:
        await asyncio.sleep(interval_ms / 1000)
        await connection.send(
            json.dumps({"type": "heartbeat", "at": asyncio.get_running_loop().time()})
        )


def fetch_account_summary():
    bridge_url = os.getenv("MT5_BRIDGE_URL", "http://127.0.0.1:8000")
    bridge_secret = os.getenv("MT5_AGENT_SECRET", "")
    request = Request(
        f"{bridge_url.rstrip('/')}/account",
        headers={"X-MT5-Bridge-Secret": bridge_secret},
    )

    try:
        with urlopen(request, timeout=10) as response:
            response_payload = json.loads(response.read())
            account = response_payload.get("account", {})
            terminal = response_payload.get("terminal", {})
    except HTTPError as error:
        raise RuntimeError(f"MT5 probe returned HTTP {error.code}") from error
    except URLError as error:
        raise RuntimeError("MT5 probe is unreachable") from error

    account_summary = {
        key: account[key]
        for key in (
            "login",
            "server",
            "currency",
            "balance",
            "equity",
            "trade_mode",
        )
        if key in account
    }
    return {
        "account": account_summary,
        "terminal": {
            key: terminal[key]
            for key in ("connected", "tradeAllowed")
            if key in terminal
        },
    }


def fetch_positions_summary(expected_login, expected_server):
    account_health = fetch_account_summary()
    validate_bound_account(
        account_health,
        expected_login,
        expected_server,
        require_trade_allowed=False,
    )
    bridge_url = os.getenv("MT5_BRIDGE_URL", "http://127.0.0.1:8000")
    bridge_secret = os.getenv("MT5_AGENT_SECRET", "")
    request = Request(
        f"{bridge_url.rstrip('/')}/positions",
        headers={"X-MT5-Bridge-Secret": bridge_secret},
    )

    try:
        with urlopen(request, timeout=10) as response:
            positions = json.loads(response.read()).get("positions", [])
    except HTTPError as error:
        raise RuntimeError(f"MT5 probe returned HTTP {error.code}") from error
    except URLError as error:
        raise RuntimeError("MT5 probe is unreachable") from error

    fields = (
        "ticket",
        "symbol",
        "type",
        "volume",
        "price_open",
        "price_current",
        "profit",
        "sl",
        "tp",
        "time",
    )
    return [
        {key: position[key] for key in fields if key in position}
        for position in positions
    ]


def fetch_candles(symbol, timeframe, count):
    bridge_url = os.getenv("MT5_BRIDGE_URL", "http://127.0.0.1:8000")
    bridge_secret = os.getenv("MT5_AGENT_SECRET", "")
    query = urlencode({"timeframe": timeframe, "count": int(count)})
    request = Request(
        f"{bridge_url.rstrip('/')}/candles/{quote(symbol, safe='')}?{query}",
        headers={"X-MT5-Bridge-Secret": bridge_secret},
    )
    try:
        with urlopen(request, timeout=10) as response:
            return json.loads(response.read())
    except HTTPError as error:
        response_body = error.read().decode("utf-8", errors="replace")
        try:
            error_payload = json.loads(response_body)
        except json.JSONDecodeError:
            error_payload = {"error": response_body}
        raise RuntimeError(
            error_payload.get("error") or f"MT5 probe returned HTTP {error.code}"
        ) from error
    except URLError as error:
        raise RuntimeError("MT5 probe is unreachable") from error


def fetch_position_history(ticket, expected_login, expected_server):
    position_ticket = validate_position_history_ticket(ticket)

    account_health = fetch_account_summary()
    validate_bound_account(
        account_health,
        expected_login,
        expected_server,
        require_trade_allowed=False,
    )
    bridge_url = os.getenv("MT5_BRIDGE_URL", "http://127.0.0.1:8000")
    bridge_secret = os.getenv("MT5_AGENT_SECRET", "")
    request = Request(
        f"{bridge_url.rstrip('/')}/position-history/{position_ticket}",
        headers={"X-MT5-Bridge-Secret": bridge_secret},
    )
    try:
        with urlopen(request, timeout=10) as response:
            return json.loads(response.read())
    except HTTPError as error:
        body = error.read().decode("utf-8", errors="replace")
        try:
            error_payload = json.loads(body)
        except json.JSONDecodeError:
            error_payload = {"error": body}
        raise RuntimeError(
            error_payload.get("error") or f"MT5 probe returned HTTP {error.code}"
        ) from error
    except URLError as error:
        raise RuntimeError("MT5 probe is unreachable") from error


def fetch_execution_status(client_order_id, expected_login, expected_server):
    if not re.fullmatch(r"mt5-[a-f0-9]{24}", str(client_order_id or "")):
        raise ValueError("clientOrderId is invalid")
    account_health = fetch_account_summary()
    validate_bound_account(
        account_health,
        expected_login,
        expected_server,
        require_trade_allowed=False,
    )
    bridge_url = os.getenv("MT5_BRIDGE_URL", "http://127.0.0.1:8000")
    bridge_secret = os.getenv("MT5_AGENT_SECRET", "")
    request = Request(
        f"{bridge_url.rstrip('/')}/execution-status/{client_order_id}",
        headers={"X-MT5-Bridge-Secret": bridge_secret},
    )
    try:
        with urlopen(request, timeout=10) as response:
            return json.loads(response.read())
    except HTTPError as error:
        body = error.read().decode("utf-8", errors="replace")
        try:
            error_payload = json.loads(body)
        except json.JSONDecodeError:
            error_payload = {"error": body}
        raise RuntimeError(
            error_payload.get("error") or f"MT5 probe returned HTTP {error.code}"
        ) from error
    except URLError as error:
        raise RuntimeError("MT5 probe is unreachable") from error


def close_demo_position(payload, expected_login, expected_server):
    validated = validate_demo_position_payload(payload)
    account_health = fetch_account_summary()
    validate_bound_account(account_health, expected_login, expected_server)
    bridge_url = os.getenv("MT5_BRIDGE_URL", "http://127.0.0.1:8000")
    bridge_secret = os.getenv("MT5_AGENT_SECRET", "")
    request = Request(
        f"{bridge_url.rstrip('/')}/demo-position-close",
        data=json.dumps(validated).encode("utf-8"),
        headers={"Content-Type": "application/json", "X-MT5-Bridge-Secret": bridge_secret},
        method="POST",
    )
    try:
        with urlopen(request, timeout=10) as response:
            return json.loads(response.read())
    except HTTPError as error:
        body = error.read().decode("utf-8", errors="replace")
        try:
            error_payload = json.loads(body)
        except json.JSONDecodeError:
            error_payload = {"error": body}
        raise RuntimeError(error_payload.get("error") or f"MT5 probe returned HTTP {error.code}") from error
    except URLError as error:
        raise RuntimeError("MT5 probe is unreachable") from error


def update_demo_position_protection(payload, expected_login, expected_server):
    validated = validate_demo_position_payload(payload, protection=True)
    account_health = fetch_account_summary()
    validate_bound_account(account_health, expected_login, expected_server)
    bridge_url = os.getenv("MT5_BRIDGE_URL", "http://127.0.0.1:8000")
    bridge_secret = os.getenv("MT5_AGENT_SECRET", "")
    request = Request(
        f"{bridge_url.rstrip('/')}/position-protection",
        data=json.dumps(validated).encode("utf-8"),
        headers={"Content-Type": "application/json", "X-MT5-Bridge-Secret": bridge_secret},
        method="POST",
    )
    try:
        with urlopen(request, timeout=10) as response:
            return json.loads(response.read())
    except HTTPError as error:
        body = error.read().decode("utf-8", errors="replace")
        try:
            error_payload = json.loads(body)
        except json.JSONDecodeError:
            error_payload = {"error": body}
        raise RuntimeError(error_payload.get("error") or f"MT5 probe returned HTTP {error.code}") from error
    except URLError as error:
        raise RuntimeError("MT5 probe is unreachable") from error


async def run_connection(
    server_url, worker_id, token, expected_login, expected_server, force_disconnect
):
    async with connect(server_url, open_timeout=10) as connection:
        await connection.send(
            json.dumps(
                {"type": "identify", "workerId": worker_id, "token": token}
            )
        )
        response = json.loads(await asyncio.wait_for(connection.recv(), timeout=10))
        if response.get("type") != "identity_ack":
            raise RuntimeError("Server did not acknowledge worker identity")

        print(
            "Identity accepted: "
            f"worker={response['workerId']} user={response['userId']} "
            f"account={response['accountId']}"
        )
        expected_login = response.get("login", expected_login)
        expected_server = response.get("server", expected_server)
        print("Waiting for server ping. Press Ctrl+C to stop.")
        heartbeat_task = asyncio.create_task(send_heartbeats(connection))
        try:
            async for message in connection:
                command = json.loads(message)
                if command.get("type") == "heartbeat_ack":
                    continue
                if command.get("type") != "command":
                    raise RuntimeError("Server sent an unsupported worker message")

                if command.get("command") == "ping":
                    await connection.send(
                        json.dumps(
                            {
                                "type": "pong",
                                "requestId": command.get("requestId"),
                                "status": "connected",
                            }
                        )
                    )
                    print("Server ping acknowledged.")
                    continue

                if command.get("command") == "place_order":
                    submission_started = False
                    try:
                        order_payload = validate_demo_order_payload(
                            command.get("payload") or {}
                        )
                        account_health = await asyncio.to_thread(
                            fetch_account_summary
                        )
                        validate_bound_account(
                            account_health, expected_login, expected_server
                        )
                        market_data = await asyncio.to_thread(
                            fetch_symbol_market_data, order_payload["symbol"]
                        )
                        validate_symbol_order_specs(
                            order_payload, market_data.get("info")
                        )
                        submission_started = True
                        response = await asyncio.to_thread(
                            submit_demo_order, order_payload
                        )
                        result = normalize_order_response(
                            response, command.get("requestId")
                        )
                        if result["ok"]:
                            print(
                                "Demo order placed; symbol="
                                f"{order_payload.get('symbol', '').upper()} "
                                f"volume={order_payload.get('volume')}"
                            )
                    except (RuntimeError, ValueError) as error:
                        unknown = submission_started
                        result = (
                            {
                                "type": "result",
                                "requestId": command.get("requestId"),
                                "ok": False,
                                "executionState": "UNKNOWN",
                                "requestSent": True,
                                "safeToRetry": False,
                                "errorCategory": "UNKNOWN_EXECUTION",
                                "error": str(error),
                            }
                            if unknown
                            else pre_submission_failure(
                                str(error), command.get("requestId")
                            )
                        )
                        print(f"Demo order request failed: {error}")
                    await connection.send(json.dumps(result))
                    continue

                if command.get("command") == "get_execution_status":
                    try:
                        status = await asyncio.to_thread(
                            fetch_execution_status,
                            (command.get("payload") or {}).get("clientOrderId"),
                            expected_login,
                            expected_server,
                        )
                        result = {
                            "type": "result",
                            "requestId": command.get("requestId"),
                            "ok": True,
                            "execution": status,
                        }
                    except (RuntimeError, ValueError) as error:
                        result = {
                            "type": "result",
                            "requestId": command.get("requestId"),
                            "ok": False,
                            "error": str(error),
                        }
                    await connection.send(json.dumps(result))
                    continue

                if command.get("command") == "get_candles":
                    payload = command.get("payload") or {}
                    try:
                        candles = await asyncio.to_thread(
                            fetch_candles,
                            str(payload.get("symbol", "")).upper(),
                            str(payload.get("timeframe", "1m")),
                            int(payload.get("count", 300)),
                        )
                        result = {
                            "type": "result",
                            "requestId": command.get("requestId"),
                            "ok": True,
                            **candles,
                        }
                    except (RuntimeError, ValueError) as error:
                        result = {"type": "result", "requestId": command.get("requestId"), "ok": False, "error": str(error)}
                    await connection.send(json.dumps(result))
                    continue

                if command.get("command") == "close_position":
                    try:
                        response = await asyncio.to_thread(
                            close_demo_position,
                            command.get("payload") or {},
                            expected_login,
                            expected_server,
                        )
                        result = {"type": "result", "requestId": command.get("requestId"), "ok": bool(response.get("sent")), "close": response}
                    except (RuntimeError, ValueError) as error:
                        result = {"type": "result", "requestId": command.get("requestId"), "ok": False, "error": str(error)}
                    await connection.send(json.dumps(result))
                    continue

                if command.get("command") == "update_position_protection":
                    try:
                        response = await asyncio.to_thread(
                            update_demo_position_protection,
                            command.get("payload") or {},
                            expected_login,
                            expected_server,
                        )
                        result = {"type": "result", "requestId": command.get("requestId"), "ok": bool(response.get("sent")), "protection": response}
                    except (RuntimeError, ValueError) as error:
                        result = {"type": "result", "requestId": command.get("requestId"), "ok": False, "error": str(error)}
                    await connection.send(json.dumps(result))
                    continue

                if command.get("command") == "get_positions":
                    try:
                        positions = await asyncio.to_thread(
                            fetch_positions_summary,
                            expected_login,
                            expected_server,
                        )
                        result = {
                            "type": "result",
                            "requestId": command.get("requestId"),
                            "ok": True,
                            "positions": positions,
                        }
                        print(
                            "Read-only positions result returned; "
                            f"count: {len(positions)}"
                        )
                    except (RuntimeError, ValueError) as error:
                        result = {
                            "type": "result",
                            "requestId": command.get("requestId"),
                            "ok": False,
                            "error": str(error),
                        }
                        print(f"Read-only positions request failed: {error}")
                    await connection.send(json.dumps(result))
                    if force_disconnect:
                        print("Forcing one local reconnect test disconnect.")
                        return True
                    continue

                if command.get("command") == "get_position_history":
                    try:
                        history = await asyncio.to_thread(
                            fetch_position_history,
                            (command.get("payload") or {}).get("ticket"),
                            expected_login,
                            expected_server,
                        )
                        result = {
                            "type": "result",
                            "requestId": command.get("requestId"),
                            "ok": True,
                            "history": history,
                        }
                    except (RuntimeError, ValueError) as error:
                        result = {
                            "type": "result",
                            "requestId": command.get("requestId"),
                            "ok": False,
                            "error": str(error),
                        }
                    await connection.send(json.dumps(result))
                    continue

                if command.get("command") != "get_account":
                    raise RuntimeError("Server requested an unsupported command")

                try:
                    health = await asyncio.to_thread(fetch_account_summary)
                    result = {
                        "type": "result",
                        "requestId": command.get("requestId"),
                        "ok": True,
                        **health,
                    }
                    print(
                        "Read-only account result returned; fields: "
                        + ", ".join(health["account"].keys())
                    )
                except RuntimeError as error:
                    result = {
                        "type": "result",
                        "requestId": command.get("requestId"),
                        "ok": False,
                        "error": str(error),
                    }
                    print(f"Read-only account request failed: {error}")

                await connection.send(json.dumps(result))
        finally:
            heartbeat_task.cancel()
            with suppress(asyncio.CancelledError):
                await heartbeat_task

    return False


async def run_worker():
    server_url = os.getenv(
        "WORKER_IDENTITY_SERVER_URL",
        "ws://127.0.0.1:4000/api/worker-connect",
    )
    worker_id = os.getenv("WORKER_ID", "local-dev-worker")
    token = os.getenv("WORKER_IDENTITY_TOKEN", "")
    if not token:
        raise RuntimeError("WORKER_IDENTITY_TOKEN is required")

    reconnect_delay = max(
        1, float(os.getenv("WORKER_RECONNECT_DELAY_SECONDS", "2"))
    )
    force_disconnect_once = os.getenv("WORKER_TEST_DISCONNECT_ONCE") == "true"
    test_disconnect_done = False

    print(f"Connecting worker {worker_id} to the identity endpoint.")
    while True:
        try:
            forced_disconnect = await run_connection(
                server_url,
                worker_id,
                token,
                os.getenv("MT5_LOGIN"),
                os.getenv("MT5_SERVER"),
                force_disconnect_once and not test_disconnect_done,
            )
            if forced_disconnect:
                test_disconnect_done = True
        except Exception as error:
            print(f"Worker connection ended ({type(error).__name__}); retrying.")

        print(f"Reconnecting in {reconnect_delay:g} seconds.")
        await asyncio.sleep(reconnect_delay)


if __name__ == "__main__":
    try:
        asyncio.run(run_worker())
    except KeyboardInterrupt:
        pass