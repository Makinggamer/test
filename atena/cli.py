"""Atena project コマンドライン。`python -m atena --help`"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
import time
from datetime import date, datetime
from pathlib import Path

from . import character as charmod
from .config import load_config
from .llm import OllamaClient
from .lounge import RoomMaster, export_highlights_markdown
from .manager import ProjectManager
from .monitor import snapshot_dict
from .office import Office
from .revenue import SOURCES
from .stream import ConsoleChat
from .stream.session import StreamSession
from .stream.voice import IrodoriTTS, OverlayWriter, bench, build_tts

EXAMPLES_DIR = Path(__file__).resolve().parent.parent / "config"


_OPEN: list[Office] = []


def _office(args) -> Office:
    o = Office(load_config(args.root))
    _OPEN.append(o)
    return o


def cmd_init(args):
    root = Path(args.root).resolve()
    cfg_dir = root / "config"
    (cfg_dir / "characters").mkdir(parents=True, exist_ok=True)
    (root / "data").mkdir(exist_ok=True)
    for name in ("atena.toml", "ng_words.txt"):
        src, dst = EXAMPLES_DIR / name, cfg_dir / name
        if src.exists() and not dst.exists() and src != dst:
            shutil.copy(src, dst)
    secrets = cfg_dir / "owner_secrets.toml"
    if not secrets.exists():
        shutil.copy(EXAMPLES_DIR / "owner_secrets.example.toml", secrets)
    office = _office(args)
    print(f"初期化しました: {root}")
    print(f"- オーナー個人情報（ガーディアンの検出用）を {secrets} に記入してください（git 管理外）")
    print(f"- 登録キャラ: {', '.join(office.names().values()) or 'なし'}")


# ---- characters ----
def cmd_char_list(args):
    o = _office(args)
    for c in o.characters.values():
        print(f"{c.id}\t{c.name}\tmodel={c.model or '(既定)'}\tL{c.autonomy_level}")


def _save(args, c: charmod.Character):
    cfg = load_config(args.root)
    path = charmod.save_character(c, cfg.characters_dir)
    print(f"保存しました: {path}")


def cmd_char_import_ollama(args):
    cfg = load_config(args.root)
    client = OllamaClient(cfg.ollama.host, cfg.ollama.timeout_sec)
    _save(args, charmod.from_ollama_model(client, args.model, args.id, args.name))


def cmd_char_import_modelfile(args):
    text = Path(args.path).read_text(encoding="utf-8")
    _save(args, charmod.from_modelfile(args.id, args.name, text, model=args.model or ""))


def cmd_char_import_webui(args):
    data = json.loads(Path(args.path).read_text(encoding="utf-8"))
    for c in charmod.from_webui_export(data):
        _save(args, c)


# ---- stream / chat ----
def _stream_extras(o: Office, args) -> dict:
    from .avatar import build_avatar
    v = o.cfg.voice
    return {
        "avatar": build_avatar(o.cfg) if args.avatar else None,
        "tts": build_tts(v) if args.tts else None,
        "overlay": OverlayWriter(o.cfg.path(v.subtitle_file), o.cfg.path(v.comment_file),
                                 o.cfg.path(v.notice_file)),
        "use_llm_judge": not args.no_judge,
        "schedule_id": args.schedule,
    }


def _print_result(r):
    print(f"終了: 返答 {r.replies} 件 / スパチャ {r.superchats} 件 約{r.superchat_jpy:,}円 / "
          f"新規メンバー {r.memberships} 人" + (" / PC 負荷のため早期終了" if r.ended_early else "")
          + (f" / ボイストラブル {r.voice_trouble} 回" if r.voice_trouble else "")
          + (f" / 中止: {r.aborted}" if r.aborted else ""))
    if r.srt_path:
        print(f"字幕: {r.srt_path}")


def cmd_stream(args):
    o = _office(args)
    print(f"{o.character(args.character).name} の模擬配信を開始します。'名前: コメント' を入力、空行で終了。")
    _print_result(StreamSession(o, args.character, ConsoleChat(), **_stream_extras(o, args)).run())


# ---- youtube ----
def _youtube_client(o: Office):
    from .stream.google_oauth import TokenProvider
    from .stream.youtube import QuotaTracker, YouTubeClient
    y = o.cfg.youtube
    quota = QuotaTracker(o.conn, y.daily_quota, y.quota_reserve)
    tp = None
    if y.client_secret_file and o.cfg.path(y.token_file).exists():
        tp = TokenProvider(o.cfg.path(y.client_secret_file), o.cfg.path(y.token_file))
    return YouTubeClient(api_key=y.api_key, token_provider=tp, quota=quota)


def cmd_youtube_auth(args):
    from .stream.google_oauth import authorize
    cfg = load_config(args.root)
    if not cfg.youtube.client_secret_file:
        raise ValueError("config/atena.toml の [youtube] client_secret_file を設定してください")
    authorize(cfg.path(cfg.youtube.client_secret_file), cfg.path(cfg.youtube.token_file))
    print("YouTube の認証が完了しました")


def cmd_youtube_live(args):
    from .stream.youtube import YouTubeLiveChat
    o = _office(args)
    client = _youtube_client(o)
    chat_id = client.live_chat_id_for_video(args.video) if args.video else client.my_active_live_chat_id()
    y = o.cfg.youtube
    print(f"本日の YouTube API 残り: {client.quota.available()} ユニット（予備 {y.quota_reserve} を除く）")
    source = YouTubeLiveChat(client, chat_id, y.fx_rates, poll_cost=y.poll_cost,
                             stream_hours=args.hours or y.expected_stream_hours)
    print(f"{o.character(args.character).name} が YouTube に接続しました。Ctrl+C で終了。")
    _print_result(StreamSession(o, args.character, source, **_stream_extras(o, args)).run())


def cmd_youtube_quota(args):
    o = _office(args)
    q = _youtube_client(o).quota
    print(f"本日の使用量: {q.used()} / {q.daily}（配信に使える残り {q.available()}）")


# ---- voice ----
def cmd_voice_list(args):
    cfg = load_config(args.root)
    for v in IrodoriTTS(cfg.voice.irodori_host).voices():
        print(v.get("id", v) if isinstance(v, dict) else v)


def cmd_voice_test(args):
    from .stream.voice import AudioPlayer
    o = _office(args)
    c = o.character(args.character)
    AudioPlayer().play(build_tts(o.cfg.voice).synthesize(args.text, c))


def cmd_voice_bench(args):
    """生配信で使える速さかを測る（合成時間 ÷ 音声の長さ）。"""
    o = _office(args)
    c = o.character(args.character)
    tts = build_tts(o.cfg.voice)
    text = args.text or "こんばんは！今日も来てくれてありがとう。コメントどんどん読んでいくね。"
    bench(tts, c, "テスト")  # 初回はモデル読み込みを含むので捨てる
    results = [bench(tts, c, text) for _ in range(args.runs)]
    avg = sum(r.seconds for r in results) / len(results)
    rtf = sum(r.rtf for r in results) / len(results)
    print(f"{tts.name}: 平均 {avg:.1f} 秒（音声 {results[0].audio_seconds:.1f} 秒） RTF {rtf:.2f}")
    if avg <= 3:
        print("→ 生配信の返答に使えます")
    elif avg <= 8:
        print("→ 使えますが返答に間が空きます。返答を短めにするか num_steps を下げてください")
    else:
        print("→ 生配信では返答の多くが字幕のみになります。num_steps を下げるか、返答を短くしてください。"
              "Irodori は ASMR・切り抜きなど事前制作にも使えます")


# ---- avatar ----
def cmd_avatar_vts_auth(args):
    from .avatar.vts import VTubeStudioAvatar
    cfg = load_config(args.root)
    a = cfg.avatar
    vts = VTubeStudioAvatar(a.vts_url, token_file=cfg.path(a.vts_token_file), mouth_param=a.vts_mouth_param)
    print("VTube Studio に接続します。VTube Studio 側に許可ダイアログが出たら「許可」を押してください")
    vts.connect()
    print("認証できました。現在のモデルのホットキー（キャラ定義の vts_hotkeys に感情ごとに指定）:")
    for h in vts.hotkeys():
        print(f"  {h.get('name')}  ({h.get('type')})")
    vts.close()


def cmd_avatar_test(args):
    """表情を順に切り替え、口をパクパクさせて表示を確認する。"""
    import time as _t
    from .avatar import EMOTIONS, build_avatar
    o = _office(args)
    c = o.character(args.character)
    av = build_avatar(o.cfg)
    if not av:
        raise ValueError("config/atena.toml の [avatar] engines を設定してください")
    try:
        for emo in EMOTIONS:
            print(f"表情: {emo}")
            av.set_emotion(c, emo)
            for i in range(10):
                av.mouth(1.0 if i % 2 == 0 else 0.0)
                _t.sleep(0.15)
        av.set_emotion(c, "neutral")
        av.mouth(0.0)
        if args.hold:
            print("Ctrl+C で終了")
            while True:
                _t.sleep(1)
    except KeyboardInterrupt:
        pass
    finally:
        av.close()


# ---- api ----
def cmd_serve(args):
    from .api import make_server
    o = _office(args)
    srv = make_server(o, o.cfg.api.host, args.port or o.cfg.api.port)
    print(f"Atena API: http://{o.cfg.api.host}:{srv.server_port}  （Ctrl+C で停止）")
    if not o.cfg.api.token:
        print(f"トークン: {o.cfg.root / 'data' / 'api_token'}")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        srv.server_close()


# ---- lounge ----
def cmd_lounge(args):
    o = _office(args)
    ids = args.characters or list(o.characters)
    res = RoomMaster(o).run(ids, topic=args.topic, turns=args.turns)
    print(f"# ラウンジ {res.session_id} 話題: {res.topic}\n")
    for who, text in res.transcript:
        print(f"{who}: {text}")
    print(f"\n規制: {len(res.warnings)} 件 / ナレッジ追加: {len(res.knowledge_ids)} 件 / "
          f"切り抜き候補: {len(res.highlight_ids)} 件")


def cmd_highlights(args):
    o = _office(args)
    md = export_highlights_markdown(o.conn, args.session)
    if args.out:
        Path(args.out).write_text(md, encoding="utf-8")
        print(f"書き出しました: {args.out}")
    else:
        print(md)


def cmd_knowledge(args):
    o = _office(args)
    rows = o.knowledge.list(args.limit)
    for r in rows:
        print(f"#{r['id']} [{r['topic']}] {r['content']}  ({r['source']})")


# ---- schedule ----
def cmd_sched_propose(args):
    o = _office(args)
    sid, problems = o.scheduler.propose(args.character, args.title, args.start, args.end,
                                        est_vram_mb=args.vram)
    print(f"提案 #{sid}")
    for p in problems:
        print(f"  ⚠ {p}")


def cmd_sched_list(args):
    o = _office(args)
    for r in o.scheduler.list(start_from=args.since, status=args.status):
        print(f"#{r['id']}\t{r['status']}\t{r['start']}〜{r['end'][-5:]}\t{r['character_id']}\t{r['title']}")


def cmd_sched_set(args):
    o = _office(args)
    problems = o.scheduler.set_status(args.id, args.status, "owner")
    if problems:
        print("変更できません:")
        for p in problems:
            print(f"  ⚠ {p}")
    else:
        print(f"#{args.id} → {args.status}")


def cmd_sched_suggest(args):
    o = _office(args)
    day = date.fromisoformat(args.day)
    for s, e in o.scheduler.suggest(day, args.minutes, limit=args.limit):
        print(f"{s}〜{e[-5:]}")


def cmd_preflight(args):
    o = _office(args)
    pf = o.scheduler.preflight(args.id)
    print(("GO: " if pf.go else "STOP: ") + pf.message)


# ---- monitor ----
def cmd_monitor(args):
    o = _office(args)
    while True:
        h = o.monitor.check()
        snap = {k: (round(v, 1) if isinstance(v, float) else v) for k, v in snapshot_dict(h.snapshot).items()}
        print(f"[{datetime.now():%H:%M:%S}] {h.status} {json.dumps(snap, ensure_ascii=False)}")
        for r in h.reasons:
            print(f"  - {r}")
        if not args.watch:
            break
        time.sleep(args.interval)


# ---- revenue ----
def cmd_rev_add(args):
    o = _office(args)
    rid = o.ledger.add(args.character, args.source, args.amount, occurred_at=args.at, memo=args.memo)
    print(f"記録 #{rid}")


def cmd_rev_rank(args):
    o = _office(args)
    today = datetime.now().date()
    entries = o.ledger.monthly_ranking(args.year or today.year, args.month or today.month, list(o.characters))
    names = o.names()
    for e in entries:
        g = f" ({e.growth_pct:+.0f}%)" if e.growth_pct is not None else ""
        bd = ", ".join(f"{k}:{v:,}" for k, v in e.breakdown.items())
        print(f"{e.rank}位 {names.get(e.character_id, e.character_id)}  {e.total:,}円{g}  [{bd}]")


def cmd_rev_export(args):
    o = _office(args)
    Path(args.out).write_text(o.ledger.export_csv(args.since, args.until), encoding="utf-8")
    print(f"書き出しました: {args.out}")


# ---- memory ----
def cmd_mem_stats(args):
    o = _office(args)
    for cid in ([args.character] if args.character else o.characters):
        s = o.memory.stats(cid)
        print(f"{cid}: 記憶 {s['items']}件 ({s['items_usage']:.0%}) / {s['chars']:,}字 ({s['chars_usage']:.0%})"
              f" / 視聴者 {s['viewers']}人 / {s['by_kind']}")


def cmd_mem_maintain(args):
    o = _office(args)
    for cid, c in o.characters.items():
        print(cid, o.memory.maintain(cid, c.name))


def cmd_mem_add(args):
    o = _office(args)
    o.memory.remember(args.character, args.content, kind=args.kind, importance=args.importance)
    print("記憶しました")


# ---- tasks / approvals ----
def cmd_task_add(args):
    o = _office(args)
    print(f"タスク #{o.tasks.add(args.title, args.assignee, created_by=args.by, description=args.desc)}")


def cmd_task_list(args):
    o = _office(args)
    for t in o.tasks.list(assignee=args.assignee, status=None if args.all else "open"):
        print(f"#{t['id']}\t{t['status']}\t{t['assignee']}\t{t['title']}")


def cmd_task_done(args):
    _office(args).tasks.set_status(args.id, "done", "owner")
    print("完了にしました")


def cmd_appr_list(args):
    o = _office(args)
    for a in o.approvals.pending():
        print(f"#{a['id']}\tL{a['level']}\t{a['kind']}\t{a['summary']}")


def cmd_appr_decide(args, approve: bool):
    o = _office(args)
    problems = ProjectManager(o).decide(args.id, approve)
    print(f"#{args.id} を{'承認' if approve else '却下'}しました")
    for p in problems:
        print(f"  ⚠ スケジュールは確定できませんでした: {p}")


# ---- manager ----
def cmd_daily(args):
    o = _office(args)
    rep = ProjectManager(o).daily_cycle()
    names = o.names()
    print(f"# 日次サイクル {rep.day}  PC: {rep.health} {' / '.join(rep.health_reasons)}")
    for x in rep.outcomes:
        title = x.plan["title"] if x.plan else "-"
        print(f"- {names.get(x.character_id, x.character_id)}: [{x.status}] {title} {x.detail}")
    for a in rep.alerts:
        print(f"⚠ {a}")


def cmd_report(args):
    print(ProjectManager(_office(args)).status_report())


def cmd_guardian_check(args):
    o = _office(args)
    v = o.guardian.check_output(args.text, speaker=args.speaker, use_llm=not args.no_judge, context="cli",
                               record=False)
    print(json.dumps({"action": v.action, "text": v.text, "categories": v.categories, "reasons": v.reasons},
                     ensure_ascii=False, indent=2))


def cmd_audit_verify(args):
    ok, bad = _office(args).audit.verify()
    print("監査ログ: 正常" if ok else f"監査ログ: 改ざんの疑い (行 #{bad})")
    return 0 if ok else 1


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="atena", description="Atena project — AI タレント事務所")
    p.add_argument("--root", default=".", help="プロジェクトのルートディレクトリ")
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("init", help="設定ファイルと DB を初期化").set_defaults(func=cmd_init)

    ch = sub.add_parser("character", help="キャラクター管理").add_subparsers(dest="sub", required=True)
    ch.add_parser("list").set_defaults(func=cmd_char_list)
    x = ch.add_parser("import-ollama", help="ollama create 済みのモデルから取り込む")
    x.add_argument("model"); x.add_argument("--id", required=True); x.add_argument("--name", required=True)
    x.set_defaults(func=cmd_char_import_ollama)
    x = ch.add_parser("import-modelfile", help="Modelfile から取り込む")
    x.add_argument("path"); x.add_argument("--id", required=True); x.add_argument("--name", required=True)
    x.add_argument("--model", help="使用モデル（省略時は FROM の値）")
    x.set_defaults(func=cmd_char_import_modelfile)
    x = ch.add_parser("import-webui", help="Open WebUI 等のモデルエクスポート JSON から取り込む")
    x.add_argument("path"); x.set_defaults(func=cmd_char_import_webui)

    def stream_opts(x):
        x.add_argument("character")
        x.add_argument("--no-judge", action="store_true", help="LLM 二次判定を省略（ルール検査のみ）")
        x.add_argument("--tts", action="store_true", help="Irodori で読み上げる")
        x.add_argument("--avatar", action="store_true", help="アバターを動かす（[avatar] engines）")
        x.add_argument("--schedule", type=int, help="配信枠 ID（開始前にプリフライト、終了時に done）")

    x = sub.add_parser("stream", help="コンソールで模擬配信")
    stream_opts(x); x.set_defaults(func=cmd_stream)

    yt = sub.add_parser("youtube", help="YouTube Live 連携").add_subparsers(dest="sub", required=True)
    yt.add_parser("auth", help="OAuth 認証（初回のみ）").set_defaults(func=cmd_youtube_auth)
    x = yt.add_parser("live", help="配信中のライブチャットに接続して応答")
    stream_opts(x)
    x.add_argument("--video", help="動画 ID（省略時は OAuth で自分の配信を自動検出）")
    x.add_argument("--hours", type=float, help="予定配信時間（API 割り当ての配分に使用）")
    x.set_defaults(func=cmd_youtube_live)
    yt.add_parser("quota", help="本日の API 使用量").set_defaults(func=cmd_youtube_quota)

    vc = sub.add_parser("voice", help="読み上げ (Irodori-TTS)").add_subparsers(dest="sub", required=True)
    vc.add_parser("list", help="Irodori の voice_id 一覧").set_defaults(func=cmd_voice_list)
    x = vc.add_parser("test"); x.add_argument("character"); x.add_argument("text")
    x.set_defaults(func=cmd_voice_test)
    x = vc.add_parser("bench", help="生配信に使える速さか計測")
    x.add_argument("character"); x.add_argument("--text"); x.add_argument("--runs", type=int, default=3)
    x.set_defaults(func=cmd_voice_bench)

    av = sub.add_parser("avatar", help="アバター (VTube Studio / PNGTuber)").add_subparsers(dest="sub", required=True)
    av.add_parser("vts-auth", help="VTube Studio に接続・認証しホットキー一覧を表示").set_defaults(
        func=cmd_avatar_vts_auth)
    x = av.add_parser("test", help="表情と口パクの確認"); x.add_argument("character")
    x.add_argument("--hold", action="store_true", help="終了せず表示を残す（OBS の配置調整用）")
    x.set_defaults(func=cmd_avatar_test)

    x = sub.add_parser("serve", help="デスクトップアプリ連携用のローカル API を起動")
    x.add_argument("--port", type=int); x.set_defaults(func=cmd_serve)

    x = sub.add_parser("lounge", help="ラウンジのセッションを実行")
    x.add_argument("characters", nargs="*"); x.add_argument("--topic"); x.add_argument("--turns", type=int)
    x.set_defaults(func=cmd_lounge)
    x = sub.add_parser("highlights", help="切り抜き候補を Markdown 出力")
    x.add_argument("--session"); x.add_argument("--out"); x.set_defaults(func=cmd_highlights)
    x = sub.add_parser("knowledge", help="ナレッジ一覧")
    x.add_argument("--limit", type=int, default=50); x.set_defaults(func=cmd_knowledge)

    sc = sub.add_parser("schedule", help="配信スケジュール").add_subparsers(dest="sub", required=True)
    x = sc.add_parser("propose")
    x.add_argument("character"); x.add_argument("title"); x.add_argument("start"); x.add_argument("end")
    x.add_argument("--vram", type=int, help="VRAM 見積もり(MB)"); x.set_defaults(func=cmd_sched_propose)
    x = sc.add_parser("list"); x.add_argument("--since"); x.add_argument("--status")
    x.set_defaults(func=cmd_sched_list)
    x = sc.add_parser("set"); x.add_argument("id", type=int)
    x.add_argument("status", choices=["approved", "rejected", "done", "cancelled"]); x.set_defaults(func=cmd_sched_set)
    x = sc.add_parser("suggest"); x.add_argument("day"); x.add_argument("--minutes", type=int, default=60)
    x.add_argument("--limit", type=int, default=3); x.set_defaults(func=cmd_sched_suggest)
    x = sc.add_parser("preflight"); x.add_argument("id", type=int); x.set_defaults(func=cmd_preflight)

    x = sub.add_parser("monitor", help="PC 状態を表示")
    x.add_argument("--watch", action="store_true"); x.add_argument("--interval", type=float, default=10)
    x.set_defaults(func=cmd_monitor)

    rv = sub.add_parser("revenue", help="収益").add_subparsers(dest="sub", required=True)
    x = rv.add_parser("add"); x.add_argument("character"); x.add_argument("source", choices=SOURCES)
    x.add_argument("amount", type=int); x.add_argument("--at"); x.add_argument("--memo", default="")
    x.set_defaults(func=cmd_rev_add)
    x = rv.add_parser("rank"); x.add_argument("--year", type=int); x.add_argument("--month", type=int)
    x.set_defaults(func=cmd_rev_rank)
    x = rv.add_parser("export"); x.add_argument("out"); x.add_argument("--since"); x.add_argument("--until")
    x.set_defaults(func=cmd_rev_export)

    mm = sub.add_parser("memory", help="記憶管理").add_subparsers(dest="sub", required=True)
    x = mm.add_parser("stats"); x.add_argument("character", nargs="?"); x.set_defaults(func=cmd_mem_stats)
    mm.add_parser("maintain").set_defaults(func=cmd_mem_maintain)
    x = mm.add_parser("add"); x.add_argument("character"); x.add_argument("content")
    x.add_argument("--kind", default="fact"); x.add_argument("--importance", type=float, default=0.6)
    x.set_defaults(func=cmd_mem_add)

    tk = sub.add_parser("task", help="タスク").add_subparsers(dest="sub", required=True)
    x = tk.add_parser("add"); x.add_argument("title"); x.add_argument("assignee")
    x.add_argument("--by", default="owner"); x.add_argument("--desc", default="")
    x.set_defaults(func=cmd_task_add)
    x = tk.add_parser("list"); x.add_argument("--assignee"); x.add_argument("--all", action="store_true")
    x.set_defaults(func=cmd_task_list)
    x = tk.add_parser("done"); x.add_argument("id", type=int); x.set_defaults(func=cmd_task_done)

    ap = sub.add_parser("approvals", help="承認キュー").add_subparsers(dest="sub", required=True)
    ap.add_parser("list").set_defaults(func=cmd_appr_list)
    x = ap.add_parser("approve"); x.add_argument("id", type=int)
    x.set_defaults(func=lambda a: cmd_appr_decide(a, True))
    x = ap.add_parser("reject"); x.add_argument("id", type=int)
    x.set_defaults(func=lambda a: cmd_appr_decide(a, False))

    sub.add_parser("daily", help="マネージャーの日次サイクルを実行").set_defaults(func=cmd_daily)
    sub.add_parser("report", help="状況レポート").set_defaults(func=cmd_report)

    x = sub.add_parser("guardian", help="ガーディアンで文章を検査")
    x.add_argument("text"); x.add_argument("--speaker"); x.add_argument("--no-judge", action="store_true")
    x.set_defaults(func=cmd_guardian_check)

    sub.add_parser("audit-verify", help="監査ログの改ざん検査").set_defaults(func=cmd_audit_verify)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return args.func(args) or 0
    except (KeyError, ValueError, RuntimeError) as e:
        print(f"エラー: {e}", file=sys.stderr)
        return 1
    finally:
        while _OPEN:
            _OPEN.pop().close()
