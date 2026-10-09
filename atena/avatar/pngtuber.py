"""内蔵 PNGTuber 表示（OBS のブラウザソース用）(AV-04, AV-09)。

Live2D モデルが無くても、デスクトップアプリで作った立ち絵で配信できる。
キャラ定義の avatar_dir に次の画像を置く（無い感情は neutral を使う）:
  必須   neutral.png, neutral_open.png（口を開けた差分）, joy.png, joy_open.png, ...
  まばたき <感情>_blink.png（目を閉じた差分）, <感情>_blink_open.png（目を閉じて口を開けた差分）
  任意   <感情>_half.png（口を半分開けた差分。小さな声のとき）, <感情>_blink_half.png
まばたき差分が無い感情はまばたきしない（別の感情の目に入れ替わると不自然なため）。
画面側で、呼吸のゆれ・話すときの弾み・表情切替のフェード・感情ごとの小さな動きを付ける。
OBS: ブラウザソース → http://127.0.0.1:8771/ （幅・高さは画像に合わせる、背景透過）
"""

from __future__ import annotations

import json
import mimetypes
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

PAGE = """<!doctype html><html><head><meta charset="utf-8"><title>Atena avatar</title>
<style>
html,body{margin:0;height:100%;background:transparent;overflow:hidden}
#breath,#hold,#fx,#talk{position:absolute;inset:0;transform-origin:50% 100%}
img{position:absolute;bottom:0;left:50%;transform:translateX(-50%);max-width:100%;max-height:100%}
#ghost{transition:opacity .18s ease-out}
.motion #breath{animation:breath 4.2s ease-in-out infinite}
@keyframes breath{0%,100%{transform:scale(1,1)}50%{transform:scale(1.004,1.012)}}
.motion #hold{transition:transform .35s ease-out}
.motion.e-sad #hold{transform:translateY(1.6%)}
.motion.e-worry #hold{transform:translateY(.8%) rotate(-.6deg)}
.motion.e-shy #hold{transform:rotate(-1.6deg)}
.motion #talk{transition:transform .08s ease-out}
.motion.talking #talk{transform:translateY(-.7%)}
.fx-surprise{animation:hop .32s ease-out}
.fx-joy{animation:bounce .5s ease-out}
.fx-angry{animation:shake .3s linear}
@keyframes hop{0%{transform:none}35%{transform:translateY(-4%)}100%{transform:none}}
@keyframes bounce{0%,100%{transform:none}25%{transform:translateY(-2%)}55%{transform:none}75%{transform:translateY(-1%)}}
@keyframes shake{0%,100%{transform:none}20%{transform:translateX(-1%)}40%{transform:translateX(1%)}
 60%{transform:translateX(-.8%)}80%{transform:translateX(.6%)}}
</style></head>
<body><div id="breath"><div id="hold"><div id="fx"><div id="talk">
<img id="a" alt=""><img id="ghost" alt="" style="opacity:0">
</div></div></div></div><script>
const img=document.getElementById('a'),ghost=document.getElementById('ghost'),fx=document.getElementById('fx');
let st={emotion:'neutral',mouth:0,images:{},motion:true,blink:true},blinking=false,shown='';
function frames(){return st.images[st.emotion]||st.images.neutral||{};}
function src(){const f=frames(),m=['closed','half','open'][st.mouth]||'closed';
 const order=m==='half'?['half','open','closed']:m==='open'?['open','half','closed']:['closed'];
 for(const k of order){if(!f[k])continue;return (blinking&&f['blink_'+k])?f['blink_'+k]:f[k];}return '';}
function draw(){const u=src();if(u&&img.getAttribute('src')!==u)img.setAttribute('src',u);}
function setEmotion(e){const old=img.getAttribute('src');
 if(st.motion&&old&&shown&&shown!==e){ghost.style.transition='none';ghost.setAttribute('src',old);ghost.style.opacity=1;
  requestAnimationFrame(()=>{ghost.style.transition='';ghost.style.opacity=0;});
  fx.className='';void fx.offsetWidth;if(['surprise','joy','angry'].includes(e))fx.className='fx-'+e;}
 document.body.className=(st.motion?'motion ':'')+'e-'+e+(st.mouth?' talking':'');shown=e;}
function blink(){const next=2500+Math.random()*3500;
 setTimeout(()=>{const f=frames();if(st.blink&&f.blink_closed){blinking=true;draw();
  setTimeout(()=>{blinking=false;draw();if(Math.random()<.2)setTimeout(()=>{blinking=true;draw();
   setTimeout(()=>{blinking=false;draw();},110);},140);},120);}blink();},next);}
blink();
const ev=new EventSource('/events');ev.onmessage=m=>{const d=JSON.parse(m.data);const changed=d.emotion!==shown;
 st=Object.assign(st,d);if(changed)setEmotion(st.emotion);
 document.body.classList.toggle('talking',st.mouth>0);draw();};
</script></body></html>"""

ALLOWED = {".png", ".webp", ".gif", ".jpg", ".jpeg"}


def parse_frame_name(stem: str) -> tuple[str, str]:
    """ファイル名（拡張子なし）→ (感情, 差分の種類)。
    種類: closed / half / open（目を開けた絵）、blink_closed / blink_half / blink_open（目を閉じた絵）。"""
    mouth = "closed"
    for m in ("open", "half"):
        if stem.endswith("_" + m):
            stem, mouth = stem[:-len(m) - 1], m
            break
    if stem.endswith("_blink"):
        return stem[:-6], "blink_" + mouth
    return stem, mouth


class PngTuberOverlay:
    def __init__(self, host: str = "127.0.0.1", port: int = 8771, open_threshold: float = 0.3,
                 wide_threshold: float = 0.6, *, motion: bool = True, blink: bool = True):
        self.state = {"emotion": "neutral", "mouth": 0, "open": False, "images": {}, "motion": motion,
                      "blink": blink}
        self.open_threshold = open_threshold
        self.wide_threshold = wide_threshold
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
            key, kind = parse_frame_name(p.stem)
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
        """口の開き 0〜1 を 3 段階（閉じ / 半開き / 開き）にする。半開きの絵が無ければ開きの絵を使う。"""
        level = 2 if value >= self.wide_threshold else 1 if value >= self.open_threshold else 0
        self._publish(mouth=level, open=level > 0)

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
