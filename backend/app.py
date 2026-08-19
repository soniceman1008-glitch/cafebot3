import json
import os
import uuid

from dotenv import load_dotenv
from flask import Flask, jsonify, request

load_dotenv()

app = Flask(__name__)

DATA_DIR = os.path.join(os.path.dirname(__file__), "..", "data")
MENU_PATH = os.path.join(DATA_DIR, "menu.json")
ORDERS_PATH = os.path.join(DATA_DIR, "orders.json")


@app.get("/health")
def health():
    return jsonify(status="ok")


@app.get("/menu")
def menu():
    with open(MENU_PATH) as f:
        return jsonify(json.load(f))


def _menu_prices():
    with open(MENU_PATH) as f:
        categories = json.load(f)
    return {item["name"]: item["price"] for cat in categories for item in cat["items"]}


@app.post("/order")
def place_order():
    data = request.get_json(silent=True) or {}
    items = data.get("items")
    if not isinstance(items, list) or not items:
        return jsonify(error="items is required"), 400

    prices = _menu_prices()
    order_items = []
    total = 0.0
    for entry in items:
        if not isinstance(entry, dict):
            return jsonify(error="each item must be an object with name and quantity"), 400
        name = entry.get("name")
        quantity = entry.get("quantity", 1)
        if name not in prices:
            return jsonify(error=f"unknown menu item: {name}"), 400
        if not isinstance(quantity, int) or quantity < 1:
            return jsonify(error="quantity must be a positive integer"), 400
        price = prices[name]
        total += price * quantity
        order_items.append({"name": name, "quantity": quantity, "price": price})

    order_record = {
        "id": str(uuid.uuid4()),
        "items": order_items,
        "total": round(total, 2),
    }

    orders = []
    if os.path.exists(ORDERS_PATH):
        with open(ORDERS_PATH) as f:
            orders = json.load(f)
    orders.append(order_record)
    with open(ORDERS_PATH, "w") as f:
        json.dump(orders, f, indent=2)

    return jsonify(order_id=order_record["id"], items=order_items, total=order_record["total"])


@app.get("/orders")
def list_orders():
    if not os.path.exists(ORDERS_PATH):
        return jsonify([])
    with open(ORDERS_PATH) as f:
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
    response.headers["Access-Control-Allow-Headers"] = "Content-Type"
    response.headers["Access-Control-Allow-Methods"] = "GET, POST"
    return response


if __name__ == "__main__":
    app.run(port=int(os.environ.get("PORT", 5000)), debug=True)
