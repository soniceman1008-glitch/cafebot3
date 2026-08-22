import datetime
import fcntl
import hmac
import json
import os
import tempfile
import threading
import time
import uuid
from collections import defaultdict, deque
from contextlib import contextmanager
from decimal import Decimal, ROUND_HALF_UP

import anthropic
from dotenv import load_dotenv
from flask import Flask, jsonify, request
from werkzeug.exceptions import HTTPException

load_dotenv()

app = Flask(__name__)
anthropic_client = anthropic.Anthropic()

# Debug mode must never be on by default. It's only enabled by explicitly setting
# FLASK_DEBUG=true/1 in the environment (local development) — never hardcode it on,
# since Flask's debug mode serves the interactive Werkzeug debugger (full stack traces,
# source code, and a REPL) directly to any client that triggers an unhandled exception.
DEBUG = os.environ.get("FLASK_DEBUG", "false").strip().lower() in ("1", "true", "yes")

DATA_DIR = os.path.join(os.path.dirname(__file__), "..", "data")
MENU_PATH = os.path.join(DATA_DIR, "menu.json")

# Orders are persisted by reading/writing this JSON file directly. This is for
# development/demo purposes only — Vercel's serverless functions run on an ephemeral,
# read-only-outside-/tmp filesystem, so writes here are not guaranteed to persist in
# production. A real deployment needs a proper database.
ORDERS_PATH = os.path.join(DATA_DIR, "orders.json")
PROMOTIONS_PATH = os.path.join(DATA_DIR, "promotions.json")

PROMPTS_DIR = os.path.join(os.path.dirname(__file__), "..", "prompts")
CHAT_SYSTEM_PATH = os.path.join(PROMPTS_DIR, "system-prompt.md")

# Simple, fixed pricing configuration — flat tax rate, flat delivery fee for delivery
# orders. Tax applies to the post-discount subtotal; the delivery fee itself is untaxed.
TAX_RATE = 0.08
DELIVERY_FEE = 3.00

# A reasonable ceiling for a single line item on a café order — guards against absurd
# quantities (e.g. accidental or automated submission of a nine-digit quantity) without
# affecting any realistic customer order.
MAX_ITEM_QUANTITY = 100

# The staff dashboard's fixed, forward-only fulfillment pipeline.
ORDER_STATUSES = ["NEW", "PREPARING", "READY", "COMPLETED"]

# Required to view full order records (with customer name/phone/address) or advance order
# status. Requests without a matching X-Staff-Key header only ever see the redacted,
# PII-free view of orders.json.
STAFF_API_KEY = os.environ.get("STAFF_API_KEY")

PUBLIC_ORDER_FIELDS = {"id", "items", "total", "status"}

# The frontend is served from its own origin (a separate static file server/host from
# the backend), so genuine cross-origin requests are expected — but only from the
# frontend's own known origin(s), never from an arbitrary website. Configurable so a
# real deployment can set its actual frontend origin(s) without a code change.
DEFAULT_ALLOWED_ORIGINS = "http://localhost:8000,http://127.0.0.1:8000,http://localhost:8500,http://127.0.0.1:8500"
ALLOWED_ORIGINS = {
    o.strip() for o in os.environ.get("ALLOWED_ORIGINS", DEFAULT_ALLOWED_ORIGINS).split(",") if o.strip()
}

# Lightweight in-memory rate limiting (no external dependency) — a fixed time window per
# bucket key. Not shared across multiple processes/workers, which is an acceptable
# limitation for this app's scale; it still stops a single client from hammering an
# endpoint with unlimited automated attempts.
RATE_LIMIT_WINDOW_SECONDS = 60
RATE_LIMIT_ORDER_PER_MINUTE = int(os.environ.get("RATE_LIMIT_ORDER_PER_MINUTE", 30))
RATE_LIMIT_CHAT_PER_MINUTE = int(os.environ.get("RATE_LIMIT_CHAT_PER_MINUTE", 30))
RATE_LIMIT_STAFF_PER_MINUTE = int(os.environ.get("RATE_LIMIT_STAFF_PER_MINUTE", 30))

_rate_lock = threading.Lock()
_rate_buckets = defaultdict(deque)


def _rate_limited(bucket_key, limit):
    now = time.time()
    with _rate_lock:
        bucket = _rate_buckets[bucket_key]
        while bucket and now - bucket[0] > RATE_LIMIT_WINDOW_SECONDS:
            bucket.popleft()
        if len(bucket) >= limit:
            return True
        bucket.append(now)
        return False


def _is_staff_request():
    if not STAFF_API_KEY:
        return False
    provided = request.headers.get("X-Staff-Key")
    if not isinstance(provided, str):
        return False
    # Constant-time comparison — a plain == leaks how many leading characters matched
    # via response timing, which a patient attacker could use to recover the key
    # byte-by-byte.
    return hmac.compare_digest(provided, STAFF_API_KEY)


def _redact_order(order):
    return {k: v for k, v in order.items() if k in PUBLIC_ORDER_FIELDS}


def _valid_quantity(quantity):
    return isinstance(quantity, int) and not isinstance(quantity, bool) and 1 <= quantity <= MAX_ITEM_QUANTITY


def _get_json_object():
    """Parse the request body as JSON, guaranteeing a dict is returned to callers so
    every route can safely call .get() on it. A missing/empty body is treated the same
    as an empty object (unchanged prior behavior); any JSON that parses to something
    other than an object (an array, string, number, or null literal) is reported back
    as a sentinel (None) rather than ever letting a non-dict reach a caller's .get()."""
    data = request.get_json(silent=True)
    if data is None:
        return {}
    if not isinstance(data, dict):
        return None
    return data

# In-memory session order state. Not persisted, not a database — resets on restart.
# Each session's state: items (each with quantity/options), orderType, customer details,
# discount, total, confirmed, and status.
session_orders = {}


def _new_order_state():
    return {
        "items": [],
        "order_type": None,
        "customer": {"name": None, "phone": None, "email": None},
        "pickup_time": None,
        "delivery": {"address": None, "apartment": None, "instructions": None, "address_confirmed": False},
        "discount": None,
        "subtotal": 0.0,
        "tax": 0.0,
        "delivery_fee": 0.0,
        "total": 0.0,
        "confirmed": False,
        "status": "in_progress",
        # True only immediately after getOrderSummary is called, so confirmOrder can verify
        # the customer confirmed the summary actually shown to them. Any tool that changes
        # the order invalidates this, forcing a fresh summary before confirming again.
        "summary_shown": False,
    }


def get_session_order(session_id):
    if session_id not in session_orders:
        session_orders[session_id] = _new_order_state()
    return session_orders[session_id]


def _round_currency(amount):
    """Round to the nearest cent using standard round-half-up currency convention.
    Python's builtin round() uses round-half-to-even on floats, which silently rounds
    exact-half-cent amounts (e.g. 10% of $10.25) down instead of up — always go through
    Decimal for money."""
    return float(Decimal(str(amount)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def _recompute_total(order):
    """The single, deterministic source of truth for order pricing — never let the model
    calculate or invent these figures itself."""
    subtotal = _round_currency(sum(i["price"] * i["quantity"] for i in order["items"]))
    discount_amount = order["discount"]["amount"] if order["discount"] else 0.0
    after_discount = max(_round_currency(subtotal - discount_amount), 0.0)
    tax = _round_currency(after_discount * TAX_RATE)
    delivery_fee = DELIVERY_FEE if order["order_type"] == "delivery" else 0.0

    order["subtotal"] = subtotal
    order["tax"] = tax
    order["delivery_fee"] = delivery_fee
    order["total"] = _round_currency(after_discount + tax + delivery_fee)
    return order


# Tracks item names already recommended per session, so a suggestion is never repeated
# (whether accepted — it's already in the cart — or declined). Separate from order state.
session_suggestions = {}


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

GET_RECOMMENDATIONS_TOOL = {
    "name": "getRecommendations",
    "description": (
        "Suggest at most 1-2 real, relevant menu items to pair with the customer's current "
        "order (e.g. a pastry with a coffee). Only ever returns real items already on the "
        "menu — never invent a suggestion. Automatically excludes items already in the "
        "cart and anything already suggested this session, so a suggestion is never "
        "repeated, whether the customer accepted or declined it."
    ),
    "input_schema": {
        "type": "object",
        "properties": {},
        "additionalProperties": False,
    },
}

APPLY_PROMOTION_TOOL = {
    "name": "applyPromotion",
    "description": (
        "List currently active promotions, or apply one to the customer's order by its "
        "exact id. Only promotions marked active in the promotions data are ever returned "
        "or applied — inactive ones and any id/code not found there are always rejected, "
        "never invented or accepted. Omit promotionId to list active promotions along with "
        "their eligibility rules, so you can check them against the order and ask the "
        "customer anything needed (like a student ID) before applying one."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "promotionId": {
                "type": "string",
                "description": "Exact id of the active promotion to apply, e.g. 'student-discount-10'. Omit to just list active promotions.",
            },
        },
        "additionalProperties": False,
    },
}

SET_PICKUP_DETAILS_TOOL = {
    "name": "setPickupDetails",
    "description": (
        "Select pickup for the order and record the customer's name (required before "
        "checkout) and an optional pickup time. Can be called with just one field at a "
        "time as the customer provides it. Always reports back which required fields are "
        "still missing, so you only ask for what's not already collected — never ask again "
        "for something the response shows is already set."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "customerName": {"type": "string", "description": "The customer's name for pickup."},
            "pickupTime": {"type": "string", "description": "Requested pickup time, if given, e.g. '3:30 PM' or 'in 20 minutes'. Optional."},
        },
        "additionalProperties": False,
    },
}

SET_DELIVERY_DETAILS_TOOL = {
    "name": "setDeliveryDetails",
    "description": (
        "Select delivery for the order and record the customer's name, phone number, and "
        "full delivery address (all required before checkout), plus apartment/unit and "
        "delivery instructions if applicable (both optional). Can be called with just one "
        "field at a time as the customer provides it. Always reports back which required "
        "fields are still missing, so you only ask for what's not already collected — never "
        "guess a value or ask again for something the response shows is already set. "
        "Changing the address or apartment resets confirmation, since a corrected address "
        "must be read back and reconfirmed. Before checkout, read the full address (street "
        "and apartment/unit if given) back to the customer, then pass confirmAddress: true "
        "only once they explicitly confirm it's correct, or confirmAddress: false if they "
        "say it's wrong — then correct it via the address/apartment fields and reconfirm."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "customerName": {"type": "string", "description": "The customer's name for delivery."},
            "phone": {"type": "string", "description": "The customer's phone number."},
            "address": {"type": "string", "description": "Full delivery address (street, city, etc.)."},
            "apartment": {"type": "string", "description": "Apartment/unit number, if applicable. Optional."},
            "instructions": {"type": "string", "description": "Delivery instructions, e.g. gate code or where to leave it. Optional."},
            "confirmAddress": {
                "type": "boolean",
                "description": "Set true only after reading the full address back and the customer explicitly confirms it's correct; set false if they say it's wrong.",
            },
        },
        "additionalProperties": False,
    },
}

GET_ORDER_TOTAL_TOOL = {
    "name": "getOrderTotal",
    "description": (
        "Get the order's real, deterministically calculated cost breakdown: subtotal, "
        "discount (if a promotion is applied), tax, delivery fee (delivery orders only), "
        "and the final total. This is calculated from real menu prices and quantities — "
        "never calculate, estimate, or state any of these figures yourself; always use "
        "this tool (or the cart figures other tools return) and report exactly what it "
        "gives you."
    ),
    "input_schema": {
        "type": "object",
        "properties": {},
        "additionalProperties": False,
    },
}

GET_ORDER_SUMMARY_TOOL = {
    "name": "getOrderSummary",
    "description": (
        "Get a complete, structured summary of the order for final review before checkout: "
        "items with quantities and customizations, fulfillment details (pickup or "
        "delivery, with whatever's been collected so far), any promotion applied, and the "
        "real calculated total. Always call this immediately before asking the customer to "
        "give final confirmation — never assemble the summary from memory."
    ),
    "input_schema": {
        "type": "object",
        "properties": {},
        "additionalProperties": False,
    },
}

CONFIRM_ORDER_TOOL = {
    "name": "confirmOrder",
    "description": (
        "Save and finalize the order — the only way an order is ever saved. Call this only "
        "after calling getOrderSummary, reading that exact summary to the customer, and "
        "receiving a clear, unambiguous 'yes' to finalize it. Pass confirmed: true for a "
        "clear affirmative (e.g. 'yes', 'confirm', 'that's correct, place it'). If the reply "
        "is unclear, ambiguous, non-committal, or not a direct answer (e.g. 'ok', 'sure', "
        "'maybe', 'I guess', a question, or anything that doesn't clearly affirm), do NOT "
        "call this tool — ask a direct yes/no question instead. Pass confirmed: false only "
        "if the customer explicitly declines or cancels. The tool itself refuses to save if "
        "the order is empty or if anything changed since the summary was last shown, so "
        "always re-call getOrderSummary and re-confirm after any change."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "confirmed": {
                "type": "boolean",
                "description": "True only for a clear, explicit 'yes' to finalize; false only for an explicit decline. Never guess this from an ambiguous reply.",
            },
        },
        "required": ["confirmed"],
        "additionalProperties": False,
    },
}


ORDERS_LOCK_PATH = ORDERS_PATH + ".lock"


@contextmanager
def _orders_file_lock():
    """Serializes every read-modify-write against data/orders.json across separate
    processes (not just threads within this one interpreter) using a POSIX advisory
    file lock, so two concurrent requests can never interleave their reads/writes.
    This is what actually fixes the original race — JS-side double-click protection
    only ever prevented one browser tab from firing two requests; it did nothing for
    two genuinely concurrent requests (e.g. two different customers, or two tabs)."""
    lock_file = open(ORDERS_LOCK_PATH, "a")
    try:
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
        yield
    finally:
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
        lock_file.close()


def _read_orders_unlocked():
    """Must only be called while holding _orders_file_lock(). Treats a missing file as
    empty, and — since a write is always a full atomic replace, never an in-place
    edit — a corrupt/unreadable file can only be leftover damage from before this
    locking existed, not something a properly-serialized writer can produce; recover
    from it the same way as a missing file rather than crashing every future request."""
    if not os.path.exists(ORDERS_PATH):
        return []
    try:
        with open(ORDERS_PATH) as f:
            content = f.read()
    except OSError:
        return []
    if not content.strip():
        return []
    try:
        return json.loads(content)
    except json.JSONDecodeError:
        app.logger.error("data/orders.json contained invalid JSON; treating as empty")
        return []


def _write_orders_atomic(orders):
    """Must only be called while holding _orders_file_lock(). Writes to a temp file in
    the same directory, then os.replace()s it over the real path — an atomic rename on
    POSIX, so a concurrent reader/writer only ever sees either the complete old file or
    the complete new one, never a partially-written one."""
    fd, tmp_path = tempfile.mkstemp(dir=DATA_DIR, prefix=".orders_tmp_")
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(orders, f, indent=2)
        os.replace(tmp_path, ORDERS_PATH)
    except BaseException:
        try:
            os.remove(tmp_path)
        except OSError:
            pass
        raise


def _append_order(order_record):
    with _orders_file_lock():
        orders = _read_orders_unlocked()
        orders.append(order_record)
        _write_orders_atomic(orders)


def _read_orders():
    with _orders_file_lock():
        return _read_orders_unlocked()


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
    if not _valid_quantity(quantity):
        return {"error": f"quantity must be a positive integer no greater than {MAX_ITEM_QUANTITY}"}

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
    _recompute_total(order)
    order["summary_shown"] = False

    return {"added": {"name": name, "quantity": quantity, "option": option}, "cart": order}


def _modify_item(session_id, tool_input):
    name = tool_input.get("name")
    quantity = tool_input.get("quantity")
    option = tool_input.get("option")

    if not isinstance(name, str) or not name.strip():
        return {"error": "name is required"}
    if quantity is None and option is None:
        return {"error": "quantity and/or option is required to modify the item"}
    if quantity is not None and not _valid_quantity(quantity):
        return {"error": f"quantity must be a positive integer no greater than {MAX_ITEM_QUANTITY}"}

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

    _recompute_total(order)
    order["summary_shown"] = False

    return {"modified": cart_item, "cart": order}


def _remove_item(session_id, tool_input):
    name = tool_input.get("name")
    quantity = tool_input.get("quantity")

    if not isinstance(name, str) or not name.strip():
        return {"error": "name is required"}
    if quantity is not None and not _valid_quantity(quantity):
        return {"error": f"quantity must be a positive integer no greater than {MAX_ITEM_QUANTITY}"}

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

    _recompute_total(order)
    order["summary_shown"] = False

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


def _get_recommendations(session_id):
    order = get_session_order(session_id)
    cart_names = {item["name"] for item in order["items"]}
    suggested = session_suggestions.setdefault(session_id, set())

    active = _get_active_menu()
    cart_categories = {
        cat["category"] for cat in active for item in cat["items"] if item["name"] in cart_names
    }

    candidates = []
    for cat in active:
        for item in cat["items"]:
            if item["name"] in cart_names or item["name"] in suggested:
                continue
            # Prefer a different category from what's already in the cart (cross-sell pairing).
            priority = 0 if cat["category"] not in cart_categories else 1
            candidates.append((priority, cat["category"], item))

    candidates.sort(key=lambda c: c[0])
    picks = candidates[:2]

    recommendations = [
        {"name": item["name"], "category": category, "description": item["description"], "price": item["price"]}
        for _priority, category, item in picks
    ]
    for rec in recommendations:
        suggested.add(rec["name"])

    return recommendations


def _active_promotions():
    with open(PROMOTIONS_PATH) as f:
        promotions = json.load(f)
    return [p for p in promotions if p.get("active", False)]


def _apply_promotion(session_id, tool_input):
    promotion_id = tool_input.get("promotionId")

    active = _active_promotions()

    if not promotion_id:
        return {"active_promotions": active}

    promo = next((p for p in active if p["id"] == promotion_id), None)
    if promo is None:
        return {"error": f"'{promotion_id}' is not a recognized active promotion"}

    order = get_session_order(session_id)

    # Some promotions (e.g. happy-hour-20) only discount items from specific categories —
    # a percentage discount must be based on just those items' subtotal, not the whole cart.
    applies_to_categories = promo.get("appliesToCategories")
    if applies_to_categories:
        with open(MENU_PATH) as f:
            menu_categories = json.load(f)
        item_category = {item["name"]: cat["category"] for cat in menu_categories for item in cat["items"]}
        discount_base_items = [i for i in order["items"] if item_category.get(i["name"]) in applies_to_categories]
    else:
        discount_base_items = order["items"]
    discount_base = sum(i["price"] * i["quantity"] for i in discount_base_items)

    discount = promo["discount"]
    if discount["type"] == "percentage":
        discount_amount = _round_currency(discount_base * discount["value"] / 100)
    else:
        discount_amount = _round_currency(discount["value"])

    order["discount"] = {"id": promo["id"], "name": promo["name"], "amount": discount_amount}
    _recompute_total(order)
    order["summary_shown"] = False

    return {"applied": order["discount"], "cart": order}


def _set_pickup_details(session_id, tool_input):
    order = get_session_order(session_id)
    order["order_type"] = "pickup"
    order["summary_shown"] = False
    _recompute_total(order)

    customer_name = tool_input.get("customerName")
    if customer_name is not None:
        if not isinstance(customer_name, str) or not customer_name.strip():
            return {"error": "customerName must be a non-empty string"}
        order["customer"]["name"] = customer_name.strip()

    pickup_time = tool_input.get("pickupTime")
    if pickup_time is not None:
        if not isinstance(pickup_time, str) or not pickup_time.strip():
            return {"error": "pickupTime must be a non-empty string"}
        order["pickup_time"] = pickup_time.strip()

    missing = []
    if not order["customer"]["name"]:
        missing.append("customerName")

    return {
        "order_type": order["order_type"],
        "customer_name": order["customer"]["name"],
        "pickup_time": order["pickup_time"],
        "missing": missing,
    }


def _set_delivery_details(session_id, tool_input):
    order = get_session_order(session_id)
    order["order_type"] = "delivery"
    order["summary_shown"] = False
    _recompute_total(order)

    customer_name = tool_input.get("customerName")
    if customer_name is not None:
        if not isinstance(customer_name, str) or not customer_name.strip():
            return {"error": "customerName must be a non-empty string"}
        order["customer"]["name"] = customer_name.strip()

    phone = tool_input.get("phone")
    if phone is not None:
        if not isinstance(phone, str) or not phone.strip():
            return {"error": "phone must be a non-empty string"}
        order["customer"]["phone"] = phone.strip()

    address = tool_input.get("address")
    if address is not None:
        if not isinstance(address, str) or not address.strip():
            return {"error": "address must be a non-empty string"}
        order["delivery"]["address"] = address.strip()
        order["delivery"]["address_confirmed"] = False

    apartment = tool_input.get("apartment")
    if apartment is not None:
        if not isinstance(apartment, str) or not apartment.strip():
            return {"error": "apartment must be a non-empty string"}
        order["delivery"]["apartment"] = apartment.strip()
        order["delivery"]["address_confirmed"] = False

    instructions = tool_input.get("instructions")
    if instructions is not None:
        if not isinstance(instructions, str) or not instructions.strip():
            return {"error": "instructions must be a non-empty string"}
        order["delivery"]["instructions"] = instructions.strip()

    confirm_address = tool_input.get("confirmAddress")
    if confirm_address is not None:
        if not isinstance(confirm_address, bool):
            return {"error": "confirmAddress must be a boolean"}
        if confirm_address and not order["delivery"]["address"]:
            return {"error": "cannot confirm: no delivery address has been captured yet"}
        order["delivery"]["address_confirmed"] = confirm_address

    missing = []
    if not order["customer"]["name"]:
        missing.append("customerName")
    if not order["customer"]["phone"]:
        missing.append("phone")
    if not order["delivery"]["address"]:
        missing.append("address")

    return {
        "order_type": order["order_type"],
        "customer_name": order["customer"]["name"],
        "phone": order["customer"]["phone"],
        "address": order["delivery"]["address"],
        "apartment": order["delivery"]["apartment"],
        "instructions": order["delivery"]["instructions"],
        "address_confirmed": order["delivery"]["address_confirmed"],
        "missing": missing,
    }


def _get_order_total(session_id):
    order = get_session_order(session_id)
    _recompute_total(order)
    return {
        "subtotal": order["subtotal"],
        "discount": order["discount"]["amount"] if order["discount"] else 0.0,
        "tax": order["tax"],
        "delivery_fee": order["delivery_fee"],
        "total": order["total"],
    }


def _build_order_snapshot(order):
    """The structured shape of an order — shared by the pre-checkout summary shown to the
    customer and the record actually saved, so what gets saved is always exactly what was
    reviewed and confirmed."""
    items = [
        {"name": i["name"], "quantity": i["quantity"], "options": i["options"], "price": i["price"]}
        for i in order["items"]
    ]

    fulfillment = {"type": order["order_type"]}
    if order["order_type"] == "pickup":
        fulfillment["customer_name"] = order["customer"]["name"]
        fulfillment["pickup_time"] = order["pickup_time"]
    elif order["order_type"] == "delivery":
        fulfillment["customer_name"] = order["customer"]["name"]
        fulfillment["phone"] = order["customer"]["phone"]
        fulfillment["address"] = order["delivery"]["address"]
        fulfillment["apartment"] = order["delivery"]["apartment"]
        fulfillment["instructions"] = order["delivery"]["instructions"]
        fulfillment["address_confirmed"] = order["delivery"]["address_confirmed"]

    promotions = [order["discount"]] if order["discount"] else []

    return {
        "items": items,
        "fulfillment": fulfillment,
        "promotions": promotions,
        "pricing": {
            "subtotal": order["subtotal"],
            "discount": order["discount"]["amount"] if order["discount"] else 0.0,
            "tax": order["tax"],
            "delivery_fee": order["delivery_fee"],
            "total": order["total"],
        },
    }


def _get_order_summary(session_id):
    order = get_session_order(session_id)
    _recompute_total(order)
    snapshot = _build_order_snapshot(order)
    order["summary_shown"] = True
    return snapshot


def _save_order(order):
    order_record = {
        "id": str(uuid.uuid4()),
        "timestamp": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "status": "NEW",
        **_build_order_snapshot(order),
    }
    _append_order(order_record)
    return order_record


def _confirm_order(session_id, tool_input):
    confirmed = tool_input.get("confirmed")
    if not isinstance(confirmed, bool):
        return {"error": "confirmed must be a boolean"}

    order = get_session_order(session_id)

    if order["status"] == "confirmed":
        return {"error": "already_confirmed", "message": "This order was already confirmed and saved."}
    if not order["items"]:
        return {"error": "empty_order", "message": "Cannot confirm an order with no items."}

    if not confirmed:
        return {"confirmed": False, "status": order["status"]}

    # The gate: only a summary the customer actually just reviewed can be confirmed. Any
    # order-changing tool call since then clears this, so a stale summary can't be finalized.
    if not order["summary_shown"]:
        return {
            "error": "summary_not_reviewed",
            "message": "Call getOrderSummary and present it to the customer before confirming.",
        }

    _recompute_total(order)
    order_record = _save_order(order)
    order["confirmed"] = True
    order["status"] = "confirmed"

    return {
        "confirmed": True,
        "order_id": order_record["id"],
        "timestamp": order_record["timestamp"],
        "total": order_record["pricing"]["total"],
        "status": order_record["status"],
    }


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
    if name == "getRecommendations":
        return _get_recommendations(session_id)
    if name == "applyPromotion":
        return _apply_promotion(session_id, tool_input)
    if name == "setPickupDetails":
        return _set_pickup_details(session_id, tool_input)
    if name == "setDeliveryDetails":
        return _set_delivery_details(session_id, tool_input)
    if name == "getOrderTotal":
        return _get_order_total(session_id)
    if name == "getOrderSummary":
        return _get_order_summary(session_id)
    if name == "confirmOrder":
        return _confirm_order(session_id, tool_input)
    return {"error": f"unknown tool: {name}"}


@app.get("/health")
def health():
    return jsonify(status="ok")


@app.get("/menu")
def menu():
    return jsonify(_get_active_menu())


def _menu_prices():
    active = _get_active_menu()
    return {item["name"]: item["price"] for cat in active for item in cat["items"]}


@app.post("/order")
def place_order():
    if _rate_limited(f"order:{request.remote_addr}", RATE_LIMIT_ORDER_PER_MINUTE):
        return jsonify(error="too many requests, please slow down"), 429

    data = _get_json_object()
    if data is None:
        return jsonify(error="invalid request body"), 400
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
        if not _valid_quantity(quantity):
            return jsonify(error=f"quantity must be a positive integer no greater than {MAX_ITEM_QUANTITY}"), 400
        # Price always comes from the trusted menu lookup above — any client-supplied
        # "price"/"total" on the request is read nowhere and has no effect.
        price = prices[name]
        total += price * quantity
        order_items.append({"name": name, "quantity": quantity, "price": price})

    order_record = {
        "id": str(uuid.uuid4()),
        "timestamp": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "status": "NEW",
        "items": order_items,
        "total": round(total, 2),
    }
    _append_order(order_record)

    return jsonify(order_id=order_record["id"], items=order_items, total=order_record["total"])


@app.get("/orders")
def list_orders():
    if request.headers.get("X-Staff-Key") is not None and _rate_limited(
        f"staffkey:{request.remote_addr}", RATE_LIMIT_STAFF_PER_MINUTE
    ):
        return jsonify(error="too many requests, please slow down"), 429

    orders = _read_orders()
    if not _is_staff_request():
        orders = [_redact_order(o) for o in orders]
    return jsonify(orders)


@app.post("/orders/<order_id>/status")
def update_order_status(order_id):
    if request.headers.get("X-Staff-Key") is not None and _rate_limited(
        f"staffkey:{request.remote_addr}", RATE_LIMIT_STAFF_PER_MINUTE
    ):
        return jsonify(error="too many requests, please slow down"), 429

    if not _is_staff_request():
        return jsonify(error="unauthorized"), 401

    data = _get_json_object()
    if data is None:
        return jsonify(error="invalid request body"), 400
    new_status = data.get("status")
    if new_status not in ORDER_STATUSES:
        return jsonify(error=f"status must be one of {ORDER_STATUSES}"), 400

    with _orders_file_lock():
        orders = _read_orders_unlocked()
        order = next((o for o in orders if o.get("id") == order_id), None)
        if order is None:
            return jsonify(error="order not found"), 404

        current_status = order.get("status", "NEW")
        if ORDER_STATUSES.index(new_status) != ORDER_STATUSES.index(current_status) + 1:
            return jsonify(error=f"cannot move from {current_status} to {new_status}"), 400

        order["status"] = new_status
        _write_orders_atomic(orders)

    return jsonify(order)


@app.post("/chat")
def chat():
    if _rate_limited(f"chat:{request.remote_addr}", RATE_LIMIT_CHAT_PER_MINUTE):
        return jsonify(error="too many requests, please slow down"), 429

    data = _get_json_object()
    if data is None:
        return jsonify(error="invalid request body"), 400
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

    tools = [
        GET_MENU_TOOL,
        ADD_ITEM_TOOL,
        MODIFY_ITEM_TOOL,
        REMOVE_ITEM_TOOL,
        VIEW_CART_TOOL,
        GET_RECOMMENDATIONS_TOOL,
        APPLY_PROMOTION_TOOL,
        SET_PICKUP_DETAILS_TOOL,
        SET_DELIVERY_DETAILS_TOOL,
        GET_ORDER_TOTAL_TOOL,
        GET_ORDER_SUMMARY_TOOL,
        CONFIRM_ORDER_TOOL,
    ]

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
def add_security_headers(response):
    # Reflect the request's Origin only if it's an explicitly allowed frontend origin —
    # never a bare wildcard. A disallowed/absent Origin simply gets no CORS header at
    # all, which is enough for browsers to block a cross-origin site's JS from reading
    # the response (and, for POST with a JSON body, from ever getting past its own
    # preflight in the first place).
    origin = request.headers.get("Origin")
    if origin in ALLOWED_ORIGINS:
        response.headers["Access-Control-Allow-Origin"] = origin
        response.headers["Vary"] = "Origin"
    response.headers["Access-Control-Allow-Headers"] = "Content-Type, X-Staff-Key"
    response.headers["Access-Control-Allow-Methods"] = "GET, POST"

    # This backend only ever returns JSON (or a plain error), never renders HTML pages
    # itself — these are safe defaults that add no risk of breaking the API responses,
    # and cost nothing since there's no inline script/style to allow-list here.
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "no-referrer"
    response.headers["Permissions-Policy"] = "geolocation=(), camera=(), microphone=()"
    response.headers["Content-Security-Policy"] = "default-src 'none'"
    return response


@app.errorhandler(Exception)
def handle_unexpected_error(e):
    # Let Flask's normal handling take care of standard HTTP errors (404, 405, etc.) —
    # only an actually-unexpected exception should be caught and generalized here.
    if isinstance(e, HTTPException):
        return e
    app.logger.exception("Unhandled exception while handling %s %s", request.method, request.path)
    return jsonify(error="internal server error"), 500


if __name__ == "__main__":
    app.run(port=int(os.environ.get("PORT", 5000)), debug=DEBUG)
