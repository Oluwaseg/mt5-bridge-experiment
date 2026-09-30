import importlib.util
import os
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

    mt5 = load_mt5()
    if mt5 is None or not initialize_mt5(mt5):
        error = str(mt5.last_error()) if mt5 is not None else "MetaTrader5 package is unavailable"
        return jsonify({"error": "MT5 initialization failed", "details": error}), 503

    try:
        info = mt5.symbol_info(symbol)
        tick = mt5.symbol_info_tick(symbol)
        if info is None or tick is None:
            return jsonify({"error": "symbol or tick data unavailable", "symbol": symbol}), 404

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
            "comment": "phase-0-demo-order",
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
        return jsonify({
            "sent": result is not None and result.retcode == mt5.TRADE_RETCODE_DONE,
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


@app.get("/")
def root():
    return jsonify({
        "service": "mt5-bridge-experiment",
        "routes": ["/health", "/account", "/positions", "/market-data/<symbol>", "/demo-order-check", "/demo-order-send", "/demo-position-close"],
        "mode": "Phase 0 probe",
    })


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", "8000")))
