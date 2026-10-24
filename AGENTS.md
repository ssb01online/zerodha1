# Agent instructions

Read-only tooling on Zerodha Kite Connect (Python, `pykiteconnect`), managed with `uv`.

## Scope
- In scope: portfolio/positions, orders/trades history, historical candle downloads.
- Out of scope: order placement, modification, cancellation, GTT, WebSocket/KiteTicker. Do not call `place_order`, `modify_order`, `cancel_order`, `place_gtt`, or any other write API, even in tests or examples.

## Where to look (in this order)
1. `ref/kite/notes.md`: verified gotchas and project decisions.
2. Installed SDK source: `.venv/Lib/site-packages/kiteconnect/connect.py` (methods, constants, docstrings for the pinned version).
3. `ref/kite/docs/` (official API docs) and `ref/kite/examples/` (official SDK examples). Generated files; refresh with `uv run --group docs scripts/refresh_docs.py`, do not hand-edit.
4. Online only to verify or fill gaps: https://kite.trade/docs/connect/v3/ (append `index.md` to a page path for Markdown).

## Secrets and sessions
- Credentials live in the gitignored `.env/` folder (`config.props.txt`, `api_secret.txt`); session token in `.env/access_token.json`.
- Never print, log, commit, or copy `api_secret` or `access_token` values. Never ask for the Zerodha password or TOTP, and never automate the Kite web login.
- Get an authenticated client with `kitetools.auth.get_kite()`. If the session is missing/expired, ask the user to run `uv run kitetools login`.

## Environment (Windows)
- Norton Antivirus intercepts HTTPS. Python code must import `kitetools` (which calls `truststore.inject_into_ssl()`) before making requests; uv needs `UV_SYSTEM_CERTS=1` for downloads.
- Respect rate limits (historical: 3 req/s, others: 10 req/s).
