import json
import os

from flask import Flask, jsonify

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


if __name__ == "__main__":
    app.run(debug=True)
