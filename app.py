from flask import Flask, request, jsonify
from binance.client import Client
import os

app = Flask(__name__)

API_KEY = os.environ.get("BINANCE_API_KEY")
API_SECRET = os.environ.get("BINANCE_SECRET_KEY")

client = Client(API_KEY, API_SECRET)
client.FUTURES_URL = 'https://testnet.binancefuture.com/fapi'

SYMBOL = "BTCUSDT"
RISK_PERCENT = 1
LEVERAGE = 50

TP_POINTS = 90
SL_POINTS = 50 # защита

last_signal = None

# установка плеча
try:
    client.futures_change_leverage(symbol=SYMBOL, leverage=LEVERAGE)
    print(f"✅ Установлено плечо {LEVERAGE}x")
except Exception as e:
    print("❌ Ошибка установки плеча:", e)


@app.route("/")
def home():
    return "Bot is alive 🚀"


@app.route("/health")
def health():
    return {"status": "ok"}


def get_balance():
    balance = client.futures_account_balance()
    for b in balance:
        if b['asset'] == 'USDT':
            return float(b['balance'])
    return 0


def calculate_quantity(price):
    balance = get_balance()
    risk_amount = balance * (RISK_PERCENT / 100)
    position_size = risk_amount * LEVERAGE
    qty = position_size / price
    return round(qty, 3)


def get_position():
    positions = client.futures_position_information(symbol=SYMBOL)
    for p in positions:
        if float(p['positionAmt']) != 0:
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
        print("⚠️ Дубликат сигнала")
        return jsonify({"status": "duplicate ignored"})

    last_signal = signal

    try:
        # текущая цена
        price_data = client.futures_mark_price(symbol=SYMBOL)
        current_price = float(price_data['markPrice'])

        # размер позиции
        quantity = calculate_quantity(current_price)
        print(f"📦 Qty: {quantity}")

        # закрываем старую позицию
        position = get_position()
        if position:
            side = "SELL" if float(position['positionAmt']) > 0 else "BUY"

            client.futures_create_order(
                symbol=SYMBOL,
                side=side,
                type="MARKET",
                quantity=abs(float(position['positionAmt']))
             )

        # направление
        if signal == "LONG":
            side = "BUY"
            exit_side = "SELL"
        elif signal == "SHORT":
            side = "SELL"
            exit_side = "BUY"
        else:
            return jsonify({"error": "unknown signal"}), 400

        # вход
        order = client.futures_create_order(
            symbol=SYMBOL,
            side=side,
            type="MARKET",
            quantity=quantity
        )

        # цена входа
        entry_price = float(order['avgPrice'])
        if entry_price == 0:
            ticker = client.futures_mark_price(symbol=SYMBOL)
            entry_price = float(ticker['markPrice'])

        print(f"📍 Entry: {entry_price}")

        # TP / SL в пунктах
        if signal == "LONG":
            tp_price = round(entry_price + TP_POINTS, 2)
            sl_price = round(entry_price - SL_POINTS, 2)
        else:
            tp_price = round(entry_price - TP_POINTS, 2)
            sl_price = round(entry_price + SL_POINTS, 2)

        # TP
        client.futures_create_order(
            symbol=SYMBOL,
            side=exit_side,
            type="TAKE_PROFIT_MARKET",
            stopPrice=tp_price,
            closePosition=True
        )

        # SL
        client.futures_create_order(
            symbol=SYMBOL,
            side=exit_side,
            type="STOP_MARKET",
            stopPrice=sl_price,
            closePosition=True
        )

        print(f"🎯 TP: {tp_price}, SL: {sl_price}")

        return jsonify({"status": "ok"})

except Exception as e:
    print("❌ Ошибка:", str(e))
    return jsonify({"error": str(e)}), 500