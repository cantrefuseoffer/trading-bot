import os
from binance.client import Client

client = Client(
    os.getenv("BINANCE_API_KEY"),
    os.getenv("BINANCE_SECRET_KEY")
)
from flask import Flask, request, jsonify
import threading
import time

app = Flask(__name__)

@app.route('/')
def home():
    return "Server is alive", 200

@app.route('/health')
def health():
    return {"status": "ok"}, 200

@app.route('/webhook', methods=['POST'])
def webhook():
    data = request.json

    if not data:
        print("⚠️ Пустой запрос")
        return jsonify({"error": "no data"}), 400

    print("🔥 Получен сигнал:", data)
    return jsonify({"status": "ok"})

# 💓 heartbeat лог
def heartbeat():
    while True:
        print("💓 Heartbeat: сервер работает")
        time.sleep(60)

threading.Thread(target=heartbeat, daemon=True).start()