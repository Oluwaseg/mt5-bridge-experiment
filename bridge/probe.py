import importlib.util
import os
import re
from datetime import datetime, timedelta, timezone

from flask import Flask, jsonify, request
from dotenv import load_dotenv

load_dotenv()
app = Flask(__name__)


def require_bridge_secret():
    expected = os.getenv("MT5_AGENT_SECRET", "").strip()
    if expected and request.headers.get("X-MT5-Bridge-Secret") != expected:
        return jsonify({"error": "Invalid MT5 bridge secret"}), 401
    return None


def load_mt5():
    if importlib.util.find_spec("MetaTrader5") is None:
        return None
    import MetaTrader5 as mt5
    return mt5


def terminal_path():
    value = os.getenv("MT5_TERMINAL_PATH", "").strip()
    return value or None


def initialize_mt5(mt5):
    configured = {
        "login": os.getenv("MT5_LOGIN", "").strip(),
        "password": os.getenv("MT5_PASSWORD", "").strip(),
        "server": os.getenv("MT5_SERVER", "").strip(),
    }
    configured_values = [bool(value) for value in configured.values()]
    if any(configured_values) and not all(configured_values):
        return False

    initialize_kwargs = {}
    path = terminal_path()
    if path:
        initialize_kwargs["path"] = path
    if all(configured_values):
        initialize_kwargs.update(
            login=int(configured["login"]),
            password=configured["password"],
            server=configured["server"],
        )
    return mt5.initialize(**initialize_kwargs)


def protection_price(info, entry_price, side, volume, amount, is_take_profit):
    if amount <= 0:
        return 0.0
    tick_size = float(getattr(info, "trade_tick_size", 0) or 0)
    tick_value_key = "trade_tick_value_profit" if is_take_profit else "trade_tick_value_loss"
    tick_value = float(
        getattr(info, tick_value_key, 0)
        or getattr(info, "trade_tick_value", 0)
        or 0
    )
    if tick_size <= 0 or tick_value <= 0 or volume <= 0:
        raise ValueError("Symbol tick value is unavailable for risk conversion")

    distance = amount * tick_size / (tick_value * volume)
    moves_with_position = (side == "BUY") == is_take_profit
    sign = 1 if moves_with_position else -1
    price = entry_price + sign * distance
    rounded_to_tick = round(price / tick_size) * tick_size
    return round(rounded_to_tick, int(info.digits))


def aggregate_mt5_ticks(ticks, timeframe_seconds, maximum_candles):
    duration = int(timeframe_seconds)
    limit = max(2, int(maximum_candles))
    buckets = {}

    def value(tick, key, default=0):
        try:
            return tick[key]
        except (KeyError, IndexError, TypeError, ValueError):
            return default

    tick_rows = ticks if ticks is not None else ()
    ordered_ticks = sorted(
        tick_rows, key=lambda tick: int(value(tick, "time"))
    )

    for tick in ordered_ticks:
        timestamp = int(value(tick, "time"))
        price = float(value(tick, "last") or value(tick, "bid") or 0)
        if price <= 0:
            continue
        epoch = timestamp // duration * duration
        candle = buckets.get(epoch)
        if candle is None:
            buckets[epoch] = {
                "epoch": epoch,
                "open": price,
                "high": price,
                "low": price,
                "close": price,
                "volume": int(value(tick, "volume") or 0),
            }
        else:
            candle["high"] = max(candle["high"], price)
            candle["low"] = min(candle["low"], price)
            candle["close"] = price
            candle["volume"] += int(value(tick, "volume") or 0)

    populated = [buckets[epoch] for epoch in sorted(buckets)]
    candles = []
    previous = None
    for candle in populated:
        if previous is not None:
            gap = (candle["epoch"] - previous["epoch"]) // duration - 1
            if 0 < gap <= limit:
                for index in range(1, gap + 1):
                    epoch = previous["epoch"] + index * duration
                    candles.append({
                        "epoch": epoch,
                        "open": previous["close"],
                        "high": previous["close"],
                        "low": previous["close"],
                        "close": previous["close"],
                        "volume": 0,
                    })
        candles.append(candle)
        previous = candle

    return candles[-limit:]


def summarize_position_history(
    deals,
    ticket,
    is_open,
    entry_in=0,
    entry_out=1,
    entry_inout=2,
    entry_out_by=3,
    buy_type=0,
):
    matching = []
    for deal in deals or ():
        values = deal if isinstance(deal, dict) else deal._asdict()
        position_id = values.get("position_id", values.get("position"))
        if position_id is not None and int(position_id) != int(ticket):
            continue
        matching.append(values)

    close_entries = {entry_out, entry_inout, entry_out_by}
    closing = [deal for deal in matching if deal.get("entry") in close_entries]
    opening = [deal for deal in matching if deal.get("entry") == entry_in]
    closed = not is_open and bool(closing)

    def finite_value(deal, field):
        try:
            value = float(deal.get(field, 0) or 0)
            return value if value == value and abs(value) != float("inf") else 0.0
        except (TypeError, ValueError):
            return 0.0

    close_volume = sum(finite_value(deal, "volume") for deal in closing)
    close_price = (
        sum(
            finite_value(deal, "price") * finite_value(deal, "volume")
            for deal in closing
        )
        / close_volume
        if close_volume > 0
        else None
    )
    open_volume = sum(finite_value(deal, "volume") for deal in opening)
    buy_price = (
        sum(
            finite_value(deal, "price") * finite_value(deal, "volume")
            for deal in opening
        )
        / open_volume
        if open_volume > 0
        else None
    )
    last_close = max(
        closing,
        key=lambda deal: deal.get("time_msc", deal.get("time", 0)),
        default={},
    )
    direction = None
    if opening:
        direction = "BUY" if opening[0].get("type") == buy_type else "SELL"

    realized_profit = None
    if closed:
        realized_profit = sum(
            finite_value(deal, field)
            for deal in matching
            for field in ("profit", "commission", "swap", "fee")
        )

    return {
        "ticket": int(ticket),
        "is_open": bool(is_open),
        "closed": closed,
        "buy_price": buy_price,
        "close_price": close_price,
        "profit": realized_profit,
        "direction": direction,
        "size": close_volume or open_volume or None,
        "close_time": last_close.get("time", last_close.get("time_msc")),
        "close_reason": last_close.get("reason"),
        "deal_count": len(matching),
    }


def position_ticket_from_deal_history(mt5, result):
    deal_ticket = getattr(result, "deal", None)
    if not deal_ticket:
        return None
    try:
        deals = mt5.history_deals_get(ticket=int(deal_ticket)) or ()
    except (TypeError, ValueError):
        return None
    for deal in deals:
        values = deal if isinstance(deal, dict) else deal._asdict()
        position_id = values.get("position_id", values.get("position"))
        if position_id:
            return int(position_id)
    return None


def classify_execution_history(client_order_id, positions, orders, deals, mt5):
    def as_values(item):
        if isinstance(item, dict):
            return item
        if hasattr(item, "_asdict"):
            return item._asdict()
        return vars(item)

    matching_positions = [
        position
        for position in positions or ()
        if str(position.comment).strip() == client_order_id
    ]
    matching_orders = [
        order
        for order in orders or ()
        if str(order.comment).strip() == client_order_id
    ]
    matching_deals = [
        deal
        for deal in deals or ()
        if str(deal.comment).strip() == client_order_id
    ]

    if matching_positions:
        position = as_values(matching_positions[0])
        return {
            "status": "OPEN",
            "clientOrderId": client_order_id,
            "ticket": position.get("ticket"),
            "position": {
                key: position.get(key)
                for key in (
                    "ticket", "symbol", "type", "volume", "price_open",
                    "price_current", "profit", "sl", "tp", "time",
                )
            },
        }

    if matching_deals:
        position_ids = {
            int(deal.position_id)
            for deal in matching_deals
            if getattr(deal, "position_id", None)
        }
        for position_id in position_ids:
            live_position = next(
                (
                    position
                    for position in positions or ()
                    if int(getattr(position, "ticket", 0)) == position_id
                ),
                None,
            )
            if live_position is not None:
                live_values = live_position._asdict()
                return {
                    "status": "OPEN",
                    "clientOrderId": client_order_id,
                    "ticket": position_id,
                    "position": live_values,
                }
            position_deals = mt5.history_deals_get(position=position_id) or ()
            summary = summarize_position_history(
                position_deals,
                position_id,
                False,
                entry_in=mt5.DEAL_ENTRY_IN,
                entry_out=mt5.DEAL_ENTRY_OUT,
                entry_inout=mt5.DEAL_ENTRY_INOUT,
                entry_out_by=mt5.DEAL_ENTRY_OUT_BY,
                buy_type=mt5.DEAL_TYPE_BUY,
            )
            if summary["closed"]:
                return {
                    "status": "CLOSED",
                    "clientOrderId": client_order_id,
                    **summary,
                }

    rejected_states = {
        getattr(mt5, "ORDER_STATE_REJECTED", object()),
        getattr(mt5, "ORDER_STATE_CANCELED", object()),
        getattr(mt5, "ORDER_STATE_EXPIRED", object()),
    }
    if matching_orders and not matching_deals and all(
        order.state in rejected_states for order in matching_orders
    ):
        order = as_values(matching_orders[-1])
        return {
            "status": "FAILED",
            "clientOrderId": client_order_id,
            "order": order,
        }

    return {"status": "UNKNOWN", "clientOrderId": client_order_id}


@app.get("/health")
def health():
    mt5_path = terminal_path()
    mt5 = load_mt5()
    initialized = False
    initialize_error = None
    version = None
    account = None
    terminal = None

    if mt5 is not None:
        try:
            initialized = initialize_mt5(mt5)
            if initialized:
                version = mt5.version()
                account_info = mt5.account_info()
                account = account_info.login if account_info else None
                terminal_info = mt5.terminal_info()
                terminal = {
                    "connected": bool(terminal_info and terminal_info.connected),
                    "tradeAllowed": bool(terminal_info and terminal_info.trade_allowed),
                    "path": terminal_info.path if terminal_info else None,
                }
            else:
                initialize_error = str(mt5.last_error())
        except Exception as error:
            initialize_error = str(error)
        finally:
            if initialized:
                mt5.shutdown()

    details = {
        "service": "mt5-bridge-experiment",
        "status": "probe-only",
        "platform": os.name,
        "ready": initialized and bool(terminal and terminal["connected"]),
        "mt5Terminal": {
            "configured": bool(mt5_path),
            "path": mt5_path,
            "exists": bool(mt5_path and os.path.exists(mt5_path)),
        },
        "metaTraderPython": {
            "available": mt5 is not None,
            "package": "MetaTrader5",
            "version": version,
            "initializeError": initialize_error,
            "accountLogin": account,
            "server": getattr(account_info, "server", None) if mt5 is not None and initialized else None,
            "company": getattr(account_info, "company", None) if mt5 is not None and initialized else None,
            "currency": getattr(account_info, "currency", None) if mt5 is not None and initialized else None,
            "tradeMode": getattr(account_info, "trade_mode", None) if mt5 is not None and initialized else None,
        },
        "terminal": terminal,
    }
    return jsonify(details), 200 if details["ready"] else 503


@app.get("/account")
def account():
    unauthorized = require_bridge_secret()
    if unauthorized:
        return unauthorized

    mt5 = load_mt5()
    if mt5 is None or not initialize_mt5(mt5):
        error = str(mt5.last_error()) if mt5 is not None else "MetaTrader5 package is unavailable"
        return jsonify({"error": "MT5 initialization failed", "details": error}), 503

    try:
        account_info = mt5.account_info()
        terminal_info = mt5.terminal_info()
        if account_info is None:
            return jsonify({"error": "Account information unavailable"}), 503
        return jsonify({
            "account": account_info._asdict(),
            "terminal": {
                "connected": bool(terminal_info and terminal_info.connected),
                "tradeAllowed": bool(terminal_info and terminal_info.trade_allowed),
            },
        })
    finally:
        mt5.shutdown()


@app.get("/positions")
def positions():
    unauthorized = require_bridge_secret()
    if unauthorized:
        return unauthorized

    mt5 = load_mt5()
    if mt5 is None or not initialize_mt5(mt5):
        error = str(mt5.last_error()) if mt5 is not None else "MetaTrader5 package is unavailable"
        return jsonify({"error": "MT5 initialization failed", "details": error}), 503

    try:
        symbol = request.args.get("symbol")
        open_positions = mt5.positions_get(symbol=symbol) if symbol else mt5.positions_get()
        return jsonify({
            "symbol": symbol,
            "positions": [position._asdict() for position in (open_positions or ())],
        })
    finally:
        mt5.shutdown()


@app.get("/position-history/<int:ticket>")
def position_history(ticket):
    unauthorized = require_bridge_secret()
    if unauthorized:
        return unauthorized

    mt5 = load_mt5()
    if mt5 is None or not initialize_mt5(mt5):
        error = str(mt5.last_error()) if mt5 is not None else "MetaTrader5 package is unavailable"
        return jsonify({"error": "MT5 initialization failed", "details": error}), 503

    try:
        matches = mt5.positions_get(ticket=ticket) or ()
        deals = mt5.history_deals_get(position=ticket) or ()
        summary = summarize_position_history(
            deals,
            ticket,
            bool(matches),
            entry_in=mt5.DEAL_ENTRY_IN,
            entry_out=mt5.DEAL_ENTRY_OUT,
            entry_inout=mt5.DEAL_ENTRY_INOUT,
            entry_out_by=mt5.DEAL_ENTRY_OUT_BY,
            buy_type=mt5.DEAL_TYPE_BUY,
        )
        return jsonify(summary)
    finally:
        mt5.shutdown()


@app.get("/execution-status/<client_order_id>")
def execution_status(client_order_id):
    unauthorized = require_bridge_secret()
    if unauthorized:
        return unauthorized
    if not re.fullmatch(r"[A-Za-z0-9_-]{8,31}", client_order_id):
        return jsonify({"error": "invalid client order ID"}), 400

    mt5 = load_mt5()
    if mt5 is None or not initialize_mt5(mt5):
        error = str(mt5.last_error()) if mt5 is not None else "MetaTrader5 package is unavailable"
        return jsonify({"error": "MT5 initialization failed", "details": error}), 503

    try:
        now = datetime.now()
        since = now - timedelta(days=7)
        positions = mt5.positions_get() or ()
        orders = mt5.history_orders_get(since, now) or ()
        deals = mt5.history_deals_get(since, now) or ()
        return jsonify(
            classify_execution_history(
                client_order_id, positions, orders, deals, mt5
            )
        )
    finally:
        mt5.shutdown()


@app.get("/market-data/<symbol>")
def market_data(symbol):
    unauthorized = require_bridge_secret()
    if unauthorized:
        return unauthorized

    mt5 = load_mt5()
    if mt5 is None:
        return jsonify({"error": "MetaTrader5 package is unavailable"}), 503

    if not initialize_mt5(mt5):
        return jsonify({"error": "MT5 initialization failed", "details": str(mt5.last_error())}), 503

    try:
        if not mt5.symbol_select(symbol, True):
            return jsonify({"error": "Unable to select symbol", "symbol": symbol}), 404

        info = mt5.symbol_info(symbol)
        tick = mt5.symbol_info_tick(symbol)
        positions = mt5.positions_get(symbol=symbol) or ()
        if info is None or tick is None:
            return jsonify({"error": "Symbol data unavailable", "symbol": symbol}), 404

        return jsonify({
            "symbol": symbol,
            "info": info._asdict(),
            "tick": tick._asdict(),
            "positions": [position._asdict() for position in positions],
        })
    finally:
        mt5.shutdown()


@app.get("/candles/<symbol>")
def candles(symbol):
    unauthorized = require_bridge_secret()
    if unauthorized:
        return unauthorized

    mt5 = load_mt5()
    if mt5 is None or not initialize_mt5(mt5):
        error = str(mt5.last_error()) if mt5 is not None else "MetaTrader5 package is unavailable"
        return jsonify({"error": "MT5 initialization failed", "details": error}), 503

    timeframes = {
        "1m": mt5.TIMEFRAME_M1,
        "2m": mt5.TIMEFRAME_M2,
        "3m": mt5.TIMEFRAME_M3,
        "5m": mt5.TIMEFRAME_M5,
        "10m": mt5.TIMEFRAME_M10,
        "15m": mt5.TIMEFRAME_M15,
        "30m": mt5.TIMEFRAME_M30,
        "1h": mt5.TIMEFRAME_H1,
        "2h": mt5.TIMEFRAME_H2,
        "4h": mt5.TIMEFRAME_H4,
        "1d": mt5.TIMEFRAME_D1,
        "1w": mt5.TIMEFRAME_W1,
    }
    timeframe = request.args.get("timeframe", "1m")
    count = min(max(int(request.args.get("count", 300)), 2), 5000)
    try:
        seconds_match = re.fullmatch(r"(\d+)s", timeframe)
        if seconds_match:
            timeframe_seconds = int(seconds_match.group(1))
            if timeframe_seconds < 1 or timeframe_seconds >= 60:
                return jsonify({"error": "Second timeframe must be between 1s and 59s"}), 400
            if not mt5.symbol_select(symbol, True):
                return jsonify({"error": "Unsupported symbol", "symbol": symbol}), 400
            tick_count = min(
                max(count * max(1, timeframe_seconds), 1000), 50000
            )
            start_time = datetime.now(timezone.utc) - timedelta(
                seconds=timeframe_seconds * count * 3
            )
            ticks = mt5.copy_ticks_from(
                symbol, start_time, tick_count, mt5.COPY_TICKS_ALL
            )
            return jsonify({
                "symbol": symbol,
                "timeframe": timeframe,
                "candles": aggregate_mt5_ticks(
                    ticks, timeframe_seconds, count
                ),
            })

        if timeframe not in timeframes or not mt5.symbol_select(symbol, True):
            return jsonify({"error": "Unsupported timeframe or symbol", "symbol": symbol}), 400
        rates = mt5.copy_rates_from_pos(symbol, timeframes[timeframe], 0, count)
        return jsonify({
            "symbol": symbol,
            "timeframe": timeframe,
            "candles": [
                {"epoch": int(rate["time"]), "open": float(rate["open"]), "high": float(rate["high"]), "low": float(rate["low"]), "close": float(rate["close"]), "volume": int(rate["tick_volume"])}
                for rate in (rates if rates is not None else [])
            ],
        })
    finally:
        mt5.shutdown()


@app.post("/demo-order-check")
def demo_order_check():
    unauthorized = require_bridge_secret()
    if unauthorized:
        return unauthorized

    payload = request.get_json(silent=True) or {}
    symbol = str(payload.get("symbol", "EURUSD")).upper()
    side = str(payload.get("side", "BUY")).upper()
    volume = payload.get("volume", 0.01)

    if side not in {"BUY", "SELL"}:
        return jsonify({"error": "side must be BUY or SELL"}), 400

    try:
        volume = float(volume)
    except (TypeError, ValueError):
        return jsonify({"error": "volume must be numeric"}), 400

    if volume <= 0 or volume > 0.01:
        return jsonify({"error": "demo order check volume must be between 0 and 0.01"}), 400

    mt5 = load_mt5()
    if mt5 is None or not initialize_mt5(mt5):
        error = str(mt5.last_error()) if mt5 is not None else "MetaTrader5 package is unavailable"
        return jsonify({"error": "MT5 initialization failed", "details": error}), 503

    try:
        account = mt5.account_info()
        info = mt5.symbol_info(symbol)
        tick = mt5.symbol_info_tick(symbol)
        if account is None or info is None or tick is None:
            return jsonify({"error": "account, symbol, or tick data unavailable", "symbol": symbol}), 404

        order_type = mt5.ORDER_TYPE_BUY if side == "BUY" else mt5.ORDER_TYPE_SELL
        if info.filling_mode & 2:
            filling_type = mt5.ORDER_FILLING_IOC
        elif info.filling_mode & 1:
            filling_type = mt5.ORDER_FILLING_FOK
        else:
            filling_type = mt5.ORDER_FILLING_RETURN
        request_data = {
            "action": mt5.TRADE_ACTION_DEAL,
            "symbol": symbol,
            "volume": volume,
            "type": order_type,
            "price": tick.ask if side == "BUY" else tick.bid,
            "deviation": 20,
            "magic": 26092401,
            "comment": "phase-0-demo-order-check",
            "type_time": mt5.ORDER_TIME_GTC,
            "type_filling": filling_type,
        }
        result = mt5.order_check(request_data)
        return jsonify({
            "sent": False,
            "accountLogin": account.login,
            "request": request_data,
            "check": result._asdict() if result else None,
        })
    finally:
        mt5.shutdown()


@app.post("/demo-order-send")
def demo_order_send():
    unauthorized = require_bridge_secret()
    if unauthorized:
        return unauthorized

    payload = request.get_json(silent=True) or {}
    if payload.get("confirm") != "DEMO_ONLY":
        return jsonify({"error": "confirm must equal DEMO_ONLY"}), 400

    symbol = str(payload.get("symbol", "EURUSD")).upper()
    side = str(payload.get("side", "BUY")).upper()
    volume = payload.get("volume", 0.01)
    if side not in {"BUY", "SELL"}:
        return jsonify({"error": "side must be BUY or SELL"}), 400

    try:
        volume = float(volume)
    except (TypeError, ValueError):
        return jsonify({"error": "volume must be numeric"}), 400
    if volume <= 0 or volume > 0.01:
        return jsonify({"error": "demo order volume must be between 0 and 0.01"}), 400

    try:
        stop_loss_amount = float(payload.get("stopLoss", 0) or 0)
        take_profit_amount = float(payload.get("takeProfit", 0) or 0)
    except (TypeError, ValueError):
        return jsonify({"error": "stopLoss and takeProfit must be numeric"}), 400
    if stop_loss_amount < 0 or take_profit_amount < 0:
        return jsonify({"error": "stopLoss and takeProfit cannot be negative"}), 400

    mt5 = load_mt5()
    if mt5 is None or not initialize_mt5(mt5):
        error = str(mt5.last_error()) if mt5 is not None else "MetaTrader5 package is unavailable"
        return jsonify({"error": "MT5 initialization failed", "details": error}), 503

    try:
        account = mt5.account_info()
        if account is None or account.trade_mode != mt5.ACCOUNT_TRADE_MODE_DEMO:
            return jsonify({"error": "Order execution is demo-account only"}), 403
        info = mt5.symbol_info(symbol)
        tick = mt5.symbol_info_tick(symbol)
        if info is None or tick is None:
            return jsonify({"error": "symbol or tick data unavailable", "symbol": symbol}), 404

        entry_price = tick.ask if side == "BUY" else tick.bid
        try:
            stop_loss_price = protection_price(
                info, entry_price, side, volume, stop_loss_amount, False
            )
            take_profit_price = protection_price(
                info, entry_price, side, volume, take_profit_amount, True
            )
        except ValueError as error:
            return jsonify({"error": str(error)}), 422

        order_type = mt5.ORDER_TYPE_BUY if side == "BUY" else mt5.ORDER_TYPE_SELL
        if info.filling_mode & 2:
            filling_type = mt5.ORDER_FILLING_IOC
        elif info.filling_mode & 1:
            filling_type = mt5.ORDER_FILLING_FOK
        else:
            filling_type = mt5.ORDER_FILLING_RETURN
        request_data = {
            "action": mt5.TRADE_ACTION_DEAL,
            "symbol": symbol,
            "volume": volume,
            "type": order_type,
            "price": entry_price,
            "sl": stop_loss_price,
            "tp": take_profit_price,
            "deviation": 20,
            "magic": 26092401,
            "comment": str(payload.get("comment") or "phase-0-demo-order")[:31],
            "type_time": mt5.ORDER_TIME_GTC,
            "type_filling": filling_type,
        }
        check = mt5.order_check(request_data)
        if check is None or check.retcode != 0:
            return jsonify({
                "sent": False,
                "check": check._asdict() if check else None,
            }), 422

        result = mt5.order_send(request_data)
        sent = result is not None and result.retcode == mt5.TRADE_RETCODE_DONE
        position_ticket = (
            position_ticket_from_deal_history(mt5, result) if sent else None
        )
        return jsonify({
            "sent": sent,
            "position_ticket": position_ticket,
            "result": result._asdict() if result else None,
        })
    finally:
        mt5.shutdown()


@app.post("/demo-position-close")
def demo_position_close():
    unauthorized = require_bridge_secret()
    if unauthorized:
        return unauthorized

    payload = request.get_json(silent=True) or {}
    if payload.get("confirm") != "DEMO_ONLY":
        return jsonify({"error": "confirm must equal DEMO_ONLY"}), 400

    try:
        ticket = int(payload["ticket"])
    except (KeyError, TypeError, ValueError):
        return jsonify({"error": "ticket must be an integer"}), 400

    mt5 = load_mt5()
    if mt5 is None or not initialize_mt5(mt5):
        error = str(mt5.last_error()) if mt5 is not None else "MetaTrader5 package is unavailable"
        return jsonify({"error": "MT5 initialization failed", "details": error}), 503

    try:
        account = mt5.account_info()
        if account is None or account.trade_mode != mt5.ACCOUNT_TRADE_MODE_DEMO:
            return jsonify({"error": "Position protection updates are demo-account only"}), 403
        matches = mt5.positions_get(ticket=ticket)
        if not matches:
            return jsonify({"error": "position not found", "ticket": ticket}), 404

        position = matches[0]
        tick = mt5.symbol_info_tick(position.symbol)
        info = mt5.symbol_info(position.symbol)
        if tick is None or info is None:
            return jsonify({"error": "symbol or tick data unavailable", "symbol": position.symbol}), 404

        close_type = mt5.ORDER_TYPE_SELL if position.type == mt5.POSITION_TYPE_BUY else mt5.ORDER_TYPE_BUY
        if info.filling_mode & 2:
            filling_type = mt5.ORDER_FILLING_IOC
        elif info.filling_mode & 1:
            filling_type = mt5.ORDER_FILLING_FOK
        else:
            filling_type = mt5.ORDER_FILLING_RETURN
        request_data = {
            "action": mt5.TRADE_ACTION_DEAL,
            "symbol": position.symbol,
            "volume": position.volume,
            "type": close_type,
            "position": position.ticket,
            "price": tick.bid if position.type == mt5.POSITION_TYPE_BUY else tick.ask,
            "deviation": 20,
            "magic": 26092401,
            "comment": "phase-0-demo-close",
            "type_time": mt5.ORDER_TIME_GTC,
            "type_filling": filling_type,
        }
        check = mt5.order_check(request_data)
        if check is None or check.retcode != 0:
            return jsonify({
                "sent": False,
                "check": check._asdict() if check else None,
            }), 422

        result = mt5.order_send(request_data)
        payload = result._asdict() if result else None
        if payload is not None:
            payload["close_price"] = request_data["price"]
            payload["close_type"] = close_type
            payload["profit"] = None

            deal_id = payload.get("deal")
            order_id = payload.get("order")
            candidates = []
            for kwargs in (
                {"ticket": deal_id},
                {"position": position.ticket},
                {"ticket": position.ticket},
                {"symbol": position.symbol},
            ):
                try:
                    values = mt5.history_deals_get(**kwargs) or ()
                    if values:
                        candidates.extend(values)
                except Exception:
                    pass

            for deal in candidates:
                deal_dict = deal._asdict()
                deal_ticket = deal_dict.get("deal")
                pos_id = deal_dict.get("position")
                deal_order = deal_dict.get("order")
                if (
                    (deal_id is not None and deal_ticket == deal_id)
                    or (order_id is not None and deal_order == order_id)
                    or (pos_id == position.ticket)
                    or (deal_ticket == position.ticket)
                ):
                    payload["profit"] = float(deal_dict.get("profit", 0.0) or 0.0)
                    payload["deal_profit"] = payload["profit"]
                    payload["deal_entry"] = deal_dict
                    break

            if payload.get("profit") is None:
                payload["profit"] = 0.0

        return jsonify({
            "sent": result is not None and result.retcode == mt5.TRADE_RETCODE_DONE,
            "result": payload,
        })
    finally:
        mt5.shutdown()


@app.post("/position-protection")
def position_protection():
    unauthorized = require_bridge_secret()
    if unauthorized:
        return unauthorized

    payload = request.get_json(silent=True) or {}
    if payload.get("confirm") != "DEMO_ONLY":
        return jsonify({"error": "confirm must equal DEMO_ONLY"}), 400

    try:
        ticket = int(payload["ticket"])
    except (KeyError, TypeError, ValueError):
        return jsonify({"error": "ticket must be an integer"}), 400

    provided = [key for key in ("stopLoss", "takeProfit") if key in payload]
    if not provided:
        return jsonify({"error": "stopLoss or takeProfit is required"}), 400

    levels = {}
    for key in provided:
        try:
            value = float(payload[key])
        except (TypeError, ValueError):
            return jsonify({"error": f"{key} must be numeric"}), 400
        if value < 0:
            return jsonify({"error": f"{key} cannot be negative"}), 400
        levels[key] = value

    mt5 = load_mt5()
    if mt5 is None or not initialize_mt5(mt5):
        error = str(mt5.last_error()) if mt5 is not None else "MetaTrader5 package is unavailable"
        return jsonify({"error": "MT5 initialization failed", "details": error}), 503

    try:
        account = mt5.account_info()
        if account is None or account.trade_mode != mt5.ACCOUNT_TRADE_MODE_DEMO:
            return jsonify({"error": "Position protection updates are demo-account only"}), 403
        matches = mt5.positions_get(ticket=ticket)
        if not matches:
            return jsonify({"error": "position not found", "ticket": ticket}), 404

        position = matches[0]
        request_data = {
            "action": mt5.TRADE_ACTION_SLTP,
            "position": position.ticket,
            "symbol": position.symbol,
            "sl": levels.get("stopLoss", float(position.sl or 0)),
            "tp": levels.get("takeProfit", float(position.tp or 0)),
        }
        result = mt5.order_send(request_data)
        success = result is not None and result.retcode == mt5.TRADE_RETCODE_DONE
        return jsonify({
            "sent": success,
            "request": request_data,
            "result": result._asdict() if result else None,
        }), 200 if success else 422
    finally:
        mt5.shutdown()


@app.get("/")
def root():
    return jsonify({
        "service": "mt5-bridge-experiment",
        "routes": ["/health", "/account", "/positions", "/market-data/<symbol>", "/demo-order-check", "/demo-order-send", "/demo-position-close"],
        "mode": "Phase 0 probe",
    })


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", "8000")))
