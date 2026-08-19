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

# In-memory session order state. Not persisted, not a database — resets on restart.
# Each session's state: items (each with quantity/options), orderType, customer details,
# discount, total, confirmed, and status.
session_orders = {}


def _new_order_state():
    return {
        "items": [],
        "order_type": None,
        "customer": {"name": None, "phone": None, "email": None},
        "discount": None,
        "total": 0.0,
        "confirmed": False,
        "status": "in_progress",
    }


def get_session_order(session_id):
    if session_id not in session_orders:
        session_orders[session_id] = _new_order_state()
    return session_orders[session_id]


GET_MENU_TOOL = {
    "name": "getMenu",
    "description": "Get the cafe's current menu, grouped by category. Only currently available items are included.",
    "input_schema": {
        "type": "object",
        "properties": {},
        "additionalProperties": False,
    },
}

ADD_ITEM_TOOL = {
    "name": "addItemToCart",
    "description": (
        "Add a menu item to the customer's current order. Requires the exact item name "
        "from the menu. If the item has options (like size) and none is given, the tool "
        "reports which options are available instead of guessing — ask the customer and "
        "call the tool again with their choice."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "name": {"type": "string", "description": "Exact menu item name, e.g. 'Latte'."},
            "quantity": {"type": "integer", "minimum": 1, "description": "How many to add. Defaults to 1."},
            "option": {"type": "string", "description": "The customer's chosen size/option, if the item has options."},
        },
        "required": ["name"],
        "additionalProperties": False,
    },
}


MODIFY_ITEM_TOOL = {
    "name": "modifyItem",
    "description": (
        "Adjust the quantity and/or size/option of an item already in the customer's "
        "order. Requires the exact item name as it appears in the cart. Provide quantity "
        "and/or option — whichever is changing."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "name": {"type": "string", "description": "Exact name of the item already in the cart to modify."},
            "quantity": {"type": "integer", "minimum": 1, "description": "New quantity for the item, if changing."},
            "option": {"type": "string", "description": "New size/option for the item, if changing."},
        },
        "required": ["name"],
        "additionalProperties": False,
    },
}


REMOVE_ITEM_TOOL = {
    "name": "removeItem",
    "description": (
        "Remove an item from the customer's order, or reduce its quantity. Requires the "
        "exact item name as it appears in the cart. If quantity is omitted, or is greater "
        "than or equal to the current quantity, the item is removed entirely; otherwise "
        "its quantity is reduced by that amount."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "name": {"type": "string", "description": "Exact name of the item in the cart to remove or reduce."},
            "quantity": {"type": "integer", "minimum": 1, "description": "How many to remove. Omit to remove the item entirely."},
        },
        "required": ["name"],
        "additionalProperties": False,
    },
}

VIEW_CART_TOOL = {
    "name": "viewCart",
    "description": (
        "Get a concise, itemized summary of the customer's current order — each item's "
        "name, quantity, and chosen customizations (like size). Does not include pricing "
        "or a total."
    ),
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


def _find_menu_item(name):
    with open(MENU_PATH) as f:
        categories = json.load(f)
    for cat in categories:
        for item in cat["items"]:
            if item["name"] == name:
                return item
    return None


def _add_item_to_cart(session_id, tool_input):
    name = tool_input.get("name")
    quantity = tool_input.get("quantity", 1)
    option = tool_input.get("option")

    if not isinstance(name, str) or not name.strip():
        return {"error": "name is required"}
    if not isinstance(quantity, int) or quantity < 1:
        return {"error": "quantity must be a positive integer"}

    item = _find_menu_item(name)
    if item is None:
        return {"error": f"'{name}' is not a valid menu item"}
    if not item.get("available", True):
        return {"error": f"'{name}' is currently unavailable"}

    options = item.get("options", [])
    if options and not option:
        return {
            "error": "option_required",
            "message": f"'{name}' requires an option to be chosen before it can be added.",
            "available_options": options,
        }
    if options and option not in options:
        return {
            "error": "invalid_option",
            "message": f"'{option}' is not a valid option for '{name}'.",
            "available_options": options,
        }

    order = get_session_order(session_id)
    order["items"].append(
        {
            "name": name,
            "quantity": quantity,
            "options": [option] if option else [],
            "price": item["price"],
        }
    )
    order["total"] = round(sum(i["price"] * i["quantity"] for i in order["items"]), 2)

    return {"added": {"name": name, "quantity": quantity, "option": option}, "cart": order}


def _modify_item(session_id, tool_input):
    name = tool_input.get("name")
    quantity = tool_input.get("quantity")
    option = tool_input.get("option")

    if not isinstance(name, str) or not name.strip():
        return {"error": "name is required"}
    if quantity is None and option is None:
        return {"error": "quantity and/or option is required to modify the item"}
    if quantity is not None and (not isinstance(quantity, int) or quantity < 1):
        return {"error": "quantity must be a positive integer"}

    order = get_session_order(session_id)
    cart_item = next((i for i in order["items"] if i["name"] == name), None)
    if cart_item is None:
        return {"error": f"'{name}' is not in the current order"}

    if option is not None:
        menu_item = _find_menu_item(name)
        if menu_item is None:
            return {"error": f"'{name}' is not a valid menu item"}
        options = menu_item.get("options", [])
        if not options:
            return {"error": f"'{name}' does not have selectable options"}
        if option not in options:
            return {
                "error": "invalid_option",
                "message": f"'{option}' is not a valid option for '{name}'.",
                "available_options": options,
            }
        cart_item["options"] = [option]

    if quantity is not None:
        cart_item["quantity"] = quantity

    order["total"] = round(sum(i["price"] * i["quantity"] for i in order["items"]), 2)

    return {"modified": cart_item, "cart": order}


def _remove_item(session_id, tool_input):
    name = tool_input.get("name")
    quantity = tool_input.get("quantity")

    if not isinstance(name, str) or not name.strip():
        return {"error": "name is required"}
    if quantity is not None and (not isinstance(quantity, int) or quantity < 1):
        return {"error": "quantity must be a positive integer"}

    order = get_session_order(session_id)
    cart_item = next((i for i in order["items"] if i["name"] == name), None)
    if cart_item is None:
        return {"error": f"'{name}' is not in the current order"}

    if quantity is None or quantity >= cart_item["quantity"]:
        removed_quantity = cart_item["quantity"]
        order["items"].remove(cart_item)
        remaining_quantity = 0
    else:
        cart_item["quantity"] -= quantity
        removed_quantity = quantity
        remaining_quantity = cart_item["quantity"]

    order["total"] = round(sum(i["price"] * i["quantity"] for i in order["items"]), 2)

    return {
        "removed": {"name": name, "quantity": removed_quantity},
        "remaining_quantity": remaining_quantity,
        "cart": order,
    }


def _view_cart(session_id):
    order = get_session_order(session_id)
    return [
        {"name": item["name"], "quantity": item["quantity"], "options": item["options"]}
        for item in order["items"]
    ]


def _run_tool(name, tool_input, session_id):
    if name == "getMenu":
        return _get_active_menu()
    if name == "addItemToCart":
        return _add_item_to_cart(session_id, tool_input)
    if name == "modifyItem":
        return _modify_item(session_id, tool_input)
    if name == "removeItem":
        return _remove_item(session_id, tool_input)
    if name == "viewCart":
        return _view_cart(session_id)
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

    session_id = data.get("session_id") or "default"

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

    tools = [GET_MENU_TOOL, ADD_ITEM_TOOL, MODIFY_ITEM_TOOL, REMOVE_ITEM_TOOL, VIEW_CART_TOOL]

    try:
        response = anthropic_client.messages.create(
            model="claude-opus-5",
            max_tokens=1024,
            system=system_prompt,
            tools=tools,
            messages=messages,
        )

        while response.stop_reason == "tool_use":
            tool_use_blocks = [b for b in response.content if b.type == "tool_use"]
            messages.append({"role": "assistant", "content": response.content})

            tool_results = [
                {
                    "type": "tool_result",
                    "tool_use_id": block.id,
                    "content": json.dumps(_run_tool(block.name, block.input, session_id)),
                }
                for block in tool_use_blocks
            ]
            messages.append({"role": "user", "content": tool_results})

            response = anthropic_client.messages.create(
                model="claude-opus-5",
                max_tokens=1024,
                system=system_prompt,
                tools=tools,
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
