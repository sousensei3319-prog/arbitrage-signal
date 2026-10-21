"""話題枠(hot bucket)の週次自動入れ替え (hot_refresh.py)。

Yahoo Finance Japan の「出来高」「値上がり率」ランキング上位から、監視ユニバース外の
個別株(＝市場で今話題になっている銘柄)を検出して hot 枠に追加し、話題でなくなった
古い hot 銘柄を外す。TOPIX500(core)/日経225(leader)は絶対に外さない。追加/除外の
根拠は詳細ログに記録し、ダッシュボードにも供給する。

「話題」の操作的定義 (事実ベース):
  複数の市場ランキング(出来高・値上がり率)の上位に登場し、かつ現ユニバース外の個別株。
  SNS感情等の曖昧な指標は使わず、「実際に資金と注目が集まっている」を数字で捉える。

追加ルール:
  ランキング上位(各RANK_TOP位まで)の、universe外・普通株(ETF/投信/REIT等を除外)を hot に追加。

除外ルール (hot枠のみ・leader/core は対象外。2026-10-21〜「話題枠に入る前の売買代金と比べる」+ 銘柄の大きさ):
  ABSENT_WEEKS(4)週連続で全ランキング圏外の hot 銘柄を、直近5営業日の1日平均の売買代金 (日足の終値×出来高) で判定する
  - 指数の銘柄 (leader/core) の KEEP_INDEX_PCT(50)%点 (=中央値) 以上 → 残す (大きい。指数の銘柄は大きさに関係なく残るのと揃える)
  - MIN_TURNOVER (1億円) 未満 → 外す (小さすぎる。入る前がほぼ売買ゼロの銘柄は倍率が大きく出て、話題の続きを見分けられない)
  - その間 → 話題枠に入る前 (最初に追加された日より前の60営業日) の1日あたり中央値の KEEP_RATIO(2.5)倍以上なら残す、
    未満なら外す。入る前が20営業日に満たない (上場して間もない)・追加日の記録が無い銘柄は外す
  - 日足が STOP_DAYS(5)営業日以上とだえ、その間の1分足も無い銘柄 (上場廃止・売買停止・名証/福証などだけの上場で東証の
    データが無い) は、圏外の週数に関係なく外す (今週ランキングに出ている銘柄は除く)。日足だけが欠けた銘柄 (引け後処理の
    取得が締め切りで打ち切られると、取得順で最後の hot だけが欠ける) は外さない
  - 1週に外すのは MAX_REMOVE(30)件まで、データがとだえた銘柄 → 1日の売買代金が少ない順。残りは翌週以降に回す
    (判定に誤りがあっても一度に大量には外さない)
  - 直近5営業日は「9割以上の銘柄に日足がある最新日」まで。その日の日足が無い・5日のうち3日に満たない銘柄は判定を見送る (残す)
  - custom_groups.csv 掲載の恒久テーマ銘柄(半導体等)は保護し、絶対に外さない。指数構成銘柄は leader/core に戻す
  - 日足の最新日が STALE_DAYS(10)暦日より古い時と、leader/core でも日足が止まった銘柄が多い時 (5銘柄かつ2%超) は、
    データの障害とみなして日足を使う除外をしない (障害で一斉に外さない)
  圏外の週数は「最後にランキングで見た週から何週たったか」を超えない (2026-W41 は入れ替えが2回動き、1回目にランキングで
  見た銘柄を2回目に圏外と数えていた)。
  旧ルール (〜2026-10-20) の「money_flow.csv の集中度 < 1.3」は、指数銘柄でも約9割が1.3未満になり、話題が続いているかを
  見分けていなかった (除外が実際には「圏外4週」だけで決まり、米国の話題枠は毎週約20件増えて336銘柄に)。入る前の倍率だけで
  判定する案は、反証 (2026-10-21) で入る前から大きかった銘柄を先に外し、ほぼ売買の無い銘柄を残すと分かったので、大きさを足した。

universe_refresh.py(月次)は既存 universe.csv の hot コードを温存する設計なので、
本スクリプトが universe.csv の hot 行を書き換えれば月次再構築とも自然に協調する。
新規追加銘柄の sector/group は空(=不明)のままとし、次回の月次 universe_refresh が
JPX33業種を機械付与する(週次でJPX一覧xlsを引くのは重いため)。

依存なし(標準ライブラリのみ)。実データ検証は GitHub Actions ランナー上でのみ可能
(サンドボックスからは Yahoo JP へ proxy403 で到達不可)。

2026-10-03 修正 (誤検出・取りこぼしの再発防止):
  - 銘柄の抽出を個別銘柄ページ(/quote/XXXX.T)へのリンクに限定。旧パターンはランキング
    最上部の広告/特集記事リンクを「出来高1位・値上がり率1位の銘柄」と誤認していた
    (2608・2609)。【】や先頭Newを含む名前も除外し、既にhot枠に入っている該当行は即時除外。
  - ページは取れたが銘柄を1件も解析できない場合はそのランキングを失敗扱いにする
    (成功扱いだと全hot銘柄が「圏外」と数えられ、HTML構造変更の数週後に一斉除外される)。
  - 圏外週カウントは同じ週に何度実行しても1回だけ数える (counted_week)。
  - 指数構成銘柄(index_members.csv)は話題枠から外しても leader/core 層へ戻すだけで、
    ユニバースからは消さない。

2026-10-05 追加 (入れ替えは週の最初の1回だけ):
  - SKIP_IF_DONE_THIS_WEEK=1 の時、今週の入れ替えが済んでいれば何もせず終了する。workflow は
    「ブランチでの検証 push」と「手動実行で force を付けた時」以外は 1 を渡す。GitHub の予定実行は
    数時間遅れて場中に動くことがあり (2026-10-05 は月曜 09:51 JST)、場中のランキングで当日の
    値上がり株まで追加していた。決まった時刻の起動 (cron-job.org → workflow_dispatch) が週の最初の
    1回になり、予定実行は cron-job.org が止まった週の予備になる。cron-job.org のジョブの時刻設定ミスや
    合鍵 (トークン) の悪用で何度も起動されても、2回目以降は何もしない。
"""
import csv
import difflib
import json
import os
import re
import socket
import ssl
import statistics
import sys
import unicodedata
import urllib.request
import urllib.error
from collections import Counter
from datetime import date, datetime, timezone, timedelta

JST = timezone(timedelta(hours=9))
DATA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "jp_stocks")
UNIVERSE_FILE = os.path.join(DATA_DIR, "universe.csv")
CUSTOM_GROUPS_CSV = os.path.join(DATA_DIR, "custom_groups.csv")
HOT_STATE = os.path.join(DATA_DIR, "hot_state.json")
HOT_LOG = os.path.join(DATA_DIR, "hot_changes_log.csv")
HOT_LATEST = os.path.join(DATA_DIR, "hot_changes_latest.json")
# universe_refresh.py が書く指数構成銘柄一覧 (code,index_bucket)。話題枠から外す銘柄が
# 指数構成銘柄なら、ユニバースから消さずに leader/core へ戻すために使う
INDEX_MEMBERS_CSV = os.path.join(DATA_DIR, "index_members.csv")
# universe_refresh.py が書く東証上場の普通株の正式名一覧 (code,name)。広告の見分けに使う
LISTED_NAMES_CSV = os.path.join(DATA_DIR, "listed_names.csv")

FIELDNAMES = ["code", "name", "bucket", "sector", "group"]
BUCKET_LABEL = {"leader": "けん引大型", "core": "主力"}   # ダッシュボードの区分の表示名 (template.html の BL と同じ)

# 調整パラメータ (環境変数で上書き可)
RANK_TOP = int(os.environ.get("RANK_TOP") or "30")        # 各ランキングの採用上位数
ABSENT_WEEKS = int(os.environ.get("ABSENT_WEEKS") or "4")  # 連続圏外で除外する週数
KEEP_RATIO = float(os.environ.get("KEEP_RATIO") or "2.5")  # 1日の売買代金が入る前のこの倍以上なら残す (話題が続いている)
KEEP_INDEX_PCT = float(os.environ.get("KEEP_INDEX_PCT") or "50")  # 指数の銘柄の売買代金のこの%点以上なら残す (大きい)
MIN_TURNOVER = float(os.environ.get("MIN_TURNOVER") or "1e8")    # 1日の売買代金がこれ (円) 未満なら倍率に関係なく外す
STOP_DAYS = int(os.environ.get("STOP_DAYS") or "5")        # この営業日数以上、日足も1分足も無ければ「データがとだえた」
MAX_REMOVE = int(os.environ.get("MAX_REMOVE") or "30")     # 1週に外す上限 (売買の少ない順)
RECENT_DAYS, BASE_DAYS, BASE_MIN = 5, 60, 20               # 直近5営業日 / 入る前の60営業日の日中央値 (20日未満は測れない)
STALE_DAYS = 10                                            # 日足の最新日がこの暦日数より古ければ、日足を使う除外はしない
FETCH_DEADLINE_MIN = float(os.environ.get("FETCH_DEADLINE_MIN") or "5")
# 1 の時、今週の入れ替えが済んでいれば何もしない (入れ替えは週の最初の1回だけ)
SKIP_IF_DONE_THIS_WEEK = (os.environ.get("SKIP_IF_DONE_THIS_WEEK") or "") == "1"

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/125.0 Safari/537.36")
CODE_RE = re.compile(r"^[0-9][0-9A-Za-z]{3}$")

# Yahoo JPの個別銘柄ページへのリンク (/quote/7203.T) 直後にリンクテキスト(銘柄名)が続くパターン。
# 【2026-10-03 修正】旧パターンは「/ + 英数4文字」で終わる任意のリンクを拾っていたため、
# ランキング最上部の広告・特集記事リンク (例: /…/2609 「【New】キオクシアや太陽誘電の売買は
# どう見極める」) を出来高1位・値上がり率1位の「銘柄」と誤認し、hot枠に追加していた
# (2026-08-10 の 2608、2026-09-21 の 2609)。個別銘柄ページ(/quote/)へのリンクだけに限定する。
PAIR_RE = re.compile(r"/quote/(\d[0-9A-Za-z]{3})(?:\.T)?/?[^>]*>\s*([^<\s][^<]{0,24})")
# 旧パターン: 除外したリンクをログに出して、判定が正しいかをランナーログで確認するためだけに使う
LEGACY_PAIR_RE = re.compile(r"/(\d[0-9A-Za-z]{3})/?[^>]*>\s*([^<\s][^<]{0,24})")
# 銘柄名として不自然なもの (広告・記事見出し)。東証の正式銘柄名に【】・読点・疑問符/感嘆符・
# 「提供」や先頭のNewは現れない。2026-10-03 のランナー実行で、銘柄ページ(/quote/5588.T)へ
# リンクする広告「SaaS過度懸念で売られた今、狙う成長株　提供:フ…」が出来高1位に入ったため追加
AD_NAME_RE = re.compile(r"[【】、？?！!]|提供|^\s*new\b", re.IGNORECASE)

RANKINGS = [
    ("出来高", "https://finance.yahoo.co.jp/stocks/ranking/volume?market=all&term=daily"),
    ("値上がり率", "https://finance.yahoo.co.jp/stocks/ranking/up?market=all&term=daily"),
]

# ETF/ETN/REIT/指数連動商品を名前で除外 (話題「個別株」だけを拾う)。
# 東証の普通株一覧 (listed_names.csv) にあるコードには当てない: 部分一致のため
# 「ダブル・スコープ」「ブルボン」「旭コンクリート工業」等の本物の個別株17銘柄を永久に弾いていた
EXCLUDE_NAME = ("ＥＴＦ", "ETF", "ＥＴＮ", "ETN", "投信", "上場", "ベア", "ブル",
                "レバレッジ", "インバース", "日経平均", "ＴＯＰＩＸ", "リート",
                "ＲＥＩＴ", "REIT", "指数", "連動", "投資法人")


def _now_week():
    d = datetime.now(JST)
    y, w, _ = d.isocalendar()
    return f"{y}-W{w:02d}", d.strftime("%Y-%m-%d")


def fetch(url, timeout=25):
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept-Language": "ja,en;q=0.8"})
    with urllib.request.urlopen(req, timeout=timeout, context=ssl.create_default_context()) as r:
        raw = r.read()
        enc = r.headers.get_content_charset() or "utf-8"
        return raw.decode(enc, errors="replace")


COMPANY_RE = re.compile(r"\(株\)|（株）|株式会社")


def is_ad_name(name):
    return bool(AD_NAME_RE.search(name or ""))


def _norm_name(s):
    """比較用の正規化: 全角→半角(NFKC)・(株)/株式会社・空白・中黒を除去・小文字化。"""
    s = unicodedata.normalize("NFKC", s or "")
    for t in ("(株)", "株式会社", "(有)", "(同)"):
        s = s.replace(t, "")
    return re.sub(r"[\s・･.,]", "", s).lower()


def names_match(scraped, official):
    """ランキング上の名前が東証の正式名と同じ会社を指しているか (表記ゆれ・途中切れは許容)。
    広告文は銘柄ページへリンクしていても正式名とは似ても似つかないので弾ける。"""
    a, b = _norm_name(scraped), _norm_name(official)
    if not a or not b:
        return True   # 判定材料が無いときは通す (ad表記チェックは別途かかる)
    if a in b or b in a:
        return True
    return difflib.SequenceMatcher(None, a, b).ratio() >= 0.5


def is_official_name(code, name, listed):
    """東証の正式名そのもの (全角半角・(株)の有無などの表記ゆれのみ) なら True。
    正式名に読点等を含む銘柄 (例: 9720「ホテル、ニューグランド」) を広告表記チェックで誤って弾かないため。"""
    official = (listed or {}).get(code)
    return bool(official) and _norm_name(name) == _norm_name(official)


def load_listed_names():
    """{code: 正式名}。ファイルが無い(月次更新が未実行)場合は空 = 名前照合はスキップ。"""
    out = {}
    if os.path.exists(LISTED_NAMES_CSV):
        with open(LISTED_NAMES_CSV, encoding="utf-8") as f:
            for r in csv.DictReader(f):
                c = (r.get("code") or "").strip()
                if c:
                    out[c] = (r.get("name") or "").strip()
    return out


def parse_ranking(html, top=RANK_TOP, listed=None, rejected=None):
    """ランキングHTMLから (code, name) を順位順・重複排除で最大top件返す。
    個別銘柄ページ(/quote/)へのリンクのみ採用し、ETF等・広告見出し風の名前・東証の正式名と
    食い違う名前 (listed={code: 正式名} が与えられた時) は除外する。名前は正式名に置き換える。
    除外した (code, name, 理由) は rejected リストへ追記。ネットワーク非依存で単体テスト可能。"""
    listed = listed or {}
    out, seen = [], set()
    for code, name in PAIR_RE.findall(html):
        name = name.strip()
        if not CODE_RE.match(code) or code in seen:
            continue
        if code not in listed and any(x in name for x in EXCLUDE_NAME):
            continue
        official = is_official_name(code, name, listed)
        reason = None
        if not official and is_ad_name(name):
            reason = "広告/記事見出し風の名前"
        elif listed and code not in listed and not COMPANY_RE.search(name):
            # 正式名一覧に無いコード (月次更新後の新規上場など) は名前照合ができないので、
            # 社名の形をしているものだけ採用する (広告文には (株)/株式会社 が付かない)
            reason = "東証の普通株一覧に無く、社名の形 ((株)/株式会社) でもない (広告の可能性)"
        elif code in listed and not official and not names_match(name, listed[code]):
            reason = f"東証の正式名「{listed[code]}」と一致しない (広告の可能性)"
        if reason:
            if rejected is not None and all(r[0] != code for r in rejected):
                rejected.append((code, name, reason))
            continue
        seen.add(code)
        out.append((code, listed.get(code) or name))
        if len(out) >= top:
            break
    return out


def rejected_links(html):
    """旧パターンなら銘柄として拾っていたが、今回は除外したリンク (code, name) の先頭10件。
    = 個別銘柄ページ(/quote/)以外へのリンク、または広告見出し風の名前。ランナーログ確認用。"""
    quote_ok = {c for c, n in PAIR_RE.findall(html) if not is_ad_name(n.strip())}
    out, seen = [], set()
    for code, name in LEGACY_PAIR_RE.findall(html):
        if code in quote_ok or code in seen or not CODE_RE.match(code):
            continue
        seen.add(code)
        out.append((code, name.strip()))
    return out[:10]


def load_universe_rows():
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


def fmt_money(v):
    """1億以上は億 (10億未満は小数2桁まで・末尾の0は省く)、それ未満は万で表す。例: 1億円・1.02億円・39.1億円・1,000万円"""
    if v < 1e8:
        return f"{v / 1e4:,.0f}万円"
    s = f"{v / 1e8:,.{2 if v < 1e9 else 1}f}"
    return (s.rstrip("0").rstrip(".") if "." in s else s) + "億円"


def weeks_between(a, b):
    """週の名前 (2026-W41) どうしの差 (週)。読めなければ None。"""
    try:
        return (date.fromisocalendar(int(b[:4]), int(b[6:]), 1) - date.fromisocalendar(int(a[:4]), int(a[6:]), 1)).days // 7
    except (TypeError, ValueError):
        return None


def load_turnover(codes):
    """{code: {日付: 売買代金 (日足の終値×出来高)}}。日足ファイルが無い・読めない銘柄は空。"""
    out = {}
    for code in codes:
        m = {}
        try:
            with open(os.path.join(DATA_DIR, f"{code}_T_1d.csv"), encoding="utf-8") as f:
                for r in csv.DictReader(f):
                    try:
                        d = (r.get("timestamp_jst") or "")[:10]
                        if d:
                            m[d] = float(r["close"]) * float(r.get("volume") or 0)
                    except (KeyError, TypeError, ValueError):
                        pass
        except OSError:
            pass
        out[code] = m
    return out


def last_1m_date(code):
    """1分足ファイルの最後の足の日付 (無い・読めない時は "")。日足だけが欠けた銘柄を「データがとだえた」と誤って外さないために見る。"""
    try:
        with open(os.path.join(DATA_DIR, f"{code}_T_1m.csv"), "rb") as f:
            f.seek(0, 2)
            f.seek(max(0, f.tell() - 1024))
            lines = f.read().decode("utf-8", "replace").strip().splitlines()
    except OSError:
        return ""
    return lines[-1][:10] if lines and lines[-1][:4].isdigit() else ""


def activity(turnover, today, index_codes):
    """除外の判定に使う日足の値をまとめて求める。
    営業日の暦 cal は「半数以上の銘柄に日足がある日」(新しく入った銘柄の初期取得だけが先に進んでいても暦を進めない)。
    直近5営業日 win は「9割以上の銘柄に日足がある最新日 ref」まで (取り込み途中の日で比べない)。
    返り値: avg={code: 直近5営業日の1日平均の売買代金。ref の日足があり、5日のうち3日以上ある時だけ} /
            lag={code: 最新の営業日まで日足が届いていない営業日数} / big=指数の銘柄 (index_codes) の KEEP_INDEX_PCT%点 /
            stale=日足が古すぎて判定に使えない"""
    have = [m for m in turnover.values() if m]
    n = len(have)
    cnt = Counter(d for m in have for d in m)
    cal = sorted(d for d, k in cnt.items() if k * 2 >= n)
    full = [d for d in cal if cnt[d] >= 0.9 * n]
    ref = full[-1] if full else (cal[-1] if cal else None)
    win = [d for d in cal if ref and d <= ref][-RECENT_DAYS:]
    avg, lag = {}, {}
    for c, m in turnover.items():
        vals = [m[d] for d in win if d in m]
        avg[c] = statistics.fmean(vals) if ref in m and len(vals) >= 3 else None
        last = max(m) if m else ""
        lag[c] = sum(1 for d in cal if d > last)
    idx = sorted(avg[c] for c in index_codes if avg.get(c) is not None)
    big = idx[min(len(idx) - 1, int(len(idx) * KEEP_INDEX_PCT / 100))] if idx else float("inf")
    stale = ref is None or (date.fromisoformat(today) - date.fromisoformat(ref)).days > STALE_DAYS
    return {"ref": ref, "win": win, "cal": cal, "avg": avg, "lag": lag, "big": big, "index_n": len(idx), "stale": stale}


def pre_entry_level(m, added):
    """話題枠に入る前 (追加日より前) の直近 BASE_DAYS 営業日の、1日あたりの売買代金の中央値。
    BASE_MIN 日に満たない (上場して間もない) か、追加日が分からない時は None。"""
    if not added:
        return None
    pre = [m[d] for d in sorted(m) if d < added][-BASE_DAYS:]
    if len(pre) < BASE_MIN:
        return None
    med = statistics.median(pre)
    return med if med > 0 else None


def load_added_dates():
    """hot_changes_log.csv から {code: 最初に追加された日}。入る前の水準は最初の追加より前で測る
    (再追加された銘柄の「入る前」に前回の話題の期間を混ぜない)。hot_state.json に追加日が無い銘柄の補いにもなる。"""
    out = {}
    if os.path.exists(HOT_LOG):
        with open(HOT_LOG, encoding="utf-8") as f:
            for r in csv.DictReader(f):
                if r.get("action") == "add" and (r.get("code") or "").strip():
                    out.setdefault(r["code"].strip(), (r.get("date") or "").strip())
    return out


def judge(code, absent, added, act, turnover, stop_ok):
    """外すなら ("remove", 順番の鍵, 理由)、残すなら ("keep", 残す理由の分類, "")。順番の鍵が小さいほど先に外す
    (データがとだえた銘柄 → 1日の売買代金が少ない順。1週の上限 MAX_REMOVE を超えた分は翌週以降)。"""
    lag = act["lag"].get(code, 0)
    if stop_ok and lag >= STOP_DAYS and last_1m_date(code) < act["cal"][-STOP_DAYS]:
        if not turnover.get(code):
            return "remove", -1.0, "日ごとの株価データ (日足) が1本も取れない (名古屋・福岡などの取引所だけに上場している可能性。原因は未確認)"
        return "remove", -1.0, f"日ごとの株価データ (日足) が{lag}営業日とだえた (原因は未確認: 買収・上場廃止・売買停止など)"
    if absent < ABSENT_WEEKS:
        return "keep", "圏外の週数が足りない", ""
    if act["stale"]:
        return "keep", "日足が古いため判定を見送り", ""
    v = act["avg"].get(code)
    if v is None:
        return "keep", "直近の日足が足りないため判定を見送り", ""
    if v >= act["big"]:
        return "keep", f"売買代金が指数の銘柄の{KEEP_INDEX_PCT:g}%点以上 (大きい)", ""
    head = f"圏外{absent}週・1日の売買代金 {fmt_money(v)}"
    if v < MIN_TURNOVER:
        return "remove", v, head + f" ({fmt_money(MIN_TURNOVER)}未満で小さすぎる)"
    base = pre_entry_level(turnover.get(code) or {}, added)
    if base is None:
        return "remove", v, head + ("・話題枠に入った日の記録が無く、入る前と比べられない" if not added
                                     else "・上場して間もなく、入る前の売買代金が無い")
    ratio = v / base
    if ratio >= KEEP_RATIO:
        return "keep", f"入る前の{KEEP_RATIO:g}倍以上 (話題が続いている)", ""
    return "remove", v, head + f"・話題枠に入る前の{ratio:.1f}倍"


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
    listed = load_listed_names()
    print(f"東証の正式名一覧: {len(listed)}銘柄" + ("" if listed else " (未生成のため名前照合はスキップ)"))
    for label, url in RANKINGS:
        if (datetime.now(timezone.utc) - t0).total_seconds() > FETCH_DEADLINE_MIN * 60:
            print("デッドライン超過、以降のランキング取得を打ち切り")
            break
        try:
            html = fetch(url)
        except (urllib.error.URLError, urllib.error.HTTPError, socket.timeout, ssl.SSLError) as e:
            print(f"  {label}: 取得失敗 {type(e).__name__} (スキップ)")
            continue
        rejected = []
        pairs = parse_ranking(html, listed=listed, rejected=rejected)
        for c, n, why in rejected:
            print(f"  {label}: 除外 {c} 「{n}」 — {why}")
        rej = rejected_links(html)
        if rej:
            print(f"  {label}: 銘柄ページ以外へのリンクとして除外(先頭{len(rej)}件): "
                  + " / ".join(f"{c} {n}" for c, n in rej))
        if not pairs:
            # ページは取れたが銘柄を1件も解析できない = HTML構造の変更。成功扱いにすると
            # 全hot銘柄が「圏外」と数えられ、数週後に一斉除外されてしまうため失敗扱いにする。
            print(f"  {label}: ⚠️ 銘柄を1件も解析できなかった (HTML構造変更の可能性) — このランキングは失敗扱い")
            continue
        ok_sources += 1
        for rank, (code, name) in enumerate(pairs, 1):
            t = trending.setdefault(code, {"name": name, "sources": []})
            t["sources"].append(f"{label}{rank}位")
        print(f"  {label}: {len(pairs)}銘柄取得 (上位: "
              + "・".join(f"{c} {n}" for c, n in pairs[:3]) + ")")

    if ok_sources == 0:
        # 何も書き換えずに赤で終える (前週の hot枠はそのまま残る)。従来は緑で終わり、Yahooの
        # ページ構成が変わって全滅しても気づけなかった
        print("::error::全ランキングの取得/解析に失敗。hot枠を変更せず終了 (前週維持)。ページ構成の変更を疑う")
        sys.exit(2)

    rows = load_universe_rows()
    protected = load_protected()
    index_members = load_index_members()
    state = load_state()

    existing_codes = {r["code"] for r in rows}
    hot_rows = [r for r in rows if r["bucket"] == "hot"]
    hot_codes = {r["code"] for r in hot_rows}
    # leader/core のコードは絶対に触らない
    non_hot_codes = existing_codes - hot_codes

    added, removed = [], []
    keep_why = Counter()

    # 2) 追加: ランキング上位の universe外・普通株を hot に (上限なし)
    ever_added = load_added_dates()
    for code, info in trending.items():
        if code in existing_codes:
            # 既存hotが再登場 → 圏外カウントをリセット
            if code in hot_codes:
                state.setdefault(code, {})["absent"] = 0
                state[code]["last_seen"] = week
            continue
        # ランキングは全市場が対象なので、名証・福証などだけに上場している銘柄も載る。それらは東証のコード (XXXX.T)
        # では取れず、日足が1本もできない (2026-10 に 2467・275A・3260 が毎回 404。3260 は7月にも追加→除外されていた)。
        # 前にも追加して日足が取れなかった、東証の普通株一覧に無いコードは、また追加しない (追加と除外の繰り返しを防ぐ)
        if (listed and code not in listed and code in ever_added
                and not os.path.exists(os.path.join(DATA_DIR, f"{code}_T_1d.csv"))):
            print(f"  追加しない {code} {info['name']} — 前にも追加したが東証のデータが取れなかった"
                  " (名証・福証などだけに上場している可能性)")
            continue
        # 新規追加
        rows.append({"code": code, "name": info["name"], "bucket": "hot",
                     "sector": "", "group": ""})  # sector/groupは次回月次でJPX付与
        existing_codes.add(code); hot_codes.add(code)
        state[code] = {"absent": 0, "last_seen": week, "added": today,
                       "source": "・".join(info["sources"][:3])}
        added.append({"code": code, "name": info["name"],
                      "reason": f"{'・'.join(info['sources'][:3])} にランクイン(ユニバース外の話題株として新規採用)"})

    # 3) 圏外カウント更新 & 除外判定 (hot枠のみ・保護銘柄と話題が続いている銘柄は残す)
    trend_codes = set(trending)
    added_codes = {a["code"] for a in added}
    turnover = load_turnover([r["code"] for r in rows])
    non_hot = [r["code"] for r in rows if r["bucket"] != "hot"]
    act = activity(turnover, today, non_hot)
    added_log = ever_added
    core_stopped = sum(1 for c in non_hot if act["lag"].get(c, 0) >= STOP_DAYS)
    # leader/core でも日足が止まった銘柄が多い = 銘柄ではなく取得の障害。「データがとだえた」で外さない
    stop_ok = not act["stale"] and core_stopped <= max(5, 0.02 * len(non_hot))
    print(f"除外の判定に使う日足: 直近5営業日 {act['win'][0] if act['win'] else '-'}〜{act['ref']} / 大きさで残す線 "
          f"(指数の銘柄{act['index_n']}の{KEEP_INDEX_PCT:g}%点) {fmt_money(act['big']) if act['index_n'] else 'なし'}/日 / "
          f"小さすぎる線 {fmt_money(MIN_TURNOVER)}/日 / leader・coreで日足が止まった銘柄 {core_stopped}")
    if act["stale"]:
        print(f"::warning::日足の最新日 ({act['ref']}) が古いため、今週は日足を使う除外をしない (引け後処理の失敗を疑う)")
    elif not stop_ok:
        print(f"::warning::leader/core でも日足が止まった銘柄が {core_stopped} あるため、今週は「データがとだえた」で外さない (取得の障害を疑う)")
    candidates = []   # (順番の鍵, 行, 理由)
    surviving = []
    for r in rows:
        if r["bucket"] != "hot":
            surviving.append(r)
            continue
        code = r["code"]
        # 過去に広告・記事リンクを誤検出して入った行 (例: 2609「【New】…」) は即時除外
        # (正式名に読点等を含む本物の銘柄は除外しない)
        if is_ad_name(r["name"]) and not is_official_name(code, r["name"], listed):
            removed.append({"code": code, "name": r["name"],
                            "reason": "ランキング上の広告/記事リンクを銘柄と誤認して追加された行のため除外"})
            state.pop(code, None)
            continue
        st = state.setdefault(code, {"absent": 0, "last_seen": week})
        if code in trend_codes:
            st["absent"] = 0; st["last_seen"] = week
        elif code not in added_codes and st.get("counted_week") != week and st.get("last_seen") != week:
            # 同じ週に再実行(手動dispatch・push失敗後の再実行等)しても二重に数えない。同じ週の前の実行でランキングに
            # 出ていた銘柄も数えない (2026-W41 は2回動き、1回目に見た銘柄を2回目に圏外と数えていた)
            st["absent"] = st.get("absent", 0) + 1
            st["counted_week"] = week
        # 圏外の週数は「最後にランキングで見た週から何週たったか」を超えない (W41 の数え過ぎもここで直る)
        gap = weeks_between(st.get("last_seen"), week)
        if gap is not None and 0 <= gap < st.get("absent", 0):
            st["absent"] = gap

        if code in protected:
            surviving.append(r); keep_why["恒久テーマ銘柄 (保護)"] += 1; continue
        if code in trend_codes or code in added_codes:
            surviving.append(r); keep_why["今週ランキングに登場"] += 1; continue
        first_added = min(filter(None, [st.get("added"), added_log.get(code)]), default="")
        verdict, key, reason = judge(code, st.get("absent", 0), first_added, act, turnover, stop_ok)
        if verdict == "keep":
            surviving.append(r); keep_why[key] += 1; continue
        candidates.append((key, r, reason))

    # データがとだえた銘柄 → 売買の少ない順に、1週の上限まで外す (同じ週に既に外した分も数える)。残りは翌週以降
    candidates.sort(key=lambda x: x[0])
    prev_removed = 0
    try:
        prev = json.load(open(HOT_LATEST, encoding="utf-8"))
        if prev.get("week") == week:
            prev_removed = len(prev.get("removed") or [])
    except (OSError, ValueError):
        pass
    budget = max(0, MAX_REMOVE - prev_removed - len(removed))
    carried = []
    for i, (key, r, reason) in enumerate(candidates):
        code = r["code"]
        if i >= budget:
            surviving.append(r); carried.append(code)
            keep_why[f"1週の上限 ({MAX_REMOVE}件) のため翌週以降に回す"] += 1; continue
        if code in index_members:
            # 指数構成銘柄はユニバースから消さず、本来の層(leader/core)へ戻して監視を継続
            r["bucket"] = index_members[code]
            surviving.append(r)
            reason += f" → 指数の銘柄なので「{BUCKET_LABEL[index_members[code]]}」に戻して監視を続ける"
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
    # rule/carried はダッシュボードの「今週の話題枠 入れ替え」欄の説明用 (外す条件・判定に使った期間・翌週以降に回した銘柄)
    rule = {"absent_weeks": ABSENT_WEEKS, "keep_ratio": KEEP_RATIO, "max_remove": MAX_REMOVE,
            "big": fmt_money(act["big"]) if act["index_n"] else "", "floor": fmt_money(MIN_TURNOVER),
            "from": act["win"][0] if act["win"] else "", "to": act["ref"] or "", "rank_top": RANK_TOP}
    json.dump({"week": week, "date": today, "added": latest_added, "removed": latest_removed,
               "hot_total": len(hot_now), "rule": rule, "carried": carried},
              open(HOT_LATEST, "w", encoding="utf-8"), ensure_ascii=False)
    # 今回の実行で追加した銘柄だけを workflow に渡す (初期履歴の取得とコミット対象用)。
    # 週の合算(latest)を使うと、同じ週の再実行で既に収集中の銘柄を取り直し、収集ジョブと衝突する
    out = os.environ.get("HOT_ADDED_OUT")
    if out:
        with open(out, "w", encoding="utf-8") as f:
            f.write(",".join(a["code"] for a in added))

    print(f"\n=== 話題枠 週次入れ替え {week} ({today}) ===")
    print(f"取得ランキング: {ok_sources}/{len(RANKINGS)} / hot枠: {len(hot_now)}銘柄")
    print(f"追加 {len(added)}件:")
    for a in added:
        print(f"  + {a['code']} {a['name']} — {a['reason']}")
    print(f"除外 {len(removed)}件 (外す候補 {len(candidates)}件・1週の上限 {MAX_REMOVE}件・翌週以降に回す {len(carried)}件):")
    for rm in removed:
        print(f"  - {rm['code']} {rm['name']} — {rm['reason']}")
    print("残した話題枠の銘柄の理由: " + (" / ".join(f"{k} {v}件" for k, v in keep_why.most_common()) or "なし"))


if __name__ == "__main__":
    main()
