"""Shared outbound HTTP for the vendor fetchers (#128): one copy of the security controls.

- A generic User-Agent with no personal data.
- Redirects are refused: a 3xx comes back as its status, so only the fixed host the caller
  names is read. Certificates are verified (the default HTTPS handler).
- The body is read with a byte cap. A network error maps to status 0, so callers retry it
  like a 5xx.
"""

import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

__all__ = ["USER_AGENT", "FetchFailed", "http_request", "write_atomic"]

USER_AGENT = "Mozilla/5.0 (compatible; historical-data-client/1.0)"


class FetchFailed(RuntimeError):
    pass


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args: object, **kwargs: object) -> None:
        return None


_OPENER = urllib.request.build_opener(_NoRedirect)


def http_request(
    method: str,
    url: str,
    headers: dict[str, str],
    form: dict[str, str] | None,
    *,
    cap: int,
    timeout: float,
) -> tuple[int, bytes]:
    """(status, body); the body is at most `cap + 1` bytes, so a caller can refuse it."""
    data = None if form is None else urllib.parse.urlencode(form).encode()
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with _OPENER.open(req, timeout=timeout) as r:
            return r.status, r.read(cap + 1)
    except urllib.error.HTTPError as e:
        return e.code, b""
    except (urllib.error.URLError, TimeoutError, OSError):
        return 0, b""


def write_atomic(path: Path, body: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_bytes(body)
    tmp.replace(path)
