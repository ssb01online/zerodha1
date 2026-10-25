"""Domain service: live cache, health, capture, snapshot reads."""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from typing import Any

import duckdb

from kitetools import auth

from . import serialize, store
from .fetch import RawPayload, TokenInvalid, fetch_live
from .models import PortfolioView
from .normalize import to_view

DEFAULT_LIVE_TTL = 10.0

Fetcher = Callable[[], RawPayload]


class DbBusy(Exception):
    """DuckDB file is locked by another process."""


class LiveCache:
    """TTL cache with single-flight fetch. Errors are never cached."""

    def __init__(self, fetcher: Fetcher, ttl: float = DEFAULT_LIVE_TTL) -> None:
        self._fetcher = fetcher
        self._ttl = ttl
        self._lock = threading.Lock()
        self._view: PortfolioView | None = None
        self._fetched_monotonic: float | None = None

    def get(self, *, refresh: bool = False) -> tuple[PortfolioView, bool]:
        with self._lock:
            now = time.monotonic()
            if (
                not refresh
                and self._view is not None
                and self._fetched_monotonic is not None
                and (now - self._fetched_monotonic) < self._ttl
            ):
                return self._view, True

            raw = self._fetcher()
            view = to_view(raw)
            self._view = view
            self._fetched_monotonic = time.monotonic()
            return view, False

    def peek(self) -> PortfolioView | None:
        """Return cached view if still fresh, else None (no fetch)."""
        with self._lock:
            if self._view is None or self._fetched_monotonic is None:
                return None
            if (time.monotonic() - self._fetched_monotonic) >= self._ttl:
                return None
            return self._view


class PortfolioService:
    def __init__(
        self,
        db_path: Path | None = None,
        fetcher: Fetcher = fetch_live,
        ttl: float = DEFAULT_LIVE_TTL,
    ) -> None:
        self._db_path = Path(db_path) if db_path is not None else store.DEFAULT_DB_PATH
        self._cache = LiveCache(fetcher, ttl=ttl)
        self._db_lock = threading.Lock()

    def _with_db(self, fn: Callable[[duckdb.DuckDBPyConnection], Any]) -> Any:
        with self._db_lock:
            try:
                con = store.connect(self._db_path)
            except Exception as exc:
                # DuckDB raises various exceptions when the file is locked.
                msg = str(exc).lower()
                if "lock" in msg or "conflicting" in msg or isinstance(exc, duckdb.IOException):
                    raise DbBusy(
                        "Database is in use by another process "
                        "(e.g. portfolio serve or CLI capture)."
                    ) from exc
                raise
            try:
                store.ensure_schema(con)
                return fn(con)
            except RuntimeError:
                # schema mismatch from ensure_schema
                raise
            except duckdb.IOException as exc:
                raise DbBusy(str(exc)) from exc
            finally:
                con.close()

    def health(self) -> dict[str, Any]:
        session: dict[str, Any] = {
            "valid": False,
            "user_id": None,
            "expires_at": None,
            "message": None,
        }
        try:
            record = auth.load_token()
            expires = auth.token_expiry(datetime.fromisoformat(record["login_time"]))
            session = {
                "valid": True,
                "user_id": record.get("user_id"),
                "expires_at": expires.isoformat(),
                "message": None,
            }
        except auth.NotLoggedIn as exc:
            session["message"] = str(exc)

        db_exists = self._db_path.exists()
        schema_version: int | None = None
        snapshot_count = 0
        if db_exists:
            def _read(con: duckdb.DuckDBPyConnection) -> tuple[int | None, int]:
                row = con.execute(
                    "SELECT value FROM meta WHERE key = 'schema_version'"
                ).fetchone()
                ver = int(row[0]) if row else None
                count_row = con.execute("SELECT COUNT(*) FROM snapshots").fetchone()
                return ver, int(count_row[0]) if count_row else 0

            try:
                schema_version, snapshot_count = self._with_db(_read)
            except DbBusy:
                schema_version = None
                snapshot_count = 0
            except RuntimeError:
                # Schema mismatch — still report existence.
                schema_version = None

        return {
            "ok": True,
            "session": session,
            "db": {
                "path": str(self._db_path),
                "exists": db_exists,
                "schema_version": schema_version,
                "expected_schema_version": store.SCHEMA_VERSION,
                "snapshot_count": snapshot_count,
            },
        }

    def live(self, *, refresh: bool = False) -> dict[str, Any]:
        view, cached = self._cache.get(refresh=refresh)
        payload = serialize.portfolio_payload(view, source="live")
        payload["cached"] = cached
        payload["fetched_at"] = serialize.iso(view.fetched_at)
        return payload

    def capture(self) -> dict[str, Any]:
        # Prefer fresh cache so auto-capture after live does not double-hit Kite.
        view = self._cache.peek()
        if view is None:
            view, _ = self._cache.get(refresh=False)

        def _cap(con: duckdb.DuckDBPyConnection) -> store.CaptureResult:
            return store.capture(con, view, source="live")

        result = self._with_db(_cap)
        return {
            "saved": result.saved,
            "snapshot_id": result.snapshot_id,
            "fingerprint_hash": result.fingerprint_hash,
            "observed_at": serialize.iso(view.fetched_at),
            "diff": serialize.diff_payload(result.diff),
        }

    def snapshots(self, limit: int = 50) -> dict[str, Any]:
        def _list(con: duckdb.DuckDBPyConnection) -> list[store.SnapshotMeta]:
            return store.list_snapshots(con, limit=limit)

        rows = self._with_db(_list)
        return {
            "snapshots": [serialize.snapshot_meta_payload(s) for s in rows],
            "count": len(rows),
        }

    def snapshot(self, snapshot_id: int) -> dict[str, Any]:
        def _get(con: duckdb.DuckDBPyConnection) -> store.SnapshotRecord:
            return store.get_snapshot(con, snapshot_id)

        try:
            record = self._with_db(_get)
        except KeyError as exc:
            raise SnapshotNotFound(str(exc)) from exc

        payload = serialize.portfolio_payload(
            record.view,
            source="snapshot",
            snapshot=record.meta,
        )
        payload["composition"] = record.composition
        return payload


class SnapshotNotFound(Exception):
    pass


__all__ = [
    "DbBusy",
    "LiveCache",
    "PortfolioService",
    "SnapshotNotFound",
    "TokenInvalid",
]
