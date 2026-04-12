from flask import Flask, request, jsonify

app = Flask(__name__)

@app.route('/webhook', methods=['POST'])
def webhook():
    data = request.json

    if not data:
        print("⚠️ Пустой запрос")
        return jsonify({"error": "no data"}), 400

    print("🔥 Получен сигнал:", data)

    return jsonify({"status": "ok"})