<!-- source: https://kite.trade/docs/connect/v3/user/ | fetched: 2026-10-24 | do not edit, regenerate via scripts/refresh_docs.py -->

# User

## Login flow

The login flow starts by navigating to the public Kite login endpoint.

> **`https://kite.zerodha.com/connect/login?v=3&api_key=xxx`**

A successful login comes back with a `request_token` as a URL query parameter to the redirect URL registered on the developer console for that `api_key`. This `request_token`, along with a `checksum` (SHA-256 of `api_key + request_token + api_secret`) is POSTed to the token API to obtain an `access_token`, which is then used for signing all subsequent requests. In summary:

1. Navigate to the Kite Connect login page with the api_key
2. A successful login comes back with a `request_token` to the registered redirect URL
3. POST the `request_token` and `checksum` (SHA-256 of `api_key + request_token + api_secret`) to `/session/token`
4. Obtain the `access_token` and use that with all subsequent requests

> An optional `redirect_params` param can be appended to public Kite login endpoint, that will be sent back to the redirect URL. The value is URL encoded query params string, eg: `some=X&more=Y`). eg: `https://kite.zerodha.com/connect/login?v=3&api_key=xxx&redirect_params=some%3DX%26more%3DY`

[Here's a webinar](https://www.youtube.com/watch?v=9vzd289Eedk) that shows the login flow and other interactions.

![Kite Connect handshake flow](images/kite-connect-flow.png)

!!! warning

      Never expose your `api_secret` by embedding it in a mobile app or a client side application. Do not expose the `access_token` you obtain for a session to the public either.

| type   | endpoint                                             | &nbsp;                                                        |
| ------ | ---------------------------------------------------- | ------------------------------------------------------------- |
| POST   | [/session/token](#authentication-and-token-exchange) | Authenticate and obtain the access_token after the login flow |
| GET    | [/user/profile](#user-profile)                       | Retrieve the user profile                                     |
| GET    | [/user/margins/:segment](#funds-and-margins)         | Retrieve detailed funds and margin information                |
| DELETE | [/session/token](#logout)                            | Logout and invalidate the API session and access_token        |

## Authentication and token exchange

Once the `request_token` is obtained from the login flow, it should be POSTed to `/session/token` to complete the token exchange and retrieve the `access_token`.

```shell
curl https://api.kite.trade/session/token \
   -H "X-Kite-Version: 3" \
   -d "api_key=xxx" \
   -d "request_token=yyy" \
   -d "checksum=zzz"
```

```json
{
    "status": "success",
    "data": {
        "user_type": "individual",
        "email": "XXXXXX",
        "user_name": "Kite Connect",
        "user_shortname": "Connect",
        "broker": "ZERODHA",
        "exchanges": [
            "NSE",
            "NFO",
            "BFO",
            "CDS",
            "BSE",
            "MCX",
            "BCD",
            "MF"
        ],
        "products": [
            "CNC",
            "NRML",
            "MIS",
            "BO",
            "CO"
        ],
        "order_types": [
            "MARKET",
            "LIMIT",
            "SL",
            "SL-M"
        ],
        "avatar_url": "abc",
        "user_id": "XX0000",
        "api_key": "XXXXXX",
        "access_token": "XXXXXX",
        "public_token": "XXXXXXXX",
        "enctoken": "XXXXXX",
        "refresh_token": "",
        "silo": "",
        "login_time": "2021-01-01 16:15:14",
        "meta": {
            "demat_consent": "physical"
        }
    }
}
```

### Request parameters

| parameter       | &nbsp;                                                                                                                                                                               |
| --------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| `api_key`       | The public API key                                                                                                                                                                   |
| `request_token` | The one-time token obtained after the login flow. This token's lifetime is only a few minutes and it is meant to be exchanged for an `access_token` immediately after being obtained |
| `checksum`      | SHA-256 hash of (api_key + request_token + api_secret)                                                                                                                               |

### Response attributes

| attribute                           | &nbsp;                                                                                                                                                                                                                                                        |
| ----------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `user_id`<span>string</span>        | The unique, permanent user id registered with the broker and the exchanges                                                                                                                                                                                    |
| `user_name`<span>string</span>      | User's real name                                                                                                                                                                                                                                              |
| `user_shortname`<span>string</span> | Shortened version of the user's real name                                                                                                                                                                                                                     |
| `email`<span>string</span>          | User's email                                                                                                                                                                                                                                                  |
| `user_type`<span>string</span>      | User's registered role at the broker. This will be `individual` for all retail users                                                                                                                                                                          |
| `broker`<span>string</span>         | The broker ID                                                                                                                                                                                                                                                 |
| `exchanges`<span>string[]</span>    | Exchanges enabled for trading on the user's account                                                                                                                                                                                                           |
| `products`<span>string[]</span>     | Margin product types enabled for the user                                                                                                                                                                                                                     |
| `order_types`<span>string[]</span>  | Order types enabled for the user                                                                                                                                                                                                                              |
| `api_key`<span>string</span>        | The API key for which the authentication was performed                                                                                                                                                                                                        |
| `access_token`<span>string</span>   | The authentication token that's used with every subsequent request Unless this is [invalidated using the API](#logout), or invalidated by a master-logout from the Kite Web trading terminal, it'll expire at `6 AM` on the next day (regulatory requirement) |
| `public_token`<span>string</span>   | A token for public session validation where requests may be exposed to the public                                                                                                                                                                             |
| `refresh_token`<span>string</span>  | A token for getting long standing read permissions. This is only available to certain approved platforms                                                                                                                                                      |
| `login_time`<span>string</span>     | User's last login time                                                                                                                                                                                                                                        |
| `meta`<span>map</span>              | `demat_consent`: empty, consent or physical                                                                                                                                                                                                                   |
| `avatar_url`<span>string</span>     | Full URL to the user's avatar (PNG image) if there's one                                                                                                                                                                                                      |

### Signing requests

Once the authentication is complete, all requests should be signed with the HTTP `Authorization` header with `token` as the authorisation scheme, followed by a space, and then the `api_key:access_token` combination. For example:

```shell
curl -H "Authorization: token api_key:access_token"
curl -H "Authorization: token xxx:yyy"
```

## User profile

While a successful token exchange returns the full user profile, it's possible to retrieve it any point of time with the `/user/profile` API. Do note that the profile API does not return any of the tokens.

```shell
curl https://api.kite.trade/user/profile \
   -H "X-Kite-Version: 3" \
   -H "Authorization: token api_key:access_token"
```

```json
{
  "status": "success",
  "data": {
    "user_id": "AB1234",
    "user_type": "individual",
    "email": "xxxyyy@gmail.com",
    "user_name": "AxAx Bxx",
    "user_shortname": "AxAx",
    "broker": "ZERODHA",
    "exchanges": [
      "BFO",
      "MCX",
      "NSE",
      "CDS",
      "BSE",
      "BCD",
      "MF",
      "NFO"
    ],
    "products": [
      "CNC",
      "NRML",
      "MIS",
      "BO",
      "CO"
    ],
    "order_types": [
      "MARKET",
      "LIMIT",
      "SL",
      "SL-M"
    ],
    "avatar_url": null,
    "meta": {
      "demat_consent": "physical"
    }
  }
}
```

### Response attributes

| attribute                           | &nbsp;                                                                               |
| ----------------------------------- | ------------------------------------------------------------------------------------ |
| `user_id`<span>string</span>        | The unique, permanent user id registered with the broker and the exchanges           |
| `user_name`<span>string</span>      | User's real name                                                                     |
| `user_shortname`<span>string</span> | Shortened version of the user's real name                                            |
| `email`<span>string</span>          | User's email                                                                         |
| `user_type`<span>string</span>      | User's registered role at the broker. This will be `individual` for all retail users |
| `broker`<span>string</span>         | The broker ID                                                                        |
| `exchanges`<span>string[]</span>    | Exchanges enabled for trading on the user's account                                  |
| `products`<span>string[]</span>     | Margin product types enabled for the user                                            |
| `order_types`<span>string[]</span>  | Order types enabled for the user                                                     |
| `meta`<span>map</span>              | `demat_consent`: empty, consent or physical                                          |
| `avatar_url`<span>string</span>     | Full URL to the user's avatar (PNG image) if there's one                             |

## Funds and margins

A GET request to `/user/margins` returns funds, cash, and margin information for the user for equity and commodity segments.

A GET request to `/user/margins/:segment` returns funds, cash, and margin information for the user. `segment` in the URI can be either `equity` or `commodity`.

```shell
curl "https://api.kite.trade/user/margins" \
    -H "X-Kite-Version: 3" \
    -H "Authorization: token api_key:access_token"
```

```json
{
	"status": "success",
	"data": {
	  "equity": {
		"enabled": true,
		"net": 99725.05000000002,
		"available": {
		  "adhoc_margin": 0,
		  "cash": 245431.6,
		  "opening_balance": 245431.6,
		  "live_balance": 99725.05000000002,
		  "collateral": 0,
		  "intraday_payin": 0
		},
		"utilised": {
		  "debits": 145706.55,
		  "exposure": 38981.25,
		  "m2m_realised": 761.7,
		  "m2m_unrealised": 0,
		  "option_premium": 0,
		  "payout": 0,
		  "span": 101989,
		  "holding_sales": 0,
		  "turnover": 0,
		  "liquid_collateral": 0,
		  "stock_collateral": 0,
		  "delivery": 0
		}
	  },
	  "commodity": {
		"enabled": true,
		"net": 100661.7,
		"available": {
		  "adhoc_margin": 0,
		  "cash": 100661.7,
		  "opening_balance": 100661.7,
		  "live_balance": 100661.7,
		  "collateral": 0,
		  "intraday_payin": 0
		},
		"utilised": {
		  "debits": 0,
		  "exposure": 0,
		  "m2m_realised": 0,
		  "m2m_unrealised": 0,
		  "option_premium": 0,
		  "payout": 0,
		  "span": 0,
		  "holding_sales": 0,
		  "turnover": 0,
		  "liquid_collateral": 0,
		  "stock_collateral": 0,
		  "delivery": 0
		}
	  }
	}
  }
```

### Response attributes

| attribute                                        | &nbsp;                                                                                                   |
| ------------------------------------------------ | -------------------------------------------------------------------------------------------------------- |
| `enabled`<span>bool</span>                       | Indicates whether the segment is enabled for the user                                                    |
| `net`<span>float64</span>                        | Net cash balance available for trading (`intraday_payin` + `adhoc_margin` + `collateral`)                |
| `available.cash`<span>float64</span>             | Raw cash balance in the account available for trading (also includes `intraday_payin`)                   |
| `available.opening_balance`<span>float64</span>  | Opening balance at the day start                                                                         |
| `available.live_balance`<span>float64</span>     | Current available balance                                                                                |
| `available.intraday_payin`<span>float64</span>   | Amount that was deposited during the day                                                                 |
| `available.adhoc_margin`<span>float64</span>     | Additional margin provided by the broker                                                                 |
| `available.collateral`<span>float64</span>       | Margin derived from pledged stocks                                                                       |
| `utilised.m2m_unrealised`<span>float64</span>    | Un-booked (open) intraday profits and losses                                                             |
| `utilised.m2m_realised`<span>float64</span>      | Booked intraday profits and losses                                                                       |
| `utilised.debits`<span>float64</span>            | Sum of all utilised margins (unrealised M2M + realised M2M + SPAN + Exposure + Premium + Holding sales)  |
| `utilised.span`<span>float64</span>              | SPAN margin blocked for all open F&O positions                                                           |
| `utilised.option_premium`<span>float64</span>    | Value of options premium received by shorting                                                            |
| `utilised.holding_sales`<span>float64</span>     | Value of holdings sold during the day                                                                    |
| `utilised.exposure`<span>float64</span>          | Exposure margin blocked for all open F&amp;O positions                                                   |
| `utilised.liquid_collateral`<span>float64</span> | Margin utilised against pledged liquidbees ETFs and liquid mutual funds                                  |
| `utilised.delivery`<span>float64</span>          | Margin blocked when you sell securities (20% of the value of stocks sold) from your demat or T1 holdings |
| `utilised.stock_collateral`<span>float64</span>  | Margin utilised against pledged stocks/ETFs                                                              |
| `utilised.turnover`<span>float64</span>          | Utilised portion of the maximum turnover limit (only applicable to certain clients)                      |
| `utilised.payout`<span>float64</span>            | Funds paid out or withdrawn to bank account during the day                                               |

## Logout

This call invalidates the access_token and destroys the API session. After this, the user should be sent through a new login flow before further interactions. This does not log the user out of the official Kite web or mobile applications.

```shell
curl --request DELETE \
  -H "X-Kite-Version: 3" \
  "https://api.kite.trade/session/token?api_key=xxx&access_token=yyy"
```

```json
{
  "status": "success",
  "data": true
}
```
