from flask import Flask, request, jsonify
from binance.client import Client
import os

app = Flask(__name__)

# 🔑 API ключи
API_KEY = os.environ.get("BINANCE_API_KEY")
API_SECRET = os.environ.get("BINANCE_SECRET_KEY")

client = Client(API_KEY, API_SECRET)
client.FUTURES_URL = 'https://testnet.binancefuture.com/fapi'

SYMBOL = "BTCUSDT"

# ⚙️ настройки
RISK_PERCENT = 2 # риск на сделку (%)
LEVERAGE = 10 # плечо

# 🔒 защита от дублей
last_signal = None

# 🚀 выставляем плечо при старте
try:
client.futures_change_leverage(symbol=SYMBOL, leverage=LEVERAGE)
print(f"✅ Установлено плечо {LEVERAGE}x")
except Exception as e:
print("❌ Ошибка установки плеча:", e)


@app.route("/")
def home():
return "Bot is alive 🚀"


# 💰 баланс
def get_balance():
balance = client.futures_account_balance()
for b in balance:
if b['asset'] == 'USDT':
return float(b['balance'])
return 0


# 📦 расчёт позиции
def calculate_quantity(price):
balance = get_balance()

risk_amount = balance * (RISK_PERCENT / 100)
position_size = risk_amount * LEVERAGE

qty = position_size / price

return round(qty, 3)


# 📊 текущая позиция
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

# ❌ защита от дублей
if signal == last_signal:
print("⚠️ Дубликат сигнала — пропуск")
return jsonify({"status": "duplicate ignored"})

last_signal = signal

try:
# 📊 текущая цена
price_data = client.futures_mark_price(symbol=SYMBOL)
current_price = float(price_data['markPrice'])

# 📦 расчёт объёма
quantity = calculate_quantity(current_price)

print(f"💰 Баланс: {get_balance()} USDT")
print(f"📦 Размер позиции: {quantity}")

# 📊 проверяем позицию
position = get_position()

# 🔄 закрываем старую позицию
if position:
print("🔄 Закрываем текущую позицию")

side = "SELL" if float(position['positionAmt']) > 0 else "BUY"

client.futures_create_order(
symbol=SYMBOL,
side=side,
type="MARKET",
quantity=abs(float(position['positionAmt']))
)

# 🚀 определяем сторону
if signal == "LONG":
side = "BUY"
elif signal == "SHORT":
side = "SELL"
else:
return jsonify({"error": "unknown signal"}), 400

# 🚀 открываем сделку
order = client.futures_create_order(
symbol=SYMBOL,
side=side,
type="MARKET",
quantity=quantity
)

print("✅ Открыта позиция:", order)

# 🎯 цена входа
entry_price = float(order['avgPrice'])

if entry_price == 0:
ticker = client.futures_mark_price(symbol=SYMBOL)
entry_price = float(ticker['markPrice'])
print("⚠️ avgPrice=0, используем mark price:", entry_price)

# 🎯 TP / SL
if signal == "LONG":
tp_price = entry_price * 1.008
sl_price = entry_price * 0.994
exit_side = "SELL"
else:
tp_price = entry_price * 0.992
sl_price = entry_price * 1.006
exit_side = "BUY"

tp_price = round(tp_price, 2)
sl_price = round(sl_price, 2)

# TAKE PROFIT
client.futures_create_order(
symbol=SYMBOL,
side=exit_side,
type="TAKE_PROFIT_MARKET",
stopPrice=tp_price,
closePosition=True
)

# STOP LOSS
client.futures_create_order(
symbol=SYMBOL,
side=exit_side,
type="STOP_MARKET",
stopPrice=sl_price,
closePosition=True
)

print(f"🎯 TP: {tp_price}, SL: {sl_price}")

return jsonify({"status": "order placed with TP/SL"})

except Exception as e:
print("❌ Ошибка:", str(e))
return jsonify({"error": str(e)}), 500