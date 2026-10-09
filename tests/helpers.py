import tempfile
from pathlib import Path

from atena.character import Character
from atena.config import load_config
from atena.llm import ScriptedLLM
from atena.monitor import Snapshot
from atena.office import Office


def make_office(responses=None, *, snapshot=None, secrets: str = "", ng_words: str = "死ね\n殺す\n",
                config_toml: str = "", default="了解です！"):
    tmp = Path(tempfile.mkdtemp())
    (tmp / "config").mkdir()
    (tmp / "config" / "ng_words.txt").write_text(ng_words, encoding="utf-8")
    if secrets:
        (tmp / "config" / "owner_secrets.toml").write_text(secrets, encoding="utf-8")
    (tmp / "config" / "atena.toml").write_text(config_toml or "[guardian]\nuse_llm_judge = false\n",
                                               encoding="utf-8")
    cfg = load_config(tmp)
    chars = {
        "hikari": Character(id="hikari", name="ひかり", persona="ゲームが大好きな元気系AIキャラクター。負けず嫌いだけど素直。"),
        "shizuku": Character(id="shizuku", name="しずく", persona="読書と紅茶が好きな癒し系AIキャラクター。"),
    }
    llm = ScriptedLLM(responses, default=default)
    snap = snapshot or Snapshot(cpu_pct=20, ram_pct=40, gpu_pct=10, vram_used_mb=2000,
                                vram_total_mb=16000, gpu_temp_c=50)
    office = Office(cfg, llm=llm, db_path=":memory:", characters=chars, sampler=lambda: snap)
    return office, llm
