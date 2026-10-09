"""内蔵 PNGTuber 表示（OBS のブラウザソース用）(AV-04)。

Live2D モデルが無くても、デスクトップアプリで作った立ち絵で配信できる。
キャラ定義の avatar_dir に次の画像を置く（無い感情は neutral を使う）:
  neutral.png, neutral_open.png（口を開けた差分）, joy.png, joy_open.png, ... 
OBS: ブラウザソース → http://127.0.0.1:8771/ （幅・高さは画像に合わせる、背景透過）
"""

from __future__ import annotations

import json
import mimetypes
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

PAGE = """<!doctype html><html><head><meta charset="utf-8"><title>Atena avatar</title>
<style>html,body{margin:0;background:transparent;overflow:hidden}
img{position:absolute;bottom:0;left:50%;transform:translateX(-50%);max-width:100%;max-height:100%}</style></head>
<body><img id="a" alt=""><script>
const img=document.getElementById('a');let st={emotion:'neutral',open:false,images:{}};
function src(){const e=st.images[st.emotion]?st.emotion:'neutral';const s=st.images[e]||{};
 return (st.open&&s.open)?s.open:(s.closed||'');}
function draw(){const u=src();if(u&&img.getAttribute('src')!==u)img.setAttribute('src',u);}
const ev=new EventSource('/events');ev.onmessage=m=>{st=Object.assign(st,JSON.parse(m.data));draw();};
</script></body></html>"""

ALLOWED = {".png", ".webp", ".gif", ".jpg", ".jpeg"}


class PngTuberOverlay:
    def __init__(self, host: str = "127.0.0.1", port: int = 8771, open_threshold: float = 0.3):
        self.state = {"emotion": "neutral", "open": False, "images": {}}
        self.open_threshold = open_threshold
        self.avatar_dir: Path | None = None
        self._cv = threading.Condition()
        self._version = 0
        self._stop = False
        self.server = ThreadingHTTPServer((host, port), self._handler())
        self.server.daemon_threads = True
        self.port = self.server.server_port
        self._thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self._thread.start()

    def _images(self, d: Path) -> dict:
        out: dict = {}
        for p in d.iterdir() if d.is_dir() else []:
            if p.suffix.lower() not in ALLOWED:
                continue
            stem = p.stem
            key, kind = (stem[:-5], "open") if stem.endswith("_open") else (stem, "closed")
            out.setdefault(key, {})[kind] = f"/img/{p.name}"
        return out

    def _publish(self, **changes) -> None:
        with self._cv:
            if all(self.state.get(k) == v for k, v in changes.items()):
                return
            self.state.update(changes)
            self._version += 1
            self._cv.notify_all()

    def set_emotion(self, character, emotion: str) -> None:
        d = Path(character.avatar_dir).expanduser() if getattr(character, "avatar_dir", "") else None
        if d != self.avatar_dir:
            self.avatar_dir = d
            self._publish(images=self._images(d) if d else {})
        self._publish(emotion=emotion)

    def mouth(self, value: float) -> None:
        self._publish(open=value >= self.open_threshold)

    def close(self) -> None:
        with self._cv:
            self._stop = True
            self._cv.notify_all()
        self.server.shutdown()
        self.server.server_close()

    def _handler(self):
        overlay = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_GET(self):  # noqa: N802
                if self.path == "/":
                    body = PAGE.encode("utf-8")
                    self.send_response(200)
                    self.send_header("Content-Type", "text/html; charset=utf-8")
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                elif self.path == "/events":
                    self._events()
                elif self.path.startswith("/img/"):
                    self._image(self.path[5:])
                else:
                    self.send_error(404)

            def _image(self, name: str):
                d = overlay.avatar_dir
                # パス区切りや親ディレクトリ参照は拒否（avatar_dir 直下の画像のみ配信）
                if not d or "/" in name or "\\" in name or name.startswith("."):
                    return self.send_error(404)
                p = d / name
                if p.suffix.lower() not in ALLOWED or not p.is_file():
                    return self.send_error(404)
                data = p.read_bytes()
                self.send_response(200)
                self.send_header("Content-Type", mimetypes.guess_type(p.name)[0] or "application/octet-stream")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def _events(self):
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Cache-Control", "no-cache")
                self.end_headers()
                seen = -1
                try:
                    while True:
                        with overlay._cv:
                            overlay._cv.wait_for(lambda: overlay._version != seen or overlay._stop, timeout=15)
                            if overlay._stop:
                                return
                            payload, seen = json.dumps(overlay.state), overlay._version
                        self.wfile.write(f"data: {payload}\n\n".encode("utf-8"))
                        self.wfile.flush()
                except (BrokenPipeError, ConnectionResetError):
                    return

        return H
