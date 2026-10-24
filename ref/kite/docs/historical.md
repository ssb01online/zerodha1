<!-- source: https://kite.trade/docs/connect/v3/historical/ | fetched: 2026-10-24 | do not edit, regenerate via scripts/refresh_docs.py -->

# Historical candle data

The historical data API provides archived data (up to date as of the time of access) for instruments across various exchanges spanning back several years. A historical record is presented in the form of a _candle_ (Timestamp, Open, High, Low, Close, Volume, OI), and the data is available in several intervals&mdash;minute, 3 minutes, 5 minutes, hourly ... daily.

| type | endpoint                                                 | &nbsp;                                                     |
| ---- | -------------------------------------------------------- | ---------------------------------------------------------- |
| GET  | [/instruments/historical/:instrument_token/:interval](#) | Retrieve historical candle records for a given instrument. |

### URI parameters

| parameter           | &nbsp;                                                                                                                                                                                                                                           |
| ------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| `:instrument_token` | Identifier for the instrument whose historical records you want to fetch. This is obtained with the [instrument list](market-quotes.md#retrieving-the-full-instrument-list) API.                                                                 |
| `:interval`         | The candle record interval. Possible values are: <br />&middot; `minute`<br />&middot; `day`<br />&middot; `3minute`<br />&middot; `5minute`<br />&middot; `10minute`<br />&middot; `15minute`<br />&middot; `30minute`<br />&middot; `60minute` |

### Request parameters

| parameter    | &nbsp;                                                                    |
| ------------ | ------------------------------------------------------------------------- |
| `from`       | `yyyy-mm-dd hh:mm:ss` formatted date indicating the start date of records |
| `to`         | `yyyy-mm-dd hh:mm:ss` formatted date indicating the end date of records   |
| `continuous` | Accepts `0` or `1`. Pass `1` to get continuous data                       |
| `oi`         | Accepts `0` or `1`. Pass `1` to get OI data                               |

### Response structure

The response is an array of records, where each record in turn is an array of the following values &mdash; `[timestamp, open, high, low, close, volume]`.

!!! note

    It is possible to retrieve candles for small time intervals by making the `from` and `to` calls granular.
    For instance `from = 2017-01-01 09:15:00` and `to = 2017-01-01 09:30:00` to fetch candles for just 15 minutes
    between those timestamps.

## Continuous data

It's important to note that the exchanges flush the `instrument_token` for futures and options contracts for every expiry. For instance, `NIFTYJAN18FUT` and `NIFTYFEB18FUT` will have different instrument tokens although their underlying contract is the same. The instrument master API only returns instrument_tokens for contracts that are live. It is not possible to retrieve instrument_tokens for expired contracts from the API, unless you regularly download and cache them.

This is where `continuous` API comes in which works for NFO and MCX futures contracts. Given a live contract's `instrument_token`, the API will return `day` candle records for the same instrument's expired contracts. For instance, assuming the current month is January and you pass `NIFTYJAN18FUT`'s `instrument_token` along with `continuous=1`, you can fetch day candles for December, November ... contracts by simply changing the `from` and `to` dates.

## Examples

```shell
# Fetch minute candles for NSE-ACC.
# This will return several days of minute data ending today.
# The time of request is assumed to be to be 01:30 PM, 1 Jan 2016,
# which is reflected in the latest (last) record.

# The data has been truncated with ... in the example responses.

curl "https://api.kite.trade/instruments/historical/5633/minute?from=2017-12-15+09:15:00&to=2017-12-15+09:20:00"
	-H "X-Kite-Version: 3" \
    -H "Authorization: token api_key:access_token" \
```

```json
{
  "status": "success",
  "data": {
    "candles": [
      [
        "2017-12-15T09:15:00+0530",
        1704.5,
        1705,
        1699.25,
        1702.8,
        2499
      ],
      [
        "2017-12-15T09:16:00+0530",
        1702,
        1702,
        1698.15,
        1698.15,
        1271
      ],
      [
        "2017-12-15T09:17:00+0530",
        1698.15,
        1700.25,
        1698,
        1699.25,
        831
      ],
      [
        "2017-12-15T09:18:00+0530",
        1700,
        1700,
        1698.3,
        1699,
        771
      ],
      [
        "2017-12-15T09:19:00+0530",
        1699,
        1700,
        1698.1,
        1699.8,
        543
      ],
      [
        "2017-12-15T09:20:00+0530",
        1699.8,
        1700,
        1696.55,
        1696.9,
        802
      ]
    ]
  }
}
```

### OI Data

```shell
# Fetch minute candles for NIFTY19DECFUT for five minutes with OI data
curl "https://api.kite.trade/instruments/historical/12517890/minute?from=2019-12-04%2009:15:00&to=2019-12-04%2009:20:00&oi=1" \
     -H 'X-Kite-Version: 3' \
     -H 'Authorization: token api_key:access_token'
```

```json
{
  "status": "success",
  "data": {
    "candles": [
      [
        "2019-12-04T09:15:00+0530",
        12009.9,
        12019.35,
        12001.25,
        12001.5,
        163275,
        13667775
      ],
      [
        "2019-12-04T09:16:00+0530",
        12001,
        12003,
        11998.25,
        12001,
        105750,
        13667775
      ],
      [
        "2019-12-04T09:17:00+0530",
        12001,
        12001,
        11995.1,
        11998.55,
        48450,
        13758000
      ],
      [
        "2019-12-04T09:18:00+0530",
        11997.8,
        12002,
        11996.25,
        12001.55,
        52875,
        13758000
      ],
      [
        "2019-12-04T09:19:00+0530",
        12002.35,
        12007,
        12001.45,
        12007,
        52200,
        13758000
      ],
      [
        "2019-12-04T09:20:00+0530",
        12006.95,
        12009.25,
        11999.6,
        11999.6,
        65325,
        13777050
      ]
    ]
  }
}
```
