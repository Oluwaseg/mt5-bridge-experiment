import asyncio
from contextlib import suppress
from decimal import Decimal, InvalidOperation
import ipaddress
import json
import math
import os
from pathlib import Path
import re
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode, urlsplit
from urllib.request import Request, urlopen

from dotenv import load_dotenv
from websockets.asyncio.client import connect
from websockets.exceptions import ConnectionClosed
try:
    from .diagnostics import log_diagnostic
except ImportError:
    try:
        from diagnostics import log_diagnostic
    except ImportError:
        from bridge.diagnostics import log_diagnostic

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


def validate_worker_account_config(identity_ack):
    account_config = identity_ack.get("accountConfig")
    expected_account_id = str(identity_ack.get("accountId", ""))
    if not expected_account_id:
        raise ValueError("Worker identity acknowledgment is missing account ID")
    if not isinstance(account_config, dict):
        raise ValueError("Backend identity acknowledgment is missing accountConfig")
    configured_account_id = str(account_config.get("accountId", ""))
    if not configured_account_id:
        raise ValueError("Backend accountConfig is missing accountId")
    if configured_account_id != expected_account_id:
        raise ValueError(
            "Backend accountConfig accountId does not match identity accountId"
        )
    if not str(account_config.get("login", "")).strip():
        raise ValueError("Worker account login is missing")
    if not str(account_config.get("server", "")).strip():
        raise ValueError("Worker account server is missing")
    if not isinstance(account_config.get("password"), str) or not account_config["password"]:
        raise ValueError("Worker account password is missing")
    return {
        "accountId": expected_account_id,
        "login": str(account_config["login"]).strip(),
        "server": str(account_config["server"]).strip(),
        "password": account_config["password"],
    }


def validate_worker_server_url(server_url):
    parsed = urlsplit(server_url)
    if not parsed.hostname or parsed.username or parsed.password:
        raise ValueError("Worker identity server URL is invalid")
    if parsed.scheme == "wss":
        return server_url
    try:
        loopback = ipaddress.ip_address(parsed.hostname).is_loopback
    except ValueError:
        loopback = parsed.hostname.casefold() == "localhost"
    if parsed.scheme != "ws" or not loopback:
        raise ValueError("Worker identity server must use WSS unless it is local")
    return server_url


def validate_bridge_url(bridge_url):
    parsed = urlsplit(bridge_url)
    if not parsed.hostname or parsed.username or parsed.password:
        raise ValueError("MT5 bridge URL is invalid")
    if parsed.scheme == "https":
        return bridge_url
    try:
        loopback = ipaddress.ip_address(parsed.hostname).is_loopback
    except ValueError:
        loopback = parsed.hostname.casefold() == "localhost"
    if parsed.scheme != "http" or not loopback:
        raise ValueError("MT5 bridge must use HTTPS unless it is local")
    return bridge_url


def bridge_headers(account_config, content_type=None):
    if not isinstance(account_config, dict):
        raise ValueError("Worker account configuration is unavailable")
    bridge_secret = os.getenv("MT5_AGENT_SECRET", "").strip()
    if not bridge_secret:
        raise RuntimeError("MT5_AGENT_SECRET is required for the local bridge")
    validate_bridge_url(os.getenv("MT5_BRIDGE_URL", "http://127.0.0.1:8000"))
    headers = {
        "X-MT5-Bridge-Secret": bridge_secret,
        "X-MT5-Account-Id": str(account_config["accountId"]),
        "X-MT5-Login": str(account_config["login"]),
        "X-MT5-Password": account_config["password"],
        "X-MT5-Server": str(account_config["server"]),
    }
    if content_type:
        headers["Content-Type"] = content_type
    return headers


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


def fetch_symbol_market_data(symbol, account_config):
    bridge_url = os.getenv("MT5_BRIDGE_URL", "http://127.0.0.1:8000")
    request = Request(
        f"{bridge_url.rstrip('/')}/market-data/{symbol}",
        headers=bridge_headers(account_config),
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


def submit_demo_order(payload, account_config):
    bridge_url = os.getenv("MT5_BRIDGE_URL", "http://127.0.0.1:8000")
    validated = validate_demo_order_payload(payload)
    request = Request(
        f"{bridge_url.rstrip('/')}/demo-order-send",
        data=json.dumps({**validated, "confirm": "DEMO_ONLY"}).encode("utf-8"),
        headers=bridge_headers(account_config, "application/json"),
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


def fetch_account_summary(account_config):
    bridge_url = os.getenv("MT5_BRIDGE_URL", "http://127.0.0.1:8000")
    request = Request(
        f"{bridge_url.rstrip('/')}/account",
        headers=bridge_headers(account_config),
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


def fetch_positions_summary(account_config):
    account_health = fetch_account_summary(account_config)
    validate_bound_account(
        account_health,
        account_config["login"],
        account_config["server"],
        require_trade_allowed=False,
    )
    bridge_url = os.getenv("MT5_BRIDGE_URL", "http://127.0.0.1:8000")
    request = Request(
        f"{bridge_url.rstrip('/')}/positions",
        headers=bridge_headers(account_config),
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


def fetch_candles(symbol, timeframe, count, account_config):
    bridge_url = os.getenv("MT5_BRIDGE_URL", "http://127.0.0.1:8000")
    query = urlencode({"timeframe": timeframe, "count": int(count)})
    request = Request(
        f"{bridge_url.rstrip('/')}/candles/{quote(symbol, safe='')}?{query}",
        headers=bridge_headers(account_config),
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


def fetch_position_history(ticket, account_config):
    position_ticket = validate_position_history_ticket(ticket)

    account_health = fetch_account_summary(account_config)
    validate_bound_account(
        account_health,
        account_config["login"],
        account_config["server"],
        require_trade_allowed=False,
    )
    bridge_url = os.getenv("MT5_BRIDGE_URL", "http://127.0.0.1:8000")
    request = Request(
        f"{bridge_url.rstrip('/')}/position-history/{position_ticket}",
        headers=bridge_headers(account_config),
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


def fetch_execution_status(client_order_id, account_config):
    if not re.fullmatch(r"mt5-[a-f0-9]{24}", str(client_order_id or "")):
        raise ValueError("clientOrderId is invalid")
    account_health = fetch_account_summary(account_config)
    validate_bound_account(
        account_health,
        account_config["login"],
        account_config["server"],
        require_trade_allowed=False,
    )
    bridge_url = os.getenv("MT5_BRIDGE_URL", "http://127.0.0.1:8000")
    request = Request(
        f"{bridge_url.rstrip('/')}/execution-status/{client_order_id}",
        headers=bridge_headers(account_config),
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


def close_demo_position(payload, account_config):
    validated = validate_demo_position_payload(payload)
    account_health = fetch_account_summary(account_config)
    validate_bound_account(
        account_health, account_config["login"], account_config["server"]
    )
    bridge_url = os.getenv("MT5_BRIDGE_URL", "http://127.0.0.1:8000")
    request = Request(
        f"{bridge_url.rstrip('/')}/demo-position-close",
        data=json.dumps(validated).encode("utf-8"),
        headers=bridge_headers(account_config, "application/json"),
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


def update_demo_position_protection(payload, account_config):
    validated = validate_demo_position_payload(payload, protection=True)
    account_health = fetch_account_summary(account_config)
    validate_bound_account(
        account_health, account_config["login"], account_config["server"]
    )
    bridge_url = os.getenv("MT5_BRIDGE_URL", "http://127.0.0.1:8000")
    request = Request(
        f"{bridge_url.rstrip('/')}/position-protection",
        data=json.dumps(validated).encode("utf-8"),
        headers=bridge_headers(account_config, "application/json"),
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

        account_config = validate_worker_account_config(response)
        log_diagnostic(
            "worker_identity_acknowledged",
            response.get("accountId"),
            worker_id=response.get("workerId"),
            account_id=response.get("accountId"),
            configured_login_suffix=account_config["login"][-4:],
            configured_server=account_config["server"],
        )
        print(
            "Identity accepted: "
            f"worker={response['workerId']} user={response['userId']} "
            f"account={response['accountId']}"
        )
        expected_login = account_config["login"]
        expected_server = account_config["server"]
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
                            fetch_account_summary, account_config
                        )
                        validate_bound_account(
                            account_health, expected_login, expected_server
                        )
                        market_data = await asyncio.to_thread(
                            fetch_symbol_market_data,
                            order_payload["symbol"],
                            account_config,
                        )
                        validate_symbol_order_specs(
                            order_payload, market_data.get("info")
                        )
                        submission_started = True
                        response = await asyncio.to_thread(
                            submit_demo_order, order_payload, account_config
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
                            account_config,
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
                            str(payload.get("symbol", "")),
                            str(payload.get("timeframe", "1m")),
                            int(payload.get("count", 300)),
                            account_config,
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
                            account_config,
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
                            account_config,
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
                            account_config,
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
                            account_config,
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

                log_diagnostic(
                    "worker_account_check_requested",
                    account_config.get("accountId"),
                    request_id=command.get("requestId"),
                )
                try:
                    health = await asyncio.to_thread(
                        fetch_account_summary, account_config
                    )
                    validate_bound_account(
                        health,
                        expected_login,
                        expected_server,
                        require_trade_allowed=False,
                    )
                    login = str(health["account"].get("login", ""))
                    server = str(health["account"].get("server", ""))
                    log_diagnostic(
                        "worker_account_verification_succeeded",
                        account_config.get("accountId"),
                        login_suffix=login[-4:],
                        server=server,
                        trade_allowed=(health.get("terminal") or {}).get("tradeAllowed"),
                    )
                    result = {
                        "type": "result",
                        "requestId": command.get("requestId"),
                        "ok": True,
                        **health,
                    }
                    print(
                        "Verified MT5 account: "
                        f"login ending {login[-4:]}; "
                        f"server={health['account'].get('server', '')}"
                    )
                    trade_allowed = (health.get("terminal") or {}).get("tradeAllowed")
                    trade_allowed_label = (
                        "Yes" if trade_allowed is True
                        else "No" if trade_allowed is False
                        else "Unknown"
                    )
                    print(f"MT5 trade allowed: {trade_allowed_label}")
                    print(
                        "Read-only account result returned; fields: "
                        + ", ".join(health["account"].keys())
                    )
                except (RuntimeError, ValueError) as error:
                    log_diagnostic(
                        "worker_account_verification_failed",
                        account_config.get("accountId"),
                        reason=str(error),
                    )
                    result = {
                        "type": "result",
                        "requestId": command.get("requestId"),
                        "ok": False,
                        "error": str(error),
                    }
                    print(f"Read-only account request failed: {error}")

                await connection.send(json.dumps(result))
                log_diagnostic(
                    "worker_account_result_emitted",
                    account_config.get("accountId"),
                    ok=result.get("ok") is True,
                    request_id=result.get("requestId"),
                )
        finally:
            heartbeat_task.cancel()
            with suppress(asyncio.CancelledError):
                await heartbeat_task

    return False


def terminal_worker_exit_code(close_reason):
    return {
        "Connector credential revoked": 20,
        "Connector revoked": 20,
        "Connector credential rotated": 21,
        "Worker identity rejected": 22,
        "Worker account configuration unavailable": 22,
    }.get(close_reason)


async def run_worker():
    server_url = os.getenv(
        "WORKER_IDENTITY_SERVER_URL",
        "ws://127.0.0.1:4000/api/worker-connect",
    )
    validate_worker_server_url(server_url)
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
        except ValueError as error:
            print(f"Worker connection ended (ValueError: {error}); retrying.")
        except ConnectionClosed as error:
            close_frame = error.rcvd or error.sent
            close_code = close_frame.code if close_frame else 1006
            close_reason = close_frame.reason if close_frame else ""
            terminal_code = terminal_worker_exit_code(close_reason)
            if terminal_code == 20:
                print("Access revoked by Profit Pilot. Pair this connector again.")
                return terminal_code
            if terminal_code == 21:
                print("Connector credential changed. Pair this connector again.")
                return terminal_code
            if terminal_code == 22:
                print("Profit Pilot rejected this connector credential or account assignment.")
                return terminal_code
            safe_reasons = {
                "Worker identity rejected",
                "Worker account configuration unavailable",
                "Worker already connected for this account",
                "Connector credential revoked",
                "Connector revoked",
                "Connector credential rotated",
            }
            reason = close_reason if close_reason in safe_reasons else "connection closed"
            print(
                f"Worker connection ended (WebSocket {close_code}: {reason}); retrying."
            )
        except Exception as error:
            print(f"Worker connection ended ({type(error).__name__}); retrying.")

        print(f"Reconnecting in {reconnect_delay:g} seconds.")
        await asyncio.sleep(reconnect_delay)


if __name__ == "__main__":
    try:
        raise SystemExit(asyncio.run(run_worker()))
    except KeyboardInterrupt:
        pass