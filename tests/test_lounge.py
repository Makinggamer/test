import random
import unittest
from datetime import date

from atena.character import Character
from atena.lounge import HOBBY, ROOM_MASTER, RoomMaster, get_session, list_sessions
from atena.manager import ProjectManager
from atena.monitor import Snapshot

from .helpers import make_office


def _prompt(call) -> str:
    return call["messages"][-1]["content"]


def _lounge_calls(llm):
    return [c for c in llm.calls if "休憩所（ラウンジ）" in _prompt(c)]


class TalkativenessTest(unittest.TestCase):
    def test_explicit_value_wins(self):
        o, llm = make_office()
        o.characters["hikari"].talkativeness = 0.9
        self.assertEqual(RoomMaster(o).talkativeness(o.characters["hikari"]), 0.9)
        self.assertEqual(llm.calls, [])

    def test_inferred_from_persona_and_cached(self):
        o, llm = make_office(['{"talkativeness": 0.2}'])
        rm = RoomMaster(o)
        c = o.characters["shizuku"]
        self.assertEqual(rm.talkativeness(c), 0.2)
        self.assertEqual(rm.talkativeness(c), 0.2)
        self.assertEqual(len(llm.calls), 1)  # 2回目はキャッシュ
        self.assertIn("読書と紅茶", _prompt(llm.calls[0]))
        c.persona += "実はおしゃべり。"  # 人格が変われば推定し直す
        rm.talkativeness(c)
        self.assertEqual(len(llm.calls), 2)

    def test_inference_failure_is_neutral(self):
        o, _ = make_office(["わからない"])
        self.assertEqual(RoomMaster(o).talkativeness(o.characters["hikari"]), 0.5)

    def test_length_hint_follows_personality(self):
        o, llm = make_office()
        o.agent("shizuku").lounge_line("t", [], talkativeness=0.1)
        o.agent("hikari").lounge_line("t", [], talkativeness=0.9)
        self.assertIn("口数が少ない", _prompt(llm.calls[0]))
        self.assertIn("話好き", _prompt(llm.calls[1]))


class TurnTakingTest(unittest.TestCase):
    def _office3(self, responses=None, default="なるほどね。"):
        o, llm = make_office(responses, default=default)
        o.characters["mio"] = Character(id="mio", name="ミオ", persona="古書店員。物静か。")
        o.characters["hikari"].talkativeness = 0.9
        o.characters["shizuku"].talkativeness = 0.8
        o.characters["mio"].talkativeness = 0.05
        return o, llm

    def test_quiet_character_gets_a_turn(self):
        o, llm = self._office3(["配信の最初に今日の流れを話すといいよ", "雑談の時間を長めに取ってる", "スパチャのお礼は名前を呼ぶ",
                                 "初見さんには一言あいさつ", "企画は週ごとに変えてる", "BGMは静かめにしてる",
                                 "サムネは顔を大きく", "告知は前日の夜に出す", "質問コーナーを作った",
                                 "終わりに次回予告をする", "コラボは月一くらい", "アーカイブにも章を付ける"])
        res = RoomMaster(o, jitter=0).run(["hikari", "shizuku", "mio"], topic="t", turns=6)
        speakers = [w for w, _ in res.transcript]
        self.assertIn("ミオ", speakers)  # 無口でも話を振られて発言する
        self.assertGreater(speakers.count("ひかり"), speakers.count("ミオ"))  # 口数は性格どおり
        hints = [_prompt(c) for c in _lounge_calls(llm)]
        self.assertTrue(any("ミオさんに話を振って" in h for h in hints))
        # 振られなかった（台本どおり名前を出さなかった）ので、ルームマスターが振る
        self.assertIn((ROOM_MASTER, "ミオさんはどう思う？"), res.transcript)
        mio_turn = next(i for i, (w, _) in enumerate(res.transcript) if w == "ミオ")
        self.assertEqual(res.transcript[mio_turn - 1], (ROOM_MASTER, "ミオさんはどう思う？"))

    def test_named_question_passes_the_turn(self):
        o, llm = self._office3(["ミオさんは最近どんな本を読んだ？", "……古い詩集を少し。"])
        res = RoomMaster(o, jitter=0).run(["hikari", "shizuku", "mio"], topic="t", turns=2)
        self.assertEqual([w for w, _ in res.transcript[1:3]], ["ひかり", "ミオ"])
        self.assertIn("話を振られた", _prompt(_lounge_calls(llm)[1]))
        self.assertNotIn((ROOM_MASTER, "ミオさんはどう思う？"), res.transcript)

    def test_jitter_varies_order(self):
        orders = set()
        for seed in range(8):
            o, _ = self._office3()
            o.characters["mio"].talkativeness = 0.8
            res = RoomMaster(o, rng=random.Random(seed), jitter=0.5).run(["hikari", "shizuku", "mio"],
                                                                           topic="t", turns=3)
            orders.add(tuple(w for w, _ in res.transcript if w != ROOM_MASTER))
        self.assertGreater(len(orders), 1)


class HobbyTopicTest(unittest.TestCase):
    def _office(self, responses=None):
        o, llm = make_office(responses, default="へえ、そうなんだ！")
        o.characters["shizuku"].favorites = ["紅茶"]
        o.characters["shizuku"].specialties = ["図書館司書"]
        for c in o.characters.values():
            c.talkativeness = 0.5
        return o, llm

    def test_explicit_favorite_makes_host(self):
        o, llm = self._office()
        res = RoomMaster(o, jitter=0).run(["hikari", "shizuku"], topic="紅茶", turns=3)
        self.assertEqual(res.mode, HOBBY)
        self.assertEqual(res.host, "shizuku")
        self.assertEqual(res.topic, "しずくさんの好きなもの「紅茶」")
        self.assertEqual(res.transcript[1][0], "しずく")  # 主役から話す
        prompts = [_prompt(c) for c in _lounge_calls(llm)]
        self.assertIn("あなたが主役", prompts[0])
        self.assertIn("聞く側", prompts[1])
        self.assertIn("知ったかぶりしない", prompts[1])
        self.assertEqual(res.knowledge_ids, [])  # 雑談の生成内容はナレッジにしない
        self.assertFalse(any("ノウハウだけを抽出" in _prompt(c) for c in llm.calls))
        self.assertTrue(o.memory.recall("hikari", "紅茶"))

    def test_host_recalls_own_expertise(self):
        o, llm = self._office()
        o.expertise.add("shizuku", "紅茶", "ダージリンは春摘みをファーストフラッシュと呼ぶ",
                        source_type="owner", source_ref="t")
        RoomMaster(o, jitter=0).run(["hikari", "shizuku"], topic="紅茶", turns=1)
        self.assertIn("ファーストフラッシュ", _lounge_calls(llm)[0]["messages"][0]["content"])

    def test_auto_pick_prefers_unused_subject(self):
        o, _ = self._office()
        o.cfg.lounge.hobby_ratio = 1.0
        rm = RoomMaster(o, rng=random.Random(1), jitter=0)
        first = rm.run(["hikari", "shizuku"], turns=1)
        second = rm.run(["hikari", "shizuku"], turns=1)
        self.assertEqual({first.topic, second.topic},
                         {"しずくさんの好きなもの「紅茶」", "しずくさんの仕事「図書館司書」"})

    def test_business_when_no_hobbies(self):
        o, _ = make_office(default="{}")
        o.cfg.lounge.hobby_ratio = 1.0
        for c in o.characters.values():
            c.talkativeness = 0.5
        res = RoomMaster(o, jitter=0).run(["hikari", "shizuku"], turns=1)
        self.assertEqual(res.mode, "business")

    def test_session_log(self):
        o, _ = self._office()
        res = RoomMaster(o, jitter=0).run(["hikari", "shizuku"], topic="紅茶", turns=2)
        s = list_sessions(o.conn)[0]
        self.assertEqual((s["session_id"], s["mode"], s["subject"], s["messages"]),
                         (res.session_id, HOBBY, "紅茶", 2))
        full = get_session(o.conn, res.session_id)
        self.assertEqual(full["participants"], ["hikari", "shizuku"])
        self.assertEqual(full["messages"][0]["speaker"], ROOM_MASTER)
        self.assertIsNone(get_session(o.conn, "nope"))


class DailyLoungeTest(unittest.TestCase):
    def test_daily_opens_lounge(self):
        o, _ = make_office(default="{}")
        rep = ProjectManager(o).daily_cycle(date(2026, 10, 9), learn=False)
        self.assertIn("session_id", rep.lounge)
        self.assertEqual(len(list_sessions(o.conn)), 1)

    def test_skipped_when_busy(self):
        hot = Snapshot(cpu_pct=99, ram_pct=97, gpu_pct=99, vram_used_mb=15900, vram_total_mb=16000, gpu_temp_c=95)
        o, _ = make_office(default="{}", snapshot=hot)
        rep = ProjectManager(o).daily_cycle(date(2026, 10, 9), learn=False)
        self.assertIn("skipped", rep.lounge)
        self.assertEqual(list_sessions(o.conn), [])

    def test_disabled(self):
        o, _ = make_office(default="{}")
        rep = ProjectManager(o).daily_cycle(date(2026, 10, 9), learn=False, lounge=False)
        self.assertIsNone(rep.lounge)


class LoungeAPITest(unittest.TestCase):
    def test_talkativeness_and_sessions(self):
        from atena.api import ApiError, AtenaAPI
        o, _ = make_office(default="{}")
        api = AtenaAPI(o)
        with self.assertRaises(ApiError):
            api.dispatch("PUT", "/api/characters/mio", {}, {"name": "ミオ", "talkativeness": 2})
        api.dispatch("PUT", "/api/characters/mio", {}, {"name": "ミオ", "talkativeness": 0.2})
        self.assertEqual(o.characters["mio"].talkativeness, 0.2)
        api.dispatch("PUT", "/api/characters/sora", {}, {"name": "ソラ", "talkativeness": 0.7})
        r = api.dispatch("POST", "/api/lounge", {}, {"participants": ["sora", "mio"], "turns": 1})
        lst = api.dispatch("GET", "/api/lounge/sessions", {}, {})
        self.assertEqual(lst["sessions"][0]["session_id"], r["session_id"])
        one = api.dispatch("GET", f"/api/lounge/sessions/{r['session_id']}", {}, {})
        self.assertEqual(one["topic"], r["topic"])


if __name__ == "__main__":
    unittest.main()


class NaturalnessTest(unittest.TestCase):
    def test_clean_line(self):
        from atena.lounge import clean_line
        self.assertEqual(clean_line("猫は人を待ってくれるんだよね。 getaway"), "猫は人を待ってくれるんだよね。")
        self.assertEqual(clean_line("今日は AI の話をしよう"), "今日は AI の話をしよう")      # 大文字の略語は残す
        self.assertEqual(clean_line("BOOTHで販売したよ"), "BOOTHで販売したよ")

    def test_repeat_is_retried_then_skipped(self):
        o, llm = make_office(["猫は賢いよね", "へえ〜そうなんだ", "猫は優しいよね", "へえ〜そうなんだ", "へえ〜そうなんだね"],
                             default="{}")
        o.characters["shizuku"].favorites = ["猫"]
        for c in o.characters.values():
            c.talkativeness = 0.5
        res = RoomMaster(o, jitter=0, listeners=[]).run(["hikari", "shizuku"], topic="猫", turns=4)
        lines = [t for w, t in res.transcript if w == "ひかり"]
        self.assertEqual(lines, ["へえ〜そうなんだ"])               # 2 回目の同じ相づちは出さない
        self.assertEqual(res.repeats, 1)
        retry = [c["messages"][-1]["content"] for c in llm.calls if "前の発言と同じ" in c["messages"][-1]["content"]]
        self.assertEqual(len(retry), 1)
        self.assertTrue(any("すでに言ったこと" in c["messages"][-1]["content"] for c in llm.calls))


class HumanlikeTest(unittest.TestCase):
    def test_thought_is_hidden_and_say_is_posted(self):
        o, llm = make_office(['{"thought": "猫の話は苦手だな…", "say": "わたしは犬派なんだけど、猫のどこが好き？"}',
                              '{"thought": "聞かれた", "say": "気まぐれなところ、かな。"}'], default="{}")
        o.characters["shizuku"].favorites = ["猫"]
        o.characters["hikari"].first_person = "ボク"
        o.characters["hikari"].sampling = {"temperature": 1.1}
        for c in o.characters.values():
            c.talkativeness = 0.5
        res = RoomMaster(o, jitter=0, listeners=[]).run(["hikari", "shizuku"], topic="猫", turns=2)
        texts = [t for w, t in res.transcript if w != ROOM_MASTER]
        self.assertEqual(texts, ["わたしは犬派なんだけど、猫のどこが好き？", "気まぐれなところ、かな。"])
        self.assertFalse(any("苦手だな" in t for _, t in res.transcript))      # 本音は出さない
        calls = [c for c in llm.calls if "休憩所" in c["messages"][-1]["content"]]
        self.assertTrue(calls[0]["json_mode"])
        self.assertIn("使わない言い回し", calls[0]["messages"][-1]["content"])

    def test_ai_opener_retried_then_stripped(self):
        from atena.agent import ai_opener, strip_opener
        self.assertEqual(ai_opener("なるほど、猫は賢いですね"), "なるほど")
        self.assertEqual(strip_opener("なるほど、猫は賢いですね"), "猫は賢いですね")
        self.assertIsNone(ai_opener("わたしは犬派かな"))
        o, llm = make_office(['{"say": "なるほど、猫っていいよね"}', '{"say": "確かに、猫っていいよね"}'], default="{}")
        for c in o.characters.values():
            c.talkativeness = 0.5
        res = RoomMaster(o, jitter=0, listeners=[]).run(["hikari", "shizuku"], topic="t", turns=1)
        self.assertEqual(res.transcript[1][1], "猫っていいよね")

    def test_style_section_in_prompt_but_not_leak_checked(self):
        from atena.character import Character
        c = Character(id="mio", name="ミオ", first_person="わたし", endings=["〜ですね"],
                      sample_lines=["この本、古いインクの匂いがして好きなんです"], relations={"sora": "元気で少し羨ましい"})
        full = c.system_prompt(style=True, names={"sora": "ソラ"})
        self.assertIn("一人称は「わたし」", full)
        self.assertIn("ソラについて: 元気で少し羨ましい", full)
        self.assertNotIn("古いインク", c.system_prompt())          # 漏洩検査の対象（見本を使っても止めない）


class DesignerTest(unittest.TestCase):
    SHEET = ('{"first_person": "わたし", "endings": ["〜ですね", "〜かしら"], "catchphrases": ["ふふ"],'
             ' "sample_lines": ["この本、古いインクの匂いがするんです", "急がなくていいですよ"],'
             ' "values": ["一冊を丁寧に読むこと"], "dislikes": ["大きな音", "IPは10.0.0.1"],'
             ' "quirks": ["考える前に本の話に寄り道する"], "relations": {"ひかり": "ひかりさん。元気で少し羨ましい"},'
             ' "talkativeness": 0.3, "temperature": 0.8}')

    def test_deepen_saves_atena_fields_safely(self):
        import tempfile
        from pathlib import Path
        from atena.character import save_character
        from atena.designer import CharacterDesigner
        o, llm = make_office([self.SHEET])
        save_character(o.characters["shizuku"], o.cfg.characters_dir)
        d = CharacterDesigner(o)
        sheet = d.deepen("shizuku")
        c = o.characters["shizuku"]
        self.assertEqual(c.first_person, "わたし")
        self.assertEqual(c.dislikes, ["大きな音"])                         # 危ない項目は落とす
        self.assertEqual(c.relations, {"hikari": "ひかりさん。元気で少し羨ましい"})  # 名前→ID
        self.assertEqual((c.talkativeness, c.sampling), (0.3, {"temperature": 0.8}))
        self.assertIn("読書と紅茶", c.persona)                               # 人格は変えない
        self.assertIn("ひかり", llm.calls[0]["messages"][0]["content"])      # ほかのキャラとの違いを意識
        self.assertIsNone(d.deepen("shizuku"))                               # 既にあれば作らない
        saved = (Path(o.cfg.characters_dir) / "shizuku.toml").read_text(encoding="utf-8")
        self.assertIn('first_person = "わたし"', saved)
        d.reset("shizuku")
        self.assertEqual((c.first_person, c.sample_lines), ("", []))

    def test_app_sync_keeps_sheet(self):
        from atena.api import AtenaAPI
        o, _ = make_office(default="{}")
        api = AtenaAPI(o)
        api.dispatch("PUT", "/api/characters/mio", {}, {"name": "ミオ", "persona": "古書店員"})
        o.characters["mio"].first_person = "わたし"
        o.characters["mio"].sample_lines = ["ふふ、いい本ですね"]
        from atena.character import save_character
        save_character(o.characters["mio"], o.cfg.characters_dir)
        api.dispatch("PUT", "/api/characters/mio", {}, {"name": "ミオ", "persona": "古書店で働く"})
        self.assertEqual(o.characters["mio"].persona, "古書店で働く")
        self.assertEqual(o.characters["mio"].sample_lines, ["ふふ、いい本ですね"])  # アプリの同期で消えない


class ApplySheetTest(unittest.TestCase):
    def test_sora_sheet_applies(self):
        import io
        import shutil
        import tempfile
        from contextlib import redirect_stdout
        from pathlib import Path
        from atena.character import load_characters
        from atena.cli import main
        root = Path(tempfile.mkdtemp())
        (root / "config" / "characters").mkdir(parents=True)
        (root / "config" / "atena.toml").write_text("[guardian]\nuse_llm_judge = false\n", encoding="utf-8")
        (root / "config" / "characters" / "sora.toml").write_text('id = "sora"\nname = "Sora"\npersona = "旧"\n',
                                                                  encoding="utf-8")
        sheet = Path(__file__).resolve().parents[1] / "docs" / "characters" / "sora_sheet.toml"
        buf = io.StringIO()
        with redirect_stdout(buf):
            main(["--root", str(root), "character", "apply-sheet", "sora", str(sheet)])
        c = load_characters(root / "config" / "characters")["sora"]
        self.assertEqual((c.name, c.first_person, c.talkativeness), ("ソラ", "あたし", 0.75))
        self.assertIn("空と天気が大好き", c.persona)
        self.assertIn("mio", c.relations)
        self.assertEqual(set(c.voice_captions), {"neutral", "joy", "shy", "sad", "worry", "angry", "surprise"})
        self.assertIn("アプリのキャラ管理にも", buf.getvalue())
        self.assertIn("あたし", c.system_prompt(style=True))
