// CafeBot frontend app logic

document.addEventListener("DOMContentLoaded", () => {
  const toggle = document.getElementById("chat-toggle");
  const closeBtn = document.getElementById("chat-close");
  const chatWindow = document.getElementById("chat-window");
  const log = document.getElementById("chat-log");
  const form = document.getElementById("chat-form");
  const input = document.getElementById("chat-input");

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

  form.addEventListener("submit", (e) => {
    e.preventDefault();
    const text = input.value.trim();
    if (!text) return;
    appendMessage(text, "user");
    input.value = "";
    appendMessage("Hi! I'm CafeBot. My AI brain isn't connected yet.", "bot");
  });
});
