# CLAUDE.md

Guidance for Claude Code (and other agents) working in this repository.

## Purpose

CafeBot is a café web app: a simple chat-style assistant that helps customers
with things like browsing the menu, asking questions, and placing orders.

## Architecture Overview

- `frontend/` — client-side code the user's browser loads: `index.html`,
  `styles.css`, `app.js`. Renders the chat UI and talks to the backend.
- `backend/` — server-side code. Receives requests from the frontend,
  applies business logic, and talks to any external services (e.g. an LLM
  API) and to `data/`.
- `data/` — static or persisted data used by the app (e.g. menu items,
  pricing, order records).
- `prompts/` — prompt templates used when calling an LLM, kept separate
  from application code so they can be edited independently.
- `README.md` — project overview and structure.

Flow: browser (`frontend/`) → backend (`backend/`) → data/prompts as needed
→ response back to the browser.

## Coding Rules

- Keep changes minimal and scoped to what the task actually requires — no
  speculative features, abstractions, or refactors "while you're in there."
- Match the existing style and structure of the file you're editing rather
  than introducing a new pattern.
- Don't add comments explaining what code does; only comment non-obvious
  *why* (a workaround, a subtle constraint).
- Don't add error handling, fallbacks, or config options for cases that
  can't happen — validate only at real boundaries (user input, external
  APIs).
- Keep frontend, backend, data, and prompts separated as laid out above;
  don't mix concerns across folders.

## Security Rules

- Never hardcode secrets, API keys, or credentials in source files. Load
  them from environment variables or a secrets manager.
- Never commit `.env` files or anything containing credentials.
- Treat all user input as untrusted: validate and sanitize it on the
  backend before using it in queries, file paths, shell commands, or
  templates.
- Escape/encode any user-generated content rendered in the frontend to
  prevent XSS.
- Don't log sensitive data (secrets, tokens, personal customer info).
- Keep prompt templates in `prompts/` free of embedded secrets, since they
  may be sent to a third-party LLM API.

## Token-Saving Rules

- Read only the files relevant to the current task, not the whole repo.
- Prefer targeted edits (diffs) over rewriting whole files.
- Don't paste large file contents into explanations when a file path and
  line number will do.
- Avoid generating boilerplate, sample data, or documentation that wasn't
  asked for.

## Scope Rule

Only modify the files needed for the current task. Do not touch unrelated
files, folders, or configuration as a side effect of an unrelated change.
