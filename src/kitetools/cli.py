import argparse
from datetime import datetime

from kiteconnect.exceptions import TokenException

from . import auth


def cmd_login(args: argparse.Namespace) -> None:
    record = auth.login(redirect_response=args.url, open_browser=not args.no_browser)
    expires = auth.token_expiry(datetime.fromisoformat(record["login_time"]))
    print(f"Logged in as {record['user_id']} ({record['user_name']}). Session valid until {expires:%Y-%m-%d %H:%M} IST.")


def cmd_whoami(args: argparse.Namespace) -> None:
    try:
        profile = auth.get_kite().profile()
    except (auth.NotLoggedIn, TokenException) as exc:
        raise SystemExit(str(exc))
    print(f"{profile['user_id']} | {profile['user_name']} | {profile['email']} | broker={profile['broker']}")
    print(f"exchanges: {', '.join(profile['exchanges'])}")
    print(f"products:  {', '.join(profile['products'])}")


def main() -> None:
    parser = argparse.ArgumentParser(prog="kitetools", description="Read-only Kite Connect utilities.")
    sub = parser.add_subparsers(dest="command", required=True)

    p_login = sub.add_parser("login", help="Run the Kite login flow and cache today's access token.")
    p_login.add_argument("--url", help="Redirected URL (or request_token) if you already completed the browser login.")
    p_login.add_argument("--no-browser", action="store_true", help="Print the login URL instead of opening it.")
    p_login.set_defaults(func=cmd_login)

    p_whoami = sub.add_parser("whoami", help="Verify the cached session by fetching the user profile.")
    p_whoami.set_defaults(func=cmd_whoami)

    args = parser.parse_args()
    args.func(args)
