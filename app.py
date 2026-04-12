from flask import Flask, request, jsonify
import threading
import time
import os
from binance.client import Client

app = Flask(__name__)

# 🔑 Binance API
client = Client(
    os.getenv("BINANCE_API_KEY"),
    os.getenv("BINANCE_SECRET_KEY")
)

# 👉 подключаем testnet futures
client.FUTURES_URL = "https://testnet.binancefuture.com"

# 💓 heartbeat лог
def heartbeat():
    while True:
        print("💓 Heartbeat: сервер работает")
        time.sleep(60)

threading.Thread(target=heartbeat, daemon=True).start()


# 🏠 проверка сервера
@app.route('/')
def home():
    return "Server is alive", 200

@app.route('/health')
def health():
    return {"status": "ok"}, 200


# 🔍 проверка открытой позиции
def has_open_position(symbol="BTCUSDT"):
    try:
        positions = client.futures_position_information(symbol=symbol)
        for p in positions:
            if float(p['positionAmt']) != 0:
                return True
        return False
    except Exception as e:
        print("❌ Ошибка проверки позиции:", e)
        return True  # на всякий случай блокируем


# 🚀 открытие позиции
def open_position(signal):
    symbol = "BTCUSDT"
    quantity = 0.001  # тестовый объем

    if has_open_position(symbol):
        print("⚠️ Уже есть открытая позиция")
        return

    try:
        if signal == "LONG":
            side = "BUY"
        elif signal == "SHORT":
            side = "SELL"
        else:
            print("❌ Неизвестный сигнал:", signal)
            return

        order = client.futures_create_order(
            symbol=symbol,
            side=side,
            type="MARKET",
            quantity=quantity
        )

        print("✅ Открыта позиция:", order)

    except Exception as e:
        print("❌ Ошибка ордера:", e)


# 📩 webhook от TradingView
@app.route('/webhook', methods=['POST'])
def webhook():
    data = request.json

    if not data:
        print("⚠️ Пустой запрос")
        return jsonify({"error": "no data"}), 400

    signal = data.get("signal")

    print("🔥 Получен сигнал:", signal)

    open_position(signal)

    return jsonify({"status": "ok"})