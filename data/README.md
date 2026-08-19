# data/

- `menu.json` — menu items served by `GET /menu`.
- `promotions.json` — sample promotions; CafeBot must only apply ones with `active: true`.
- `orders.json` — **temporary, dev-only order storage.** The backend reads and appends to
  this plain JSON file for local development; it is not a database. Revisit before
  production (a real database, per-request writes are not safe under concurrent access).
