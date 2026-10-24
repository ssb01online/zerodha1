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
