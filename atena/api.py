"""デスクトップアプリ連携用のローカル HTTP API (IN-01〜IN-06)。

- 127.0.0.1 のみで待ち受ける
- 全リクエストに `Authorization: Bearer <token>` が必要。
  ブラウザの他サイトからはカスタムヘッダ付きで送れない（CORS プリフライトに応答しない）ため、
  悪意あるウェブページから事務所を操作されることを防ぐ
- 単一スレッドで処理する（SQLite 接続と LLM 呼び出しを直列化）
"""

from __future__ import annotations

import hmac
import json
import re
import secrets
from datetime import date
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from .character import Character, save_character
from .lounge import RoomMaster, get_session, list_sessions
from .manager import ProjectManager
from .monitor import snapshot_dict

MAX_BODY = 1_000_000
_ID_RE = re.compile(r"^[a-z0-9_-]{1,40}$")


def load_or_create_token(cfg) -> str:
    if cfg.api.token:
        return cfg.api.token
    path: Path = cfg.root / "data" / "api_token"
    if path.exists():
        return path.read_text(encoding="utf-8").strip()
    path.parent.mkdir(parents=True, exist_ok=True)
    token = secrets.token_urlsafe(32)
    path.write_text(token, encoding="utf-8")
    path.chmod(0o600)
    return token


class ApiError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status


def _char_json(c: Character) -> dict:
    return {"id": c.id, "name": c.name, "model": c.model, "persona": c.persona,
            "speaking_style": c.speaking_style, "goals": c.goals, "autonomy_level": c.autonomy_level,
            "tags": c.tags, "voice_id": c.voice_id,
            "voice_caption": c.voice_caption, "voice_captions": c.voice_captions, "avatar_dir": c.avatar_dir,
            "vts_hotkeys": c.vts_hotkeys,
            "specialties": c.specialties, "favorites": c.favorites, "learning_sources": c.learning_sources,
            "talkativeness": c.talkativeness}


class AtenaAPI:
    """ルーティングと処理本体（HTTP から切り離してテストしやすくしている）。"""

    def __init__(self, office):
        self.o = office
        self.pm = ProjectManager(office)
        self.routes = [
            ("GET", r"/api/health", self.health),
            ("GET", r"/api/characters", self.list_characters),
            ("PUT", r"/api/characters/(?P<cid>[^/]+)", self.put_character),
            ("POST", r"/api/characters/(?P<cid>[^/]+)/reply", self.reply),
            ("POST", r"/api/guardian/check", self.guardian_check),
            ("GET", r"/api/characters/(?P<cid>[^/]+)/expertise", self.list_expertise),
            ("POST", r"/api/characters/(?P<cid>[^/]+)/expertise", self.add_expertise),
            ("GET", r"/api/monitor", self.monitor),
            ("GET", r"/api/schedule", self.schedule),
            ("GET", r"/api/ranking", self.ranking),
            ("GET", r"/api/approvals", self.approvals),
            ("POST", r"/api/approvals/(?P<aid>\d+)", self.decide),
            ("POST", r"/api/lounge", self.lounge),
            ("GET", r"/api/lounge/sessions", self.lounge_sessions),
            ("GET", r"/api/lounge/sessions/(?P<sid>[0-9A-Za-z_-]+)", self.lounge_session),
            ("GET", r"/api/report", self.report),
        ]

    def dispatch(self, method: str, path: str, query: dict, body: dict):
        for m, pattern, fn in self.routes:
            match = re.fullmatch(pattern, path)
            if match and m == method:
                return fn(query=query, body=body, **match.groupdict())
        raise ApiError(404, "not found")

    # ---- handlers -------------------------------------------------------
    def health(self, **_):
        return {"ok": True, "characters": len(self.o.characters)}

    def list_characters(self, **_):
        return {"characters": [_char_json(c) for c in self.o.characters.values()]}

    def put_character(self, cid: str, body: dict, **_):
        """デスクトップアプリで作ったキャラを登録・更新する（IN-02）。"""
        if not _ID_RE.match(cid):
            raise ApiError(400, "id は英小文字・数字・_- の40文字以内")
        if not body.get("name"):
            raise ApiError(400, "name は必須です")
        for k in ("voice_captions", "vts_hotkeys"):
            v = body.get(k)
            if v is not None and not (isinstance(v, dict) and all(
                    isinstance(a, str) and isinstance(b, str) for a, b in v.items())):
                raise ApiError(400, f"{k} は {{感情: 文字列}} のオブジェクトで指定してください")
        for k in ("name", "model", "persona", "speaking_style", "voice_id", "voice_caption", "avatar_dir"):
            if k in body and not isinstance(body[k], str):
                raise ApiError(400, f"{k} は文字列で指定してください")
        for k in ("goals", "tags", "specialties", "favorites", "learning_sources"):
            if k in body and not (isinstance(body[k], list) and all(isinstance(x, str) for x in body[k])):
                raise ApiError(400, f"{k} は文字列の配列で指定してください")
        if "autonomy_level" in body and body["autonomy_level"] not in (0, 1, 2, 3):
            raise ApiError(400, "autonomy_level は 0〜3")
        t = body.get("talkativeness")
        if t is not None and (isinstance(t, bool) or not isinstance(t, (int, float)) or not 0 <= t <= 1):
            raise ApiError(400, "talkativeness は 0〜1 の数値（未設定なら null）")
        fields = Character.__dataclass_fields__
        c = Character(**{k: v for k, v in body.items() if k in fields and k != "id"}, id=cid)
        if cid in self.o.characters:  # 既存キャラは読み込んだファイルに書き戻す
            c._source = getattr(self.o.characters[cid], "_source", None)
        save_character(c, self.o.cfg.characters_dir)
        self.o.reload_characters()
        self.o.audit.record("desktop_app", "character:upsert", {"id": cid})
        return {"character": _char_json(self.o.character(cid))}

    def reply(self, cid: str, body: dict, **_):
        text = str(body.get("comment", ""))
        author = str(body.get("author", "視聴者"))
        reply = self.o.agent(cid).reply_to_comment(text, author, platform=str(body.get("platform", "app")),
                                                   use_llm_judge=body.get("use_llm_judge"))
        return {"reply": reply}

    def list_expertise(self, cid: str, query: dict, **_):
        c = self.o.character(cid)
        return {"stats": self.o.expertise.stats(c.id),
                "facts": [dict(r) for r in self.o.expertise.list(c.id, topic=query.get("topic"))]}

    def add_expertise(self, cid: str, body: dict, **_):
        """オーナーがアプリから知識を登録する（最優先の情報源）。"""
        c = self.o.character(cid)
        if not isinstance(body.get("topic"), str) or not isinstance(body.get("content"), str):
            raise ApiError(400, "topic と content（文字列）が必要です")
        r = self.o.expertise.add(c.id, body["topic"], body["content"], source_type="owner", source_ref="オーナー(app)")
        return {"id": r.id, "status": r.status, "superseded": r.superseded, "disputed": r.disputed}

    def guardian_check(self, body: dict, **_):
        """アプリ側で生成した発言を公開前に検査する（IN-03）。"""
        v = self.o.guardian.check_output(str(body.get("text", "")), speaker=body.get("speaker"),
                                         use_llm=body.get("use_llm_judge"), context="desktop_app",
                                         record=bool(body.get("record", True)))
        return {"action": v.action, "text": v.text, "categories": v.categories, "reasons": v.reasons}

    def monitor(self, **_):
        h = self.o.monitor.check()
        return {"status": h.status, "reasons": h.reasons, "snapshot": snapshot_dict(h.snapshot)}

    def schedule(self, query: dict, **_):
        rows = self.o.scheduler.list(start_from=query.get("since", date.today().isoformat()))
        return {"schedule": [dict(r) for r in rows]}

    def ranking(self, **_):
        names = self.o.names()
        return {"ranking": [{"rank": e.rank, "character_id": e.character_id,
                             "name": names.get(e.character_id, e.character_id), "total_jpy": e.total,
                             "growth_pct": e.growth_pct, "breakdown": e.breakdown}
                            for e in self.o.current_ranking()]}

    def approvals(self, **_):
        return {"approvals": [dict(a) for a in self.o.approvals.pending()]}

    def decide(self, aid: str, body: dict, **_):
        if "approve" not in body:
            raise ApiError(400, "approve (true/false) が必要です")
        problems = self.pm.decide(int(aid), bool(body["approve"]), actor="owner(app)")
        return {"ok": True, "problems": problems}

    def lounge(self, body: dict, **_):
        ids = body.get("participants") or list(self.o.characters)
        res = RoomMaster(self.o).run(ids, topic=body.get("topic"), turns=body.get("turns"))
        return {"session_id": res.session_id, "topic": res.topic, "mode": res.mode, "host": res.host,
                "transcript": [{"speaker": s, "text": t} for s, t in res.transcript],
                "warnings": res.warnings, "knowledge_ids": res.knowledge_ids, "highlight_ids": res.highlight_ids}

    def lounge_sessions(self, query: dict, **_):
        """ラウンジ閲覧アプリ用: セッション一覧（新しい順）。"""
        try:
            limit = max(1, min(100, int(query.get("limit", 20))))
        except ValueError:
            raise ApiError(400, "limit は数値")
        return {"sessions": list_sessions(self.o.conn, limit)}

    def lounge_session(self, sid: str, **_):
        s = get_session(self.o.conn, sid)
        if s is None:
            raise ApiError(404, "not found")
        return s

    def report(self, **_):
        return {"markdown": self.pm.status_report()}


def make_handler(api: AtenaAPI, token: str):
    class Handler(BaseHTTPRequestHandler):
        server_version = "Atena/0.2"

        def _send(self, status: int, payload: dict):
            data = json.dumps(payload, ensure_ascii=False, default=str).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def _handle(self, method: str):
            auth = self.headers.get("Authorization", "")
            if not hmac.compare_digest(auth.encode(), f"Bearer {token}".encode()):
                return self._send(401, {"error": "unauthorized"})
            url = urlparse(self.path)
            query = {k: v[0] for k, v in parse_qs(url.query).items()}
            body: dict = {}
            length = int(self.headers.get("Content-Length") or 0)
            if length > MAX_BODY:
                return self._send(413, {"error": "body too large"})
            if length:
                try:
                    body = json.loads(self.rfile.read(length).decode("utf-8"))
                except (ValueError, UnicodeDecodeError):
                    return self._send(400, {"error": "invalid json"})
                if not isinstance(body, dict):
                    return self._send(400, {"error": "json object required"})
            try:
                self._send(200, api.dispatch(method, url.path, query, body))
            except ApiError as e:
                self._send(e.status, {"error": str(e)})
            except KeyError as e:
                self._send(404, {"error": str(e).strip("'\"")})
            except (ValueError, TypeError, RuntimeError) as e:
                self._send(400, {"error": str(e)})
            except Exception as e:  # noqa: BLE001 - 接続を落とさずアプリに理由を返す
                self._send(500, {"error": f"internal error: {type(e).__name__}"})

        def do_GET(self):  # noqa: N802
            self._handle("GET")

        def do_POST(self):  # noqa: N802
            self._handle("POST")

        def do_PUT(self):  # noqa: N802
            self._handle("PUT")

        def log_message(self, fmt, *args):
            pass

    return Handler


def make_server(office, host: str = "127.0.0.1", port: int = 8765, token: str | None = None) -> HTTPServer:
    if host not in ("127.0.0.1", "localhost", "::1"):
        raise ValueError("安全のため API はローカルホストでのみ公開できます")
    token = token or load_or_create_token(office.cfg)
    return HTTPServer((host, port), make_handler(AtenaAPI(office), token))
