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

TP_POINTS = 95
SL_POINTS = 40

last_signal = None


@app.route("/")
def home():
    return "Bybit bot is alive 🚀"

@app.route("/health")
def health():
return {"status": "ok"}

def get_position():
    positions = session.get_positions(
        category="linear",
        symbol=SYMBOL
    )

    for p in positions["result"]["list"]:
        if float(p["size"]) > 0:
            return p

    return None

@app.route("/webhook", methods=["POST"])
def webhook():
    global last_signal

    data = request.json

    if not data:
        return jsonify({"error": "no data"}), 400

    signal = data.get("signal")
    print("🔥 Сигнал:", signal)

    if signal == last_signal:
        return jsonify({"status": "duplicate ignored"})

    last_signal = signal

    try:
        # получаем текущую цену
        ticker = session.get_tickers(category="linear", symbol=SYMBOL)
        price = float(ticker["result"]["list"][0]["lastPrice"])

        # закрываем позицию если есть
        position = get_position()
        if position:
            side = "Sell" if position["side"] == "Buy" else "Buy"

            session.place_order(
                category="linear",
                symbol=SYMBOL,
                side=side,
                orderType="Market",
                qty=position["size"],
                reduceOnly=True
            )

         # направление сделки
         if signal == "LONG":
             side = "Buy"
             tp = price + TP_POINTS
             sl = price - SL_POINTS
         elif signal == "SHORT":
             side = "Sell"
             tp = price - TP_POINTS
             sl = price + SL_POINTS
         else:
             return jsonify({"error": "unknown signal"}), 400

         # открываем позицию СРАЗУ с TP/SL
         order = session.place_order(
             category="linear",
             symbol=SYMBOL,
             side=side,
             orderType="Market",
             qty=QTY,
             takeProfit=round(tp, 2),
             stopLoss=round(sl, 2)
          )

          print(f"📍 Entry ~ {price}")
          print(f"🎯 TP: {tp} | 🛑 SL: {sl}")

          return jsonify({"status": "order placed"})

    except Exception as e:
        print("❌ Ошибка:", str(e))
        return jsonify({"error": str(e)}), 500