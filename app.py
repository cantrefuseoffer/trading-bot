from flask import Flask, request, jsonify
from binance.client import Client
import os
import time

app = Flask(__name__)

API_KEY = os.environ.get("BINANCE_API_KEY")
API_SECRET = os.environ.get("BINANCE_SECRET_KEY")

client = Client(API_KEY, API_SECRET)
client.FUTURES_URL = 'https://testnet.binancefuture.com/fapi'

SYMBOL = "BTCUSDT"
QUANTITY = 1
LEVERAGE = 50

# настройки стратегии
import random

last_signal = None
last_trade_time = 0
COOLDOWN = 120  # секунды

# установка плеча
try:
    client.futures_change_leverage(symbol=SYMBOL, leverage=LEVERAGE)
    print(f"✅ Плечо установлено: {LEVERAGE}x")
except Exception as e:
    print("❌ Ошибка установки плеча:", e)

@app.route("/")
def home():
    return "Bot is alive 🚀"

@app.route("/health")
def health():
    return "ok", 200

def get_position():
    positions = client.futures_position_information(symbol=SYMBOL)
    for p in positions:
        if float(p['positionAmt']) != 0:
            return p
    return None

@app.route("/webhook", methods=["POST"])
def webhook():
    global last_signal, last_trade_time

    data = request.json

    if not data:
        return jsonify({"error": "no data"}), 400

    signal = data.get("signal")

    print("🔥 Сигнал:", signal)

    # защита от дублей
    if signal == last_signal:
        print("⚠️ Дубликат сигнала")
        return jsonify({"status": "duplicate ignored"})

    # cooldown
    if time.time() - last_trade_time < COOLDOWN:
        print("⏳ Cooldown активен")
        return jsonify({"status": "cooldown"})

    last_signal = signal
    last_trade_time = time.time()

    try:
        position = get_position()

        # закрываем старую позицию
        if position:
            print("🔄 Закрываем текущую позицию")

            side = "SELL" if float(position['positionAmt']) > 0 else "BUY"

            client.futures_create_order(
                symbol=SYMBOL,
                side=side,
                type="MARKET",
                quantity=abs(float(position['positionAmt']))
            )

        # определяем сторону
        if signal == "LONG":
            side = "BUY"
            exit_side = "SELL"
        elif signal == "SHORT":
            side = "SELL"
            exit_side = "BUY"
        else:
            return jsonify({"error": "unknown signal"}), 400

        # открытие позиции
        order = client.futures_create_order(
            symbol=SYMBOL,
            side=side,
            type="MARKET",
            quantity=QUANTITY
        )

        print("✅ Открыта позиция:", order)

        time.sleep(2)

        ticker = client.futures_mark_price(symbol=SYMBOL)
        entry_price = round(float(ticker['markPrice']), 2)

        print(f"📍 Entry price (real): {entry_price}")

        # 🔥 ТРЕЙЛИНГ СТОП
        trailing_callback = round(random.uniform(0.5, 1.0), 2)

        client.futures_create_order(
            symbol=SYMBOL,
            side=exit_side,
            type="TRAILING_STOP_MARKET",
            callbackRate=trailing_callback,
            closePosition=True
        )
        print(f"🎯 Trailing: {trailing_callback}%")

        return jsonify({"status": "order placed with trailing stop"})

    except Exception as e:
        print("❌ Ошибка:", str(e))
        return jsonify({"error": str(e)}), 500