"""話題枠(hot bucket)の週次自動入れ替え (us_hot_refresh.py)。hot_refresh.py の移植・適応。

Yahoo Finance US の predefined screener JSON API (most_actives / day_gainers) 上位から、
監視ユニバース外の個別株(＝市場で今話題になっている銘柄)を検出して hot 枠に追加し、
話題でなくなった古い hot 銘柄を外す。S&P500(core)/Nasdaq-100(leader)は絶対に外さない。
追加/除外の根拠は詳細ログに記録し、ダッシュボードにも供給する。

JP版(hot_refresh.py)との差分:
  - データ源: JP版はYahoo Finance JapanのランキングHTMLを正規表現でスクレイピング
    していたが、米国版はYahoo Finance USのpredefined screener JSON API
    (v1/finance/screener/predefined/saved?scrIds=...) を使う。応答JSONの
    finance.result[0].quotes[] から symbol/shortName/quoteType を取り、
    quoteType=="EQUITY" のみ採用 (ETF/投信/指数連動は quoteType で機械的に除外できる
    ため、JP版のような名前ベースの除外リストが不要になった)。
  - JSON APIがクラム(crumb)要求等で401/403を返す可能性への備えとして、HTMLフォールバック
    は実装しない — 取得全滅時は前週の hot 枠を維持して正常終了する設計で十分と判断
    (JP版と同じ「落ちない」設計思想。無理にフォールバックを増やすと壊れ方が複雑になる)。

「話題」の操作的定義 (事実ベース):
  複数の市場ランキング(取引高上位・値上がり率)の上位に登場し、かつ現ユニバース外の
  普通株(EQUITY)。SNS感情等の曖昧な指標は使わず、「実際に資金と注目が集まっている」を
  数字で捉える。

追加ルール:
  ランキング上位(各RANK_TOP位まで)の、universe外・EQUITY銘柄を hot に追加。

除外ルール (hot枠のみ・leader/core は対象外。2026-10-21〜「話題枠に入る前の売買代金と比べる」):
  - ABSENT_WEEKS(4)週連続で全ランキング圏外、かつ 直近5営業日の売買代金 (日足の終値×出来高) が
    「話題枠に入る前の60営業日の日中央値×5」の KEEP_RATIO(2.5)倍未満、かつ ユニバース全体の上位
    KEEP_TOP_PCT(10)%に入っていない hot 銘柄を外す (= 入る前の水準まで冷めて、今も大きくない)
  - 日足が最新の営業日まで STOP_DAYS(5)営業日以上届いていない銘柄 (上場廃止・売買停止) は、圏外の週数に
    関係なく外す (今週ランキングに出ている銘柄は除く)
  - 1週に外すのは MAX_REMOVE(30)件まで、冷めた順 (データが止まった銘柄 → 入る前比の小さい順 → 入る前の水準が
    測れない銘柄)。残りは来週に持ち越す (判定の誤りがあっても一度に大量には外さない)
  - 入る前の水準が測れない銘柄 (上場して間もなく入る前の日足が20営業日に満たない・追加日の記録が無い) は、
    ユニバースの上位10%に入っているかだけで判定する
  - custom_groups.csv 掲載の恒久テーマ銘柄(半導体等)は保護し、絶対に外さない。指数構成銘柄は leader/core に戻す
  - 日足の最新日が STALE_DAYS(10)暦日より古い時と、leader/core でも日足が止まった銘柄が多い時 (5銘柄かつ2%超) は、
    データの障害とみなして日足を使う除外をしない (障害で一斉に外さない)
  旧ルール (〜2026-10-20) の「money_flow.csv の集中度 < 1.3」は、指数銘柄でも約9割が1.3未満になり、話題が
  続いているかを見分けていなかった (除外が実際には「圏外4週」だけで決まり、米国の話題枠は毎週増え続けた)。

us_universe_refresh.py(月次)は既存 universe.csv の hot コードを温存する設計なので、
本スクリプトが universe.csv の hot 行を書き換えれば月次再構築とも自然に協調する。
新規追加銘柄の sector は空(=不明)のままとし、group は次回の月次 us_universe_refresh が
GICSセクターを機械付与する。

依存なし(標準ライブラリのみ)。実データ検証は GitHub Actions ランナー上でのみ可能
(サンドボックスからは Yahoo へ proxy403 で到達不可)。

2026-10-03 修正 (JP版 hot_refresh.py と同じ再発防止):
  - 応答は取れたが銘柄を1件も解析できない場合はそのランキングを失敗扱いにする。
  - 圏外週カウントは同じ週に何度実行しても1回だけ数える (counted_week)。
  - 指数構成銘柄(index_members.csv)は話題枠から外しても leader/core 層へ戻すだけで、
    ユニバースからは消さない。同週再実行でも「今週の入れ替え」欄を空で上書きしない。

2026-10-05 追加 (入れ替えは週の最初の1回だけ。JP版と同じ):
  - SKIP_IF_DONE_THIS_WEEK=1 の時、今週 (ET) の入れ替えが済んでいれば何もせず終了する。workflow は
    「ブランチでの検証 push」と「手動実行で force を付けた時」以外は 1 を渡す。予定実行は数時間遅れて
    場中に動くことがあり、場中のランキングで当日の値上がり株まで追加してしまうため。決まった時刻の
    起動 (cron-job.org → workflow_dispatch) が週の最初の1回になり、予定実行はその予備になる。
"""
import csv
import json
import math
import os
import socket
import ssl
import statistics
import sys
import urllib.request
import urllib.error
from collections import Counter
from datetime import date, datetime, timezone
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")
DATA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "us_stocks")
UNIVERSE_FILE = os.path.join(DATA_DIR, "universe.csv")
CUSTOM_GROUPS_CSV = os.path.join(DATA_DIR, "custom_groups.csv")
HOT_STATE = os.path.join(DATA_DIR, "hot_state.json")
HOT_LOG = os.path.join(DATA_DIR, "hot_changes_log.csv")
HOT_LATEST = os.path.join(DATA_DIR, "hot_changes_latest.json")
# us_universe_refresh.py が書く指数構成銘柄一覧 (code,index_bucket)。話題枠から外す銘柄が
# 指数構成銘柄なら、ユニバースから消さずに leader/core へ戻すために使う
INDEX_MEMBERS_CSV = os.path.join(DATA_DIR, "index_members.csv")

FIELDNAMES = ["code", "name", "bucket", "sector", "group"]

# 調整パラメータ (環境変数で上書き可)
RANK_TOP = int(os.environ.get("RANK_TOP") or "30")        # 各ランキングの採用上位数
ABSENT_WEEKS = int(os.environ.get("ABSENT_WEEKS") or "4")  # 連続圏外で除外する週数
KEEP_RATIO = float(os.environ.get("KEEP_RATIO") or "2.5")  # 直近5営業日の売買代金が入る前の水準のこの倍以上なら残す
KEEP_TOP_PCT = float(os.environ.get("KEEP_TOP_PCT") or "10")  # 直近5営業日の売買代金がユニバースの上位この%なら残す (0で無効)
STOP_DAYS = int(os.environ.get("STOP_DAYS") or "5")        # この営業日数以上、日足が届いていなければ「データが止まった」
MAX_REMOVE = int(os.environ.get("MAX_REMOVE") or "30")     # 1週に外す上限 (冷めた順)
RECENT_DAYS, BASE_DAYS, BASE_MIN = 5, 60, 20               # 直近5営業日 / 入る前の60営業日の日中央値 (20日未満は測れない)
STALE_DAYS = 10                                            # 日足の最新日がこの暦日数より古ければ、日足を使う除外はしない
FETCH_DEADLINE_MIN = float(os.environ.get("FETCH_DEADLINE_MIN") or "5")
# 1 の時、今週の入れ替えが済んでいれば何もしない (入れ替えは週の最初の1回だけ)
SKIP_IF_DONE_THIS_WEEK = (os.environ.get("SKIP_IF_DONE_THIS_WEEK") or "") == "1"

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/125.0 Safari/537.36")

SCREENER_HOSTS = ("query1.finance.yahoo.com", "query2.finance.yahoo.com")
# (表示ラベル, scrId) — most_actives=取引高(売買代金)上位, day_gainers=値上がり率上位
SOURCES = [
    ("取引高上位", "most_actives"),
    ("値上がり率", "day_gainers"),
]
SCREENER_COUNT = 50


def _now_week():
    d = datetime.now(ET)
    y, w, _ = d.isocalendar()
    return f"{y}-W{w:02d}", d.strftime("%Y-%m-%d")


def fetch_screener(scr_id, count=SCREENER_COUNT, timeout=25):
    """predefined screener JSON APIから quotes[] を返す。両ホスト失敗で例外送出。"""
    last_err = None
    for host in SCREENER_HOSTS:
        url = (f"https://{host}/v1/finance/screener/predefined/saved"
               f"?scrIds={scr_id}&count={count}")
        req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=timeout, context=ssl.create_default_context()) as r:
                data = json.loads(r.read().decode())
            result = ((data.get("finance") or {}).get("result") or [])
            if not result:
                err = (data.get("finance") or {}).get("error")
                raise ValueError(f"no result (error={err})")
            return result[0].get("quotes") or []
        except Exception as e:
            last_err = e
            continue
    raise last_err


def parse_screener(quotes, top=RANK_TOP):
    """quotes[] から (code, name) を順位順・EQUITYのみ・重複排除で最大top件返す。
    ネットワーク非依存なのでローカルでも単体テスト可能。"""
    out, seen = [], set()
    for q in quotes:
        if (q.get("quoteType") or "").upper() != "EQUITY":
            continue
        code = (q.get("symbol") or "").strip().upper()
        if not code or code in seen:
            continue
        name = (q.get("shortName") or q.get("longName") or code).strip()
        seen.add(code)
        out.append((code, name))
        if len(out) >= top:
            break
    return out


def load_universe_rows():
    # universe.csv 未生成 (初回ブートストラップで us_universe_refresh.py より先に
    # 走った場合等) は traceback で落とさず、正直にスキップして正常終了する。
    # 2026-07-16 のブランチ検証で並走ブートストラップの FileNotFoundError を実測。
    if not os.path.exists(UNIVERSE_FILE):
        print(f"{UNIVERSE_FILE} が無い。先に us_universe_refresh.py を実行すること。"
              "話題枠の更新をスキップして終了する。")
        sys.exit(0)
    rows = []
    with open(UNIVERSE_FILE, encoding="utf-8") as f:
        for r in csv.DictReader(f):
            rows.append({k: (r.get(k) or "") for k in FIELDNAMES})
    return rows


def load_protected():
    """恒久テーマ銘柄(custom_groups.csv 掲載 = 半導体等)は自動除外しない。"""
    protected = set()
    if os.path.exists(CUSTOM_GROUPS_CSV):
        with open(CUSTOM_GROUPS_CSV, encoding="utf-8") as f:
            for r in csv.DictReader(f):
                c = (r.get("code") or "").strip()
                if c:
                    protected.add(c)
    return protected


def load_index_members():
    """{code: "leader"/"core"}。ファイルが無い(月次更新が未実行)場合は空 = 従来どおり削除。"""
    out = {}
    if os.path.exists(INDEX_MEMBERS_CSV):
        with open(INDEX_MEMBERS_CSV, encoding="utf-8") as f:
            for r in csv.DictReader(f):
                c, b = (r.get("code") or "").strip(), (r.get("index_bucket") or "").strip()
                if c and b in ("leader", "core"):
                    out[c] = b
    return out


def load_turnover(codes):
    """{code: {日付: 売買代金 (日足の終値×出来高)}}。日足ファイルが無い・読めない銘柄は空。"""
    out = {}
    for code in codes:
        m = {}
        try:
            with open(os.path.join(DATA_DIR, f"{code}_1d.csv"), encoding="utf-8") as f:
                for r in csv.DictReader(f):
                    try:
                        d = (r.get("timestamp_et") or "")[:10]
                        if d:
                            m[d] = float(r["close"]) * float(r.get("volume") or 0)
                    except (KeyError, TypeError, ValueError):
                        pass
        except OSError:
            pass
        out[code] = m
    return out


def activity(turnover, today):
    """除外の判定に使う日足の値をまとめて求める。
    営業日の暦は「半数以上の銘柄に日足がある日」(新しく入った銘柄の初期取得だけが先に進んでいても暦を進めない)。
    直近5営業日は「9割以上の銘柄に日足がある最新日」まで (取り込み途中の日で比べない)。
    返り値: ref=基準日 / recent={code: 直近5営業日の売買代金 (3日以上ある時だけ・無い日は平均で補う)} /
            lag={code: 最新の営業日まで日足が届いていない営業日数} / top=ユニバースの上位 KEEP_TOP_PCT% の境目 /
            stale=日足が古すぎて判定に使えない"""
    have = [m for m in turnover.values() if m]
    n = len(have)
    cnt = Counter(d for m in have for d in m)
    cal = sorted(d for d, k in cnt.items() if k * 2 >= n)
    full = [d for d in cal if cnt[d] >= 0.9 * n]
    ref = full[-1] if full else (cal[-1] if cal else None)
    win = [d for d in cal if ref and d <= ref][-RECENT_DAYS:]
    recent, lag = {}, {}
    for c, m in turnover.items():
        vals = [m[d] for d in win if d in m]
        recent[c] = statistics.fmean(vals) * RECENT_DAYS if len(vals) >= 3 else None
        last = max(m) if m else ""
        lag[c] = sum(1 for d in cal if d > last)
    ranked = sorted((v for v in recent.values() if v is not None), reverse=True)
    k = math.ceil(len(ranked) * KEEP_TOP_PCT / 100)
    top = ranked[k - 1] if ranked and k >= 1 else float("inf")
    stale = ref is None or (date.fromisoformat(today) - date.fromisoformat(ref)).days > STALE_DAYS
    return {"ref": ref, "recent": recent, "lag": lag, "top": top, "stale": stale}


def pre_entry_level(m, added):
    """話題枠に入る前 (追加日より前) の直近 BASE_DAYS 営業日の、日々の売買代金の中央値×5 (= 入る前の5営業日分の水準)。
    BASE_MIN 日に満たない (上場して間もない) か、追加日が分からない時は None。"""
    if not added:
        return None
    pre = [m[d] for d in sorted(m) if d < added][-BASE_DAYS:]
    if len(pre) < BASE_MIN:
        return None
    med = statistics.median(pre)
    return med * RECENT_DAYS if med > 0 else None


def load_added_dates():
    """hot_changes_log.csv から {code: 最後に追加された日}。hot_state.json に追加日が無い銘柄の補い。"""
    out = {}
    if os.path.exists(HOT_LOG):
        with open(HOT_LOG, encoding="utf-8") as f:
            for r in csv.DictReader(f):
                if r.get("action") == "add" and (r.get("code") or "").strip():
                    out[r["code"].strip()] = (r.get("date") or "").strip()
    return out


def judge(code, absent, added, act, turnover, stop_ok):
    """外すなら ("remove", 順番の鍵, 理由)、残すなら ("keep", 残す理由の分類, "")。
    順番の鍵が小さいほど先に外す (1週の上限 MAX_REMOVE を超えた分は来週に持ち越す)。"""
    lag = act["lag"].get(code, 0)
    if stop_ok and lag >= STOP_DAYS:
        if not turnover.get(code):
            return "remove", -1.0, "日足が1本も取れていない (ティッカーの変更・上場廃止の可能性) ため除外"
        return "remove", -1.0, f"日足が{lag}営業日届いていない (上場廃止・売買停止の可能性) ため除外"
    if absent < ABSENT_WEEKS:
        return "keep", "圏外の週数が足りない", ""
    if act["stale"]:
        return "keep", "日足が古いため判定を見送り", ""
    v = act["recent"].get(code)
    if v is None:
        return "keep", "直近の日足が足りないため判定を見送り", ""
    if v >= act["top"]:
        return "keep", f"売買代金がユニバースの上位{KEEP_TOP_PCT:g}%", ""
    base = pre_entry_level(turnover.get(code) or {}, added)
    head = f"{absent}週連続でランキング圏外・"
    tail = f"・監視銘柄の売買代金の上位{KEEP_TOP_PCT:g}%外のため除外"
    if base is None:
        return "remove", float("inf"), head + "話題枠に入る前の売買代金が測れない (上場して間もない等)" + tail
    ratio = v / base
    if ratio >= KEEP_RATIO:
        return "keep", f"入る前の{KEEP_RATIO:g}倍以上 (話題が続いている)", ""
    return "remove", ratio, head + f"直近5営業日の売買代金が話題枠に入る前の{ratio:.1f}倍 ({KEEP_RATIO:g}倍未満)" + tail


def load_state():
    if os.path.exists(HOT_STATE):
        try:
            return json.load(open(HOT_STATE, encoding="utf-8"))
        except (ValueError, OSError):
            return {}
    return {}


def main():
    t0 = datetime.now(timezone.utc)
    socket.setdefaulttimeout(35)
    week, today = _now_week()
    if SKIP_IF_DONE_THIS_WEEK:
        try:
            done = json.load(open(HOT_LATEST, encoding="utf-8"))
        except (OSError, ValueError):
            done = {}
        if done.get("week") == week:
            print(f"今週 ({week}) の入れ替えは {done.get('date')} に実行済みのため何もしない"
                  " (やり直す時は Actions の手動実行で force にチェック)")
            return

    # 1) ランキング取得 (取れたものだけ使う。全滅ならhot枠を維持して終了)
    trending = {}  # code -> {"name","sources":[...]}
    ok_sources = 0
    for label, scr_id in SOURCES:
        if (datetime.now(timezone.utc) - t0).total_seconds() > FETCH_DEADLINE_MIN * 60:
            print("デッドライン超過、以降のランキング取得を打ち切り")
            break
        try:
            pairs = parse_screener(fetch_screener(scr_id))
        except (urllib.error.URLError, urllib.error.HTTPError, socket.timeout,
                ssl.SSLError, ValueError, OSError) as e:
            print(f"  {label}: 取得失敗 {type(e).__name__} (スキップ)")
            continue
        if not pairs:
            # 応答は取れたがEQUITYを1件も解析できない = 応答形式の変更。成功扱いにすると
            # 全hot銘柄が「圏外」と数えられ数週後に一斉除外されるため失敗扱いにする。
            print(f"  {label}: ⚠️ 銘柄を1件も解析できなかった (応答形式変更の可能性) — このランキングは失敗扱い")
            continue
        ok_sources += 1
        for rank, (code, name) in enumerate(pairs, 1):
            t = trending.setdefault(code, {"name": name, "sources": []})
            t["sources"].append(f"{label}{rank}位")
        print(f"  {label}: {len(pairs)}銘柄取得")

    if ok_sources == 0:
        # 何も書き換えずに赤で終える (前週の hot枠はそのまま残る。従来は緑で気づけなかった)
        print("::error::全ランキングの取得/解析に失敗。hot枠を変更せず終了 (前週維持)。応答形式の変更を疑う")
        sys.exit(2)

    rows = load_universe_rows()
    protected = load_protected()
    index_members = load_index_members()
    state = load_state()

    existing_codes = {r["code"] for r in rows}
    hot_rows = [r for r in rows if r["bucket"] == "hot"]
    hot_codes = {r["code"] for r in hot_rows}
    # leader/core のコードは絶対に触らない

    added, removed = [], []
    keep_why = Counter()

    # 2) 追加: ランキング上位の universe外・EQUITY銘柄を hot に (上限なし)
    for code, info in trending.items():
        if code in existing_codes:
            # 既存hotが再登場 → 圏外カウントをリセット
            if code in hot_codes:
                state.setdefault(code, {})["absent"] = 0
                state[code]["last_seen"] = week
            continue
        # 新規追加 (sector/groupは次回月次のus_universe_refresh.pyがGICSセクターを付与)
        rows.append({"code": code, "name": info["name"], "bucket": "hot",
                     "sector": "", "group": ""})
        existing_codes.add(code); hot_codes.add(code)
        state[code] = {"absent": 0, "last_seen": week, "added": today,
                       "source": "・".join(info["sources"][:3])}
        added.append({"code": code, "name": info["name"],
                      "reason": f"{'・'.join(info['sources'][:3])} にランクイン(ユニバース外の話題株として新規採用)"})

    # 3) 圏外カウント更新 & 除外判定 (hot枠のみ・保護銘柄と話題が続いている銘柄は残す)
    trend_codes = set(trending)
    added_codes = {a["code"] for a in added}
    turnover = load_turnover([r["code"] for r in rows])
    act = activity(turnover, today)
    added_log = load_added_dates()
    non_hot = [r["code"] for r in rows if r["bucket"] != "hot"]
    core_stopped = sum(1 for c in non_hot if act["lag"].get(c, 0) >= STOP_DAYS)
    # leader/core でも日足が止まった銘柄が多い = 銘柄ではなく取得の障害。「データが止まった」で外さない
    stop_ok = not act["stale"] and core_stopped <= max(5, 0.02 * len(non_hot))
    print(f"除外の判定に使う日足: 基準日 {act['ref']} / 上位{KEEP_TOP_PCT:g}%の境目 "
          + (f"${act['top'] / 1e6:,.0f}M (直近5営業日)" if act["top"] != float("inf") else "なし")
          + f" / leader・coreで日足が止まった銘柄 {core_stopped}")
    if act["stale"]:
        print(f"::warning::日足の最新日 ({act['ref']}) が古いため、今週は日足を使う除外をしない (引け後処理の失敗を疑う)")
    elif not stop_ok:
        print(f"::warning::leader/core でも日足が止まった銘柄が {core_stopped} あるため、今週は「データが止まった」で外さない (取得の障害を疑う)")
    candidates = []   # (順番の鍵, 行, 理由)
    surviving = []
    for r in rows:
        if r["bucket"] != "hot":
            surviving.append(r)
            continue
        code = r["code"]
        st = state.setdefault(code, {"absent": 0, "last_seen": week})
        if code in trend_codes:
            st["absent"] = 0; st["last_seen"] = week
        elif code not in added_codes and st.get("counted_week") != week:
            # 同じ週に再実行(手動dispatch・push失敗後の再実行等)しても二重に数えない
            st["absent"] = st.get("absent", 0) + 1
            st["counted_week"] = week

        if code in protected:
            surviving.append(r); keep_why["恒久テーマ銘柄 (保護)"] += 1; continue
        if code in trend_codes or code in added_codes:
            surviving.append(r); keep_why["今週ランキングに登場"] += 1; continue
        verdict, key, reason = judge(code, st.get("absent", 0), st.get("added") or added_log.get(code),
                                     act, turnover, stop_ok)
        if verdict == "keep":
            surviving.append(r); keep_why[key] += 1; continue
        candidates.append((key, r, reason))

    # 冷めた順に、1週の上限まで外す (同じ週に既に外した分も数える)。残りは来週に持ち越す
    candidates.sort(key=lambda x: x[0])
    prev_removed = 0
    try:
        prev = json.load(open(HOT_LATEST, encoding="utf-8"))
        if prev.get("week") == week:
            prev_removed = len(prev.get("removed") or [])
    except (OSError, ValueError):
        pass
    budget = max(0, MAX_REMOVE - prev_removed - len(removed))
    for i, (key, r, reason) in enumerate(candidates):
        code = r["code"]
        if i >= budget:
            surviving.append(r); keep_why[f"1週の上限 ({MAX_REMOVE}件) のため来週に持ち越し"] += 1; continue
        if code in index_members:
            # 指数構成銘柄はユニバースから消さず、本来の層(leader/core)へ戻して監視を継続
            r["bucket"] = index_members[code]
            surviving.append(r)
            reason += f" (指数構成銘柄のため {index_members[code]} 層として監視は継続)"
        removed.append({"code": code, "name": r["name"], "reason": reason})
        state.pop(code, None)

    # 4) 書き出し (universe.csv は leader>core>hot 並びを維持)
    order = {"leader": 0, "core": 1, "hot": 2}
    surviving.sort(key=lambda r: (order.get(r["bucket"], 9), r["code"]))
    with open(UNIVERSE_FILE, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=FIELDNAMES)
        w.writeheader(); w.writerows(surviving)

    json.dump(state, open(HOT_STATE, "w", encoding="utf-8"), ensure_ascii=False, indent=0)

    # 変更ログ(追記) + 最新サマリー(ダッシュボード用)
    new_log = not os.path.exists(HOT_LOG)
    with open(HOT_LOG, "a", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        if new_log:
            w.writerow(["date", "week", "action", "code", "name", "reason"])
        for a in added:
            w.writerow([today, week, "add", a["code"], a["name"], a["reason"]])
        for rm in removed:
            w.writerow([today, week, "remove", rm["code"], rm["name"], rm["reason"]])

    hot_now = [r for r in surviving if r["bucket"] == "hot"]
    # 同じ週の再実行 (手動dispatch等) で「今週の入れ替え」欄が空で上書きされないよう、
    # 同週の既存サマリーがあれば追加/除外を合算する
    # (同じ週に「追加→除外」された銘柄は差し引きゼロなので両方から外す。新規銘柄の初期取得も行わない)
    latest_added, latest_removed = added, removed
    try:
        prev = json.load(open(HOT_LATEST, encoding="utf-8"))
        if prev.get("week") == week:
            a_codes, r_codes = {a["code"] for a in added}, {x["code"] for x in removed}
            prev_a = [a for a in prev.get("added", []) if a.get("code") not in a_codes]
            prev_r = [x for x in prev.get("removed", []) if x.get("code") not in r_codes]
            undone = {a.get("code") for a in prev_a} & r_codes      # 今週追加→今回除外
            redone = {x.get("code") for x in prev_r} & a_codes      # 今週除外→今回再追加
            latest_added = [a for a in prev_a + added if a.get("code") not in undone | redone]
            latest_removed = [x for x in prev_r + removed if x.get("code") not in undone | redone]
    except (OSError, ValueError):
        pass
    json.dump({"week": week, "date": today, "added": latest_added, "removed": latest_removed,
               "hot_total": len(hot_now)},
              open(HOT_LATEST, "w", encoding="utf-8"), ensure_ascii=False)
    # 今回の実行で追加した銘柄だけを workflow に渡す (初期履歴の取得とコミット対象用)。
    # 週の合算(latest)を使うと、同じ週の再実行で既に収集中の銘柄を取り直し、収集ジョブと衝突する
    out = os.environ.get("HOT_ADDED_OUT")
    if out:
        with open(out, "w", encoding="utf-8") as f:
            f.write(",".join(a["code"] for a in added))

    print(f"\n=== 話題枠 週次入れ替え {week} ({today}) ===")
    print(f"取得ランキング: {ok_sources}/{len(SOURCES)} / hot枠: {len(hot_now)}銘柄")
    print(f"追加 {len(added)}件:")
    for a in added:
        print(f"  + {a['code']} {a['name']} — {a['reason']}")
    print(f"除外 {len(removed)}件 (外す候補 {len(candidates)}件・1週の上限 {MAX_REMOVE}件):")
    for rm in removed:
        print(f"  - {rm['code']} {rm['name']} — {rm['reason']}")
    print("残した話題枠の銘柄の理由: " + (" / ".join(f"{k} {v}件" for k, v in keep_why.most_common()) or "なし"))


if __name__ == "__main__":
    main()
