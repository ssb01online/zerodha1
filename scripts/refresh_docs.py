"""Refresh the offline Kite Connect knowledge base under ref/kite/.

Run with: uv run --group docs scripts/refresh_docs.py
Review changes afterwards with: git diff ref/kite
"""

from datetime import datetime, timezone
from pathlib import Path

import httpx
import html2text
import truststore

truststore.inject_into_ssl()

ROOT = Path(__file__).resolve().parent.parent
DOCS_DIR = ROOT / "ref" / "kite" / "docs"
EXAMPLES_DIR = ROOT / "ref" / "kite" / "examples"

DOCS_BASE = "https://kite.trade/docs/connect/v3"
DOC_PAGES = {
    "introduction": "",
    "user": "user/",
    "portfolio": "portfolio/",
    "orders": "orders/",
    "market-quotes": "market-quotes/",
    "historical": "historical/",
    "exceptions": "exceptions/",
}

EXAMPLES_API = "https://api.github.com/repos/zerodha/pykiteconnect/contents/examples"


def fetch_doc(client: httpx.Client, path: str) -> str:
    md = client.get(f"{DOCS_BASE}/{path}index.md")
    if md.status_code == 200 and "text/html" not in md.headers.get("content-type", ""):
        return md.text
    page = client.get(f"{DOCS_BASE}/{path}")
    page.raise_for_status()
    converter = html2text.HTML2Text()
    converter.body_width = 0
    converter.ignore_images = True
    return converter.handle(page.text)


def header(source: str, fetched: str) -> str:
    return f"<!-- source: {source} | fetched: {fetched} | do not edit, regenerate via scripts/refresh_docs.py -->\n\n"


def main() -> None:
    fetched = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    DOCS_DIR.mkdir(parents=True, exist_ok=True)
    EXAMPLES_DIR.mkdir(parents=True, exist_ok=True)

    with httpx.Client(timeout=30, follow_redirects=True) as client:
        for name, path in DOC_PAGES.items():
            text = fetch_doc(client, path)
            (DOCS_DIR / f"{name}.md").write_text(header(f"{DOCS_BASE}/{path}", fetched) + text, encoding="utf-8")
            print(f"docs/{name}.md")

        listing = client.get(EXAMPLES_API)
        listing.raise_for_status()
        for item in listing.json():
            if item["type"] != "file" or not item["name"].endswith(".py"):
                continue
            code = client.get(item["download_url"])
            code.raise_for_status()
            comment = f"# source: {item['html_url']} | fetched: {fetched}\n\n"
            (EXAMPLES_DIR / item["name"]).write_text(comment + code.text, encoding="utf-8")
            print(f"examples/{item['name']}")


if __name__ == "__main__":
    main()
