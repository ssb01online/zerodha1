# Kite Connect notes (verified against ref/kite/docs)

## Auth
- `request_token` lives only a few minutes; exchange it immediately (`user.md`).
- `access_token` expires at 6 AM IST the next day, or earlier on logout / master logout from Kite Web (`user.md`).
- Redirect URL `https://kite.trade` works but needs a manual paste of the redirected URL. A local URL such as `http://127.0.0.1:5050/callback` (set both in the developer console and `.env/config.props.txt`) lets `kitetools login` capture the token automatically.
- On this machine port 5000 on 127.0.0.1 is taken by `help-server-win`; use 5050.

## Rate limits (`exceptions.md`)
| Endpoint | Limit |
| --- | --- |
| Quote | 1 req/s |
| Historical candles | 3 req/s |
| Everything else | 10 req/s |
- HTTP 403 = session expired/invalid, relogin. HTTP 429 = rate limited, back off.

## Orders and trades
- `orders()` and `trades()` return the current day only; the order book is transient (`orders.md`). Past trades come from the Console tradebook export (manual download), going forward from a daily snapshot.

## Historical data (`historical.md`)
- `historical_data(instrument_token, from_date, to_date, interval, continuous=False, oi=False)`; intervals: minute, 3minute, 5minute, 10minute, 15minute, 30minute, 60minute, day.
- Candle = `[timestamp, open, high, low, close, volume(, oi)]`, timestamps in IST (+0530). SDK returns dicts (`date`, `open`, `high`, `low`, `close`, `volume`, optional `oi`).
- Instrument master only lists live contracts; F&O tokens change every expiry. Cache `instruments()` daily to keep tokens for expired contracts.
- `continuous=1` works only for NFO/MCX futures and returns day candles only.
- Max date range (verified Phase 0 probe `data/ohlcv/nfo/_probe/20261025-213003.json`, token NIFTY 50):
  - **minute: 60 days** (API error text: `interval exceeds max limit: 60 days`). Inclusive calendar span of 61 days still returned OK once; design chunk size = **60**.
  - **day: ≥2001 days** (all probe spans succeeded; true upper bound not hit).

## Historical probe (Phase 0)

CLI: `uv run ohlcv probe` (report under `data/ohlcv/nfo/_probe/`). Re-test expired tokens after 27 Oct 2026: `uv run ohlcv probe --expired` (uses `watch_list` from latest report).

Verified 2026-10-25 (Sunday, IST) against live Kite:

| Check | Result |
| --- | --- |
| Minute max span | 60 days (API); treat as hard limit for chunking |
| Day max span | ≥2001 days |
| OI (`oi=True`) | Present on minute and day for NFO FUT; field varies across bars |
| Session timestamps | Minute bars start `09:15` IST; liquid NFO often runs to `15:30`–`15:39` (not just 15:29). Expect ~376–385 rows/day when dense |
| Sparsity (liquid ATM CE) | Dense ~385 rows/day, almost no zero-volume bars |
| Sparsity (far OTM stock CE) | Sparse (57–275 rows/day); many gaps **and** many zero-volume bars — empty minutes are often omitted, not filled |
| Pre-listing | Day request before first trade returns **empty list**, not an error (`RELIANCE26DECFUT` first day 2026-09-30) |
| Day vs minute volume/OI | Do **not** require exact equality (NIFTY Oct FUT 2026-10-23: day vol 5 761 080 vs minute sum 5 527 730; day OI ≠ last-minute OI) |
| Latency | Historical median ~0.19s (p95 ~1.2s) at ≤3 req/s pacing |
| Quote (500 NFO, weekend) | All keys returned; `last_trade_time` / `timestamp` present; `volume` and `average_price` were **0** on Sunday while `oi` / `last_price` / `ohlc` remained. `ohlc.close` ≠ `last_price` (previous close). Trade date for a snapshot = date of `last_trade_time`, not calendar today |

Phase 3 fetch decision from this probe:
- 60-day minute backfill fits in **one** request per contract → day-candle prefetch is **not** required for chunking.
- Still useful later for “which days traded” screens, but optional for the first minute downloader.
- Take `quote()` daily snapshots on a **trading day after close** (weekend volume is cleared).
- Pending: `ohlcv probe --expired` on/after 2026-10-28 for watch_list tokens (NIFTY/RELIANCE 2026-10-27 series).

## OHLCV Phase 1 (foundation + download queue)

Workspace package `ohlcv`. Offline check: `uv run python -m ohlcv.selftest`.

### Commands
| Command | Needs login | Purpose |
| --- | --- | --- |
| `uv run ohlcv sync` | only if master cache must refresh from Kite | Archive `data/instruments/master.json` → `data/instruments/archive/yyyymmdd.json.gz`; upsert NFO rows into `data/ohlcv/nfo/catalog.duckdb` |
| `uv run ohlcv rank [--force]` | yes | Rank underlyings by nearest-FUT **day** candle `volume * close` (weekend-safe; quote volume is 0 on Sundays) |
| `uv run ohlcv queue [--expiry] [--underlying] [--limit]` | only if sync must refresh master | Print series queue: expiry asc, traded_value desc, unranked last; request/time estimates |
| `uv run ohlcv probe` | yes | Phase 0 measurements (unchanged) |

### Catalog (`data/ohlcv/nfo/catalog.duckdb`, schema_version=1)
Tables: `meta`, `contracts`, `fetch_state` (watermarks; Phase 3 writes), `underlying_rank`, `runs`.
Single-writer: only one process should open the catalog for writes; a lock error means close notebooks / other `ohlcv` runs.

### Universe
- Live: nearest **3** expiries per NFO underlying with `expiry >= today`.
- Plus **unfinished expired** series still in `contracts` whose minute `fetch_state` is missing / not `done` through expiry-day 15:40 IST (so a series can be finished after it drops off the live master).

### Ranking
Uses historical day candles of each underlying’s nearest live FUT (not `quote()`), because weekend quote `volume` / `average_price` are cleared. Phase 2 may switch to quote-snapshot traded value on trading days.

### Queue estimates
- Default backfill = **60 inclusive calendar days** → **1** minute request per contract when chunk size is 60 (Phase 0).
- Time ≈ `requests * max(0.34s pace, 0.19s Phase-0 median latency)`.
- Partial watermarks roll back to 09:15 IST of that day before counting chunks.

## Portfolio data (verified 2026-10-25 sample `data/samples/20261025-121155`)
APIs: `holdings()`, `positions()` → `{net, day}`, `mf_holdings()`. No historical portfolio or demat txn API.

### Holdings (equity CNC demat)
- Fields seen: standard docs fields plus `las_quantity`, `authorisation`, nested `mtf.{quantity,used_quantity,average_price,value,initial_margin}`.
- Identity verified on this account: `quantity + t1_quantity + collateral_quantity == opening_quantity` (28/28). Also `quantity == realised_quantity`.
- Pledged stock still counts as owned: several rows had `quantity=0` with `collateral_quantity == opening_quantity` (e.g. fully pledged HDFCBANK, RELIANCE). Fingerprint **must** include collateral or those disappear.
- `quantity + t1_quantity == opening_quantity` alone fails when collateral is non-zero.
- No multi-exchange duplicate ISINs in this sample; still key equity by **ISIN** (symbols/exchanges can rename or dual-list).
- `t1_quantity` / `used_quantity` / `mtf.quantity` / `short_quantity` / `las_quantity` were all zero in this off-market sample — re-check on a delivery buy/sell day.

### Positions
- F&O overnight stays in `positions` (product `NRML`), never moves to holdings. Sample: 14 NFO/NRML rows, all with `overnight_quantity == quantity` (signed; shorts negative), `multiplier=1`.
- Equity delivery taken today would appear as CNC in positions and move to holdings after settlement — none in this sample (`day` empty; weekend).
- Key derivatives / MIS by `exchange:tradingsymbol` (add `:product` if the same symbol appears under more than one product).
- `positions()` has no `expiry`. Enrich from the shared `instruments` workspace package (full daily dump at `data/instruments/master.json`, IST calendar day via `kite.instruments()`, scoped to NSE/BSE/NFO). Portfolio uses slim `get_meta(token)` → `{expiry, instrument_type}`. Normalized / API rows add nullable `expiry` (`YYYY-MM-DD`) and `instrument_type` (`EQ`/`FUT`/`CE`/`PE`/…). Snapshot reads rebuild via `to_view(raw)` and re-join today’s master — contracts rolled off the dump may show `expiry: null` on old snapshots until a later phase reads denormalized `snapshot_positions` columns only.

### Mutual funds (`mf_holdings`)
- Empty on this account (0 rows). Treat key as `isin:folio`, units rounded to 3 decimals, until a non-empty sample revises it.

### Snapshot fingerprint (frozen for Phase 1)
Trigger a new snapshot only when this composition map changes (price-only changes do not count). Drop entries whose quantity is 0 after aggregation.

| Book | Key | Quantity |
| --- | --- | --- |
| Equity holdings | `isin` | `quantity + t1_quantity + collateral_quantity` |
| Equity CNC in positions.net | map onto same `isin` when known, else `exchange:tradingsymbol:CNC` | net `quantity` (signed) |
| F&O / MIS positions.net | `exchange:tradingsymbol:product` | net `quantity` (signed) |
| MF holdings | `isin:folio` | `round(quantity, 3)` |

Notes:
- `opening_quantity` equals owned qty at day open but does not move with same-day buys/sells; use the sum above for live fingerprints.
- `used_quantity` is not added back (docs: sold from net holding; appears already reflected in `quantity` when non-zero — confirm on a sell day).
- Snapshot `observed_at` = fetch time; `effective_date` of the true trade stays null until tradebook reconstruction.

## Snapshots (Phase 1)

- Formula lives only in `portfolio.fingerprint` (keys prefixed `EQ:` / `POS:` / `MF:`).
- DuckDB path: `data/portfolio.duckdb` (gitignored via `data/`). Schema version `2` in `meta.schema_version` (v1→v2 soft-migrates `snapshot_positions` with `expiry` / `instrument_type`).
- Tables: `snapshots` (incl. ISO `observed_at`, `raw_json`, `fingerprint_hash`), `snapshot_composition`, `snapshot_holdings`, `snapshot_mf_holdings`, `snapshot_positions`.
- Capture writes **only** when the fingerprint hash differs from the latest snapshot. Prices at capture are stored as metadata, never as a trigger.
- Single-writer assumption: only one process should write the DB. Concurrent DuckDB writers fail on Windows file locks.
- CLI: `uv run portfolio capture [--from-sample] [--db PATH]`, `snapshots`, `show ID`. Self-test: `uv run python -m portfolio.selftest`.
- Open verification (first real buy/sell day): confirm `used_quantity` behaviour and CNC equity rows in `positions.net` merge onto the holding ISIN as expected.

## API (Phase 2)

- Serve: `uv run portfolio serve` → `http://127.0.0.1:8765` (bind loopback only). One port for portfolio + instruments.
- Portfolio endpoints: `GET /api/health`, `GET /api/portfolio/live?refresh=`, `POST /api/portfolio/capture`, `GET /api/portfolio/snapshots`, `GET /api/portfolio/snapshots/{id}`.
- Instruments endpoints (router from workspace package `instruments`, dump-only): 
  - `GET /api/instruments/meta`
  - `GET /api/instruments/search?q=&exchange=&instrument_type=&limit=&offset=`
  - `GET /api/instruments/by-token/{token}`
  - `GET /api/instruments/nfo/underlyings?q=&limit=`
  - `GET /api/instruments/nfo/expiries?underlying=`
  - `GET /api/instruments/nfo/chain?underlying=&expiry=`
- `/api/health` may include `instruments_as_of` when the master cache is already loaded in-process (does not trigger a refresh).
- Live Kite responses are cached ~10s (errors never cached). Capture prefers the fresh cache so UI auto-capture after live does not double-fetch.
- API opens DuckDB per call under a process lock (releases the file between requests so CLI reads can still work). Prefer capturing via the API while `serve` is running.
- Token missing/expired → HTTP 401 `{error: token_invalid, message, steps}` with `uv run kitetools login` instructions. CORS allows Vite `http://127.0.0.1:5173` / `localhost:5173` only.
- Instruments self-test (offline): `uv run python -m instruments.selftest`.

## API and UI (Phase 3)

Dev UI lives in `ui/` (React 19 + Vite 6 + TypeScript).

### Run (two terminals)

```powershell
# Terminal 1 — API
uv run portfolio serve
# http://127.0.0.1:8765

# Terminal 2 — Vite (Norton SSL: use system CAs)
cd ui
$env:NODE_OPTIONS="--use-system-ca"
npm install
npm run dev
# http://127.0.0.1:5173 — proxies /api → :8765
```

### Behaviour
- Live page (`/portfolio`): `GET /api/portfolio/live` then auto `POST /api/portfolio/capture` once per load (StrictMode-guarded). Refresh button forces `?refresh=true` then capture again.
- Capture soft-fails in the status bar (`db_busy`, etc.) without hiding portfolio data.
- 401 → token panel with the server's `steps` (run `uv run kitetools login`).
- Snapshots list `/snapshots`; detail `/snapshots/:id` shows as-of banner + same tables + composition keys.
- Instruments browser: `/instruments` (Equity | NFO tabs), `/instruments/nfo/:underlying?expiry=`, `/instruments/token/:token`. Token always shown/copyable. `last_price` is dump as-of, not live.
- `localStorage`: `portfolio_ui_theme_v1` (`dark`|`light`), `portfolio_ui_tab_v1` (`holdings`|`mf`|`positions`), `portfolio_ui_pos_filters_v1` (`{ kind: all|equity|fo, expiryKey: all|unknown|YYYY-MM-DD }` — Positions toolbar kind/expiry filters), `instruments_ui_tab_v1` (`equity`|`nfo`), column prefs `instruments_ui_cols_equity_v1` / `instruments_ui_cols_nfo_v1` / `instruments_ui_cols_chain_v1`.
- Build check: `cd ui; $env:NODE_OPTIONS="--use-system-ca"; npm run build`.

## UI polish and SPA mount (Phase 4)

- Tables: clickable column sort (unsorted → asc → desc); Columns picker with prefs in `localStorage`:
  - `portfolio_ui_cols_holdings_v1`, `portfolio_ui_cols_mf_v1`, `portfolio_ui_cols_positions_v1` (`{ hidden: string[] }`).
  - Identity columns (`symbol` / `fund`) are locked and cannot be hidden.
- Single-process: after `npm run build`, `uv run portfolio serve` mounts `ui/dist` (`/assets` + SPA fallback to `index.html`). `/api/health` reports `ui_dist_exists`. If dist is missing, `/` returns a JSON hint (API-only).
- Open `http://127.0.0.1:8765/portfolio` or `/instruments` (no Vite required when dist is present).

## Instruments browser (Phase 2 app)

- Shared SPA + single `portfolio serve` port; domain code in workspace package `instruments` (`cache`, `query`, `api` router).
- Scope: NSE/BSE equity + NFO. NFO underlying key = dump `name`.
- TODOs (not in this phase): live LTP/OI quotes, MF (`mf_instruments`), CDS, MCX, BFO, NSE/BSE same-scrip pairing, renaming serve to a generic app name.
