"""
http.py — the ONLY module in EdgeDash that performs HTTP requests.

All sources call get_json() from here. No other module may import requests
or call requests.get directly.

Behaviour (steering rule 11):
  - 10 second timeout on every request
  - 2 retries with exponential backoff (1s, 2s) on transient failures
  - Real User-Agent header on every request
  - Raises SourceError with a clear message on unrecoverable failure
"""

from __future__ import annotations

import time
from typing import Any

import requests

_USER_AGENT = (
    "EdgeDash/0.1 (autonomous career intelligence agent; "
    "contact: github.com/edgedash)"
)

_DEFAULT_TIMEOUT = 10       # seconds
_MAX_RETRIES     = 2
_BACKOFF_BASE    = 1.0      # seconds; doubles each retry


class SourceError(Exception):
    """Raised when a source fails in a way the cycle cannot recover from."""


def get_json(
    url: str,
    params: dict[str, Any] | None = None,
    headers: dict[str, str] | None = None,
    timeout: int = _DEFAULT_TIMEOUT,
) -> Any:
    """
    GET a URL and return the parsed JSON body.

    Retries up to _MAX_RETRIES times on connection errors or 5xx responses,
    with exponential backoff. Raises SourceError on final failure.
    """
    merged_headers = {"User-Agent": _USER_AGENT}
    if headers:
        merged_headers.update(headers)

    last_error: Exception | None = None

    for attempt in range(_MAX_RETRIES + 1):
        if attempt > 0:
            wait = _BACKOFF_BASE * (2 ** (attempt - 1))
            time.sleep(wait)

        try:
            response = requests.get(
                url,
                params=params,
                headers=merged_headers,
                timeout=timeout,
            )

            if response.status_code == 429:
                retry_after = int(response.headers.get("Retry-After", _BACKOFF_BASE * (2 ** attempt)))
                time.sleep(retry_after)
                last_error = SourceError(f"Rate limited by {url} (HTTP 429)")
                continue

            if response.status_code >= 500:
                last_error = SourceError(
                    f"Server error from {url}: HTTP {response.status_code}"
                )
                continue

            if not response.ok:
                raise SourceError(
                    f"HTTP {response.status_code} from {url} — not retrying"
                )

            return response.json()

        except requests.exceptions.Timeout as exc:
            last_error = SourceError(f"Request to {url} timed out after {timeout}s")
            continue

        except requests.exceptions.ConnectionError as exc:
            last_error = SourceError(f"Connection error reaching {url}: {exc}")
            continue

        except requests.exceptions.JSONDecodeError as exc:
            raise SourceError(f"Response from {url} is not valid JSON: {exc}") from exc

        except SourceError:
            raise  # non-retryable, propagate immediately

    raise SourceError(
        f"Failed to GET {url} after {_MAX_RETRIES + 1} attempts. "
        f"Last error: {last_error}"
    ) from last_error
