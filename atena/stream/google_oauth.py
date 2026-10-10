"""Google OAuth（インストールアプリ / ループバック方式 + PKCE）。YouTube の読み取り権限のみを要求する。

事前準備: Google Cloud Console で YouTube Data API v3 を有効化し、
OAuth クライアント ID（種類: デスクトップアプリ）を作成して JSON をダウンロードする。
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import secrets
import time
import urllib.parse
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

from ..http import request

AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"
SCOPE = "https://www.googleapis.com/auth/youtube.readonly"


class OAuthError(RuntimeError):
    pass


def _client(secret_file: Path) -> dict:
    data = json.loads(secret_file.read_text(encoding="utf-8"))
    c = data.get("installed") or data.get("web")
    if not c:
        raise OAuthError("client_secret JSON の形式が不正です（デスクトップアプリ用を使用してください）")
    return c


def _save(path: Path, token: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(token), encoding="utf-8")
    os.chmod(path, 0o600)


def authorize(secret_file: Path, token_file: Path, *, open_browser=webbrowser.open, fetch=request,
              timeout: float = 300) -> None:
    client = _client(secret_file)
    verifier = secrets.token_urlsafe(64)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    state = secrets.token_urlsafe(16)
    result: dict = {}

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            result.update({k: v[0] for k, v in q.items()})
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write("認証が完了しました。このタブを閉じてターミナルに戻ってください。".encode("utf-8"))

        def log_message(self, *a):
            pass

    server = HTTPServer(("127.0.0.1", 0), Handler)
    server.timeout = timeout
    redirect = f"http://127.0.0.1:{server.server_port}"
    url = AUTH_URL + "?" + urllib.parse.urlencode({
        "client_id": client["client_id"], "redirect_uri": redirect, "response_type": "code", "scope": SCOPE,
        "access_type": "offline", "prompt": "consent", "state": state,
        "code_challenge": challenge, "code_challenge_method": "S256"})
    print(f"ブラウザで Google にログインしてください:\n{url}")
    open_browser(url)
    server.handle_request()
    server.server_close()

    if result.get("state") != state or "code" not in result:
        raise OAuthError(f"認証に失敗しました: {result.get('error', 'state 不一致またはタイムアウト')}")
    body = urllib.parse.urlencode({
        "code": result["code"], "client_id": client["client_id"], "client_secret": client.get("client_secret", ""),
        "redirect_uri": redirect, "grant_type": "authorization_code", "code_verifier": verifier}).encode()
    tok = json.loads(fetch("POST", TOKEN_URL, data=body,
                           headers={"Content-Type": "application/x-www-form-urlencoded"}).decode())
    tok["expires_at"] = time.time() + tok.get("expires_in", 3600) - 60
    _save(token_file, tok)


class TokenProvider:
    """保存済みトークンからアクセストークンを返す。期限切れなら更新する。"""

    def __init__(self, secret_file: Path, token_file: Path, *, fetch=request, clock=time.time):
        self.secret_file, self.token_file, self.fetch, self.clock = secret_file, token_file, fetch, clock

    def __call__(self) -> str:
        if not self.token_file.exists():
            raise OAuthError("YouTube の認証がまだです。`atena youtube auth` を実行してください")
        tok = json.loads(self.token_file.read_text(encoding="utf-8"))
        if tok.get("access_token") and tok.get("expires_at", 0) > self.clock():
            return tok["access_token"]
        if not tok.get("refresh_token"):
            raise OAuthError("リフレッシュトークンがありません。`atena youtube auth` をやり直してください")
        client = _client(self.secret_file)
        body = urllib.parse.urlencode({
            "client_id": client["client_id"], "client_secret": client.get("client_secret", ""),
            "refresh_token": tok["refresh_token"], "grant_type": "refresh_token"}).encode()
        new = json.loads(self.fetch("POST", TOKEN_URL, data=body,
                                    headers={"Content-Type": "application/x-www-form-urlencoded"}).decode())
        tok.update(new)
        tok["expires_at"] = self.clock() + new.get("expires_in", 3600) - 60
        _save(self.token_file, tok)
        return tok["access_token"]
