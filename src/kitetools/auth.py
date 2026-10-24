"""Kite Connect login flow and access-token caching.

The access_token expires at 6 AM IST the next day, so `login` is needed once per trading day.
"""

import json
import webbrowser
from datetime import datetime, time, timedelta, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import parse_qs, urlparse

from kiteconnect import KiteConnect

from .config import TOKEN_FILE, Settings, load_settings

IST = timezone(timedelta(hours=5, minutes=30))
TOKEN_RESET = time(6, 0)
LOCAL_HOSTS = {"127.0.0.1", "localhost"}


class NotLoggedIn(Exception):
    pass


def token_expiry(login_time: datetime) -> datetime:
    login_ist = login_time.astimezone(IST)
    reset = datetime.combine(login_ist.date(), TOKEN_RESET, tzinfo=IST)
    return reset if login_ist < reset else reset + timedelta(days=1)


def extract_request_token(text: str) -> str:
    text = text.strip()
    if "request_token=" not in text:
        return text
    query = parse_qs(urlparse(text).query)
    if query.get("status", ["success"])[0] != "success":
        raise SystemExit(f"Login did not succeed: {query}")
    return query["request_token"][0]


def _capture_on_localhost(redirect_url: str, login_url: str, open_browser: bool) -> str:
    target = urlparse(redirect_url)
    captured: dict[str, str] = {}

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            if "request_token=" not in self.path:
                self.send_response(404)
                self.end_headers()
                return
            captured["url"] = self.path
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(b"<h3>Kite login captured. You can close this tab.</h3>")

        def log_message(self, *args):
            pass

    class ExclusiveServer(HTTPServer):
        # Windows SO_REUSEADDR lets a second process share a busy port, silently splitting the redirect.
        allow_reuse_address = False

    try:
        server = ExclusiveServer((target.hostname, target.port or 80), Handler)
    except OSError as exc:
        raise SystemExit(
            f"Cannot listen on {target.hostname}:{target.port}: {exc}. "
            "Pick a free port and update redirect_url in both the Kite developer console and .env/config.props.txt."
        )
    if open_browser:
        webbrowser.open(login_url)
    print(f"Waiting for redirect on {redirect_url} ...")
    while "url" not in captured:
        server.handle_request()
    server.server_close()
    return extract_request_token(captured["url"])


def login(redirect_response: str | None = None, open_browser: bool = True) -> dict:
    settings = load_settings()
    kite = KiteConnect(api_key=settings.api_key)

    if redirect_response:
        request_token = extract_request_token(redirect_response)
    else:
        login_url = kite.login_url()
        print(f"Login URL: {login_url}")
        if urlparse(settings.redirect_url).hostname in LOCAL_HOSTS:
            request_token = _capture_on_localhost(settings.redirect_url, login_url, open_browser)
        else:
            if open_browser:
                webbrowser.open(login_url)
            print(f"After logging in you will land on {settings.redirect_url or 'your redirect URL'}?request_token=...")
            request_token = extract_request_token(input("Paste that full URL (or just the request_token): "))

    session = kite.generate_session(request_token, api_secret=settings.api_secret)
    record = {
        "access_token": session["access_token"],
        "user_id": session.get("user_id"),
        "user_name": session.get("user_name"),
        "login_time": datetime.now(timezone.utc).isoformat(),
    }
    TOKEN_FILE.parent.mkdir(parents=True, exist_ok=True)
    TOKEN_FILE.write_text(json.dumps(record, indent=2), encoding="utf-8")
    return record


def load_token() -> dict:
    if not TOKEN_FILE.exists():
        raise NotLoggedIn("No saved session. Run: uv run kitetools login")
    record = json.loads(TOKEN_FILE.read_text(encoding="utf-8"))
    expires = token_expiry(datetime.fromisoformat(record["login_time"]))
    if datetime.now(timezone.utc) >= expires:
        raise NotLoggedIn(f"Saved session expired at {expires:%Y-%m-%d %H:%M} IST. Run: uv run kitetools login")
    return record


def get_kite(settings: Settings | None = None) -> KiteConnect:
    settings = settings or load_settings()
    kite = KiteConnect(api_key=settings.api_key)
    kite.set_access_token(load_token()["access_token"])
    return kite
