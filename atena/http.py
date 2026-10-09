"""外部 HTTP 呼び出しの共通処理（標準ライブラリのみ）。テストでは fetch を差し替える。"""

from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from typing import Callable


class HTTPError(RuntimeError):
    def __init__(self, status: int, message: str, body: str = ""):
        super().__init__(f"HTTP {status}: {message}")
        self.status = status
        self.body = body


def request(method: str, url: str, *, params: dict | None = None, json_body=None, data: bytes | None = None,
            headers: dict | None = None, timeout: float = 30.0) -> bytes:
    if params:
        url += ("&" if "?" in url else "?") + urllib.parse.urlencode(params)
    hdrs = dict(headers or {})
    if json_body is not None:
        data = json.dumps(json_body).encode("utf-8")
        hdrs.setdefault("Content-Type", "application/json")
    req = urllib.request.Request(url, data=data, headers=hdrs, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.read()
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", "replace")
        raise HTTPError(e.code, e.reason, body) from e
    except (urllib.error.URLError, TimeoutError) as e:
        raise HTTPError(0, str(e)) from e


def get_json(url: str, **kw) -> dict:
    return json.loads(request("GET", url, **kw).decode("utf-8"))


Fetch = Callable[..., bytes]
