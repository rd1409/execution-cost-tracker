"""Small JSON-over-HTTP helpers (standard library only).

Every network client in the package takes ``http_get`` / ``http_post``
callables with these signatures, so tests can pass fakes instead.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from typing import Any, Callable

from .reference import USER_AGENT

HttpGet = Callable[[str, dict], Any]
HttpPost = Callable[[str, dict, dict], Any]

DEFAULT_TIMEOUT = 15


class HttpError(Exception):
    """A request failed; ``status`` is the HTTP status (None for network errors)."""

    def __init__(self, message: str, status: int | None = None) -> None:
        super().__init__(message)
        self.status = status


def _open(req: urllib.request.Request, timeout: float) -> Any:
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.load(resp)
    except urllib.error.HTTPError as e:
        body = e.read().decode(errors="replace")[:300]
        raise HttpError(f"HTTP {e.code}: {body}", e.code) from e
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        raise HttpError(f"{type(e).__name__}: {getattr(e, 'reason', e)}") from e
    except ValueError as e:
        raise HttpError(f"response wasn't JSON: {e}") from e


def get_json(url: str, headers: dict | None = None, timeout: float = DEFAULT_TIMEOUT) -> Any:
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "application/json", **(headers or {})})
    return _open(req, timeout)


def post_json(url: str, body: dict, headers: dict | None = None, timeout: float = DEFAULT_TIMEOUT) -> Any:
    req = urllib.request.Request(
        url,
        data=json.dumps(body).encode(),
        method="POST",
        headers={
            "User-Agent": USER_AGENT,
            "Accept": "application/json",
            "Content-Type": "application/json",
            **(headers or {}),
        },
    )
    return _open(req, timeout)
