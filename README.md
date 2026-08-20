# CafeBot

A café web app.

## Project Structure

- `frontend/` — client-side code (`index.html`, `styles.css`, `app.js`)
- `backend/` — server-side code
- `data/` — data files
- `prompts/` — prompt templates

## Setup

1. Install backend dependencies:
   ```
   pip install -r backend/requirements.txt
   ```
2. Copy `.env.example` to `.env` and fill in a real Anthropic API key:
   ```
   cp .env.example .env
   ```
   Set `PORT=3000` in `.env` — `frontend/app.js` is hardcoded to call the
   backend at `http://localhost:3000`.

## Running locally

Start the backend:
```
python backend/app.py
```

In a separate terminal, serve the frontend:
```
cd frontend && python -m http.server 8000
```
Then open `http://localhost:8000/index.html` in a browser.

Note: `data/orders.json` is file-based storage meant for local development
only — see the comment above `ORDERS_PATH` in `backend/app.py`.
