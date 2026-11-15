"""Polite HTTP client: per-host rate limit, retries, and a SQLite response cache."""

from __future__ import annotations

import json
import logging
import sqlite3
import time
from urllib.parse import urlencode, urlsplit

import httpx

log = logging.getLogger(__name__)


class NotFound(Exception):
    pass


class HttpError(Exception):
    pass


class Http:
    def __init__(
        self,
        conn: sqlite3.Connection | None,
        user_agent: str,
        min_interval_s: float = 1.0,
        cache_hours: float = 6,
        use_cache: bool = True,
        timeout_s: float = 60,
    ):
        self.conn = conn
        self.min_interval_s = min_interval_s
        self.cache_hours = cache_hours
        self.use_cache = use_cache and conn is not None
        self._last_hit: dict[str, float] = {}
        self.stats = {"requests": 0, "cache_hits": 0}
        self.client = httpx.Client(
            headers={"User-Agent": user_agent, "Accept": "application/json"},
            timeout=timeout_s,
            follow_redirects=True,
        )

    def close(self) -> None:
        self.client.close()

    # -- public ----------------------------------------------------------
    def get_json(self, url: str, params: dict | None = None):
        full = f"{url}?{urlencode(params)}" if params else url
        return self._request("GET", full, None)

    def post_json(self, url: str, body: dict):
        return self._request("POST", url, body)

    # -- internals -------------------------------------------------------
    def _cache_key(self, method: str, url: str, body: dict | None) -> str:
        if method == "GET":
            return url
        return f"POST {url} {json.dumps(body, sort_keys=True)}"

    def _cache_get(self, key: str):
        if not self.use_cache:
            return None
        row = self.conn.execute(
            "SELECT body FROM http_cache WHERE url = ? "
            "AND fetched_at >= datetime('now', ?)",
            (key, f"-{self.cache_hours * 3600:.0f} seconds"),
        ).fetchone()
        return json.loads(row[0]) if row else None

    def _cache_put(self, key: str, data) -> None:
        if not self.use_cache:
            return
        self.conn.execute(
            "INSERT INTO http_cache(url, body, fetched_at) VALUES(?, ?, datetime('now')) "
            "ON CONFLICT(url) DO UPDATE SET body = excluded.body, fetched_at = excluded.fetched_at",
            (key, json.dumps(data)),
        )
        self.conn.commit()

    def _throttle(self, host: str) -> None:
        last = self._last_hit.get(host)
        if last is not None:
            wait = self.min_interval_s - (time.monotonic() - last)
            if wait > 0:
                time.sleep(wait)
        self._last_hit[host] = time.monotonic()

    def _request(self, method: str, url: str, body: dict | None):
        key = self._cache_key(method, url, body)
        cached = self._cache_get(key)
        if cached is not None:
            self.stats["cache_hits"] += 1
            return cached

        host = urlsplit(url).netloc
        last_err: Exception | None = None
        for attempt in range(3):
            self._throttle(host)
            self.stats["requests"] += 1
            try:
                if method == "GET":
                    r = self.client.get(url)
                else:
                    r = self.client.post(url, json=body)
            except httpx.TransportError as e:
                last_err = e
                log.warning("%s %s failed (%s), attempt %d", method, url, e, attempt + 1)
                time.sleep(2 ** (attempt + 1))
                continue

            if r.status_code == 404:
                raise NotFound(url)
            if r.status_code == 429 or r.status_code >= 500:
                last_err = HttpError(f"{r.status_code} for {url}")
                retry_after = r.headers.get("Retry-After")
                delay = float(retry_after) if retry_after and retry_after.isdigit() else 2 ** (attempt + 1)
                log.warning("%s %s -> %d, retrying in %.0fs", method, url, r.status_code, delay)
                time.sleep(min(delay, 60))
                continue
            if r.status_code >= 300:
                raise HttpError(f"{r.status_code} for {url}: {r.text[:200]}")
            try:
                data = r.json()
            except ValueError as e:
                raise HttpError(f"non-JSON response from {url}") from e
            self._cache_put(key, data)
            return data

        raise HttpError(f"giving up on {url}: {last_err}")
