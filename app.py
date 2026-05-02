from collections import OrderedDict
from decimal import Decimal, InvalidOperation, ROUND_DOWN, ROUND_HALF_UP
from flask import Flask, request, jsonify
from pybit.unified_trading import HTTP
from threading import Lock
import hashlib
import hmac
import json
import logging
import os
import time


TRUE_VALUES = {"1", "true", "yes", "on"}
FALSE_VALUES = {"0", "false", "no", "off"}
VALID_CATEGORIES = {"linear", "inverse"}
VALID_POSITION_MODES = {"one_way", "hedge"}
VALID_TRIGGER_BY = {"LastPrice", "MarkPrice", "IndexPrice"}

CONFIG_ERRORS = []


def read_bool(name, default=False):
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default

    value = raw.strip().lower()
    if value in TRUE_VALUES:
        return True
    if value in FALSE_VALUES:
        return False

    CONFIG_ERRORS.append(f"{name} must be one of: true, false, 1, 0, yes, no, on, off")
    return default


def read_decimal(name, default=None, gt=None, gte=None):
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        raw = default

    if raw is None:
        return None

    try:
        value = Decimal(str(raw).strip())
    except (InvalidOperation, ValueError):
        CONFIG_ERRORS.append(f"{name} must be a decimal number")
        return None

    if not value.is_finite():
        CONFIG_ERRORS.append(f"{name} must be a finite decimal number")
        return None

    if gt is not None and value <= Decimal(str(gt)):
        CONFIG_ERRORS.append(f"{name} must be greater than {gt}")
    if gte is not None and value < Decimal(str(gte)):
        CONFIG_ERRORS.append(f"{name} must be at least {gte}")

    return value


def read_float(name, default, gt=None, gte=None):
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        raw = default

    try:
        value = float(str(raw).strip())
    except ValueError:
        CONFIG_ERRORS.append(f"{name} must be a number")
        return float(default)

    if gt is not None and value <= gt:
        CONFIG_ERRORS.append(f"{name} must be greater than {gt}")
    if gte is not None and value < gte:
        CONFIG_ERRORS.append(f"{name} must be at least {gte}")

    return value


def read_int(name, default, gt=None, gte=None):
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        raw = default

    try:
        value = int(str(raw).strip())
    except ValueError:
        CONFIG_ERRORS.append(f"{name} must be an integer")
        return int(default)

    if gt is not None and value <= gt:
        CONFIG_ERRORS.append(f"{name} must be greater than {gt}")
    if gte is not None and value < gte:
        CONFIG_ERRORS.append(f"{name} must be at least {gte}")

    return value


def clean_env(name, default=""):
    raw = os.environ.get(name)
    if raw is None:
        raw = default
    return str(raw).strip()


API_KEY = clean_env("BYBIT_API_KEY")
API_SECRET = clean_env("BYBIT_SECRET_KEY") or clean_env("BYBIT_API_SECRET")
WEBHOOK_SECRET = clean_env("WEBHOOK_SECRET")

TESTNET = read_bool("BYBIT_TESTNET", False)
LIVE_TRADING_ENABLED = read_bool("LIVE_TRADING_ENABLED", False)
LIVE_SYMBOL_CONFIRMATION = clean_env("LIVE_SYMBOL_CONFIRMATION").upper()

CATEGORY = clean_env("BYBIT_CATEGORY", "linear").lower()
if CATEGORY not in VALID_CATEGORIES:
    CONFIG_ERRORS.append("BYBIT_CATEGORY must be 'linear' or 'inverse'")

SYMBOL = clean_env("BYBIT_SYMBOL", "BTCUSDT").upper()
if not SYMBOL:
    CONFIG_ERRORS.append("BYBIT_SYMBOL cannot be empty")

ORDER_QTY = read_decimal("ORDER_QTY", "0.001", gt=0)
TP_POINTS = read_decimal("TP_POINTS", "90", gt=0)
SL_POINTS = read_decimal("SL_POINTS", "40", gt=0)

POSITION_MODE = clean_env("POSITION_MODE", "one_way").lower().replace("-", "_")
if POSITION_MODE not in VALID_POSITION_MODES:
    CONFIG_ERRORS.append("POSITION_MODE must be 'one_way' or 'hedge'")

TPSL_TRIGGER_BY = clean_env("TPSL_TRIGGER_BY", "LastPrice")
if TPSL_TRIGGER_BY not in VALID_TRIGGER_BY:
    CONFIG_ERRORS.append("TPSL_TRIGGER_BY must be LastPrice, MarkPrice, or IndexPrice")

ALLOWED_TIMEFRAMES = {
    item.strip()
    for item in clean_env("ALLOWED_TIMEFRAMES").split(",")
    if item.strip()
}

REQUIRE_ALERT_ID = read_bool("REQUIRE_ALERT_ID", True)
RETURN_ERROR_DETAILS = read_bool("RETURN_ERROR_DETAILS", TESTNET)

CLOSE_WAIT_SECONDS = read_float("CLOSE_WAIT_SECONDS", "5", gt=0)
POLL_INTERVAL_SECONDS = read_float("POLL_INTERVAL_SECONDS", "0.3", gt=0)
INSTRUMENT_RULES_TTL_SECONDS = read_float("INSTRUMENT_RULES_TTL_SECONDS", "3600", gte=0)

PROCESSED_ALERTS_LIMIT = read_int("PROCESSED_ALERTS_LIMIT", "1000", gt=0)
PROCESSED_ALERTS_TTL_SECONDS = read_float("PROCESSED_ALERTS_TTL_SECONDS", "604800", gt=0)
PROCESSED_ALERTS_FILE = clean_env("PROCESSED_ALERTS_FILE", "processed_alerts.json")

MAX_CONTENT_LENGTH = read_int("MAX_CONTENT_LENGTH", "8192", gt=0)

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = MAX_CONTENT_LENGTH

logging.basicConfig(
    level=clean_env("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s %(levelname)s %(message)s"
)
log = logging.getLogger(__name__)

trade_lock = Lock()
processed_alerts = OrderedDict()

session = None
instrument_rules = None
instrument_rules_cached_at = 0


class BybitError(Exception):
    pass


def missing_config():
    missing = list(CONFIG_ERRORS)

    if not API_KEY:
        missing.append("BYBIT_API_KEY")
    if not API_SECRET:
        missing.append("BYBIT_SECRET_KEY")
    if not WEBHOOK_SECRET:
        missing.append("WEBHOOK_SECRET")

    if LIVE_TRADING_ENABLED and not TESTNET:
        if not clean_env("BYBIT_SYMBOL"):
            missing.append("BYBIT_SYMBOL")
        if not clean_env("ORDER_QTY"):
            missing.append("ORDER_QTY")
        if not clean_env("TP_POINTS"):
            missing.append("TP_POINTS")
        if not clean_env("SL_POINTS"):
            missing.append("SL_POINTS")
        if LIVE_SYMBOL_CONFIRMATION != SYMBOL:
            missing.append(f"LIVE_SYMBOL_CONFIRMATION must equal {SYMBOL}")

    return missing


def get_session():
    global session

    missing = missing_config()
    if missing:
        raise RuntimeError(f"Missing or invalid environment variables: {', '.join(missing)}")

    if session is None:
        session = HTTP(
            testnet=TESTNET,
            api_key=API_KEY,
            api_secret=API_SECRET
        )

    return session


def bybit_call(method, **kwargs):
    response = method(**kwargs)

    if not isinstance(response, dict):
        name = getattr(method, "__name__", "bybit_call")
        raise BybitError(f"{name} returned an unexpected response type")

    if response.get("retCode") not in {0, "0"}:
        name = getattr(method, "__name__", "bybit_call")
        raise BybitError(f"{name} failed: {response.get('retCode')} {response.get('retMsg')}")

    return response


def format_decimal(value):
    if value is None:
        return None
    return format(value.normalize(), "f")


def round_price(value, tick_size):
    return (value / tick_size).to_integral_value(rounding=ROUND_HALF_UP) * tick_size


def round_qty(value, qty_step):
    return (value / qty_step).to_integral_value(rounding=ROUND_DOWN) * qty_step


def decimal_field(data, key, default=None):
    raw = data.get(key)
    if raw is None or raw == "":
        if default is None:
            raise BybitError(f"Instrument field missing: {key}")
        raw = default

    try:
        return Decimal(str(raw))
    except InvalidOperation as exc:
        raise BybitError(f"Instrument field is not a decimal: {key}={raw}") from exc


def get_instrument_rules(force=False):
    global instrument_rules, instrument_rules_cached_at

    now = time.time()
    cache_is_fresh = (
        instrument_rules is not None
        and not force
        and INSTRUMENT_RULES_TTL_SECONDS > 0
        and now - instrument_rules_cached_at < INSTRUMENT_RULES_TTL_SECONDS
    )
    if cache_is_fresh:
        return instrument_rules

    s = get_session()

    response = bybit_call(
        s.get_instruments_info,
        category=CATEGORY,
        symbol=SYMBOL
    )

    instruments = response["result"]["list"]
    if not instruments:
        raise BybitError(f"Instrument not found: {SYMBOL}")

    item = instruments[0]
    if item.get("status") != "Trading":
        raise BybitError(f"Instrument is not trading: {SYMBOL} status={item.get('status')}")

    lot_filter = item["lotSizeFilter"]
    price_filter = item["priceFilter"]
    max_market_qty = decimal_field(
        lot_filter,
        "maxMktOrderQty",
        default=lot_filter.get("maxOrderQty", "0")
    )

    rules = {
        "tick_size": decimal_field(price_filter, "tickSize"),
        "min_price": decimal_field(price_filter, "minPrice", default="0"),
        "max_price": decimal_field(price_filter, "maxPrice", default="0"),
        "qty_step": decimal_field(lot_filter, "qtyStep"),
        "min_qty": decimal_field(lot_filter, "minOrderQty"),
        "max_market_qty": max_market_qty,
        "min_notional_value": decimal_field(lot_filter, "minNotionalValue", default="0"),
    }

    if rules["tick_size"] <= 0:
        raise BybitError("Instrument tickSize must be greater than 0")
    if rules["qty_step"] <= 0:
        raise BybitError("Instrument qtyStep must be greater than 0")

    instrument_rules = rules
    instrument_rules_cached_at = now
    return instrument_rules


def normalize_ticker(ticker):
    if not ticker:
        return ""

    ticker = str(ticker).upper().strip()

    if ":" in ticker:
        ticker = ticker.split(":", 1)[1]

    if ticker.endswith(".P"):
        ticker = ticker[:-2]

    if ticker.endswith("PERP"):
        ticker = ticker[:-4]

    return ticker


def verify_secret(data):
    expected = (WEBHOOK_SECRET or "").strip()
    if not expected:
        return False

    candidates = [
        request.headers.get("X-Webhook-Secret", ""),
        data.get("secret", "") if isinstance(data, dict) else "",
    ]

    authorization = request.headers.get("Authorization", "").strip()
    if authorization.lower().startswith("bearer "):
        candidates.append(authorization[7:])

    return any(
        hmac.compare_digest(str(candidate).strip(), expected)
        for candidate in candidates
        if candidate is not None
    )


def clean_payload(data):
    safe = dict(data)
    sensitive_keys = {"secret", "token", "api_key", "apikey", "api_secret", "apisecret"}

    for key in list(safe):
        if str(key).lower() in sensitive_keys:
            safe[key] = "***"

    return safe


def prune_processed_alerts(now=None):
    if now is None:
        now = time.time()

    while processed_alerts and now - next(iter(processed_alerts.values())) > PROCESSED_ALERTS_TTL_SECONDS:
        processed_alerts.popitem(last=False)

    while len(processed_alerts) > PROCESSED_ALERTS_LIMIT:
        processed_alerts.popitem(last=False)


def load_processed_alerts():
    if not PROCESSED_ALERTS_FILE or not os.path.exists(PROCESSED_ALERTS_FILE):
        return

    try:
        with open(PROCESSED_ALERTS_FILE, "r", encoding="utf-8") as handle:
            loaded = json.load(handle)
    except (OSError, json.JSONDecodeError):
        log.warning("Could not load processed alert state from %s", PROCESSED_ALERTS_FILE, exc_info=True)
        return

    if not isinstance(loaded, dict):
        log.warning("Ignoring processed alert state because it is not a JSON object")
        return

    for alert_id, timestamp in sorted(loaded.items(), key=lambda item: item[1]):
        try:
            processed_alerts[str(alert_id)] = float(timestamp)
        except (TypeError, ValueError):
            continue

    prune_processed_alerts()


def persist_processed_alerts():
    if not PROCESSED_ALERTS_FILE:
        return

    tmp_file = f"{PROCESSED_ALERTS_FILE}.tmp"
    try:
        with open(tmp_file, "w", encoding="utf-8") as handle:
            json.dump(processed_alerts, handle, separators=(",", ":"))
        os.replace(tmp_file, PROCESSED_ALERTS_FILE)
    except OSError:
        log.warning("Could not persist processed alert state to %s", PROCESSED_ALERTS_FILE, exc_info=True)


def remember_alert(alert_id):
    processed_alerts[alert_id] = time.time()
    prune_processed_alerts()
    persist_processed_alerts()


def forget_alert(alert_id):
    processed_alerts.pop(alert_id, None)
    persist_processed_alerts()


def alert_id_from(data, signal):
    raw_id = data.get("alert_id") or data.get("id") or data.get("bar_time") or data.get("time")

    if raw_id is None:
        return ""

    raw_id = str(raw_id).strip()
    if not raw_id:
        return ""

    timeframe = str(data.get("timeframe", "")).strip() or "na"
    return f"{SYMBOL}:{timeframe}:{signal}:{raw_id}"


def make_order_link_id(signal, alert_id):
    if alert_id:
        raw = f"{SYMBOL}:{signal}:{alert_id}"
    else:
        raw = f"{SYMBOL}:{signal}:{time.time_ns()}"

    digest = hashlib.sha256(raw.encode()).hexdigest()[:24]
    return f"tv_{signal.lower()}_{digest}"[:36]


def position_idx_for_side(side):
    if POSITION_MODE == "hedge":
        return 1 if side == "Buy" else 2

    return 0


def position_idx_from_position(position):
    try:
        return int(position.get("positionIdx"))
    except (TypeError, ValueError):
        return position_idx_for_side(position["side"])


def get_last_price():
    s = get_session()

    response = bybit_call(
        s.get_tickers,
        category=CATEGORY,
        symbol=SYMBOL
    )

    return Decimal(response["result"]["list"][0]["lastPrice"])


def get_open_positions():
    s = get_session()

    response = bybit_call(
        s.get_positions,
        category=CATEGORY,
        symbol=SYMBOL
    )

    positions = []

    for position in response["result"]["list"]:
        try:
            size = Decimal(str(position.get("size", "0")))
        except InvalidOperation:
            continue

        side = position.get("side")

        if size > 0 and side in {"Buy", "Sell"}:
            positions.append(position)

    return positions


def close_position(position):
    s = get_session()
    close_side = "Sell" if position["side"] == "Buy" else "Buy"

    log.info("Closing %s position: symbol=%s size=%s", position["side"], SYMBOL, position["size"])

    return bybit_call(
        s.place_order,
        category=CATEGORY,
        symbol=SYMBOL,
        side=close_side,
        orderType="Market",
        qty=str(position["size"]),
        reduceOnly=True,
        positionIdx=position_idx_from_position(position),
        orderLinkId=make_order_link_id("CLOSE", f"{position['side']}:{time.time_ns()}")
    )


def wait_until_side_closed(side):
    deadline = time.time() + CLOSE_WAIT_SECONDS

    while time.time() < deadline:
        still_open = any(position["side"] == side for position in get_open_positions())

        if not still_open:
            return True

        time.sleep(POLL_INTERVAL_SECONDS)

    return False


def validate_order_size(qty, price, rules):
    if qty <= 0:
        raise ValueError("ORDER_QTY rounds down to zero for this symbol's qtyStep")

    if qty < rules["min_qty"]:
        raise ValueError(
            f"ORDER_QTY {format_decimal(qty)} is below minimum {format_decimal(rules['min_qty'])}"
        )

    if rules["max_market_qty"] > 0 and qty > rules["max_market_qty"]:
        raise ValueError(
            f"ORDER_QTY {format_decimal(qty)} exceeds max market qty "
            f"{format_decimal(rules['max_market_qty'])}"
        )

    min_notional = rules["min_notional_value"]
    if CATEGORY == "linear" and min_notional > 0 and qty * price < min_notional:
        raise ValueError(
            f"ORDER_QTY notional {format_decimal(qty * price)} is below minimum "
            f"{format_decimal(min_notional)}"
        )


def validate_tpsl_prices(signal, price, tp_price, sl_price, rules):
    if signal == "LONG":
        if tp_price <= price:
            raise ValueError("LONG take profit must be above the current price")
        if sl_price >= price:
            raise ValueError("LONG stop loss must be below the current price")
    else:
        if tp_price >= price:
            raise ValueError("SHORT take profit must be below the current price")
        if sl_price <= price:
            raise ValueError("SHORT stop loss must be above the current price")

    min_price = rules["min_price"]
    max_price = rules["max_price"]
    for label, candidate in {"takeProfit": tp_price, "stopLoss": sl_price}.items():
        if candidate <= 0:
            raise ValueError(f"{label} must be greater than zero")
        if min_price > 0 and candidate < min_price:
            raise ValueError(f"{label} {format_decimal(candidate)} is below min price {format_decimal(min_price)}")
        if max_price > 0 and candidate > max_price:
            raise ValueError(f"{label} {format_decimal(candidate)} is above max price {format_decimal(max_price)}")


def calculate_order_plan(signal):
    rules = get_instrument_rules()
    price = get_last_price()
    qty = round_qty(ORDER_QTY, rules["qty_step"])

    validate_order_size(qty, price, rules)

    if signal == "LONG":
        tp_price = round_price(price + TP_POINTS, rules["tick_size"])
        sl_price = round_price(price - SL_POINTS, rules["tick_size"])
    else:
        tp_price = round_price(price - TP_POINTS, rules["tick_size"])
        sl_price = round_price(price + SL_POINTS, rules["tick_size"])

    validate_tpsl_prices(signal, price, tp_price, sl_price, rules)

    return {
        "rules": rules,
        "price": price,
        "qty": qty,
        "tp_price": tp_price,
        "sl_price": sl_price,
    }


@app.route("/")
def home():
    return "Bybit bot is alive"


@app.route("/health")
def health():
    missing = missing_config()

    return jsonify({
        "status": "ok" if not missing else "missing_config",
        "missing": missing,
        "symbol": SYMBOL,
        "category": CATEGORY,
        "testnet": TESTNET,
        "live_trading_enabled": LIVE_TRADING_ENABLED,
        "live_symbol_confirmation_ok": TESTNET or not LIVE_TRADING_ENABLED or LIVE_SYMBOL_CONFIRMATION == SYMBOL,
        "position_mode": POSITION_MODE,
        "order_qty": format_decimal(ORDER_QTY),
        "tp_points": format_decimal(TP_POINTS),
        "sl_points": format_decimal(SL_POINTS),
        "tpsl_trigger_by": TPSL_TRIGGER_BY,
        "require_alert_id": REQUIRE_ALERT_ID,
        "allowed_timeframes": sorted(ALLOWED_TIMEFRAMES),
    })


@app.route("/preflight", methods=["GET", "POST"])
def preflight():
    data = request.get_json(silent=True) if request.method == "POST" else {}
    if not isinstance(data, dict):
        data = {}

    missing = missing_config()
    if missing:
        return jsonify({
            "error": "missing_config",
            "missing": missing
        }), 500

    if not verify_secret(data):
        return jsonify({"error": "unauthorized"}), 401

    signal = str(data.get("signal", "LONG")).upper().strip()
    if signal not in {"LONG", "SHORT"}:
        return jsonify({"error": "wrong signal", "signal": signal}), 400

    plan = calculate_order_plan(signal)
    positions = get_open_positions()

    return jsonify({
        "status": "ok",
        "message": "Preflight passed. No order was placed.",
        "symbol": SYMBOL,
        "category": CATEGORY,
        "testnet": TESTNET,
        "live_trading_enabled": LIVE_TRADING_ENABLED,
        "signal": signal,
        "qty": format_decimal(plan["qty"]),
        "last_price": format_decimal(plan["price"]),
        "tp": format_decimal(plan["tp_price"]),
        "sl": format_decimal(plan["sl_price"]),
        "open_positions": [
            {
                "side": position.get("side"),
                "size": position.get("size"),
                "positionIdx": position.get("positionIdx"),
            }
            for position in positions
        ],
        "rules": {
            "tick_size": format_decimal(plan["rules"]["tick_size"]),
            "qty_step": format_decimal(plan["rules"]["qty_step"]),
            "min_qty": format_decimal(plan["rules"]["min_qty"]),
            "max_market_qty": format_decimal(plan["rules"]["max_market_qty"]),
            "min_notional_value": format_decimal(plan["rules"]["min_notional_value"]),
        }
    })


@app.route("/webhook", methods=["POST"])
def webhook():
    missing = missing_config()
    if missing:
        return jsonify({
            "error": "missing_config",
            "missing": missing
        }), 500

    if not TESTNET and not LIVE_TRADING_ENABLED:
        return jsonify({
            "error": "live_trading_disabled",
            "message": "Set LIVE_TRADING_ENABLED=true and LIVE_SYMBOL_CONFIRMATION to the exact symbol to allow real trading"
        }), 403

    data = request.get_json(silent=True)

    if not isinstance(data, dict):
        return jsonify({"error": "invalid or missing JSON"}), 400

    if not verify_secret(data):
        return jsonify({"error": "unauthorized"}), 401

    signal = str(data.get("signal", "")).upper().strip()
    ticker = normalize_ticker(data.get("ticker"))
    timeframe = str(data.get("timeframe", "")).strip()

    if signal not in {"LONG", "SHORT"}:
        return jsonify({"error": "wrong signal", "signal": signal}), 400

    if ticker and ticker != SYMBOL:
        return jsonify({
            "error": "ticker mismatch",
            "expected": SYMBOL,
            "got": ticker
        }), 400

    if ALLOWED_TIMEFRAMES and timeframe not in ALLOWED_TIMEFRAMES:
        return jsonify({
            "error": "timeframe not allowed",
            "allowed": sorted(ALLOWED_TIMEFRAMES),
            "got": timeframe
        }), 400

    desired_side = "Buy" if signal == "LONG" else "Sell"
    alert_id = alert_id_from(data, signal)

    if REQUIRE_ALERT_ID and not alert_id:
        return jsonify({
            "error": "missing_alert_id",
            "message": "Include alert_id, id, bar_time, or time in the webhook JSON for idempotency"
        }), 400

    order_link_id = make_order_link_id(signal, alert_id)

    log.info("Webhook received: %s", clean_payload(data))

    with trade_lock:
        if alert_id and alert_id in processed_alerts:
            return jsonify({
                "status": "duplicate ignored",
                "alert_id": alert_id,
                "order_link_id": order_link_id
            })

        if alert_id:
            remember_alert(alert_id)

        try:
            plan = calculate_order_plan(signal)

            open_positions = get_open_positions()

            opposite_positions = [
                position for position in open_positions
                if position["side"] != desired_side
            ]

            for position in opposite_positions:
                close_position(position)

                if not wait_until_side_closed(position["side"]):
                    raise BybitError(f"Timed out waiting for {position['side']} position to close")

            open_positions = get_open_positions()

            same_side_positions = [
                position for position in open_positions
                if position["side"] == desired_side
            ]

            if same_side_positions:
                return jsonify({
                    "status": "same position already open",
                    "signal": signal,
                    "symbol": SYMBOL,
                    "side": desired_side,
                    "size": same_side_positions[0]["size"],
                    "alert_id": alert_id,
                    "order_link_id": order_link_id
                })

            s = get_session()

            order = bybit_call(
                s.place_order,
                category=CATEGORY,
                symbol=SYMBOL,
                side=desired_side,
                orderType="Market",
                qty=format_decimal(plan["qty"]),
                takeProfit=format_decimal(plan["tp_price"]),
                stopLoss=format_decimal(plan["sl_price"]),
                tpTriggerBy=TPSL_TRIGGER_BY,
                slTriggerBy=TPSL_TRIGGER_BY,
                tpslMode="Full",
                tpOrderType="Market",
                slOrderType="Market",
                positionIdx=position_idx_for_side(desired_side),
                orderLinkId=order_link_id
            )

            log.info(
                "Order accepted: signal=%s symbol=%s side=%s qty=%s entry_approx=%s tp=%s sl=%s orderLinkId=%s",
                signal,
                SYMBOL,
                desired_side,
                plan["qty"],
                plan["price"],
                plan["tp_price"],
                plan["sl_price"],
                order_link_id
            )

            return jsonify({
                "status": "accepted",
                "message": "Bybit accepted the order request; final fill status is asynchronous",
                "signal": signal,
                "symbol": SYMBOL,
                "side": desired_side,
                "qty": format_decimal(plan["qty"]),
                "entry_approx": format_decimal(plan["price"]),
                "tp": format_decimal(plan["tp_price"]),
                "sl": format_decimal(plan["sl_price"]),
                "order_id": order["result"].get("orderId"),
                "order_link_id": order_link_id,
                "alert_id": alert_id,
                "testnet": TESTNET,
                "live_trading_enabled": LIVE_TRADING_ENABLED
            })

        except Exception:
            if alert_id:
                forget_alert(alert_id)
            raise


@app.errorhandler(BybitError)
def handle_bybit_error(error):
    log.exception("Bybit error")
    return jsonify({
        "error": "bybit_error",
        "detail": str(error) if RETURN_ERROR_DETAILS else "see server logs"
    }), 502


@app.errorhandler(Exception)
def handle_unexpected_error(error):
    log.exception("Unexpected error")
    return jsonify({
        "error": "internal_error",
        "detail": str(error) if RETURN_ERROR_DETAILS else "see server logs"
    }), 500


load_processed_alerts()
