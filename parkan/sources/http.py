"""Минимальный HTTP-клиент с повторами. Транспорт подменяется в тестах."""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.request


class HttpError(RuntimeError):
    def __init__(self, url: str, status: int, body: bytes = b""):
        super().__init__(f"HTTP {status} для {url}: {body[:200]!r}")
        self.status = status


def urllib_transport(url: str, headers: dict | None = None, timeout: float = 60.0) -> tuple[int, bytes]:
    req = urllib.request.Request(url, headers={"Accept": "application/json", **(headers or {})})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, resp.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()


def get_json(url: str, *, transport=None, headers=None, retries: int = 3, backoff: float = 2.0,
             sleep=time.sleep):
    """GET с повтором на сетевых ошибках, 429 и 5xx. 4xx (кроме 429) — сразу ошибка."""
    transport = transport or urllib_transport
    last: Exception | None = None
    for attempt in range(retries + 1):
        try:
            status, body = transport(url, headers=headers)
        except (urllib.error.URLError, TimeoutError, ConnectionError) as e:
            last = e
        else:
            if 200 <= status < 300:
                return json.loads(body.decode("utf-8-sig")), body
            last = HttpError(url, status, body)
            if status != 429 and status < 500:
                raise last
        if attempt < retries:
            sleep(backoff * 2 ** attempt)
    raise last
