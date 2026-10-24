"""Credentials loaded from the gitignored .env/ folder, overridable via environment variables."""

import os
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SECRETS_DIR = ROOT / ".env"
PROPS_FILE = SECRETS_DIR / "config.props.txt"
SECRET_FILE = SECRETS_DIR / "api_secret.txt"
TOKEN_FILE = SECRETS_DIR / "access_token.json"
DATA_DIR = ROOT / "data"


@dataclass(frozen=True)
class Settings:
    api_key: str
    api_secret: str
    redirect_url: str

    def __repr__(self) -> str:
        return f"Settings(api_key={self.api_key[:4]}..., redirect_url={self.redirect_url!r})"


def _read_props(path: Path) -> dict[str, str]:
    props: dict[str, str] = {}
    if not path.exists():
        return props
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        sep = min((i for i in (line.find("="), line.find(":")) if i > 0), default=-1)
        if sep < 0:
            continue
        props[line[:sep].strip().lower()] = line[sep + 1 :].strip().strip('"').strip("'")
    return props


def load_settings() -> Settings:
    props = _read_props(PROPS_FILE)
    secret_from_file = SECRET_FILE.read_text(encoding="utf-8").strip() if SECRET_FILE.exists() else ""

    api_key = os.environ.get("KITE_API_KEY") or props.get("api_key", "")
    api_secret = os.environ.get("KITE_API_SECRET") or props.get("api_secret") or secret_from_file
    redirect_url = os.environ.get("KITE_REDIRECT_URL") or props.get("redirect_url", "")

    missing = [name for name, value in (("api_key", api_key), ("api_secret", api_secret)) if not value]
    if missing:
        raise SystemExit(f"Missing {', '.join(missing)}: set them in {PROPS_FILE} / {SECRET_FILE} or KITE_* env vars.")
    return Settings(api_key=api_key, api_secret=api_secret, redirect_url=redirect_url)
