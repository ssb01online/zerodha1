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
- Candle = `[timestamp, open, high, low, close, volume(, oi)]`, timestamps in IST (+0530).
- Instrument master only lists live contracts; F&O tokens change every expiry. Cache `instruments()` daily to keep tokens for expired contracts.
- `continuous=1` works only for NFO/MCX futures and returns day candles only.
- Max date range per request per interval is not in the docs; confirm before designing the chunked downloader.

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
