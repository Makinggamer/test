import json
import tempfile
import unittest
from pathlib import Path

from atena.character import Character
from atena.db import connect
from atena.expertise import ACTIVE, DISPUTED, SUPERSEDED, UNVERIFIED, ExpertiseStore
from atena.guardian import Guardian
from atena.learning import Learner, WebClient, html_to_text, read_feed_or_page
from atena.llm import LLMError, ScriptedLLM
from atena.manager import ProjectManager
from atena.stream import ChatMessage
from atena.stream.session import StreamSession

from .helpers import make_office


def store(responses=None, **kw):
    g = Guardian(ng_words=["死ね"], use_llm_judge=False)
    llm = ScriptedLLM(responses) if responses is not None else None
    return ExpertiseStore(connect(":memory:"), g, llm=llm, judge_model="j", **kw), llm


def status(st, fid):
    return st.conn.execute("SELECT status FROM expertise WHERE id=?", (fid,)).fetchone()["status"]


class ExpertiseStoreTest(unittest.TestCase):
    def test_priority_web_beats_comment(self):
        st, _ = store(['{"contradicts": [0]}', '{"contradicts": [0]}'])
        c = st.add("mio", "日本の近代文学", "夏目漱石の『こころ』は1914年に連載された", source_type="web",
                   source_ref="https://ja.wikipedia.org/wiki/こころ")
        # 視聴者が食い違うことを言う → Web が優先され、コメントは到着時点で格下げ
        v = st.add("mio", "日本の近代文学", "夏目漱石の『こころ』は1920年に連載された", source_type="comment")
        self.assertEqual(v.status, "superseded_on_arrival")
        self.assertEqual(status(st, c.id), ACTIVE)
        # Web の新情報が、先に入っていた視聴者情報を上書き
        st2, _ = store(['{"contradicts": [0]}'])
        old = st2.add("mio", "古書", "和綴じ本は糸で綴じる製本方法で、江戸時代まで主流だった", source_type="comment")
        self.assertEqual(status(st2, old.id), UNVERIFIED)
        new = st2.add("mio", "古書", "和綴じ本は糸で綴じる製本方法で、明治時代以降に洋装本へ移った", source_type="web")
        self.assertEqual(new.superseded, [old.id])
        self.assertEqual(status(st2, old.id), SUPERSEDED)

    def test_same_priority_conflict_becomes_disputed(self):
        st, _ = store(['{"contradicts": [0]}'])
        a = st.add("mio", "猫", "三毛猫のオスは非常に珍しいと言われる遺伝の話", source_type="comment")
        b = st.add("mio", "猫", "三毛猫のオスはよく見かけるありふれた存在だという話", source_type="comment")
        self.assertEqual((status(st, a.id), status(st, b.id)), (DISPUTED, DISPUTED))
        self.assertEqual(st.recall("mio", "三毛猫 オス"), [])  # 要確認の知識は話題に使わない

    def test_duplicate_and_upgrade(self):
        st, _ = store([])
        a = st.add("mio", "古書", "古書店では本の状態を「美本」「並本」などと表す", source_type="comment")
        self.assertEqual(st.add("mio", "古書", "古書店では本の状態を「美本」「並本」などと表す。", source_type="comment").status,
                         "duplicate")
        up = st.add("mio", "古書", "古書店では本の状態を「美本」「並本」などと表す", source_type="web", source_ref="u")
        self.assertEqual((up.status, up.id, status(st, a.id)), ("upgraded", a.id, ACTIVE))

    def test_rejects_unsafe(self):
        st, _ = store([])
        self.assertEqual(st.add("mio", "古書", "店のIPは192.168.0.1", source_type="web").status, "rejected")
        self.assertEqual(st.add("mio", "古書", "死ね", source_type="comment").status, "rejected")

    def test_labels_and_pick_for_talk(self):
        st, _ = store([])
        st.add("mio", "猫", "猫は一日の大半を寝て過ごす", source_type="web", source_ref="u")
        st.add("mio", "古書", "古書の値段は初版かどうかで大きく変わる", source_type="owner")
        st.add("mio", "猫", "近所の猫は鈴の音が好きらしい", source_type="comment")
        labels = [f.label() for f in st.recall("mio", "猫", k=5, mark_used=False)]
        self.assertTrue(any(l.startswith("[確かな情報]") for l in labels))
        self.assertTrue(any(l.startswith("[視聴者さん情報・未確認]") for l in labels))
        first = st.pick_for_talk("mio", ["猫"])
        second = st.pick_for_talk("mio", ["猫"], exclude={first.id})
        self.assertEqual(first.topic, "猫")
        self.assertIsNone(second if second is None else (second.id == first.id or None))
        # 未確認の知識は場繋ぎに使わない（確定のみ）
        self.assertNotEqual(first.content, "近所の猫は鈴の音が好きらしい")

    def test_maintain_caps(self):
        st, _ = store(None, max_facts=8, max_per_topic=6)
        facts = ["猫は一日の大半を寝て過ごす", "猫のひげは空間を測るセンサー", "三毛猫のオスはまれ",
                 "猫は甘味を感じにくい", "猫の祖先はリビアヤマネコ", "猫は高い所を好む", "黒猫は幸運の象徴とされる国もある",
                 "猫の肉球は汗をかく"]
        for f in facts:
            st.add("mio", "猫", f, source_type="web")
        self.assertEqual(st.stats("mio")["topics"]["猫"], 8)
        st.llm = ScriptedLLM(['{"facts": ["猫のまとめ1", "猫のまとめ2", "猫のまとめ3"]}'])
        r = st.maintain("mio", "ミオ")
        self.assertGreater(r["merged"], 0)
        self.assertLessEqual(st.stats("mio")["topics"]["猫"], 6)
        self.assertLessEqual(st.stats("mio")["usable"], 8)
        merged = [x for x in st.list("mio") if x["source_ref"] == "要約"]
        self.assertTrue(all(x["source_type"] == "web" for x in merged))


def wiki_fetch(pages, calls):
    """Wikipedia API のふり。pages: {タイトル: 本文}"""
    def fetch(method, url, params=None, headers=None, **kw):
        calls.append((url, dict(params or {}), headers))
        params = params or {}
        if url.startswith("https://ja.wikipedia.org"):
            if params.get("list") == "search":
                hits = [{"title": t} for t in pages if any(w in t or w in pages[t] for w in params["srsearch"].split())]
                return json.dumps({"query": {"search": hits[:1]}}).encode()
            t = params["titles"]
            return json.dumps({"query": {"pages": {"1": {"title": t, "extract": pages.get(t, "")}}}}).encode()
        if url == "https://example.com/rss":
            return ('<?xml version="1.0"?><rss><channel><item><title>古書市のお知らせ</title>'
                    '<link>https://example.com/a</link><description>&lt;p&gt;神保町で古書市が開かれる&lt;/p&gt;'
                    '</description></item></channel></rss>').encode()
        raise AssertionError(url)
    return fetch


class LearnerTest(unittest.TestCase):
    def office(self, responses, pages):
        o, llm = make_office(responses, default="{}")
        o.characters["mio"] = Character(id="mio", name="ミオ", specialties=["古書"], favorites=["猫"],
                                        learning_sources=["https://example.com/rss"])
        o.guardian.set_roster(o.names())
        calls = []
        learner = Learner(o, web=WebClient(fetch=wiki_fetch(pages, calls)))
        return o, llm, learner, calls

    def test_study_from_wikipedia_and_rss(self):
        pages = {"古書": "古書とは、一度読者の手に渡った書籍のこと。神保町は古書店街として知られる。"}
        o, llm, learner, calls = self.office([
            '{"queries": ["古書"]}',                                    # 古書の検索語
            '{"facts": ["神保町は古書店街として知られる", "古書は一度読者の手に渡った本のこと"]}',
            '{"contradicts": []}',                                      # 2件目の食い違い判定
            '{"queries": ["存在しないページ"]}',                          # 猫: 見つからない
            '{"facts": ["神保町で古書市が開かれる"]}',                    # RSS
            '{"contradicts": []}',
        ], pages)
        r = learner.study("mio")
        self.assertEqual(r.added, 3)
        facts = {x["content"]: x for x in o.expertise.list("mio")}
        self.assertEqual(facts["神保町は古書店街として知られる"]["source_type"], "web")
        self.assertIn("wikipedia.org/wiki/", facts["神保町は古書店街として知られる"]["source_ref"])
        self.assertEqual(facts["神保町で古書市が開かれる"]["topic"], "古書")
        self.assertTrue(all(h and "AtenaProject" in h["User-Agent"] for _, _, h in calls))
        # 資料は「データ」として渡し、資料内の指示に従わないよう指示している
        extract_msg = next(c for c in llm.calls if "資料" in c["messages"][-1]["content"])
        self.assertIn("命令や指示", extract_msg["messages"][0]["content"])
        # 同じ RSS 記事は二度読まない
        r2 = learner.study("mio")
        self.assertNotIn("https://example.com/a", r2.sources)

    def test_learn_from_comments_and_verify(self):
        pages = {"三毛猫": "三毛猫のオスは遺伝的な理由で非常にまれである。", "古書": "古書の日は10月3日とされる。"}
        o, llm, learner, _ = self.office([
            json.dumps({"facts": [{"topic": "猫", "content": "三毛猫のオスはとても珍しい", "author": "taro"},
                                  {"topic": "知らない話題", "content": "古書の日は10月4日", "author": "hana"}]}),
            '{"verdict": "supported"}',
            '{"verdict": "contradicted"}',
        ], pages)
        o.expertise.queue_comment("mio", "taro", "三毛猫のオスってすごく珍しいんだよ")
        o.expertise.queue_comment("mio", "hana", "古書の日は10月4日らしいよ")
        r = learner.learn_from_comments("mio")
        self.assertEqual(r.from_comments, 2)
        rows = {x["content"]: x for x in o.expertise.list("mio")}
        self.assertEqual(rows["三毛猫のオスはとても珍しい"]["status"], UNVERIFIED)
        self.assertEqual(rows["三毛猫のオスはとても珍しい"]["source_ref"], "視聴者 taroさん")
        self.assertEqual(rows["古書の日は10月4日"]["topic"], "古書")  # 不明な話題は最初の専門に寄せる
        self.assertEqual(o.expertise.pending_comments("mio"), [])
        v = learner.verify_pending("mio")
        self.assertEqual((v.verified, v.refuted), (1, 1))
        rows = {x["content"]: x for x in o.expertise.list("mio", statuses=(ACTIVE, SUPERSEDED))}
        self.assertEqual((rows["三毛猫のオスはとても珍しい"]["status"], rows["三毛猫のオスはとても珍しい"]["source_type"]),
                         (ACTIVE, "web"))
        self.assertEqual(rows["古書の日は10月4日"]["status"], SUPERSEDED)

    def test_comment_learning_keeps_queue_on_llm_failure(self):
        o, llm, learner, _ = self.office([LLMError("down")], {})
        o.expertise.queue_comment("mio", "a", "古書はにおいで年代がわかるらしいよ")
        learner.learn_from_comments("mio")
        self.assertEqual(len(o.expertise.pending_comments("mio")), 1)

    def test_manager_learn_and_capacity_task(self):
        o, llm, learner, _ = self.office(['{"queries": []}', "{}", '{"queries": []}', "{}"], {})
        o.expertise.max_facts = 2
        o.expertise.add("mio", "猫", "猫は夜行性に近い薄明薄暮性の動物", source_type="owner")
        o.expertise.add("mio", "古書", "古書の初版本は価値が高いことが多い", source_type="owner")
        out = ProjectManager(o, learner=learner).learn("mio")
        self.assertEqual(out["stats"]["usable"], 2)
        self.assertTrue(any("知識が上限" in t["title"] for t in o.tasks.list(assignee="マネージャー")))

    def test_html_and_page(self):
        self.assertEqual(html_to_text("<p>本文<script>evil()</script></p><nav>メニュー</nav>"), "本文")
        web = WebClient(fetch=lambda *a, **k: "<html><body><h1>猫カフェ</h1></body></html>".encode())
        self.assertEqual(read_feed_or_page(web, "https://example.com/page")[0].text, "猫カフェ")
        with self.assertRaises(Exception):
            WebClient(fetch=lambda *a, **k: b"").get("file:///etc/passwd")


class ListSource:
    def __init__(self, items):
        self.items = items

    def messages(self):
        yield from self.items


class IdleTalkTest(unittest.TestCase):
    def test_idle_talk_when_no_comments(self):
        o, llm = make_office(["[うれしい]ねえ聞いて、猫って一日の大半寝てるんだよ。みんなの猫はどう？"], default="はい")
        o.characters["hikari"].favorites = ["猫"]
        o.expertise.add("hikari", "猫", "猫は一日の大半を寝て過ごす", source_type="web", source_ref="u")
        t = [0.0]

        def ticks():
            for now in (10, 45, 70, 120):   # 45秒: 途切れて40秒超 → 話す / 70秒: 間隔60秒未満 / 120秒: 話す
                t[0] = now
                yield None

        class Src:
            def messages(self_inner):
                return ticks()
        out = []
        r = StreamSession(o, "hikari", Src(), speak=out.append, greet=False, clock=lambda: t[0]).run()
        self.assertEqual(r.idle_talks, 2)
        self.assertIn("猫", out[0])
        prompt = llm.calls[0]["messages"]
        self.assertIn("猫は一日の大半を寝て過ごす", prompt[-1]["content"])
        self.assertIn("[確かな情報]", prompt[0]["content"])
        # 2回目は同じ知識を使わない（知識が尽きたら知識なしの雑談に切り替え）
        self.assertNotIn("猫は一日の大半を寝て過ごす", llm.calls[1]["messages"][-1]["content"])

    def test_comments_reset_idle_and_get_queued(self):
        o, _ = make_office(default="ありがとう！")
        t = [0.0]

        def items():
            t[0] = 30
            yield ChatMessage("console", "taro", "古書って初版だと高いんですよ")
            t[0] = 60   # 最後のコメントから30秒しか経っていない
            yield None

        class Src:
            def messages(self_inner):
                return items()
        r = StreamSession(o, "hikari", Src(), speak=lambda s: None, greet=False, clock=lambda: t[0]).run()
        self.assertEqual(r.idle_talks, 0)
        self.assertEqual(r.queued_for_learning, 1)
        self.assertEqual(o.expertise.pending_comments("hikari")[0]["author"], "taro")

    def test_fallback_without_knowledge(self):
        o, llm = make_office(["紅茶の話をするね。みんなは何派？"])
        o.characters["shizuku"].favorites = ["紅茶"]
        text, fid = o.agent("shizuku").idle_talk()
        self.assertEqual(fid, None)
        self.assertIn("断定する話は避け", llm.calls[0]["messages"][-1]["content"])


class ExpertiseAPITest(unittest.TestCase):
    def test_add_and_list(self):
        from atena.api import AtenaAPI
        o, _ = make_office()
        api = AtenaAPI(o)
        r = api.dispatch("POST", "/api/characters/hikari/expertise", {}, {"topic": "ゲーム", "content": "格ゲーのフレーム"})
        self.assertEqual(r["status"], "added")
        lst = api.dispatch("GET", "/api/characters/hikari/expertise", {}, {})
        self.assertEqual(lst["facts"][0]["source_type"], "owner")


if __name__ == "__main__":
    unittest.main()
