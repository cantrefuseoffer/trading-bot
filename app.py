import os
from typing import Optional, Dict, Any

from flask import Flask, request, jsonify
from pybit.unified_trading import HTTP

app = Flask(__name__)

API_KEY = os.environ.get("BYBIT_API_KEY")
API_SECRET = os.environ.get("BYBIT_SECRET_KEY")
SYMBOL = os.environ.get("BYBIT_SYMBOL", "BTCUSDT")
QTY = float(os.environ.get("BYBIT_QTY", "0.001"))
TP_POINTS = float(os.environ.get("TP_POINTS", "75"))
SL_POINTS = float(os.environ.get("SL_POINTS", "50"))
TESTNET = os.environ.get("BYBIT_TESTNET", "true").lower() == "true"

if not API_KEY or not API_SECRET:
    app.logger.warning("Bybit API credentials are missing. Trading requests will fail until they are set.")

session = HTTP(
    testnet=TESTNET,
    api_key=API_KEY,
    api_secret=API_SECRET,
)

last_signal: Optional[str] = None


@app.route("/")
def home() -> str:
    return "Bybit bot is alive 🚀"


@app.route("/health")
def health() -> Dict[str, str]:
    return {"status": "ok"}


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def get_position() -> Optional[Dict[str, Any]]:
    positions = session.get_positions(category="linear", symbol=SYMBOL)
    for position in positions.get("result", {}).get("list", []):
        if _safe_float(position.get("size")) > 0:
            return position
    return None


def get_last_price() -> float:
    ticker = session.get_tickers(category="linear", symbol=SYMBOL)
    ticker_rows = ticker.get("result", {}).get("list", [])
    if not ticker_rows:
        raise RuntimeError(f"No ticker data returned for {SYMBOL}")
    return _safe_float(ticker_rows[0].get("lastPrice"))


def close_existing_position(position: Dict[str, Any]) -> None:
    app.logger.info("Closing existing position")
    close_side = "Sell" if position.get("side") == "Buy" else "Buy"
    session.place_order(
        category="linear",
        symbol=SYMBOL,
        side=close_side,
        orderType="Market",
        qty=position.get("size"),
        reduceOnly=True,
    )


def place_market_order(signal: str, price: float) -> None:
    side = "Buy" if signal == "LONG" else "Sell"
    tp_price = price + TP_POINTS if signal == "LONG" else price - TP_POINTS
    sl_price = price - SL_POINTS if signal == "LONG" else price + SL_POINTS

    session.place_order(
        category="linear",
        symbol=SYMBOL,
        side=side,
        orderType="Market",
        qty=QTY,
    )

    session.set_trading_stop(
        category="linear",
        symbol=SYMBOL,
        takeProfit=str(round(tp_price, 2)),
        stopLoss=str(round(sl_price, 2)),
        positionIdx=0,
    )

    app.logger.info("Order placed: side=%s entry=%s tp=%s sl=%s qty=%s", side, price, tp_price, sl_price, QTY)


@app.route("/webhook", methods=["POST"])
def webhook():
    global last_signal

    data = request.get_json(silent=True)
    if not data:
        return jsonify({"error": "no data"}), 400

    signal = str(data.get("signal", "")).upper()
    app.logger.info("Incoming signal: %s", signal)

    if signal not in ("LONG", "SHORT"):
        return jsonify({"error": "wrong signal"}), 400

    if signal == last_signal:
        app.logger.info("Duplicate signal skipped")
        return jsonify({"status": "duplicate"})

    try:
        price = get_last_price()
        app.logger.info("Configured qty=%s symbol=%s testnet=%s", QTY, SYMBOL, TESTNET)

        position = get_position()
        if position:
            close_existing_position(position)

        place_market_order(signal, price)

        # update duplicate guard only after successful execution
        last_signal = signal
        return jsonify({"status": "ok"})

    except Exception as exc:  # noqa: BLE001
        app.logger.exception("Webhook handling failed")
        return jsonify({"error": str(exc)}), 500
