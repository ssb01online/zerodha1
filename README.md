# kitetools

Read-only helpers for [Zerodha Kite Connect](https://kite.trade/docs/connect/v3/): authenticate once per trading day, then call portfolio / history APIs from Python.

This project does **not** place, modify, or cancel orders.

---

## One-time setup

Do these steps once on each machine.

### 1. Environment variable (Windows)

Norton Antivirus intercepts HTTPS and breaks `uv` package downloads unless system certs are used. Set this **user** environment variable permanently:

| Variable | Value |
| --- | --- |
| `UV_SYSTEM_CERTS` | `1` |

PowerShell (current user, persists across reboots):

```powershell
[System.Environment]::SetEnvironmentVariable("UV_SYSTEM_CERTS", "1", "User")
```

Open a **new** terminal after setting it so `uv` picks it up. Confirm with:

```powershell
echo $env:UV_SYSTEM_CERTS
```

### 2. Install uv and project deps

Install [uv](https://docs.astral.sh/uv/) if needed, then from this repo:

```powershell
uv sync
```

### 3. Kite Connect app + credentials

1. Create an app at the [Kite Connect developer console](https://developers.kite.trade/).
2. Set the app **Redirect URL** to a free localhost port, e.g. `http://127.0.0.1:5050/callback`  
   (port `5000` is often already taken on Windows).
3. Put secrets in the gitignored `.env/` folder (create the folder if missing):

**`.env/config.props.txt`**

```text
api_key=YOUR_API_KEY
redirect_url=http://127.0.0.1:5050/callback
```

**`.env/api_secret.txt`**

```text
YOUR_API_SECRET
```

Use the **same** redirect URL in the developer console and in `config.props.txt`. Never commit `.env/`.

---

## Every morning (refresh the session)

Kite `access_token`s expire at **6:00 AM IST** the next calendar day (or sooner if you log out of Kite). Before using any scripts for the day:

```powershell
cd J:\src\cursor\zerodha1
uv run kitetools login
```

What happens:

1. Your browser opens the Kite login page (or the URL is printed).
2. Sign in with your Zerodha credentials and TOTP as usual.
3. After success, Kite redirects to your localhost callback.
4. The token is saved under `.env/access_token.json` for the rest of the day.

Check that it worked:

```powershell
uv run kitetools whoami
```

If you see a session-expired / not-logged-in message later, run `login` again.

### Do I need something always listening on the redirect port?

**No.** You do **not** leave a server running on that port overnight or all day.

`uv run kitetools login` starts a short-lived local listener **only while that command is waiting** for the browser redirect. As soon as the callback arrives (or you Ctrl+C), the port is released. Between logins, nothing needs to bind `5050` (or whichever port you chose).

If login fails with “Cannot listen on …”, something else is using that port — pick another free port and update it in **both** the Kite developer console and `.env/config.props.txt`.

### Manual paste (optional)

If your redirect URL is not localhost (e.g. `https://kite.trade`), or you already finished login in the browser:

```powershell
uv run kitetools login --url "PASTE_FULL_REDIRECT_URL_OR_request_token"
```

---

## Using the session from Python

```python
import kitetools  # injects system SSL trust (needed with Norton)
from kitetools.auth import get_kite

kite = get_kite()
print(kite.profile())
print(kite.holdings())
```

Always `import kitetools` (or `from kitetools...`) before other HTTPS clients so SSL works on this machine.

---

## OHLCV package (NFO candles)

Workspace member for F&O price/volume download planning and (later) Parquet storage. After `uv sync --all-packages`:

```powershell
uv run ohlcv sync          # archive instruments master + upsert NFO contracts into catalog
uv run ohlcv rank          # rank underlyings by nearest-FUT day traded value (needs login)
uv run ohlcv queue         # print expiry-first download queue with request estimates
uv run ohlcv queue --expiry 20261027 --limit 20
uv run python -m ohlcv.selftest
```

Data lives under gitignored `data/ohlcv/nfo/` (`catalog.duckdb`) and `data/instruments/archive/`. Minute candle downloads are Phase 3. Notes: `ref/kite/notes.md` (OHLCV Phase 1).

---

## Portfolio package

Workspace member for samples, sparse composition snapshots, FastAPI JSON API, and a React SPA (holdings / mutual funds / positions). After `uv sync --all-packages`:

```powershell
uv run portfolio sample      # fetch holdings + positions + mf_holdings → data/samples/<IST timestamp>/
uv run portfolio summarize   # field/qty report for the latest sample (or pass a folder name)
uv run portfolio capture     # write a DuckDB snapshot only when composition changed
uv run portfolio snapshots   # list stored snapshots
uv run portfolio serve       # FastAPI on http://127.0.0.1:8765 (serves UI when ui/dist exists)
```

### UI — single-process (after build)

```powershell
cd ui
$env:NODE_OPTIONS="--use-system-ca"   # Norton / system CA on this machine
npm install
npm run build
cd ..
uv run portfolio serve
```

Open [http://127.0.0.1:8765](http://127.0.0.1:8765) — routes: `/portfolio` (live), `/snapshots`, `/snapshots/:id`. Health includes `ui_dist_exists`.

### UI — dev (two terminals)

```powershell
# Terminal 1
uv run portfolio serve

# Terminal 2
cd ui
$env:NODE_OPTIONS="--use-system-ca"
npm run dev
# http://127.0.0.1:5173 — proxies /api → :8765
```

API: [http://127.0.0.1:8765/api/health](http://127.0.0.1:8765/api/health), `/api/portfolio/live`, `POST /api/portfolio/capture`.

Requires a valid session (`uv run kitetools login`). Samples and `data/portfolio.duckdb` stay under gitignored `data/`. Verified quantity rules and API notes live in `ref/kite/notes.md`. Offline checks: `uv run python -m portfolio.selftest`.
