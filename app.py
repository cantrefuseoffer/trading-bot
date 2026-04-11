from flask import Flask, request, jsonify

app = Flask(__name__)

@app.route('/webhook', methods=['POST'])
def webhook():
    data = request.json

    if not data:
        print("⚠️ Пустой запрос")
        return jsonify({"error": "no data"}), 400

    print("🔥 SIGNAL:", data)

    return jsonify({"status": "ok"})

@app.route('/')
def home():
    return "Server is running"

if name == '__main__':
    app.run(host='0.0.0.0', port=10000)