import json
import os
import uuid

import anthropic
from dotenv import load_dotenv
from flask import Flask, jsonify, request

load_dotenv()

app = Flask(__name__)
anthropic_client = anthropic.Anthropic()

DATA_DIR = os.path.join(os.path.dirname(__file__), "..", "data")
MENU_PATH = os.path.join(DATA_DIR, "menu.json")
ORDERS_PATH = os.path.join(DATA_DIR, "orders.json")

PROMPTS_DIR = os.path.join(os.path.dirname(__file__), "..", "prompts")
CHAT_SYSTEM_PATH = os.path.join(PROMPTS_DIR, "system-prompt.md")

GET_MENU_TOOL = {
    "name": "getMenu",
    "description": "Get the cafe's current menu, grouped by category. Only currently available items are included.",
    "input_schema": {
        "type": "object",
        "properties": {},
        "additionalProperties": False,
    },
}


def _get_active_menu():
    with open(MENU_PATH) as f:
        categories = json.load(f)
    active = []
    for cat in categories:
        active_items = [item for item in cat["items"] if item.get("available", True)]
        if active_items:
            active.append({"category": cat["category"], "items": active_items})
    return active


def _run_tool(name, tool_input):
    if name == "getMenu":
        return _get_active_menu()
    return {"error": f"unknown tool: {name}"}


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

    history = data.get("history", [])
    if not isinstance(history, list):
        return jsonify(error="history must be a list"), 400

    messages = []
    for entry in history:
        if (
            not isinstance(entry, dict)
            or entry.get("role") not in ("user", "assistant")
            or not isinstance(entry.get("content"), str)
        ):
            return jsonify(error="each history entry must have role (user or assistant) and content"), 400
        messages.append({"role": entry["role"], "content": entry["content"]})
    messages.append({"role": "user", "content": message})

    with open(CHAT_SYSTEM_PATH) as f:
        system_prompt = f.read()

    try:
        response = anthropic_client.messages.create(
            model="claude-opus-5",
            max_tokens=1024,
            system=system_prompt,
            tools=[GET_MENU_TOOL],
            messages=messages,
        )

        while response.stop_reason == "tool_use":
            tool_use_blocks = [b for b in response.content if b.type == "tool_use"]
            messages.append({"role": "assistant", "content": response.content})

            tool_results = [
                {
                    "type": "tool_result",
                    "tool_use_id": block.id,
                    "content": json.dumps(_run_tool(block.name, block.input)),
                }
                for block in tool_use_blocks
            ]
            messages.append({"role": "user", "content": tool_results})

            response = anthropic_client.messages.create(
                model="claude-opus-5",
                max_tokens=1024,
                system=system_prompt,
                tools=[GET_MENU_TOOL],
                messages=messages,
            )
    except anthropic.APIError:
        return jsonify(error="chat service unavailable"), 502

    reply = next((block.text for block in response.content if block.type == "text"), "")
    return jsonify(reply=reply)


@app.after_request
def add_cors_headers(response):
    response.headers["Access-Control-Allow-Origin"] = "*"
    response.headers["Access-Control-Allow-Headers"] = "Content-Type"
    response.headers["Access-Control-Allow-Methods"] = "GET, POST"
    return response


if __name__ == "__main__":
    app.run(port=int(os.environ.get("PORT", 5000)), debug=True)
