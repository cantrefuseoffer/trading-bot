from flask import Flask, request, jsonify
from pybit.unified_trading import HTTP
import os

app = Flask(__name__)

API_KEY = os.environ.get("BYBIT_API_KEY")
API_SECRET = os.environ.get("BYBIT_SECRET_KEY")

session = HTTP(
    testnet=True,
    api_key=API_KEY,
    api_secret=API_SECRET
)

SYMBOL = "BTCUSDT"
QTY = 0.001

TP_POINTS = 75
SL_POINTS = 50

last_signal = None


@app.route("/")
def home():
    return "Bybit bot is alive 🚀"


@app.route("/health")
def health():
    return {"status": "ok"}


def get_position():
    positions = session.get_positions(category="linear", symbol=SYMBOL)
    for p in positions["result"]["list"]:
        if float(p["size"]) > 0:
            return p
    return None


@app.route("/webhook", methods=["POST"])
def webhook():
    global last_signal

    data = request.get_json(silent=True)

    if not data:
        return jsonify({"error": "no data"}), 400

    signal = str(data.get("signal", "")).upper()
    print("🔥 Сигнал:", signal)

    if signal not in ("LONG", "SHORT"):
        return jsonify({"error": "wrong signal"}), 400

    if signal == last_signal:
        print("⚠️ Дубликат")
        return jsonify({"status": "duplicate"})

    last_signal = signal

    try:
        # получаем текущую цену
        ticker = session.get_tickers(category="linear", symbol=SYMBOL)
        price = float(ticker['result']['list'][0]['lastPrice'])

        print(f"📦 Qty: {QTY}")

        position = get_position()

        if position:
            print("🔄 Закрываем старую позицию")

            side = "Sell" if position['side'] == "Buy" else "Buy"

            session.place_order(
                category="linear",
                symbol=SYMBOL,
                side=side,
                orderType="Market",
                qty=position['size'],
                reduceOnly=True
            )

        # направление сделки
        if signal == "LONG":
            side = "Buy"
            tp_price = price + TP_POINTS
            sl_price = price - SL_POINTS
        else:
            side = "Sell"
            tp_price = price - TP_POINTS
            sl_price = price + SL_POINTS

        # открытие позиции
        session.place_order(
            category="linear",
            symbol=SYMBOL,
            side=side,
            orderType="Market",
            qty=QTY
        )

        print(f"📍 Entry: {price}")

        session.set_trading_stop(
            category="linear",
            symbol=SYMBOL,
            takeProfit=str(round(tp_price, 2)),
            stopLoss=str(round(sl_price, 2)),
            positionIdx=0
        )

        print(f"🎯 TP: {tp_price} | SL: {sl_price}")

        return jsonify({"status": "ok"})

    except Exception as e:
        print("❌ Ошибка:", str(e))
        return jsonify({"error": str(e)}), 500
