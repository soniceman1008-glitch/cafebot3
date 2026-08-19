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

  async function showMenu() {
    try {
      const res = await fetch(`${API_BASE}/menu`);
      if (!res.ok) throw new Error("menu request failed");
      const categories = await res.json();
      const text = categories
        .map((cat) => {
          const items = cat.items
            .map((item) => `  ${item.name} — $${item.price.toFixed(2)}`)
            .join("\n");
          return `${cat.category}\n${items}`;
        })
        .join("\n\n");
      appendMessage(text, "bot");
    } catch (err) {
      appendMessage("Sorry, I couldn't load the menu right now.", "bot");
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
      appendMessage("Hi! I'm CafeBot. My AI brain isn't connected yet.", "bot");
    }
  });
});
