<!-- source: https://kite.trade/docs/connect/v3/ | fetched: 2026-10-24 | do not edit, regenerate via scripts/refresh_docs.py -->

# Introduction

Kite Connect is a set of REST-like HTTP APIs that expose many capabilities required to build a complete stock market investment and trading platform. It lets you execute orders in real time (equities, commodities, mutual funds), manage user portfolios, stream live market data over WebSockets, and more.

![Kite Connect](images/kite-connect.png)

All inputs are form-encoded parameters and responses are JSON (apart from a couple exceptions). The responses may be Gzipped. Standard HTTP codes are used to indicate success and error states with accompanying JSON data. The API endpoints are _not_ cross site request enabled, hence cannot be called directly from browsers.

An `api_key` + `api_secret` pair is issued and you have to register a redirect url where a user is sent after the login flow. For mobile and desktop applications, there has to be a remote backend which does the handshake on behalf of the mobile app and the `api_secret` should never be embedded in the app.

## Getting Started & Prerequisites

To use Kite Connect APIs, you'll need:

### Trading Account Requirements

- An active Zerodha trading account.
- Account with [2FA TOTP enabled](https://support.zerodha.com/category/trading-and-markets/general-kite/login-credentials-of-trading-platforms/articles/time-based-otp-setup).

### Developer Account Setup

- **Create Developer Account**: Visit the [Kite Connect Developer Portal](https://developers.kite.trade/login) and sign up.
- **App Creation**: Log in and create a new app to get your API credentials.
- **Set Redirect URL**: Configure the URL where user will be redirected after authentication.
- **Get API Keys**: Note down your `api_key` and `api_secret` (keep the secret secure) for future reference.

!!! note

    Visit the [Developer Community Forum](https://kite.trade/forum/discussion/4732/frequently-asked-questions-faqs#latest) for support and discussions.

### Start Building

Once you have your API credentials:

- **Complete Authentication**: Follow the [login flow documentation](https://kite.trade/docs/connect/v3/user/) to authenticate user.
- **Choose Your SDK**: Pick from our [official libraries](sdks.md#libraries-and-sdks) in your preferred language.
- **Explore Examples & Documentation**: Each SDK comes with comprehensive examples and documentation to help you get started quickly.

!!! note

    The authentication flow is crucial for security - make sure to understand it [thoroughly before proceeding](user.md#login-flow).
