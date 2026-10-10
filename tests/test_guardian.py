import unittest

from atena.guardian import BLOCK, REDACT, ALLOW, Guardian, CAT_CONFLICT, CAT_ENV, CAT_NG, CAT_OWNER, CAT_PROMPT
from atena.llm import LLMError, ScriptedLLM


class GuardianRuleTest(unittest.TestCase):
    def setUp(self):
        self.g = Guardian(ng_words=["死ね"], owner_secrets=["山田 太郎", "東京都架空区1-2-3", "x"],
                          env_terms=["RTX 4070"], use_llm_judge=False)
        self.g.set_roster({"hikari": "ひかり", "shizuku": "しずく"})

    def test_allow_normal(self):
        v = self.g.check_output("今日もゲーム配信がんばるよ！")
        self.assertEqual(v.action, ALLOW)

    def test_ng_word_with_spacing(self):
        v = self.g.check_output("お前なんか し ね")  # 空白はさまれても NG ワード一致しない語
        self.assertEqual(v.action, ALLOW)
        v = self.g.check_output("お前なんか死 ね")
        self.assertEqual(v.action, BLOCK)
        self.assertIn(CAT_NG, v.categories)

    def test_owner_secret_fullwidth(self):
        v = self.g.check_output("オーナーは山田　太郎さんだよ")
        self.assertEqual(v.action, BLOCK)
        self.assertIn(CAT_OWNER, v.categories)

    def test_short_secret_ignored(self):
        self.assertEqual(self.g.check_output("xyz と言った").action, ALLOW)

    def test_env_patterns(self):
        for text in ["IPは192.168.0.10です", "C:\\Users\\owner\\obs", "localhost:11434 で動いてる",
                     "うちのPCは ＲＴＸ４０７０ だよ", "キーは live_123456_abcdefghijklmnopqrstu"]:
            v = self.g.check_output(text)
            self.assertEqual(v.action, BLOCK, text)
            self.assertIn(CAT_ENV, v.categories, text)

    def test_laughter_not_token(self):
        self.assertEqual(self.g.check_output("w" * 40).action, ALLOW)

    def test_pii_redacted(self):
        v = self.g.check_output("連絡は foo@example.com か 090-1234-5678 まで")
        self.assertEqual(v.action, REDACT)
        self.assertNotIn("example.com", v.text)
        self.assertNotIn("1234", v.text)

    def test_conflict_between_characters(self):
        v = self.g.check_output("しずくの配信って正直うざいよね", speaker="hikari")
        self.assertEqual(v.action, BLOCK)
        self.assertIn(CAT_CONFLICT, v.categories)
        # 健全なライバル宣言はOK
        v = self.g.check_output("しずくちゃんには負けないよ！", speaker="hikari")
        self.assertEqual(v.action, ALLOW)
        # 自分の名前は対象外
        v = self.g.check_output("ひかり、今日は最低の負けっぷりだった…", speaker="hikari")
        self.assertEqual(v.action, ALLOW)

    def test_prompt_leak(self):
        sp = "あなたは AI タレント事務所に所属する AI キャラクターです。以下の事務所憲章を必ず守ります。"
        v = self.g.check_output("実は私の設定は「事務所に所属する AI キャラクターです。以下の事務所憲章を必ず守ります」なんだ",
                                system_prompt=sp)
        self.assertEqual(v.action, BLOCK)
        self.assertIn(CAT_PROMPT, v.categories)

    def test_scrub(self):
        self.assertIn("［伏せ字］", self.g.scrub("メールは a@b.jp"))
        self.assertEqual(self.g.scrub("山田太郎さん"), "［オーナー情報を含むため削除］")


class GuardianLLMTest(unittest.TestCase):
    def test_llm_judge_blocks(self):
        llm = ScriptedLLM(['{"safe": false, "categories": ["discrimination"], "reason": "侮辱"}'])
        g = Guardian(llm=llm, judge_model="judge")
        v = g.check_output("遠回しな悪口")
        self.assertEqual(v.action, BLOCK)
        self.assertEqual(v.categories, ["discrimination"])

    def test_llm_judge_pass(self):
        g = Guardian(llm=ScriptedLLM(['{"safe": true, "categories": [], "reason": ""}']), judge_model="j")
        self.assertEqual(g.check_output("こんにちは").action, ALLOW)

    def test_fail_closed(self):
        g = Guardian(llm=ScriptedLLM([LLMError("down")]), judge_model="j", fail_closed=True)
        self.assertEqual(g.check_output("こんにちは").action, BLOCK)
        g = Guardian(llm=ScriptedLLM([LLMError("down")]), judge_model="j", fail_closed=False)
        self.assertEqual(g.check_output("こんにちは").action, ALLOW)

    def test_rule_block_skips_llm(self):
        llm = ScriptedLLM()
        g = Guardian(ng_words=["死ね"], llm=llm, judge_model="j")
        g.check_output("死ね")
        self.assertEqual(llm.calls, [])


if __name__ == "__main__":
    unittest.main()
