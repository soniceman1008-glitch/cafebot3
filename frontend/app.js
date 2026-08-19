// CafeBot frontend app logic

const API_BASE = "http://localhost:3000";

document.addEventListener("DOMContentLoaded", () => {
  const toggle = document.getElementById("chat-toggle");
  const closeBtn = document.getElementById("chat-close");
  const chatWindow = document.getElementById("chat-window");
  const log = document.getElementById("chat-log");
  const form = document.getElementById("chat-form");
  const input = document.getElementById("chat-input");
  const menuBtn = document.getElementById("chat-menu-btn");
  const cartBar = document.getElementById("chat-cart");
  const cartSummary = document.getElementById("chat-cart-summary");
  const placeOrderBtn = document.getElementById("chat-place-order");
  const ordersBtn = document.getElementById("chat-orders-btn");
  const allOrdersBtn = document.getElementById("chat-all-orders-btn");

  let order = [];
  let placedOrders = [];
  let chatHistory = [];
  const MAX_HISTORY = 20;

  function setOpen(open) {
    chatWindow.hidden = !open;
    toggle.setAttribute("aria-expanded", String(open));
    if (open) input.focus();
  }

  toggle.addEventListener("click", () => setOpen(chatWindow.hidden));
  closeBtn.addEventListener("click", () => setOpen(false));

  function appendMessage(text, sender) {
    const bubble = document.createElement("div");
    bubble.className = `chat-msg ${sender}`;
    bubble.textContent = text;
    log.appendChild(bubble);
    log.scrollTop = log.scrollHeight;
  }

  function renderCart() {
    if (order.length === 0) {
      cartBar.hidden = true;
      return;
    }
    const totalItems = order.reduce((sum, o) => sum + o.quantity, 0);
    const total = order.reduce((sum, o) => sum + o.price * o.quantity, 0);
    cartSummary.textContent = `${totalItems} item${totalItems === 1 ? "" : "s"} — $${total.toFixed(2)}`;
    cartBar.hidden = false;
  }

  function addToOrder(item) {
    const existing = order.find((o) => o.name === item.name);
    if (existing) {
      existing.quantity += 1;
    } else {
      order.push({ name: item.name, price: item.price, quantity: 1 });
    }
    renderCart();
    appendMessage(`Added ${item.name} to your order.`, "bot");
  }

  function appendMenu(categories) {
    const wrapper = document.createElement("div");
    wrapper.className = "chat-msg bot";
    categories.forEach((cat) => {
      const catEl = document.createElement("div");
      catEl.className = "chat-menu-category";
      catEl.textContent = cat.category;
      wrapper.appendChild(catEl);
      cat.items.forEach((item) => {
        const row = document.createElement("div");
        row.className = "chat-menu-item";

        const label = document.createElement("span");
        label.textContent = `${item.name} — $${item.price.toFixed(2)}`;

        const addBtn = document.createElement("button");
        addBtn.type = "button";
        addBtn.className = "chat-menu-add";
        addBtn.textContent = "Add";
        addBtn.addEventListener("click", () => addToOrder(item));

        row.appendChild(label);
        row.appendChild(addBtn);
        wrapper.appendChild(row);
      });
    });
    log.appendChild(wrapper);
    log.scrollTop = log.scrollHeight;
  }

  async function showMenu() {
    try {
      const res = await fetch(`${API_BASE}/menu`);
      if (!res.ok) throw new Error("menu request failed");
      const categories = await res.json();
      appendMenu(categories);
    } catch (err) {
      appendMessage("Sorry, I couldn't load the menu right now.", "bot");
    }
  }

  async function placeOrder() {
    if (order.length === 0) return;
    try {
      const res = await fetch(`${API_BASE}/order`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          items: order.map((o) => ({ name: o.name, quantity: o.quantity })),
        }),
      });
      if (!res.ok) throw new Error("order request failed");
      const data = await res.json();
      appendMessage(
        `Order placed! Confirmation #${data.order_id.slice(0, 8)} — total $${data.total.toFixed(2)}. Thank you!`,
        "bot"
      );
      placedOrders.push({ id: data.order_id, items: data.items, total: data.total });
      order = [];
      renderCart();
    } catch (err) {
      appendMessage("Sorry, I couldn't place your order right now.", "bot");
    }
  }

  placeOrderBtn.addEventListener("click", () => placeOrder());

  function appendOrderList(orders) {
    const wrapper = document.createElement("div");
    wrapper.className = "chat-msg bot";
    orders.forEach((o) => {
      const block = document.createElement("div");
      block.className = "chat-order";

      const header = document.createElement("div");
      header.className = "chat-order-id";
      header.textContent = `Order #${o.id.slice(0, 8)}`;
      block.appendChild(header);

      o.items.forEach((item) => {
        const row = document.createElement("div");
        row.className = "chat-menu-item";
        row.textContent = `${item.name} x${item.quantity} — $${(item.price * item.quantity).toFixed(2)}`;
        block.appendChild(row);
      });

      const total = document.createElement("div");
      total.className = "chat-order-total";
      total.textContent = `Total: $${o.total.toFixed(2)}`;
      block.appendChild(total);

      wrapper.appendChild(block);
    });
    log.appendChild(wrapper);
    log.scrollTop = log.scrollHeight;
  }

  function showOrderHistory() {
    if (placedOrders.length === 0) {
      appendMessage("You haven't placed any orders yet.", "bot");
      return;
    }
    appendOrderList(placedOrders);
  }

  ordersBtn.addEventListener("click", () => showOrderHistory());

  async function showAllOrders() {
    try {
      const res = await fetch(`${API_BASE}/orders`);
      if (!res.ok) throw new Error("orders request failed");
      const orders = await res.json();
      if (orders.length === 0) {
        appendMessage("No orders have been placed yet.", "bot");
        return;
      }
      appendOrderList(orders);
    } catch (err) {
      appendMessage("Sorry, I couldn't load orders right now.", "bot");
    }
  }

  allOrdersBtn.addEventListener("click", () => showAllOrders());

  function showTyping() {
    const bubble = document.createElement("div");
    bubble.className = "chat-msg bot";
    bubble.textContent = "CafeBot is typing…";
    log.appendChild(bubble);
    log.scrollTop = log.scrollHeight;
    return bubble;
  }

  function hideTyping(bubble) {
    if (bubble && bubble.parentNode) {
      bubble.parentNode.removeChild(bubble);
    }
  }

  async function sendChat(text) {
    const typingBubble = showTyping();
    try {
      const res = await fetch(`${API_BASE}/chat`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ message: text, history: chatHistory }),
      });
      if (!res.ok) throw new Error("chat request failed");
      const data = await res.json();
      hideTyping(typingBubble);
      appendMessage(data.reply, "bot");
      chatHistory.push({ role: "user", content: text });
      chatHistory.push({ role: "assistant", content: data.reply });
      if (chatHistory.length > MAX_HISTORY) {
        chatHistory = chatHistory.slice(-MAX_HISTORY);
      }
    } catch (err) {
      hideTyping(typingBubble);
      appendMessage("Sorry, I'm having trouble responding right now.", "bot");
    }
  }

  menuBtn.addEventListener("click", () => showMenu());

  form.addEventListener("submit", (e) => {
    e.preventDefault();
    const text = input.value.trim();
    if (!text) return;
    appendMessage(text, "user");
    input.value = "";
    if (/menu/i.test(text)) {
      showMenu();
    } else {
      sendChat(text);
    }
  });
});
