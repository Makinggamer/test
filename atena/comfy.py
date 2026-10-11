"""ComfyUI でショートの背景を作る (SH-06)。

話題から背景のプロンプト（英語のタグ・人物なし）を LLM で作り、ComfyUI の API に
ワークフロー（API 形式の JSON）を送って 1 枚生成し、data/shorts/backgrounds/ に保存する。
ワークフローの中の文字列 %PROMPT% / %NEGATIVE% / %SEED% / %WIDTH% / %HEIGHT% / %CKPT% を差し替える。
ワークフローのファイルが無ければ、標準ノードだけの簡単な SDXL 用（DEFAULT_WORKFLOW）を使う。
重い処理なので、他の重い処理のロックがあれば作らず、作っている間は自分がロックを置く。
"""

from __future__ import annotations

import hashlib
import json
import os
import random
import re
import time
from datetime import datetime
from pathlib import Path

from . import http
from .llm import LLMError, parse_json

BG_PROMPT = """\
縦長の動画の背景イラストを画像生成 AI で作ります。会話の話題: {topic}
アニメ調の 2 人のキャラクターが手前に立って雑談する「休憩室・ラウンジ」の背景です。
話題に合う小物や窓の外の景色を少し入れ、人物は入れない。文字・看板・ロゴは入れない。実在の店名や作品は入れない。
英語のタグをカンマ区切りで 15〜25 個。
JSON だけを出力: {{"prompt": "tag, tag, ..."}}"""

STYLE = ("masterpiece, best quality, anime background, scenery, no humans, cozy lounge interior, "
         "soft lighting, depth of field, vertical composition")
NEGATIVE = ("person, people, girl, boy, character, face, hands, text, watermark, signature, logo, letters, "
            "lowres, blurry, jpeg artifacts, worst quality")

# 標準ノードだけの SDXL ワークフロー（API 形式）。checkpoint は [shorts] comfy_checkpoint
DEFAULT_WORKFLOW = {
    "4": {"class_type": "CheckpointLoaderSimple", "inputs": {"ckpt_name": "%CKPT%"}},
    "5": {"class_type": "EmptyLatentImage", "inputs": {"width": "%WIDTH%", "height": "%HEIGHT%", "batch_size": 1}},
    "6": {"class_type": "CLIPTextEncode", "inputs": {"text": "%PROMPT%", "clip": ["4", 1]}},
    "7": {"class_type": "CLIPTextEncode", "inputs": {"text": "%NEGATIVE%", "clip": ["4", 1]}},
    "3": {"class_type": "KSampler", "inputs": {
        "seed": "%SEED%", "steps": 28, "cfg": 6.0, "sampler_name": "euler_ancestral", "scheduler": "normal",
        "denoise": 1.0, "model": ["4", 0], "positive": ["6", 0], "negative": ["7", 0], "latent_image": ["5", 0]}},
    "8": {"class_type": "VAEDecode", "inputs": {"samples": ["3", 0], "vae": ["4", 2]}},
    "9": {"class_type": "SaveImage", "inputs": {"filename_prefix": "atena_bg", "images": ["8", 0]}},
}


class ComfyError(RuntimeError):
    pass


def fill(workflow, values: dict):
    """ワークフローの中の %KEY% を差し替える。文字列全体が %KEY% なら値の型（数値）のまま入れる。"""
    if isinstance(workflow, dict):
        return {k: fill(v, values) for k, v in workflow.items()}
    if isinstance(workflow, list):
        return [fill(v, values) for v in workflow]
    if isinstance(workflow, str):
        m = re.fullmatch(r"%([A-Z_]+)%", workflow)
        if m and m.group(1) in values:
            return values[m.group(1)]
        return re.sub(r"%([A-Z_]+)%", lambda x: str(values.get(x.group(1), x.group(0))), workflow)
    return workflow


class ComfyClient:
    def __init__(self, host: str = "http://127.0.0.1:8188", *, fetch=http.request, sleep=time.sleep,
                 timeout: float = 600):
        self.host = host.rstrip("/")
        self.fetch = fetch
        self.sleep = sleep
        self.timeout = timeout

    def ready(self) -> bool:
        try:
            self.fetch("GET", self.host + "/system_stats", timeout=5)
            return True
        except http.HTTPError:
            return False

    def generate(self, workflow: dict) -> bytes:
        """ワークフローを実行し、最初に保存された画像を返す。"""
        try:
            r = json.loads(self.fetch("POST", self.host + "/prompt", json_body={"prompt": workflow},
                                      timeout=30).decode("utf-8"))
        except http.HTTPError as e:
            raise ComfyError(f"ComfyUI に送れませんでした（起動していますか？）: {e} {e.body[:300]}") from e
        pid = r.get("prompt_id")
        if not pid:
            raise ComfyError(f"ComfyUI の応答が想定外です: {str(r)[:300]}")
        waited = 0.0
        while waited < self.timeout:
            hist = json.loads(self.fetch("GET", f"{self.host}/history/{pid}", timeout=30).decode("utf-8"))
            item = hist.get(pid)
            if item:
                status = item.get("status", {})
                if status.get("status_str") == "error":
                    raise ComfyError(f"ComfyUI での生成に失敗しました: {str(status.get('messages'))[:300]}")
                for out in item.get("outputs", {}).values():
                    for img in out.get("images", []):
                        return self.fetch("GET", self.host + "/view", params={
                            "filename": img["filename"], "subfolder": img.get("subfolder", ""),
                            "type": img.get("type", "output")}, timeout=60)
                if status.get("completed"):
                    raise ComfyError("ComfyUI のワークフローが画像を保存していません（SaveImage ノードが必要）")
            self.sleep(2)
            waited += 2
        raise ComfyError(f"ComfyUI の生成が {self.timeout:.0f} 秒で終わりませんでした")


class BackgroundMaker:
    def __init__(self, office, client: ComfyClient | None = None, *, rng: random.Random | None = None):
        self.o = office
        c = office.cfg.shorts
        self.client = client or ComfyClient(c.comfy_host)
        self.rng = rng or random.Random()

    def prompt_for(self, topic: str) -> str:
        tags = ""
        try:
            data = parse_json(self.o.llm.chat(self.o.cfg.ollama.staff_model, [{"role": "user", "content": BG_PROMPT.format(
                topic=topic)}], json_mode=True))
            tags = str(data.get("prompt", "")).strip()
        except (LLMError, AttributeError, TypeError, ValueError):
            tags = ""
        tags = re.sub(r"[^\x20-\x7e]", "", tags)[:400]  # 英語のタグだけ（日本語や記号の混入を落とす）
        return f"{STYLE}, {tags}" if tags else STYLE

    def workflow(self) -> dict:
        p = self.o.cfg.path(self.o.cfg.shorts.comfy_workflow) if self.o.cfg.shorts.comfy_workflow else None
        if p and p.exists():
            return json.loads(p.read_text(encoding="utf-8"))
        return DEFAULT_WORKFLOW

    def cached(self, topic: str) -> Path | None:
        p = self._path(topic)
        return p if p.exists() else None

    def _path(self, topic: str) -> Path:
        h = hashlib.sha1(topic.encode("utf-8")).hexdigest()[:10]
        return self.o.cfg.path(self.o.cfg.shorts.out_dir) / "backgrounds" / f"bg-{h}.png"

    def make(self, topic: str, *, force: bool = False) -> Path:
        c = self.o.cfg.shorts
        if not self.o.cfg.shorts.comfy_workflow or not self.o.cfg.path(c.comfy_workflow).exists():
            if not c.comfy_checkpoint:
                raise ComfyError("[shorts] comfy_workflow（API 形式の JSON）か comfy_checkpoint を設定してください")
        if not force and (hit := self.cached(topic)):
            return hit
        lock_name = self.o.cfg.resources.stream_lock_file
        lock = Path(lock_name).expanduser() if lock_name else None
        if lock and lock.exists() and not force:
            raise ComfyError(f"他の重い処理が動いているので背景を作りません（{lock}）")
        values = {"PROMPT": self.prompt_for(topic), "NEGATIVE": NEGATIVE, "SEED": self.rng.randrange(2 ** 31),
                  "WIDTH": c.comfy_width, "HEIGHT": c.comfy_height, "CKPT": c.comfy_checkpoint}
        own = False
        if lock and not lock.exists():
            lock.parent.mkdir(parents=True, exist_ok=True)
            lock.write_text(json.dumps({"pid": os.getpid(), "what": "Atena ショートの背景（ComfyUI）",
                                        "started": datetime.now().strftime("%H:%M")}, ensure_ascii=False),
                            encoding="utf-8")
            own = True
        try:
            img = self.client.generate(fill(self.workflow(), values))
        finally:
            if own:
                try:
                    if json.loads(lock.read_text(encoding="utf-8")).get("pid") == os.getpid():
                        lock.unlink()
                except (OSError, ValueError):
                    pass
        out = self._path(topic)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_bytes(img)
        out.with_suffix(".json").write_text(json.dumps({"topic": topic, "prompt": values["PROMPT"],
                                                        "seed": values["SEED"]}, ensure_ascii=False, indent=2),
                                            encoding="utf-8")
        self.o.audit.record("ショート制作", "short:background", {"topic": topic, "path": str(out)})
        return out
