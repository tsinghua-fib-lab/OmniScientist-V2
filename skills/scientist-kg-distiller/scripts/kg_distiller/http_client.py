from __future__ import annotations

import json
import time
import urllib.parse
import urllib.request
from http.client import IncompleteRead
from typing import Any
from urllib.error import HTTPError, URLError


class HttpClient:
    def __init__(
        self,
        *,
        timeout: float = 30.0,
        max_attempts: int = 3,
        min_interval: float = 0.15,
    ):
        self.timeout = timeout
        self.max_attempts = max_attempts
        self.min_interval = min_interval
        self._last_request_at = 0.0

    def get_json(
        self,
        url: str,
        params: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        data, _ = self.get_bytes(url, params=params, headers=headers)
        try:
            value = json.loads(data.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValueError(f"{url} returned invalid JSON: {exc}") from exc
        if not isinstance(value, dict):
            raise TypeError(f"{url} returned JSON that is not an object")
        return value

    def get_text(
        self,
        url: str,
        params: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
    ) -> str:
        data, response_headers = self.get_bytes(url, params=params, headers=headers)
        charset = response_headers.get_content_charset() or "utf-8"
        return data.decode(charset, errors="replace")

    def get_bytes(
        self,
        url: str,
        params: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
    ) -> tuple[bytes, Any]:
        if params:
            separator = "&" if "?" in url else "?"
            url = f"{url}{separator}{urllib.parse.urlencode(params, doseq=True)}"
        request_headers = {
            "User-Agent": "scientist-kg-distiller/1.0 academic-research"
        }
        request_headers.update(headers or {})
        last_error: Exception | None = None
        for attempt in range(1, self.max_attempts + 1):
            self._respect_rate_limit()
            request = urllib.request.Request(url, headers=request_headers)
            try:
                with urllib.request.urlopen(request, timeout=self.timeout) as response:
                    return response.read(), response.headers
            except HTTPError as exc:
                last_error = exc
                if exc.code not in {408, 429, 500, 502, 503, 504}:
                    raise
                delay = _retry_delay(exc, attempt)
            except (URLError, TimeoutError, IncompleteRead, ConnectionError) as exc:
                last_error = exc
                delay = min(2 ** (attempt - 1), 8)
            if attempt < self.max_attempts:
                time.sleep(delay)
        assert last_error is not None
        raise last_error

    def _respect_rate_limit(self) -> None:
        elapsed = time.monotonic() - self._last_request_at
        if elapsed < self.min_interval:
            time.sleep(self.min_interval - elapsed)
        self._last_request_at = time.monotonic()


def _retry_delay(exc: HTTPError, attempt: int) -> float:
    retry_after = exc.headers.get("Retry-After") if exc.headers else None
    if retry_after:
        try:
            return min(float(retry_after), 30.0)
        except ValueError:
            pass
    return min(2 ** (attempt - 1), 8)
