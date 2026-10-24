<!-- source: https://kite.trade/docs/connect/v3/exceptions/ | fetched: 2026-10-24 | do not edit, regenerate via scripts/refresh_docs.py -->

# Exceptions and errors

In addition to the `40x` and `50x` headers, error responses come with the name of the exception generated internally by the API server. You can define corresponding exceptions in your language or library, and raise them by doing a switch on the returned exception name.

### Example

```http
HTTP/1.1 500 Server error
Content-Type: application/json

{
	"status": "error",
	"message": "Error message",
	"error_type": "GeneralException"
}
```

| exception        | &nbsp;                                                                                                                                                                                                                                                                                                       |
| ---------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| TokenException   | Preceded by a `403` header, this indicates the expiry or invalidation of an authenticated session. This can be caused by the user logging out, a natural expiry, or the user logging into another Kite instance. When you encounter this error, you should clear the user's session and re-initiate a login. |
| UserException    | Represents user account related errors                                                                                                                                                                                                                                                                       |
| OrderException   | Represents order related errors such placement failures, a corrupt fetch etc                                                                                                                                                                                                                                 |
| InputException   | Represents missing required fields, bad values for parameters etc.                                                                                                                                                                                                                                           |
| MarginException  | Represents insufficient funds, required for the order placement                                                                                                                                                                                                                                              |
| HoldingException | Represents insufficient holdings, available to place sell order for specified instrument                                                                                                                                                                                                                     |
| NetworkException | Represents a network error where the API was unable to communicate with the OMS (Order Management System)                                                                                                                                                                                                    |
| DataException    | Represents an internal system error where the API was unable to understand the response from the OMS to inturn respond to a request                                                                                                                                                                          |
| GeneralException | Represents an unclassified error. This should only happen rarely                                                                                                                                                                                                                                             |

### Common HTTP error codes

| code  | &nbsp;                                                                   |
| ----- | ------------------------------------------------------------------------ |
| `400` | Missing or bad request parameters or values                              |
| `403` | Session expired or invalidate. Must relogin                              |
| `404` | Request resource was not found                                           |
| `405` | Request method (GET, POST etc.) is not allowed on the requested endpoint |
| `410` | The requested resource is gone permanently                               |
| `429` | Too many requests to the API (rate limiting)                             |
| `500` | Something unexpected went wrong                                          |
| `502` | The backend OMS is down and the API is unable to communicate with it     |
| `503` | Service unavailable; the API is down                                     |
| `504` | Gateway timeout; the API is unreachable                                  |

### API rate limit

| end-point                                   | rate-limit   |
| ------------------------------------------- | ------------ |
| [Quote](market-quotes.md#market-quotes)     | 1req/second  |
| [Historical candle](historical.md)          | 3req/second  |
| [Order placement](orders.md#placing-orders) | 10req/second |
| All other endpoints                         | 10req/second |

!!! note

    There are limitations at 400 orders per minute and 10 orders per second. <br>
    As a risk management measure, at Zerodha, a single user/API key will not be able to place more than 5000 orders per day. This restriction is across all segments and varieties.<br>
    Rate limitations also apply for order modification where a maximum of 25 modifications are allowed per order. Post that user has to cancel the order and place it again. <br>
