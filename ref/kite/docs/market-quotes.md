<!-- source: https://kite.trade/docs/connect/v3/market-quotes/ | fetched: 2026-10-24 | do not edit, regenerate via scripts/refresh_docs.py -->

# Market quotes and instruments

| type | endpoint                               | &nbsp;                                                          |
| ---- | -------------------------------------- | --------------------------------------------------------------- |
| GET  | [/instruments](#instruments)           | Retrieve the CSV dump of all tradable instruments               |
| GET  | [/instruments/:exchange](#instruments) | Retrieve the CSV dump of instruments in the particular exchange |
| GET  | [/quote](#market-quotes)               | Retrieve full market quotes for one or more instruments         |
| GET  | [/quote/ohlc](#retrieving-ohlc-quotes) | Retrieve OHLC quotes for one or more instruments                |
| GET  | [/quote/ltp](#retrieving-ltp-quotes)   | Retrieve LTP quotes for one or more instruments                 |

## Instruments

Between multiple exchanges and segments, there are tens of thousands of different kinds of instruments that trade. Any application that facilitates trading needs to have a master list of these instruments. The instruments API provides a consolidated, import-ready CSV list of instruments available for trading.

### Retrieving the full instrument list

Unlike the rest of the calls that return JSON, the instrument list API returns a gzipped CSV dump of instruments across all exchanges that can be imported into a database. The dump is generated once everyday and hence last_price is not real time.

```shell
curl "https://api.kite.trade/instruments" \
  -H "X-Kite-Version: 3" \
  -H "Authorization: token api_key:access_token"
```

```text
instrument_token, exchange_token, tradingsymbol, name, last_price, expiry, strike, tick_size, lot_size, instrument_type, segment, exchange
408065,1594,INFY,INFOSYS,0,,,0.05,1,EQ,NSE,NSE
5720322,22345,NIFTY15DECFUT,,78.0,2015-12-31,,0.05,75,FUT,NFO-FUT,NFO
5720578,22346,NIFTY159500CE,,23.0,2015-12-31,9500,0.05,75,CE,NFO-OPT,NFO
645639,SILVER15DECFUT,,7800.0,2015-12-31,,1,1,FUT,MCX,MCX
```

### CSV response columns

| column                                | &nbsp;                                                                                  |
| ------------------------------------- | --------------------------------------------------------------------------------------- |
| `instrument_token`<span>string</span> | Numerical identifier used for subscribing to live market quotes with the WebSocket API. |
| `exchange_token`<span>string</span>   | The numerical identifier issued by the exchange representing the instrument.            |
| `tradingsymbol`<span>string</span>    | Exchange tradingsymbol of the instrument                                                |
| `name`<span>string</span>             | Name of the company (for equity instruments)                                            |
| `last_price`<span>float64</span>      | Last traded market price                                                                |
| `expiry`<span>string</span>           | Expiry date (for derivatives)                                                           |
| `strike`<span>float64</span>          | Strike (for options)                                                                    |
| `tick_size`<span>float64</span>       | Value of a single price tick                                                            |
| `lot_size`<span>int64</span>          | Quantity of a single lot                                                                |
| `instrument_type`<span>string</span>  | EQ, FUT, CE, PE                                                                         |
| `segment`<span>string</span>          | Segment the instrument belongs to                                                       |
| `exchange`<span>string</span>         | Exchange                                                                                |

!!! warning

    The instrument list API returns large amounts of data. It's best to request it once a day (ideally at around 08:30 AM) and store in a database at your end.

!!! note

    For storage, it is recommended to use a combination of exchange and tradingsymbol as the unique key, not the numeric instrument token. Exchanges may reuse instrument tokens for different derivative instruments after each expiry.

## Market quotes

The market quotes APIs enable you to retrieve market data snapshots of various instruments. These are snapshots gathered from the exchanges at the time of the request. For realtime streaming market quotes, use the [WebSocket](websocket.md) API.

### Retrieving full market quotes

This API returns the complete market data snapshot of up to **500** instruments in one go. It includes the quantity, OHLC, and Open Interest fields, and the complete bid/ask market depth amongst others.

Instruments are identified by the `exchange:tradingsymbol` combination and are passed as values to the query parameter `i` which is repeated for every instrument. If there is no data available for a given key, the key will be absent from the response. The existence of all the instrument keys in the response map should be checked before to accessing them.

```shell
curl "https://api.kite.trade/quote?i=NSE:INFY" \
  -H "X-Kite-Version: 3" \
  -H "Authorization: token api_key:access_token"
```

```json
{
	"status": "success",
	"data": {
	  "NSE:INFY": {
		"instrument_token": 408065,
		"timestamp": "2021-06-08 15:45:56",
		"last_trade_time": "2021-06-08 15:45:52",
		"last_price": 1412.95,
		"last_quantity": 5,
		"buy_quantity": 0,
		"sell_quantity": 5191,
		"volume": 7360198,
		"average_price": 1412.47,
		"oi": 0,
		"oi_day_high": 0,
		"oi_day_low": 0,
		"net_change": 0,
		"lower_circuit_limit": 1250.7,
		"upper_circuit_limit": 1528.6,
		"ohlc": {
		  "open": 1396,
		  "high": 1421.75,
		  "low": 1395.55,
		  "close": 1389.65
		},
		"depth": {
		  "buy": [
			{
			  "price": 0,
			  "quantity": 0,
			  "orders": 0
			},
			{
			  "price": 0,
			  "quantity": 0,
			  "orders": 0
			},
			{
			  "price": 0,
			  "quantity": 0,
			  "orders": 0
			},
			{
			  "price": 0,
			  "quantity": 0,
			  "orders": 0
			},
			{
			  "price": 0,
			  "quantity": 0,
			  "orders": 0
			}
		  ],
		  "sell": [
			{
			  "price": 1412.95,
			  "quantity": 5191,
			  "orders": 13
			},
			{
			  "price": 0,
			  "quantity": 0,
			  "orders": 0
			},
			{
			  "price": 0,
			  "quantity": 0,
			  "orders": 0
			},
			{
			  "price": 0,
			  "quantity": 0,
			  "orders": 0
			},
			{
			  "price": 0,
			  "quantity": 0,
			  "orders": 0
			}
		  ]
		}
	  }
	}
  }
```

### Response attributes

| attribute                                  | &nbsp;                                                                                                                                                                                                                                |
| ------------------------------------------ | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `instrument_token`<span>uint32</span>      | The numerical identifier issued by the exchange representing the instrument.                                                                                                                                                          |
| `timestamp`<span>string</span>             | The exchange timestamp of the quote packet                                                                                                                                                                                            |
| `last_trade_time`<span>null, string</span> | Last trade timestamp                                                                                                                                                                                                                  |
| `last_price`<span>float64</span>           | Last traded market price                                                                                                                                                                                                              |
| `volume`<span>int64</span>                 | Volume traded today                                                                                                                                                                                                                   |
| `average_price`<span>float64</span>        | The volume weighted average price of a stock at a given time during the day[?](https://support.zerodha.com/category/trading-and-markets/kite-web-and-mobile/kite-mw/articles/what-does-the-average-price-on-kite-3-market-depth-mean) |
| `buy_quantity`<span>int64</span>           | Total quantity of buy orders pending at the exchange                                                                                                                                                                                  |
| `sell_quantity`<span>int64</span>          | Total quantity of sell orders pending at the exchange                                                                                                                                                                                 |
| `open_interest`<span>float64</span>        | Total number of outstanding contracts held by market participants exchange-wide (only F&amp;O)                                                                                                                                        |
| `last_quantity`<span>int64</span>          | Last traded quantity                                                                                                                                                                                                                  |
| `ohlc.open`<span>float64</span>            | Price at market opening                                                                                                                                                                                                               |
| `ohlc.high`<span>float64</span>            | Highest price today                                                                                                                                                                                                                   |
| `ohlc.low`<span>float64</span>             | Lowest price today                                                                                                                                                                                                                    |
| `ohlc.close`<span>float64</span>           | Closing price of the instrument from the last trading day                                                                                                                                                                             |
| `net_change`<span>float64</span>           | The absolute change from yesterday's close to last traded price                                                                                                                                                                       |
| `lower_circuit_limit`<span>float64</span>  | The current lower circuit limit                                                                                                                                                                                                       |
| `upper_circuit_limit`<span>float64</span>  | The current upper circuit limit                                                                                                                                                                                                       |
| `oi`<span>float64</span>                   | The Open Interest for a futures or options contract [?](https://zerodha.com/varsity/chapter/open-interest/)                                                                                                                           |
| `oi_day_high`<span>float64</span>          | The highest Open Interest recorded during the day                                                                                                                                                                                     |
| `oi_day_low`<span>float64</span>           | The lowest Open Interest recorded during the day                                                                                                                                                                                      |
| `depth.buy[].price`<span>float64</span>    | Price at which the depth stands                                                                                                                                                                                                       |
| `depth.buy[].orders`<span>int64</span>     | Number of open BUY (bid) orders at the price                                                                                                                                                                                          |
| `depth.buy[].quantity`<span>int64</span>   | Net quantity from the pending orders                                                                                                                                                                                                  |
| `depth.sell[].price`<span>float64</span>   | Price at which the depth stands                                                                                                                                                                                                       |
| `depth.sell[].orders`<span>int64</span>    | Number of open SELL (ask) orders at the price                                                                                                                                                                                         |
| `depth.sell[].quantity`<span>int64</span>  | Net quantity from the pending orders                                                                                                                                                                                                  |

## Retrieving OHLC quotes

This API returns the OHLC + LTP snapshots of up to **1000** instruments in one go.

Instruments are identified by the `exchange:tradingsymbol` combination and are passed as values to the query parameter `i` which is repeated for every instrument. If there is no data available for a given key, the key will be absent from the response. The existence of all the instrument keys in the response map should be checked before to accessing them.

```shell
curl "https://api.kite.trade/quote/ohlc?i=NSE:INFY&i=BSE:SENSEX&i=NSE:NIFTY+50" \
 	-H "X-Kite-Version: 3" \
  -H "Authorization: token api_key:access_token"
```

```json
{
	"status": "success",
	"data": {
		"NSE:INFY": {
			"instrument_token": 408065,
			"last_price": 1075,
			"ohlc": {
				"open": 1085.8,
				"high": 1085.9,
				"low": 1070.9,
				"close": 1075.8
			}
		}
	}
}
```

### Response attributes

| attribute                             | &nbsp;                                                                       |
| ------------------------------------- | ---------------------------------------------------------------------------- |
| `instrument_token`<span>uint32</span> | The numerical identifier issued by the exchange representing the instrument. |
| `last_price`<span>float64</span>      | Last traded market price                                                     |
| `ohlc.open`<span>float64</span>       | Price at market opening                                                      |
| `ohlc.high`<span>float64</span>       | Highest price today                                                          |
| `ohlc.low`<span>float64</span>        | Lowest price today                                                           |
| `ohlc.close`<span>float64</span>      | Closing price of the instrument from the last trading day                    |

!!! note

    Always check for the existence of a particular key you've requested (eg: NSE:INFY) in the response. If there's no data for the particular instrument or if it has expired, the key will be missing from the response.

## Retrieving LTP quotes

This API returns the LTPs of up to **1000** instruments in one go.

Instruments are identified by the `exchange:tradingsymbol` combination and are passed as values to the query parameter `i` which is repeated for every instrument. If there is no data available for a given key, the key will be absent from the response. The existence of all the instrument keys in the response map should be checked before to accessing them.

```shell
curl "https://api.kite.trade/quote/ltp?i=NSE:INFY&i=BSE:SENSEX&i=NSE:NIFTY+50" \
 	-H "X-Kite-Version: 3" \
  -H "Authorization: token api_key:access_token"
```

```json
{
	"status": "success",
	"data": {
		"NSE:INFY": {
			"instrument_token": 408065,
			"last_price": 1074.35
		}
	}
}
```

### Response attributes

| attribute                             | &nbsp;                                                                       |
| ------------------------------------- | ---------------------------------------------------------------------------- |
| `instrument_token`<span>uint32</span> | The numerical identifier issued by the exchange representing the instrument. |
| `last_price`<span>float64</span>      | Last traded market price                                                     |

!!! note

    Always check for the existence of a particular key you've requested (eg: NSE:INFY) in the response. If there's no data for the particular instrument or if it has expired, the key will be absent from the response.

## Limits

| attribute     | number of instruments |
| ------------- | --------------------- |
| `/quote`      | 500                   |
| `/quote/ohlc` | 1000                  |
| `/quote/ltp`  | 1000                  |
