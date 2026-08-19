import json
import os

from flask import Flask, jsonify, request

app = Flask(__name__)

DATA_DIR = os.path.join(os.path.dirname(__file__), "..", "data")
MENU_PATH = os.path.join(DATA_DIR, "menu.json")


@app.get("/health")
def health():
    return jsonify(status="ok")


@app.get("/menu")
def menu():
    with open(MENU_PATH) as f:
        return jsonify(json.load(f))


@app.post("/chat")
def chat():
    data = request.get_json(silent=True) or {}
    message = data.get("message")
    if not isinstance(message, str) or not message.strip():
        return jsonify(error="message is required"), 400
    return jsonify(reply="Hi! I'm CafeBot. My AI brain isn't connected yet.")


@app.after_request
def add_cors_headers(response):
    response.headers["Access-Control-Allow-Origin"] = "*"
    return response


if __name__ == "__main__":
    app.run(debug=True)
