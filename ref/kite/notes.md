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

## Historical probe MCX/CDS (Phase 0)

CLI: `uv run python -m ohlcv.probe_mcx_cds` (reports under `data/ohlcv/mcx/_probe/`, `data/ohlcv/cds/_probe/`, combined `data/ohlcv/_probe/mcx_cds_*.json`).

Verified 2026-10-28 (Wednesday, IST) against live Kite (`20261028-180557`):

| Check | MCX | CDS |
| --- | --- | --- |
| Live instruments | ~16 089 (FUT 174, CE/PE ~7 952 each, 11 INDICES as `EQ`) | ~8 973 (FUT 155, CE/PE ~4 409 each); segments `CDS-FUT` / `CDS-OPT` |
| Top underlyings | GOLD, SILVER, SILVERM, CRUDEOIL, … | USDINR, EURINR, GBPINR, JPYINR (+ G-Sec names) |
| Minute max span | **60 days** (same InputException; 61 inclusive OK once) | same |
| Day max span | ≥2001 days | same |
| OI on FUT | Present minute + day | Present minute + day |
| Session (liquid FUT) | **09:00–23:29** IST; ~870 minute rows/day (GOLD) | **09:00–16:59** IST; ~480 minute rows/day (USDINR) |
| MCX indices hist | `MCXGOLDEX` minute only **09:15–15:29** (~303 rows) — truncated vs FUT evening session | n/a (no CDS indices) |
| `continuous=1` day | Works on FUT (~1407 bars / 2000 calendar days on GOLD) | **Also works** on USDINR FUT (~1331 bars) — docs only mention NFO/MCX |
| `continuous=1` minute | `InputException: invalid interval for continuous data` | same |
| Contract-only day | Shorter than continuous (GOLD26DECFUT listed ~2025-12-16) | USDINR26OCTFUT ~1y of day bars |
| Pre-listing | Empty list, not error (`GOLD27OCTFUT`) | Far contract may still be empty |
| Day vs minute vol/OI | Do not require equality (same as NFO) | same |
| Options minute | Liquid GOLD/CRUDEOIL CE: full MCX hours + OI; FUT expiry ≠ option expiry (GOLD FUT monthly vs CE weekly) | Illiquid far OTM can return 0 rows |
| Quote (after CDS close) | Keys return; many `volume`/`average_price` 0; `last_trade_time` epoch when never traded | same |

Implications for downloaders:
- Reuse NFO chunking (minute ≤60d, day ~2000d) but **do not** reuse NFO `09:15–15:40` session bounds.
- Expand `instruments` scope to include `MCX` and `CDS` before catalog sync.
- Prefer per-market data roots (`data/ohlcv/mcx/`, `data/ohlcv/cds/`).
- Archive instruments daily if minute history after expiry is required (no continuous minute).
- Treat MCX index historical as equity-hours only unless re-verified later.

## OHLCV multi-market foundation (Phase 1)

- `instruments.scope.IN_SCOPE_EXCHANGES` includes **MCX** and **CDS** (plus NSE/BSE/NFO). Next `ensure_cache()` refresh rewrites `data/instruments/master.json` with those rows; until then the on-disk master may still be NSE/BSE/NFO-only.
- `ohlcv.markets.MarketProfile` registry: `nfo` (09:15–15:40), `mcx` (09:00–23:30), `cds` (09:00–17:00); all `continuous_day=True`. Helpers: `get_market`, `get_market_by_exchange`.
- `ohlcv.paths.paths_for(market_id)` → `data/ohlcv/<id>/` (`catalog.duckdb`, `day/`, `minute/`, …). Module aliases `OHLCV_DIR` / `CATALOG_PATH` / … remain **NFO**.
- NFO `SESSION_START` / `SESSION_END` sourced from the NFO profile (behaviour unchanged).
- Offline: `uv run python -m instruments.selftest`, `uv run python -m ohlcv.selftest`.

## OHLCV multi-market catalog + sync (Phase 2)

CLI: `uv run ohlcv sync|queue --market nfo|mcx|cds` (default `nfo`).

- `MarketProfile.sync_instrument_types`: NFO `None` (all rows with expiry); MCX/CDS `{"FUT"}` only.
- Per-market catalog: `data/ohlcv/<market>/catalog.duckdb` (same schema as NFO).
- `universe.sync_contracts(..., market=)` filters by exchange + sync types; `select_series(..., market=)` uses that market’s `session_end` for unfinished-expired checks.
- Shared instruments archive still under `data/instruments/archive/` (master must include MCX/CDS after a refresh).
- Universe v1 unchanged: nearest **3** live expiries per underlying + unfinished expired.
- Offline: `uv run python -m ohlcv.selftest` (includes `test_sync_mcx_cds_fut_only`).

## OHLCV multi-market fetch (Phase 3)

CLI: `uv run ohlcv fetch|status|show|queue --market nfo|mcx|cds` (default `nfo`).

- Planner/fetch session bounds come from `MarketProfile` (`market=` on `fetch_end`, chunk windows, watermarks, queue estimates).
- `fetch` writes day/minute parquet under `data/ohlcv/<market>/` and updates that market’s `catalog.duckdb` / logs.
- MCX/CDS sync remains **FUT-only**; same day-then-minute fetch as NFO (minute ≤60d chunks, day ~2000d).
- `status` / `show` open the per-market catalog and series roots.
- **Still NFO-only:** `rank`, `daily` quote snapshots (untraded-token skip is a no-op without snapshots).
- Offline: `uv run python -m ohlcv.selftest` (includes MCX session / chunk / fetch tests).

## OHLCV Phase 1 (NFO foundation + download queue)

Workspace package `ohlcv`. Offline check: `uv run python -m ohlcv.selftest`.

### Commands
| Command | Needs login | Purpose |
| --- | --- | --- |
| `uv run ohlcv sync [--market nfo\|mcx\|cds]` | only if master cache must refresh from Kite | Archive master → `data/instruments/archive/`; upsert into `data/ohlcv/<market>/catalog.duckdb` (NFO all expiry types; MCX/CDS FUT) |
| `uv run ohlcv rank [--force]` | only if no final snapshot (falls back to FUT day) | Rank underlyings (**NFO only**): prefer final quote snapshot; else nearest-FUT day |
| `uv run ohlcv queue [--market] [--expiry] [--underlying] [--limit] [--interval minute\|day] [-d N\|all]` | only if sync must refresh master | Print series queue for market: expiry asc, traded_value desc, unranked last; request/time estimates (`-d` for minute only) |
| `uv run ohlcv probe` | yes | Phase 0 measurements (unchanged) |
| `uv run ohlcv daily [--dry-run\|--show [yyyymmdd]]` | yes (except `--dry-run` / `--show`) | End-of-day quote snapshot (**NFO only**) |
| `uv run ohlcv fetch [--market] […] [-d N\|all]` | yes (except `--dry-run`) | Day (~2000d) then minute candles; `-d` controls minute window only (default 60) |
| `uv run ohlcv status [--market] [--all] [--expiry] [--underlying] [--interval] [--limit] [--runs]` | no | Offline progress report |
| `uv run ohlcv show [--market] UNDERLYING EXPIRY [--interval] [--symbol\|--token] [--tail]` | no | Inspect one minute or day series file |

### Catalog (`data/ohlcv/nfo/catalog.duckdb`, schema_version=2)
Tables: `meta`, `contracts`, `fetch_state` (watermarks; Phase 3 writes), `underlying_rank`, `runs`, `daily_snapshots`.
v1→v2 soft-migrates by creating `daily_snapshots`.
Single-writer: only one process should open the catalog for writes; a lock error means close notebooks / other `ohlcv` runs.

### Universe
- Live: nearest **3** expiries per NFO underlying with `expiry >= today`.
- Plus **unfinished expired** series still in `contracts` whose `fetch_state` for the requested interval (`minute` or `day`) is missing / not `done` through expiry-day 15:40 IST (so a series can be finished after it drops off the live master).

### Ranking
- Prefer `daily/yyyymmdd.parquet` when a **final** snapshot exists: per-underlying `sum(volume * average_price)`, `source=quote`.
- Fallback: historical day candles of each underlying’s nearest live FUT (`volume * close`, `source=fut_day`) — weekend-safe when no snapshot.

### Queue estimates
- Default backfill = **60 inclusive calendar days** → **1** minute request per contract when chunk size is 60 (Phase 0).
- Time ≈ `requests * max(0.34s pace, 0.19s Phase-0 median latency)`.
- Partial watermarks roll back to 09:15 IST of that day before counting chunks.

## OHLCV Phase 2 (daily quote snapshot)

End-of-day `quote()` sweep of **all live NFO** contracts (not just nearest-3 expiries).

### When to run
- On a **trading day after 15:40 IST** (`status=final`). Earlier same-day runs write `status=intraday` and are overwritten later.
- Weekend / holiday / next-morning runs are **refused** (quote `volume` / `average_price` are cleared; trade date ≠ today). Message: take the snapshot on the session day after close.
- A missed day cannot be recovered from `quote()`; use `ohlcv fetch` (day pass) for historical day OHLC+OI per series.

### Layout
- File: `data/ohlcv/nfo/daily/yyyymmdd.parquet` (atomic write via `_staging/`).
- Catalog row: `daily_snapshots` (one per `trade_date`; rerun overwrites).
- Helper for Phase 3: `snapshot.untraded_tokens(trade_date)` → tokens with `volume == 0`.

### Columns
Contract: `trade_date`, `token`, `underlying`, `expiry`, `tradingsymbol`, `instrument_type`, `strike`, `lot_size`.
Prices: `open`/`high`/`low` from `ohlc.*` (**NULL** when `volume=0`); `close` = `last_price`; `prev_close` = `ohlc.close` (previous session).
Activity: `volume`, `average_price`, `traded_value` (`volume * average_price`), `traded`, `oi`, `oi_day_high`, `oi_day_low`, `buy_quantity`, `sell_quantity`, `last_trade_time`.

### Commands
| Command | Purpose |
| --- | --- |
| `uv run ohlcv daily` | Sync → quote all live NFO (~500/batch, 1 req/s) → write parquet → record snapshot → rank (`source=quote`) |
| `uv run ohlcv daily --dry-run` | Contract/batch/time estimate; no quote calls |
| `uv run ohlcv daily --show [yyyymmdd]` | Offline summary of an existing snapshot (default: latest) |

Trade date = date of the latest `last_trade_time` in the response (must equal IST run date). Close notebooks that hold `catalog.duckdb` before running.

## OHLCV Phase 3 + 5 (combined `fetch`)

One CLI run downloads **day** candles first (~2000d, one request/contract), then **minute** candles for the same queue. Separate `fetch_state` rows and parquet trees (`day/` vs `minute/`).

### Command
```powershell
uv run ohlcv fetch [--market nfo|mcx|cds] [--expiry yyyymmdd] [--underlying NIFTY] [-d N|all] [--max-series N] [--max-minutes M] [--dry-run]
```
- Queue: union of unfinished series for day **or** minute; order expiry ascending, then traded-value rank.
- Layout roots: `data/ohlcv/<market>/day|minute|catalog.duckdb` (default market `nfo`).
- Pass 1 = day (`DAY_BACKFILL_DAYS=2000`); pass 2 = minute with `-d` / `--days` (default **60**, or `all`).
- `-d=all`: tighten minute start to the earliest **traded** day bar (`volume>0`) from the series day parquet; skip minute when the day file exists and the token has no traded days.
- Minute API still chunks at **60** calendar days per request when the window is wider.
- `--dry-run` prints day + minute request estimates (including untraded / no-traded-day skips).
- Close notebooks that hold `catalog.duckdb` **or** a series parquet before running (Windows file locks).

### Layout
- Minute: `data/ohlcv/nfo/minute/<UNDERLYING>/yyyymmdd.parquet`
- Day: `data/ohlcv/nfo/day/<UNDERLYING>/yyyymmdd.parquet`
- Columns (both): `underlying`, `expiry`, `token`, `tradingsymbol`, `instrument_type`, `strike`, `lot_size`, `ts`, `open`, `high`, `low`, `close`, `volume`, `oi`.
- `ts` is naive IST. Rows sorted by `(token, ts)`.

### `fetch_state` status
| Status | Meaning |
| --- | --- |
| `done` | Watermark at/after session end (or before open); next run is a no-op until new session data is due |
| `partial` | Fetched during market hours; next run rolls back to 09:15 IST that day (minute) or previous day session end (day) and refreshes |
| `error` | Request rejected / retries exhausted; watermark unchanged; `attempts` incremented |
| `gave_up` | Expired contract failed 3 times — treated as finished so the series drops off the unfinished queue |

Watermark `fetched_to` is the **requested** end (not last candle); sparse contracts often have no bar at the last minute.

### Skip rules (no historical call)
- **Quote untraded**: watermark exists **and** every remaining weekday has a `daily_snapshots` `final` row listing the token as `volume=0`.
- **Day-bar (minute only)**: day parquet present and no `volume>0` days in the needed window (`-d=all` skips with empty traded set even without a minute watermark).
- Weekend-only windows do not auto-skip via quote snapshots.

### Day pass specifics
- Before 15:40 IST, `day_fetch_end` is **previous calendar day** (do not store today’s incomplete bar). After close, include today.
- Day vs `daily/` quote snapshots remain different layouts (see Phase 2 / layout table below).

### Interrupts / locks
- Ctrl+C, `--max-minutes`, `--max-series`: finish the current contract, merge + commit that series, then stop (shared budget across both passes).
- Session expired (HTTP 403 / `TokenException`): commit fetched contracts in the current series, then exit with `uv run kitetools login`.
- 5 consecutive `RetryExhausted` across contracts: commit and abort (likely network down).
- If `os.replace` hits `PermissionError` (file open elsewhere), `fetch_state` is **not** committed; one retry at end of pass after 5s; otherwise re-run later (merge is idempotent).

### Offline check
`uv run python -m ohlcv.selftest` (includes Phase 3/5 fake-kite tests).

## OHLCV Phase 4 (status and read)

Offline visibility and Parquet read helpers. No Kite calls; catalog opened **read-only**.

### Daily routine (after 15:40 IST on a session day)
1. `uv run ohlcv daily` — quote snapshot + rank
2. `uv run ohlcv fetch` — day then minute (use `--max-minutes` / `-d` to budget)
3. `uv run ohlcv status` / `status --interval day` — check completion, snapshot gaps, recent runs

If `status`/`show` fail with a catalog lock message, a `daily`/`fetch` run (or notebook) still holds `catalog.duckdb`; retry after it finishes. On Windows a read-only open fails while another process holds the write connection. Notebooks using `ohlcv.read` open/close DuckDB per call so they do not keep series Parquet files locked between queries.

### Commands
| Command | Purpose |
| --- | --- |
| `uv run ohlcv status [--all] [--expiry] [--underlying] [--interval minute\|day] [--limit] [--runs N]` | Series table (done%/pending/file size), subtotals, latest snapshots + weekday gaps, recent runs |
| `uv run ohlcv show UNDERLYING EXPIRY [--interval minute\|day]` | File path, row/ts span, contracts with data, type breakdown, top-10 by volume |
| `uv run ohlcv show UNDERLYING EXPIRY --symbol TS` / `--token N` | Per-day bars + `fetch_state` + last `--tail` bars (default 5) |

`--all` includes every series with a `minute|day/<UL>/yyyymmdd.parquet` file (sanitised folders like `M_M` map back to catalog `M&M`). Default set is the download universe (`select_series`: live nearest-3 + unfinished expired for that interval).

### Status semantics
- **complete%**: contracts whose watermark reaches `min(interval_fetch_end(now), expiry 15:40 IST)` with status `done`, plus every `gave_up`.
- **pending**: estimated historical requests left (`planner.series_pending_requests(..., interval=)`).
- **snapshot gaps**: weekdays from the first recorded snapshot through the last completed session with no `final` row. **May include market holidays**; a missed quote day cannot be recovered from `quote()`.

### Read helpers (`ohlcv.read`)
- `daily(trade_date)`, `series_daily(underlying, expiry)` — quote snapshots under `daily/` (not historical day series)
- `series(underlying, expiry, *, interval=, tokens=, symbols=, start=, end=)` — minute or day series slices
- `day_path` / `day_series` — historical day series under `day/<UL>/yyyymmdd.parquet`
- `series_rel(con, ...)` — same filters as a DuckDB relation (caller owns `con`)
- `underlying_minute(underlying, ...)` — glob all expiries under `minute/<UL>/`
- `file_info(path)` → `(bytes, rows, min_ts, max_ts)` via Parquet footer metadata
- In-memory DuckDB: `memory_limit=4GB`, modest threads, temp dir under `_staging/duckdb_tmp`

### Offline check
`uv run python -m ohlcv.selftest` (includes Phase 4/5 status/read/day-fetch tests).

## Day vs daily layout (Phase 5 data)

Historical day candles (written by the day pass of `ohlcv fetch`) vs quote snapshots:

| Path | Source | Grain |
| --- | --- | --- |
| `data/ohlcv/nfo/daily/yyyymmdd.parquet` | End-of-day `quote()` snapshot | All live NFO on one trade date |
| `data/ohlcv/nfo/day/<UL>/yyyymmdd.parquet` | `historical_data(..., interval="day")` | One series (expiry in filename) |

`fetch_state` rows use `interval='day'` independent of minute watermarks. Day backfill = **2000** inclusive calendar days → typically **1** request per contract.

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
