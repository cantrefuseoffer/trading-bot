from flask import Flask, request, jsonify
from binance.client import Client
import os

app = Flask(__name__)

# API ключи из Render ENV
API_KEY = os.environ.get("BINANCE_API_KEY")
API_SECRET = os.environ.get("BINANCE_API_SECRET")

client = Client(API_KEY, API_SECRET)

SYMBOL = "BTCUSDT"
QUANTITY = 0.001  # настрой под себя

@app.route("/")
def home():
    return "Bot is alive 🚀"

@app.route("/webhook", methods=["POST"])
def webhook():
    data = request.json

    if not data:
        return jsonify({"error": "no data"}), 400

    print("🔥 Получен сигнал:", data)

    signal = data.get("signal")

    try:
        if signal == "LONG":
            order = client.futures_create_order(
                symbol=SYMBOL,
                side="BUY",
                type="MARKET",
                quantity=QUANTITY
            )
            print("✅ LONG открыт:", order)

        elif signal == "SHORT":
            order = client.futures_create_order(
                symbol=SYMBOL,
                side="SELL",
                type="MARKET",
                quantity=QUANTITY
            )
            print("✅ SHORT открыт:", order)

        else:
            return jsonify({"error": "unknown signal"}), 400

        return jsonify({"status": "order sent"})

    except Exception as e:
        print("❌ Ошибка:", str(e))
        return jsonify({"error": str(e)}), 500