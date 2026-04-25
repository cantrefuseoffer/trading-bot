from decimal import Decimal, InvalidOperation, ROUND_DOWN, ROUND_HALF_UP
from flask import Flask, request, jsonify
from pybit.unified_trading import HTTP
from threading import Lock
import hashlib
import hmac
import logging
import os
import time


app = Flask(__name__)

logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s %(levelname)s %(message)s"
)
log = logging.getLogger(__name__)


def required_env(name):
    value = os.environ.get(name)
    if not value:
        raise RuntimeError(f"Missing required environment variable: {name}")
    return value


def env_bool(name, default):
    value = os.environ.get(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


API_KEY = required_env("BYBIT_API_KEY")
API_SECRET = required_env("BYBIT_SECRET_KEY")
WEBHOOK_SECRET = required_env("WEBHOOK_SECRET")

TESTNET = env_bool("BYBIT_TESTNET", True)
CATEGORY = os.environ.get("BYBIT_CATEGORY", "linear")
SYMBOL = os.environ.get("BYBIT_SYMBOL", "BTCUSDT").upper()

ORDER_QTY = Decimal(os.environ.get("ORDER_QTY", "0.01"))
TP_POINTS = Decimal(os.environ.get("TP_POINTS", "75"))
SL_POINTS = Decimal(os.environ.get("SL_POINTS", "50"))

POSITION_MODE = os.environ.get("POSITION_MODE", "one_way").lower()
ALLOWED_TIMEFRAMES = {
    item.strip()
    for item in os.environ.get("ALLOWED_TIMEFRAMES", "").split(",")
    if item.strip()
}

CLOSE_WAIT_SECONDS = float(os.environ.get("CLOSE_WAIT_SECONDS", "5"))
POLL_INTERVAL_SECONDS = float(os.environ.get("POLL_INTERVAL_SECONDS", "0.3"))

session = HTTP(
    testnet=TESTNET,
    api_key=API_KEY,
    api_secret=API_SECRET
)

trade_lock = Lock()
processed_alerts = set()
instrument_rules = None


class BybitError(Exception):
    pass


def bybit_call(method, **kwargs):
    response = method(**kwargs)

    if response.get("retCode") != 0:
        raise BybitError(
            f"{method.__name__} failed: "
            f"{response.get('retCode')} {response.get('retMsg')}"
        )

    return response


def decimal_from(value, field_name):
    try:
        return Decimal(str(value))
    except (InvalidOperation, TypeError):
        raise ValueError(f"Invalid decimal value for {field_name}: {value}")


def format_decimal(value):
    return format(value.normalize(), "f")


def round_price(value, tick_size):
    return (value / tick_size).to_integral_value(rounding=ROUND_HALF_UP) * tick_size


def round_qty(value, qty_step):
    return (value / qty_step).to_integral_value(rounding=ROUND_DOWN) * qty_step


def get_instrument_rules():
    global instrument_rules

    if instrument_rules is not None:
        return instrument_rules

    response = bybit_call(
        session.get_instruments_info,
        category=CATEGORY,
        symbol=SYMBOL
    )

    instruments = response["result"]["list"]
    if not instruments:
        raise BybitError(f"Instrument not found: {SYMBOL}")

    item = instruments[0]
    lot_filter = item["lotSizeFilter"]

    instrument_rules = {
        "tick_size": Decimal(item["priceFilter"]["tickSize"]),
        "qty_step": Decimal(lot_filter["qtyStep"]),
        "min_qty": Decimal(lot_filter["minOrderQty"]),
    }

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
    provided = request.headers.get("X-Webhook-Secret") or data.get("secret") or ""
    return hmac.compare_digest(str(provided), WEBHOOK_SECRET)


def clean_payload(data):
    safe = dict(data)
    if "secret" in safe:
        safe["secret"] = "***"
    return safe


def alert_id_from(data, signal):
    raw_id = (
        data.get("alert_id")
        or data.get("id")
        or data.get("bar_time")
        or data.get("time")
    )

    if not raw_id:
        return ""

    return f"{SYMBOL}:{signal}:{raw_id}"


def make_order_link_id(signal, alert_id):
    raw = f"{SYMBOL}:{signal}:{alert_id}:{time.time_ns()}"
    digest = hashlib.sha256(raw.encode()).hexdigest()[:16]
    return f"tv_{signal.lower()}_{digest}"[:36]


def position_idx_for_side(side):
    if POSITION_MODE == "hedge":
        return 1 if side == "Buy" else 2

    return 0


def get_last_price():
    response = bybit_call(
        session.get_tickers,
        category=CATEGORY,
        symbol=SYMBOL
    )

    return Decimal(response["result"]["list"][0]["lastPrice"])


def get_open_positions():
    response = bybit_call(
        session.get_positions,
        category=CATEGORY,
        symbol=SYMBOL
    )

    positions = []

    for position in response["result"]["list"]:
        size = Decimal(str(position.get("size", "0")))
        side = position.get("side")

        if size > 0 and side in {"Buy", "Sell"}:
            positions.append(position)

    return positions


def close_position(position):
    close_side = "Sell" if position["side"] == "Buy" else "Buy"

    log.info(
        "Closing %s position: symbol=%s size=%s",
        position["side"],
        SYMBOL,
        position["size"]
    )

    return bybit_call(
        session.place_order,
        category=CATEGORY,
        symbol=SYMBOL,
        side=close_side,
        orderType="Market",
        qty=str(position["size"]),
        reduceOnly=True,
        positionIdx=int(position.get("positionIdx", position_idx_for_side(position["side"])))
    )


def wait_until_side_closed(side):
    deadline = time.time() + CLOSE_WAIT_SECONDS

    while time.time() < deadline:
        open_positions = get_open_positions()
        still_open = any(position["side"] == side for position in open_positions)

        if not still_open:
            return True

        time.sleep(POLL_INTERVAL_SECONDS)

    return False


@app.route("/")
def home():
    return "Bybit bot is alive"


@app.route("/health")
def health():
    return jsonify({
        "status": "ok",
        "symbol": SYMBOL,
        "category": CATEGORY,
        "testnet": TESTNET,
        "position_mode": POSITION_MODE
    })


@app.route("/webhook", methods=["POST"])
def webhook():
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

    log.info("Webhook received: %s", clean_payload(data))

    with trade_lock:
        if alert_id and alert_id in processed_alerts:
            return jsonify({
                "status": "duplicate ignored",
                "alert_id": alert_id
            })

        if alert_id:
            processed_alerts.add(alert_id)

        try:
            rules = get_instrument_rules()

            qty = round_qty(ORDER_QTY, rules["qty_step"])
            if qty < rules["min_qty"]:
                raise ValueError(
                    f"ORDER_QTY {ORDER_QTY} is below minimum quantity {rules['min_qty']}"
                )

            open_positions = get_open_positions()

            same_side_positions = [
                position for position in open_positions
                if position["side"] == desired_side
            ]

            opposite_positions = [
                position for position in open_positions
                if position["side"] != desired_side
            ]

            if same_side_positions:
                return jsonify({
                    "status": "same position already open",
                    "signal": signal,
                    "symbol": SYMBOL,
                    "side": desired_side,
                    "size": same_side_positions[0]["size"]
                })

            for position in opposite_positions:
                close_position(position)

                if not wait_until_side_closed(position["side"]):
                    raise BybitError(
                        f"Timed out waiting for {position['side']} position to close"
                    )

            price = get_last_price()

            if signal == "LONG":
                tp_price = round_price(price + TP_POINTS, rules["tick_size"])
                sl_price = round_price(price - SL_POINTS, rules["tick_size"])
            else:
                tp_price = round_price(price - TP_POINTS, rules["tick_size"])
                sl_price = round_price(price + SL_POINTS, rules["tick_size"])

            order_link_id = make_order_link_id(signal, alert_id)

            order = bybit_call(
                session.place_order,
                category=CATEGORY,
                symbol=SYMBOL,
                side=desired_side,
                orderType="Market",
                qty=format_decimal(qty),
                takeProfit=format_decimal(tp_price),
                stopLoss=format_decimal(sl_price),
                tpTriggerBy="LastPrice",
                slTriggerBy="LastPrice",
                tpslMode="Full",
                positionIdx=position_idx_for_side(desired_side),
                orderLinkId=order_link_id
            )

            log.info(
                "Order placed: signal=%s symbol=%s side=%s qty=%s entry_approx=%s tp=%s sl=%s",
                signal,
                SYMBOL,
                desired_side,
                qty,
                price,
                tp_price,
                sl_price
            )

            return jsonify({
                "status": "ok",
                "signal": signal,
                "symbol": SYMBOL,
                "side": desired_side,
                "qty": format_decimal(qty),
                "entry_approx": format_decimal(price),
                "tp": format_decimal(tp_price),
                "sl": format_decimal(sl_price),
                "order_id": order["result"].get("orderId"),
                "order_link_id": order_link_id,
                "testnet": TESTNET
            })

        except Exception:
            if alert_id:
                processed_alerts.discard(alert_id)
            raise


@app.errorhandler(BybitError)
def handle_bybit_error(error):
    log.exception("Bybit error")
    return jsonify({
        "error": "bybit_error",
        "detail": str(error)
    }), 502


@app.errorhandler(Exception)
def handle_unexpected_error(error):
    log.exception("Unexpected error")
    return jsonify({
        "error": "internal_error",
        "detail": str(error)
    }), 500
